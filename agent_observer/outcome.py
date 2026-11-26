"""Read immutable purpose and native response witnesses in one ledger snapshot."""

from collections import defaultdict
from dataclasses import dataclass, field
import json
from typing import NamedTuple

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


def purpose_evidence(native):
    """Whether Chromeria purpose evidence decides this task: runs, deliveries or rejects."""
    return bool(native and (native["runs"] or native["deliveries"] or native["conflicting_source_runs"]
                            or native.get("rejected_rows") or native.get("deleted_runs")
                            or native.get("invalid_runs")))


def legacy_link(evidence):
    return evidence == "t3" or (evidence or "").startswith("t3:")


@dataclass
class Ownership:
    """Task ownership within the inspected sessions, with canonical witnesses kept separately."""
    native: dict | None = None
    # Explicit and native claims resolved against the same literal response.
    owners: dict = field(default_factory=dict)
    turns: set = field(default_factory=set)
    missing_by_response: dict = field(default_factory=dict)
    legacy_event_turns: set = field(default_factory=set)
    response_turns: dict = field(default_factory=dict)
    literal_sessions: set = field(default_factory=set)
    conflicts: set = field(default_factory=set)
    unavailable_usage: set = field(default_factory=set)
    unlocated_usage_sessions: set = field(default_factory=set)
    whole: set = field(default_factory=set)

    def event_turns(self, con, key):
        """Literal event partitions. None excludes, an empty set admits session scope only."""
        if key not in self.literal_sessions:
            return
        for event, display_turn, responses in native_response.event_responses(con, key):
            turns = None
            if responses is not None and len(responses) <= 1:
                rid = next(iter(responses), None)
                turns = self.response_turns.get((key, rid))
                if not responses and (key, display_turn) in self.legacy_event_turns:
                    turns = {display_turn}
                if turns is None and key in self.whole and rid not in self.owners and rid not in self.conflicts:
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

    # The run record classes every run this ledger has seen; deleted, invalid
    # and conflicting runs, and rejected rows, reach the task through it. It
    # is read here, before any gate, so a task with no valid run still sees it.
    record = RunRecord.read(con)
    deleted, invalid, changed = _recorded_runs(con, task_id, record)
    disputed_runs = sorted(set(disputed_runs) | changed)
    origins = record.origins(task_id)
    if _has_table(con, "t3_source_runs"):
        origins |= {r[0] for r in con.execute(
            "SELECT DISTINCT source_path FROM t3_source_runs WHERE task_id=?", (task_id,))}
    rejected = _rejected(con, {"task_id": task_id}, record, origins)
    if runs or deliveries or session_runs or disputed_runs or rejected or deleted or invalid:
        result.native = {"version": 1, "task_id": task_id, "runs": runs, "deliveries": deliveries,
                         "rejected_rows": rejected, "deleted_runs": deleted, "invalid_runs": invalid,
                         "membership": members, "sessions": session_runs,
                         "unavailable_response_usage": sorted(result.unavailable_usage),
                         "unlocated_usage_sessions": sorted(result.unlocated_usage_sessions),
                         "conflicting_responses": sorted(result.conflicts),
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
    prompt_ids = defaultdict(set)
    claimed_inputs = defaultdict(set)
    input_turns = defaultdict(set)
    for msg, alias, turn, owner in con.execute(
            "SELECT s.native_id,s.alias_id,s.turn_id,a.task_id FROM user_messages s "
            "LEFT JOIN assignments a ON a.user_message_id=s.native_id WHERE s.session_key=? AND s.is_genuine=1",
            (key,)):
        if turn:
            prompt_ids[turn].add(msg)
            if owner:
                claimed_turns[turn].add(owner)
        # Claude and OpenCode identify inputs by message, Codex by turn.
        for identity in ((msg, alias, turn) if harness == "codex" else (msg, alias)):
            if identity:
                prompt_ids[identity].add(msg)
                if owner:
                    claimed_inputs[identity].add(owner)
                if turn:
                    input_turns[identity].add(turn)
    literal_messages = harness in {"claude", "opencode"}
    if not by_run and not whole and not claimed_inputs:
        return
    listed, listed_turns = _listed_responses(con, key, by_run) if harness == "claude" else ({}, set())
    if literal_messages:
        result.literal_sessions.add(key)
    call_witnesses = set()
    disputed_completions = set()
    unavailable = {}
    if harness == "claude":
        from .adapters import claude
        responses, disputed_completions = claude.response_witnesses(con, key)
        unavailable = {rid: responses.get(rid, set()) for rid in claude.responses_without_final_usage(con, key)}
        disputed_completions |= claude.conflicting_final_measurements(con, key)
        result.conflicts.update(disputed_completions)
        if _has_table(con, "claude_unreadable_sources") and con.execute(
                "SELECT 1 FROM claude_unreadable_sources WHERE session_key=? LIMIT 1", (key,)).fetchone():
            result.unlocated_usage_sessions.add(key)
    elif harness == "opencode":
        from .adapters import opencode
        responses, disputed_completions, unavailable = opencode.response_witnesses(con, key)
        if opencode.has_unlocated_usage_error(con, key):
            result.unlocated_usage_sessions.add(key)
    elif harness == "grok":
        from .adapters import grok
        responses, call_witnesses, disputed_completions = grok.response_witnesses(con, key)
    else:
        responses = native_response.inputs(con, key)
        if harness == "codex":
            from .adapters import codex, codex_atomic
            conflicts, incomplete, unavailable = codex_atomic.disputes(con, key, codex.SEMANTICS)
            disputed_completions.update(conflicts)
            result.conflicts.update(conflicts)
            for rid, turns in conflicts.items():
                responses.setdefault(rid, set()).update(turns)
            if incomplete:
                result.unlocated_usage_sessions.add(key)
    # In a contested whole session, only events on resolved turns can be
    # shown. Untagged events cannot be separated between the claimants.
    exclusive = not by_run or (whole == {task_id} and set(by_run.values()) == {task_id})
    prefix = key if harness == "grok" else harness
    blocked_turns = set()
    unsafe_display = set()
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
    measured_turns = dict(con.execute(
        "SELECT response_id,turn_id FROM responses WHERE session_key=? AND is_overlap=0", (key,)))
    for rid in measured_turns.keys() | unavailable.keys():
        display_turn = measured_turns.get(rid)
        input_ids = responses.get(rid, unavailable.get(rid, set()))
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
        if identities and display_turn not in identities and not any(
                display_turn in input_turns[identity] for identity in identities):
            unsafe_display.add(display_turn)
        candidates = {candidate for identity in input_ids for candidate in by_input.get(identity, ())}
        if listed_turns and (rid in listed or any(c[1] in listed_turns for c in candidates)):
            # The turn's own list names its members. Ancestry may agree, reach
            # an unwitnessed injected input, or reach no input at all; it must
            # never place the response on a different witnessed turn.
            entries = listed.get(rid, set())
            entry = next(iter(entries)) if len(entries) == 1 else None
            owner = by_run.get(entry[0]) if entry else None
            claims = whole | {o for identity in identities for o in claimed_inputs[identity]}
            if (entry is None or any(c[3] == "conflicting_snapshot" or c[1] != entry[1] for c in candidates)
                    or (claims and claims != {owner}) or (reference_conflict and owner == task_id)):
                result.conflicts.add(rid)
                blocked_turns.add(display_turn)
                exclusive = False
                continue
            result.owners[rid] = {owner} if owner else set()
            if owner != task_id:
                blocked_turns.add(display_turn)
                continue
            result.response_turns[key, rid] = identities
            if rid in measured_turns:
                members[rid] = {"run_id": entry[0], "session_key": key,
                                "native_input_id": next(iter(input_ids)) if len(input_ids) == 1 else "",
                                "provider_turn_id": entry[1], "segment_id": None,
                                "validation": "claude_native_response_id"}
            continue
        unresolved_ancestry = harness == "claude" and rid in responses and not input_ids
        claims = set() if unresolved_ancestry else whole.copy()
        if unresolved_ancestry:
            exclusive = False
        for identity in identities:
            claims.update(claimed_inputs[identity])
        # Only an explicit capture may use the historical display turn.
        # A session with native run evidence must resolve its literal input.
        legacy_capture = not by_run and rid not in responses
        if not identities and (not literal_messages or legacy_capture):
            claims.update(claimed_turns[display_turn])
        if not candidates:
            result.owners[rid] = claims
            if not claims:
                missing = {msg for identity in identities for msg in prompt_ids[identity]}
                if legacy_capture:
                    missing.update(prompt_ids[display_turn])
                if missing:
                    result.missing_by_response[rid] = missing
            if task_id not in claims:
                unsafe_display.add(display_turn)
            if task_id in claims:
                turns = {t for identity in identities for t in input_turns[identity]}
                if literal_messages:
                    turns.update(identities)
                if not turns and display_turn and (not literal_messages or legacy_capture):
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
            if rid not in measured_turns:
                continue
            members[rid] = {"run_id": run, "session_key": key, "native_input_id": native_id,
                            "provider_turn_id": turn, "segment_id": segment, "validation": validation}
            if harness == "codex":
                result.turns.add((key, "codex:" + native_id))
            elif harness == "grok":
                result.turns.add((key, rid))
    result.unavailable_usage.update(rid for rid in unavailable
                                    if not result.owners.get(rid) or task_id in result.owners[rid])
    if literal_messages and not by_run:
        result.legacy_event_turns.update((key, turn) for turn, owners in claimed_turns.items()
                                        if task_id in owners and turn not in blocked_turns and turn not in unsafe_display)
    if not exclusive:
        result.whole.discard(key)
    result.turns.difference_update((key, turn) for turn in blocked_turns)


def _columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _listed_responses(con, key, by_run):
    """Claude responses each turn of this session lists, by response id."""
    listed, turns = defaultdict(set), set()
    if not by_run or "native_response_ids_json" not in _columns(con, "t3_purpose_turns"):
        return listed, turns
    for run, turn, ids in con.execute(
            "SELECT run_id,provider_turn_id,native_response_ids_json FROM t3_purpose_turns "
            "WHERE session_key=? AND native_response_ids_json IS NOT NULL", (key,)):
        if run in by_run:
            turns.add(turn)
            for native_id in json.loads(ids):
                listed["claude:" + native_id].add((run, turn))
    return listed, turns


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


TERMINAL = frozenset({"completed", "failed", "cancelled", "superseded", "interrupted", "rolled_back"})
CHILD_SEGMENTS = frozenset({"start", "resume", "workflow_member"})


def _closure(turn):
    """The mirrored usage closure, or None when the producer made no statement."""
    closure = json.loads(turn.get("usage_closure_json") or "null")
    return closure if isinstance(closure, dict) else None


def _turn_check(turn, responses, finalized, listed=None, compactions=None):
    """Reconcile one provider turn's final counter with its joined responses.

    ``listed`` is the response ids the turn names. ``compactions`` is, for a
    closed turn, its stated uncounted compactions and what the native records
    show; compaction responses stay in the cost but not in the counter.
    """
    usage = json.loads(turn["usage_json"]) if turn["usage_json"] else {}
    if turn["status"] != "completed" or usage.get("usageStatus") != "complete":
        return "native_final_usage_missing"
    joined = {r["response_id"] for r in responses}
    if listed is not None and not listed <= joined:
        return "listed_response_missing"
    if not responses:
        return "no_joined_responses"
    if any(r["harness"] == "claude" and r["response_id"] not in finalized for r in responses):
        return "response_not_final"
    if compactions is not None:
        expected, found = compactions
        if found["unattributed"]:
            return "compaction_unattributed"
        if found["missing"] or not found["identified"] <= joined:
            return "compaction_response_missing"
        if found["count"] != expected:
            return "compaction_count_mismatch"
        responses = [r for r in responses if r["response_id"] not in found["identified"]]
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


def _compactions(con, turn, closure):
    """The compactions a Codex rollout records for one provider turn.

    One native compaction writes a `compacted` record, whose event detail
    names its response and turn, and a ContextCompaction marker tagged with
    the same turn. A compaction no turn can own leaves every closed turn of
    its session unresolved.
    """
    found = {"identified": set(), "count": 0, "missing": False, "unattributed": False}
    key = turn.get("session_key") or ""
    if not key.startswith("codex:"):
        found["unattributed"] = closure.get("witness") == "event_stream"
        return found
    native_turns = {r[0] for r in con.execute(
        "SELECT native_id FROM t3_purpose_inputs WHERE provider_turn_id=? AND session_key=?",
        (turn["provider_turn_id"], key))}
    if not native_turns:
        found["unattributed"] = True
        return found
    records, markers = [], 0
    for detail, event_turn in con.execute(
            "SELECT detail_json,turn_id FROM events WHERE session_key=? AND family='compaction'", (key,)):
        detail = json.loads(detail) if detail else {}
        if detail.get("turn_id"):
            if detail["turn_id"] in native_turns:
                records.append(detail.get("compaction_response_id"))
        elif event_turn:
            markers += event_turn.partition(":")[2] in native_turns
        else:
            found["unattributed"] = True
    found["identified"] = {"codex:" + rid for rid in records if rid}
    found["count"] = max(len(records), markers)
    found["missing"] = None in records or markers > len(records)
    return found


class _Gaps:
    """Each reason the total stays unknown, with the distinct items it affects."""

    def __init__(self):
        self.items = {}

    def add(self, name, unit, item):
        self.items.setdefault(name, (unit, set()))[1].add(item)

    def counts(self):
        return {name: {"count": len(items), "unit": unit} for name, (unit, items) in sorted(self.items.items())}


def _child_runs(con, participant, task_id):
    return participant["child_thread_id"] and con.execute(
        "SELECT 1 FROM t3_purpose_runs WHERE thread_id=? AND task_id=? LIMIT 1",
        (participant["child_thread_id"], task_id)).fetchone() is not None


def _children(con, task_id, run, gaps, not_counted):
    """Apply the delegated-child rule to one run's participants."""
    from .adapters import t3_purpose
    expected = {r["participant_id"] for r in run["participants"] if r["kind"] == "subagent_item"}
    observed = {r["participant_id"] for r in run["participants"]
                if r["kind"] not in {"subagent_item", "pending_background", "completion_member", "completion_cause"}}
    run["missing_children"] = sorted(expected - observed)
    for child in run["missing_children"]:
        gaps.add("expected_child_missing", "children", child)
    for p in run["participants"]:
        child = (run["run_id"], p["participant_id"])
        if p["environment_id"]:
            gaps.add("remote_participation", "children", child)
        elif p["kind"] == "pending_background":
            gaps.add("child_active", "children", child)
        elif p["kind"] == "provider_native":
            gaps.add("native_child_agent", "children", child)
        if p["kind"] != "app_owned" or p["environment_id"]:
            continue
        declared = json.loads(p["declaration_json"] or "null") or {}
        reference = declared.get("reference")
        if declared.get("scope") == "single" and reference:
            if t3_purpose.task_id(con, reference) != task_id:
                not_counted["declared_elsewhere"] += 1
            elif p["status"] not in TERMINAL:
                gaps.add("child_active", "children", child)
            elif not _child_runs(con, p, task_id):
                gaps.add("child_runs_missing", "children", child)
        elif declared.get("scope") == "unscoped" and declared.get("reason") == "deliberate":
            not_counted["deliberately_unscoped"] += 1
        elif declared.get("scope") == "unscoped" and declared.get("reason") == "legacy_no_intent":
            gaps.add("child_purpose_undeclared", "children", child)
        else:
            gaps.add("child_purpose_unknown", "children", child)


class RunRecord(NamedTuple):
    """The run record of every source, read once: (class, task, thread) per run."""
    runs: dict
    threads: dict

    @classmethod
    def read(cls, con):
        runs, threads = defaultdict(dict), defaultdict(lambda: defaultdict(list))
        if _has_table(con, "t3_seen_runs") and "class" in {
                r[1] for r in con.execute("PRAGMA table_info(t3_seen_runs)")}:
            for path, run, thread, kind, task in con.execute(
                    "SELECT source_path,run_id,thread_id,class,task_id FROM t3_seen_runs"):
                runs[path][run] = (kind, task, thread)
                threads[path][thread].append((kind, task))
        return cls(runs, threads)

    def origins(self, task_id):
        """Sources in which this task has a recorded task or invalid run."""
        return {path for path, runs in self.runs.items()
                if any(task == task_id for kind, task, _ in runs.values() if kind in ("task", "invalid"))}

    def reaches(self, origin, run, thread, task_id, origins):
        """Whether a row of `origin` naming this run or thread reaches the task.

        By class: a task run reaches its task; an invalid run with a task
        reference reaches that task; an invalid run without one, or a run in
        no class (never seen), reaches every task of its source; unscoped and
        legacy runs reach no task.
        """
        if run is not None:
            entries = [self.runs[origin][run][:2]] if run in self.runs[origin] else None
        else:
            entries = self.threads[origin].get(thread) if thread is not None else None
        if not entries:
            return origin in origins
        return any((kind in ("task", "invalid") and task == task_id)
                   or (kind == "invalid" and task is None and origin in origins) for kind, task in entries)


def _current_classes(con, paths):
    """The class each run has in its source now, for the given sources."""
    args = (json.dumps(sorted(paths)),)
    if _has_table(con, "t3_source_classes"):
        return {(r[0], r[1]): (r[2], r[3]) for r in con.execute(
            "SELECT source_path,run_id,class,task_id FROM t3_source_classes WHERE source_path IN "
            "(SELECT value FROM json_each(?))", args)}
    # A ledger refreshed before current classes were kept: mirrored runs only.
    return {(r[0], r[1]): (r[2], r[3]) for r in con.execute(
        "SELECT source_path,run_id,CASE WHEN scope='single' AND task_id IS NOT NULL THEN 'task'"
        " WHEN scope='unscoped' THEN 'unscoped' ELSE 'invalid' END,CASE WHEN scope='single' THEN task_id END"
        " FROM t3_source_runs WHERE source_path IN (SELECT value FROM json_each(?))", args)}


def _recorded_runs(con, task_id, record):
    """The task's runs by recorded class, compared with the class each has in its source now.

    Combined rule: a run absent from its source keeps its recorded class
    (deleted task run: `native_run_deleted`; invalid run: `native_run_invalid`).
    A present run whose recorded and current class and task agree is judged
    by that class. A recorded invalid run has already healed to its current
    class when that class is valid and agrees with its stored reference.
    Any other disagreement between the recorded and current class or task is
    a conflict for both tasks. Assumption: a Chromeria run's purpose is fixed
    when the run is created, so such a disagreement is never a legitimate change.
    """
    if not _has_table(con, "t3_source_runs"):
        return [], [], set()
    origins = record.origins(task_id) | {r[0] for r in con.execute(
        "SELECT DISTINCT source_path FROM t3_source_runs WHERE task_id=?", (task_id,))}
    current = _current_classes(con, origins)
    deleted, invalid, changed = set(), set(), set()
    for path in origins:
        for run, (kind, task, _) in record.runs[path].items():
            now = current.get((path, run))
            if now is None:
                if kind == "task" and task == task_id:
                    deleted.add(run)
                elif kind == "invalid" and task in (task_id, None):
                    invalid.add(run)
            elif now == (kind, task):
                if kind == "invalid" and task in (task_id, None):
                    invalid.add(run)
            elif task_id in (task, now[1]) or (kind == "invalid" and task is None):
                changed.add(run)
    return sorted(deleted), sorted(invalid), changed


def _rejected(con, native, record, origins):
    """Rejected Chromeria rows that reach this task through the run record.

    A row reaches the task by its own task reference, or by the class of the
    run or thread it names (RunRecord.reaches). A rejected run row is
    represented by its recorded class instead.
    """
    if not _has_table(con, "t3_purpose_rejects"):
        return []
    tied = []
    for r in con.execute("SELECT * FROM t3_purpose_rejects ORDER BY table_name,reason,row_id"):
        r = dict(r)
        if r["table_name"] == "runs" and r["run_id"] in record.runs[r["origin"]]:
            continue
        # A run or attempt row whose run id cannot be read names a run in no class.
        never_seen = r["run_id"] is None and r["table_name"] in ("runs", "run_attempts")
        if r["task_id"] == native["task_id"] or (never_seen and r["origin"] in origins) or record.reaches(
                r["origin"], r["run_id"], r["thread_id"], native["task_id"], origins):
            tied.append({k: r[k] for k in ("table_name", "reason", "row_id", "run_id", "thread_id")})
    return tied


def check_inventory(con, native, rows):
    """Decide whether the task's native evidence is complete, naming each gap with a count.

    A provider turn is complete when its usage closure states that the main
    agent's counters are all of its model usage and those counters reconcile
    with its joined responses. Runs, segments and delegated children add only
    the gaps their own evidence supports.
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
    gaps = _Gaps()
    main_gaps = set()
    for run_id in native["conflicting_source_runs"]:
        gaps.add("conflicting_native_run_records", "runs", run_id)
    if not native["runs"]:
        gaps.add("purpose_run_unavailable", "tasks", native["task_id"])
    native["segments"] = [dict(r) for r in con.execute(
        "SELECT * FROM t3_purpose_segments WHERE task_id=? ORDER BY thread_id,owner_id,segment_id",
        (native["task_id"],))]
    child_segment_runs = set()
    for segment in native["segments"]:
        segment["provider_turns"] = [dict(r) for r in con.execute(
            "SELECT * FROM t3_purpose_child_turns WHERE run_id=? AND segment_id=? ORDER BY provider_turn_id",
            (segment["run_id"], segment["segment_id"]))]
        turns = segment["provider_turns"]
        for turn in turns:
            group = by_segment[(turn["run_id"], turn["segment_id"], turn["session_key"])]
            turn["counter_check"] = (_turn_check(turn, group, finalized) if len(turns) == 1
                                     else "segment_turn_boundary_unresolved")
        item = (segment["thread_id"], segment["owner_id"], segment["segment_id"])
        if segment["kind"] in CHILD_SEGMENTS:
            # Native child agents are not reconciled; their usage stays unknown.
            gaps.add("native_child_segment", "segments", item)
            child_segment_runs.add(segment["run_id"])
        elif segment["run_id"] is None:
            gaps.add("background_segment_unresolved", "segments", item)
    checks = []
    not_counted = {"declared_elsewhere": 0, "deliberately_unscoped": 0}
    for run in native["runs"]:
        run_id = run["run_id"]
        attempts = [dict(r) for r in con.execute(
            "SELECT a.*,c.native_inputs,c.child_segments,c.background_inventory,c.native_usage_finality "
            "FROM t3_purpose_attempts a LEFT JOIN t3_purpose_capabilities c USING(provider_session_id) "
            "WHERE a.run_id=? ORDER BY a.attempt_id", (run_id,))]
        unsupported_table = _has_table(con, "t3_purpose_unsupported_turns")
        for a in attempts:
            a["turns"] = [dict(r) for r in con.execute(
                "SELECT * FROM t3_purpose_turns WHERE attempt_id=? ORDER BY provider_turn_id", (a["attempt_id"],))]
            # A turn on a driver Observer cannot read has no usage closure and joins no response.
            a["unsupported_turns"] = [dict(r) for r in con.execute(
                "SELECT * FROM t3_purpose_unsupported_turns WHERE attempt_id=? ORDER BY provider_turn_id",
                (a["attempt_id"],))] if unsupported_table else []
        # Only the refresh, which reads the raw projection before validation,
        # can prove a run never ran; absence in the mirror proves nothing.
        run["never_ran"] = run.get("never_ran") == "proven" and not any(
            a["turns"] or a["unsupported_turns"] for a in attempts)
        # A native operation on a driver Observer cannot read, tied to this run.
        if _has_table(con, "t3_purpose_unsupported_operations") and con.execute(
                "SELECT 1 FROM t3_purpose_unsupported_operations WHERE run_id=? LIMIT 1", (run_id,)).fetchone():
            gaps.add("provider_unsupported", "runs", run_id)
            main_gaps.add("provider_unsupported")
        if run["status"] not in TERMINAL:
            gaps.add("run_not_terminal", "runs", run_id)
        if not run["never_ran"] and run["active_attempt_id"] not in {a["attempt_id"] for a in attempts}:
            gaps.add("expected_attempt_missing", "runs", run_id)
            main_gaps.add("expected_attempt_missing")
        for a in attempts:
            turns = a.pop("turns")
            seen = {t["provider_turn_id"] for t in turns + a["unsupported_turns"]}
            if not run["never_ran"] and (not seen or (
                    a["provider_turn_id"] is not None and a["provider_turn_id"] not in seen)):
                gaps.add("expected_provider_turn_missing", "attempts", a["attempt_id"])
                main_gaps.add("expected_provider_turn_missing")
            if a["unsupported_turns"]:
                gaps.add("provider_unsupported", "runs", run_id)
                main_gaps.add("provider_unsupported")
            for turn in turns:
                closure = _closure(turn)
                closed = closure is not None and closure["state"] == "main_agent_only"
                listed = turn.get("native_response_ids_json")
                listed = {"claude:" + i for i in json.loads(listed)} if listed else None
                compactions = ((closure["uncountedCompactions"], _compactions(con, turn, closure))
                               if closed else None)
                state = _turn_check(turn, by_turn[turn["provider_turn_id"]], finalized, listed, compactions)
                turn_id = turn["provider_turn_id"]
                checks.append({"run_id": run_id, "attempt_id": a["attempt_id"],
                               "provider_turn_id": turn_id, "status": state,
                               "usage_closure": closure["state"] if closure else "missing",
                               **({"usage_closure_reason": closure["reason"]}
                                  if closure and closure["state"] == "open" else {}),
                               "provider_usage": json.loads(turn["usage_json"] or "null")})
                if state != "reconciled":
                    gaps.add(state, "turns", turn_id)
                    main_gaps.add(state)
                if closure is None:
                    gaps.add("usage_closure_missing", "turns", turn_id)
                elif not closed:
                    gaps.add("usage_closure_open", "turns", turn_id)
                elif run_id in child_segment_runs:
                    gaps.add("usage_closure_contradiction", "turns", turn_id)
        run["attempts"] = attempts
        run["participants"] = [dict(r) for r in con.execute(
            "SELECT * FROM t3_purpose_participants WHERE run_id=? ORDER BY kind,participant_id", (run_id,))]
        _children(con, native["task_id"], run, gaps, not_counted)
    for run_id in native.get("deleted_runs") or []:
        gaps.add("native_run_deleted", "runs", run_id)
    for run_id in native.get("invalid_runs") or []:
        gaps.add("native_run_invalid", "runs", run_id)
    for item in native.get("rejected_rows") or []:
        gaps.add("native_rows_rejected", "rows", tuple(item.values()))
    if native["conflicting_reference_kinds"]:
        for run in native["runs"]:
            gaps.add("conflicting_reference_kinds", "runs", run["run_id"])
    for rid in native["unavailable_response_usage"]:
        gaps.add("native_response_usage_unavailable", "responses", rid)
    for key in native["unlocated_usage_sessions"]:
        gaps.add("native_usage_unlocated", "sessions", key)
    for rid in native["conflicting_responses"]:
        gaps.add("conflicting_response_ownership", "responses", rid)
    native["turn_checks"] = checks
    native["main_agent_counters_reconciled"] = bool(checks) and not main_gaps
    native["coverage_gap_counts"] = gaps.counts()
    native["coverage_gaps"] = sorted(native["coverage_gap_counts"])
    native["children_not_counted"] = not_counted
    return bool(native["coverage_gaps"])
