"""Legacy checkout placement for sessions shared by T3 PR links.

Sync records what checkout each tool path was observed in, with its event
and response. Reports match those observations against the current PR links.
A checkout path can be reused without changing an earlier observation.
"""

from __future__ import annotations

import bisect
import hashlib
import os
import re
import subprocess

from .db import _has_table

# Claude writes tool_use in the response record. Codex reports usage after
# the response's items.
CALL_FOLLOWS_RESPONSE = {"claude": True, "codex": False}

PATH_RE = re.compile(r"(?<![\w.~/-])(?:~|/)[^\s'\"`;|&<>(){}\[\],=]*")
REMOTE_RE = re.compile(r"[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def _git(root: str, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True,
                             text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _checkout(path: str, roots: dict) -> tuple:
    root = os.path.normpath(os.path.expanduser(path))
    while root != os.path.dirname(root):
        if os.path.exists(os.path.join(root, ".git")):
            if root not in roots:
                remote = _git(root, "remote", "get-url", "origin") or ""
                match = REMOTE_RE.search(remote)
                branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD") if match else None
                roots[root] = (match.group(1) if match else None,
                               branch if branch != "HEAD" else None)
            return roots[root]
        root = os.path.dirname(root)
    return None, None


def _legacy_session(con, session_key):
    rows = con.execute(
        "SELECT s.harness, a.evidence, a.task_id FROM sessions s"
        " JOIN session_assignments a USING(session_key) WHERE s.session_key=?",
        (session_key,)).fetchall()
    if (len(rows) < 2 or rows[0]["harness"] not in CALL_FOLLOWS_RESPONSE
            or any(r["evidence"] != "t3" and not r["evidence"].startswith("t3:")
                   for r in rows)):
        return None
    return rows[0]["harness"], [r["task_id"] for r in rows]


def _task_heads(con, task_ids):
    by_head = {}
    if "head_branch" not in {r["name"] for r in con.execute("PRAGMA table_info(t3_links)")}:
        return by_head
    for task in task_ids:
        repo, _, number = task.rpartition("#")
        for row in con.execute(
                "SELECT DISTINCT head_branch FROM t3_links WHERE kind='pr'"
                " AND repository=? AND number=? AND head_branch IS NOT NULL",
                (repo, number)):
            by_head.setdefault((repo, row["head_branch"]), set()).add(task)
    return by_head


def _has_occurrences(con, session_key: str) -> bool:
    return _has_table(con, "codex_occurrences") and con.execute(
        "SELECT 1 FROM codex_occurrences WHERE session_key=? LIMIT 1",
        (session_key,)).fetchone() is not None


def _occurrence_owners(con, session_key, live_ids):
    """A call's owner is the next live response in each witnessing source.

    A source with no following response supplies no witness. Disagreeing
    witnesses leave the call unplaced, regardless of import order.
    """
    witnesses = {}
    sources = con.execute(
        "SELECT DISTINCT source_id FROM codex_occurrences WHERE session_key=?",
        (session_key,))
    for source in sources:
        following = None
        for row in con.execute(
                "SELECT row_kind, row_key FROM codex_occurrences"
                " WHERE session_key=? AND source_id=? AND row_kind IN ('response','event')"
                " ORDER BY record_position DESC, row_kind DESC",
                (session_key, source["source_id"])):
            if row["row_kind"] == "response":
                if row["row_key"] in live_ids:
                    following = row["row_key"]
            elif following is not None:
                witnesses.setdefault(row["row_key"], set()).add(following)
    return {event: next(iter(owners)) for event, owners in witnesses.items()
            if len(owners) == 1}


def _session_paths(con, session_key, harness):
    """Current response ids and tool paths with their native call owners."""
    responses = con.execute(
        "SELECT response_id, ordinal_num FROM responses WHERE session_key=?"
        " AND is_overlap=0 ORDER BY ordinal_num", (session_key,)).fetchall()
    ids = [r["response_id"] for r in responses]
    timed = [r for r in responses if r["ordinal_num"] is not None]
    ordinals = [r["ordinal_num"] for r in timed]
    owners = None
    if harness == "codex" and _has_occurrences(con, session_key):
        owners = _occurrence_owners(con, session_key, set(ids))
    paths = {}
    for call in con.execute(
            "SELECT id, ordinal_num, target FROM events WHERE session_key=?"
            " AND family IN ('tool_call','read','skill_read','file_change')",
            (session_key,)):
        rid = None
        if owners is not None:
            rid = owners.get(str(call["id"]))
        elif call["ordinal_num"] is not None:
            if CALL_FOLLOWS_RESPONSE[harness]:
                index = bisect.bisect_right(ordinals, call["ordinal_num"]) - 1
            else:
                index = bisect.bisect_left(ordinals, call["ordinal_num"])
            if 0 <= index < len(timed):
                rid = timed[index]["response_id"]
        for path in PATH_RE.findall(call["target"] or ""):
            # Bind the observation to this path without retaining tool inputs.
            digest = hashlib.sha256(path.encode()).digest()
            paths[call["id"], digest] = (rid, path)
    return ids, paths


def _observations(con, session_key):
    return {(r["event_id"], r["path_digest"]):
            (r["response_id"], r["repository"], r["branch"])
            for r in con.execute(
                "SELECT o.* FROM events e JOIN response_checkouts o ON e.id=o.event_id"
                " WHERE e.session_key=?", (session_key,))}


def read_placements(con, session_key: str) -> dict | None:
    """Read response_id -> sole matching PR, or None for an unplaced response.

    None leaves whole-session sharing to the caller. An empty map leaves
    every response unplaced on a ledger without observations. Only current
    call/response pairings count, even between syncs.
    """
    legacy = _legacy_session(con, session_key)
    if legacy is None:
        return None
    harness, tasks = legacy
    if not _has_table(con, "response_checkouts"):
        return {}
    heads = _task_heads(con, tasks)
    if not heads:
        return None
    ids, paths = _session_paths(con, session_key, harness)
    observations = _observations(con, session_key)
    matches = {rid: set() for rid in ids}
    pending = set()
    for key, (rid, _) in paths.items():
        if rid is None:
            continue
        observed = observations.get(key)
        if observed is None or observed[0] != rid:
            pending.add(rid)
        else:
            matches[rid].update(heads.get(observed[1:], ()))
    return {rid: next(iter(owners)) if rid not in pending and len(owners) == 1 else None
            for rid, owners in matches.items()}


# Compatibility for callers using the original report helper.
place_session = read_placements


def record_shared_checkouts(con) -> int:
    """Observe each event/path once. Reimports can revise its response witness."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS response_checkouts("
        " event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,"
        " path_digest BLOB NOT NULL, response_id TEXT, repository TEXT, branch TEXT,"
        " PRIMARY KEY(event_id, path_digest))")
    shared = [r[0] for r in con.execute(
        "SELECT session_key FROM session_assignments GROUP BY session_key HAVING COUNT(*) > 1")]
    con.commit()
    roots = {}
    for session_key in shared:
        legacy = _legacy_session(con, session_key)
        if legacy is None:
            continue
        harness, _ = legacy
        _, paths = _session_paths(con, session_key, harness)
        observations = _observations(con, session_key)
        changed = []
        for key, (rid, path) in paths.items():
            observed = observations.get(key)
            if observed is None:
                repo, branch = _checkout(path, roots)
            elif observed[0] != rid:
                repo, branch = observed[1:]
            else:
                continue
            changed.append((*key, rid, repo, branch))
        # No git subprocess runs under the ledger write lock. On a racing
        # sync the first checkout observation wins, including a missing one.
        with con:
            con.executemany(
                "INSERT INTO response_checkouts(event_id,path_digest,response_id,repository,branch)"
                " VALUES(?,?,?,?,?) ON CONFLICT(event_id,path_digest) DO UPDATE"
                " SET response_id=excluded.response_id", changed)
    return len(shared)
