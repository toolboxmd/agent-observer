"""Evidence across machines (#56): each machine exports, one place combines.

Every machine runs its own Observer over its own ledger; transcripts stay
on their machine and only aggregates travel. ``export`` writes one
document per machine: per-session model usage (the same buckets ``task
show`` reports, never summed across counter semantics), and per task its
outcome, identifiers and session keys. It carries no prompt or assistant text,
excerpts, titles, local paths or branch names; a task's project and URL
travel only as validated GitHub identifiers. Judge evidence (#60) travels
as intervention counts by created_by and kind, and per judge its verdict counts,
coverage and groups: group id, the judge's one-line problem and each
judged-yes intervention's id, session, created_by and time, so ``combine`` can merge
groups across machines and count an intervention seen twice once. Intervention and
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
from . import interventions as _interventions
from . import verdicts as _verdicts
from .publish import _GITHUB_URL_RE, _REPO_RE

SCHEMA = "agent-observer.fleet-export/2"
# Exports from Observer before ledger schema 3 (#65); read and translated
# during the switch, removed in the next release.
LEGACY_SCHEMA = "agent-observer.fleet-export/1"
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
            "interventions": counts, "judgments": judgments}


def _point(intervention: dict) -> dict:
    return {"id": intervention["id"], "session": intervention["session"],
            "created_by": intervention["created_by"], "at": intervention["at"]}


def _bump(table: dict, session: str, part: str, key: str) -> None:
    counts = table.setdefault(session, {}).setdefault(part, {})
    counts[key] = counts.get(key, 0) + 1


def _judgments(con: sqlite3.Connection) -> tuple[dict, list]:
    """Per-session intervention counts and, per behavior and judge, per-session
    verdict counts plus the judged-yes interventions of each group.

    Counts travel per session so ``combine`` can take each session's
    evidence from one machine only, the same rule as token usage."""
    found, policy = _interventions._all(con)
    by_id = {m["id"]: m for m in found}
    sessions: dict[str, dict] = {}
    for m in found:
        _bump(sessions, m["session"], "by_created_by", m["created_by"])
        _bump(sessions, m["session"], "by_kind", m["kind"])
    for _, bucket, session in policy:
        _bump(sessions, session, "policy_denials", bucket)
    judged: dict[tuple, dict] = {}
    for r in con.execute("SELECT intervention_id, behavior, judge, verdict,"
                         " problem, group_id FROM verdicts"):
        intervention = by_id.get(r["intervention_id"])
        if intervention is None:
            continue
        entry = judged.setdefault((r["behavior"], r["judge"]), {
            "behavior": r["behavior"], "judge": r["judge"],
            "sessions": {}, "groups": {}, "ungrouped": []})
        _bump(entry["sessions"], intervention["session"], "verdicts", r["verdict"])
        _bump(entry["sessions"], intervention["session"], "judged_by_created_by",
              intervention["created_by"])
        if r["verdict"] != "yes":
            continue
        if r["group_id"]:
            group = entry["groups"].setdefault(r["group_id"], {
                "group": r["group_id"], "problem": r["problem"],
                "interventions": []})
            group["interventions"].append(_point(intervention))
        else:
            entry["ungrouped"].append(_point(intervention))
    out = []
    for key in sorted(judged):
        entry = judged[key]
        groups = []
        for name in sorted(entry["groups"]):
            group = entry["groups"][name]
            group["interventions"].sort(key=lambda m: (m["at"] or "", m["id"]))
            groups.append(group)
        entry["groups"] = groups
        out.append(entry)
    return {"sessions": sessions}, out


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


INTERVENTION_PARTS = {"by_created_by": _interventions.CREATED_BY, "by_kind": _interventions.KINDS,
                "policy_denials": tuple(_interventions.POLICY_DENIALS.values())}
JUDGED_PARTS = {"verdicts": _verdicts.VERDICTS,
                "judged_by_created_by": _interventions.CREATED_BY}


def _session_counts(value, parts: dict) -> bool:
    """{session key: {part: {key: count}}} with known parts and keys."""
    return isinstance(value, dict) and all(
        _clean(key) and isinstance(counts, dict) and all(
            part in parts and _count_map(c, parts[part])
            for part, c in counts.items())
        for key, counts in value.items())


def _identifier(value, pattern) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _valid_point(point) -> bool:
    return isinstance(point, dict) and _clean(point.get("id")) \
        and len(point["id"]) <= 400 and _clean(point.get("session")) \
        and point.get("created_by") in _interventions.CREATED_BY \
        and _text_or_none(point.get("at"))


def _valid_judgment(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    if not (_identifier(entry.get("behavior"), _verdicts.BEHAVIOR_RE)
            and _identifier(entry.get("judge"), _verdicts.JUDGE_RE)):
        return False
    if not _session_counts(entry.get("sessions"), JUDGED_PARTS):
        return False
    groups, ungrouped = entry.get("groups"), entry.get("ungrouped")
    if not isinstance(groups, list) or not isinstance(ungrouped, list):
        return False
    for group in groups:
        if not isinstance(group, dict) \
                or not _identifier(group.get("group"), _verdicts.GROUP_RE) \
                or not (_clean(group.get("problem"))
                        and len(group["problem"]) <= _verdicts.PROBLEM_MAX) \
                or not isinstance(group.get("interventions"), list) \
                or not all(_valid_point(m) for m in group["interventions"]):
            return False
    return all(_valid_point(m) for m in ungrouped)


def _from_legacy(doc: dict) -> dict:
    """A schema-1 export in schema-2 terms: moments become interventions,
    origin becomes created_by and `human` becomes `user`. Shapes stay as
    sent; validation still checks every field afterwards."""
    who = {"human": "user"}
    parts = {"by_origin": "by_created_by",
             "judged_by_origin": "judged_by_created_by"}

    def counts(value):
        if not isinstance(value, dict):
            return value
        return {key: {parts.get(part, part): (
                    {who.get(k, k): v for k, v in c.items()}
                    if isinstance(c, dict) else c)
                    for part, c in counts_.items()}
                if isinstance(counts_, dict) else counts_
                for key, counts_ in value.items()}

    def point(p):
        if not isinstance(p, dict):
            return p
        out = {k: v for k, v in p.items() if k != "origin"}
        out["created_by"] = who.get(p.get("origin"), p.get("origin"))
        return out

    new = {k: v for k, v in doc.items() if k != "moments"}
    new["schema"] = SCHEMA
    if isinstance(doc.get("moments"), dict):
        new["interventions"] = {"sessions": counts(
            doc["moments"].get("sessions", {}))}
    judgments = []
    for j in doc.get("judgments", []) if isinstance(
            doc.get("judgments", []), list) else []:
        if not isinstance(j, dict):
            judgments.append(j)
            continue
        j = dict(j)
        j["sessions"] = counts(j.get("sessions"))
        groups = []
        for g in j.get("groups", []) if isinstance(j.get("groups"), list) \
                else []:
            if isinstance(g, dict) and isinstance(g.get("moments"), list):
                old_points = g["moments"]
                g = {k: v for k, v in g.items() if k != "moments"}
                g["interventions"] = [point(p) for p in old_points]
            groups.append(g)
        j["groups"] = groups
        if isinstance(j.get("ungrouped"), list):
            j["ungrouped"] = [point(p) for p in j["ungrouped"]]
        judgments.append(j)
    new["judgments"] = judgments
    return new


def validate(doc) -> dict:
    """An export document, or ValueError naming what is wrong.

    Another machine's export is untrusted input: every field ``combine``
    reads is type-checked here, so a stored export can never break a
    later report. A schema-1 export is translated first.
    """
    if isinstance(doc, dict) and doc.get("schema") == LEGACY_SCHEMA:
        doc = _from_legacy(doc)
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
    counts = doc.get("interventions")
    if counts is not None and not (
            isinstance(counts, dict)
            and _session_counts(counts.get("sessions", {}), INTERVENTION_PARTS)):
        raise ValueError("invalid intervention counts")
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
    def per_session(value, parts):
        return {key: {part: dict(c) for part, c in counts.items()
                      if part in parts}
                for key, counts in (value or {}).items()}
    if doc.get("interventions") is not None:
        out["interventions"] = {"sessions": per_session(
            doc["interventions"].get("sessions"), INTERVENTION_PARTS)}
    point = ("id", "session", "created_by", "at")
    out["judgments"] = [
        {"behavior": j["behavior"], "judge": j["judge"],
         "sessions": per_session(j["sessions"], JUDGED_PARTS),
         "groups": [{"group": g["group"], "problem": g["problem"],
                     "interventions": [{k: m.get(k) for k in point}
                                 for m in g["interventions"]]}
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
            "interventions": moment_counts,
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
    one rule decides every count: a session reported by several machines
    takes all its evidence (interventions, denials, verdicts, yes interventions) from
    one owner, the first machine by name, as token usage does. A behavior
    judged by different judges on different machines is reported per
    judge and listed in ``unmerged_behaviors``. Headline numbers count the created_by values
    whose corrections count (user and agent); each group keeps its
    per-created_by counts."""
    counted = _interventions.COUNTED
    moment_counts = {"by_created_by": {o: 0 for o in _interventions.CREATED_BY},
                     "by_kind": {k: 0 for k in _interventions.KINDS},
                     "policy_denials": {b: 0 for b in
                                        _interventions.POLICY_DENIALS.values()}}
    owner: dict[str, str] = {}
    for doc in docs:  # sorted by machine name
        keys = {s.get("session_key") for s in doc["sessions"]}
        keys |= set(((doc.get("interventions") or {}).get("sessions") or {}))
        for j in doc.get("judgments") or []:
            keys |= set(j["sessions"])
            keys |= {m["session"] for g in j["groups"] for m in g["interventions"]}
            keys |= {m["session"] for m in j["ungrouped"]}
        for key in keys:
            owner.setdefault(key, doc["machine"])

    def owned(session, machine):
        return owner.get(session) == machine

    def add_counts(target, counts):
        for part, c in counts.items():
            for k, v in c.items():
                target[part][k] = target[part].get(k, 0) + v

    judges: dict[tuple, dict] = {}
    for doc in docs:
        machine = doc["machine"]
        for key, counts in ((doc.get("interventions") or {}).get("sessions")
                            or {}).items():
            if owned(key, machine):
                add_counts(moment_counts, counts)
        for j in doc.get("judgments") or []:
            entry = judges.setdefault((j["behavior"], j["judge"]), {
                "behavior": j["behavior"], "judge": j["judge"],
                "machines": [], "verdicts": {v: 0 for v in _verdicts.VERDICTS},
                "judged_by_created_by": {o: 0 for o in _interventions.CREATED_BY},
                "points": {}, "groups": {}, "problems": {}})
            entry["machines"].append(machine)
            for key, counts in j["sessions"].items():
                if owned(key, machine):
                    add_counts(entry, counts)
            for g in j["groups"]:
                for m in g["interventions"]:
                    if not owned(m["session"], machine) \
                            or m["id"] in entry["points"]:
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
                if owned(m["session"], machine) \
                        and m["id"] not in entry["points"]:
                    entry["points"][m["id"]] = dict(m, ungrouped=True)
    out = []
    for key in sorted(judges):
        entry = judges[key]
        groups = []
        for name, members in entry["groups"].items():
            kept = [(m, machine) for m, machine in members
                    if m["created_by"] in counted]
            if not kept:
                continue
            kept.sort(key=lambda x: (x[0]["at"] or "", x[0]["id"]))
            by_created_by: dict[str, int] = {}
            for m, _ in kept:
                by_created_by[m["created_by"]] = by_created_by.get(m["created_by"], 0) + 1
            groups.append({
                "group": name, "problem": entry["problems"][name][1],
                "occurrences": len(kept), "repeats": len(kept) - 1,
                "by_created_by": by_created_by,
                "sessions": len({m["session"] for m, _ in kept}),
                "machines": sorted({machine for _, machine in kept}),
                "first_seen": kept[0][0]["at"], "last_seen": kept[-1][0]["at"]})
        groups.sort(key=lambda g: (-g["occurrences"], -g["sessions"],
                                   g["first_seen"] or "", g["group"]))
        yes = [m for m in entry["points"].values() if m["created_by"] in counted]
        by_created_by = {o: sum(1 for m in yes if m["created_by"] == o)
                     for o in counted}
        out.append({
            "behavior": entry["behavior"], "judge": entry["judge"],
            "machines": sorted(entry["machines"]),
            "coverage": {"verdicts": entry["verdicts"],
                         "judged_by_created_by": entry["judged_by_created_by"]},
            "occurrences": len(yes), "by_created_by": by_created_by,
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
    counts = result.get("interventions") or {}
    if counts:
        lines.append("interventions by created_by: " + ", ".join(
            f"{k} {v}" for k, v in counts["by_created_by"].items())
            + "; policy denials (not corrections): " + ", ".join(
            f"{k} {v}" for k, v in counts["policy_denials"].items()))
    for j in result.get("recurrence") or []:
        lines.append(
            f"{j['behavior']} by {j['judge']} on {', '.join(j['machines'])}:"
            f" {j['occurrences']} yes ("
            + ", ".join(f"{k} {v}" for k, v in j["by_created_by"].items())
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
