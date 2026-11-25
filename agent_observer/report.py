"""Query surface: task usage reconciliation and event timelines."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import sqlite3

from . import codex_order, outcome, placement
from .db import _has_table

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


def _capabilities() -> dict:
    """harness -> {capability family: declared support}."""
    from .adapters import capabilities as adapter_capabilities
    out: dict = {}
    for row in adapter_capabilities():
        out.setdefault(row["harness"], {})[row["family"]] = row["supported"]
    return out


def _session_harness(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT harness FROM sessions WHERE session_key=?",
                      (key,)).fetchone()
    return row["harness"] if row is not None else None


def unmeasured_sessions(con: sqlite3.Connection, session_keys) -> set:
    """Sessions whose usage is not imported at all (#127).

    Only a session whose native harness declares model_usage=False and
    that holds no responses: its consumption is unknown, not zero. A
    response-less session of a harness that imports usage keeps the
    existing exact rendering.
    """
    caps = _capabilities()
    out = set()
    for key in sorted(session_keys or ()):
        harness = _session_harness(con, key)
        if harness is None or caps.get(harness, {}).get("model_usage") is not False:
            continue
        if con.execute("SELECT 1 FROM responses WHERE session_key=? LIMIT 1",
                       (key,)).fetchone() is None:
            out.add(key)
    return out


def _mark_unmeasured(totals: dict, sessions) -> dict:
    """Mark a usage total that misses unmeasured sessions' consumption.

    With no responses every raw bucket is unknown (None); with responses
    the numbers are measured subtotals, a lower bound. Mixed semantics keep
    by_semantics only. Response counts and unknown_counts keep their
    response-level meaning.
    """
    if not sessions:
        return totals
    totals["unmeasured_sessions"] = sorted(sessions)
    if not totals.get("responses") and "by_semantics" not in totals:
        for bucket in BUCKETS:
            if bucket in totals:
                totals[bucket] = None
        totals["usage_status"] = "unknown"
    else:
        totals["usage_status"] = "lower_bound"
    return totals


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
    if session_keys is not None:
        _mark_unmeasured(totals, unmeasured_sessions(con, session_keys))
    return totals


# The ledger only knows what the last sync imported, so an empty or
# unknown task may just be unsynced work (#147).
STALE_LEDGER_HINT = ("if its work is recent, run `agent-observer sync`"
                     " and retry")


def task_sessions(con: sqlite3.Connection, task_id: str) -> set:
    """Sessions a task touches: those holding its assigned submissions or
    wholly assigned to it."""
    keys = {r["session_key"] for r in con.execute(
        "SELECT DISTINCT s.session_key FROM assignments a JOIN user_messages s"
        " ON s.native_id=a.user_message_id WHERE a.task_id=?"
        " AND s.session_key IS NOT NULL", (task_id,))}
    canonical = outcome.active(con, task_id)
    keys.update(outcome.sessions(con, task_id))
    for r in con.execute(
            "SELECT session_key, evidence FROM session_assignments WHERE task_id=?", (task_id,)):
        if not outcome.legacy_link(r["evidence"]) or not canonical:
            keys.add(r["session_key"])
    keys |= {r["session_key"] for r in con.execute(
        "SELECT session_key FROM task_turns WHERE task_id=? AND session_key IS NOT NULL",
        (task_id,))}
    return keys


def _turn_submission(con: sqlite3.Connection) -> dict:
    """Map each session and turn to the genuine submission that started it.

    One turn follows one genuine user submission; the mapping uses the
    submission turn_id recorded at import. Tool results and assistant rows
    never create submissions.
    """
    mapping = {}
    for key, turn, msg in con.execute(
            "SELECT session_key,turn_id,native_id FROM user_messages WHERE is_genuine=1 AND turn_id IS NOT NULL"):
        mapping.setdefault((key, turn), msg)
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
    tables = ["responses", "events", "user_messages", "sessions"]
    for table in ("codex_occurrences", "opencode_response_observations"):
        if _has_table(con, table):
            tables.append(table)
    backing = " UNION ".join(
        f"SELECT DISTINCT source_id FROM {table}"
        f" WHERE session_key IN ({placeholders})" for table in tables)
    row = con.execute(
        f"SELECT MAX(imported_at) m FROM sources WHERE id IN ({backing})",
        keys * len(tables)).fetchone()
    return row["m"] if row is not None else None


def _snapshot_id(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _task_turn_ids(con: sqlite3.Connection, turns: set, whole: set) -> set:
    """Turn IDs for diagnostics, including turns in wholly owned sessions."""
    owned = {turn for _, turn in turns}
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
                      whole: set, task_turns: set) -> dict:
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


# Activity counter -> the capability family that must support it (#127).
ACTIVITY_CAPABILITIES = {
    "tool_calls": "tool_calls", "mcp_results": "tool_results",
    "reads": "read_evidence", "file_changes": "file_change",
    "failed_tool_results": "tool_results", "skill_invocations": "skill_invocation",
}


def _activity_coverage(cell: dict, harnesses: list, caps: dict) -> dict:
    """Per counter: unavailable when no contributing session supports it,
    partial when only some do. Unavailable counters become None."""
    coverage = {}
    for counter, family in ACTIVITY_CAPABILITIES.items():
        supported = [caps.get(h, {}).get(family) is not False for h in harnesses]
        if supported and not any(supported):
            cell[counter] = None
            coverage[counter] = "unavailable"
        elif not all(supported):
            coverage[counter] = "partial"
    return coverage


def _is_mcp_result(event) -> bool:
    """An MCP result by recorded identity; the mcp. name rule only for
    records that carry no tool_source (Codex)."""
    try:
        detail = json.loads(event["detail_json"] or "{}")
    except (TypeError, ValueError):
        detail = {}
    if isinstance(detail, dict) and "tool_source" in detail:
        return detail["tool_source"] == "mcp"
    return (event["name"] or "").startswith("mcp.")


def _phase_report(con, task_id, rows, whole, attempts, schedule,
                  unmeasured=frozenset(), native_sessions=(), ownership=None):
    from . import pricing, analysis
    ownership = ownership or outcome.Ownership()
    by_turn, by_session = {}, {}

    def phase(value):
        if value in PHASES or value == "mixed":
            return value
        if value in ("implementation_default", "implementation_small", "implementation_hard"):
            return "implementation"
        if value in ("review_final", "review_ticket"):
            return "review"
        return "unclassified"

    for a in con.execute("SELECT COALESCE(s.turn_id,s.native_id) AS turn_id,a.phase FROM assignments a JOIN user_messages s"
                         " ON s.native_id=a.user_message_id WHERE a.task_id=?", (task_id,)):
        if a["turn_id"] and a["phase"]:
            by_turn.setdefault(a["turn_id"], set()).add(phase(a["phase"]))
    for a in attempts:
        # A captured phase on the exact turn outranks missing submission labels.
        if a.get("stage") and a.get("harness") != "router":
            by_turn[a["turn_id"]] = {phase(a["stage"])}
        if a.get("session_key") in whole and a.get("harness") == "router":
            by_session.setdefault(a["session_key"], set()).add(phase(a.get("stage")))

    def selected(row, literal_turns=None):
        if literal_turns is None:
            if (row["session_key"] in native_sessions and row["session_key"] not in whole
                    and (row["session_key"], row["turn_id"]) not in ownership.turns):
                return "unclassified"
            literal_turns = (row["turn_id"],)
        labels = {label for turn in literal_turns for label in by_turn.get(turn, ())}
        labels = labels or by_session.get(row["session_key"]) or {"unclassified"}
        return next(iter(labels)) if len(labels) == 1 else "mixed"

    groups = {}
    for row in rows:
        literal_turns = ownership.response_turns.get((row["session_key"], row["response_id"]))
        groups.setdefault(selected(row, literal_turns), []).append(row)
    activities = {}
    contributors: dict[str, dict[str, str | None]] = {}
    turns = {r["turn_id"] for r in rows if r["turn_id"]}
    # Exclusively owned whole sessions count even without responses (#127).
    for key in sorted({r["session_key"] for r in rows} | set(whole)):
        harness = _session_harness(con, key)
        event_turns = dict(ownership.event_turns(con, key))
        for event in con.execute("SELECT * FROM events WHERE session_key=?", (key,)):
            literal_turns = event_turns.get(event["id"])
            if event["id"] in event_turns:
                owned = literal_turns is not None
            elif key in native_sessions:
                owned = key in whole or (key, event["turn_id"]) in ownership.turns
            else:
                owned = key in whole or event["turn_id"] in turns
            label = selected(event, literal_turns) if owned else "session context"
            # Capability comes from the event's own native session, never
            # from the importer that attributed it (Router).
            contributors.setdefault(label, {})[key] = harness
            cell = activities.setdefault(label, {"tool_calls": 0, "mcp_results": 0,
                "reads": 0, "file_changes": 0, "failed_tool_results": 0,
                "skill_invocations": 0})
            if event["family"] == "tool_call": cell["tool_calls"] += 1
            if event["family"] == "tool_result":
                if _is_mcp_result(event): cell["mcp_results"] += 1
                if analysis._failed(event): cell["failed_tool_results"] += 1
            if event["family"] == "read": cell["reads"] += 1
            if event["family"] == "file_change": cell["file_changes"] += 1
            if event["family"] == "skill_invoke": cell["skill_invocations"] += 1
    caps = _capabilities()
    coverage = {label: _activity_coverage(cell, list(contributors[label].values()), caps)
                for label, cell in activities.items()}
    context = activities.pop("session context", {})
    context_coverage = coverage.pop("session context", {})

    def phase_entry(name):
        group = groups.get(name, [])
        missing = sorted(k for k in contributors.get(name, {}) if k in unmeasured)
        usage = _mark_unmeasured(_usage_totals(group), missing)
        estimate = pricing.price_scope(group, schedule)
        if missing:
            # Same rule as the task-level gap: no complete total, any priced
            # subtotal kept.
            estimate.update(complete=False, estimated_cost_usd_total=None,
                            status="partial" if estimate.get("priced_responses") else "unknown",
                            coverage_note="Unmeasured sessions: native usage is not imported")
        return {"phase": name, "usage": usage, "models": model_usage(group),
                "estimated_cost": estimate,
                "activity": activities.get(name, {}),
                "activity_coverage": coverage.get(name, {}),
                "activity_note": "Recorded observations, not separately billed token costs; coverage varies by harness."}

    phases = [phase_entry(name) for name in sorted(set(groups) | set(activities))]
    return phases, context, context_coverage


def task_report(con: sqlite3.Connection, task_id: str,
                schedule: dict | None = None) -> dict:
    from . import pricing as _pricing
    if schedule is None:
        # No explicit override: T3's rate table when present, bundled
        # fallback otherwise, with the winning source labeled.
        schedule = _pricing.default_schedule(con)
    # Retain the exact selected schedule with the export. A caller changing
    # its own dictionary later must not alter an already produced valuation.
    schedule = json.loads(json.dumps(schedule))
    task = con.execute("SELECT * FROM tasks WHERE task_id=?",
                       (task_id,)).fetchone()
    if task is None:
        raise KeyError(f"unknown task: {task_id}; {STALE_LEDGER_HINT}")
    turn_sub = _turn_submission(con)
    assigns = con.execute(
        "SELECT * FROM assignments WHERE task_id=?", (task_id,)).fetchall()
    bound_subs = {a["user_message_id"]: a for a in assigns}
    # Joint submissions bound to several tasks stay shared, never divided.
    joint = {}
    for a in con.execute(
            "SELECT user_message_id, COUNT(*) n FROM assignments "
            "GROUP BY 1 HAVING n > 1"):
        rows = con.execute(
            "SELECT task_id, shared FROM assignments WHERE "
            "user_message_id=?", (a["user_message_id"],)).fetchall()
        joint[a["user_message_id"]] = {
            "tasks": [r["task_id"] for r in rows],
            "all_shared": all(r["shared"] for r in rows),
        }

    scope_keys = task_sessions(con, task_id)
    whole = {r["session_key"]: r["evidence"] for r in con.execute(
        "SELECT session_key, evidence FROM session_assignments WHERE task_id=?",
        (task_id,))}
    ownership = outcome.evidence(con, task_id, scope_keys)
    native = ownership.native
    canonical = bool(native and native["runs"])
    if native:
        whole = {k: v for k, v in whole.items()
                 if not outcome.legacy_link(v) or (not canonical and k not in native["sessions"])}
    legacy_links = {key for key, ev in whole.items() if outcome.legacy_link(ev)}
    session_owners = defaultdict(set)
    linked_tasks = defaultdict(set)
    for key, owner, evidence in con.execute("SELECT session_key,task_id,evidence FROM session_assignments"):
        if key in scope_keys:
            claims = linked_tasks if outcome.legacy_link(evidence) else session_owners
            claims[key].add(owner)
    prompt_owners = defaultdict(set)
    assigned_anywhere = set()
    for key, msg, owner in con.execute(
            "SELECT s.session_key,s.native_id,a.task_id FROM assignments a "
            "JOIN user_messages s ON s.native_id=a.user_message_id WHERE s.is_genuine=1"):
        if key in scope_keys:
            prompt_owners[key].add(owner)
            assigned_anywhere.add(msg)
    whole_conflicts = {key for key, owners in session_owners.items()
                       if prompt_owners[key] and len(owners | prompt_owners[key]) > 1}
    whole_shared = whole_conflicts | {key for key, owners in session_owners.items() if len(owners) > 1}
    responses = _responses(con, scope_keys)
    attributed, shared, unassigned = [], [], []
    # A link can supply session context while the individual owners remain unknown.
    placements = {key: _place(con, key) for key in legacy_links
                  if len(linked_tasks[key]) > 1 and key not in session_owners}
    checkout_records = {}
    for key in sorted(legacy_links):
        checkout_records.update(placement.checkout_observations(con, key))
    unplaced, elsewhere = [], []
    missing_submissions = sorted({msg for ids in ownership.missing_by_response.values() for msg in ids})
    sharing_tasks = set()
    legacy_shared = set()
    conflicts = ownership.conflicts
    for r in responses:
        if r["is_overlap"]:
            continue
        key, rid = r["session_key"], r["response_id"]
        if rid in conflicts or rid in ownership.owners or (native and key in native["sessions"]):
            owners = ownership.owners.get(rid)
            if (rid in ownership.missing_by_response and key not in legacy_links
                    and not (native and key in native["sessions"])):
                unassigned.append(dict(r))
            elif rid in conflicts or not owners:
                unplaced.append(dict(r))
            elif task_id not in owners:
                elsewhere.append(dict(r))
            elif len(owners) > 1:
                shared.append(dict(r))
                sharing_tasks.update(owners)
            else:
                attributed.append(dict(r))
            continue
        sub = turn_sub.get((key, r["turn_id"]))
        if sub in bound_subs:
            if sub in joint or key in whole_conflicts:
                shared.append(dict(r))
                if sub in joint:
                    sharing_tasks.update(joint[sub]["tasks"])
                sharing_tasks.update(session_owners.get(key, ()))
            else:
                attributed.append(dict(r))
        elif task_id in session_owners.get(key, ()):
            # Keep the existing whole-session pool when several records bind
            # the session and this response has no explicit capture.
            if key in whole_shared:
                shared.append(dict(r))
                sharing_tasks.update(session_owners[key] | prompt_owners[key])
            else:
                attributed.append(dict(r))
        elif sub in assigned_anywhere or key in session_owners:
            elsewhere.append(dict(r))
        elif key in legacy_links:
            placed = placements.get(key)
            if placed is not None:
                owner = placed.get(rid)
                (attributed if owner == task_id else unplaced if owner is None else elsewhere).append(dict(r))
            elif len(linked_tasks[key]) > 1 and not prompt_owners[key]:
                shared.append(dict(r))
                legacy_shared.add(rid)
            else:
                unplaced.append(dict(r))
        else:
            unassigned.append(dict(r))
            if sub is not None and sub not in assigned_anywhere and sub not in missing_submissions:
                missing_submissions.append(sub)

    # Sessions are known but nothing is bound: the scope holds measured
    # responses while attributed and shared stay empty (for example every
    # prompt read synthetic, so per-prompt assignment refused them). This
    # is a visible coverage gap, never a silent "missing assignments: none".
    unbound_usage = sorted({r["session_key"] for r in unassigned + unplaced}
                           - {r["session_key"] for r in attributed + shared + elsewhere})

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
        "SELECT COUNT(*) n FROM task_turns WHERE task_id=? AND state='crashed'",
        (task_id,)).fetchone()["n"]
    outcome_row = _row_or_none(
        con.execute("SELECT * FROM outcomes WHERE task_id=?",
                    (task_id,)).fetchone())
    task_turn_rows = [dict(r) for r in con.execute(
        "SELECT * FROM task_turns WHERE task_id=? ORDER BY turn_id",
        (task_id,))]
    delegation_rows = [dict(r) for r in con.execute(
        "SELECT * FROM delegations WHERE owning_user_message IN "
        "(SELECT user_message_id FROM assignments WHERE task_id=?)",
        (task_id,))]
    owned_sessions = outcome.sessions(con, task_id) | set(whole) | {r["session_key"] for r in con.execute(
        "SELECT s.session_key FROM user_messages s JOIN assignments a"
        " ON a.user_message_id=s.native_id WHERE a.task_id=?", (task_id,))}

    def child_session(identity):
        exact = con.execute("SELECT session_key FROM sessions WHERE session_key=?",
                            (identity,)).fetchone()
        if exact:
            return exact["session_key"]
        matches = con.execute("SELECT session_key FROM sessions WHERE native_id=?",
                              (identity,)).fetchall()
        return matches[0]["session_key"] if len(matches) == 1 else None

    unbound_delegations = sorted({d["child_thread"] for d in delegation_rows
                                 if child_session(d["child_thread"]) not in owned_sessions})
    missing_sessions = sorted(key for key in scope_keys if not con.execute(
        "SELECT 1 FROM sessions WHERE session_key=?", (key,)).fetchone())
    empty_sessions = sorted(key for key in scope_keys if key not in missing_sessions
                            and not con.execute("SELECT 1 FROM responses WHERE session_key=?",
                                                (key,)).fetchone())
    # Children linked to an owned session but not owned by this task are a
    # gap; the link never assigns ownership (#127).
    owned_scope = sorted(owned_sessions & scope_keys)
    linked_children = {r["session_key"] for r in con.execute(
        "SELECT session_key FROM sessions WHERE parent_session_key IN"
        f" ({','.join('?' * len(owned_scope))})", owned_scope)} if owned_scope else set()
    unbound_children = sorted({a["session_key"] for a in task_turn_rows
                              if a.get("harness") == "router" and a.get("session_key")
                              and a["session_key"] not in whole}
                              | (linked_children - owned_sessions))
    # Unmeasured sessions join the partition their responses would take.
    unmeasured = unmeasured_sessions(con, scope_keys)
    prompt_sessions: dict[str, set] = {}
    for row in con.execute(
            "SELECT s.session_key, a.user_message_id FROM assignments a JOIN user_messages s"
            " ON s.native_id=a.user_message_id WHERE a.task_id=?", (task_id,)):
        prompt_sessions.setdefault(row["session_key"], set()).add(row["user_message_id"])
    shared_owned = (set(whole) & whole_shared) | whole_conflicts
    unmeasured_attributed, unmeasured_shared = set(), set()
    for key in unmeasured:
        subs = prompt_sessions.get(key, set())
        if key in shared_owned:
            unmeasured_shared.add(key)
        elif key in ownership.whole or any(s not in joint for s in subs):
            unmeasured_attributed.add(key)
        elif subs:
            unmeasured_shared.add(key)
    has_purpose = native and (canonical or native["deliveries"] or native["conflicting_source_runs"])
    inventory_gaps = outcome.check_inventory(con, native, attributed) if has_purpose else False
    unresolved_usage = bool(unplaced or unassigned)
    unavailable_usage = bool(unresolved_usage or conflicts or inventory_gaps or legacy_links
                             or not scope_keys or missing_sessions or empty_sessions
                             or unbound_children or unbound_delegations or whole_conflicts)
    cutoff = _source_cutoff(con, scope_keys)
    task_turns = _task_turn_ids(con, ownership.turns, ownership.whole)
    diagnostics = _task_diagnostics(con, scope_keys, ownership.whole, task_turns)
    phases, activity_context, activity_context_coverage = _phase_report(
        con, task_id, attributed, ownership.whole - whole_shared, task_turn_rows, schedule,
        unmeasured_attributed, native["sessions"] if native else (), ownership)

    def _resp_tuple(r) -> list:
        return [r.get("response_id"), r.get("harness"), r.get("model"),
                r.get("effort"), _semantics_key(r), r.get("turn_id"),
                r.get("input_tokens"), r.get("cached_input_tokens"),
                r.get("cache_write_input_tokens"), r.get("output_tokens"),
                r.get("reasoning_output_tokens"), r.get("total_tokens"),
                r.get("cost_usd"), r.get("cache_write_5m_tokens"), r.get("cache_write_1h_tokens")]

    from . import lookup
    source_availability = lookup.availability(con, session_keys=scope_keys)
    unavailable_sessions = sorted(
        key for key, status in (
            source_availability.get("by_session") or {}).items()
        if status != "available")
    unavailable_sources = list(
        source_availability.get("unavailable_sources") or [])
    snapshot_payload = {
        **({"legacy_checkout_observations": checkout_records} if checkout_records else {}),
        "price_schedule": schedule,
        "phases": phases,
        "activity_session_context": activity_context,
        "activity_session_context_coverage": activity_context_coverage,
        "coverage_evidence": {"missing_submissions": sorted(missing_submissions),
                              "conflicting_sessions": sorted(whole_conflicts),
                              "joint": joint, "whole_shared": sorted(whole_shared),
                              "missing_sessions": missing_sessions,
                              "empty_sessions": empty_sessions,
                              "unbound_children": unbound_children,
                              "unbound_delegations": unbound_delegations,
                              "source_availability": source_availability["status"],
                              "ledger_only_events": source_availability["events"]["ledger_only"],
                              "unavailable_sessions": unavailable_sessions,
                              "unavailable_sources": unavailable_sources},
        "task_id": task_id,
        "sessions": sorted(scope_keys),
        "attributed": sorted(_resp_tuple(r) for r in attributed),
        "shared": sorted(_resp_tuple(r) for r in shared),
        "unassigned": sorted(_resp_tuple(r) for r in unassigned),
        "unplaced": sorted(_resp_tuple(r) for r in unplaced),
        "elsewhere": sorted(_resp_tuple(r) for r in elsewhere),
        "assignments": sorted(
            [a["user_message_id"], a["task_id"], a["shared"]]
            for a in con.execute(
                "SELECT user_message_id, task_id, shared FROM assignments"
                " WHERE task_id=?", (task_id,))),
        "outcome": outcome_row,
        "task_turns": sorted(
            [a.get("turn_id"), a.get("role"), a.get("state")] for a in task_turn_rows),
        "delegations": sorted(
            [d.get("owning_user_message"), d.get("child_thread")] for d in delegation_rows),
        "source_cutoff": cutoff,
    }
    if native:
        snapshot_payload["native_purpose"] = native
    snapshot = _snapshot_id(snapshot_payload)
    estimated = _pricing.price_scope(attributed, schedule)
    shared_estimated = _pricing.price_scope(shared, schedule)
    billed_shared = [r for r in shared if r["response_id"] not in legacy_shared]
    total_estimated = _pricing.price_scope(attributed + billed_shared, schedule)
    unplaced_estimated = _pricing.price_scope(unplaced, schedule)
    for estimate in (estimated, shared_estimated, total_estimated, unplaced_estimated):
        estimate["status"] = ("complete" if estimate["complete"] else
                              "partial" if estimate["priced_responses"] else "unknown")
        if unavailable_usage:
            estimate.update(complete=False, estimated_cost_usd_total=None,
                            status="partial" if estimate["priced_responses"] else "unknown",
                            coverage_note="Native session usage or worker ownership is unavailable")
    native_cost = _native_cost(attributed)
    if unavailable_usage:
        native_cost["total_usd"] = None
    acceptance = (outcome_row or {}).get("acceptance_state") or "unknown"
    # Acceptance is explicit only: process success, zero exit or a
    # successful attempt never implies an accepted outcome.
    active_task_turns = [a for a in task_turn_rows if a.get("state") == "active"]
    report = {
        "task": dict(task),
        "attributed": _mark_unmeasured(total(attributed), unmeasured_attributed),
        "models": model_usage(attributed),
        "phases": phases,
        "activity_session_context": activity_context,
        "activity_session_context_coverage": activity_context_coverage,
        "shared_joint": _mark_unmeasured(total(shared), unmeasured_shared),
        "shared_models": model_usage(shared),
        "unassigned_in_scope": total(unassigned),
        "unplaced_shared": total(unplaced),
        "placed_on_other_tasks": total(elsewhere),
        "estimated_cost_unplaced": unplaced_estimated,
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
        "task_turns": task_turn_rows,
        "delegations": delegation_rows,
        "active_task_turns": active_task_turns,
        "has_active_work": bool(active_task_turns) or acceptance == "active",
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
            "unplaced_responses": sorted(r.get("response_id") for r in unplaced),
            "other_task_responses": sorted(r.get("response_id") for r in elsewhere),
            "source_cutoff": cutoff,
        },
        "diagnostics": diagnostics,
        "time": {
            "task_elapsed_s": diagnostics["task_elapsed_s"],
            "task_elapsed_source": diagnostics["task_elapsed_source"],
            "session_span_s": diagnostics["session_span_s"],
            "session_span_label": diagnostics["session_span_label"],
        },
        "native_cost": native_cost,
        "native_cost_shared": _native_cost(shared),
        "native_cost_billed_shared": _native_cost(billed_shared),
        "estimated_cost": estimated,
        "estimated_cost_shared": shared_estimated,
        "subscription_note": "Router quota readings are account evidence, "
                             "not task cost; subscription billing is separate "
                             "and is never posted as spend.",
        "coverage": {
            "no_measured_sessions": not scope_keys,
            "unbound_usage": list(unbound_usage),
            "conflicting_sessions": sorted(whole_conflicts),
            "missing_sessions": missing_sessions,
            "sessions_without_usage": empty_sessions,
            "unbound_child_sessions": unbound_children,
            "unbound_delegations": unbound_delegations,
            "missing": list(missing_submissions),
            "conflicting": [c["submission"] for c in [
                {"submission": s, **j} for s, j in joint.items()
                if not j["all_shared"] and s in bound_subs]],
            "joint": [c["submission"] for c in [
                {"submission": s, **j} for s, j in joint.items()
                if j["all_shared"] and s in bound_subs]],
            "active_task_turns": [a.get("turn_id") for a in active_task_turns],
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
        + report["unassigned_in_scope"]["responses"]
        + len(unplaced) + len(elsewhere) == scope["responses"]
    )
    if "total_tokens" in scope:
        totals_reconcile = (
            _known(report["attributed"]["total_tokens"])
            + _known(report["shared_joint"]["total_tokens"])
            + _known(report["unassigned_in_scope"]["total_tokens"])
            + sum(_known(r["total_tokens"]) for r in unplaced + elsewhere)
            == _known(scope["total_tokens"])
        )
    else:
        parts = _sem_known_sums(attributed)
        for rows in (shared, unassigned, unplaced, elsewhere):
            for sem, value in _sem_known_sums(rows).items():
                parts[sem] = parts.get(sem, 0) + value
        wholes = _sem_known_sums(live_scope)
        totals_reconcile = all(
            parts.get(sem, 0) == value
            for sem, value in wholes.items()) and all(
            parts.get(sem, 0) == wholes.get(sem, 0)
            for sem in parts)
    report["reconciles"] = responses_reconcile and totals_reconcile
    if legacy_links:
        report["coverage"]["legacy_thread_links"] = sorted(legacy_links)
    if checkout_records:
        report["legacy_checkout_observations"] = {"basis": "first_native_import", "responses": checkout_records}
    if native:
        report["native_purpose"] = native
    report["unbound_usage"] = unbound_usage
    report["source_availability"] = {
        "status": source_availability["status"],
        "events": source_availability["events"],
        "reasons": source_availability["reasons"],
    }
    report["complete"] = (not missing_submissions
                          and not report["conflicting_assignments"]
                          and not unavailable_usage
                          and not report["has_active_work"]
                          and source_availability["status"] == "available")
    report["coverage"]["reconciles"] = report["reconciles"]
    report["coverage"]["complete"] = report["complete"]
    report["coverage"]["source_availability"] = source_availability["status"]
    report["coverage"]["ledger_only_events"] = (
        source_availability["events"]["ledger_only"])
    report["coverage"]["unavailable_sessions"] = unavailable_sessions
    report["coverage"]["unavailable_sources"] = unavailable_sources
    report["total_cost"] = _total_cost(
        task_id, total_estimated, attributed, billed_shared, sharing_tasks)
    if unavailable_usage and total_estimated.get("priced_responses"):
        subtotal = total_estimated["estimated_cost_usd_partial"]
        amount = "under $0.01" if 0 < subtotal < 0.005 else f"${subtotal:,.2f}"
        report["total_cost"].update(usd=None, observed_subtotal_usd=subtotal,
                                    text=f"unknown (observed priced subtotal {amount})")
    report["total_cost"]["unplaced"] = {
        "text": _money(unplaced_estimated) if unplaced else None,
        "responses": len(unplaced),
        "other_task_responses": len(elsewhere),
    }
    return report


def _place(con, session_key: str):
    from . import placement
    return placement.read_placements(con, session_key)


def _money(estimate: dict) -> str:
    """One estimate as money: exact when complete, a floor when partial."""
    value = estimate.get("estimated_cost_usd_total")
    if value is None:
        if not estimate.get("priced_responses"):
            return "unknown"
        value = estimate.get("estimated_cost_usd_partial") or 0.0
        prefix = "at least "
    else:
        prefix = ""
    text = "under $0.01" if 0 < value < 0.005 else f"${value:,.2f}"
    return prefix + text


def _total_cost(task_id, estimate, attributed, shared, sharing_tasks) -> dict:
    """One total for the task's work: its own usage plus the whole shared pool.

    Shared usage is never divided, so every task drawing on a pool counts
    it whole: the total is right for one task, and sums across tasks
    overlap by the shared part. ``shared_with_tasks`` names those tasks.
    """
    # Nothing priced is unknown, even when missing usage marks the estimate
    # partial: an empty subtotal must never read as a numeric zero.
    status = estimate["status"] if estimate.get("priced_responses") else "unknown"
    return {
        "text": _money(estimate) if status != "unknown" else "unknown",
        "usd": estimate.get("estimated_cost_usd_total") if status == "complete"
        else estimate.get("estimated_cost_usd_partial") if status == "partial"
        else None,
        "status": status,
        "responses": len(attributed) + len(shared),
        "shared_responses": len(shared),
        "shared_with_tasks": sorted(sharing_tasks - {task_id}) if shared else [],
        "models": model_usage(attributed + shared),
        "estimate": estimate,
    }


def timeline(con: sqlite3.Connection, task_id: str | None = None,
             turn_id: str | None = None, family: str | None = None,
             limit: int = 200, session_key: str | None = None) -> dict:
    q = "SELECT * FROM events WHERE 1=1"
    args: list = []
    if session_key:
        q += " AND session_key=?"
        args.append(session_key)
    literal_events = set()
    trace_scope = {}
    if task_id is not None:
        ownership = outcome.evidence(con, task_id, task_sessions(con, task_id))
        native = ownership.native
        excluded_events = set()
        if native:
            matches = {rid: member for rid, member in native["membership"].items()
                       if (not session_key or member["session_key"] == session_key)
                       and (not turn_id or turn_id == (rid if member["session_key"].startswith("grok:")
                            else member["session_key"].partition(":")[0] + ":" + member["native_input_id"]))}
            if native["runs"]:
                trace_scope = {"native_response_count": len(matches),
                               "native_responses": {rid: matches[rid] for rid in sorted(matches)[:limit]}}
        for key in ownership.literal_sessions:
            if session_key and key != session_key:
                continue
            for event, turns in ownership.event_turns(con, key):
                if turns is not None and (not turn_id or turn_id in turns):
                    literal_events.add(event)
                else:
                    excluded_events.add(event)
        if not ownership.turns and not ownership.whole and not literal_events:
            return {**trace_scope, "events": [], "returned": 0, "total": 0,
                    "truncated": False,
                    "note": "no bound turns; timeline empty",
                    "join": {"calls": 0, "results": 0, "joined_call_ids": [],
                             "unmatched_results": [], "unanswered_calls": []}}
        clauses = []
        if excluded_events:
            q += " AND id NOT IN (SELECT value FROM json_each(?))"
            args.append(json.dumps(sorted(excluded_events)))
        if literal_events:
            clauses.append("id IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(sorted(literal_events)))
        if ownership.turns:
            pairs = sorted(ownership.turns)
            clauses.append("(session_key, turn_id) IN (VALUES " + ",".join(["(?,?)"] * len(pairs)) + ")")
            for pair in pairs:
                args.extend(pair)
        if ownership.whole:
            clauses.append(f"session_key IN ({','.join('?' * len(ownership.whole))})")
            args.extend(sorted(ownership.whole))
        q += " AND (" + " OR ".join(clauses) + ")"
    if turn_id:
        if literal_events:
            q += " AND (turn_id=? OR id IN (SELECT value FROM json_each(?)))"
            args.extend((turn_id, json.dumps(sorted(literal_events))))
        else:
            q += " AND turn_id=?"
            args.append(turn_id)
    if family:
        q += " AND family=?"
        args.append(family)
    # Source order is applied to the assembled rows. The representative
    # ordinal is one file's locator, so limiting by it can drop an earlier
    # call whose locator was written by a later file.
    q += " ORDER BY COALESCE(ts, 0), ordinal_num, id"
    rows = con.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if trace_scope:
            d.pop("detail_json", None)
            d.pop("target", None)
            out.append(d)
            continue
        try:
            d["detail"] = json.loads(d["detail_json"]) if d["detail_json"] else None
        except ValueError:
            d["detail"] = {"raw": d["detail_json"]}
        d.pop("detail_json", None)
        out.append(d)
    sessions = {}
    for event in out:
        sessions.setdefault(event["session_key"], []).append(event)
    unknown = {}
    ordered_sessions = {}
    for key, events in sessions.items():
        wanted = {("event", str(event["id"])) for event in events}
        state, order, sequences = codex_order.resolve(con, key, wanted)
        if state == "known" and order is not None:
            index = {}
            for place, group in enumerate(order):
                for ident in group:
                    index[ident] = place
            ordered_sessions[key] = iter(sorted(events, key=lambda item: (
                index.get(("event", str(item["id"])), len(index)),
                item["id"])))
            if len(sessions) > 1:
                unknown[key] = [order]
        elif state in ("partial", "conflict"):
            unknown[key] = sequences
    # Apply supported source order within that session's existing slots.
    # Unrelated timestamped rows keep their interleaving, and the limit
    # still sees the corrected order when locators came from later files.
    out = [next(ordered_sessions[event["session_key"]])
           if event["session_key"] in ordered_sessions else event
           for event in out]
    total = len(out)
    out = out[:limit]
    # Tool join status: calls joined to results only on equal call_id.
    calls = {e["native_id"] for e in out if e["family"] == "tool_call"}
    results = {e["native_id"] for e in out if e["family"] == "tool_result"}
    result = {
        **trace_scope, "events": out,
        "returned": len(out),
        "total": total,
        "truncated": len(out) < total,
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
    if result["truncated"]:
        result["truncation_hint"] = "Increase --limit to show more events."
    if unknown:
        result["chronology"] = "unknown"
        result["source_sequences"] = {
            key: [[[list(ident) for ident in sorted(group)] for group in sequence]
                  for sequence in sequences]
            for key, sequences in unknown.items()}
    return result


def capabilities() -> list[dict]:
    from .adapters import capabilities as adapter_capabilities
    return adapter_capabilities()


def _row_or_none(row) -> dict | None:
    return dict(row) if row is not None else None
