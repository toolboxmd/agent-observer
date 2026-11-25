"""Check session visibility and whether the ledger is keeping up with native records.

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
import stat
import subprocess
import time
from contextlib import closing
from pathlib import Path

from .adapters import bare as bare_adapter
from .adapters.discovery import path_info, sqlite_mtime

CLAUDE_FIX = ("start the session with the parent's CLAUDE_CODE_*, CLAUDECODE, CLAUDE_PID "
              "and CLAUDE_EFFORT variables unset, or with CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1")
GRACE_SECONDS = 120
SCAN_TIMEOUT_SECONDS = 5
DEFAULT_SYNC_INTERVAL_SECONDS = 3600


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


def _checked_process(name: str, command: str) -> bool:
    """Whether a command is evaluated by the process visibility checks."""
    if name == "codex":
        return " exec" not in f" {command}" and "app-server" not in command
    return name == "opencode" and "serve" not in command


def process_table() -> list[dict]:
    """Live agent CLI processes with start time and working directory."""
    out = subprocess.run(["ps", "-Ao", "pid=,lstart=,command="],
                         capture_output=True, text=True, check=True,
                         timeout=SCAN_TIMEOUT_SECONDS,
                         env={**os.environ, "LC_ALL": "C"}).stdout
    if not out.strip():
        raise OSError("unparseable ps output: empty process table")
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid, command = parts[0], parts[6]
        name = os.path.basename(command.split()[0])
        if not _checked_process(name, command):
            continue
        try:
            pid = int(pid)
            if pid <= 0:
                raise ValueError("PID must be positive")
            started = time.mktime(time.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y"))
        except (ValueError, OverflowError) as exc:
            raise OSError("unparseable ps output: invalid PID or start time") from exc
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
            and _checked_process(p["name"], p["command"])]
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
            and _checked_process(p["name"], p["command"])]
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
    from .adapters import codex, grok
    with bare_adapter.one_read():
        return {codex.HARNESS: bare_adapter.discover(codex.HARNESS, codex._bare_home)[1],
                grok.HARNESS: bare_adapter.discover(grok.HARNESS, grok._sessions_under)[1]}


def _mtime(path: str) -> float | None:
    info = path_info(path)
    if info is not None and stat.S_ISREG(info.st_mode):
        return info.st_mtime
    return None


def _opencode_record_times(paths):
    for path in paths:
        uri = Path(path).absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=SCAN_TIMEOUT_SECONDS)) as native:
            # A message or part can advance without touching its session row.
            # Read only timestamps, following the same source boundary as sync.
            session_cols = {row[1] for row in native.execute("PRAGMA table_info(session)")}
            if "id" not in session_cols:
                raise sqlite3.DatabaseError("OpenCode session table unavailable")
            selects = [f"SELECT id AS session_id, {col} AS stamp FROM session"
                       for col in ("time_created", "time_updated") if col in session_cols]
            for table in ("message", "part"):
                cols = {row[1] for row in native.execute(f"PRAGMA table_info({table})")}
                if {"session_id", "time_updated"} <= cols:
                    selects.append(f"SELECT session_id, MAX(time_updated) AS stamp"
                                   f" FROM {table} GROUP BY session_id")
            if not selects:
                raise sqlite3.DatabaseError("OpenCode session timestamps unavailable")
            combined = " UNION ALL ".join(selects)
            for (stamp,) in native.execute(
                    f"WITH activity AS ({combined})"
                    " SELECT MAX(stamp) FROM activity JOIN session ON session.id = activity.session_id"
                    " GROUP BY session.id"):
                if isinstance(stamp, (int, float)):
                    yield stamp / 1000


def _native_record_times(harness, paths):
    from .adapters import grok

    if harness == "opencode":
        yield from _opencode_record_times(paths)
        return
    seen_files = set()
    for path in dict.fromkeys(paths):
        if harness == "grok":
            # One session directory can contain two independently imported logs.
            times = (_mtime(os.path.join(path, name))
                     for name in grok.SESSION_RECORD_FILES)
        elif harness == "codex":
            info = path_info(path)
            if info is None or not stat.S_ISREG(info.st_mode):
                continue
            identity = (info.st_dev, info.st_ino)
            if identity not in seen_files:
                seen_files.add(identity)
                yield info.st_mtime
            continue
        elif harness in ("t3", "router"):
            times = (sqlite_mtime(path),)
        else:
            times = (_mtime(path),)
        newest = max((stamp for stamp in times if stamp is not None), default=None)
        if newest is not None:
            yield newest


def _discover_native_paths(harness, adapter):
    from .adapters import router
    if harness == "t3":
        return [adapter.state_path(strict=True)]
    paths = adapter.discover(strict=True)
    if harness == "codex":
        paths.extend(router.discover_rollouts(strict=True))
    return paths


def _ledger_freshness(con, sync_interval_seconds, unavailable, ledger_error):
    from .adapters import available, grok

    imports = {}
    if con is not None:
        try:
            imports = dict(con.execute(
                "SELECT harness, MAX(imported_at) FROM sources GROUP BY harness"))
            metadata_import = grok.newest_metadata_import(con)
            if metadata_import is not None:
                recorded = imports.get("grok", metadata_import)
                imports["grok"] = max(recorded, metadata_import)
        except sqlite3.Error as exc:
            ledger_error = str(exc)
    if ledger_error is not None:
        unavailable.append({"check": "ledger freshness", "reason": ledger_error})

    freshness = {}
    for harness, adapter in available().items():
        imported = imports.get(harness)
        try:
            paths = _discover_native_paths(harness, adapter)
            records = list(_native_record_times(harness, paths))
        except (OSError, sqlite3.Error) as exc:
            unavailable.append({"check": f"{harness} native record scan", "reason": str(exc)})
            freshness[harness] = {"newest_native_at": None, "newest_import_at": imported,
                                  "newer_sources": None, "lag_seconds": None,
                                  "status": "unavailable"}
            continue

        newest = max(records, default=None)
        newer = lag = None
        if ledger_error is not None:
            status = "unavailable"
        elif newest is None:
            newer = 0
            status = "no_native_records"
        elif imported is None:
            newer = len(records)
            status = "never_synced"
        else:
            newer = sum(stamp > imported for stamp in records)
            lag = max(0, newest - imported)
            status = "stale" if lag > sync_interval_seconds else (
                "within_interval" if newer else "current")
        freshness[harness] = {"newest_native_at": newest, "newest_import_at": imported,
                              "newer_sources": newer, "lag_seconds": lag, "status": status}
    return freshness


def check(processes=None, registry=None, con=None, bare=None, *,
          sync_interval_seconds=DEFAULT_SYNC_INTERVAL_SECONDS, ledger_error=None) -> dict:
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
    from . import cue
    with bare_adapter.one_read():
        freshness = _ledger_freshness(con, sync_interval_seconds, unavailable, ledger_error)
        roots = bare_roots() if bare is None else bare
    conflicts = None
    if con is not None:
        try:
            conflicts = identity_conflicts(con)
        except sqlite3.Error as exc:
            unavailable.append({"check": "identity conflicts", "reason": str(exc)})
    return {"cue": cue.health(con), "findings": findings,
            "unavailable": unavailable,
            "identity_conflicts": conflicts,
            "bare_roots": roots,
            "ledger_freshness": freshness, "sync_interval_seconds": sync_interval_seconds,
            "checked": {"claude_sessions": len(registry),
                        "processes": len(processes)},
            "note": ("Codex app-server and exec sessions and OpenCode servers are not "
                     "checked by process: their records are matched by the router import.")}
