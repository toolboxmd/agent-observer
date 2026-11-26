"""Exercise shared-planner reports with native usage and independent purpose rows."""

from contextlib import closing
from pathlib import Path
import json
import sqlite3

from agent_observer import publish, report
from agent_observer.adapters import claude, opencode, t3_purpose
from tests.test_native_response import claude_response, claude_user, write_records
from tests.test_opencode import build_native
from tests.helpers import LedgerCase


class NativeOutcomeTest(LedgerCase):
    session = "codex:sess-fixture-mini-01"
    task = "example/repo#11"

    def setUp(self):
        super().setUp()
        self.sync("codex-mini.jsonl")
        with closing(sqlite3.connect(":memory:")) as native:
            t3_purpose.refresh(self.con, native, set())
        self.con.execute("INSERT INTO tasks(task_id,project,title,created_at)"
                         " VALUES(?,?,?,0)", (self.task, "example/repo", self.task))
        self.con.execute("INSERT INTO t3_purpose_runs(run_id,thread_id,task_id,scope,kind,"
                         "status,active_attempt_id,raw_rows_json) VALUES('a','planner',?,'single','issue',"
                         "'completed','aa','{\"attempts\":[\"aa\"],\"turns\":[\"ta\"]}')",
                         (self.task,))
        self.con.execute("INSERT INTO t3_purpose_sessions VALUES('a',?)", (self.session,))
        self.con.execute("INSERT INTO t3_purpose_inputs(run_id,session_key,native_id,kind,validation,"
                         "attempt_id,provider_turn_id) VALUES('a',?,'turn-mini-aaa','turn',"
                         "'codex_turn_started','aa','ta')", (self.session,))
        self.con.execute("INSERT INTO t3_purpose_attempts(attempt_id,run_id,provider_turn_id)"
                         " VALUES('aa','a','ta')")
        self.con.execute("INSERT INTO t3_purpose_turns(provider_turn_id,attempt_id,run_id,"
                         "status,usage_json) VALUES('ta','aa','a','completed',?)",
                         (json.dumps(dict(usageStatus="complete", usageScope="main_agent",
                                          inputTokens=3000, outputTokens=350, hasSubagents=False)),))
        self.con.commit()

    def test_final_counter_reconciliation_changes_when_a_response_disappears(self):
        rep = report.task_report(self.con, self.task)
        self.assertEqual(rep["measured"]["attributed_responses"], ["codex:resp-mini-001", "codex:resp-mini-002"])
        self.assertEqual(rep["native_purpose"]["turn_checks"][0]["status"], "reconciled")
        self.assertFalse(rep["complete"])
        self.assertIsNone(rep["total_cost"]["usd"])
        self.assertEqual(rep["native_purpose"]["coverage_gaps"], ["usage_closure_missing"])
        self.con.execute("DELETE FROM responses WHERE response_id='codex:resp-mini-002'")
        late = report.task_report(self.con, self.task)
        self.assertEqual(late["native_purpose"]["turn_checks"][0]["status"], "counter_mismatch")
        self.assertNotEqual(late["snapshot_id"], rep["snapshot_id"])

    def test_validated_inverse_turn_witness_when_attempt_has_no_turn_pointer(self):
        self.con.execute("UPDATE t3_purpose_attempts SET provider_turn_id=NULL")
        self.assertTrue(report.task_report(self.con, self.task)["native_purpose"]["main_agent_counters_reconciled"])
        self.con.execute("DELETE FROM t3_purpose_turns")
        gaps = report.task_report(self.con, self.task)["native_purpose"]["coverage_gaps"]
        self.assertIn("expected_provider_turn_missing", gaps)

    def test_two_issues_share_a_planner_but_each_gets_its_own_responses(self):
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES('example/repo#12',0)")
        self.con.execute("INSERT INTO t3_purpose_runs(run_id,thread_id,task_id,scope)"
                         " VALUES('b','planner','example/repo#12','single')")
        self.con.execute("INSERT INTO t3_purpose_sessions VALUES('b',?)", (self.session,))
        self.con.execute("INSERT INTO t3_purpose_inputs(run_id,session_key,native_id,kind,validation)"
                         " VALUES('b',?,'turn-mini-bbb','turn','codex_turn_started')", (self.session,))
        a = report.task_report(self.con, self.task)
        b = report.task_report(self.con, "example/repo#12")
        self.assertEqual(b["measured"]["attributed_responses"], ["codex:resp-mini-003"])
        self.assertEqual(b["measured"]["other_task_responses"], a["measured"]["attributed_responses"])
        self.assertEqual(a["measured"]["other_task_responses"], b["measured"]["attributed_responses"])
        self.assertTrue(a["reconciles"] and b["reconciles"])

    def test_expected_child_without_its_projection_remains_missing(self):
        self.con.execute("INSERT INTO t3_purpose_participants(run_id,thread_id,kind,participant_id)"
                         " VALUES('a','planner','subagent_item','child-1')")
        rep = report.task_report(self.con, self.task)
        self.assertEqual(rep["native_purpose"]["runs"][0]["missing_children"], ["child-1"])
        self.assertIn("expected_child_missing", rep["native_purpose"]["coverage_gaps"])
        self.assertTrue(rep["native_purpose"]["main_agent_counters_reconciled"])
        self.assertFalse(rep["coverage"]["complete"])

    def test_second_claim_on_the_same_input_fails_closed_and_trace_filters_intersect(self):
        self.con.execute("INSERT INTO t3_purpose_runs(run_id,thread_id,task_id,scope)"
                         " VALUES('b','planner','example/repo#12','single')")
        self.con.execute("INSERT INTO t3_purpose_sessions VALUES('b',?)", (self.session,))
        self.con.execute("INSERT INTO t3_purpose_inputs(run_id,session_key,native_id,kind,validation)"
                         " VALUES('b',?,'turn-mini-aaa','turn','codex_turn_started')", (self.session,))
        rep = report.task_report(self.con, self.task)
        self.assertEqual(rep["attributed"]["responses"], 0)
        self.assertEqual(len(rep["native_purpose"]["conflicting_responses"]), 2)
        self.assertEqual(rep["total_cost"]["text"], "unknown")
        self.assertFalse(report.timeline(self.con, self.task, turn_id="codex:turn-mini-aaa")["events"])

    def test_delivery_does_not_move_responses_and_report_needs_no_write(self):
        before = report.task_report(self.con, self.task)
        self.con.execute("INSERT INTO t3_purpose_deliveries(task_id,thread_id,host,repository,number,"
                         "url,declared_at) VALUES(?,'planner','github.com','example/repo',42,"
                         "'https://github.com/example/repo/pull/42','2026-11-25T00:00:00Z')", (self.task,))
        self.con.execute("PRAGMA query_only=ON")
        after = report.task_report(self.con, self.task)
        self.assertEqual(after["measured"], before["measured"])
        self.assertNotEqual(after["snapshot_id"], before["snapshot_id"])
        summary = publish.summarize(self.con, set(), self.task, task_id=self.task)
        self.assertIn("Delivery PR: https://github.com/example/repo/pull/42", publish.render(summary))
        self.assertTrue(report.timeline(self.con, self.task)["events"])

    def test_delivery_survives_missing_run_with_unknown_cost(self):
        self.con.execute("INSERT INTO t3_purpose_deliveries VALUES(?, 'planner', 'github.com',"
                         "'example/repo',42,'https://github.com/example/repo/pull/42','2026-11-25T00:00:00Z')",
                         (self.task,))
        self.con.execute("DELETE FROM t3_purpose_runs")
        rep = report.task_report(self.con, self.task)
        self.assertEqual(rep["native_purpose"]["deliveries"][0]["number"], 42)
        self.assertIn("purpose_run_unavailable", rep["native_purpose"]["coverage_gaps"])
        self.assertIsNone(rep["total_cost"]["usd"])

    def assign(self, task, prompt):
        self.con.execute("INSERT INTO assignments(user_message_id,task_id,created_at)"
                         " VALUES(?,?,0)", ("codex:msg-mini-sub-" + prompt, task))

    def test_capture_alongside_native_turn(self):
        self.assign(self.task, "02")
        rep = report.task_report(self.con, self.task)
        self.assertEqual(rep["measured"]["attributed_responses"],
                         ["codex:resp-mini-001", "codex:resp-mini-002", "codex:resp-mini-003"])
        self.assertNotIn("codex:resp-mini-003", rep["native_purpose"]["membership"])
        trace = report.timeline(self.con, self.task, turn_id="codex:turn-mini-bbb")
        self.assertEqual({e["native_id"] for e in trace["events"]},
                         {"call-mini-spawn-01", "task_complete:turn-mini-bbb"})
        self.assertEqual(trace["native_response_count"], 0)
        self.assertTrue(rep["reconciles"])

    def test_conflicting_capture_without_native_run(self):
        other = "example/repo#12"
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", (other,))
        self.assign(other, "01")
        self.assign(other, "02")
        for task in (other, self.task):
            rep = report.task_report(self.con, task)
            self.assertEqual(rep["measured"]["unplaced_responses"],
                             ["codex:resp-mini-001", "codex:resp-mini-002"])
            self.assertTrue(rep["reconciles"])
            trace = report.timeline(self.con, task)
            if task == other:
                self.assertEqual(rep["measured"]["attributed_responses"], ["codex:resp-mini-003"])
                self.assertEqual({e["native_id"] for e in trace["events"]},
                                 {"call-mini-spawn-01", "task_complete:turn-mini-bbb"})
            else:
                self.assertEqual(rep["measured"]["other_task_responses"], ["codex:resp-mini-003"])
                self.assertFalse(trace["events"])

    def test_router_whole_session_conflict_only_exposes_uncontested_turn(self):
        other = "router-job"
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", (other,))
        self.con.execute("INSERT INTO session_assignments VALUES(?,?,'router:job',0)", (self.session, other))
        for task in (other, self.task):
            rep = report.task_report(self.con, task)
            self.assertEqual(rep["measured"]["unplaced_responses"],
                             ["codex:resp-mini-001", "codex:resp-mini-002"])
            self.assertTrue(rep["reconciles"])
            trace = report.timeline(self.con, task)
            if task == other:
                self.assertEqual(rep["measured"]["attributed_responses"], ["codex:resp-mini-003"])
                self.assertEqual({e["native_id"] for e in trace["events"]},
                                 {"call-mini-spawn-01", "task_complete:turn-mini-bbb"})
            else:
                self.assertFalse(trace["events"])

    def test_compatible_router_binding_keeps_whole_session_events(self):
        self.con.execute("INSERT INTO session_assignments VALUES(?,?,'router:job',0)", (self.session, self.task))
        self.con.execute("INSERT INTO events(session_key,turn_id,native_id,family) "
                         "VALUES('codex:unrelated','codex:turn-mini-aaa','foreign-call','tool_call')")
        rep = report.task_report(self.con, self.task)
        self.assertEqual(rep["attributed"]["responses"], 3)
        task_trace = report.timeline(self.con, self.task)
        session_trace = report.timeline(self.con, session_key=self.session)
        self.assertEqual([e["id"] for e in task_trace["events"]], [e["id"] for e in session_trace["events"]])
        self.assertEqual(task_trace["native_response_count"], 2)

    def test_claude_display_turn_cannot_override_native_ancestry_in_capture_trace(self):
        path = Path(self.tmp.name, "s.jsonl")
        answers = [claude_response("answer-a", "a", "first"), claude_response("answer-b", "b", "second")]
        for answer in answers:
            answer["message"]["content"] = [{"type": "text", "text": "answer"}]
        answers[0]["message"]["content"].append({"type": "tool_use", "id": "foreign-call", "name": "Read"})
        # Both assistant events carry the latest display turn, but their parents differ.
        write_records(path, [claude_user("a"), claude_user("b"), *answers])
        claude.import_claude_file(self.con, str(path))
        self.con.execute("INSERT INTO t3_purpose_sessions VALUES('a','claude:s')")
        self.con.execute("INSERT INTO t3_purpose_inputs(run_id,session_key,native_id,kind,validation)"
                         " VALUES('a','claude:s','a','message','claude_prompt_echo')")
        other = "example/repo#12"
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", (other,))
        self.con.execute("INSERT INTO assignments(user_message_id,task_id,created_at) VALUES('claude:b',?,0)",
                         (other,))
        for task, expected, excluded in ((self.task, "first", "second"), (other, "second", "first")):
            rep = report.task_report(self.con, task)
            self.assertIn("claude:" + expected, rep["measured"]["attributed_responses"])
            self.assertIn("claude:" + excluded, rep["measured"]["other_task_responses"])
            trace = report.timeline(self.con, task, session_key="claude:s", family="assistant_message")
            self.assertEqual([e["native_id"] for e in trace["events"]],
                             ["answer-a:0" if task == self.task else "answer-b:0"])
            selected = report.timeline(self.con, task, session_key="claude:s",
                                       turn_id="claude:a" if task == self.task else "claude:b")
            self.assertCountEqual([e["native_id"] for e in selected["events"]],
                                  ["foreign-call", "answer-a:0"] if task == self.task else ["answer-b:0"])

    def test_delivery_alone_preserves_legacy_placement_and_capture_trace(self):
        from unittest.mock import patch
        self.con.execute("DELETE FROM t3_purpose_runs")
        other = "example/repo#12"
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", (other,))
        self.con.executemany("INSERT INTO session_assignments VALUES(?,?,'t3',0)",
                             [(self.session, self.task), (self.session, other)])
        observed = {"codex:resp-mini-001": [{"host": "github.com", "repository": "example/repo",
                                            "branch": "topic", "candidate_tasks": [self.task]}]}
        with patch("agent_observer.placement.checkout_observations", return_value=observed):
            before = report.task_report(self.con, self.task)
            self.con.execute("INSERT INTO t3_purpose_deliveries VALUES(?, 'planner', 'github.com',"
                             "'example/repo',42,'https://github.com/example/repo/pull/42','now')", (self.task,))
            after = report.task_report(self.con, self.task)
        self.assertEqual(before["measured"]["attributed_responses"], [])
        self.assertEqual(after["legacy_checkout_observations"],
                         {"basis": "first_native_import", "responses": observed})
        self.assertIsNone(after["total_cost"]["usd"])
        self.assertEqual(after["measured"], before["measured"])
        self.assertIn("purpose_run_unavailable", after["native_purpose"]["coverage_gaps"])
        self.assign(self.task, "02")
        trace = report.timeline(self.con, self.task)
        self.assertEqual({e["native_id"] for e in trace["events"]},
                         {"call-mini-spawn-01", "task_complete:turn-mini-bbb"})
        self.assertNotIn("native_response_count", trace)

    def test_legacy_navigation_preserves_captured_turn_in_every_view(self):
        self.con.execute("DELETE FROM t3_purpose_runs")
        other = "example/repo#12"
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", (other,))
        self.assign(self.task, "01")
        first = ["codex:resp-mini-001", "codex:resp-mini-002"]
        self.con.execute("INSERT INTO session_assignments VALUES(?,?,'t3',0)", (self.session, self.task))
        mine = report.task_report(self.con, self.task)
        self.assertEqual(mine["measured"]["attributed_responses"], first)
        self.assertEqual(mine["measured"]["unplaced_responses"], ["codex:resp-mini-003"])
        self.assertEqual(mine["total_cost"]["responses"], 2)
        self.assertEqual(report.timeline(self.con, self.task)["returned"], 2)

        self.con.execute("INSERT INTO session_assignments VALUES(?,?,'t3:thread',0)", (self.session, other))
        linked = report.task_report(self.con, other)
        self.assertEqual(linked["measured"]["other_task_responses"], first)
        self.assertEqual(linked["total_cost"]["responses"], 0)
        self.assertFalse(report.timeline(self.con, other)["events"])
        self.assign(other, "02")
        for task, owned, elsewhere, turn in ((self.task, first, ["codex:resp-mini-003"], "aaa"),
                                              (other, ["codex:resp-mini-003"], first, "bbb")):
            rep = report.task_report(self.con, task)
            self.assertEqual(rep["measured"]["attributed_responses"], owned)
            self.assertEqual(rep["measured"]["other_task_responses"], elsewhere)
            self.assertFalse(rep["coverage"]["conflicting_sessions"])
            self.assertTrue(rep["reconciles"])
            self.assertEqual({e["turn_id"] for e in report.timeline(self.con, task)["events"]},
                             {"codex:turn-mini-" + turn})

    def test_legacy_links_do_not_join_or_hide_an_explicit_shared_pool(self):
        self.con.execute("DELETE FROM t3_purpose_runs")
        partner, observer = "example/repo#12", "example/repo#13"
        for task in (partner, observer):
            self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", (task,))
        for task in (self.task, partner):
            self.assign(task, "01")
        self.con.execute("UPDATE assignments SET shared=1")
        self.con.execute("UPDATE responses SET cost_usd=CASE WHEN turn_id='codex:turn-mini-aaa' THEN 1 ELSE 100 END")
        for task in (self.task, partner, observer):
            self.con.execute("INSERT INTO session_assignments VALUES(?,?,'t3:planner',0)", (self.session, task))
        for task, peer in ((self.task, partner), (partner, self.task)):
            rep = report.task_report(self.con, task)
            self.assertEqual(rep["shared_joint"]["responses"], 2)
            self.assertEqual(rep["total_cost"]["shared_responses"], 2)
            self.assertEqual(rep["total_cost"]["shared_with_tasks"], [peer])
            self.assertEqual(rep["native_cost_billed_shared"]["total_usd"], 2)
            self.assertEqual(report.timeline(self.con, task)["returned"], 2)
        rep = report.task_report(self.con, observer)
        self.assertEqual(rep["total_cost"]["responses"], 0)
        self.assertEqual(rep["measured"]["other_task_responses"], ["codex:resp-mini-001", "codex:resp-mini-002"])
        self.assertFalse(report.timeline(self.con, observer)["events"])
        self.assertEqual(rep["native_cost_billed_shared"]["total_usd"], 0)

    def test_linked_session_reusing_a_captured_turn_id_stays_unplaced(self):
        self.con.execute("DELETE FROM t3_purpose_runs")
        self.assign(self.task, "01")
        key = "codex:other-session"
        self.con.execute("INSERT INTO session_assignments VALUES(?,?,'t3',0)", (key, self.task))
        self.con.execute("INSERT INTO responses(response_id,source_id,session_key,turn_id)"
                         " SELECT 'foreign-response',source_id,?,turn_id FROM responses WHERE response_id=?",
                         (key, "codex:resp-mini-001"))
        self.con.execute("INSERT INTO events(session_key,turn_id,native_id,family)"
                         " VALUES(?,'codex:turn-mini-aaa','foreign-event','tool_call')", (key,))
        rep = report.task_report(self.con, self.task)
        self.assertEqual(rep["measured"]["unplaced_responses"], ["foreign-response"])
        self.assertEqual(rep["total_cost"]["responses"], 2)
        self.assertFalse(report.timeline(self.con, self.task, session_key=key)["events"])

    def project_input(self, session, native_id, validation):
        self.con.execute("INSERT INTO t3_purpose_sessions VALUES('a',?)", (session,))
        self.con.execute("INSERT INTO t3_purpose_inputs(run_id,session_key,native_id,kind,validation)"
                         " VALUES('a',?,?,'message',?)", (session, native_id, validation))

    def test_disputed_claude_parent_never_refunds_usage_to_router(self):
        path = Path(self.tmp.name, "original.jsonl")
        response = claude_response("reply", "a", "mid")
        response["message"]["content"] = [{"type": "text", "text": "answer"},
                                          {"type": "tool_use", "id": "call", "name": "Read"}]
        write_records(path, [claude_user("a"), response])
        claude.import_claude_file(self.con, str(path))
        self.project_input("claude:s", "a", "claude_prompt_echo")
        self.assertIn("claude:mid", report.task_report(self.con, self.task)["measured"]["attributed_responses"])
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES('router-job',0)")
        self.con.execute("INSERT INTO session_assignments VALUES('claude:s','router-job','router:job',0)")
        copy = Path(self.tmp.name, "copy.jsonl")
        response["parentUuid"] = "missing"
        write_records(copy, [response])
        claude.import_claude_file(self.con, str(copy))
        for owner in ("router-job", self.task):
            rep = report.task_report(self.con, owner)
            self.assertIn("claude:mid", rep["native_purpose"]["conflicting_responses"])
            self.assertIn("claude:mid", rep["measured"]["unplaced_responses"])
            self.assertFalse(report.timeline(self.con, owner, session_key="claude:s")["events"])
        self.con.execute("DELETE FROM session_assignments WHERE task_id='router-job'")
        response["parentUuid"] = "a"
        write_records(copy, [response])
        claude.import_claude_file(self.con, str(copy), full=True)
        self.assertIn("claude:mid", report.task_report(self.con, self.task)["measured"]["attributed_responses"])

    def test_claude_calls_and_results_follow_later_response_to_earlier_prompt(self):
        path = Path(self.tmp.name, "s.jsonl")
        self.con.execute("DELETE FROM t3_purpose_sessions")
        rows = [claude_user("b"), claude_user("a")]
        for prompt in ("a", "b"):
            answer = claude_response("reply-" + prompt, prompt, "msg-" + prompt)
            call = prompt + "-call"
            answer["message"]["content"] = [{"type": "tool_use", "name": "Read", "id": call,
                                             "input": {"file_path": "/fixture.txt"}}]
            rows.extend([answer, claude_user("result-" + prompt, "reply-" + prompt,
                         [{"type": "tool_result", "tool_use_id": call, "content": "ok"}])])
        write_records(path, rows)
        claude.import_claude_file(self.con, str(path))
        self.project_input("claude:s", "a", "claude_prompt_echo")
        self.con.execute("INSERT INTO assignments(user_message_id,task_id,created_at) VALUES('claude:a',?,0)",
                         (self.task,))
        for task, prompt in ((self.task, "a"), ("capture-b", "b")):
            if prompt == "b":
                self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", (task,))
                self.con.execute("INSERT INTO assignments(user_message_id,task_id,created_at)"
                                 " VALUES('claude:b',?,0)", (task,))
            task_report = report.task_report(self.con, task)
            self.assertEqual(sum(p["activity"].get("tool_calls", 0) for p in task_report["phases"]), 1)
            self.assertEqual(task_report["activity_session_context"]["tool_calls"], 1)
            measured = task_report["measured"]
            self.assertIn("claude:msg-" + prompt, measured["attributed_responses"])
            self.assertIn("claude:msg-b" if prompt == "a" else "claude:msg-a",
                          measured["unplaced_responses" if prompt == "a" else "other_task_responses"])
            for turn in (None, "claude:" + prompt):
                trace = report.timeline(self.con, task, turn_id=turn, session_key="claude:s")
                self.assertEqual({(e["family"], e["native_id"]) for e in trace["events"]},
                                 {("tool_call", prompt + "-call"), ("tool_result", prompt + "-call"),
                                  ("read", prompt + "-call:read")})
            wrong_turn = "claude:b" if prompt == "a" else "claude:a"
            self.assertFalse(report.timeline(self.con, task, turn_id=wrong_turn, session_key="claude:s")["events"])

    def test_router_session_survives_navigation_and_separate_canonical_run(self):
        self.con.execute("DELETE FROM t3_purpose_sessions")
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES('navigator',0)")
        self.con.executemany("INSERT INTO session_assignments VALUES(?,?,?,0)",
                             [(self.session, self.task, "router:job"), (self.session, "navigator", "t3")])
        path = Path(self.tmp.name, "s.jsonl")
        write_records(path, [claude_user("a"), claude_response("reply", "a", "mid")])
        claude.import_claude_file(self.con, str(path))
        self.project_input("claude:s", "a", "claude_prompt_echo")
        rep = report.task_report(self.con, self.task)
        expected = ["codex:resp-mini-001", "codex:resp-mini-002", "codex:resp-mini-003"]
        self.assertEqual(rep["measured"]["attributed_responses"], ["claude:mid", *expected])
        self.assertEqual(rep["total_cost"]["responses"], 4)
        self.assertEqual(report.task_report(self.con, "navigator")["measured"]["other_task_responses"], expected)
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES('partner',0)")
        self.con.execute("INSERT INTO session_assignments VALUES(?,'partner','router:job',0)", (self.session,))
        joint = report.task_report(self.con, self.task)
        self.assertEqual(joint["total_cost"]["shared_responses"], 3)
        self.assertEqual(joint["total_cost"]["shared_with_tasks"], ["partner"])

    def test_opencode_parts_follow_message_for_native_and_capture(self):
        path = Path(self.tmp.name, "oc.db")
        build_native(str(path))
        with closing(sqlite3.connect(path)) as source, source:
            source.execute("UPDATE message SET data=json_set(data,'$.parentID','msg_u1') WHERE id='msg_a1'")
            source.execute("INSERT INTO part SELECT 'text1',message_id,session_id,time_created,time_updated,"
                           "? FROM part WHERE id='p_read1'", (json.dumps({"type": "text", "text": "answer"}),))
        opencode.sync(self.con, source=str(path))
        key = "opencode:ses_parent"
        self.project_input(key, "msg_u1", "opencode_admitted_message")
        for native in (True, False):
            if not native:
                self.con.execute("DELETE FROM t3_purpose_sessions WHERE session_key=?", (key,))
                self.con.execute("INSERT INTO assignments(user_message_id,task_id,created_at)"
                                 " VALUES('opencode:msg_u1',?,0)", (self.task,))
            self.assertIn("opencode:msg_a1", report.task_report(self.con, self.task)["measured"]["attributed_responses"])
            for turn in (None, "opencode:msg_u1"):
                trace = report.timeline(self.con, self.task, turn_id=turn, session_key=key)
                self.assertEqual(trace["native_response_count"], int(native))
                events = {(e["family"], e["native_id"]) for e in trace["events"]}
                self.assertTrue({("read", "call_read1"), ("tool_call", "call_read1"),
                                 ("tool_result", "call_read1"), ("assistant_message", "text1"),
                                 ("file_change", "p_patch1")} <= events, events)
                self.assertNotIn(("tool_call", "call_run1"), events)
            self.assertFalse(report.timeline(self.con, self.task, turn_id="opencode:unknown", session_key=key)["events"])

    def test_event_edge_cannot_reuse_an_owned_response_from_another_session(self):
        for sid in ("s", "other"):
            answer = claude_response("reply", "a", "same-message-id")
            answer["message"]["content"] = [{"type": "tool_use", "id": sid + "-call", "name": "Read"}]
            records = [dict(row, sessionId=sid) for row in (claude_user("a"), answer)]
            path = Path(self.tmp.name, sid + ".jsonl")
            write_records(path, records)
            claude.import_claude_file(self.con, str(path))
        self.project_input("claude:s", "a", "claude_prompt_echo")
        self.con.execute("INSERT INTO t3_purpose_sessions VALUES('a','claude:other')")
        trace = report.timeline(self.con, self.task, turn_id="claude:a", family="tool_call")
        self.assertEqual([e["native_id"] for e in trace["events"]], ["s-call"])
