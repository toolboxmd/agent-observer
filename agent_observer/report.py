"""Query surface: task usage reconciliation and event timelines."""

from __future__ import annotations

import hashlib
import json
import sqlite3

BUCKETS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
           "output_tokens", "reasoning_output_tokens", "total_tokens")
PHASES = ("research", "planning", "design", "implementation", "review",
          "correction", "recovery", "dispatch", "verification", "delivery")


def generation_usage(rows) -> dict:
    """Disjoint generated-token buckets, retaining unknown split coverage."""
    reasoning, other, missing = [], [], 0
    for row in rows:
        r, out, sem = row["reasoning_output_tokens"], row["output_tokens"], _semantics_key(row)
        inclusive = sem.startswith(("codex:input_includes_cached", "claude:input_excludes_cache"))
        separate = sem == "opencode:input_excludes_cache,reasoning_separate"
        if type(r) is not int or type(out) is not int or min(r, out) < 0 \
                or not (inclusive or separate) or (inclusive and r > out):
            missing += 1
            continue
        reasoning.append(r)
        other.append(out-r if inclusive else out)
    total = sum(reasoning)+sum(other)
    return {"reasoning_tokens": sum(reasoning) if reasoning else None,
            "other_output_tokens": sum(other) if other else None,
            "reasoning_share": sum(reasoning)/total if total and not missing else None,
            "measured_responses": len(reasoning), "unknown_responses": missing,
            "note": "Other output includes code, tool calls and replies; it is not a work phase."}


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


