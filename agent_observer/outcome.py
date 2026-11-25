"""Read immutable purpose and native response witnesses in one ledger snapshot."""

from collections import defaultdict
from dataclasses import dataclass, field
import json

from . import native_response
from .db import _has_table


def sessions(con, task_id):
    if not _has_table(con, "t3_purpose_runs"):
        return set()
    keys = {row[0] for row in con.execute(
        "SELECT DISTINCT s.session_key FROM t3_purpose_sessions s "
        "JOIN t3_purpose_runs r USING(run_id) WHERE r.task_id=?", (task_id,))}
    if _has_table(con, "t3_source_runs"):
        keys.update(row[0] for row in con.execute(
            "SELECT DISTINCT s.session_key FROM t3_source_runs r JOIN t3_purpose_sessions s USING(run_id) "
            "WHERE r.task_id=?", (task_id,)))
    return keys


def active(con, task_id):
    return _has_table(con, "t3_purpose_runs") and con.execute(
        "SELECT 1 FROM t3_purpose_runs WHERE task_id=? LIMIT 1", (task_id,)
    ).fetchone() is not None


def legacy_link(evidence):
    return evidence == "t3" or (evidence or "").startswith("t3:")


@dataclass
class Ownership:
    """Task ownership within the inspected sessions, with canonical witnesses kept separately."""
    native: dict | None = None
    # Explicit and native claims resolved against the same literal response.
    owners: dict = field(default_factory=dict)
    turns: set = field(default_factory=set)
    response_turns: dict = field(default_factory=dict)
    literal_sessions: set = field(default_factory=set)
    conflicts: set = field(default_factory=set)
    whole: set = field(default_factory=set)

    def event_turns(self, con, key):
        """Literal event partitions. None excludes, an empty set admits session scope only."""
        if key not in self.literal_sessions:
            return
        for event, responses in native_response.event_responses(con, key):
            turns = None
            if len(responses) <= 1:
                rid = next(iter(responses), None)
                turns = self.response_turns.get((key, rid))
                if key in self.whole and rid not in self.owners and rid not in self.conflicts:
                    # Whole-session capture includes events without a usage row.
                    turns = set()
            yield event, turns


def evidence(con, task_id, scope):
    """Resolve report and event scope from the same native and explicit claims."""
    result = Ownership()
    result.whole.update(key for key, ev in con.execute(
        "SELECT session_key,evidence FROM session_assignments WHERE task_id=?", (task_id,))
        if not legacy_link(ev))
    result.turns.update((key, turn) for key, turn in con.execute(
        "SELECT s.session_key,s.turn_id FROM assignments a JOIN user_messages s "
        "ON a.user_message_id=s.native_id WHERE a.task_id=? AND s.is_genuine=1 "
        "AND s.session_key IS NOT NULL AND s.turn_id IS NOT NULL",
        (task_id,)))
    if not _has_table(con, "t3_purpose_runs"):
        for key in scope:
            if key.startswith(("claude:", "opencode:")):
                _session_ownership(con, key, task_id, {}, False, result, {})
        return result
    runs = [dict(r) for r in con.execute(
        "SELECT * FROM t3_purpose_runs WHERE task_id=? ORDER BY run_id", (task_id,))]
    deliveries = [dict(r) for r in con.execute(
        "SELECT * FROM t3_purpose_deliveries WHERE task_id=? ORDER BY declared_at, url, thread_id", (task_id,))]
    disputed_runs = []
    if _has_table(con, "t3_source_runs"):
        disputed_runs = [r[0] for r in con.execute(
            "SELECT DISTINCT s.run_id FROM t3_source_runs s LEFT JOIN t3_purpose_runs r USING(run_id) "
            "WHERE s.task_id=? AND r.run_id IS NULL ORDER BY s.run_id", (task_id,))]
    reference_conflict = len({r["kind"] for r in runs}) > 1
    session_runs = {}
    members = {}
    for key in sorted(scope):
        by_run = dict(con.execute(
            "SELECT r.run_id, r.task_id FROM t3_purpose_sessions s "
            "JOIN t3_purpose_runs r USING(run_id) WHERE s.session_key=?", (key,)))
        if _has_table(con, "t3_source_runs"):
            for run, in con.execute(
                    "SELECT DISTINCT s.run_id FROM t3_purpose_sessions s JOIN t3_source_runs x USING(run_id) "
                    "LEFT JOIN t3_purpose_runs r USING(run_id) WHERE s.session_key=? AND r.run_id IS NULL", (key,)):
                by_run[run] = None
        if by_run:
            session_runs[key] = sorted(by_run)
        elif not key.startswith(("claude:", "opencode:")):
            continue
        _session_ownership(con, key, task_id, by_run, reference_conflict, result, members)

    if runs or deliveries or session_runs or disputed_runs:
        result.native = {"version": 1, "task_id": task_id, "runs": runs, "deliveries": deliveries,
                         "membership": members, "sessions": session_runs, "conflicting_responses": sorted(result.conflicts),
                         "conflicting_reference_kinds": reference_conflict, "conflicting_source_runs": disputed_runs}
    return result


