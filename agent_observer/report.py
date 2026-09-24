"""Query surface: task usage reconciliation and event timelines."""

from __future__ import annotations

import json
import sqlite3

BUCKETS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
           "output_tokens", "reasoning_output_tokens", "total_tokens")


def _bucket_totals(rows, include_responses: bool = True) -> dict:
    """Per-bucket sums over known values; unknown stays visible.

    Contract rule 5: unknown counters stay NULL, never zero. A bucket
    with any NULL is a lower bound when some values are known (the sum
    of the known ones) and None when none are, with the count of
    unknown rows in unknown_counts. Scopes with no unknowns return
    exactly the historical keys.
    """
    out = {}
    unknown = {}
    for bucket in BUCKETS:
        known = [x[bucket] for x in rows if x[bucket] is not None]
        missing = sum(1 for x in rows if x[bucket] is None)
        if missing:
            unknown[bucket] = missing
            out[bucket] = sum(known) if known else None
        else:
            out[bucket] = sum(known)
    if include_responses:
        out["responses"] = len(rows)
    if unknown:
        out["unknown_counts"] = unknown
    return out


def _live(rows) -> list:
    return [r for r in rows if not r["is_overlap"]]


def _semantics_key(row) -> str:
    """The counter semantics of one response; missing stays visible."""
    return row["semantics"] if row["semantics"] else "unknown"


def _usage_totals(rows, include_responses: bool = True) -> dict:
    """Bucket sums that never mix different counter semantics.

    A scope with one semantics (missing semantics throughout counts as
    one homogeneous group) returns the historical raw bucket keys via
    _bucket_totals. A scope mixing semantics, including a known
    semantics beside missing semantics, omits the top-level raw
    buckets and returns only response counts with complete
    per-semantics totals under by_semantics, so no cross-semantics
    arithmetic is ever exposed.
    """
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(_semantics_key(row), []).append(row)
    if len(groups) > 1:
        out: dict = {}
        if include_responses:
            out["responses"] = len(rows)
        out["by_semantics"] = {
            sem: _bucket_totals(group, include_responses=False)
            for sem, group in sorted(groups.items())}
        return out
    return _bucket_totals(rows, include_responses=include_responses)


def _responses(con: sqlite3.Connection, session_keys=None) -> list:
    if session_keys is None:
        return con.execute("SELECT * FROM responses").fetchall()
    keys = sorted(session_keys)
    if not keys:
        return []
    return con.execute(
        f"SELECT * FROM responses WHERE session_key IN ({','.join('?' * len(keys))})",
        keys).fetchall()


def scope_totals(con: sqlite3.Connection, session_keys=None) -> dict:
    """Usage over a scope. total_tokens is each harness's own total; the
    raw buckets are only summed within one counter semantics. A scope
    mixing semantics omits the top-level raw buckets and lists complete
    per-semantics totals under by_semantics instead."""
    rows = _responses(con, session_keys)
    live = _live(rows)
    totals = _usage_totals(live) | {
        "overlap_responses": sum(1 for r in rows if r["is_overlap"]),
    }
    return totals


def task_sessions(con: sqlite3.Connection, task_id: str) -> set:
    """Sessions a task touches: those holding its assigned submissions or
    wholly assigned to it."""
    keys = {r["session_key"] for r in con.execute(
        "SELECT DISTINCT s.session_key FROM assignments a JOIN submissions s"
        " ON s.native_id=a.submission_native_id WHERE a.task_id=?"
        " AND s.session_key IS NOT NULL", (task_id,))}
    keys |= {r["session_key"] for r in con.execute(
        "SELECT session_key FROM session_assignments WHERE task_id=?",
        (task_id,))}
    return keys


def _turn_submission(con: sqlite3.Connection) -> dict:
    """Map turn_id to the genuine submission that started it.

    One turn follows one genuine user submission; the mapping uses the
    submission turn_id recorded at import. Tool results and assistant rows
    never create submissions.
    """
    mapping = {}
    for s in con.execute(
            "SELECT native_id, turn_id FROM submissions WHERE is_genuine=1"):
        if s["turn_id"] and s["turn_id"] not in mapping:
            mapping[s["turn_id"]] = s["native_id"]
    return mapping


