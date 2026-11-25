"""Exercise shared-planner reports with native usage and independent purpose rows."""

from contextlib import closing
import json
import sqlite3

from agent_observer import publish, report
from agent_observer.adapters import t3_purpose
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
                         "status,active_attempt_id) VALUES('a','planner',?,'single','issue','completed','aa')",
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
        self.assertIn("background_inventory_unavailable", rep["native_purpose"]["coverage_gaps"])
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
        from pathlib import Path
        from agent_observer.adapters import claude
        from tests.test_native_response import claude_response, claude_user, write_records

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
            self.assertEqual([e["native_id"] for e in selected["events"]],
                             ["answer-a:0" if task == self.task else "answer-b:0"])

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