def _session_ownership(con, key, task_id, by_run, reference_conflict, result, members):
    by_input = defaultdict(set)
    if by_run:
        for run, native_id, turn, segment, validation in con.execute(
                "SELECT run_id,native_id,provider_turn_id,segment_id,validation FROM t3_purpose_inputs "
                "WHERE session_key=?", (key,)):
            by_input[native_id].add((run, turn, segment, validation))
        if _has_table(con, "t3_source_inputs"):
            for native_id, in con.execute(
                    "SELECT DISTINCT s.native_id FROM t3_source_inputs s WHERE s.session_key=? "
                    "AND NOT EXISTS (SELECT 1 FROM t3_purpose_inputs i "
                    "WHERE i.session_key=s.session_key AND i.native_id=s.native_id)", (key,)):
                # A rejected identity still contradicts explicit or whole-session
                # credit. Carry it through the same report and event filters.
                by_input[native_id].add((None, None, None, "conflicting_snapshot"))
    whole = {r["task_id"] for r in con.execute(
        "SELECT task_id, evidence FROM session_assignments WHERE session_key=?", (key,))
        if not legacy_link(r["evidence"])}
    harness = key.partition(":")[0]
    claimed_turns = defaultdict(set)
    claimed_inputs = defaultdict(set)
    input_turns = defaultdict(set)
    for msg, alias, turn, owner in con.execute(
            "SELECT s.native_id,s.alias_id,s.turn_id,a.task_id FROM user_messages s "
            "LEFT JOIN assignments a ON a.user_message_id=s.native_id WHERE s.session_key=? AND s.is_genuine=1",
            (key,)):
        if owner and turn:
            claimed_turns[turn].add(owner)
        # Claude and OpenCode identify inputs by message, Codex by turn.
        for identity in ((msg, alias, turn) if harness == "codex" else (msg, alias)):
            if identity:
                if owner:
                    claimed_inputs[identity].add(owner)
                if turn:
                    input_turns[identity].add(turn)
    literal_messages = harness in {"claude", "opencode"}
    if not by_run and not whole and not claimed_inputs:
        return
    if literal_messages:
        result.literal_sessions.add(key)
    call_witnesses = set()
    disputed_completions = set()
    if harness == "claude":
        from .adapters import claude
        responses, disputed_completions = claude.response_witnesses(con, key)
    elif harness == "grok":
        from .adapters import grok
        responses, call_witnesses, disputed_completions = grok.response_witnesses(con, key)
    else:
        responses = native_response.inputs(con, key)
    # In a contested whole session, only events on resolved turns can be
    # shown. Untagged events cannot be separated between the claimants.
    exclusive = not by_run or (whole == {task_id} and set(by_run.values()) == {task_id})
    prefix = key if harness == "grok" else harness
    blocked_turns = set()
    for native_id, candidates in by_input.items():
        identity = f"{prefix}:{native_id}"
        input_owners = {by_run.get(entry[0]) for entry in candidates}
        claims = whole | claimed_inputs[identity]
        if (len(candidates) > 1 or input_owners != {task_id}
                or claims - {task_id} or reference_conflict):
            blocked_turns.update(input_turns[identity])
            if harness == "codex":
                blocked_turns.add(identity)
            exclusive = False
    for rid, display_turn in con.execute(
            "SELECT response_id,turn_id FROM responses WHERE session_key=? AND is_overlap=0", (key,)):
        input_ids = responses.get(rid, set())
        if rid in disputed_completions or len(input_ids) > 1:
            result.conflicts.add(rid)
            blocked_turns.update((display_turn, rid))
            for identity in input_ids:
                qualified = f"{prefix}:{identity}"
                blocked_turns.update(input_turns[qualified])
                if harness == "codex":
                    blocked_turns.add(qualified)
            exclusive = False
            continue
        identities = {f"{prefix}:{identity}" for identity in input_ids}
        candidates = {candidate for identity in input_ids for candidate in by_input.get(identity, ())}
        claims = whole.copy()
        for identity in identities:
            claims.update(claimed_inputs[identity])
        if not identities and not literal_messages:
            claims.update(claimed_turns[display_turn])
        if not candidates:
            result.owners[rid] = claims
            if task_id in claims:
                turns = {t for identity in identities for t in input_turns[identity]}
                if literal_messages:
                    turns.update(identities)
                elif not turns and display_turn:
                    turns.add(display_turn)
                result.turns.update((key, t) for t in turns)
                if literal_messages:
                    result.response_turns[key, rid] = turns
            continue

        run, turn, segment, validation = next(iter(candidates))
        owner = by_run.get(run)
        disputed = (validation == "conflicting_snapshot" or len(input_ids) != 1 or len(candidates) != 1
                    or (reference_conflict and owner == task_id)
                    or (claims and claims != {owner}))
        if disputed or owner != task_id:
            for identity in identities:
                blocked_turns.update(input_turns[identity])
            if harness == "codex":
                blocked_turns.update(identities)
            elif harness == "grok":
                blocked_turns.add(rid)
            elif harness == "claude":
                # Even a tool from this response can carry another prompt's
                # display turn. Keep only literal events on that mixed turn.
                blocked_turns.add(display_turn)
        if disputed:
            result.conflicts.add(rid)
            exclusive = False
            continue
        result.owners[rid] = {owner} if owner else set()
        if owner == task_id:
            native_id, = input_ids
            if literal_messages:
                result.response_turns[key, rid] = identities
            if rid in call_witnesses:
                validation = "grok_tool_call"
            members[rid] = {"run_id": run, "session_key": key, "native_input_id": native_id,
                            "provider_turn_id": turn, "segment_id": segment, "validation": validation}
            if harness == "codex":
                result.turns.add((key, "codex:" + native_id))
            elif harness == "grok":
                result.turns.add((key, rid))
    if not exclusive:
        result.whole.discard(key)
    result.turns.difference_update((key, turn) for turn in blocked_turns)


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
    if native["conflicting_source_runs"]:
        main_gaps.add("conflicting_native_run_records")
    if not native["runs"]:
        main_gaps.add("purpose_run_unavailable")
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
            if not turns or (a["provider_turn_id"] is not None and
                             a["provider_turn_id"] not in {t["provider_turn_id"] for t in turns}):
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
