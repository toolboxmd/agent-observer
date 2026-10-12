"""Judge verdicts on interventions, and the recurrence report built from them.

An outside judge reads interventions (agent_observer/interventions.py) and writes one
verdict per intervention, behavior and judge: `yes`, `no` or `unclear`, with a
one-line problem and an optional group id for `yes`. A batch is validated
whole and stored only when every line is valid. Re-adding the same
intervention, behavior and judge replaces the verdict, so grouping can run as a
later pass.

A problem recurs when its group appears in two or more sessions. Problem
lines are judge text: redacted, kept in the local ledger, never published.
"""

from __future__ import annotations

import re
import sqlite3

from . import db as _db
from . import interventions as _interventions
from . import privacy

VERDICTS = ("yes", "no", "unclear")
FIELDS = frozenset({"intervention", "behavior", "judge", "verdict", "problem",
                    "group"})
BEHAVIOR_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
JUDGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,79}$")
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
SESSION_RE = re.compile(r"^[a-z0-9]+:[A-Za-z0-9._:-]{1,200}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
PROBLEM_MAX = 200
VALUE_MAX = 80


class VerdictError(ValueError):
    """A rejected batch or report request; errors name each bad line."""

    def __init__(self, message: str, errors: list | None = None):
        super().__init__(message)
        self.errors = errors or []


def _one_line(value, limit: int) -> bool:
    return isinstance(value, str) and value.strip() != "" \
        and "\n" not in value and "\r" not in value and len(value) <= limit


def _check(con, record, ev) -> str | None:
    if not isinstance(record, dict):
        return "not a JSON object"
    extra = set(record) - FIELDS
    if extra:
        return f"unknown fields: {', '.join(sorted(extra))}"
    for key in ("intervention", "behavior", "judge", "verdict"):
        if key not in record:
            return f"missing {key}"
    if not isinstance(record["behavior"], str) \
            or not BEHAVIOR_RE.match(record["behavior"]):
        return "behavior must match [a-z][a-z0-9_-]*"
    if not isinstance(record["judge"], str) \
            or not JUDGE_RE.match(record["judge"]):
        return "judge must be a short identifier"
    if record["verdict"] not in VERDICTS:
        return "verdict must be yes, no or unclear"
    problem, group = record.get("problem"), record.get("group")
    if record["verdict"] == "yes":
        if not _one_line(problem, PROBLEM_MAX):
            return f"yes needs a one-line problem of at most {PROBLEM_MAX} characters"
        if group is not None and (not isinstance(group, str)
                                  or not GROUP_RE.match(group)):
            return "group must match [a-z0-9][a-z0-9._-]*"
    elif problem is not None or group is not None:
        return "problem and group belong only to a yes verdict"
    if _interventions.resolve(con, record["intervention"], ev) is None:
        return "unknown intervention"
    return None


def _renamed(record):
    """A verdict line written with the schema-2 key `moment` (#65)."""
    if isinstance(record, dict) and "moment" in record \
            and "intervention" not in record:
        record = {("intervention" if k == "moment" else k): v
                  for k, v in record.items()}
    return record


def add(con: sqlite3.Connection, records: list) -> dict:
    records = [_renamed(r) for r in records]
    ev = _interventions.evidence(con)
    errors, seen = [], set()
    for n, r in enumerate(records, 1):
        error = _check(con, r, ev)
        if error is None:
            key = (r["intervention"], r["behavior"], r["judge"])
            if key in seen:
                error = "the same intervention, behavior and judge appear twice"
            seen.add(key)
        if error:
            errors.append({"line": n, "error": error})
    if errors:
        raise VerdictError(f"{len(errors)} invalid lines; nothing stored",
                           errors)
    stored = updated = 0
    now = _db.now()
    for r in records:
        key = (r["intervention"], r["behavior"], r["judge"])
        exists = con.execute(
            "SELECT 1 FROM verdicts WHERE intervention_id=? AND behavior=? AND"
            " judge=?", key).fetchone()
        problem = r.get("problem")
        con.execute(
            "INSERT INTO verdicts(intervention_id, behavior, judge, verdict,"
            " problem, group_id, recorded_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(intervention_id, behavior, judge) DO UPDATE SET"
            " verdict=excluded.verdict, problem=excluded.problem,"
            " group_id=excluded.group_id, recorded_at=excluded.recorded_at",
            (*key, r["verdict"],
             privacy.redact_secrets(problem.strip()) if problem else None,
             r.get("group"), now))
        if exists:
            updated += 1
        else:
            stored += 1
    con.commit()
    return {"stored": stored, "updated": updated}


def add_session_attributes(con: sqlite3.Connection, records: list) -> dict:
    errors = []
    for n, r in enumerate(records, 1):
        if not isinstance(r, dict) or set(r) != {"session", "name", "value"}:
            errors.append({"line": n, "error":
                           "needs exactly session, name and value"})
        elif not isinstance(r["session"], str) \
                or not SESSION_RE.match(r["session"]):
            errors.append({"line": n, "error":
                           "session must be a session key <harness>:<id>"})
        elif not isinstance(r["name"], str) or not NAME_RE.match(r["name"]):
            errors.append({"line": n, "error":
                           "name must match [a-z][a-z0-9_-]*"})
        elif not _one_line(r["value"], VALUE_MAX):
            errors.append({"line": n, "error":
                           f"value must be one line of at most {VALUE_MAX} characters"})
    if errors:
        raise VerdictError(f"{len(errors)} invalid lines; nothing stored",
                           errors)
    stored = updated = 0
    now = _db.now()
    for r in records:
        exists = con.execute(
            "SELECT 1 FROM session_attributes WHERE session_key=? AND name=?",
            (r["session"], r["name"])).fetchone()
        con.execute(
            "INSERT INTO session_attributes(session_key, name, value,"
            " recorded_at) VALUES(?,?,?,?) ON CONFLICT(session_key, name)"
            " DO UPDATE SET value=excluded.value,"
            " recorded_at=excluded.recorded_at",
            (r["session"], r["name"], r["value"], now))
        updated += bool(exists)
        stored += not exists
    con.commit()
    return {"stored": stored, "updated": updated}


def _rows(con, behavior: str, judge=None) -> list:
    sql = ("SELECT intervention_id, judge, verdict, problem, group_id FROM verdicts"
           " WHERE behavior=?")
    args = [behavior]
    if judge:
        sql += " AND judge=?"
        args.append(judge)
    return con.execute(sql, args).fetchall()


def _group_entries(rows, scope: dict) -> list:
    """Groups of yes verdicts, largest first, from interventions in scope."""
    groups: dict[str, list] = {}
    for r in rows:
        intervention = scope.get(r["intervention_id"])
        if intervention is None or r["verdict"] != "yes" or not r["group_id"]:
            continue
        groups.setdefault(r["group_id"], []).append((intervention, r))
    out = []
    for group, members in groups.items():
        members.sort(key=lambda m: (m[0]["ts"] or 0, m[0]["id"]))
        sessions = {m["session"] for m, _ in members}
        by_created_by: dict[str, int] = {}
        for m, _ in members:
            by_created_by[m["created_by"]] = by_created_by.get(m["created_by"], 0) + 1
        out.append({"group": group, "problem": members[0][1]["problem"],
                    "occurrences": len(members), "repeats": len(members) - 1,
                    "by_created_by": by_created_by,
                    "sessions": len(sessions),
                    "hosts": sorted({m["harness"] for m, _ in members}),
                    "first_seen": members[0][0]["at"],
                    "last_seen": members[-1][0]["at"],
                    "interventions": [m["id"] for m, _ in members]})
    out.sort(key=lambda g: (-g["occurrences"], -g["sessions"],
                            g["first_seen"] or "", g["group"]))
    return out


def groups(con: sqlite3.Connection, behavior: str, judge=None,
           created_by=_interventions.COUNTED) -> list:
    """Existing groups, so a judge reuses a group id for the same problem."""
    found, _ = _interventions._all(con)
    scope = {m["id"]: m for m in found
             if not created_by or m["created_by"] in created_by}
    out = []
    judges = sorted({r["judge"] for r in _rows(con, behavior, judge)})
    for name in judges:
        for entry in _group_entries(_rows(con, behavior, name), scope):
            entry["judge"] = name
            out.append(entry)
    return out


def listing(con: sqlite3.Connection, behavior: str, judge=None,
            verdict=None) -> list:
    """Every stored verdict for a behavior with its intervention's session, host
    and time, oldest first; the grouping pass reads its problem lines here."""
    found, _ = _interventions._all(con)
    scope = {m["id"]: m for m in found}
    out = []
    for r in _rows(con, behavior, judge):
        if verdict and r["verdict"] != verdict:
            continue
        intervention = scope.get(r["intervention_id"])
        out.append({"intervention": r["intervention_id"], "judge": r["judge"],
                    "verdict": r["verdict"], "problem": r["problem"],
                    "group": r["group_id"],
                    "created_by": intervention["created_by"] if intervention else None,
                    "session": intervention["session"] if intervention else None,
                    "harness": intervention["harness"] if intervention else None,
                    "at": intervention["at"] if intervention else None})
    out.sort(key=lambda v: (v["at"] or "", v["intervention"]))
    return out


def recurrence(con: sqlite3.Connection, behavior: str, judge=None,
               since=None, until=None, harness=None, project=None,
               attrs=None, created_by=_interventions.COUNTED) -> dict:
    """Recurrence over interventions by the given creators. The default counts
    corrections from a person and from another agent together, with a
    breakdown per created_by value; coverage still counts every value in scope."""
    selected = _interventions.select(con, since=since, until=until,
                               harness=harness, project=project,
                               created_by=created_by, attrs=attrs)
    scope = {m["id"]: m for m in selected["interventions"]}
    rows = [r for r in _rows(con, behavior, judge) if r["intervention_id"] in scope]
    judges = sorted({r["judge"] for r in rows})
    if judge is None and len(judges) > 1:
        raise VerdictError(
            f"several judges labeled {behavior} in this scope"
            f" ({', '.join(judges)}); pass --judge")
    chosen = judge or (judges[0] if judges else None)
    rows = [r for r in rows if r["judge"] == chosen]
    judged = {r["intervention_id"] for r in rows}
    unjudged = {o: 0 for o in _interventions.CREATED_BY}
    for m in scope.values():
        if m["id"] not in judged:
            unjudged[m["created_by"]] += 1
    verdict_counts = {v: 0 for v in VERDICTS}
    for r in rows:
        verdict_counts[r["verdict"]] += 1
    entries = _group_entries(rows, scope)
    by_created_by = {}
    for origin in (created_by or _interventions.CREATED_BY):
        part = _group_entries(rows, {k: m for k, m in scope.items()
                                     if m["created_by"] == origin})
        by_created_by[origin] = {
            "occurrences": sum(1 for r in rows if r["verdict"] == "yes"
                               and scope[r["intervention_id"]]["created_by"] == origin),
            "problems": len(part),
            "recurring_problems": sum(1 for g in part if g["sessions"] >= 2),
            "repeats": sum(g["repeats"] for g in part)}
    # Yes verdicts on interventions outside the chosen created_by values (for example a
    # parent agent correcting a worker) are reported apart, never mixed in.
    others: dict[str, dict] = {}
    if created_by:
        every = {m["id"]: m for m in _interventions.select(
            con, since=since, until=until, harness=harness, project=project,
            created_by=None, attrs=attrs)["interventions"]}
        for r in _rows(con, behavior, chosen):
            intervention = every.get(r["intervention_id"])
            if intervention is None or intervention["created_by"] in created_by \
                    or r["verdict"] != "yes":
                continue
            entry = others.setdefault(intervention["created_by"],
                                      {"occurrences": 0, "groups": set()})
            entry["occurrences"] += 1
            if r["group_id"]:
                entry["groups"].add(r["group_id"])
        for entry in others.values():
            entry["problems"] = len(entry.pop("groups"))
    return {
        "behavior": behavior, "judge": chosen,
        "period": {"since": _interventions._iso(since), "until": _interventions._iso(until)},
        "filters": {"harness": harness, "project": project,
                    "attrs": attrs or {},
                    "created_by": list(created_by) if created_by else "all"},
        "coverage": {"interventions": len(scope), "judged": len(judged),
                     "by_created_by": selected["counts"]["by_created_by"],
                     "unjudged_by_created_by": unjudged},
        "verdicts": verdict_counts,
        "occurrences": verdict_counts["yes"],
        "ungrouped": sum(1 for r in rows
                         if r["verdict"] == "yes" and not r["group_id"]),
        "problems": len(entries),
        "recurring_problems": sum(1 for g in entries if g["sessions"] >= 2),
        "repeats": sum(g["repeats"] for g in entries),
        "groups": entries,
        "by_created_by": by_created_by,
        "policy_denials": selected["policy_denials"],
        "other_created_by": others,
    }


def render(report: dict) -> str:
    cov = report["coverage"]
    lines = [
        f"{report['behavior']} by {report['judge'] or 'no judge yet'}:"
        f" {report['occurrences']} yes in {cov['judged']} judged of"
        f" {cov['interventions']} interventions",
        f"problems {report['problems']}, recurring (2+ sessions)"
        f" {report['recurring_problems']}, repeats {report['repeats']},"
        f" ungrouped {report['ungrouped']}",
        "unjudged by created_by: " + ", ".join(
            f"{k} {v}" for k, v in cov["unjudged_by_created_by"].items()),
        "policy denials (not corrections): " + ", ".join(
            f"{k} {v}" for k, v in report["policy_denials"].items()),
    ]
    for origin, part in report.get("by_created_by", {}).items():
        lines.append(f"  from {origin}: {part['occurrences']} yes,"
                     f" problems {part['problems']}, recurring"
                     f" {part['recurring_problems']}, repeats {part['repeats']}")
    for origin, entry in sorted(report.get("other_created_by", {}).items()):
        lines.append(f"also {entry['occurrences']} yes on {origin} interventions"
                     f" ({entry['problems']} problems), not counted above")
    for g in report["groups"]:
        lines.append(f"- {g['group']}: {g['occurrences']}x in {g['sessions']}"
                     f" sessions ({', '.join(g['hosts'])}), {g['first_seen']}"
                     f" to {g['last_seen']}: {g['problem']}")
    return "\n".join(lines)
