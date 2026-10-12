"""Evidence across machines (#56): each machine exports, one place combines.

Every machine runs its own Observer over its own ledger; transcripts stay
on their machine and only aggregates travel. ``export`` writes one
document per machine: per-session model usage (the same buckets ``task
show`` reports, never summed across counter semantics), and per task its
outcome, identifiers and session keys. It carries no prompt or assistant text,
excerpts, titles, local paths or branch names; a task's project and URL
travel only as validated GitHub identifiers.

``pull`` fetches another machine's export over SSH and stores it beside
the ledger; ``combine`` joins the local export with stored ones. A
session found on several machines counts once and is listed; machines
expected but absent are listed. A task's combined usage is the whole
sessions touching it, each once, with the count shared with other tasks;
the exact per-task attribution stays with each machine's ``task show``
(about two seconds per task, too slow to export hundreds). Nothing runs
on a schedule.

Stdlib only.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import socket
import sqlite3
import subprocess
import tempfile

from . import __version__, report
from .publish import _GITHUB_URL_RE, _REPO_RE

SCHEMA = "agent-observer.fleet-export/1"
MACHINE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Task ids travel only as identifiers (repo#N, T-42, router ids); a free-text
# id stays home and is counted as withheld.
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#@+-]{0,199}$")
REMOTE_COMMAND = "agent-observer export"
# Usage fields an export keeps per model group; everything else stays home.
USAGE_FIELDS = ("harness", "model", "effort", "semantics", "responses",
                *report.BUCKETS, "unknown_counts")
SESSION_FIELDS = ("session_key", "harness", "parent_session_key",
                  "started_at", "ended_at", "agentsmd_version")


class PullError(RuntimeError):
    """A pull that stored nothing."""


def machine_name(host: str | None = None) -> str:
    """This machine's slug: $AGENT_OBSERVER_MACHINE, else the short hostname."""
    name = os.environ.get("AGENT_OBSERVER_MACHINE") or host \
        or socket.gethostname()
    return name.split(".")[0].lower()


def _iso(epoch) -> str | None:
    if not isinstance(epoch, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _usage(models: list) -> list:
    return [{k: m.get(k) for k in USAGE_FIELDS if k in m} for m in models]


def _task_id(value) -> str | None:
    if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value):
        return None
    return value


def export(con: sqlite3.Connection, machine: str | None = None) -> dict:
    """One aggregate document for this machine's ledger."""
    machine = machine or machine_name()
    if not MACHINE_RE.fullmatch(machine):
        raise ValueError(f"invalid machine name: {machine!r}")
    by_session: dict[str, list] = {}
    for row in con.execute("SELECT * FROM responses"):
        by_session.setdefault(row["session_key"], []).append(row)
    sessions = []
    for row in con.execute("SELECT * FROM sessions ORDER BY session_key"):
        entry = {k: row[k] for k in SESSION_FIELDS}
        entry["usage"] = _usage(report.model_usage(
            by_session.get(row["session_key"], [])))
        sessions.append(entry)
    states = {r["task_id"]: r["acceptance_state"] for r in con.execute(
        "SELECT task_id, acceptance_state FROM outcomes")}
    tasks = []
    withheld = 0
    for task in con.execute("SELECT * FROM tasks ORDER BY task_id"):
        task_id = _task_id(task["task_id"])
        if task_id is None:
            withheld += 1
            continue
        project, url = task["project"], task["issue_url"]
        tasks.append({
            "task_id": task_id,
            "project": project if isinstance(project, str)
            and _REPO_RE.fullmatch(project) else None,
            "issue_url": url if isinstance(url, str)
            and _GITHUB_URL_RE.fullmatch(url) else None,
            "origin": task["origin"],
            "acceptance_state": states.get(task_id) or "unknown",
            "sessions": sorted(report.task_sessions(con, task_id))})
    synced = con.execute("SELECT MAX(imported_at) FROM sources").fetchone()[0]
    return {"schema": SCHEMA, "machine": machine,
            "observer_version": __version__, "exported_at": _now(),
            "ledger_synced_at": _iso(synced),
            "sessions": sessions, "tasks": tasks, "withheld_tasks": withheld}


def _clean(value) -> bool:
    """A string with no control characters: safe to print one per line."""
    return isinstance(value, str) and not any(ord(c) < 32 for c in value)


def _text_or_none(value) -> bool:
    return value is None or _clean(value)


def _count_or_none(value) -> bool:
    return value is None or (isinstance(value, int)
                             and not isinstance(value, bool))