def model_usage(rows) -> list[dict]:
    """Model buckets over an already selected response scope.

    Task callers pass the existing attribution partition, never all
    responses from the sessions that happen to contain that task.
    """
    groups = {}
    for row in _live(rows):
        key = (row["harness"], row["model"], row["effort"], _semantics_key(row))
        groups.setdefault(key, []).append(row)
    models = []
    for (harness, model, effort, semantics), group in groups.items():
        totals = _bucket_totals(group)
        models.append({"harness": harness, "model": model, "effort": effort,
                       "semantics": semantics, **totals,
                       "generation": generation_usage(group),
                       "tokens": totals["total_tokens"],
                       "unknown_tokens": (totals.get("unknown_counts") or {}).get(
                           "total_tokens", 0)})
    return sorted(models, key=lambda m: (
        m["tokens"] is None, -(m["tokens"] or 0), m["harness"],
        m["model"] or "", m["effort"] or "", m["semantics"]))


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
    keys |= {r["session_key"] for r in con.execute(
        "SELECT session_key FROM attempts WHERE task_id=? AND session_key IS NOT NULL",
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


def _source_cutoff(con: sqlite3.Connection, scope_keys: set) -> float | None:
    """Max native import time backing the scope; None when scope is empty.

    The cutoff moves only when native evidence backing the measured
    sessions changes, so repeated rendering without ledger changes keeps
    the same cutoff and snapshot identity.
    """
    keys = sorted(scope_keys)
    if not keys:
        return None
    placeholders = ",".join("?" * len(keys))
    row = con.execute(
        "SELECT MAX(imported_at) m FROM sources WHERE id IN ("
        "SELECT DISTINCT source_id FROM responses WHERE session_key IN "
        f"({placeholders}) UNION "
        "SELECT DISTINCT source_id FROM events WHERE session_key IN "
        f"({placeholders}) UNION "
        "SELECT DISTINCT source_id FROM submissions WHERE session_key IN "
        f"({placeholders}) UNION "
        "SELECT DISTINCT source_id FROM sessions WHERE session_key IN "
        f"({placeholders}))",
        keys + keys + keys + keys).fetchone()
    return row["m"] if row is not None else None


def _snapshot_id(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _task_turn_ids(con: sqlite3.Connection, task_id: str,
                   turn_sub: dict, whole: dict) -> set:
    """Turns owned by the task: bound submission turns plus whole sessions."""
    bound = {a["submission_native_id"] for a in con.execute(
        "SELECT submission_native_id FROM assignments WHERE task_id=?",
        (task_id,))}
    owned = {t for t, s in turn_sub.items() if s in bound}
    whole_keys = sorted(whole)
    if whole_keys:
        placeholders = ",".join("?" * len(whole_keys))
        for row in con.execute(
                f"SELECT turn_id FROM turns WHERE session_key IN "
                f"({placeholders})", whole_keys):
            if row["turn_id"]:
                owned.add(row["turn_id"])
        for row in con.execute(
                f"SELECT DISTINCT turn_id FROM responses WHERE session_key IN "
                f"({placeholders})", whole_keys):
            if row["turn_id"]:
                owned.add(row["turn_id"])
    return owned


def _task_diagnostics(con: sqlite3.Connection, scope_keys: set,
                      whole: dict, task_turns: set) -> dict:
    """Task-scoped diagnostics and time, with session context labeled.

    Incidents whose evidence resolves only to task-owned turns are
    task-scoped; everything else stays explicitly labeled session context
    and is never presented as a task-only fact. Elapsed time comes from
    task-owned turns when timing exists, otherwise it is unavailable and
    the whole-session span is labeled session context.
    """
    from . import analysis as _analysis
    task_counts: dict = {}
    context_counts: dict = {}
    for key in sorted(scope_keys):
        try:
            session = con.execute(
                "SELECT * FROM sessions WHERE session_key=?", (key,)).fetchone()
        except Exception:
            session = None
        if session is None:
            continue
        for incident in _analysis.detect_session(con, session):
            turns = _analysis._incident_turns(con, key, incident)
            if key in whole or (turns and turns <= task_turns):
                task_counts[incident["detector"]] = \
                    task_counts.get(incident["detector"], 0) + 1
            else:
                context_counts[incident["detector"]] = \
                    context_counts.get(incident["detector"], 0) + 1
    starts, ends = [], []
    if task_turns:
        placeholders = ",".join("?" * len(task_turns))
        for row in con.execute(
                f"SELECT started_at, completed_at FROM turns WHERE turn_id IN "
                f"({placeholders})", sorted(task_turns)):
            if row["started_at"]:
                starts.append(row["started_at"])
            if row["completed_at"]:
                ends.append(row["completed_at"])
    if starts and ends:
        task_elapsed = max(ends) - min(starts)
        task_source = "task turns"
    else:
        task_elapsed = None
        task_source = "unavailable (no task turn timing)"
    sessions = [dict(r) for r in con.execute(
        "SELECT started_at, ended_at FROM sessions WHERE session_key IN "
        f"({','.join('?' * len(sorted(scope_keys)))})",
        sorted(scope_keys))] if scope_keys else []
    s_starts = [s["started_at"] for s in sessions if s["started_at"]]
    s_ends = [s["ended_at"] for s in sessions if s["ended_at"]]
    session_span = (max(s_ends) - min(s_starts)) if s_starts and s_ends else None
    return {"task_scoped_counts": task_counts,
            "session_context_counts": context_counts,
            "task_elapsed_s": task_elapsed,
            "task_elapsed_source": task_source,
            "session_span_s": session_span,
            "session_span_label": "session context, not task-only"}


def _native_cost(rows: list) -> dict:
    known = [r["cost_usd"] for r in rows
             if not r.get("is_overlap") and r.get("cost_usd") is not None]
    unknown = sum(1 for r in rows
                  if not r.get("is_overlap") and r.get("cost_usd") is None)
    total = sum(known) if known else (0.0 if not unknown else None)
    # An empty partition costs nothing; a partition with any unknown
    # native cost stays explicitly partial, never zero-filled.
    if not rows or all(r.get("is_overlap") for r in rows):
        total = 0.0
        unknown = 0
    return {"total_usd": total if not unknown else None,
            "known_subtotal_usd": total, "known_responses": len(known),
            "unknown_responses": unknown,
            "note": "Native harness-reported cost, separate from list-price "
                    "estimates and subscription billing."}


def _phase_report(con, task_id, rows, whole, attempts, schedule):
    from . import pricing, analysis
    by_turn, by_session = {}, {}

    def phase(value):
        if value in PHASES or value == "mixed":
            return value
        if value in ("implementation_default", "implementation_small", "implementation_hard"):
            return "implementation"
        if value in ("review_final", "review_ticket"):
            return "review"
        return "unclassified"

    for a in con.execute("SELECT s.turn_id,a.phase FROM assignments a JOIN submissions s"
                         " ON s.native_id=a.submission_native_id WHERE a.task_id=?", (task_id,)):
        if a["turn_id"] and a["phase"]:
            by_turn.setdefault(a["turn_id"], set()).add(phase(a["phase"]))
    for a in attempts:
        # A captured phase on the exact turn outranks missing submission labels.
        if a.get("stage") and a.get("harness") != "router":
            by_turn[a["turn_id"]] = {phase(a["stage"])}
        if a.get("session_key") in whole and a.get("harness") == "router":
            by_session.setdefault(a["session_key"], set()).add(phase(a.get("stage")))

    def selected(row):
        labels = by_turn.get(row["turn_id"]) or by_session.get(row["session_key"]) or {"unclassified"}
        return next(iter(labels)) if len(labels) == 1 else "mixed"

    groups = {}
    for row in rows:
        groups.setdefault(selected(row), []).append(row)
    activities = {}
    turns = {r["turn_id"] for r in rows if r["turn_id"]}
    for key in sorted({r["session_key"] for r in rows}):
        for event in con.execute("SELECT * FROM events WHERE session_key=?", (key,)):
            label = ("session context" if key not in whole and event["turn_id"] not in turns
                     else selected(event))
            cell = activities.setdefault(label, {"tool_calls": 0, "mcp_results": 0,
                "reads": 0, "file_changes": 0, "failed_tool_results": 0})
            if event["family"] == "tool_call": cell["tool_calls"] += 1
            if event["family"] == "tool_result":
                if (event["name"] or "").startswith("mcp."): cell["mcp_results"] += 1
                if analysis._failed(event): cell["failed_tool_results"] += 1
            if event["family"] == "read": cell["reads"] += 1
            if event["family"] == "file_change": cell["file_changes"] += 1
    context = activities.pop("session context", {})
    phases = [{"phase": name, "usage": _usage_totals(groups.get(name, [])),
             "models": model_usage(groups.get(name, [])),
             "estimated_cost": pricing.price_scope(groups.get(name, []), schedule),
             "activity": activities.get(name, {}),
             "activity_note": "Recorded observations, not separately billed token costs; coverage varies by harness."}
            for name in sorted(set(groups) | set(activities))]
    return phases, context


def task_report(con: sqlite3.Connection, task_id: str,
                schedule: dict | None = None) -> dict:
    from . import pricing as _pricing
    if schedule is None:
        schedule = _pricing.load_schedule()
    # Retain the exact selected schedule with the export. A caller changing
    # its own dictionary later must not alter an already produced valuation.
    schedule = json.loads(json.dumps(schedule))
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
    whole_conflicts = {r["session_key"] for r in con.execute(
        "SELECT DISTINCT w.session_key FROM session_assignments w JOIN submissions s"
        " ON s.session_key=w.session_key JOIN assignments a ON a.submission_native_id=s.native_id"
        " WHERE a.task_id<>w.task_id") if r["session_key"] in scope_keys}
    whole_shared |= whole_conflicts
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
            if sub in joint or r["session_key"] in whole_conflicts:
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
    outcome_row = _row_or_none(
        con.execute("SELECT * FROM outcomes WHERE task_id=?",
                    (task_id,)).fetchone())
    attempts_rows = [dict(r) for r in con.execute(
        "SELECT * FROM attempts WHERE task_id=? ORDER BY turn_id",
        (task_id,))]
    dispatches_rows = [dict(r) for r in con.execute(
        "SELECT * FROM dispatches WHERE owning_submission IN "
        "(SELECT submission_native_id FROM assignments WHERE task_id=?)",
        (task_id,))]
    owned_sessions = set(whole) | {r["session_key"] for r in con.execute(
        "SELECT s.session_key FROM submissions s JOIN assignments a"
        " ON a.submission_native_id=s.native_id WHERE a.task_id=?", (task_id,))}

    def worker_session(identity):
        exact = con.execute("SELECT session_key FROM sessions WHERE session_key=?",
                            (identity,)).fetchone()
        if exact:
            return exact["session_key"]
        matches = con.execute("SELECT session_key FROM sessions WHERE native_id=?",
                              (identity,)).fetchall()
        return matches[0]["session_key"] if len(matches) == 1 else None

    unbound_dispatches = sorted({d["worker_thread"] for d in dispatches_rows
                                 if worker_session(d["worker_thread"]) not in owned_sessions})
    missing_sessions = sorted(key for key in scope_keys if not con.execute(
        "SELECT 1 FROM sessions WHERE session_key=?", (key,)).fetchone())
    empty_sessions = sorted(key for key in scope_keys if key not in missing_sessions
                            and not con.execute("SELECT 1 FROM responses WHERE session_key=?",
                                                (key,)).fetchone())
    unbound_workers = sorted({a["session_key"] for a in attempts_rows
                              if a.get("harness") == "router" and a.get("session_key")
                              and a["session_key"] not in whole})
    unavailable_usage = bool(not scope_keys or missing_sessions or empty_sessions
                             or unbound_workers or unbound_dispatches or whole_conflicts)
    cutoff = _source_cutoff(con, scope_keys)
    task_turns = _task_turn_ids(con, task_id, turn_sub, whole)
    diagnostics = _task_diagnostics(con, scope_keys, whole, task_turns)
    phases, activity_context = _phase_report(con, task_id, attributed, set(whole)-whole_shared,
                                             attempts_rows, schedule)
    from . import timing as _timing
    completion = _timing.completion_timing(
        con, task_id, outcome_row, cutoff,
        diagnostics["task_elapsed_s"], diagnostics["task_elapsed_source"],
        diagnostics["session_span_s"])
    attempt_time = _timing.attempt_timing(
        con, attempts_rows, set(whole_shared), set(whole_conflicts))
    executions = attempt_time.get("executions") or attempts_rows
    failures = _timing.failure_summary(executions)
    jobs = _timing.job_outcomes(con, attempts_rows)
    recovery = _timing.recovery_summary(
        executions, con, set(whole_shared), set(whole_conflicts))

    def _resp_tuple(r) -> list:
        return [r.get("response_id"), r.get("harness"), r.get("model"),
                r.get("effort"), _semantics_key(r), r.get("turn_id"),
                r.get("input_tokens"), r.get("cached_input_tokens"),
                r.get("cache_write_input_tokens"), r.get("output_tokens"),
                r.get("reasoning_output_tokens"), r.get("total_tokens"),
                r.get("cost_usd"), r.get("cache_write_5m_tokens"), r.get("cache_write_1h_tokens")]

    snapshot_payload = {
        "price_schedule": schedule,
        "phases": phases,
        "activity_session_context": activity_context,
        "coverage_evidence": {"missing_submissions": sorted(missing_submissions),
                              "conflicting_sessions": sorted(whole_conflicts),
                              "joint": joint, "whole_shared": sorted(whole_shared),
                              "missing_sessions": missing_sessions,
                              "empty_sessions": empty_sessions,
                              "unbound_workers": unbound_workers,
                              "unbound_dispatches": unbound_dispatches},
        "task_id": task_id,
        "sessions": sorted(scope_keys),
        "attributed": sorted(_resp_tuple(r) for r in attributed),
        "shared": sorted(_resp_tuple(r) for r in shared),
        "unassigned": sorted(_resp_tuple(r) for r in unassigned),
        "assignments": sorted(
            [a["submission_native_id"], a["task_id"], a["shared"]]
            for a in con.execute(
                "SELECT submission_native_id, task_id, shared FROM assignments"
                " WHERE task_id=?", (task_id,))),
        "outcome": outcome_row,
        "attempts": sorted(
            [a.get("turn_id"), a.get("role"), a.get("state")] for a in attempts_rows),
        "attempt_timing_evidence": sorted(
            [a.get("turn_id"), a.get("started_at"), a.get("ended_at"),
             a.get("elapsed_s"), a.get("terminal_class")] for a in attempts_rows),
        "attempt_report_inputs": sorted(
            [a.get("turn_id"), a.get("role"), a.get("harness"),
             a.get("session_key"), a.get("stage"), a.get("reason"),
             a.get("model_observed"), a.get("effort_observed"),
             a.get("route_requested"), a.get("state"),
             a.get("terminal_class"), a.get("started_at"),
             a.get("ended_at"), a.get("elapsed_s"),
             a.get("usage_json") is not None] for a in attempts_rows),
        "job_evidence": sorted(
            [j.get("request_id"), j.get("status"), j.get("updated_at")]
            for j in jobs),
        "reconciliation_evidence": sorted(
            [g.get("representative"), sorted(g.get("members") or []),
             g.get("is_duplicate_group")] for g in (
                attempt_time.get("reconciliation_groups") or [])),
        "recovery_inputs": sorted(
            [r.get("failed_turn"), r.get("failed_stage"),
             r.get("compat_scope"),
             r.get("next_attempt_turn"), r.get("first_progress_turn"),
             r.get("first_progress_stage"),
             r.get("same_stage_progress_turn"),
             r.get("failure_to_next_start_s"),
             r.get("time_to_first_progress_s"),
             r.get("time_to_same_stage_progress_s"),
             r.get("later_failed_attempts"),
             r.get("recovery_outcome")] for r in recovery),
        "usage_attribution": sorted(
            [u.get("turn_id"), u.get("session_key"),
             u.get("router_usage_present"), u.get("native_responses"),
             u.get("attribution")] for u in (
                _timing.usage_source_coverage(
                    con, executions, {"priced_responses": 0,
                                      "unpriced_responses": 0},
                    set(whole_shared),
                    set(whole_conflicts)).get("per_attempt") or [])),
        "submission_timing": sorted(
            [r["native_id"], r["ts"]] for r in con.execute(
                "SELECT s.native_id AS native_id, s.ts AS ts FROM assignments a"
                " JOIN submissions s ON s.native_id=a.submission_native_id"
                " WHERE a.task_id=?", (task_id,))),
        "dispatches": sorted(
            [d.get("owning_submission"), d.get("worker_thread")] for d in dispatches_rows),
        "source_cutoff": cutoff,
    }
    snapshot = _snapshot_id(snapshot_payload)
    estimated = _pricing.price_scope(attributed, schedule)
    shared_estimated = _pricing.price_scope(shared, schedule)
    for estimate in (estimated, shared_estimated):
        estimate["status"] = ("complete" if estimate["complete"] else
                              "partial" if estimate["priced_responses"] else "unknown")
        if unavailable_usage:
            estimate.update(complete=False, estimated_cost_usd_total=None, status="partial",
                            coverage_note="Native session usage or worker ownership is unavailable")
    native_cost = _native_cost(attributed)
    if unavailable_usage:
        native_cost["total_usd"] = None
    usage_coverage = _timing.usage_source_coverage(
        con, executions, estimated, set(whole_shared), set(whole_conflicts))
    acceptance = (outcome_row or {}).get("acceptance_state") or "unknown"
    # Acceptance is explicit only: process success, zero exit or a
    # successful attempt never implies an accepted outcome.
    active_attempts = [a for a in attempts_rows if a.get("state") == "active"]
    report = {
        "task": dict(task),
        "attributed": total(attributed),
        "models": model_usage(attributed),
        "phases": phases,
        "activity_session_context": activity_context,
        "shared_joint": total(shared),
        "shared_models": model_usage(shared),
        "unassigned_in_scope": total(unassigned),
        "scope": scope,
        "scope_sessions": sorted(scope_keys),
        "scope_kind": "task",
        "whole_session_assignments": whole,
        "missing_assignments": missing_submissions,
        "conflicting_assignments": [
            {"submission": s, **j} for s, j in joint.items()
            if not j["all_shared"] and s in bound_subs],
        "joint_assignments": [
            {"submission": s, **j} for s, j in joint.items()
            if j["all_shared"] and s in bound_subs],
        "crashes_counted_separately": crashes,
        "outcome": outcome_row,
        "acceptance_state": acceptance,
        "attempts": attempts_rows,
        "dispatches": dispatches_rows,
        "active_attempts": active_attempts,
        "has_active_work": bool(active_attempts) or acceptance == "active",
        "snapshot_id": snapshot,
        "price_schedule_id": _snapshot_id(schedule),
        "price_schedule": schedule,
        "source_cutoff": cutoff,
        "measured": {
            "task_id": task_id,
            "sessions": sorted(scope_keys),
            "attributed_responses": sorted(
                r.get("response_id") for r in attributed),
            "shared_responses": sorted(r.get("response_id") for r in shared),
            "unassigned_responses": sorted(
                r.get("response_id") for r in unassigned),
            "source_cutoff": cutoff,
        },
        "diagnostics": diagnostics,
        "time": {
            "task_elapsed_s": diagnostics["task_elapsed_s"],
            "task_elapsed_source": diagnostics["task_elapsed_source"],
            "session_span_s": diagnostics["session_span_s"],
            "session_span_label": diagnostics["session_span_label"],
        },
        "timing": completion,
        "attempt_timing": attempt_time,
        "failures": failures,
        "job_outcomes": jobs,
        "recovery": recovery,
        "usage_coverage": usage_coverage,
        "native_cost": native_cost,
        "native_cost_shared": _native_cost(shared),
        "estimated_cost": estimated,
        "estimated_cost_shared": shared_estimated,
        "subscription_note": "Router quota readings are account evidence, "
                             "not task cost; subscription billing is separate "
                             "and is never posted as spend.",
        "coverage": {
            "no_measured_sessions": not scope_keys,
            "conflicting_sessions": sorted(whole_conflicts),
            "missing_sessions": missing_sessions,
            "sessions_without_usage": empty_sessions,
            "unbound_worker_sessions": unbound_workers,
            "unbound_dispatches": unbound_dispatches,
            "missing": list(missing_submissions),
            "conflicting": [c["submission"] for c in [
                {"submission": s, **j} for s, j in joint.items()
                if not j["all_shared"] and s in bound_subs]],
            "joint": [c["submission"] for c in [
                {"submission": s, **j} for s, j in joint.items()
                if j["all_shared"] and s in bound_subs]],
            "active_attempts": [a.get("turn_id") for a in active_attempts],
            "crashes": crashes,
        },
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
                          and not report["conflicting_assignments"]
                          and not unavailable_usage
                          and not report["has_active_work"])
    report["coverage"]["reconciles"] = report["reconciles"]
    report["coverage"]["complete"] = report["complete"]
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
