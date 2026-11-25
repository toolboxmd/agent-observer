"""Read immutable purpose and native response witnesses in one ledger snapshot."""

from collections import defaultdict
import json

from .db import _has_table


def sessions(con, task_id):
    if not _has_table(con, "t3_purpose_runs"):
        return set()
    return {row[0] for row in con.execute(
        "SELECT DISTINCT s.session_key FROM t3_purpose_sessions s "
        "JOIN t3_purpose_runs r USING(run_id) WHERE r.task_id=?", (task_id,))}


def active(con, task_id):
    return _has_table(con, "t3_purpose_runs") and con.execute(
        "SELECT 1 FROM t3_purpose_runs WHERE task_id=? LIMIT 1", (task_id,)
    ).fetchone() is not None


def evidence(con, task_id, scope):
    """A response belongs to one execution only if both native joins agree."""
    from . import native_response

    runs = [dict(r) for r in con.execute(
        "SELECT * FROM t3_purpose_runs WHERE task_id=? ORDER BY run_id", (task_id,))]
    deliveries = [dict(r) for r in con.execute(
        "SELECT * FROM t3_purpose_deliveries WHERE task_id=? ORDER BY declared_at, url, thread_id", (task_id,))]
    reference_conflict = len({r["kind"] for r in runs}) > 1
    session_runs = {}
    members = {}
    ownership = {}
    turn_keys = set()
    conflicts = set()
    for key in sorted(scope):
        by_run = dict(con.execute(
            "SELECT r.run_id, r.task_id FROM t3_purpose_sessions s "
            "JOIN t3_purpose_runs r USING(run_id) WHERE s.session_key=?", (key,)))
        if not by_run:
            continue
        session_runs[key] = sorted(by_run)
        by_input = defaultdict(set)
        for run, native_id, turn, segment, validation in con.execute(
                "SELECT run_id,native_id,provider_turn_id,segment_id,validation FROM t3_purpose_inputs "
                "WHERE session_key=?", (key,)):
            by_input[native_id].add((run, turn, segment, validation))
        whole = {r["task_id"] for r in con.execute(
            "SELECT task_id, evidence FROM session_assignments WHERE session_key=?", (key,))
            if not (r["evidence"] or "").startswith("t3:")}
        claimed_inputs = defaultdict(set)
        for msg, alias, turn, owner in con.execute(
                "SELECT s.native_id,s.alias_id,s.turn_id,a.task_id FROM assignments a "
                "JOIN user_messages s ON a.user_message_id=s.native_id WHERE s.session_key=?", (key,)):
            for identity in (msg, alias, turn):
                if identity:
                    claimed_inputs[identity].add(owner)
        harness = key.partition(":")[0]
        for rid, input_ids in native_response.inputs(con, key).items():
            candidates = {run for identity in input_ids for run in by_input.get(identity, ())}
            if not candidates:
                continue
            if len(input_ids) != 1 or len(candidates) != 1:
                conflicts.add(rid)
                continue
            run, turn, segment, validation = next(iter(candidates))
            owner = by_run.get(run)
            native_id = next(iter(input_ids))
            claims = whole | claimed_inputs.get(f"{harness}:{native_id}", set())
            if (reference_conflict and owner == task_id) or (claims and claims != {owner}):
                conflicts.add(rid)
                continue
            ownership[rid] = owner
            if owner == task_id:
                members[rid] = {"run_id": run, "session_key": key, "native_input_id": native_id,
                                "provider_turn_id": turn, "segment_id": segment, "validation": validation}
                if harness == "codex":
                    turn_keys.add((key, "codex:" + native_id))
                elif harness == "grok":
                    turn_keys.add((key, rid))

    return ({"version": 1, "task_id": task_id, "runs": runs, "deliveries": deliveries,
             "membership": members, "sessions": session_runs, "conflicting_responses": sorted(conflicts),
             "conflicting_reference_kinds": reference_conflict},
            ownership, turn_keys)


def _normalized_counters(row):
    """Render the native response in Chromeria's inclusive comparison units."""
    from .adapters import claude, codex, grok, opencode

    sem = row["semantics"]
    if sem not in {codex.SEMANTICS, claude.SEMANTICS, opencode.SEMANTICS, grok.SEMANTICS}:
        return {}
    inp = [row["input_tokens"]]
    out = [row["output_tokens"]]
    if sem in {claude.SEMANTICS, opencode.SEMANTICS}:
        inp.extend([row["cached_input_tokens"], row["cache_write_input_tokens"]])
    if sem == opencode.SEMANTICS:
        out.append(row["reasoning_output_tokens"])

    def count(values):
        return sum(values) if all(type(n) is int and n >= 0 for n in values) else None

    return {"inputTokens": count(inp), "outputTokens": count(out),
            "cachedInputTokens": row["cached_input_tokens"],
            "cacheCreationTokens": row["cache_write_input_tokens"],
            "reasoningTokens": row["reasoning_output_tokens"]}