def _valid_group(group) -> bool:
    if not isinstance(group, dict):
        return False
    if not all(_text_or_none(group.get(k))
               for k in ("harness", "model", "effort", "semantics")):
        return False
    if not all(_count_or_none(group.get(k))
               for k in ("responses", *report.BUCKETS)):
        return False
    unknown = group.get("unknown_counts")
    return unknown is None or (isinstance(unknown, dict) and all(
        _clean(k) and _count_or_none(v) for k, v in unknown.items()))


def validate(doc) -> dict:
    """An export document, or ValueError naming what is wrong.

    Another machine's export is untrusted input: every field ``combine``
    reads is type-checked here, so a stored export can never break a
    later report.
    """
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError("not an Agent Observer fleet export")
    if not isinstance(doc.get("machine"), str) \
            or not MACHINE_RE.fullmatch(doc["machine"]):
        raise ValueError(f"invalid machine name: {doc.get('machine')!r}")
    for key in ("sessions", "tasks"):
        if not isinstance(doc.get(key), list) \
                or not all(isinstance(x, dict) for x in doc[key]):
            raise ValueError(f"invalid {key} list")
    if not all(_text_or_none(doc.get(k)) for k in
               ("observer_version", "exported_at", "ledger_synced_at")):
        raise ValueError("invalid export header")
    withheld = doc.get("withheld_tasks", 0)
    if not _count_or_none(withheld) or (withheld or 0) < 0:
        raise ValueError("invalid withheld_tasks count")
    for session in doc["sessions"]:
        if not _clean(session.get("session_key")) \
                or not isinstance(session.get("usage", []), list) \
                or not all(_valid_group(g) for g in session.get("usage", [])):
            raise ValueError("invalid session entry")
    for task in doc["tasks"]:
        sessions = task.get("sessions", [])
        if _task_id(task.get("task_id")) is None \
                or not _text_or_none(task.get("acceptance_state")) \
                or not isinstance(sessions, list) \
                or not all(_clean(k) for k in sessions):
            raise ValueError("invalid task entry")
    return doc


def store_dir(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "fleet")


def stored(directory: str) -> list:
    """Every valid stored export, by machine name."""
    if not os.path.isdir(directory):
        return []
    docs = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                docs.append(validate(json.load(fh)))
        except (OSError, ValueError):
            continue
    return docs


