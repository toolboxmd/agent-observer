"""Timing, failure and recovery evidence derived from existing records.

Stdlib only. All metrics derive from explicit task, submission,
invocation, attempt and outcome rows. Missing evidence stays visible as
unknown or missing lists. Nothing is inferred across scopes.

Meanings (also in GLOSSARY.md and docs/contracts.md):
- Submission time is the earliest explicit bound submission timestamp.
- Accepted completion is the first explicit outcome row with
  acceptance_state complete, at its first recorded updated_at.
  Re-recording a complete outcome to add proof or metadata never moves
  that timestamp. Failed, cancelled and unaccepted tasks never acquire
  an invented accepted completion time.
- Submission-to-accepted-completion elapsed time exists only when both
  endpoints above are explicit. A known accepted completion without a
  submission timestamp is reported as known but elapsed-unavailable,
  never as no accepted completion. Negative endpoint differences are
  rejected and qualified, never reported as elapsed.
- Active tasks report elapsed-so-far at the named source cutoff.
- Partial execution span is the owned-turn span from diagnostics. It is
  not accepted completion time.
- Observed wall time is the measured duration of one attempt or
  session. It is never called active thinking time and no unobserved
  phase is inferred from it.
- Waiting intervals derive only from explicit compatible start and end
  timestamps under the same known session or the same Router request,
  with no overlap. Parallel overlap is not waiting.
- Parallel attempt durations never become task elapsed time.
- Router and native evidence for one execution is reconciled into one
  execution identity on explicit turn/session/time evidence. No
  duration or attempt count is doubled. Shared sessions and unknown
  ownership stay qualified and are never auto-merged.
- Failure class derives from explicit terminal and stage evidence
  only. Router reason describes why an invocation was launched and
  never classifies a later failure. Missing terminal evidence stays
  unknown. Context pressure is a provider signal, not infrastructure.
  A supervisor rc124 with no proof outcome (startup failure) is
  infrastructure; an executed proof rc124 (proof_class timeout) is a
  timeout. Legacy rows without rc stay on terminal evidence alone.
- Production attempts are complete plus failed plus quota_blocked
  provider exhaustion. Quota exhaustion is a provider failure attempt
  with separate quota visibility. Intentional cancellation needs
  explicit intent evidence; a bare Router cancelled stays intent
  unknown and outside production. Crashes stay separately counted.
  Active and unknown states stay outside.
- Recovery pairs only compatible execution identities: the same Router
  request_id resolved through router_invocations, or the same known
  non-shared native session/ownership. Another request, another
  session, sort order alone or shared context never pairs. Next start
  must be at or after failed end. First subsequent progress is the
  first compatible completion at any stage, labelled with its stage
  and measured at its end, not its start. Recovery outcome, recovered
  status, active status and repeated counts are stage specific: failed
  and candidate stages must both be known and equal, with unknown
  never matching. Another attempt starting alone is not successful
  recovery.
"""

from __future__ import annotations

import sqlite3

FAILURE_CLASSES = ("timeout", "stall", "provider", "infrastructure",
                   "implementation", "verification")

# Production denominator: finished execution attempts whose outcome is a
# genuine execution result. Complete plus failed plus quota_blocked
# provider exhaustion. Intentional cancellation needs explicit intent
# evidence and stays outside; bare Router cancelled is intent unknown
# and stays outside. Crashes are counted separately. Active and unknown
# states stay outside. Quota exhaustion remains visible separately as
# well as inside failed/production.
PRODUCTION_STATES = frozenset({"complete", "failed", "quota_blocked"})

# Attempt states treated as failures for failure and recovery timing.
# quota_blocked is Router terminal_class quota imported as an attempt
# state; it is a provider failure attempt, not a separate non-failure.
FAILURE_STATES = frozenset({"failed", "quota_blocked"})


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def failure_class(attempt: dict) -> str | None:
    """Observed failure class for one attempt, or None when not a failure.

    Only state failed and state quota_blocked are production attempt
    failures. quota_blocked (Router terminal_class quota) is a provider
    failure. Cancelled (intent unknown without explicit evidence),
    crashed (counted separately), active and unknown states never become
    failure classes. Expected failures inside successful regression
    tests have no explicit marker in the current ledger and stay out of
    scope; they are not counted here.

    Classification uses explicit terminal_class, stage, rc and
    proof_class only. Router reason (pool_move, lateral,
    dispatch_stalled, preflight_* and others) describes why an
    invocation was launched and never classifies a later failure.
    Context pressure (terminal context) is a provider capacity signal,
    not infrastructure. A supervisor rc124 with no proof outcome is
    infrastructure (the suite never ran); an executed proof rc124
    with proof_class timeout stays timeout. Legacy rows without rc
    keep terminal-only behavior, so old timeout rows stay timeout.
    """
    state = attempt.get("state")
    if state == "quota_blocked":
        return "provider"
    if state != "failed":
        return None
    terminal = attempt.get("terminal_class")
    stage = attempt.get("stage")
    rc = attempt.get("rc")
    proof_class = attempt.get("proof_class")
    role = attempt.get("role")
    harness = attempt.get("harness")
    if terminal == "timeout":
        # Proof timeout stays timeout: an executed proof ran past its
        # budget (kind proof or proof_class timeout). A supervisor
        # startup rc124 with no proof outcome is infrastructure (the
        # suite never ran). Legacy rows without rc keep timeout.
        is_proof = (role == "proof" or proof_class == "timeout")
        if is_proof:
            return "timeout"
        if rc == 124 and harness == "router" and role != "proof":
            return "infrastructure"
        # Native timeouts and legacy Router timeouts without rc stay
        # timeout.
        return "timeout"
    if terminal == "stalled":
        return "stall"
    if terminal in ("overloaded", "quota"):
        return "provider"
    if terminal == "hard_error":
        return "infrastructure"
    if terminal == "context":
        return "provider"
    if stage == "verification":
        return "verification"
    if terminal == "failed":
        return "implementation"
    if terminal is None:
        # Failed without a recorded terminal class: classification is
        # missing evidence, not a guessed implementation failure.
        return None
    # Any other closed terminal value on a failed attempt is a generic
    # execution failure with the evidence we hold.
    return "implementation"