def _turn_check(turn, responses, finalized):
    usage = json.loads(turn["usage_json"]) if turn["usage_json"] else {}
    if turn["status"] != "completed" or usage.get("usageStatus") != "complete":
        return "native_final_usage_missing"
    if not responses:
        return "no_joined_responses"
    if any(r["harness"] == "claude" and r["response_id"] not in finalized for r in responses):
        return "response_not_final"
    normalized = [_normalized_counters(r) for r in responses]
    for name in ("inputTokens", "outputTokens", "cachedInputTokens", "cacheCreationTokens", "reasoningTokens"):
        if name not in usage:
            continue
        counters = [c.get(name) for c in normalized]
        if any(type(n) is not int or n < 0 for n in counters):
            return "counter_unavailable"
        if sum(counters) != usage[name]:
            return "counter_mismatch"
    return "reconciled"


def check_inventory(con, native, rows):
    """Compare independent provider final counters and the expected attempt/turn edges.

    The v1 capability records observed background causes, or no inventory.
    Neither can certify that the entire outcome has been collected.
    """
    by_turn = defaultdict(list)
    by_segment = defaultdict(list)
    for row in rows:
        member = native["membership"].get(row["response_id"])
        if member:
            member["counters"] = {k: row[k] for k in (
                "harness", "model", "semantics", "input_tokens", "cached_input_tokens",
                "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens",
                "total_tokens", "cost_usd", "cache_write_5m_tokens", "cache_write_1h_tokens")}
            if member["segment_id"]:
                by_segment[(member["run_id"], member["segment_id"], member["session_key"])].append(row)
            if member["provider_turn_id"]:
                by_turn[member["provider_turn_id"]].append(row)
    finalized = set()
    if _has_table(con, "claude_response_finality"):
        for key in native["sessions"]:
            if key.startswith("claude:"):
                finalized.update(r[0] for r in con.execute(
                    "SELECT f.response_id FROM responses r JOIN claude_response_finality f "
                    "USING(response_id) WHERE r.session_key=?", (key,)))
    gaps = set()
    main_gaps = set()
    checks = []
    for run in native["runs"]:
        run_id = run["run_id"]
        if run["status"] != "completed":
            main_gaps.add("run_not_completed")
        attempts = [dict(r) for r in con.execute(
            "SELECT a.*,c.native_inputs,c.child_segments,c.background_inventory,c.native_usage_finality "
            "FROM t3_purpose_attempts a LEFT JOIN t3_purpose_capabilities c USING(provider_session_id) "
            "WHERE a.run_id=? ORDER BY a.attempt_id", (run_id,))]
        if run["active_attempt_id"] not in {a["attempt_id"] for a in attempts}:
            main_gaps.add("expected_attempt_missing")
        for a in attempts:
            turns = [dict(r) for r in con.execute(
                "SELECT * FROM t3_purpose_turns WHERE attempt_id=? ORDER BY provider_turn_id", (a["attempt_id"],))]
            if a["provider_turn_id"] not in {t["provider_turn_id"] for t in turns}:
                main_gaps.add("expected_provider_turn_missing")
            for turn in turns:
                state = _turn_check(turn, by_turn[turn["provider_turn_id"]], finalized)
                checks.append({"run_id": run_id, "attempt_id": a["attempt_id"],
                               "provider_turn_id": turn["provider_turn_id"], "status": state,
                               "provider_usage": json.loads(turn["usage_json"] or "null")})
                if state != "reconciled":
                    main_gaps.add(state)
            # Even a valid final main-agent counter omits independent background work.
            gaps.add("observed_background_causes_only" if a["background_inventory"] == "observed_causes"
                     else "background_inventory_unavailable")
        run["attempts"] = attempts
        run["participants"] = [dict(r) for r in con.execute(
            "SELECT * FROM t3_purpose_participants WHERE run_id=? ORDER BY kind,participant_id", (run_id,))]
        expected = {r["participant_id"] for r in run["participants"] if r["kind"] == "subagent_item"}
        observed = {r["participant_id"] for r in run["participants"]
                    if r["kind"] not in {"subagent_item", "pending_background", "completion_member", "completion_cause"}}
        run["missing_children"] = sorted(expected - observed)
        if run["missing_children"]:
            gaps.add("expected_child_missing")
        if any(p["environment_id"] for p in run["participants"]):
            gaps.add("remote_participation")
        if run["participants"]:
            gaps.add("child_inventory_requires_reconciliation")
    native["segments"] = [dict(r) for r in con.execute(
        "SELECT * FROM t3_purpose_segments WHERE task_id=? ORDER BY thread_id,owner_id,segment_id",
        (native["task_id"],))]
    for segment in native["segments"]:
        segment["provider_turns"] = [dict(r) for r in con.execute(
            "SELECT * FROM t3_purpose_child_turns WHERE run_id=? AND segment_id=? ORDER BY provider_turn_id",
            (segment["run_id"], segment["segment_id"]))]
        turns = segment["provider_turns"]
        for turn in turns:
            group = by_segment[(turn["run_id"], turn["segment_id"], turn["session_key"])]
            turn["counter_check"] = (_turn_check(turn, group, finalized) if len(turns) == 1
                                     else "segment_turn_boundary_unresolved")
        gaps.add("segment_final_usage_unreconciled")
    if native["conflicting_reference_kinds"]:
        gaps.add("conflicting_reference_kinds")
    if native["conflicting_responses"]:
        gaps.add("conflicting_response_ownership")
    native["turn_checks"] = checks
    native["main_agent_counters_reconciled"] = bool(checks) and not main_gaps
    native["coverage_gaps"] = sorted(gaps | main_gaps)
    return bool(native["coverage_gaps"])