def pull(host: str, directory: str, command: str = REMOTE_COMMAND,
         run=subprocess.run) -> dict:
    """Fetch ``host``'s export over SSH and store it as <machine>.json."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "--", host, command]
    proc = run(argv, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise PullError(f"{host}: ssh exit {proc.returncode}:"
                        f" {(proc.stderr or '').strip()[-300:]}")
    try:
        doc = validate(json.loads(proc.stdout))
    except ValueError as exc:
        raise PullError(f"{host}: {exc}") from None
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(directory, doc["machine"] + ".json"))
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    result = {"host": host, "machine": doc["machine"],
              "exported_at": doc.get("exported_at"),
              "sessions": len(doc["sessions"]), "tasks": len(doc["tasks"])}
    if doc["machine"] != host.rsplit("@", 1)[-1].split(".")[0].lower():
        result["warning"] = (f"{host} reported machine {doc['machine']!r};"
                             f" stored as {doc['machine']}.json")
    return result


def _add(totals: dict, usage: list, count_session: bool = False) -> None:
    """Sum model usage into per-semantics totals; unknown stays visible."""
    seen = set()
    for group in usage:
        sem = group.get("semantics") or "unknown"
        bucket = totals.setdefault(sem, {"responses": 0})
        if count_session and sem not in seen:
            bucket["sessions"] = bucket.get("sessions", 0) + 1
            seen.add(sem)
        bucket["responses"] += group.get("responses") or 0
        unknown = dict(group.get("unknown_counts") or {})
        for name in report.BUCKETS:
            if name not in group:
                continue
            value = group[name]
            if value is None:
                unknown[name] = unknown.get(name) or group.get("responses") or 1
                bucket.setdefault(name, None)
                continue
            bucket[name] = (bucket.get(name) or 0) + value
        for name, n in unknown.items():
            counts = bucket.setdefault("unknown_counts", {})
            counts[name] = counts.get(name, 0) + n


def combine(docs: list, expect: list | None = None) -> dict:
    """Join machine exports; each session counts once, gaps stay listed."""
    docs = sorted((validate(d) for d in docs), key=lambda d: d["machine"])
    owners: dict[str, list] = {}
    for doc in docs:
        for session in doc["sessions"]:
            owners.setdefault(session.get("session_key"), []).append(
                doc["machine"])
    duplicates = {k: sorted(set(v)) for k, v in owners.items()
                  if len(set(v)) > 1}
    totals: dict = {}
    counted: set = set()
    machines = []
    for doc in docs:
        own: dict = {}
        for session in doc["sessions"]:
            key = session.get("session_key")
            _add(own, session.get("usage") or [], count_session=True)
            if key in counted:
                continue
            counted.add(key)
            _add(totals, session.get("usage") or [], count_session=True)
        machines.append({
            "machine": doc["machine"],
            "observer_version": doc.get("observer_version"),
            "exported_at": doc.get("exported_at"),
            "ledger_synced_at": doc.get("ledger_synced_at"),
            "sessions": len(doc["sessions"]), "tasks": len(doc["tasks"]),
            "usage": own})
    usage_of: dict[str, list] = {}
    for doc in docs:
        for session in doc["sessions"]:
            usage_of.setdefault(session.get("session_key"),
                                session.get("usage") or [])
    tasks_of: dict[str, set] = {}
    for doc in docs:
        for task in doc["tasks"]:
            for key in task.get("sessions") or []:
                tasks_of.setdefault(key, set()).add(task.get("task_id"))
    tasks: dict[str, dict] = {}
    for doc in docs:
        for task in doc["tasks"]:
            entry = tasks.setdefault(task.get("task_id"), {
                "task_id": task.get("task_id"), "machines": [],
                "acceptance_states": {}, "keys": set()})
            entry["machines"].append(doc["machine"])
            entry["acceptance_states"][doc["machine"]] = \
                task.get("acceptance_state")
            entry["keys"] |= set(task.get("sessions") or [])
    for entry in tasks.values():
        keys = sorted(entry.pop("keys"))
        usage: dict = {}
        for key in keys:
            if key in usage_of:
                _add(usage, usage_of[key])
        entry.update({
            "sessions": len(keys),
            # Sessions also bound to another task: their usage appears in
            # every task they touch, so it is not this task's alone.
            "shared_sessions": sum(1 for k in keys if len(tasks_of[k]) > 1),
            "missing_sessions": [k for k in keys if k not in usage_of],
            "duplicate_sessions": [k for k in keys if k in duplicates],
            "session_usage": usage})
    present = [d["machine"] for d in docs]
    return {"machines": machines,
            "withheld_tasks": {d["machine"]: d.get("withheld_tasks") or 0
                               for d in docs},
            "missing": [m for m in (expect or []) if m not in present],
            "duplicate_sessions": [{"session_key": k, "machines": v}
                                   for k, v in sorted(duplicates.items())],
            "totals": totals,
            "tasks": [tasks[k] for k in sorted(tasks, key=str)]}


def _tokens(usage) -> str:
    if not usage:
        return "none"
    parts = []
    for sem, bucket in sorted(usage.items()):
        value = bucket.get("total_tokens")
        unknown = (bucket.get("unknown_counts") or {}).get("total_tokens")
        text = "unknown" if value is None else f"{value:,}"
        if unknown:
            text += f" (+{unknown} unknown)"
        parts.append(f"{text} [{sem}]")
    return "; ".join(parts)


def render(result: dict) -> str:
    lines = [f"machines: {len(result['machines'])}"]
    for m in result["machines"]:
        lines.append(f"- {m['machine']} {m['observer_version']} exported"
                     f" {m['exported_at']} (ledger synced"
                     f" {m['ledger_synced_at'] or 'unknown'}):"
                     f" {m['sessions']} sessions, {m['tasks']} tasks;"
                     f" tokens {_tokens(m['usage'])}")
    lines.append("missing machines: " + (", ".join(result["missing"])
                                         or "none"))
    for machine in result.get("skipped_stored") or []:
        lines.append(f"stored export named {machine} skipped: it is this"
                     " machine's name, so the live export was used")
    lines.append(f"sessions on several machines (counted once):"
                 f" {len(result['duplicate_sessions'])}")
    for d in result["duplicate_sessions"]:
        lines.append(f"- {d['session_key']}: {', '.join(d['machines'])}")
    lines.append(f"tokens, each session once: {_tokens(result['totals'])}")
    withheld = {m: n for m, n in result["withheld_tasks"].items() if n}
    lines.append(f"tasks: {len(result['tasks'])}" + (
        "; free-text ids kept home: " + ", ".join(
            f"{m} {n}" for m, n in sorted(withheld.items()))
        if withheld else ""))
    for t in result["tasks"]:
        states = ", ".join(f"{m}={s}" for m, s in
                           sorted(t["acceptance_states"].items()))
        lines.append(f"- {t['task_id']}: {states}; {t['sessions']} sessions"
                     f" ({t['shared_sessions']} shared with other tasks,"
                     f" {len(t['missing_sessions'])} not exported);"
                     f" whole-session tokens {_tokens(t['session_usage'])}")
    return "\n".join(lines)
