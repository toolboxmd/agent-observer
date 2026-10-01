"""Place each response of a session shared by several PRs on one PR (#43).

A T3 thread linked to several PRs binds its sessions whole to every one of
them. Within such a session, a response belongs to a PR when the paths its
tool calls name lie in a checkout of that PR's repository on that PR's head
branch. A response matching no PR, or more than one, stays unplaced and is
reported apart from every PR's headline.

Checkouts are recorded in the ledger when first seen, so placement still
works after a task's worktree is removed.
"""

from __future__ import annotations

import bisect
import os
import re
import sqlite3
import subprocess
import time

# Harnesses whose tool calls can be tied to the response that made them.
# Claude writes the tool_use in the response's own record, so a call
# belongs to the latest response at or before it. Codex reports usage
# after the response's items, so a call belongs to the next response.
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


def _known_checkouts(con) -> list:
    return con.execute(
        "SELECT root, repository, branch FROM checkouts ORDER BY length(root) DESC"
    ).fetchall()


def checkout_of(con, path: str, known: list | None = None,
                roots: dict | None = None):
    """(repository, branch) of the checkout holding path, or None.

    A checkout that still exists is read live through git and its record
    refreshed, so a reused path never keeps a stale repository or branch.
    Only a removed checkout falls back to its recorded mapping. Detached
    checkouts record no branch and so match no PR.
    """
    path = os.path.normpath(os.path.expanduser(path))
    probe = path
    while probe and probe != os.path.dirname(probe):
        if os.path.exists(os.path.join(probe, ".git")):
            return _live(con, probe, known, roots)
        probe = os.path.dirname(probe)
    for row in known if known is not None else _known_checkouts(con):
        if path == row["root"] or path.startswith(row["root"] + "/"):
            if not os.path.exists(row["root"]):
                return row["repository"], row["branch"]
    return None


def _write(con, sql: str, args: tuple) -> None:
    """Record a checkout; `task` and `publish` read a read-only ledger,
    where the live answer still applies and only the record is skipped."""
    try:
        con.execute(sql, args)
    except sqlite3.OperationalError:
        pass


def _live(con, root: str, known: list | None, roots: dict | None):
    if roots is not None and root in roots:
        return roots[root]
    remote = _git(root, "remote", "get-url", "origin") or ""
    match = REMOTE_RE.search(remote)
    found = None
    if match is not None:
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        branch = branch if branch and branch != "HEAD" else None
        found = (match.group(1), branch)
        _write(con, "INSERT OR REPLACE INTO checkouts(root, repository, branch, seen_at)"
               " VALUES(?,?,?,?)", (root, found[0], branch, time.time()))
        if known is not None:
            known[:] = [r for r in known if r["root"] != root]
            known.append({"root": root, "repository": found[0], "branch": branch})
            known.sort(key=lambda r: -len(r["root"]))
    else:
        # No recognizable origin now: drop the old mapping so it cannot
        # answer for this path after the checkout is removed.
        _write(con, "DELETE FROM checkouts WHERE root=?", (root,))
        if known is not None:
            known[:] = [r for r in known if r["root"] != root]
    if roots is not None:
        roots[root] = found
    return found


def task_heads(con, task_ids) -> dict:
    """task_id -> (repository, head branch) for linked PRs with a known head."""
    heads = {}
    for t in task_ids:
        repo, _, number = t.rpartition("#")
        row = con.execute(
            "SELECT head_branch FROM t3_links WHERE kind='pr' AND repository=?"
            " AND number=? AND head_branch IS NOT NULL LIMIT 1",
            (repo, int(number) if number.isdigit() else -1)).fetchone()
        if row is not None:
            heads[t] = (repo, row["head_branch"])
    return heads


def session_tasks(con, session_key: str) -> list:
    return [r["task_id"] for r in con.execute(
        "SELECT task_id FROM session_assignments WHERE session_key=?",
        (session_key,))]


def place_session(con, session_key: str) -> dict | None:
    """response_id -> task_id (or None when unplaced) for one shared session.

    None when the harness cannot tie tool calls to responses, so the
    session keeps whole-session sharing.
    """
    session = con.execute("SELECT harness, project_dir FROM sessions WHERE session_key=?",
                          (session_key,)).fetchone()
    if session is None or session["harness"] not in CALL_FOLLOWS_RESPONSE:
        return None
    follows = CALL_FOLLOWS_RESPONSE[session["harness"]]
    heads = task_heads(con, session_tasks(con, session_key))
    if not heads:
        return None
    by_head = {head: task for task, head in heads.items()}
    responses = con.execute(
        "SELECT response_id, ordinal_num FROM responses WHERE session_key=?"
        " AND is_overlap=0 AND ordinal_num IS NOT NULL ORDER BY ordinal_num",
        (session_key,)).fetchall()
    calls = con.execute(
        "SELECT ordinal_num, target FROM events WHERE session_key=? AND"
        " family IN ('tool_call', 'read', 'skill_read', 'file_change')"
        " AND ordinal_num IS NOT NULL ORDER BY ordinal_num", (session_key,)).fetchall()
    known = [dict(r) for r in _known_checkouts(con)]
    seen: dict = {}
    roots: dict = {}
    touched: dict = {r["response_id"]: set() for r in responses}
    ordinals = [r["ordinal_num"] for r in responses]
    for call in calls:
        owner = _owner(ordinals, call["ordinal_num"], follows)
        if owner is None:
            continue
        rid = responses[owner]["response_id"]
        for path in PATH_RE.findall(call["target"] or ""):
            if path not in seen:
                seen[path] = checkout_of(con, path, known, roots)
            found = seen[path]
            if found in by_head:
                touched[rid].add(by_head[found])
    return {rid: next(iter(tasks)) if len(tasks) == 1 else None
            for rid, tasks in touched.items()}


def _owner(ordinals: list, ordinal: int, follows: bool) -> int | None:
    """Index of the response that issued the call at ordinal."""
    if follows:
        i = bisect.bisect_right(ordinals, ordinal) - 1
        return i if i >= 0 else None
    i = bisect.bisect_left(ordinals, ordinal)
    return i if i < len(ordinals) else None


def record_shared_checkouts(con) -> int:
    """Record the checkouts of every session bound to several PRs.

    Runs during sync, while task worktrees still exist.
    """
    shared = [r["session_key"] for r in con.execute(
        "SELECT session_key FROM session_assignments GROUP BY session_key"
        " HAVING COUNT(DISTINCT task_id) > 1")]
    for key in shared:
        place_session(con, key)
    con.commit()
    return len(shared)
