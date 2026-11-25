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
        self.assertIn("background_inventory_unavailable", rep["native_purpose"]["coverage_gaps"])
        self.con.execute("DELETE FROM responses WHERE response_id='codex:resp-mini-002'")
        late = report.task_report(self.con, self.task)
        self.assertEqual(late["native_purpose"]["turn_checks"][0]["status"], "counter_mismatch")
        self.assertNotEqual(late["snapshot_id"], rep["snapshot_id"])

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
