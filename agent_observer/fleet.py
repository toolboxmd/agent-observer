"""Evidence across machines (#56): each machine exports, one place combines.

Every machine runs its own Observer over its own ledger; transcripts stay
on their machine and only aggregates travel. ``export`` writes one
document per machine: per-session model usage (the same buckets ``task
show`` reports, never summed across counter semantics), and per task its
outcome, identifiers and session keys. It carries no prompt or assistant text,
excerpts, titles, local paths or branch names; a task's project and URL
travel only as validated GitHub identifiers. Judge evidence (#60) travels
as moment counts by origin and kind, and per judge its verdict counts,
coverage and groups: group id, the judge's one-line problem and each
judged-yes moment's id, session, origin and time, so ``combine`` can merge
groups across machines and count a moment seen twice once. Moment and
transcript text never leave.

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
import unicodedata

from . import __version__, report
from . import moments as _moments
from . import verdicts as _verdicts
from .publish import _GITHUB_URL_RE, _REPO_RE

SCHEMA = "agent-observer.fleet-export/1"
MACHINE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Task ids travel only as identifiers (repo#N, T-42, router ids); a free-text
# id stays home and is counted as withheld.
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#@+-]{0,199}$")
REMOTE_COMMAND = "agent-observer export"
# Largest count an export may carry. Real token totals are far below it,
# and sums of millions of such values still print and serialize.
MAX_COUNT = 2 ** 53
# Distinct counter semantics one export may name (real exports carry 4-5).
# Every task prints each semantics once, so this keeps reports linear.
MAX_SEMANTICS = 64
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
    counts, judgments = _judgments(con)
    return {"schema": SCHEMA, "machine": machine,
            "observer_version": __version__, "exported_at": _now(),
            "ledger_synced_at": _iso(synced),
            "sessions": sessions, "tasks": tasks, "withheld_tasks": withheld,
            "moments": counts, "judgments": judgments}


def _point(moment: dict) -> dict:
    return {"id": moment["id"], "session": moment["session"],
            "origin": moment["origin"], "at": moment["at"]}


def _judgments(con: sqlite3.Connection) -> tuple[dict, list]:
    """Moment counts and, per behavior and judge, the verdict aggregates."""
    found, policy = _moments._all(con)
    by_id = {m["id"]: m for m in found}
    counts = {"by_origin": {o: 0 for o in _moments.ORIGINS},
              "by_kind": {k: 0 for k in _moments.KINDS},
              "policy_denials": {b: 0 for b in
                                 _moments.POLICY_DENIALS.values()}}
    for m in found:
        counts["by_origin"][m["origin"]] += 1
        counts["by_kind"][m["kind"]] += 1
    for _, bucket in policy:
        counts["policy_denials"][bucket] += 1
    judged: dict[tuple, dict] = {}
    for r in con.execute("SELECT moment_id, behavior, judge, verdict,"
                         " problem, group_id FROM verdicts"):
        moment = by_id.get(r["moment_id"])
        if moment is None:
            continue
        entry = judged.setdefault((r["behavior"], r["judge"]), {
            "behavior": r["behavior"], "judge": r["judge"],
            "verdicts": {v: 0 for v in _verdicts.VERDICTS},
            "judged_by_origin": {o: 0 for o in _moments.ORIGINS},
            "groups": {}, "ungrouped": []})
        entry["verdicts"][r["verdict"]] += 1
        entry["judged_by_origin"][moment["origin"]] += 1
        if r["verdict"] != "yes":
            continue
        if r["group_id"]:
            group = entry["groups"].setdefault(r["group_id"], {
                "group": r["group_id"], "problem": r["problem"],
                "moments": []})
            group["moments"].append(_point(moment))
        else:
            entry["ungrouped"].append(_point(moment))
    out = []
    for key in sorted(judged):
        entry = judged[key]
        groups = []
        for name in sorted(entry["groups"]):
            group = entry["groups"][name]
            group["moments"].sort(key=lambda m: (m["at"] or "", m["id"]))
            groups.append(group)
        entry["groups"] = groups
        out.append(entry)
    return counts, out


def _clean(value) -> bool:
    """A string safe to print one per line: no control, format, surrogate
    or line separator characters (ASCII or Unicode)."""
    return isinstance(value, str) and not any(
        unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp")
        for c in value)


def _text_or_none(value) -> bool:
    return value is None or _clean(value)


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) \
        and 0 <= value <= MAX_COUNT


def _count_or_none(value) -> bool:
    return value is None or _count(value)


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
        _clean(k) and _count(v) for k, v in unknown.items()))


def _count_map(value, keys) -> bool:
    return isinstance(value, dict) and all(
        k in keys and _count(v) for k, v in value.items())


def _valid_point(point) -> bool:
    return isinstance(point, dict) and _clean(point.get("id")) \
        and len(point["id"]) <= 400 and _clean(point.get("session")) \
        and point.get("origin") in _moments.ORIGINS \
        and _text_or_none(point.get("at"))


def _valid_judgment(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    if not (isinstance(entry.get("behavior"), str)
            and _verdicts.BEHAVIOR_RE.match(entry["behavior"])
            and isinstance(entry.get("judge"), str)
            and _verdicts.JUDGE_RE.match(entry["judge"])):
        return False
    if not _count_map(entry.get("verdicts"), _verdicts.VERDICTS) \
            or not _count_map(entry.get("judged_by_origin"),
                              _moments.ORIGINS):
        return False
    groups, ungrouped = entry.get("groups"), entry.get("ungrouped")
    if not isinstance(groups, list) or not isinstance(ungrouped, list):
        return False
    for group in groups:
        if not isinstance(group, dict) \
                or not isinstance(group.get("group"), str) \
                or not _verdicts.GROUP_RE.match(group["group"]) \
                or not (_clean(group.get("problem"))
                        and len(group["problem"]) <= _verdicts.PROBLEM_MAX) \
                or not isinstance(group.get("moments"), list) \
                or not all(_valid_point(m) for m in group["moments"]):
            return False
    return all(_valid_point(m) for m in ungrouped)


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
    if not _count_or_none(doc.get("withheld_tasks")):
        raise ValueError("invalid withheld_tasks count")
    for session in doc["sessions"]:
        if not _clean(session.get("session_key")) \
                or not isinstance(session.get("usage", []), list) \
                or not all(_valid_group(g) for g in session.get("usage", [])):
            raise ValueError("invalid session entry")
    # Checked after the types above, so every semantics is hashable.
    semantics = {g.get("semantics") for s in doc["sessions"]
                 for g in s.get("usage", [])}
    if len(semantics) > MAX_SEMANTICS:
        raise ValueError("too many counter semantics")
    for task in doc["tasks"]:
        sessions = task.get("sessions", [])
        if _task_id(task.get("task_id")) is None \
                or not _text_or_none(task.get("acceptance_state")) \
                or not isinstance(sessions, list) \
                or not all(_clean(k) for k in sessions):
            raise ValueError("invalid task entry")
    # Judge evidence is optional: exports from before #60 carry none.
    counts = doc.get("moments")
    if counts is not None and not (
            isinstance(counts, dict)
            and _count_map(counts.get("by_origin", {}), _moments.ORIGINS)
            and _count_map(counts.get("by_kind", {}), _moments.KINDS)
            and _count_map(counts.get("policy_denials", {}),
                           tuple(_moments.POLICY_DENIALS.values()))):
        raise ValueError("invalid moment counts")
    judgments = doc.get("judgments", [])
    if not isinstance(judgments, list) \
            or not all(_valid_judgment(j) for j in judgments):
        raise ValueError("invalid judgments")
    return doc


HEADER_FIELDS = ("schema", "machine", "observer_version", "exported_at",
                 "ledger_synced_at", "withheld_tasks")
TASK_FIELDS = ("task_id", "project", "issue_url", "origin",
               "acceptance_state", "sessions")


def _schema_only(doc: dict) -> dict:
    """A validated export reduced to its schema fields; extras never stored."""
    out = {k: doc.get(k) for k in HEADER_FIELDS if k in doc}
    out["sessions"] = [
        {**{k: s.get(k) for k in SESSION_FIELDS if k in s},
         "usage": [{k: g.get(k) for k in USAGE_FIELDS if k in g}
                   for g in s.get("usage", [])]}
        for s in doc["sessions"]]
    out["tasks"] = [{k: t.get(k) for k in TASK_FIELDS if k in t}
                    for t in doc["tasks"]]
    if doc.get("moments") is not None:
        out["moments"] = {k: dict(doc["moments"].get(k, {}))
                          for k in ("by_origin", "by_kind", "policy_denials")}
    point = ("id", "session", "origin", "at")
    out["judgments"] = [
        {"behavior": j["behavior"], "judge": j["judge"],
         "verdicts": dict(j["verdicts"]),
         "judged_by_origin": dict(j["judged_by_origin"]),
         "groups": [{"group": g["group"], "problem": g["problem"],
                     "moments": [{k: m.get(k) for k in point}
                                 for m in g["moments"]]}
                    for g in j["groups"]],
         "ungrouped": [{k: m.get(k) for k in point} for m in j["ungrouped"]]}
        for j in doc.get("judgments", [])]
    return out


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
        doc = _schema_only(validate(json.loads(proc.stdout)))
        # Store only what a report can print and serialize: a document
        # that passes validation but cannot be reported is refused here,
        # not discovered by every later fleet report.
        render(combine([doc])).encode("utf-8")
        text = json.dumps(doc)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise PullError(f"{host}: {type(exc).__name__}: {exc}") from None
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
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
    recurrence, unmerged, moment_counts = _combine_judgments(docs)
    return {"machines": machines,
            "moments": moment_counts,
            "recurrence": recurrence,
            "unmerged_behaviors": unmerged,
            "withheld_tasks": {d["machine"]: d.get("withheld_tasks") or 0
                               for d in docs},
            "missing": [m for m in (expect or []) if m not in present],
            "duplicate_sessions": [{"session_key": k, "machines": v}
                                   for k, v in sorted(duplicates.items())],
            "totals": totals,
            "tasks": [tasks[k] for k in sorted(tasks, key=str)]}


def _combine_judgments(docs: list) -> tuple[list, list, dict]:
    """Judge evidence across machines.

    Groups merge by id under one judge (a shared judge run grouped them);
    a moment reported by two machines counts once. A behavior judged by
    different judges on different machines is reported per judge and
    listed in ``unmerged_behaviors``. Headline numbers count the origins
    whose corrections count (human and agent); each group keeps its
    per-origin counts."""
    counted = _moments.COUNTED_ORIGINS
    moment_counts = {"by_origin": {o: 0 for o in _moments.ORIGINS},
                     "by_kind": {k: 0 for k in _moments.KINDS},
                     "policy_denials": {b: 0 for b in
                                        _moments.POLICY_DENIALS.values()}}
    judges: dict[tuple, dict] = {}
    for doc in docs:
        counts = doc.get("moments") or {}
        for part in moment_counts:
            for k, v in (counts.get(part) or {}).items():
                moment_counts[part][k] = moment_counts[part].get(k, 0) + v
        for j in doc.get("judgments") or []:
            entry = judges.setdefault((j["behavior"], j["judge"]), {
                "behavior": j["behavior"], "judge": j["judge"],
                "machines": [], "verdicts": {v: 0 for v in _verdicts.VERDICTS},
                "judged_by_origin": {o: 0 for o in _moments.ORIGINS},
                "points": {}, "groups": {}, "problems": {}})
            entry["machines"].append(doc["machine"])
            for k, v in j["verdicts"].items():
                entry["verdicts"][k] += v
            for k, v in j["judged_by_origin"].items():
                entry["judged_by_origin"][k] += v
            for g in j["groups"]:
                for m in g["moments"]:
                    if m["id"] in entry["points"]:
                        continue
                    entry["points"][m["id"]] = m
                    entry["groups"].setdefault(g["group"], []).append(
                        (m, doc["machine"]))
                    entry["problems"].setdefault(g["group"], (m["at"] or "",
                                                              g["problem"]))
                    first = entry["problems"][g["group"]]
                    if (m["at"] or "") < first[0]:
                        entry["problems"][g["group"]] = (m["at"] or "",
                                                         g["problem"])
            for m in j["ungrouped"]:
                if m["id"] not in entry["points"]:
                    entry["points"][m["id"]] = dict(m, ungrouped=True)
    out = []
    for key in sorted(judges):
        entry = judges[key]
        groups = []
        for name, members in entry["groups"].items():
            kept = [(m, machine) for m, machine in members
                    if m["origin"] in counted]
            if not kept:
                continue
            kept.sort(key=lambda x: (x[0]["at"] or "", x[0]["id"]))
            by_origin: dict[str, int] = {}
            for m, _ in kept:
                by_origin[m["origin"]] = by_origin.get(m["origin"], 0) + 1
            groups.append({
                "group": name, "problem": entry["problems"][name][1],
                "occurrences": len(kept), "repeats": len(kept) - 1,
                "by_origin": by_origin,
                "sessions": len({m["session"] for m, _ in kept}),
                "machines": sorted({machine for _, machine in kept}),
                "first_seen": kept[0][0]["at"], "last_seen": kept[-1][0]["at"]})
        groups.sort(key=lambda g: (-g["occurrences"], -g["sessions"],
                                   g["first_seen"] or "", g["group"]))
        yes = [m for m in entry["points"].values() if m["origin"] in counted]
        by_origin = {o: sum(1 for m in yes if m["origin"] == o)
                     for o in counted}
        out.append({
            "behavior": entry["behavior"], "judge": entry["judge"],
            "machines": sorted(entry["machines"]),
            "coverage": {"verdicts": entry["verdicts"],
                         "judged_by_origin": entry["judged_by_origin"]},
            "occurrences": len(yes), "by_origin": by_origin,
            "ungrouped": sum(1 for m in yes if m.get("ungrouped")),
            "problems": len(groups),
            "recurring_problems": sum(1 for g in groups if g["sessions"] >= 2),
            "repeats": sum(g["repeats"] for g in groups),
            "groups": groups})
    behaviors: dict[str, list] = {}
    for entry in out:
        behaviors.setdefault(entry["behavior"], []).append(entry["judge"])
    unmerged = [{"behavior": b, "judges": sorted(js)}
                for b, js in sorted(behaviors.items()) if len(js) > 1]
    return out, unmerged, moment_counts


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
    counts = result.get("moments") or {}
    if counts:
        lines.append("moments by origin: " + ", ".join(
            f"{k} {v}" for k, v in counts["by_origin"].items())
            + "; policy denials (not corrections): " + ", ".join(
            f"{k} {v}" for k, v in counts["policy_denials"].items()))
    for j in result.get("recurrence") or []:
        lines.append(
            f"{j['behavior']} by {j['judge']} on {', '.join(j['machines'])}:"
            f" {j['occurrences']} yes ("
            + ", ".join(f"{k} {v}" for k, v in j["by_origin"].items())
            + f"), problems {j['problems']}, recurring (2+ sessions)"
            f" {j['recurring_problems']}, repeats {j['repeats']},"
            f" ungrouped {j['ungrouped']}")
        for g in j["groups"]:
            lines.append(f"- {g['group']}: {g['occurrences']}x in"
                         f" {g['sessions']} sessions on"
                         f" {', '.join(g['machines'])}: {g['problem']}")
    for u in result.get("unmerged_behaviors") or []:
        lines.append(f"not merged: {u['behavior']} was judged by"
                     f" {', '.join(u['judges'])} on different machines")
    for t in result["tasks"]:
        states = ", ".join(f"{m}={s}" for m, s in
                           sorted(t["acceptance_states"].items()))
        lines.append(f"- {t['task_id']}: {states}; {t['sessions']} sessions"
                     f" ({t['shared_sessions']} shared with other tasks,"
                     f" {len(t['missing_sessions'])} not exported);"
                     f" whole-session tokens {_tokens(t['session_usage'])}")
    return "\n".join(lines)
