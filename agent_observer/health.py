"""Session visibility: find live agent sessions whose records Observer cannot see.

A Claude Code session started from inside another one inherits the parent's
child-session marker and saves no transcript unless the launcher scrubs the
parent's CLAUDE_CODE_* variables or sets CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1.
Such a session is invisible to every reader. Claude Code lists running
sessions in ~/.claude/sessions/<pid>.json, so the check compares that registry
with the transcript directory. OpenCode and Codex CLI processes are checked
against their own stores by working directory and start time.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import subprocess
import time

CLAUDE_FIX = ("start the session with the parent's CLAUDE_CODE_*, CLAUDECODE, CLAUDE_PID "
              "and CLAUDE_EFFORT variables unset, or with CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1")
GRACE_SECONDS = 120
SCAN_TIMEOUT_SECONDS = 5


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def claude_registry(root: str | None = None) -> list[dict]:
    root = root or os.path.expanduser("~/.claude/sessions")
    entries = []
    for path in glob.glob(os.path.join(root, "*.json")):
        try:
            with open(path) as fh:
                entry = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and entry.get("pid") and entry.get("sessionId"):
            entries.append(entry)
    return entries


def check_claude(registry: list[dict], projects_root: str | None = None,
                 alive=_alive, now: float | None = None) -> list[dict]:
    projects_root = projects_root or os.path.expanduser("~/.claude/projects")
    now = now if now is not None else time.time()
    findings = []
    for entry in registry:
        pid = int(entry["pid"])
        if not alive(pid):
            continue
        started = (entry.get("startedAt") or 0) / 1000.0
        if started and now - started < GRACE_SECONDS:
            continue
        pattern = os.path.join(projects_root, "*", f"{entry['sessionId']}.jsonl")
        if glob.glob(pattern):
            continue
        findings.append({
            "host": "claude", "pid": pid, "cwd": entry.get("cwd"),
            "session": entry["sessionId"], "kind": entry.get("kind"),
            "problem": "running session has no transcript: it is invisible to Observer",
            "fix": CLAUDE_FIX})
    return findings


def process_table() -> list[dict]:
    """Live agent CLI processes with start time and working directory."""
    out = subprocess.run(["ps", "-Ao", "pid=,lstart=,command="],
                         capture_output=True, text=True, check=True,
                         timeout=SCAN_TIMEOUT_SECONDS).stdout
    if not out.strip():
        raise OSError("unparseable ps output: empty process table")
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            raise OSError("unparseable ps output: incomplete process row")
        pid, command = parts[0], parts[6]
        try:
            pid = int(pid)
            if pid <= 0:
                raise ValueError("PID must be positive")
            started = time.mktime(time.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y"))
        except (ValueError, OverflowError) as exc:
            raise OSError("unparseable ps output: invalid PID or start time") from exc
        name = os.path.basename(command.split()[0])
        if name not in ("opencode", "codex"):
            continue
        row = {"pid": pid, "name": name, "command": command,
               "started": started, "cwd": None}
        try:
            row["cwd"] = _cwd(pid)
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            row["cwd_error"] = _scan_error(exc)
        rows.append(row)
    return rows


def _scan_error(exc) -> str:
    return str(exc) + (f": {exc.stderr.strip()}" if getattr(exc, "stderr", None) else "")


def _cwd(pid: int) -> str:
    out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                         capture_output=True, text=True, check=True,
                         timeout=SCAN_TIMEOUT_SECONDS).stdout
    cwd = None
    for line in out.splitlines():
        if line.startswith("n/"):
            cwd = line[1:]
        elif not (line.startswith("p") and line[1:].isdigit() or line == "fcwd"):
            raise OSError("unparseable lsof output: invalid working-directory record")
    if cwd is not None:
        return cwd
    raise OSError("lsof returned no working directory (process exited or access unavailable)")


def check_opencode(processes: list[dict], db_path: str | None = None) -> list[dict]:
    db_path = db_path or os.path.expanduser("~/.local/share/opencode/opencode.db")
    findings = []
    live = [p for p in processes if p["name"] == "opencode" and p.get("cwd")
            and "serve" not in p["command"]]
    if not live:
        return findings
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        con = None
    for proc in live:
        seen = None
        if con is not None:
            row = con.execute("SELECT MAX(time_updated) FROM session WHERE directory=?",
                              (proc["cwd"],)).fetchone()
            seen = row[0] / 1000.0 if row and row[0] else None
        if seen is None or (proc.get("started") and seen < proc["started"]):
            findings.append({
                "host": "opencode", "pid": proc["pid"], "cwd": proc["cwd"],
                "problem": "running OpenCode process has no session record since it started",
                "fix": "keep OpenCode's data directory reachable (no XDG_DATA_HOME override)"})
    if con is not None:
        con.close()
    return findings


def check_codex(processes: list[dict], sessions_root: str | None = None) -> list[dict]:
    sessions_root = sessions_root or os.path.expanduser("~/.codex/sessions")
    findings = []
    live = [p for p in processes if p["name"] == "codex" and p.get("cwd")
            and " exec" not in f" {p['command']}" and "app-server" not in p["command"]]
    for proc in live:
        newest = 0.0
        for path in glob.glob(os.path.join(sessions_root, "**", "rollout-*.jsonl"),
                              recursive=True):
            mtime = os.path.getmtime(path)
            if proc.get("started") and mtime < proc["started"]:
                continue
            try:
                with open(path) as fh:
                    meta = json.loads(fh.readline()).get("payload") or {}
            except (OSError, ValueError):
                continue
            if meta.get("cwd") == proc["cwd"]:
                newest = max(newest, mtime)
        if not newest:
            findings.append({
                "host": "codex", "pid": proc["pid"], "cwd": proc["cwd"],
                "problem": "running Codex CLI has no rollout since it started",
                "fix": "keep CODEX_HOME's sessions directory reachable"})
    return findings


def identity_conflicts(con) -> list[dict]:
    """Stored user-message identity gaps a privacy replay could not resolve."""
    return [dict(row) for row in con.execute(
        "SELECT harness, source_path, ordinal_num FROM import_errors"
        " WHERE error='identity_conflict' ORDER BY harness, source_path, ordinal_num")]


def bare_roots() -> dict:
    """Chromeria bare receipts per harness and their unreadable roots (#92).

    Reported beside the findings, never as one: a gap is a record root, not
    a running session.
    """
    from .adapters import bare, codex, grok
    with bare.one_read():
        return {codex.HARNESS: bare.discover(codex.HARNESS, codex._bare_home)[1],
                grok.HARNESS: bare.discover(grok.HARNESS, grok._sessions_under)[1]}


def check(processes=None, registry=None, con=None, bare=None) -> dict:
    unavailable = []
    if processes is None:
        try:
            processes = process_table()
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            processes = []
            unavailable.append({"check": "process scan", "reason": _scan_error(exc)})
    for proc in processes:
        if proc.get("cwd_error"):
            unavailable.append({"check": "working directory", "host": proc["name"],
                                "pid": proc["pid"], "reason": proc["cwd_error"]})
    registry = claude_registry() if registry is None else registry
    findings = check_claude(registry) + check_opencode(processes) + check_codex(processes)
    return {"findings": findings,
            "unavailable": unavailable,
            "identity_conflicts": identity_conflicts(con) if con is not None else None,
            "bare_roots": bare_roots() if bare is None else bare,
            "checked": {"claude_sessions": len(registry),
                        "processes": len(processes)},
            "note": ("Codex app-server and exec sessions and OpenCode servers are not "
                     "checked by process: their records are matched by the router import.")}