def attempt_wall_time(attempt: dict) -> tuple[float | None, str]:
    """Observed wall time for one attempt with its source label."""
    elapsed = attempt.get("elapsed_s")
    if isinstance(elapsed, bool):
        elapsed = None
    if isinstance(elapsed, (int, float)):
        return float(elapsed), "explicit elapsed_s"
    started = attempt.get("started_at")
    ended = attempt.get("ended_at")
    if isinstance(started, bool) or isinstance(ended, bool):
        started, ended = None, None
    if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
        return float(ended) - float(started), "derived from explicit start/end"
    missing = []
    if not isinstance(started, (int, float)):
        missing.append("started_at")
    if not isinstance(ended, (int, float)):
        missing.append("ended_at")
    if not isinstance(elapsed, (int, float)):
        missing.append("elapsed_s")
    return None, "missing timing (%s)" % (", ".join(missing) if missing else "unknown")


def completion_timing(con: sqlite3.Connection, task_id: str,
                      outcome_row: dict | None,
                      source_cutoff: float | None,
                      partial_span_s: float | None,
                      partial_source: str | None,
                      session_span_s: float | None) -> dict:
    """Submission to accepted completion timing with explicit endpoints."""
    subs = con.execute(
        "SELECT s.native_id, s.ts FROM assignments a JOIN submissions s"
        " ON s.native_id=a.submission_native_id WHERE a.task_id=?",
        (task_id,)).fetchall()
    timed = [(r["native_id"], r["ts"]) for r in subs
             if isinstance(r["ts"], (int, float)) and not isinstance(r["ts"], bool)]
    missing_subs = sorted(r["native_id"] for r in subs
                          if not isinstance(r["ts"], (int, float)) or isinstance(r["ts"], bool))
    submission_time = min((ts for _, ts in timed), default=None)
    submission_id = None
    if timed:
        submission_id = sorted(timed, key=lambda kv: (kv[1], kv[0]))[0][0]
    acceptance = (outcome_row or {}).get("acceptance_state") or "unknown"
    accepted_raw = (outcome_row or {}).get("updated_at")
    # Only a complete outcome carries an accepted completion time.
    # Failed, cancelled and unaccepted rows never acquire one, even
    # though they carry their own updated_at for bookkeeping.
    if acceptance == "complete" and _is_num(accepted_raw):
        accepted_at = accepted_raw
    else:
        accepted_at = None
    completed_s = None
    label = "unavailable (no accepted completion)"
    status = "unavailable"
    if acceptance == "complete" and accepted_at is not None:
        if submission_time is not None:
            diff = float(accepted_at) - float(submission_time)
            if diff < 0:
                completed_s = None
                label = ("unavailable (negative endpoint difference; "
                         "accepted completion precedes submission time)")
                status = "unavailable"
            else:
                completed_s = diff
                label = "submission-to-accepted-completion"
                status = "complete"
        else:
            completed_s = None
            label = ("accepted completion known but elapsed unavailable "
                     "(missing submission time)")
            status = "accepted-no-elapsed"
    elif acceptance == "complete" and accepted_at is None:
        label = "unavailable (accepted completion timestamp missing)"
        status = "unavailable"
    elif acceptance == "active":
        if submission_time is not None and _is_num(source_cutoff) \
                and float(source_cutoff) >= float(submission_time):
            completed_s = float(source_cutoff) - float(submission_time)
            label = "elapsed-so-far (active, cutoff=source_cutoff)"
            status = "active-so-far"
        else:
            label = "unavailable (active task needs submission time and source cutoff)"
            status = "unavailable"
    elif acceptance in ("failed", "cancelled", "quota_blocked", "crashed", "unknown"):
        label = ("unavailable (no accepted completion; failed, cancelled and "
                 "unaccepted tasks never acquire an invented accepted-completion time)")
        status = "unavailable"
    missing = []
    if submission_time is None:
        missing.append("submission_time")
    if accepted_at is None:
        missing.append("accepted_completion_time")
    if acceptance == "active" and not _is_num(source_cutoff):
        missing.append("source_cutoff")
    return {
        "submission_time": submission_time,
        "submission_id": submission_id,
        "submissions_timed": len(timed),
        "submissions_missing_timing": missing_subs,
        "acceptance_state": acceptance,
        "accepted_completion_time": accepted_at,
        "completion_elapsed_s": completed_s,
        "completion_label": label,
        "completion_status": status,
        "cutoff": source_cutoff,
        "cutoff_kind": "source_cutoff (max native imported_at backing the scope)",
        "partial_execution_span_s": partial_span_s,
        "partial_execution_span_source": (partial_source or "") + (
            "; partial execution span, not accepted completion" if partial_span_s is not None else ""),
        "session_span_s": session_span_s,
        "session_span_label": "session context, not task-only",
        "missing": missing,
    }


def _native_turns_for(con: sqlite3.Connection, session_key: str) -> list[dict]:
    try:
        rows = con.execute(
            "SELECT turn_id, started_at, completed_at FROM turns WHERE session_key=?",
            (session_key,)).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [dict(r) for r in rows]


def _native_session_span(con: sqlite3.Connection, session_key: str) -> dict:
    try:
        row = con.execute(
            "SELECT started_at, ended_at FROM sessions WHERE session_key=?",
            (session_key,)).fetchone()
    except sqlite3.DatabaseError:
        return {"started_at": None, "ended_at": None}
    if row is None:
        return {"started_at": None, "ended_at": None}
    return {"started_at": row["started_at"], "ended_at": row["ended_at"]}


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    if not all(_is_num(v) for v in (a_start, a_end, b_start, b_end)):
        return False
    return max(a_start, b_start) <= min(a_end, b_end)


