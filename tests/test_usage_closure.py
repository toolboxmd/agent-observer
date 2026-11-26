"""Chromeria usage closures decide whether a task's cost can be known (#153, #154).

Each case builds a native Chromeria projection and real Claude or Codex
transcripts, syncs them through the T3 adapter and reads the public report and
the `task show` CLI.
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from agent_observer import publish, report
from agent_observer.adapters import claude, codex, t3
from tests.helpers import LedgerCase
from tests.test_native_response import claude_user, write_records
from tests.test_t3_purpose import PREFIX, PURPOSE, REF, TIME, strong

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASK = "example/project#7"
OTHER = dict(PURPOSE, scope=dict(type="single", reference=dict(REF, number=8)))
CLOSED_CLAUDE = dict(version=1, state="main_agent_only", witness="process_ledger", uncountedCompactions=0)
CLOSED_CODEX = dict(version=1, state="main_agent_only", witness="event_stream", uncountedCompactions=0)
TABLES = {
    "threads": "thread_id,created_at",
    "runs": "run_id,thread_id,status",
    "run_attempts": "attempt_id,run_id,thread_id,provider_thread_id,provider_turn_id,root_node_id",
    "provider_turns": "provider_turn_id,thread_id,provider_thread_id,run_attempt_id,node_id",
    "provider_threads": "provider_thread_id,thread_id,driver,provider,updated_at",
    "subagents": "subagent_id,thread_id,run_id,origin,driver,child_thread_id",
}


def claude_usage(inp, out):
    return dict(usageScope="main_agent", usageStatus="complete", hasSubagents=False,
                inputTokens=inp, outputTokens=out, cachedInputTokens=0, cacheCreationTokens=0)


def assistant(uuid, parent, mid, inp=10, out=5):
    return {"sessionId": "s", "type": "assistant", "uuid": uuid, "parentUuid": parent,
            "message": {"id": mid, "role": "assistant", "model": "claude-haiku-4-5",
                        "stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}


def codex_rollout(path, session, turns):
    """turns: (turn id, [(response id, input, output)], [compactions]).

    A compaction is a response id, written as real rollouts write it: one
    `compacted` record naming its response and turn, then a ContextCompaction
    marker in the same turn. A tuple (response id or None, attributed, marker)
    varies those parts.
    """
    rows = [dict(type="session_meta", payload=dict(session_id=session, cwd="/fixture", source="vscode"))]
    for turn, responses, compactions in turns:
        rows += [dict(type="event_msg", payload=dict(type="task_started", turn_id=turn)),
                 dict(type="turn_context", payload=dict(turn_id=turn, root_turn_id=turn, model="gpt-6-astra",
                                                        effort="high", cwd="/fixture")),
                 dict(type="response_item", payload=dict(
                     type="message", role="user", id="msg-" + turn,
                     content=[dict(type="input_text", text="fixture prompt")],
                     internal_chat_message_metadata_passthrough=dict(turn_id=turn)))]
        for rid, inp, out in responses:
            usage = dict(input_tokens=inp, cached_input_tokens=0, cache_write_input_tokens=0,
                         output_tokens=out, reasoning_output_tokens=0, total_tokens=inp + out)
            rows.append(dict(type="token_usage_record", payload=dict(
                response_id=rid, turn_id=turn, root_turn_id=turn, session_id=session, thread_id=session,
                usage=usage, turn_token_usage=usage, thread_token_usage=usage)))
        for i, spec in enumerate(compactions):
            rid, attributed, marker = (spec, True, True) if isinstance(spec, str) else spec
            record = dict(message="", window_id=f"window-{turn}-{i}")
            if rid:
                record["compaction_response_id"] = rid
            if attributed:
                record["latest_token_usage_record"] = dict(response_id=rid, turn_id=turn, session_id=session,
                                                           thread_id=session)
            rows.append(dict(type="compacted", payload=record))
            if marker:
                rows.append(dict(type="event_msg", payload=dict(
                    type="item_completed", turn_id=turn, thread_id=session,
                    item=dict(type="ContextCompaction", id=f"ctx-{turn}-{i}"))))
        rows.append(dict(type="event_msg", payload=dict(type="task_complete", turn_id=turn)))
    with open(path, "w") as out:
        for i, row in enumerate(rows):
            out.write(json.dumps(dict(row, ordinal=i, timestamp=f"2026-11-26T10:00:{i:02d}.000Z")) + "\n")


class UsageClosureTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp.name, "chromeria-v2.sqlite")
        self.native = sqlite3.connect(self.path)
        self.addCleanup(self.native.close)
        for name, columns in TABLES.items():
            self.native.execute(f"CREATE TABLE {PREFIX}{name} ({columns},payload_json)")
        self.row("threads", ["t", TIME], {"id": "t"})
        for driver in ("claudeAgent", "codex"):
            pt = "pt-" + driver
            self.row("provider_threads", [pt, "t", driver, driver + "-custom", TIME], {
                "id": pt, "appThreadId": "t", "driver": driver, "providerInstanceId": driver + "-custom",
                "nativeThreadRef": strong(driver, "current")})

    def row(self, table, columns, payload):
        values = [*columns, json.dumps(payload)]
        self.native.execute(f"INSERT INTO {PREFIX}{table} VALUES({','.join('?' for _ in values)})", values)

    def run_turn(self, run, driver, session, *, inputs=(), usage=None, closure=None, response_ids=None,
                 status="completed", run_status="completed", started=TIME, attempt_started=TIME,
                 purpose=PURPOSE, provenance=None, turn=True, thread="t", pt=None,
                 attempt=True, names_turn=None):
        pt = pt or "pt-" + driver
        att, turn_id, root = "attempt-" + run, "turn-" + run, "node-" + run
        instance = driver + "-custom"
        purpose = dict(purpose, provenance=provenance) if provenance else purpose
        named = turn_id if (turn if names_turn is None else names_turn) else None
        self.row("runs", [run, thread, run_status], dict(
            id=run, threadId=thread, providerThreadId=pt, providerInstanceId=instance, purpose=purpose,
            status=run_status, startedAt=started, activeAttemptId=att))
        if not attempt:
            return
        self.row("run_attempts", [att, run, thread, pt, named, root], dict(
            id=att, runId=run, providerThreadId=pt, rootNodeId=root, providerInstanceId=instance,
            nativeThreadId=session, providerTurnId=named, status=run_status,
            startedAt=attempt_started))
        if not turn:
            return
        kind, validation = (("message", "claude_prompt_echo") if driver == "claudeAgent"
                            else ("turn", "codex_turn_started"))
        payload = dict(id=turn_id, nodeId=root, providerThreadId=pt, runAttemptId=att, status=status,
                       nativeInputs=[dict(version=1, kind=kind, nativeId=i, validation=validation) for i in inputs],
                       turnTokenUsage=usage or claude_usage(0, 0))
        if closure is not None:
            payload["usageClosure"] = closure
        if response_ids is not None:
            payload["nativeResponseIds"] = response_ids
        self.row("provider_turns", [turn_id, thread, pt, att, root], payload)

    def sync(self):
        self.native.commit()
        result = t3.sync(self.con, source=self.path)
        self.assertEqual(result["failed"], [])
        self.con.commit()

    def native_report(self):
        self.sync()
        return report.task_report(self.con, TASK)

    def gaps(self, rep):
        native = rep["native_purpose"]
        return {name: (value["count"], value["unit"]) for name, value in native["coverage_gap_counts"].items()}

    def cli(self, *args):
        env = dict(os.environ, AGENT_OBSERVER_DB=self.db_path)
        result = subprocess.run([sys.executable, "-m", "agent_observer", "task", "show", "--task", TASK, *args],
                                cwd=REPO, capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    # Claude ---------------------------------------------------------------

    def claude_transcript(self, rows, session="s"):
        for row in rows:
            row["sessionId"] = session
        path = Path(self.tmp.name, session + ".jsonl")
        write_records(path, rows)
        claude.import_claude_file(self.con, str(path), full=True)

    def injected_turn(self, **kw):
        # A harness-injected input in the middle of the turn becomes the
        # nearest input above the second response.
        self.claude_transcript([
            claude_user("u-a", None, "fixture prompt"),
            assistant("r1", "u-a", "msg-1"),
            claude_user("u-inj", "r1", "<task-notification>fixture</task-notification>"),
            assistant("r2", "u-inj", "msg-2")])
        args = dict(inputs=["u-a"], usage=claude_usage(20, 10), closure=CLOSED_CLAUDE,
                    response_ids=["msg-1", "msg-2"])
        args.update(kw)
        self.run_turn("c1", "claudeAgent", "s", **args)

    def test_closed_claude_turn_with_injected_input_has_a_known_cost(self):
        self.injected_turn()
        rep = self.native_report()
        native = rep["native_purpose"]
        self.assertEqual(rep["measured"]["attributed_responses"], ["claude:msg-1", "claude:msg-2"])
        self.assertEqual([c["status"] for c in native["turn_checks"]], ["reconciled"])
        self.assertEqual(native["coverage_gaps"], [])
        self.assertEqual(rep["total_cost"]["status"], "complete")
        self.assertIsNotNone(rep["total_cost"]["usd"])
        text = self.cli()
        self.assertIn("estimated cost: under $0.01", text)
        self.assertNotIn("unknown (observed priced subtotal", text)
        # Removing one joined response turns the total back to unknown.
        self.con.execute("DELETE FROM responses WHERE response_id='claude:msg-2'")
        self.con.commit()
        late = report.task_report(self.con, TASK)
        self.assertIn("listed_response_missing", late["native_purpose"]["coverage_gaps"])
        self.assertIn("unknown (observed priced subtotal", late["total_cost"]["text"])

    def test_claude_turn_woken_by_a_background_task_joins_its_listed_responses(self):
        self.claude_transcript([claude_user("u-note", None, "<task-notification>done</task-notification>"),
                                assistant("r3", "u-note", "msg-3")])
        self.run_turn("wake", "claudeAgent", "s", inputs=[], usage=claude_usage(10, 5), closure=CLOSED_CLAUDE,
                      response_ids=["msg-3"], provenance=dict(type="native_operation", operationIds=["op-1"]))
        rep = self.native_report()
        self.assertEqual(rep["measured"]["attributed_responses"], ["claude:msg-3"])
        self.assertEqual(rep["native_purpose"]["membership"]["claude:msg-3"]["validation"],
                         "claude_native_response_id")
        self.assertEqual(rep["native_purpose"]["coverage_gaps"], [])
        self.assertEqual(rep["total_cost"]["status"], "complete")

    def test_listed_id_without_a_response_row_leaves_the_turn_unresolved(self):
        self.injected_turn(response_ids=["msg-1", "msg-2", "msg-9"])
        rep = self.native_report()
        self.assertEqual([c["status"] for c in rep["native_purpose"]["turn_checks"]], ["listed_response_missing"])
        self.assertEqual(self.gaps(rep), {"listed_response_missing": (1, "turns")})
        self.assertIn("unknown (observed priced subtotal", rep["total_cost"]["text"])

    def test_listed_response_that_ancestry_assigns_to_another_turn_conflicts(self):
        self.injected_turn(response_ids=["msg-2"])
        self.run_turn("c2", "claudeAgent", "s", inputs=[], usage=claude_usage(10, 5), closure=CLOSED_CLAUDE,
                      response_ids=["msg-1"])
        rep = self.native_report()
        self.assertIn("claude:msg-1", rep["native_purpose"]["conflicting_responses"])
        self.assertIn("conflicting_response_ownership", rep["native_purpose"]["coverage_gaps"])
        self.assertNotIn("claude:msg-1", rep["measured"]["attributed_responses"])

    # Codex ----------------------------------------------------------------

    def codex_turn(self, compactions, closure, *, recorded=("resp-c",)):
        path = os.path.join(self.tmp.name, "rollout-2026-11-26T10-00-00-cx-s.jsonl")
        responses = [("resp-1", 100, 10)] + [(rid, 50, 5) for rid in recorded]
        codex_rollout(path, "cx-s", [("cx-t1", responses, compactions)])
        codex.import_codex_file(self.con, path)
        usage = dict(usageScope="main_agent", usageStatus="complete", hasSubagents=False,
                     inputTokens=100, outputTokens=10)
        self.run_turn("x1", "codex", "cx-s", inputs=["cx-t1"], usage=usage, closure=closure)

    def test_closed_codex_turn_with_one_compaction_has_a_known_cost(self):
        self.codex_turn(["resp-c"], dict(CLOSED_CODEX, uncountedCompactions=1))
        rep = self.native_report()
        self.assertEqual(rep["measured"]["attributed_responses"], ["codex:resp-1", "codex:resp-c"])
        self.assertEqual([c["status"] for c in rep["native_purpose"]["turn_checks"]], ["reconciled"])
        self.assertEqual(rep["native_purpose"]["coverage_gaps"], [])
        self.assertEqual(rep["total_cost"]["status"], "complete")
        self.assertEqual(rep["total_cost"]["responses"], 2)  # Compaction responses stay in the cost.

    def test_codex_compaction_record_without_its_response_leaves_the_turn_unresolved(self):
        self.codex_turn(["resp-c"], dict(CLOSED_CODEX, uncountedCompactions=1), recorded=())
        rep = self.native_report()
        self.assertEqual([c["status"] for c in rep["native_purpose"]["turn_checks"]],
                         ["compaction_response_missing"])
        self.assertEqual(self.gaps(rep), {"compaction_response_missing": (1, "turns")})
        self.assertIn("unknown (observed priced subtotal", rep["total_cost"]["text"])

    def test_codex_compaction_count_that_differs_from_the_closure_leaves_the_turn_unresolved(self):
        self.codex_turn(["resp-c"], dict(CLOSED_CODEX, uncountedCompactions=2))
        rep = self.native_report()
        self.assertEqual([c["status"] for c in rep["native_purpose"]["turn_checks"]],
                         ["compaction_count_mismatch"])
        self.assertIn("unknown (observed priced subtotal", rep["total_cost"]["text"])

    def test_compaction_without_a_response_id_in_the_turn_leaves_a_zero_closure_unresolved(self):
        self.codex_turn([(None, True, True)], CLOSED_CODEX, recorded=())
        rep = self.native_report()
        self.assertEqual([c["status"] for c in rep["native_purpose"]["turn_checks"]],
                         ["compaction_response_missing"])
        self.assertIn("unknown (observed priced subtotal", rep["total_cost"]["text"])

    def test_compaction_no_turn_can_own_leaves_the_sessions_closed_turns_unresolved(self):
        self.codex_turn([("resp-c", False, False)], CLOSED_CODEX, recorded=())
        rep = self.native_report()
        self.assertEqual([c["status"] for c in rep["native_purpose"]["turn_checks"]],
                         ["compaction_unattributed"])
        self.assertEqual(self.gaps(rep), {"compaction_unattributed": (1, "turns")})

    def test_compaction_marker_without_its_record_is_a_compaction_whose_response_is_missing(self):
        # A pre-change import kept the marker's turn but not the record's ids.
        self.codex_turn([("resp-c", False, True)], CLOSED_CODEX, recorded=())
        self.con.execute("UPDATE events SET detail_json=NULL WHERE family='compaction'")
        self.con.execute("UPDATE events SET turn_id=NULL WHERE family='compaction' AND native_id LIKE 'window-%'")
        rep = self.native_report()
        self.assertEqual([c["status"] for c in rep["native_purpose"]["turn_checks"]],
                         ["compaction_unattributed"])
        self.con.execute("DELETE FROM events WHERE family='compaction' AND native_id LIKE 'window-%'")
        rep = report.task_report(self.con, TASK)
        self.assertEqual([c["status"] for c in rep["native_purpose"]["turn_checks"]],
                         ["compaction_response_missing"])

    # Closure states --------------------------------------------------------

    def test_open_missing_and_unknown_closures_keep_the_total_unknown_with_counts(self):
        self.claude_transcript([claude_user("u-a", None, "a"), assistant("r1", "u-a", "msg-1"),
                                claude_user("u-b", "r1", "b"), assistant("r2", "u-b", "msg-2"),
                                claude_user("u-c", "r2", "c"), assistant("r3", "u-c", "msg-3")])
        self.run_turn("o", "claudeAgent", "s", inputs=["u-a"], usage=claude_usage(10, 5),
                      closure=dict(version=1, state="open", reason="subagent_observed"))
        self.run_turn("m", "claudeAgent", "s", inputs=["u-b"], usage=claude_usage(10, 5))
        self.run_turn("u", "claudeAgent", "s", inputs=["u-c"], usage=claude_usage(10, 5),
                      closure=dict(CLOSED_CLAUDE, version=2))
        rep = self.native_report()
        self.assertEqual([c["status"] for c in rep["native_purpose"]["turn_checks"]], ["reconciled"] * 3)
        self.assertEqual(self.gaps(rep), {"usage_closure_open": (1, "turns"),
                                          "usage_closure_missing": (2, "turns")})
        self.assertEqual(rep["total_cost"]["status"], "partial")
        text = self.cli()
        self.assertIn("usage_closure_missing (2 turns)", text)
        self.assertIn("usage_closure_open (1 turn)", text)
        counts = json.loads(self.cli("--json"))["native_purpose"]["coverage_gap_counts"]
        self.assertEqual(counts["usage_closure_missing"], {"count": 2, "unit": "turns"})
        body = publish.render(publish.summarize(self.con, set(), TASK, task_id=TASK))
        self.assertIn("usage closure missing (2 turns), usage closure open (1 turn)", body)
        self.assertNotIn("subagent_observed", body)

    def test_closure_or_response_list_with_unexpected_shape_is_no_statement(self):
        self.claude_transcript([claude_user("u-a", None, "a"), assistant("r1", "u-a", "msg-1"),
                                claude_user("u-b", "r1", "b"), assistant("r2", "u-b", "msg-2")])
        self.run_turn("x", "claudeAgent", "s", inputs=["u-a"], usage=claude_usage(10, 5),
                      closure=dict(CLOSED_CLAUDE, qualifier="x"), response_ids=["msg-1"])
        self.run_turn("y", "claudeAgent", "s", inputs=["u-b"], usage=claude_usage(10, 5),
                      closure=dict(version=1, state="open", reason="subagent_observed", witness="process_ledger"),
                      response_ids=["msg-2", "msg-2"])
        rep = self.native_report()
        self.assertEqual(self.gaps(rep), {"usage_closure_missing": (2, "turns")})
        turns = dict(self.con.execute("SELECT provider_turn_id,native_response_ids_json FROM t3_purpose_turns"))
        self.assertEqual(turns, {"turn-x": '["msg-1"]', "turn-y": None})

    # Run rule ---------------------------------------------------------------

    def closed_turn(self):
        self.claude_transcript([claude_user("u-a", None, "a"), assistant("r1", "u-a", "msg-1")])
        self.run_turn("c1", "claudeAgent", "s", inputs=["u-a"], usage=claude_usage(10, 5),
                      closure=CLOSED_CLAUDE, response_ids=["msg-1"])

    def test_run_that_never_started_adds_no_gap(self):
        self.closed_turn()
        for status in ("cancelled", "failed", "interrupted"):
            self.run_turn("never-" + status, "claudeAgent", "s", run_status=status, started=None,
                          attempt_started=None, turn=False)
        rep = self.native_report()
        self.assertEqual(rep["native_purpose"]["coverage_gaps"], [])
        self.assertEqual(rep["total_cost"]["status"], "complete")

    def test_unseen_turn_or_active_attempt_is_not_proof_that_a_run_never_started(self):
        self.closed_turn()
        # The attempt names a provider turn the record does not hold.
        self.run_turn("named", "claudeAgent", "s", run_status="cancelled", started=None,
                      attempt_started=None, turn=False, names_turn=True)
        # The run names an active attempt the record does not hold.
        self.run_turn("ghost", "claudeAgent", "s", run_status="cancelled", started=None, attempt=False)
        rep = self.native_report()
        gaps = self.gaps(rep)
        self.assertEqual(gaps["expected_provider_turn_missing"], (1, "attempts"))
        self.assertEqual(gaps["expected_attempt_missing"], (1, "runs"))
        self.assertFalse(any(r["never_ran"] for r in rep["native_purpose"]["runs"]
                             if r["run_id"] in ("named", "ghost")))
        self.assertIn("unknown (observed priced subtotal", rep["total_cost"]["text"])

    def hidden_turn(self, run, attempt_payload=None, turn_payload=None, started=None):
        """A cancelled run whose unstarted, turnless-looking attempt has a raw turn row."""
        pt, att = "pt-claudeAgent", "attempt-" + run
        self.row("runs", [run, "t", "cancelled"], dict(
            id=run, threadId="t", providerThreadId=pt, providerInstanceId="claudeAgent-custom", purpose=PURPOSE,
            status="cancelled", startedAt=started, activeAttemptId=att))
        self.row("run_attempts", [att, run, "t", pt, None, "node-" + run], dict(dict(
            id=att, runId=run, providerThreadId=pt, rootNodeId="node-" + run,
            providerInstanceId="claudeAgent-custom", nativeThreadId="s", providerTurnId=None,
            status="cancelled", startedAt=None), **(attempt_payload or {})))
        if turn_payload is not None:
            self.row("provider_turns", ["turn-" + run, "t", pt, att, "node-" + run], dict(dict(
                id="turn-" + run, nodeId="node-" + run, providerThreadId=pt, runAttemptId=att,
                status="interrupted", nativeInputs=[], turnTokenUsage=dict(
                    usageScope="main_agent", usageStatus="partial", hasSubagents=False, inputTokens=999,
                    outputTokens=99)), **turn_payload))

    def test_a_turn_the_mirror_dropped_is_not_proof_that_a_run_never_ran(self):
        self.closed_turn()
        # The attempt fails validation, so its turn is never mirrored.
        self.hidden_turn("bad-attempt", attempt_payload=dict(providerInstanceId="other"), turn_payload={})
        # The attempt is valid but its turn fails node validation.
        self.hidden_turn("bad-turn", turn_payload=dict(nodeId="elsewhere"))
        rep = self.native_report()
        self.assertEqual(self.gaps(rep), {"expected_provider_turn_missing": (2, "attempts"),
                                          "native_rows_unmirrored": (2, "runs")})
        self.assertFalse(any(r["never_ran"] for r in rep["native_purpose"]["runs"] if r["run_id"].startswith("bad")))
        self.assertIn("unknown (observed priced subtotal", rep["total_cost"]["text"])

    def test_a_dropped_turn_or_attempt_beside_valid_ones_is_a_gap(self):
        self.closed_turn()
        # A second turn of the same attempt fails node validation.
        self.row("provider_turns", ["turn-c1-b", "t", "pt-claudeAgent", "attempt-c1", "elsewhere"], dict(
            id="turn-c1-b", nodeId="elsewhere", providerThreadId="pt-claudeAgent", runAttemptId="attempt-c1",
            status="completed", nativeInputs=[], turnTokenUsage=claude_usage(500, 50)))
        rep = self.native_report()
        self.assertEqual(self.gaps(rep), {"native_rows_unmirrored": (1, "runs")})
        self.native.execute(f"DELETE FROM {PREFIX}provider_turns WHERE provider_turn_id='turn-c1-b'")
        # An earlier attempt of the run is on another thread, so it is never mirrored.
        self.row("run_attempts", ["attempt-c1-0", "c1", "other-thread", "pt-claudeAgent", None, "node-0"], dict(
            id="attempt-c1-0", runId="c1", providerThreadId="pt-claudeAgent", rootNodeId="node-0",
            providerInstanceId="claudeAgent-custom", nativeThreadId="s", providerTurnId=None,
            status="failed", startedAt=TIME))
        rep = self.native_report()
        self.assertEqual(self.gaps(rep), {"native_rows_unmirrored": (1, "runs")})
        self.assertIn("unknown (observed priced subtotal", rep["total_cost"]["text"])

    def test_a_started_run_whose_attempt_never_started_is_not_exempt(self):
        self.closed_turn()
        self.hidden_turn("started", started=TIME)
        rep = self.native_report()
        self.assertEqual(self.gaps(rep), {"expected_provider_turn_missing": (1, "attempts")})

    def test_run_without_a_start_time_is_judged_by_its_turn(self):
        self.closed_turn()
        self.run_turn("late", "claudeAgent", "s", run_status="interrupted", status="interrupted",
                      started=None, attempt_started=None, usage=claude_usage(253345, 0))
        rep = self.native_report()
        self.assertEqual(self.gaps(rep), {"native_final_usage_missing": (1, "turns"),
                                          "usage_closure_missing": (1, "turns")})

    def test_started_attempt_without_a_turn_and_an_active_run_keep_the_total_unknown(self):
        self.closed_turn()
        self.run_turn("lost", "claudeAgent", "s", run_status="failed", started=None, turn=False)
        self.run_turn("busy", "claudeAgent", "s", run_status="running", started=TIME, turn=False)
        rep = self.native_report()
        gaps = self.gaps(rep)
        self.assertEqual(gaps["expected_provider_turn_missing"], (2, "attempts"))
        self.assertEqual(gaps["run_not_terminal"], (1, "runs"))
        self.assertNotIn("run_not_completed", gaps)

    # Segments and children --------------------------------------------------

    def segment(self, kind, source_run="c1", child="child"):
        source = dict(type="app_run", threadId="t", runId=source_run)
        return dict(version=1, id=f"s:{kind}", nativeSessionId="s", nativeChildId=child,
                    nativeOperationId="op-" + kind, kind=kind, purpose=PURPOSE, source=source,
                    boundary=dict(state="unresolved", reason="claude_resume_response_boundary_missing"),
                    status="ended", usageFinality="unreconciled", noticeIds=[], wakeDispositions=[],
                    observedAt=TIME)

    def test_child_kind_segment_contradicts_a_closed_closure_and_background_adds_no_gap(self):
        self.closed_turn()
        self.native.execute(f"UPDATE {PREFIX}provider_threads SET payload_json=json_set(payload_json,"
                            "'$.nativeOperations',json(?)) WHERE provider_thread_id='pt-claudeAgent'",
                            (json.dumps([self.segment("background", child="shell")]),))
        rep = self.native_report()
        self.assertEqual(rep["native_purpose"]["coverage_gaps"], [])
        self.assertEqual(rep["total_cost"]["status"], "complete")
        self.row("subagents", ["agent-1", "t", "c1", "provider_native", "claudeAgent", None], {
            "id": "agent-1", "threadId": "t", "runId": "c1", "driver": "claudeAgent",
            "origin": "provider_native", "childThreadId": None, "status": "completed",
            "nativeSegments": [self.segment("start")]})
        rep = self.native_report()
        gaps = self.gaps(rep)
        self.assertEqual(gaps["native_child_segment"], (1, "segments"))
        self.assertEqual(gaps["usage_closure_contradiction"], (1, "turns"))
        self.assertIn("unknown (observed priced subtotal", rep["total_cost"]["text"])

    def child(self, name, purpose, status="completed", runs=True):
        thread = "child-" + name
        self.row("subagents", [name, "t", "c1", "app_owned", "claudeAgent", thread], {
            "id": name, "threadId": "t", "runId": "c1", "driver": "claudeAgent", "origin": "app_owned",
            "childThreadId": thread, "status": status, **({"purpose": purpose} if purpose else {})})
        if runs:
            self.row("threads", [thread, TIME], {"id": thread})
            self.row("runs", ["run-" + name, thread, "completed"], dict(
                id="run-" + name, threadId=thread, purpose=purpose or PURPOSE, status="completed",
                startedAt=None, activeAttemptId=None))

    def test_children_are_counted_by_their_declared_purpose(self):
        self.closed_turn()
        self.child("same", PURPOSE)
        self.child("elsewhere", OTHER)
        self.child("deliberate", dict(PURPOSE, scope=dict(type="unscoped", reason="deliberate")))
        rep = self.native_report()
        native = rep["native_purpose"]
        # The same-task child's run has no attempt and never started: it adds nothing.
        self.assertEqual(native["coverage_gaps"], [])
        self.assertEqual(native["children_not_counted"], {"declared_elsewhere": 1, "deliberately_unscoped": 1})
        self.assertEqual(rep["total_cost"]["status"], "complete")
        self.assertIn("not counted: 1 delegated child declared another task, 1 deliberately unscoped",
                      self.cli())
        self.child("legacy", dict(PURPOSE, scope=dict(type="unscoped", reason="legacy_no_intent")))
        self.child("active", PURPOSE, status="running")
        self.child("orphan", PURPOSE, runs=False)
        rep = self.native_report()
        gaps = self.gaps(rep)
        self.assertEqual(gaps["child_purpose_undeclared"], (1, "children"))
        self.assertEqual(gaps["child_active"], (1, "children"))
        self.assertEqual(gaps["child_runs_missing"], (1, "children"))
        self.assertNotIn("child_inventory_requires_reconciliation", gaps)
        self.assertIn("unknown (observed priced subtotal", rep["total_cost"]["text"])

    def test_same_task_child_that_ran_is_counted_through_its_own_turn(self):
        self.closed_turn()
        self.row("subagents", ["kid", "t", "c1", "app_owned", "claudeAgent", "child-kid"], {
            "id": "kid", "threadId": "t", "runId": "c1", "driver": "claudeAgent", "origin": "app_owned",
            "childThreadId": "child-kid", "status": "completed", "purpose": PURPOSE})
        self.row("threads", ["child-kid", TIME], {"id": "child-kid"})
        self.row("provider_threads", ["pt-kid", "child-kid", "claudeAgent", "claudeAgent-custom", TIME], {
            "id": "pt-kid", "appThreadId": "child-kid", "driver": "claudeAgent",
            "providerInstanceId": "claudeAgent-custom", "nativeThreadRef": strong("claudeAgent", "current")})
        self.claude_transcript([claude_user("k-a", None, "child prompt"), assistant("k1", "k-a", "msg-k1", 30, 7)],
                               session="kid-s")
        self.run_turn("kid-run", "claudeAgent", "kid-s", inputs=["k-a"], usage=claude_usage(30, 7),
                      closure=CLOSED_CLAUDE, response_ids=["msg-k1"], thread="child-kid", pt="pt-kid")
        rep = self.native_report()
        native = rep["native_purpose"]
        self.assertEqual(sorted(c["status"] for c in native["turn_checks"]), ["reconciled", "reconciled"])
        self.assertIn("claude:msg-k1", rep["measured"]["attributed_responses"])
        self.assertEqual(native["coverage_gaps"], [])
        self.assertEqual(rep["total_cost"]["status"], "complete")
        self.native.execute(f"UPDATE {PREFIX}provider_turns SET payload_json=json_remove(payload_json,"
                            "'$.turnTokenUsage') WHERE provider_turn_id='turn-kid-run'")
        rep = self.native_report()
        self.assertEqual(self.gaps(rep), {"native_final_usage_missing": (1, "turns")})
        self.assertIn("unknown (observed priced subtotal", rep["total_cost"]["text"])

    # Upgrade ----------------------------------------------------------------

    def test_ledger_from_1_7_0_upgrades_on_sync_and_reads_stay_read_only(self):
        self.closed_turn()
        self.sync()
        # Recreate the 1.7.0 mirror: the new columns did not exist.
        for table, column in (("runs", "never_ran"), ("runs", "raw_rows_json"),
                              ("turns", "native_response_ids_json"), ("turns", "usage_closure_json")):
            for prefix in ("t3_purpose_", "t3_source_"):
                self.con.execute(f"ALTER TABLE {prefix}{table} DROP COLUMN {column}")
        self.con.commit()
        self.con.execute("PRAGMA query_only=ON")
        old = report.task_report(self.con, TASK)
        self.assertIn("usage_closure_missing", old["native_purpose"]["coverage_gaps"])
        self.con.execute("PRAGMA query_only=OFF")
        rep = self.native_report()
        self.assertEqual(rep["native_purpose"]["coverage_gaps"], [])
        self.assertEqual(rep["total_cost"]["status"], "complete")
