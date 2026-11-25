"""Regression fixtures for task ownership across partial capture and copied stores."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from agent_observer import report
from agent_observer.adapters import claude, opencode, t3_purpose
from tests.helpers import LedgerCase
from tests.test_cli import run
from tests.test_native_response import claude_response, claude_user, write_records
from tests.test_opencode import build_native


class EvidenceCoverageTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.con.executemany("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", [("A",), ("B",)])

    def assign(self, task, message):
        self.con.execute("INSERT INTO assignments(user_message_id,task_id,created_at) VALUES(?,?,0)",
                         (message, task))

    def draft(self):
        self.con.commit()
        result = run(self.db_path, "publish", "--task", "A", "--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def import_claude(self, legacy=False):
        rows = [claude_user("b"), claude_user("a")]
        for prompt in ("a", "b"):
            response = claude_response("reply-" + prompt, prompt, "msg-" + prompt)
            response["message"].update(model="claude-haiku-4-5",
                usage=dict(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0),
                content=[{"type": "tool_use", "id": prompt + "-call", "name": "Read"}])
            if legacy:
                del response["parentUuid"]
                rows = [claude_user("a"), response]
                break
            rows.append(response)
        path = Path(self.tmp.name, "claude.jsonl")
        write_records(path, rows)
        claude.import_claude_file(self.con, str(path))
        self.assign("A", "claude:a")
        return path

    def test_missing_literal_capture_survives_partial_session(self):
        self.import_claude()
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["missing_assignments"], ["claude:b"])
        self.assertEqual(rep["measured"]["unassigned_responses"], ["claude:msg-b"])
        self.assertIsNone(rep["total_cost"]["usd"])
        self.assertGreater(rep["total_cost"]["observed_subtotal_usd"], 0)
        self.assertFalse(rep["complete"])
        draft = self.draft()
        self.assertIn("Evidence complete: no", draft["body"])
        self.assertIn("Estimated cost unknown", draft["body"])
        self.assertEqual(draft["summary"]["total_cost"], rep["total_cost"])
        self.assertEqual([e["native_id"] for e in report.timeline(self.con, "A")["events"]], ["a-call"])
        self.assign("B", "claude:b")
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["measured"]["other_task_responses"], ["claude:msg-b"])
        self.assertEqual(rep["missing_assignments"], [])
        self.assertTrue(rep["complete"])
        self.assertGreater(rep["total_cost"]["usd"], 0)

    def test_legacy_explicit_capture_with_and_without_source(self):
        source = self.import_claude(legacy=True)
        for old in (False, True):
            with self.subTest(ledger_only=old):
                if old:
                    self.con.execute("DROP TABLE claude_response_ancestry")
                    self.con.execute("DROP TABLE native_response_events")
                    self.con.execute("UPDATE sources SET claude_usage_version=5")
                    source.unlink()
                    self.con.execute("PRAGMA query_only=ON")
                rep = report.task_report(self.con, "A")
                self.assertEqual(rep["measured"]["attributed_responses"], ["claude:msg-a"])
                self.assertEqual(rep["total_cost"]["responses"], 1)
                self.assertEqual([e["native_id"] for e in report.timeline(
                    self.con, "A", turn_id="claude:a")["events"]], ["a-call"])

    def project_input(self, session, message, validation):
        with closing(sqlite3.connect(":memory:")) as source:
            t3_purpose.refresh(self.con, source, set())
        self.con.execute("INSERT INTO t3_purpose_runs(run_id,thread_id,task_id,scope)"
                         " VALUES('run','thread','A','single')")
        self.con.execute("INSERT INTO t3_purpose_sessions VALUES('run',?)", (session,))
        self.con.execute("INSERT INTO t3_purpose_inputs(run_id,session_key,native_id,kind,validation)"
                         " VALUES('run',?,?,'message',?)", (session, message, validation))

    def test_native_input_without_response_parent_cannot_use_display_turn(self):
        self.import_claude(legacy=True)
        self.project_input("claude:s", "a", "claude_prompt_echo")
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["measured"]["unplaced_responses"], ["claude:msg-a"])
        self.assertEqual(rep["native_purpose"]["membership"], {})
        self.assertFalse(report.timeline(self.con, "A")["events"])

    def opencode_store(self, name, parent, cost, output, independent=False):
        path = Path(self.tmp.name, name + ".db")
        build_native(str(path))
        with closing(sqlite3.connect(path)) as source, source:
            source.execute("UPDATE message SET data=json_set(data,'$.parentID',?,'$.cost',?,"
                           "'$.tokens.output',?) WHERE id='msg_a1'", (parent, cost, output))
            if independent:
                source.execute("INSERT INTO message SELECT 'msg_unique',session_id,time_created,time_updated,"
                               "json_set(data,'$.cost',0.10,'$.tokens.output',3) FROM message WHERE id='msg_a1'")
        return str(path)

    def check_opencode_copies(self, reversed_order):
        first = self.opencode_store("original", "msg_u1", 0.20, 50, independent=True)
        second = self.opencode_store("copy", None, 7.25, 500)
        paths = [second, first] if reversed_order else [first, second]
        for path in paths:
            self.assertFalse(opencode.sync(self.con, source=path)["failed"])
        self.assign("A", "opencode:msg_u1")
        for canonical in (False, True):
            if canonical:
                self.project_input("opencode:ses_parent", "msg_u1", "opencode_admitted_message")
            rep = report.task_report(self.con, "A")
            self.assertEqual(rep["measured"]["attributed_responses"], ["opencode:msg_unique"])
            self.assertEqual(rep["measured"]["unplaced_responses"], ["opencode:msg_a1"])
            self.assertEqual(rep["native_cost"]["known_subtotal_usd"], 0.10)
            self.assertEqual(rep["attributed"]["output_tokens"], 3)
            trace = report.timeline(self.con, "A")
            self.assertFalse(trace["events"])
            if canonical:
                self.assertEqual(set(trace["native_responses"]), {"opencode:msg_unique"})
            draft = self.draft()
            self.assertEqual(draft["summary"]["usage"]["output_tokens"], 3)
            self.assertEqual(draft["summary"]["native_cost"]["known_subtotal_usd"], 0.10)
        with closing(sqlite3.connect(second)) as source, source:
            source.execute("UPDATE message SET time_updated=time_updated+1,"
                           "data=json_set(data,'$.parentID','msg_u1','$.cost',0.20,'$.tokens.output',50)"
                           " WHERE id='msg_a1'")
            source.execute("UPDATE session SET time_updated=time_updated+1 WHERE id='ses_parent'")
        opencode.sync(self.con, source=second)
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["total_cost"]["responses"], 2)
        self.assertEqual(rep["attributed"]["output_tokens"], 53)
        self.assertAlmostEqual(rep["native_cost"]["known_subtotal_usd"], 0.30)
        self.assertIn("call_read1", [e["native_id"] for e in report.timeline(self.con, "A")["events"]])

    def test_opencode_proved_then_parentless_copy(self):
        self.check_opencode_copies(False)

    def test_opencode_parentless_then_proved_copy(self):
        self.check_opencode_copies(True)

    def test_unresolved_literal_parent_cannot_use_legacy_capture(self):
        path = self.import_claude(legacy=True)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        records[1]["parentUuid"] = "unseen"
        write_records(path, records)
        claude.import_claude_file(self.con, str(path), full=True)
        self.assertEqual(report.task_report(self.con, "A")["attributed"]["responses"], 0)
        self.assertFalse(report.timeline(self.con, "A")["events"])

    def test_copies_must_agree_on_both_parent_and_measurement(self):
        first = self.opencode_store("one", "msg_u1", 0.20, 50)
        second = self.opencode_store("two", None, 0.20, 50)
        opencode.sync(self.con, source=first)
        self.assign("A", "opencode:msg_u1")
        for parent, cost, expected in ((None, 0.20, 0), ("msg_u1", 7.25, 0), ("msg_u1", 0.20, 1)):
            with self.subTest(parent=parent, cost=cost):
                with closing(sqlite3.connect(second)) as source, source:
                    source.execute("UPDATE message SET data=json_set(data,'$.parentID',?,'$.cost',?) "
                                   "WHERE id='msg_a1'", (parent, cost))
                opencode.sync(self.con, source=second, full=True)
                rep = report.task_report(self.con, "A")
                self.assertEqual(rep["attributed"]["responses"], expected)
                self.assertEqual(rep["total_cost"]["responses"], expected)
                self.assertEqual(bool(report.timeline(self.con, "A")["events"]), bool(expected))

    def test_read_only_old_opencode_ledger_with_overwritten_payload(self):
        first = self.opencode_store("older", "msg_u1", 0.20, 50, independent=True)
        second = self.opencode_store("newer", None, 7.25, 500)
        for path in (first, second):
            opencode.sync(self.con, source=path)
        self.assign("A", "opencode:msg_u1")
        # Before source observations, both edges came from the first store
        # and msg_a1's one surviving payload came from the second store.
        key = "opencode:ses_parent"
        self.con.execute("INSERT INTO native_response_inputs SELECT session_key,response_id,parent,source_id "
                         "FROM opencode_response_observations WHERE parent IS NOT NULL")
        self.con.execute("UPDATE responses SET source_id=(SELECT id FROM sources WHERE path=?),"
                         "output_tokens=500,total_tokens=516,cost_usd=7.25 WHERE response_id='opencode:msg_a1'",
                         (second + "#ses_parent",))
        self.con.execute("DROP TABLE opencode_response_observations")
        self.con.execute("UPDATE sources SET sha256='old'")
        for path in (first, second):
            Path(path).unlink()
        self.con.execute("PRAGMA query_only=ON")
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["measured"]["attributed_responses"], ["opencode:msg_unique"])
        self.assertEqual(rep["native_cost"]["known_subtotal_usd"], 0.10)
        self.assertFalse(report.timeline(self.con, "A", session_key=key)["events"])