def task_report(con: sqlite3.Connection, task_id: str) -> dict:
    task = con.execute("SELECT * FROM tasks WHERE task_id=?",
                       (task_id,)).fetchone()
    if task is None:
        raise KeyError(f"unknown task: {task_id}")
    turn_sub = _turn_submission(con)
    assigns = con.execute(
        "SELECT * FROM assignments WHERE task_id=?", (task_id,)).fetchall()
    bound_subs = {a["submission_native_id"]: a for a in assigns}
    # Joint submissions bound to several tasks stay shared, never divided.
    joint = {}
    for a in con.execute(
            "SELECT submission_native_id, COUNT(*) n FROM assignments "
            "GROUP BY 1 HAVING n > 1"):
        rows = con.execute(
            "SELECT task_id, shared FROM assignments WHERE "
            "submission_native_id=?", (a["submission_native_id"],)).fetchall()
        joint[a["submission_native_id"]] = {
            "tasks": [r["task_id"] for r in rows],
            "all_shared": all(r["shared"] for r in rows),
        }

    scope_keys = task_sessions(con, task_id)
    whole = {r["session_key"]: r["evidence"] for r in con.execute(
        "SELECT session_key, evidence FROM session_assignments WHERE task_id=?",
        (task_id,))}
    whole_shared = {r["session_key"] for r in con.execute(
        "SELECT session_key FROM session_assignments GROUP BY session_key"
        " HAVING COUNT(DISTINCT task_id) > 1")}
    responses = _responses(con, scope_keys)
    attributed, shared, unassigned = [], [], []
    missing_submissions = []
    assigned_anywhere = {r["submission_native_id"] for r in
                         con.execute("SELECT submission_native_id "
                                     "FROM assignments").fetchall()}
    for r in responses:
        if r["is_overlap"]:
            continue
        if r["session_key"] in whole:
            (shared if r["session_key"] in whole_shared else attributed).append(dict(r))
            continue
        sub = turn_sub.get(r["turn_id"])
        if sub is None:
            unassigned.append(dict(r))
        elif sub in bound_subs:
            if sub in joint:
                shared.append(dict(r))
            else:
                attributed.append(dict(r))
        else:
            unassigned.append(dict(r))
            if sub not in assigned_anywhere and sub not in missing_submissions:
                missing_submissions.append(sub)

    def total(rows):
        return _usage_totals(rows)

    def _known(value):
        return value if value is not None else 0

    def _sem_known_sums(rows):
        """Known total_tokens per counter semantics; missing keys are 0."""
        sums: dict[str, int] = {}
        for r in rows:
            sums[_semantics_key(r)] = sums.get(_semantics_key(r), 0) \
                + _known(r["total_tokens"])
        return sums

    scope = scope_totals(con, scope_keys)
    crashes = con.execute(
        "SELECT COUNT(*) n FROM attempts WHERE task_id=? AND state='crashed'",
        (task_id,)).fetchone()["n"]
    report = {
        "task": dict(task),
        "attributed": total(attributed),
        "shared_joint": total(shared),
        "unassigned_in_scope": total(unassigned),
        "scope": scope,
        "scope_sessions": sorted(scope_keys),
        "whole_session_assignments": whole,
        "missing_assignments": missing_submissions,
        "conflicting_assignments": [
            {"submission": s, **j} for s, j in joint.items()
            if not j["all_shared"] and s in bound_subs],
        "joint_assignments": [
            {"submission": s, **j} for s, j in joint.items()
            if j["all_shared"] and s in bound_subs],
        "crashes_counted_separately": crashes,
        "outcome": _row_or_none(
            con.execute("SELECT * FROM outcomes WHERE task_id=?",
                        (task_id,)).fetchone()),
        "attempts": [dict(r) for r in con.execute(
            "SELECT * FROM attempts WHERE task_id=? ORDER BY turn_id",
            (task_id,))],
        "dispatches": [dict(r) for r in con.execute(
            "SELECT * FROM dispatches WHERE owning_submission IN "
            "(SELECT submission_native_id FROM assignments WHERE task_id=?)",
            (task_id,))],
    }
    # Reconciliation: attributed + shared + unassigned partition the same
    # rows as the scope, so responses add up exactly and known token sums
    # agree per counter semantics. A mixed scope has no top-level raw
    # total; comparing per-semantics sums keeps the check truthful
    # instead of treating an absent mixed total as zero. Unknown_counts
    # on any side marks the lower bound.
    live_scope = [r for r in responses if not r["is_overlap"]]
    responses_reconcile = (
        report["attributed"]["responses"]
        + report["shared_joint"]["responses"]
        + report["unassigned_in_scope"]["responses"] == scope["responses"]
    )
    if "total_tokens" in scope:
        totals_reconcile = (
            _known(report["attributed"]["total_tokens"])
            + _known(report["shared_joint"]["total_tokens"])
            + _known(report["unassigned_in_scope"]["total_tokens"])
            == _known(scope["total_tokens"])
        )
    else:
        parts = _sem_known_sums(attributed)
        for rows in (shared, unassigned):
            for sem, value in _sem_known_sums(rows).items():
                parts[sem] = parts.get(sem, 0) + value
        wholes = _sem_known_sums(live_scope)
        totals_reconcile = all(
            parts.get(sem, 0) == value
            for sem, value in wholes.items()) and all(
            parts.get(sem, 0) == wholes.get(sem, 0)
            for sem in parts)
    report["reconciles"] = responses_reconcile and totals_reconcile
    report["complete"] = (not missing_submissions
                          and not report["conflicting_assignments"])
    return report


