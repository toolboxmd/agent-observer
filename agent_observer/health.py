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
                         capture_output=True, text=True, check=False).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid, command = parts[0], parts[6]
        name = os.path.basename(command.split()[0]) if command.split() else ""
        if name not in ("opencode", "codex"):
            continue
        try:
            started = time.mktime(time.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            started = None
        rows.append({"pid": int(pid), "name": name, "command": command,
                     "started": started, "cwd": _cwd(int(pid))})
    return rows


def _cwd(pid: int) -> str | None:
    out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                         capture_output=True, text=True, check=False).stdout
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


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


def check(processes=None, registry=None) -> dict:
    processes = process_table() if processes is None else processes
    registry = claude_registry() if registry is None else registry
    findings = check_claude(registry) + check_opencode(processes) + check_codex(processes)
    return {"findings": findings,
            "checked": {"claude_sessions": len(registry),
                        "processes": len(processes)},
            "note": ("Codex app-server and exec sessions and OpenCode servers are not "
                     "checked by process: their records are matched by the router import.")}