def _union_span(windows: list[tuple[float, float]]) -> float | None:
    """Merged covered duration of explicit attempt windows.

    Overlapping or adjacent windows merge; gaps stay excluded. A field
    named union span must be this covered duration, never first start
    to last end with gaps included.
    """
    clean = sorted((float(s), float(e)) for s, e in windows
                   if _is_num(s) and _is_num(e) and float(e) >= float(s))
    if not clean:
        return None
    total = 0.0
    cur_s, cur_e = clean[0]
    for s, e in clean[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    total += cur_e - cur_s
    return total


def _request_ids_for(con: sqlite3.Connection | None,
                     attempts_rows: list[dict]) -> dict[str, str | None]:
    """Map Router attempt turn_id to its router_invocations request_id."""
    mapping: dict[str, str | None] = {}
    if con is None:
        return {a.get("turn_id"): None for a in attempts_rows if a.get("turn_id")}
    for attempt in attempts_rows:
        turn = attempt.get("turn_id") or ""
        if not turn.startswith("router:"):
            mapping[attempt.get("turn_id")] = None
            continue
        invocation_id = turn.split("router:", 1)[1]
        try:
            row = con.execute(
                "SELECT request_id FROM router_invocations WHERE invocation_id=?",
                (invocation_id,)).fetchone()
        except sqlite3.DatabaseError:
            row = None
        mapping[attempt.get("turn_id")] = (
            row["request_id"] if row is not None else None)
    return mapping


def _turn_lookup(con: sqlite3.Connection | None, turn_ids: list[str]) -> dict:
    """Map native turn_id to its turns row for enrichment."""
    out: dict = {}
    if con is None:
        return out
    wanted = sorted({t for t in turn_ids if t and not t.startswith("router:")})
    for turn_id in wanted:
        try:
            row = con.execute(
                "SELECT turn_id, session_key, started_at, completed_at"
                " FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
        except sqlite3.DatabaseError:
            row = None
        if row is not None:
            out[turn_id] = dict(row)
    return out


def deduplicate_attempts(con: sqlite3.Connection | None,
                         attempts_rows: list[dict],
                         whole_shared: set | None = None,
                         whole_conflicts: set | None = None) -> tuple[list[dict], list[dict]]:
    """Group Router/native attempt rows describing one execution.

    A Router-dispatched worker following the capture procedure records
    `capture attempt --turn <native turn>` with no session or
    timestamps, while Router imports `router:<inv>` with the worker
    session and an overlapping window. Both rows describe one
    execution and must count once.

    A merge needs explicit matching evidence: the same known
    non-shared session plus overlapping explicit time windows, with
    the native side enriched through its turns row where possible.
    Shared sessions, unknown ownership and coincidental task or route
    matches never merge. Raw rows stay visible; counts, per-role and
    per-session aggregates, failure counts and recovery ordering use
    the deduplicated representatives.
    """
    shared = set(whole_shared or set())
    conflicts = set(whole_conflicts or set())
    tainted = shared | conflicts
    turn_ids = [a.get("turn_id") for a in attempts_rows if a.get("turn_id")]
    turns = _turn_lookup(con, turn_ids)
    n = len(attempts_rows)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    def enriched(idx: int) -> tuple[str | None, float | None, float | None]:
        attempt = attempts_rows[idx]
        session = attempt.get("session_key")
        started = attempt.get("started_at")
        ended = attempt.get("ended_at")
        if attempt.get("harness") != "router":
            turn = turns.get(attempt.get("turn_id"))
            if turn is not None:
                if not session:
                    session = turn.get("session_key")
                if not _is_num(started) or not _is_num(ended):
                    if _is_num(turn.get("started_at")) and _is_num(turn.get("completed_at")):
                        started = turn.get("started_at")
                        ended = turn.get("completed_at")
        if not _is_num(started):
            started = None
        if not _is_num(ended):
            ended = None
        return session, started, ended

    enriched_cache = [enriched(i) for i in range(n)]
    for i in range(n):
        if attempts_rows[i].get("harness") == "router":
            continue
        sess_i, s_i, e_i = enriched_cache[i]
        if not sess_i or sess_i in tainted:
            continue
        if not _is_num(s_i) or not _is_num(e_i) or float(e_i) < float(s_i):
            continue
        for j in range(n):
            if attempts_rows[j].get("harness") != "router":
                continue
            sess_j, s_j, e_j = enriched_cache[j]
            if sess_j != sess_i:
                continue
            if not _is_num(s_j) or not _is_num(e_j) or float(e_j) < float(s_j):
                continue
            if _overlaps(s_i, e_i, s_j, e_j):
                union(i, j)
    groups_map: dict[int, list[int]] = {}
    for i in range(n):
        groups_map.setdefault(find(i), []).append(i)
    groups: list[dict] = []
    representatives: list[dict] = []
    for root in sorted(groups_map):
        members = sorted(groups_map[root])
        rows = [attempts_rows[i] for i in members]
        router_members = [attempts_rows[i].get("turn_id") for i in members
                          if attempts_rows[i].get("harness") == "router"]
        native_members = [attempts_rows[i].get("turn_id") for i in members
                          if attempts_rows[i].get("harness") != "router"]
        is_duplicate_group = len(members) > 1 and bool(router_members) and bool(native_members)
        # Representative prefers the Router row (it carries invocation
        # timing, session and terminal evidence); otherwise the first.
        rep_idx = members[0]
        for i in members:
            if attempts_rows[i].get("harness") == "router":
                rep_idx = i
                break
        rep = dict(attempts_rows[rep_idx])
        rep["_group_id"] = root
        rep["_group_members"] = sorted(attempts_rows[i].get("turn_id") for i in members)
        rep["_is_duplicate_group"] = is_duplicate_group
        representatives.append(rep)
        groups.append({
            "group_id": root,
            "members": sorted(attempts_rows[i].get("turn_id") for i in members),
            "router_members": sorted(router_members),
            "native_members": sorted(native_members),
            "is_duplicate_group": is_duplicate_group,
            "representative": rep.get("turn_id"),
            "evidence": ("shared explicit session with overlapping explicit "
                         "windows; native side enriched through turns"
                         if is_duplicate_group else "single row; no merge evidence"),
        })
    representatives.sort(key=lambda a: (a.get("started_at") is None,
                                        a.get("started_at") if _is_num(a.get("started_at")) else 0,
                                        a.get("turn_id") or ""))
    return groups, representatives


def attempt_timing(con: sqlite3.Connection, attempts_rows: list[dict],
                   whole_shared: set, whole_conflicts: set) -> dict:
    """Per attempt wall time with Router/native reconciliation.

    A Router attempt bound to a native session with an overlapping native
    turn window names that turn as a second source for the same
    execution. A separately captured native attempt row for the same
    turn/session/window is deduplicated into one execution identity on
    explicit evidence (see deduplicate_attempts). Wall time is counted
    once, never summed across sources. Shared sessions and unknown
    ownership stay qualified on each row and never auto-merge.
    """
    detailed = []
    for attempt in attempts_rows:
        wall, source = attempt_wall_time(attempt)
        session_key = attempt.get("session_key")
        native_turns = _native_turns_for(con, session_key) if session_key else []
        native_span = _native_session_span(con, session_key) if session_key else {
            "started_at": None, "ended_at": None}
        reconciled_with = []
        reconciled_wall = wall
        reconciled_sources = ["attempt"]
        a_start = attempt.get("started_at")
        a_end = attempt.get("ended_at")
        for turn in native_turns:
            if _overlaps(a_start, a_end, turn.get("started_at"), turn.get("completed_at")):
                reconciled_with.append(turn["turn_id"])
        if reconciled_with:
            # Union once across the attempt window and overlapping native
            # turns. Never subtract whole-session span from task-turn span.
            starts = [a_start] + [t["started_at"] for t in native_turns
                                  if t["turn_id"] in reconciled_with]
            ends = [a_end] + [t["completed_at"] for t in native_turns
                              if t["turn_id"] in reconciled_with]
            if all(_is_num(v) for v in starts + ends):
                reconciled_wall = float(max(ends)) - float(min(starts))
                reconciled_sources = ["attempt", "native"]
        shared = bool(session_key and (session_key in whole_shared or session_key in whole_conflicts))
        unknown_owner = not session_key or not attempt.get("harness") or not attempt.get("role")
        detailed.append({
            "turn_id": attempt.get("turn_id"),
            "role": attempt.get("role"),
            "stage": attempt.get("stage"),
            "session_key": session_key,
            "model_observed": attempt.get("model_observed"),
            "effort_observed": attempt.get("effort_observed"),
            "route_requested": attempt.get("route_requested"),
            "reason": attempt.get("reason"),
            "state": attempt.get("state"),
            "terminal_class": attempt.get("terminal_class"),
            "failure_class": failure_class(attempt),
            "started_at": attempt.get("started_at"),
            "ended_at": attempt.get("ended_at"),
            "wall_time_s": wall,
            "wall_time_source": source,
            "wall_time_label": "observed wall time (not active thinking time)",
            "reconciled_wall_time_s": reconciled_wall,
            "reconciled_sources": reconciled_sources,
            "reconciled_native_turns": sorted(reconciled_with),
            "duplicate_native": bool(reconciled_with),
            "native_session_start": native_span["started_at"],
            "native_session_end": native_span["ended_at"],
            "shared_session": shared,
            "unknown_ownership": unknown_owner,
        })
    groups, representatives = deduplicate_attempts(
        con, attempts_rows, whole_shared, whole_conflicts)
    rep_by_turn = {r.get("turn_id"): r for r in representatives}
    for row in detailed:
        rep = rep_by_turn.get(row["turn_id"])
        # A raw row merged into a duplicate group names its execution
        # identity; single rows keep their own identity.
        if rep is not None and rep.get("_is_duplicate_group"):
            row["execution_id"] = rep.get("_group_members")
            row["execution_representative"] = rep.get("turn_id")
            row["duplicate_attempt_row"] = True
        else:
            row["execution_id"] = [row["turn_id"]]
            row["execution_representative"] = row["turn_id"]
            row["duplicate_attempt_row"] = False
    # Per session aggregation on deduplicated executions: merged covered
    # union span plus explicit sum kept separate. The sum is reported
    # for inspection; it never becomes task elapsed.
    request_ids = _request_ids_for(con, representatives)
    by_session: dict = {}
    for rep in representatives:
        # Wall for one execution is counted once: prefer the reconciled
        # wall already derived for the representative row.
        det = next((d for d in detailed if d["turn_id"] == rep.get("turn_id")), None)
        wall = det["reconciled_wall_time_s"] if det else None
        if not _is_num(wall):
            wall_raw, _ = attempt_wall_time(rep)
            wall = wall_raw
        key = rep.get("session_key") or "unknown"
        cell = by_session.setdefault(key, {
            "session_key": rep.get("session_key"),
            "attempts": 0, "roles": set(), "models": set(),
            "completed_attempts": 0, "active_attempts": 0,
            "walls": [], "windows": [],
            "shared_session": False, "unknown_ownership": False,
            "duplicate_native": False,
        })
        cell["attempts"] += 1
        if rep.get("role"):
            cell["roles"].add(rep["role"])
        if rep.get("model_observed"):
            cell["models"].add(rep["model_observed"])
        if rep.get("state") == "complete":
            cell["completed_attempts"] += 1
        if rep.get("state") == "active":
            cell["active_attempts"] += 1
        if _is_num(wall):
            cell["walls"].append(float(wall))
        if _is_num(rep.get("started_at")) and _is_num(rep.get("ended_at")) \
                and float(rep["ended_at"]) >= float(rep["started_at"]):
            cell["windows"].append((float(rep["started_at"]), float(rep["ended_at"])))
        sess = rep.get("session_key")
        is_shared = bool(sess and (sess in whole_shared or sess in whole_conflicts))
        is_unknown = not sess or not rep.get("harness") or not rep.get("role")
        det_dup = det.get("duplicate_native") if det else False
        cell["shared_session"] = cell["shared_session"] or is_shared
        cell["unknown_ownership"] = cell["unknown_ownership"] or is_unknown
        cell["duplicate_native"] = cell["duplicate_native"] or bool(det_dup)
    sessions = []
    for key in sorted(by_session):
        cell = by_session[key]
        sessions.append({
            "session_key": cell["session_key"],
            "attempts": cell["attempts"],
            "roles": sorted(cell["roles"]),
            "models": sorted(cell["models"]),
            "completed_attempts": cell["completed_attempts"],
            "active_attempts": cell["active_attempts"],
            "wall_sum_s": sum(cell["walls"]) if cell["walls"] else None,
            "wall_sum_note": ("sum of observed attempt wall times for inspection; "
                              "parallel durations never become task elapsed time"),
            "union_span_s": _union_span(cell["windows"]),
            "union_span_note": ("merged covered duration of explicit attempt "
                                "windows; gaps excluded; parallel sums and "
                                "task elapsed stay separate"),
            "walls_measured": len(cell["walls"]),
            "shared_session": cell["shared_session"],
            "unknown_ownership": cell["unknown_ownership"],
            "duplicate_native": cell["duplicate_native"],
        })
    # Per role aggregation on deduplicated executions.
    by_role: dict = {}
    for rep in representatives:
        key = (rep.get("role") or "unknown", rep.get("model_observed") or "unknown")
        cell = by_role.setdefault(key, {"role": key[0], "model": key[1],
                                        "attempts": 0, "completed": 0, "active": 0,
                                        "walls": []})
        cell["attempts"] += 1
        if rep.get("state") == "complete":
            cell["completed"] += 1
        if rep.get("state") == "active":
            cell["active"] += 1
        wall_raw, _ = attempt_wall_time(rep)
        det = next((d for d in detailed if d["turn_id"] == rep.get("turn_id")), None)
        wall = det["reconciled_wall_time_s"] if det and _is_num(det["reconciled_wall_time_s"]) else wall_raw
        if _is_num(wall):
            cell["walls"].append(float(wall))
    roles = [{
        "role": cell["role"], "model": cell["model"], "attempts": cell["attempts"],
        "completed_attempts": cell["completed"], "active_attempts": cell["active"],
        "wall_sum_s": sum(cell["walls"]) if cell["walls"] else None,
        "walls_measured": len(cell["walls"]),
    } for cell in sorted(by_role.values(), key=lambda c: (c["role"], c["model"]))]
    # Waiting intervals only for compatible ownership: the same Router
    # request_id, else the same known non-shared session. Explicit
    # end/start with no overlap only. Parallel overlap is not waiting
    # and produces no waiting row.
    compat_groups: dict = {}
    for rep in representatives:
        if not _is_num(rep.get("started_at")) or not _is_num(rep.get("ended_at")):
            continue
        if float(rep["ended_at"]) < float(rep["started_at"]):
            continue
        turn = rep.get("turn_id")
        req = request_ids.get(turn)
        sess = rep.get("session_key")
        if rep.get("harness") == "router" and req:
            compat_groups.setdefault(("request", req), []).append(rep)
        elif sess and sess not in whole_shared and sess not in whole_conflicts:
            compat_groups.setdefault(("session", sess), []).append(rep)
        else:
            continue
    waiting = []
    for group_key in sorted(compat_groups):
        ordered = sorted(compat_groups[group_key],
                         key=lambda r: (float(r["started_at"]), float(r["ended_at"]),
                                        r.get("turn_id") or ""))
        for prev, nxt in zip(ordered, ordered[1:]):
            gap = float(nxt["started_at"]) - float(prev["ended_at"])
            if gap < 0:
                continue
            kind, scope = group_key
            waiting.append({
                "from_turn": prev["turn_id"],
                "to_turn": nxt["turn_id"],
                "gap_s": gap,
                "kind": "waiting interval",
                "scope": f"{kind} {scope}",
                "note": ("explicit compatible timestamps under the same "
                         f"{kind}; no phase inferred; parallel overlap is "
                         "not waiting and is excluded"),
            })
    waiting.sort(key=lambda w: (w["from_turn"] or "", w["to_turn"] or ""))
    raw = len(detailed)
    reconciled = len(representatives)
    dup_groups = sum(1 for g in groups if g["is_duplicate_group"])
    return {
        "attempts": detailed,
        "per_session": sessions,
        "per_role_model": roles,
        "waiting_intervals": waiting,
        "raw_attempt_count": raw,
        "reconciled_execution_count": reconciled,
        "duplicate_router_native_groups": dup_groups,
        "reconciliation_groups": groups,
        "executions": representatives,
        "reconciliation_note": ("Router/native attempt rows for one execution "
                                "merge on explicit turn/session/time evidence "
                                "into one execution identity; overlapping "
                                "native turns are a second source for that "
                                "same execution, never an extra attempt. "
                                "Counts, per-role/session aggregates and "
                                "recovery ordering use deduplicated "
                                "executions. Shared sessions and unknown "
                                "ownership stay qualified and never "
                                "auto-merge. Parallel durations never become "
                                "task elapsed time."),
    }


def failure_summary(attempts_rows: list[dict]) -> dict:
    """Failures by class and role/model with production denominators.

    Callers pass deduplicated execution representatives so a Router
    attempt and its native capture row count once. quota_blocked
    provider exhaustion counts as a failed production attempt of class
    provider and stays visible separately as quota_blocked.
    Cancellation intent is unknown without explicit evidence: bare
    Router cancelled rows are reported as cancelled with unknown
    intent, never as intentional, and stay outside production.
    """
    by_class: dict[str, int] = {}
    by_role_model: dict[tuple, dict] = {}
    failed = 0
    production_total = 0
    cancelled = 0
    cancelled_intentional_explicit = 0
    crashed = 0
    active = 0
    quota_blocked = 0
    unknown_state = 0
    missing_classification = []
    for attempt in attempts_rows:
        # Skip rows merged away: callers pass representatives, but a raw
        # list stays safe because duplicate members name the same
        # execution only through deduplicate_attempts.
        state = attempt.get("state")
        if state in ("failed", "quota_blocked"):
            production_total += 1
            failed += 1
            if state == "quota_blocked":
                quota_blocked += 1
            cls = failure_class(attempt)
            if cls is None:
                missing_classification.append(attempt.get("turn_id"))
                cls = "unknown"
            by_class[cls] = by_class.get(cls, 0) + 1
            key = (attempt.get("role") or "unknown",
                   attempt.get("model_observed") or attempt.get("route_requested") or "unknown")
            cell = by_role_model.setdefault(key, {"role": key[0], "model": key[1],
                                                  "failed": 0, "production_total": 0})
            cell["failed"] += 1
            cell["production_total"] += 1
        elif state == "complete":
            production_total += 1
            key = (attempt.get("role") or "unknown",
                   attempt.get("model_observed") or attempt.get("route_requested") or "unknown")
            cell = by_role_model.setdefault(key, {"role": key[0], "model": key[1],
                                                  "failed": 0, "production_total": 0})
            cell["production_total"] += 1
        elif state == "cancelled":
            cancelled += 1
            # No explicit intent column exists in the current ledger, so
            # a bare Router cancelled never proves intentional
            # cancellation. Explicit intentional cancellations would set
            # this counter; all current rows stay intent unknown.
            if attempt.get("cancel_intent") == "intentional":
                cancelled_intentional_explicit += 1
        elif state == "crashed":
            crashed += 1
        elif state == "active":
            active += 1
        else:
            unknown_state += 1
    rate = (failed / production_total) if production_total else None
    return {
        "failed_attempts": failed,
        "production_attempts": production_total,
        "failure_rate": rate,
        "rate_note": ("failed/production attempts in this task scope with sample "
                      "size shown; never a general rate across tasks"),
        "by_class": dict(sorted(by_class.items())),
        "by_role_model": sorted(by_role_model.values(),
                                key=lambda c: (c["role"], c["model"])),
        "cancelled": cancelled,
        "cancelled_intentional": cancelled_intentional_explicit,
        "cancelled_unknown_intent": cancelled - cancelled_intentional_explicit,
        "cancelled_intent_note": ("Router cancelled alone never proves intent; "
                                  "intent stays unknown without explicit "
                                  "evidence and stays outside production"),
        "crashed_separate": crashed,
        "active_attempts": active,
        "quota_blocked": quota_blocked,
        "quota_note": ("quota_blocked provider exhaustion counts inside "
                       "failed/production as class provider and stays "
                       "visible here separately"),
        "unknown_state": unknown_state,
        "missing_classification": sorted(t for t in missing_classification if t),
        "denominator_note": ("Production attempts are complete plus failed "
                             "plus quota_blocked provider exhaustion. "
                             "Cancellations of unknown intent, separately "
                             "counted crashes, active and unknown states "
                             "stay outside failed/total. Failure class uses "
                             "explicit terminal, stage, rc and proof_class"
                             " only; Router reason never classifies. Context"
                             " pressure is provider. Startup rc124 without"
                             " proof outcome is infrastructure; executed"
                             " proof rc124 stays timeout. A provider"
                             " exhaustion followed by a successful pool move"
                             " is an attempt outcome, not a failed accepted"
                             " task."),
    }


def job_outcomes(con: sqlite3.Connection, attempts_rows: list[dict]) -> list[dict]:
    """Separately defined job outcomes for the jobs behind this task."""
    request_ids: set[str] = set()
    for attempt in attempts_rows:
        turn = attempt.get("turn_id") or ""
        if turn.startswith("router:"):
            invocation_id = turn.split("router:", 1)[1]
            try:
                row = con.execute(
                    "SELECT request_id FROM router_invocations WHERE invocation_id=?",
                    (invocation_id,)).fetchone()
            except sqlite3.DatabaseError:
                row = None
            if row is not None and row["request_id"]:
                request_ids.add(row["request_id"])
    jobs = []
    for request_id in sorted(request_ids):
        try:
            job = con.execute(
                "SELECT request_id, status, lane, job_kind, block_reason,"
                " created_at, updated_at, cancel_requested FROM router_jobs WHERE request_id=?",
                (request_id,)).fetchone()
        except sqlite3.DatabaseError:
            job = None
        if job is None:
            jobs.append({"request_id": request_id, "missing": True})
            continue
        try:
            cancel_flag = job["cancel_requested"]
        except (KeyError, TypeError, IndexError):
            cancel_flag = None
        if cancel_flag == 1:
            cancel_intent = "intentional"
        else:
            # 0, 2 (timeout drain), NULL and legacy missing stay
            # unknown; a bare cancelled never proves intent.
            cancel_intent = "unknown"
        jobs.append({
            "request_id": job["request_id"],
            "status": job["status"],
            "lane": job["lane"],
            "job_kind": job["job_kind"],
            "block_reason": job["block_reason"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "cancel_requested": cancel_flag,
            "cancel_intent": cancel_intent,
            "note": ("job outcome is separate from attempt failure counts;"
                     " cancellation is intentional only with explicit"
                     " cancel_requested evidence"),
        })
    return jobs


def _compat_key(attempt: dict, request_id: str | None,
                whole_shared: set, whole_conflicts: set) -> tuple | None:
    """Compatible recovery identity for one execution representative."""
    tainted = (whole_shared or set()) | (whole_conflicts or set())
    if attempt.get("harness") == "router" and request_id:
        return ("request", request_id)
    sess = attempt.get("session_key")
    if sess and sess not in tainted:
        return ("session", sess)
    return None


def _known_stage(value) -> str | None:
    """Explicit attempt stage, or None when missing or unknown.

    Stage compatibility uses only the explicit attempt stage.
    Role, model, route, reason, launch reason and task order never
    infer a stage. Unknown never matches unknown.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.lower() == "unknown":
            return None
        return text
    return None


def recovery_summary(attempts_rows: list[dict], con: sqlite3.Connection | None = None,
                     whole_shared: set | None = None,
                     whole_conflicts: set | None = None) -> list[dict]:
    """Failure to next attempt start, first progress and recovery outcome.

    Only compatible execution identities pair: the same Router
    request_id resolved through router_invocations, or the same known
    non-shared session/ownership for native attempts. Attempts from
    another request never pair merely because they share a task, sort
    order or session context. Next start must be at or after failed
    end. First subsequent progress is the first compatible completed
    attempt by timestamp at any stage, labelled with its stage, and
    measured at its end, not its start. Recovery outcome, recovered
    status, active status and repeated counts are stage specific:
    the failed stage and the candidate stage must both be known and
    equal. A dispatcher or other different-stage completion never
    recovers implementation work, and unknown stage never matches.
    A new attempt starting alone is never successful recovery.
    Unresolved recovery stays active or unknown. quota_blocked
    provider exhaustion participates when compatible.
    """
    shared = set(whole_shared or set())
    conflicts = set(whole_conflicts or set())
    request_ids = _request_ids_for(con, attempts_rows)
    # Group representatives by compatible identity.
    compat_groups: dict = {}
    ungrouped: list[dict] = []
    for attempt in attempts_rows:
        key = _compat_key(attempt, request_ids.get(attempt.get("turn_id")),
                          shared, conflicts)
        if key is None:
            ungrouped.append(attempt)
        else:
            compat_groups.setdefault(key, []).append(attempt)
    for key in compat_groups:
        compat_groups[key] = sorted(
            compat_groups[key],
            key=lambda a: ((a.get("started_at") is None,
                            a.get("started_at") if _is_num(a.get("started_at")) else 0,
                            a.get("turn_id") or "")))
    out = []
    for failed in sorted(
            attempts_rows,
            key=lambda a: ((a.get("started_at") is None,
                            a.get("started_at") if _is_num(a.get("started_at")) else 0,
                            a.get("turn_id") or ""))):
        if failed.get("state") not in FAILURE_STATES:
            continue
        failed_end = failed.get("ended_at")
        failed_key = _compat_key(failed, request_ids.get(failed.get("turn_id")),
                                 shared, conflicts)
        failed_req = request_ids.get(failed.get("turn_id"))
        gap_missing: list[str] = []
        if not _is_num(failed_end):
            gap_missing.append("failed ended_at")
        if failed_key is None:
            gap_missing.append("compatible identity (request_id/session)")
        if failed_key is None:
            later: list[dict] = []
        else:
            group = compat_groups.get(failed_key, [])
            # Position of this failure inside its compatible group.
            try:
                pos = next(i for i, cand in enumerate(group)
                           if cand.get("turn_id") == failed.get("turn_id"))
            except StopIteration:
                pos = -1
            candidates = group[pos + 1:] if pos >= 0 else []
            later = []
            for cand in candidates:
                cand_start = cand.get("started_at")
                if not _is_num(cand_start) or not _is_num(failed_end):
                    later.append(cand)
                    continue
                if float(cand_start) >= float(failed_end):
                    later.append(cand)
                # Overlapping or earlier starts in the same group are
                # parallel work, not recovery ordering; skip them.
        next_row = None
        for cand in later:
            # The first later compatible attempt with usable ordering is
            # the retry candidate, even when its own timestamps are
            # partial (then the gap stays missing rather than invented).
            next_row = cand
            break
        gap = None
        if next_row is None:
            if failed_key is not None and _is_num(failed_end):
                gap_missing.append("next attempt in compatible identity")
        else:
            if not _is_num(next_row.get("started_at")):
                gap_missing.append("next started_at")
            if not gap_missing:
                diff = float(next_row["started_at"]) - float(failed_end)
                if diff < 0:
                    gap_missing.append("negative failure-to-next-start")
                else:
                    gap = diff
        failed_stage = _known_stage(failed.get("stage"))
        first_progress = None
        first_progress_end = None
        first_progress_stage: str | None = None
        time_to_progress = None
        progress_missing: list[str] = []
        for cand in later:
            if cand.get("state") == "complete":
                first_progress = cand.get("turn_id")
                first_progress_end = cand.get("ended_at")
                first_progress_stage = _known_stage(cand.get("stage"))
                if _is_num(failed_end) and _is_num(first_progress_end):
                    diff = float(first_progress_end) - float(failed_end)
                    if diff < 0:
                        progress_missing.append("negative time-to-progress")
                    else:
                        time_to_progress = diff
                else:
                    if not _is_num(first_progress_end):
                        progress_missing.append("progress ended_at")
                break
        # Stage-specific recovery chain: only candidates whose
        # explicit stage equals the failed explicit stage count.
        same_progress_turn = None
        same_progress_end = None
        time_to_same_progress = None
        if failed_stage is not None:
            for cand in later:
                if cand.get("state") == "complete" \
                        and _known_stage(cand.get("stage")) == failed_stage:
                    same_progress_turn = cand.get("turn_id")
                    same_progress_end = cand.get("ended_at")
                    if _is_num(failed_end) and _is_num(same_progress_end):
                        diff = float(same_progress_end) - float(failed_end)
                        if diff >= 0:
                            time_to_same_progress = diff
                    break
        # A failure after the first same-stage completion starts a
        # new chain; it is not repeated failed recovery for this
        # failure. Count only same-stage failures before recovery.
        chain = later
        if same_progress_turn is not None:
            for idx, cand in enumerate(later):
                if cand.get("turn_id") == same_progress_turn:
                    chain = later[:idx]
                    break
        same_failed_turns = [
            cand.get("turn_id") for cand in chain
            if cand.get("state") in FAILURE_STATES
            and failed_stage is not None
            and _known_stage(cand.get("stage")) == failed_stage
        ]
        repeated = len(same_failed_turns)
        same_active_turn = None
        if failed_stage is not None and same_progress_turn is None \
                and not same_failed_turns:
            for cand in later:
                if cand.get("state") == "active" \
                        and _known_stage(cand.get("stage")) == failed_stage:
                    same_active_turn = cand.get("turn_id")
                    break
        first_label = first_progress_stage if first_progress_stage else "unknown"
        if failed_key is None:
            outcome = ("unknown (no compatible retry identity; attempts from "
                       "another request or shared/unknown session never pair)")
        elif failed_stage is None:
            if first_progress is not None:
                outcome = (f"unknown (failed stage unknown; first subsequent progress "
                           f"{first_progress} at {first_label} observed but stage "
                           f"cannot be matched)")
            else:
                outcome = ("unknown (failed stage unknown; no stage-specific "
                           "recovery assessed)")
        elif same_progress_turn is not None:
            if first_progress == same_progress_turn:
                outcome = (f"recovered (later completed {failed_stage} "
                           f"attempt observed)")
            else:
                outcome = (f"recovered (later completed {failed_stage} attempt "
                           f"{same_progress_turn} observed; first subsequent progress "
                           f"was {first_label} at {first_progress})")
        elif same_failed_turns:
            if first_progress is not None:
                outcome = (f"repeated failed recovery (first subsequent progress at "
                           f"{first_label} {first_progress}; later {failed_stage} "
                           f"failure observed)")
            else:
                outcome = (f"repeated failed recovery (later {failed_stage} "
                           f"attempt also failed)")
        elif same_active_turn is not None:
            if first_progress is not None:
                outcome = (f"active (same-stage {failed_stage} recovery in progress, "
                           f"no completed {failed_stage} progress yet; first subsequent "
                           f"progress was {first_label} at {first_progress})")
            else:
                outcome = (f"active (same-stage {failed_stage} recovery in progress, "
                           f"no completed progress yet)")
        elif first_progress is not None:
            outcome = (f"unknown (first subsequent progress at {first_label} "
                       f"{first_progress}; no completed {failed_stage} "
                       f"progress observed)")
        elif next_row is not None:
            outcome = (f"unknown (next attempt started but no completed "
                       f"{failed_stage} progress observed)")
        else:
            outcome = "unknown (no later attempt observed)"
        entry: dict = {
            "failed_turn": failed.get("turn_id"),
            "failed_ended_at": failed_end if _is_num(failed_end) else None,
            "failed_class": failure_class(failed),
            "failed_stage": failed_stage,
            "failed_request_id": failed_req,
            "failed_session": failed.get("session_key"),
            "compat_scope": f"{failed_key[0]} {failed_key[1]}" if failed_key else None,
            "next_attempt_turn": next_row.get("turn_id") if next_row else None,
            "next_attempt_state": next_row.get("state") if next_row else None,
            "failure_to_next_start_s": gap,
            "gap_missing": sorted(set(gap_missing + progress_missing))
            if first_progress is None and time_to_progress is None and next_row is not None
            else sorted(set(gap_missing)),
            "first_progress_turn": first_progress,
            "first_progress_stage": first_progress_stage,
            "first_progress_ended_at": first_progress_end if _is_num(first_progress_end) else None,
            "time_to_first_progress_s": time_to_progress,
            "same_stage_progress_turn": same_progress_turn,
            "same_stage_progress_ended_at": same_progress_end if _is_num(same_progress_end) else None,
            "time_to_same_stage_progress_s": time_to_same_progress,
            "recovery_outcome": outcome,
            "later_failed_attempts": repeated,
            "evidence": {
                "failed_turn": failed.get("turn_id"),
                "failed_stage": failed_stage,
                "failed_request_id": failed_req,
                "next_turn": next_row.get("turn_id") if next_row else None,
                "progress_turn": first_progress,
                "progress_stage": first_progress_stage,
                "same_stage_progress_turn": same_progress_turn,
                "same_stage_failed_turns": sorted(same_failed_turns),
            },
        }
        out.append(entry)
    # Deterministic order by failed turn.
    out.sort(key=lambda r: r["failed_turn"] or "")
    return out


def usage_source_coverage(con: sqlite3.Connection, attempts_rows: list[dict],
                          estimated: dict, whole_shared: set | None = None,
                          whole_conflicts: set | None = None) -> dict:
    """Router usage source coverage with attributable native reconciliation.

    Only native responses explicitly attributable to the matching
    attempt count: the same known session plus either a reconciled
    native turn match or a response timestamp inside the explicit
    attempt window. Shared or conflicting sessions need the turn
    match; session alone never credits a resumed or shared session to
    every attempt. A null Router usage_json beside attributable
    native usage is source coverage, never zero usage and never
    complete loss. Measured but unpriced usage is distinct from
    missing usage.
    """
    tainted = set(whole_shared or set()) | set(whole_conflicts or set())
    router_total = 0
    router_with = 0
    null_with_native = 0
    null_without_native = 0
    per_attempt = []
    for attempt in attempts_rows:
        if attempt.get("harness") != "router":
            continue
        router_total += 1
        usage = attempt.get("usage_json")
        has_router = usage is not None
        if has_router:
            router_with += 1
        native_count = 0
        native_total = None
        session_key = attempt.get("session_key")
        attribution = "no attributable session"
        if session_key:
            a_start = attempt.get("started_at")
            a_end = attempt.get("ended_at")
            window_ok = (_is_num(a_start) and _is_num(a_end)
                         and float(a_end) >= float(a_start))
            reconciled_turns: set[str] = set()
            for turn in _native_turns_for(con, session_key):
                if _overlaps(a_start, a_end, turn.get("started_at"),
                             turn.get("completed_at")):
                    reconciled_turns.add(turn["turn_id"])
            try:
                rows = con.execute(
                    "SELECT turn_id, ts, total_tokens FROM responses"
                    " WHERE session_key=? AND is_overlap=0",
                    (session_key,)).fetchall()
            except sqlite3.DatabaseError:
                rows = []
            strict_turn_only = session_key in tainted
            matched = 0
            total = 0
            total_known = False
            for r in rows:
                turn_hit = r["turn_id"] in reconciled_turns if r["turn_id"] else False
                time_hit = (window_ok and _is_num(r["ts"])
                            and float(a_start) <= float(r["ts"]) <= float(a_end))
                if strict_turn_only:
                    hit = turn_hit
                else:
                    hit = bool(turn_hit or time_hit)
                if hit:
                    matched += 1
                    if r["total_tokens"] is not None:
                        total += r["total_tokens"]
                        total_known = True
            native_count = matched
            native_total = total if total_known else (None if not matched else None)
            if strict_turn_only:
                attribution = ("shared/conflicting session: turn-matched "
                               "responses only")
            elif window_ok or reconciled_turns:
                attribution = "session plus turn/time match"
            else:
                attribution = "session present but no explicit window or turn match"
        if not has_router:
            if native_count:
                null_with_native += 1
            else:
                null_without_native += 1
        per_attempt.append({
            "turn_id": attempt.get("turn_id"),
            "session_key": session_key,
            "router_usage_present": has_router,
            "native_responses": native_count,
            "native_total_tokens": native_total,
            "attribution": attribution,
            "coverage": ("router usage present" if has_router
                         else ("null router usage with reconciled native usage"
                               if native_count else "null router usage without native usage")),
        })
    priced = estimated.get("priced_responses", 0) if isinstance(estimated, dict) else 0
    unpriced = estimated.get("unpriced_responses", 0) if isinstance(estimated, dict) else 0
    return {
        "router_attempts": router_total,
        "router_with_usage": router_with,
        "router_null_usage": router_total - router_with,
        "null_with_reconciled_native": null_with_native,
        "null_without_native": null_without_native,
        "per_attempt": sorted(per_attempt, key=lambda r: r["turn_id"] or ""),
        "priced_responses": priced,
        "unpriced_responses": unpriced,
        "unpriced_reasons": (estimated.get("unpriced_reasons") or {}) if isinstance(estimated, dict) else {},
        "note": ("Null Router usage with attributable native usage is source "
                 "coverage, not zero usage or complete loss. Attribution "
                 "needs the same session plus a turn or time match; shared "
                 "sessions need the turn match. Measured but unpriced usage "
                 "is distinct from missing usage."),
    }
