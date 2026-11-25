"""First-native-import checkout diagnostics for legacy T3 sessions.

The observation describes the checkout at import. Claude's call directory
and branch lack repository identity. Codex's Git identity is session-scoped.
Neither source proves the checkout where a specific historical call ran.
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
CHECKOUT_EVENT_FAMILIES = ("tool_call", "read", "skill_read", "file_change")
CHECKOUT_EVENT_PARAMS = ",".join("?" for _ in CHECKOUT_EVENT_FAMILIES)

PATH_RE = re.compile(r"(?<![\w.~/-])(?:~|/)[^\s'\"`;|&<>(){}\[\],=]*")
REMOTE_RE = re.compile(
    r"(?:(?:https?|ssh|git)://(?:[^/\s@]+@)?(?P<url_host>[a-z0-9.-]+(?::[0-9]+)?)/"
    r"|(?:[^/:\s@]+@)?(?P<scp_host>[a-z0-9.-]+):)"
    r"(?P<repository>[\w.-]+/[\w.-]+?)(?:\.git)?/?",
    re.IGNORECASE | re.ASCII,
)


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
                match = REMOTE_RE.fullmatch(remote)
                if match:
                    host = match["url_host"] or match["scp_host"]
                    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
                    roots[root] = (host.lower(), match["repository"].lower(),
                                   branch if branch != "HEAD" else None)
                else:
                    roots[root] = (None, None, None)
            return roots[root]
        root = os.path.dirname(root)
    return None, None, None


def _session_tasks(con, session_key):
    return con.execute(
        "SELECT a.task_id, a.evidence FROM session_assignments a WHERE a.session_key=?",
        (session_key,)).fetchall()


def _task_heads(con, task_ids):
    by_head = {}
    columns = {r["name"] for r in con.execute("PRAGMA table_info(t3_links)")}
    if not {"host", "head_branch"} <= columns:
        return by_head
    for task in task_ids:
        coordinate, separator, number = task.rpartition("#")
        parts = coordinate.lower().split("/")
        if not separator or len(parts) not in (2, 3):
            continue
        # T3 task IDs omit the host for GitHub and prefix it for other forges.
        host = parts[0] if len(parts) == 3 else "github.com"
        repo = "/".join(parts[-2:])
        for row in con.execute(
                "SELECT DISTINCT head_branch FROM t3_links WHERE kind='pr'"
                " AND host=? COLLATE NOCASE AND repository=? COLLATE NOCASE"
                " AND number=? AND head_branch<>''", (host, repo, number)):
            by_head.setdefault((host, repo, row["head_branch"]), set()).add(task)
    return by_head


def _has_occurrences(con, session_key: str) -> bool:
    return _has_table(con, "codex_occurrences") and con.execute(
        "SELECT 1 FROM codex_occurrences WHERE session_key=? LIMIT 1",
        (session_key,)).fetchone() is not None


def _occurrence_owners(con, session_key, live_ids):
    """Pair a call with the next live response in each witnessing source.

    A source with no following response supplies no witness. Disagreeing
    witnesses leave the call unpaired, regardless of import order.
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


def _path_keys(target):
    for path in PATH_RE.findall(target or ""):
        # Bind evidence to the safe target without persisting the path again.
        yield hashlib.sha256(path.encode()).digest(), path


def _session_paths(con, session_key, harness):
    """Current response ids and event/path keys with their response owners."""
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
            f" AND family IN ({CHECKOUT_EVENT_PARAMS})",
            (session_key, *CHECKOUT_EVENT_FAMILIES)):
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
        if rid is not None:
            for digest, _ in _path_keys(call["target"]):
                paths[call["id"], digest] = rid
    return paths


def _observations(con, session_key):
    return {(r["event_id"], r["path_digest"]):
            (r["host"], r["repository"], r["branch"])
            for r in con.execute(
                "SELECT o.* FROM events e JOIN event_checkouts o ON e.id=o.event_id"
                " WHERE e.session_key=? AND o.host<>'' AND o.repository<>''"
                " AND o.branch<>''", (session_key,))}


def checkout_observations(con, session_key: str) -> dict:
    """Read complete first-import coordinates and current PR candidates by response."""
    if not _has_table(con, "event_checkouts"):
        return {}
    session = con.execute("SELECT harness FROM sessions WHERE session_key=?",
                          (session_key,)).fetchone()
    if session is None or session["harness"] not in CALL_FOLLOWS_RESPONSE:
        return {}
    observations = _observations(con, session_key)
    if not observations:
        return {}
    heads = _task_heads(con, [row["task_id"] for row in _session_tasks(con, session_key)
                             if _is_legacy_link(row["evidence"])])
    by_response = {}
    for key, rid in _session_paths(con, session_key, session["harness"]).items():
        if key in observations:
            by_response.setdefault(rid, set()).add(observations[key])
    return {rid: [{"host": host, "repository": repo, "branch": branch,
                   "candidate_tasks": sorted(heads.get((host, repo, branch), ()))}
                  for host, repo, branch in sorted(checkouts)]
            for rid, checkouts in sorted(by_response.items())}


def _is_legacy_link(evidence):
    return evidence == "t3" or evidence.startswith("t3:")


def read_placements(con, session_key: str) -> dict | None:
    """Keep shared legacy PR usage unplaced wherever a PR advertises a head.

    None delegates shared context and other assignment types to the caller.
    Checkout diagnostics cannot grant ownership.
    """
    session = con.execute("SELECT harness FROM sessions WHERE session_key=?",
                          (session_key,)).fetchone()
    if session is None or session["harness"] not in CALL_FOLLOWS_RESPONSE:
        return None
    tasks = _session_tasks(con, session_key)
    if (len(tasks) >= 2 and all(_is_legacy_link(row["evidence"]) for row in tasks)
            and _task_heads(con, [row["task_id"] for row in tasks])):
        return {}
    return None


# Compatibility for callers using the original report helper.
place_session = read_placements


def observe_new_checkouts(con, events) -> None:
    """Capture diagnostics only for events inserted from new native records."""
    roots = {}
    observations = {}
    for event_id, target in events:
        for digest, path in _path_keys(target):
            observations[event_id, digest] = _checkout(path, roots)
    if not observations:
        return
    # The native import committed before running Git. An interruption can
    # leave a gap, which later imports cannot fill from a reused checkout.
    with con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS event_checkouts("
            " event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,"
            " path_digest BLOB NOT NULL, host TEXT, repository TEXT, branch TEXT,"
            " PRIMARY KEY(event_id,path_digest))")
        con.executemany(
            "INSERT OR IGNORE INTO event_checkouts(event_id,path_digest,host,repository,branch)"
            " SELECT ?,?,?,?,? WHERE EXISTS(SELECT 1 FROM events WHERE id=?)",
            ((*key, *checkout, key[0]) for key, checkout in observations.items()))


def record_shared_checkouts(con) -> int:
    """Return the T3 sync counter. Native import records checkout diagnostics."""
    return con.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM session_assignments"
        " GROUP BY session_key HAVING COUNT(*) > 1)").fetchone()[0]