def timeline(con: sqlite3.Connection, task_id: str | None = None,
             turn_id: str | None = None, family: str | None = None,
             limit: int = 200, session_key: str | None = None) -> dict:
    turn_sub = _turn_submission(con)
    task_turns: set[str] | None = None
    if task_id is not None:
        bound = {a["submission_native_id"] for a in con.execute(
            "SELECT submission_native_id FROM assignments WHERE task_id=?",
            (task_id,))}
        task_turns = {t for t, s in turn_sub.items() if s in bound}
    q = "SELECT * FROM events WHERE 1=1"
    args: list = []
    if session_key:
        q += " AND session_key=?"
        args.append(session_key)
    if turn_id:
        q += " AND turn_id=?"
        args.append(turn_id)
    elif task_turns is not None:
        whole = sorted(r["session_key"] for r in con.execute(
            "SELECT session_key FROM session_assignments WHERE task_id=?",
            (task_id,)))
        if not task_turns and not whole:
            return {"events": [], "note": "no bound turns; timeline empty",
                    "join": {"calls": 0, "results": 0, "joined_call_ids": [],
                             "unmatched_results": [], "unanswered_calls": []}}
        clauses = []
        if task_turns:
            clauses.append(f"turn_id IN ({','.join('?' * len(task_turns))})")
            args.extend(sorted(task_turns))
        if whole:
            clauses.append(f"session_key IN ({','.join('?' * len(whole))})")
            args.extend(whole)
        q += " AND (" + " OR ".join(clauses) + ")"
    if family:
        q += " AND family=?"
        args.append(family)
    q += " ORDER BY COALESCE(ts, 0), ordinal_num LIMIT ?"
    args.append(limit)
    rows = con.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d["detail_json"]) if d["detail_json"] else None
        except ValueError:
            d["detail"] = {"raw": d["detail_json"]}
        d.pop("detail_json", None)
        out.append(d)
    # Tool join status: calls joined to results only on equal call_id.
    calls = {e["native_id"] for e in out if e["family"] == "tool_call"}
    results = {e["native_id"] for e in out if e["family"] == "tool_result"}
    return {
        "events": out,
        "join": {
            "calls": len(calls),
            "results": len(results),
            "joined_call_ids": sorted(calls & results),
            "unmatched_results": sorted(results - calls),
            "unanswered_calls": sorted(calls - results),
        },
        "coverage_note": ("Native evidence absent stays unknown; unmatched "
                          "items are retained, never guessed into a match."),
    }


def capabilities() -> list[dict]:
    from .adapters import capabilities as adapter_capabilities
    return adapter_capabilities()


def _row_or_none(row) -> dict | None:
    return dict(row) if row is not None else None
