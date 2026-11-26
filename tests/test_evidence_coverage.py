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
            # These task scopes exercise only the two copies of the parent.
            source.execute("DELETE FROM session WHERE parent_id IS NOT NULL")
            source.execute("DELETE FROM part WHERE message_id IN ('msg_a_unf','msg_a_err')")
            source.execute("DELETE FROM message WHERE id IN ('msg_a_unf','msg_a_err')")
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

    def test_malformed_refresh_preserves_competing_parent(self):
        first = self.opencode_store("one", "msg_u1", 0.20, 50, independent=True)
        second = self.opencode_store("two", "other", 0.20, 50)
        with closing(sqlite3.connect(second)) as source, source:
            source.execute("INSERT INTO message SELECT 'other',session_id,time_created,time_updated,data "
                           "FROM message WHERE id='msg_u1'")
        for path in (first, second):
            opencode.sync(self.con, source=path)
        self.assign("A", "opencode:msg_u1")
        self.assertEqual(report.task_report(self.con, "A")["attributed"]["responses"], 1)
        with closing(sqlite3.connect(second)) as source, source:
            source.execute("UPDATE message SET data=json_set(data,'$.tokens.output','bad') WHERE id='msg_a1'")
            source.execute("UPDATE session SET time_updated=time_updated+1 WHERE id='ses_parent'")
        refresh = opencode.sync(self.con, source=second)
        self.assertEqual(refresh["failed"], [])
        self.assertEqual(refresh["malformed"], 1)
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["measured"]["attributed_responses"], ["opencode:msg_unique"])
        self.assertEqual(rep["measured"]["unplaced_responses"], ["opencode:msg_a1"])
        self.assertFalse(report.timeline(self.con, "A")["events"])
        draft = self.draft()
        self.assertIn("Evidence complete: no", draft["body"])
        self.assertEqual(draft["summary"]["native_cost"]["known_subtotal_usd"], 0.10)
        with closing(sqlite3.connect(second)) as source, source:
            source.execute("UPDATE message SET data=json_set(data,'$.tokens.output',50,'$.parentID','msg_u1') "
                           "WHERE id='msg_a1'")
            source.execute("UPDATE session SET time_updated=time_updated+1 WHERE id='ses_parent'")
        opencode.sync(self.con, source=second)
        self.assertAlmostEqual(report.task_report(self.con, "A")["native_cost"]["known_subtotal_usd"], 0.30)

    def check_unusable_claude_ancestry(self, field, value):
        path = self.import_claude()
        self.assign("B", "claude:b")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        response = rows[-1]
        self.assertEqual(response["parentUuid"], "b")
        if value is None:
            del response[field]
        else:
            response[field] = value
        write_records(path, rows)
        claude.import_claude_file(self.con, str(path), full=True)
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["measured"]["attributed_responses"], ["claude:msg-a"])
        self.assertEqual(rep["measured"]["unplaced_responses"], ["claude:msg-b"])
        self.assertFalse(report.task_report(self.con, "B")["measured"]["attributed_responses"])
        self.assertEqual([e["native_id"] for e in report.timeline(self.con, "A")["events"]], ["a-call"])
        self.assertIn("Evidence complete: no", self.draft()["body"])

    def test_claude_parent_without_node_cannot_fall_back_to_display_turn(self):
        self.check_unusable_claude_ancestry("uuid", None)

    def test_claude_malformed_parent_cannot_fall_back_to_display_turn(self):
        self.check_unusable_claude_ancestry("parentUuid", "bad parent")

    def test_unreadable_opencode_message_still_disputes_its_copy(self):
        first = self.opencode_store("one", "msg_u1", 0.20, 50, independent=True)
        second = self.opencode_store("two", "other", 0.20, 50)
        with closing(sqlite3.connect(second)) as source, source:
            source.execute("INSERT INTO message SELECT 'other',session_id,time_created,time_updated,data "
                           "FROM message WHERE id='msg_u1'")
        for path in (first, second):
            opencode.sync(self.con, source=path)
        self.assign("A", "opencode:msg_u1")
        self.assertEqual(report.task_report(self.con, "A")["attributed"]["responses"], 1)
        with closing(sqlite3.connect(second)) as source, source:
            source.execute("UPDATE message SET data='{' WHERE id='msg_a1'")
            source.execute("UPDATE session SET time_updated=time_updated+1 WHERE id='ses_parent'")
        self.assertEqual(opencode.sync(self.con, source=second)["malformed"], 1)
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["measured"]["attributed_responses"], ["opencode:msg_unique"])
        self.assertFalse(report.timeline(self.con, "A")["events"])
        self.assertEqual(rep["native_cost"]["known_subtotal_usd"], 0.10)
        self.assertIn("Evidence complete: no", self.draft()["body"])
        with closing(sqlite3.connect(first)) as original:
            data = json.loads(original.execute("SELECT data FROM message WHERE id='msg_a1'").fetchone()[0])
        del data["time"]["completed"]
        with closing(sqlite3.connect(second)) as source, source:
            source.execute("UPDATE message SET data=? WHERE id='msg_a1'", (json.dumps(data),))
        opencode.sync(self.con, source=second, full=True)
        self.assertEqual(report.task_report(self.con, "A")["measured"]["attributed_responses"], ["opencode:msg_unique"])

    def test_rejected_claude_counters_do_not_resolve_conflicting_parents(self):
        path = self.import_claude()
        self.assign("B", "claude:b")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        response = rows[-1]
        response["parentUuid"] = "a"
        copy = path.with_name("copy.jsonl")
        write_records(copy, rows)
        claude.import_claude_file(self.con, str(copy))
        self.assertFalse(report.task_report(self.con, "B")["attributed"]["responses"])
        response["message"]["usage"]["output_tokens"] = "bad"
        write_records(copy, rows)
        claude.import_claude_file(self.con, str(copy), full=True)
        self.assertFalse(report.task_report(self.con, "B")["attributed"]["responses"])
        self.assertFalse(report.timeline(self.con, "B")["events"])
        self.assertIn("Evidence complete: no", self.draft()["body"])
        response["message"]["usage"]["output_tokens"] = 5
        response["parentUuid"] = "b"
        write_records(copy, rows)
        claude.import_claude_file(self.con, str(copy), full=True)
        self.assertEqual(report.task_report(self.con, "B")["measured"]["attributed_responses"], ["claude:msg-b"])

    def test_missing_usage_row_keeps_priced_subtotal_separate(self):
        self.import_claude(legacy=True)
        only = self.opencode_store("damaged", "msg_u1", 0.20, "bad")
        opencode.sync(self.con, source=only)
        self.assign("A", "opencode:msg_u1")
        rep = report.task_report(self.con, "A")
        self.assertFalse(rep["complete"])
        self.assertIsNone(rep["total_cost"]["usd"])
        self.assertGreater(rep["total_cost"]["observed_subtotal_usd"], 0)
        self.assertIn("Estimated cost unknown", self.draft()["body"])

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

    def check_malformed_claude(self, mutate):
        path = self.import_claude()
        self.assign("B", "claude:b")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        mutate(rows[-1])
        write_records(path, rows)
        claude.import_claude_file(self.con, str(path), full=True)
        path.unlink()
        for canonical in (False, True):
            if canonical:
                self.project_input("claude:s", "a", "claude_prompt_echo")
                self.con.execute("PRAGMA query_only=ON")
            for task in ("A", "B"):
                rep = report.task_report(self.con, task)
                self.assertEqual(rep["measured"]["unplaced_responses"], ["claude:msg-b"])
                self.assertNotIn("b-call", [e["native_id"] for e in report.timeline(self.con, task)["events"]])
            draft = self.draft()
            self.assertEqual(draft["summary"]["total_cost"]["responses"], 1)
            self.assertIn("Evidence complete: no", draft["body"])

    def test_claude_response_with_parent_but_no_block_uuid(self):
        self.check_malformed_claude(lambda row: row.pop("uuid"))

    def test_claude_response_with_malformed_parent(self):
        self.check_malformed_claude(lambda row: row.update(parentUuid={"invalid": True}))

    def test_claude_response_with_missing_link_in_modern_source(self):
        self.check_malformed_claude(lambda row: row.pop("parentUuid"))

    def check_malformed_opencode(self, bad_first):
        good = self.opencode_store("good", "msg_u1", 0.20, 50, independent=True)
        bad = self.opencode_store("bad", "msg_other", 0.20, 50)
        with closing(sqlite3.connect(bad)) as source, source:
            source.execute("INSERT INTO message SELECT 'msg_other',session_id,time_created,time_updated,data "
                           "FROM message WHERE id='msg_u1'")
        if not bad_first:
            for path in (good, bad):
                opencode.sync(self.con, source=path)
            self.assign("A", "opencode:msg_u1")
            self.assertEqual(report.task_report(self.con, "A")["attributed"]["output_tokens"], 3)
        with closing(sqlite3.connect(bad)) as source, source:
            source.execute("UPDATE message SET data=json_set(data,'$.tokens.output','invalid') WHERE id='msg_a1'")
            source.execute("UPDATE session SET time_updated=time_updated+1")
        self.assertGreater(opencode.sync(self.con, source=bad)["malformed"], 0)
        opencode.sync(self.con, source=good)
        if bad_first:
            self.assign("A", "opencode:msg_u1")
        for canonical in (False, True):
            if canonical:
                self.project_input("opencode:ses_parent", "msg_u1", "opencode_admitted_message")
                for path in (bad, good):
                    opencode.sync(self.con, source=path, full=True)
                self.con.execute("PRAGMA query_only=ON")
            rep = report.task_report(self.con, "A")
            self.assertEqual(rep["measured"]["unplaced_responses"], ["opencode:msg_a1"])
            self.assertEqual(rep["attributed"]["output_tokens"], 3)
            self.assertIsNone(rep["native_cost"]["total_usd"])
            self.assertEqual(rep["native_cost"]["known_subtotal_usd"], 0.10)
            self.assertFalse(report.timeline(self.con, "A")["events"])
            draft = self.draft()
            self.assertIn("Evidence complete: no", draft["body"])
            self.assertEqual(draft["summary"]["native_cost"], rep["native_cost"])

    def test_malformed_opencode_refresh_preserves_dispute(self):
        self.check_malformed_opencode(False)

    def test_malformed_opencode_first_preserves_dispute(self):
        self.check_malformed_opencode(True)

    def test_malformed_only_opencode_input_keeps_its_gap_local(self):
        path = self.opencode_store("single", "msg_u1", .20, 50, independent=True)
        with closing(sqlite3.connect(path)) as source, source:
            source.execute("INSERT INTO message SELECT 'other',session_id,time_created,time_updated,data "
                           "FROM message WHERE id='msg_u1'")
            source.execute("INSERT INTO part SELECT 'other-text','other',session_id,time_created,time_updated,data "
                           "FROM part WHERE message_id='msg_u1' LIMIT 1")
            source.execute("UPDATE message SET data=json_set(data,'$.tokens.output','bad','$.parentID','other') "
                           "WHERE id='msg_a1'")
        opencode.sync(self.con, source=path)
        self.assign("A", "opencode:msg_u1")
        self.assign("B", "opencode:other")
        a, b = [report.task_report(self.con, task) for task in ("A", "B")]
        self.assertEqual(a["native_cost"]["total_usd"], 0.10)
        self.assertEqual(a["coverage"]["unavailable_response_usage"], [])
        self.assertTrue(a["complete"])
        self.assertEqual(b["coverage"]["unavailable_response_usage"], ["opencode:msg_a1"])
        self.assertIsNone(b["native_cost"]["total_usd"])
        self.assertFalse(b["complete"])

    def test_claude_invalid_copy_and_correction_in_both_orders(self):
        self.import_claude()
        self.assign("B", "claude:b")
        path = Path(self.tmp.name, "claude.jsonl")
        other = Path(self.tmp.name, "copy.jsonl")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        bad = dict(rows[-1], uuid=None)
        for order in ((other, path), (path, other)):
            write_records(other, [bad])
            for source in order:
                claude.import_claude_file(self.con, str(source), full=True)
            rep = report.task_report(self.con, "B")
            self.assertIn("claude:msg-b", rep["measured"]["unplaced_responses"])
            self.assertFalse(report.timeline(self.con, "B")["events"])
            write_records(other, [rows[-1]])
            claude.import_claude_file(self.con, str(other), full=True)
            self.assertEqual(report.task_report(self.con, "B")["measured"]["attributed_responses"], ["claude:msg-b"])

    def test_unreadable_opencode_identity_cannot_release_previous_dispute(self):
        good = self.opencode_store("good", "msg_u1", 0.20, 50, independent=True)
        bad = self.opencode_store("bad", None, 7.25, 500)
        for source in (good, bad):
            opencode.sync(self.con, source=source)
        self.assign("A", "opencode:msg_u1")
        for sql in ("UPDATE message SET id=NULL WHERE id='msg_a1'",
                    "UPDATE message SET id='msg_a1',data='broken' WHERE id IS NULL"):
            with closing(sqlite3.connect(bad)) as source, source:
                source.execute(sql)
            for _ in range(2):
                opencode.sync(self.con, source=bad, full=True)
                rep = report.task_report(self.con, "A")
                self.assertEqual(rep["measured"]["attributed_responses"], ["opencode:msg_unique"])
                self.assertIsNone(rep["native_cost"]["total_usd"])
                self.assertEqual(rep["native_cost"]["known_subtotal_usd"], 0.10)
        with closing(sqlite3.connect(good)) as source:
            valid = source.execute("SELECT data FROM message WHERE id='msg_a1'").fetchone()[0]
        with closing(sqlite3.connect(bad)) as source, source:
            source.execute("UPDATE message SET data=? WHERE id='msg_a1'", (valid,))
        opencode.sync(self.con, source=bad, full=True)
        rep = report.task_report(self.con, "A")
        self.assertTrue(rep["complete"])
        self.assertAlmostEqual(rep["native_cost"]["total_usd"], 0.30)
        self.assertEqual(rep["attributed"]["output_tokens"], 53)

    def test_unmeasured_claude_event_with_a_parent_cannot_inherit_display_turn(self):
        path = self.import_claude(legacy=True)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[1]["parentUuid"] = "a"
        foreign = claude_response("foreign", "b", "ignored")
        del foreign["message"]["id"]
        foreign["message"]["content"] = [{"type": "tool_use", "id": "foreign-call", "name": "Read"}]
        write_records(path, [claude_user("b"), rows[0], rows[1], foreign])
        claude.import_claude_file(self.con, str(path), full=True)
        self.assertNotIn("foreign-call", [e["native_id"] for e in report.timeline(self.con, "A")["events"]])

    def test_erasing_last_claude_parent_does_not_turn_source_into_legacy(self):
        path = self.import_claude(legacy=True)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows.insert(0, claude_user("b"))
        rows[-1]["parentUuid"] = "b"
        write_records(path, rows)
        claude.import_claude_file(self.con, str(path), full=True)
        self.assign("B", "claude:b")
        self.assertEqual(report.task_report(self.con, "B")["attributed"]["responses"], 1)
        del rows[-1]["parentUuid"]
        write_records(path, rows)
        for _ in range(2):
            claude.import_claude_file(self.con, str(path), full=True)
            self.assertEqual(report.task_report(self.con, "A")["measured"]["unplaced_responses"], ["claude:msg-a"])
            self.assertFalse(report.timeline(self.con, "A")["events"])

    def test_claude_disagreeing_final_measurements_in_both_orders(self):
        path = self.import_claude()
        self.assign("B", "claude:b")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        other = path.with_name("final-copy.jsonl")
        for first, second, rid in ((path, other, "high-first"), (other, path, "low-first")):
            with self.subTest(rid=rid):
                response = dict(rows[-1], uuid=rid, message=dict(rows[-1]["message"], id=rid))
                for loc, count in ((first, 90000 if rid == "high-first" else 5000),
                                   (second, 5000 if rid == "high-first" else 90000)):
                    response["message"]["usage"]["output_tokens"] = count
                    write_records(loc, [*rows[:2], response])
                    claude.import_claude_file(self.con, str(loc), full=True)
                rep = report.task_report(self.con, "B")
                self.assertNotIn("claude:" + rid, rep["measured"]["attributed_responses"])
                self.assertIn("claude:" + rid, rep["measured"]["unplaced_responses"])
                self.assertIsNone(rep["total_cost"]["usd"])
                # Repair the first copy to the second final. A first-import
                # preference would preserve the wrong measurement here.
                first.write_bytes(second.read_bytes())
                claude.import_claude_file(self.con, str(first), full=True)
                rep = report.task_report(self.con, "B")
                self.assertIn("claude:" + rid, rep["measured"]["attributed_responses"])
                self.assertEqual(self.con.execute("SELECT output_tokens FROM responses WHERE response_id=?",
                                                  ("claude:" + rid,)).fetchone()[0], count)

    def test_unfinished_opencode_message_keeps_owned_total_open(self):
        path = self.opencode_store("pending", "msg_u1", 0.20, 50, independent=True)
        with closing(sqlite3.connect(path)) as source, source:
            source.execute("UPDATE message SET data=json_remove(data,'$.time.completed') WHERE id='msg_a1'")
        opencode.sync(self.con, source=path)
        self.assign("A", "opencode:msg_u1")
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["measured"]["attributed_responses"], ["opencode:msg_unique"])
        self.assertFalse(rep["complete"])
        self.assertIsNone(rep["native_cost"]["total_usd"])
        self.assertEqual(rep["native_cost"]["known_subtotal_usd"], 0.10)
        self.assertIn("Evidence complete: no", self.draft()["body"])
        with closing(sqlite3.connect(path)) as source, source:
            source.execute("UPDATE message SET data=json_set(data,'$.time.completed',123) WHERE id='msg_a1'")
        opencode.sync(self.con, source=path, full=True)
        self.assertAlmostEqual(report.task_report(self.con, "A")["native_cost"]["total_usd"], 0.30)

    def test_rejected_claude_event_has_no_display_turn_fallback(self):
        path = Path(self.tmp.name, "initial.jsonl")
        a, b = [claude_response("reply-" + p, p, "msg-" + p) for p in ("a", "b")]
        for response, call in ((a, "a-call"), (b, "b-call")):
            response["message"]["content"] = [{"type": "tool_use", "id": call, "name": "Read"}]
        b["message"]["usage"]["output_tokens"] = "bad"
        write_records(path, [claude_user("b"), claude_user("a"), a, b])
        claude.import_claude_file(self.con, str(path))
        self.assign("A", "claude:a")
        self.assign("B", "claude:b")
        self.assertEqual([e["native_id"] for e in report.timeline(self.con, "A")["events"]], ["a-call"])
        self.assertFalse(report.timeline(self.con, "B")["events"])

    def test_unreadable_claude_line_cannot_erase_a_dispute(self):
        path = self.import_claude()
        self.assign("B", "claude:b")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        copy = path.with_name("damaged.jsonl")
        rows[-1]["parentUuid"] = "a"
        write_records(copy, rows)
        claude.import_claude_file(self.con, str(copy), full=True)
        write_records(copy, rows[:-1])
        with copy.open("a") as source:
            source.write("{\n")
        for _ in range(2):
            claude.import_claude_file(self.con, str(copy), full=True)
            self.assertFalse(report.task_report(self.con, "B")["attributed"]["responses"])
        rows[-1]["parentUuid"] = "b"
        write_records(copy, rows)
        claude.import_claude_file(self.con, str(copy), full=True)
        self.assertEqual(report.task_report(self.con, "B")["attributed"]["responses"], 1)

    def test_losing_measurement_or_message_cannot_clear_claude_final_dispute(self):
        path = self.import_claude()
        self.assign("B", "claude:b")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        other = path.with_name("changed.jsonl")
        msg = rows[-1]["message"]
        msg["usage"]["output_tokens"] = 90000
        write_records(other, rows)
        claude.import_claude_file(self.con, str(other))
        self.assertFalse(report.task_report(self.con, "B")["attributed"]["responses"])
        del msg["usage"]
        for replacement in (msg, [], None):
            with self.subTest(message_type=type(replacement).__name__):
                rows[-1]["message"] = replacement
                write_records(other, rows)
                for _ in range(2):
                    claude.import_claude_file(self.con, str(other), full=True)
                    self.assertFalse(report.task_report(self.con, "B")["attributed"]["responses"])
                    self.assertFalse(report.timeline(self.con, "B")["events"])
        other.write_bytes(path.read_bytes())
        claude.import_claude_file(self.con, str(other), full=True)
        self.assertEqual(report.task_report(self.con, "B")["attributed"]["responses"], 1)

    def test_zero_counters_and_invalid_completion_time_withhold_opencode_cost(self):
        path = self.opencode_store("unmeasured", "msg_u1", .20, 50, independent=True)
        with closing(sqlite3.connect(path)) as src:
            original = json.loads(src.execute("SELECT data FROM message WHERE id='msg_a1'").fetchone()[0])
        opencode.sync(self.con, source=path)
        self.assign("A", "opencode:msg_u1")
        zero = dict(original, tokens={"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}})
        invalid_times = [dict(original, time={"completed": v}) for v in (False, True, -1, "broken", {})]
        no_counters = [dict(original, tokens=tokens) for tokens in ({}, {"cache": {}}, None)]
        for record in [zero, *invalid_times, *no_counters]:
            with self.subTest(time=record["time"], tokens=record["tokens"]):
                with closing(sqlite3.connect(path)) as src, src:
                    src.execute("UPDATE message SET data=? WHERE id='msg_a1'", (json.dumps(record),))
                opencode.sync(self.con, source=path, full=True)
                rep = report.task_report(self.con, "A")
                self.assertFalse(rep["complete"])
                self.assertIsNone(rep["native_cost"]["total_usd"])
                self.assertEqual(rep["native_cost"]["known_subtotal_usd"], .10)
                self.assertIn("Evidence complete: no", self.draft()["body"])

    def test_provisional_claude_copy_cannot_retract_an_observed_final(self):
        path = self.import_claude()
        self.assign("B", "claude:b")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        other = path.with_name("withdrawn.jsonl")
        msg = rows[-1]["message"]
        msg["usage"]["output_tokens"] = 90000
        write_records(other, rows)
        claude.import_claude_file(self.con, str(other))
        self.assertFalse(report.task_report(self.con, "B")["attributed"]["responses"])
        provisional = dict(msg, stop_reason=None)
        for message in (provisional, dict(msg, usage=None), [], provisional, provisional):
            rows[-1]["message"] = message
            write_records(other, rows)
            claude.import_claude_file(self.con, str(other), full=True)
            rep = report.task_report(self.con, "B")
            self.assertFalse(rep["attributed"]["responses"])
            self.assertFalse(rep["complete"])
            self.assertFalse(report.timeline(self.con, "B")["events"])
        other.write_bytes(path.read_bytes())
        claude.import_claude_file(self.con, str(other), full=True)
        self.assertTrue(report.task_report(self.con, "B")["complete"])

    def test_partial_measurements_contribute_observations_without_closing_the_total(self):
        path = self.opencode_store("partial", "msg_u1", 0.20, 50)
        with closing(sqlite3.connect(path)) as source, source:
            source.execute("UPDATE message SET data=json_remove(data,'$.tokens.input') WHERE id='msg_a1'")
        opencode.sync(self.con, source=path)
        self.assign("A", "opencode:msg_u1")
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["attributed"]["output_tokens"], 50)
        self.assertFalse(rep["complete"])
        self.assertIsNone(rep["native_cost"]["total_usd"])
        self.assertEqual(rep["native_cost"]["known_subtotal_usd"], 0.20)

    def test_claude_provisional_usage_is_an_observation_until_its_first_final(self):
        path = Path(self.tmp.name, "stream.jsonl")
        response = claude_response("block", "p", "streamed")
        response["message"]["usage"].update(input_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        response["message"]["stop_reason"] = None
        write_records(path, [claude_user("p"), response])
        claude.import_claude_file(self.con, str(path))
        self.assign("A", "claude:p")
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["attributed"]["output_tokens"], 1)
        self.assertFalse(rep["complete"])
        response["message"]["stop_reason"] = "end_turn"
        write_records(path, [response], mode="a")
        claude.import_claude_file(self.con, str(path))
        self.assertTrue(report.task_report(self.con, "A")["complete"])

    def test_claude_final_with_unavailable_counters_cannot_close_the_bill(self):
        path = Path(self.tmp.name, "only-final.jsonl")
        message = claude_response("block", "p", "partial")
        message["message"]["usage"] = {"output_tokens": None}
        write_records(path, [claude_user("p"), message])
        claude.import_claude_file(self.con, str(path))
        self.assign("A", "claude:p")
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["attributed"]["responses"], 0)
        self.assertFalse(rep["complete"])
        message["message"]["usage"]["output_tokens"] = 8
        write_records(path, [claude_user("p"), message])
        claude.import_claude_file(self.con, str(path), full=True)
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["attributed"]["output_tokens"], 8)
        self.assertFalse(rep["complete"])

    def test_torn_final_line_preserves_dispute_until_a_readable_repair(self):
        path = self.import_claude()
        self.assign("B", "claude:b")
        other = path.with_name("torn.jsonl")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[-1]["message"]["usage"]["output_tokens"] = 90000
        write_records(other, rows)
        claude.import_claude_file(self.con, str(other))
        write_records(other, rows[:-1])
        tail = json.dumps(rows[-1])
        with other.open("a") as out:
            out.write(tail[:24])
        for _ in range(2):
            claude.import_claude_file(self.con, str(other))
            self.assertFalse(report.task_report(self.con, "B")["attributed"]["responses"])
            self.assertFalse(report.timeline(self.con, "B")["events"])
        rows[-1]["message"]["usage"]["output_tokens"] = 5
        with other.open("a") as out:
            out.write(json.dumps(rows[-1])[24:] + "\n")
        claude.import_claude_file(self.con, str(other))
        self.assertTrue(report.task_report(self.con, "B")["complete"])

    def check_completed_partial_final(self, append):
        p = Path(self.tmp.name, "partial-final.jsonl")
        msg = claude_response("reply", "p", "mid")
        usage = {"output_tokens": 90000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        msg["message"].update(model="claude-haiku-4-5", usage=usage,
                              content=[{"type": "tool_use", "id": "call", "name": "Read"}])
        write_records(p, [claude_user("p"), msg])
        claude.import_claude_file(self.con, str(p))
        self.assign("A", "claude:p")
        before = report.task_report(self.con, "A")
        self.assertEqual(before["attributed"]["output_tokens"], 90000)
        self.assertFalse(before["complete"])
        usage["input_tokens"] = 11
        write_records(p, [msg] if append else [claude_user("p"), msg], mode="a" if append else "w")
        claude.import_claude_file(self.con, str(p))
        after = report.task_report(self.con, "A")
        self.assertTrue(after["complete"])
        self.assertEqual(after["attributed"]["total_tokens"], 90011)
        self.assertEqual([e["native_id"] for e in report.timeline(self.con, "A")["events"]], ["call"])

    def test_appended_final_enriches_missing_counter(self):
        self.check_completed_partial_final(True)

    def test_rewritten_final_enriches_missing_counter(self):
        self.check_completed_partial_final(False)

    def test_partial_copy_cannot_retract_its_disputed_counter(self):
        path = self.import_claude()
        self.assign("B", "claude:b")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        copy = path.with_name("partial-copy.jsonl")
        usage = rows[-1]["message"]["usage"]
        for output in (90000, None, 5):
            with self.subTest(output=output):
                if output is None:
                    del usage["output_tokens"]
                else:
                    usage["output_tokens"] = output
                write_records(copy, rows)
                claude.import_claude_file(self.con, str(copy))
                rep = report.task_report(self.con, "B")
                self.assertEqual(rep["complete"], output == 5)
                self.assertEqual(rep["attributed"]["responses"], int(output == 5))

    def check_withdrawn_field(self, ttl):
        path = Path(self.tmp.name, "original.jsonl")
        response = claude_response("reply", "b", "response")
        usage = dict(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                     cache_creation_input_tokens=10 if ttl else 0)
        if ttl:
            usage["cache_creation"] = {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 0}
        response["message"].update(usage=usage, model="claude-haiku-4-5",
                                   content=[{"type": "tool_use", "id": "b-call", "name": "Read"}])
        rows = [claude_user("b"), response]
        write_records(path, rows)
        claude.import_claude_file(self.con, str(path))
        self.assign("B", "claude:b")
        rid = "claude:" + rows[-1]["message"]["id"]
        call = rows[-1]["message"]["content"][0]["id"]
        copy = path.with_name("competing.jsonl")
        if ttl:
            usage["cache_creation"] = {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 10}
        else:
            del usage["input_tokens"]
            usage["output_tokens"] = 90000
        for step in ("different", "withdrawn", "repeat", "restored"):
            if step == "withdrawn":
                if ttl:
                    del usage["cache_creation"]
                else:
                    usage["input_tokens"] = 10
                    del usage["output_tokens"]
            if step == "restored":
                rows = [json.loads(line) for line in path.read_text().splitlines()]
            write_records(copy, rows)
            claude.import_claude_file(self.con, str(copy))
            with self.subTest(step=step):
                rep = report.task_report(self.con, "B")
                self.assertEqual(rid in rep["measured"]["attributed_responses"], step == "restored")
                self.assertEqual(call in [e["native_id"] for e in report.timeline(self.con, "B")["events"]],
                                 step == "restored")
                if step != "restored":
                    self.assertIsNone(rep["total_cost"]["usd"])

    def test_withdrawing_partial_disputed_output_does_not_resolve_it(self):
        self.check_withdrawn_field(False)

    def test_withdrawing_disputed_cache_split_does_not_resolve_it(self):
        self.check_withdrawn_field(True)

    def check_contradictory_cache_enrichment(self, append):
        path = Path(self.tmp.name, "cache.jsonl")
        msg = claude_response("reply", "p", "cache")
        usage = dict(input_tokens=5, output_tokens=7, cache_read_input_tokens=0,
                     cache_creation=dict(ephemeral_5m_input_tokens=100000, ephemeral_1h_input_tokens=0))
        msg["message"].update(model="claude-haiku-4-5", usage=usage,
                              content=[{"type": "tool_use", "id": "cache-call", "name": "Read"}])
        write_records(path, [claude_user("p"), msg])
        claude.import_claude_file(self.con, str(path))
        self.assign("A", "claude:p")
        del usage["cache_creation"]
        usage["cache_creation_input_tokens"] = 0
        write_records(path, [msg] if append else [claude_user("p"), msg], mode="a" if append else "w")
        claude.import_claude_file(self.con, str(path))
        rep = report.task_report(self.con, "A")
        self.assertFalse(rep["attributed"]["responses"])
        self.assertFalse(rep["complete"])
        self.assertIsNone(rep["total_cost"]["usd"])
        self.assertFalse(report.timeline(self.con, "A")["events"])
        self.assertIn("Evidence complete: no", self.draft()["body"])
        # A readable replacement with the matching aggregate can still seal the bill.
        usage["cache_creation_input_tokens"] = 100000
        write_records(path, [claude_user("p"), msg])
        claude.import_claude_file(self.con, str(path))
        rep = report.task_report(self.con, "A")
        self.assertTrue(rep["complete"])
        self.assertEqual(rep["attributed"]["total_tokens"], 100012)

    def test_appending_incompatible_cache_components_withholds_usage(self):
        self.check_contradictory_cache_enrichment(True)

    def test_rewriting_incompatible_cache_components_withholds_usage(self):
        self.check_contradictory_cache_enrichment(False)

    def test_complete_copy_and_corrected_partial_confirm_the_same_measurement(self):
        partial = Path(self.tmp.name, "first.jsonl")
        complete = Path(self.tmp.name, "second.jsonl")
        msg = claude_response("reply", "p", "id")
        rows = [claude_user("p"), msg]
        usage = dict(output_tokens=90000, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        msg["message"].update(usage=usage, model="claude-haiku-4-5")
        write_records(partial, rows)
        claude.import_claude_file(self.con, str(partial))
        self.assign("A", "claude:p")
        usage.update(output_tokens=5, input_tokens=10)
        write_records(complete, rows)
        claude.import_claude_file(self.con, str(complete))
        self.assertFalse(report.task_report(self.con, "A")["attributed"]["responses"])
        del usage["input_tokens"]
        write_records(partial, rows)
        claude.import_claude_file(self.con, str(partial))
        rep = report.task_report(self.con, "A")
        self.assertTrue(rep["complete"])
        self.assertEqual(rep["attributed"]["total_tokens"], 15)

    def check_removed_claude_final(self, remove_complete, complete_first):
        a = Path(self.tmp.name, "partial.jsonl")
        b = Path(self.tmp.name, "complete.jsonl")
        msg = claude_response("reply", "p", "id")
        msg["message"].update(model="claude-haiku-4-5", content=[{"type": "tool_use", "id": "call", "name": "Read"}])
        common = dict(output_tokens=5, cache_read_input_tokens=0)
        partial = dict(common, cache_creation_input_tokens=0) if remove_complete else dict(
            common, input_tokens=10, cache_creation={"ephemeral_5m_input_tokens": 100000, "ephemeral_1h_input_tokens": 0})
        for path, counters in [(a, partial), (b, dict(common, input_tokens=10, cache_creation_input_tokens=0))]:
            msg["message"]["usage"] = counters
            write_records(path, [claude_user("p"), msg])
        for path in ([b, a] if complete_first else [a, b]):
            claude.import_claude_file(self.con, str(path))
        self.assign("A", "claude:p")
        self.assertEqual(report.task_report(self.con, "A")["complete"], remove_complete)
        removed, survivor = (b, a) if remove_complete else (a, b)
        write_records(removed, [claude_user("p")])
        claude.import_claude_file(self.con, str(removed))
        for repeat in (False, True):
            if repeat:
                self.assertEqual(claude.import_claude_file(self.con, str(survivor), full=True)["malformed"], 0)
            with self.subTest(reimport=repeat):
                rep = report.task_report(self.con, "A")
                self.assertEqual(rep["attributed"]["responses"], 1)
                self.assertEqual(rep["complete"], not remove_complete)
                self.assertEqual(rep["total_cost"]["usd"] is None, remove_complete)
                self.assertEqual([e["native_id"] for e in report.timeline(self.con, "A")["events"]], ["call"])
                measured = self.con.execute("SELECT total_tokens,input_tokens,output_tokens,cache_write_5m_tokens "
                                            "FROM responses WHERE response_id='claude:id'").fetchone()
                self.assertEqual(tuple(measured), (None, None, 5, None) if remove_complete else (15, 10, 5, None))
                self.assertIn("Evidence complete: " + ("no" if remove_complete else "yes"), self.draft()["body"])

    def test_remove_complete_copy_imported_first(self):
        self.check_removed_claude_final(True, True)

    def test_remove_complete_copy_imported_second(self):
        self.check_removed_claude_final(True, False)

    def test_remove_competing_cache_split_imported_first(self):
        self.check_removed_claude_final(False, False)

    def test_remove_competing_cache_split_imported_second(self):
        self.check_removed_claude_final(False, True)

    def test_upgrade_retires_cache_split_with_live_original_total(self):
        a = Path(self.tmp.name, "aggregate.jsonl")
        b = Path(self.tmp.name, "split.jsonl")
        user = claude_user("p")
        msg = claude_response("reply", "p", "id")
        usage = dict(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=100000)
        msg["message"].update(model="claude-haiku-4-5", usage=usage)
        write_records(a, [user, msg])
        claude.import_claude_file(self.con, str(a))
        self.assign("A", "claude:p")
        del usage["input_tokens"]
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 100000}
        write_records(b, [user, msg])
        claude.import_claude_file(self.con, str(b))
        priced = report.task_report(self.con, "A")["total_cost"]["usd"]
        self.assertAlmostEqual(priced, .200035)
        write_records(b, [user])
        claude.import_claude_file(self.con, str(b))
        # Version 15 lost the supporting copy but left its TTL fields and
        # the original source id on the materialized response.
        self.con.execute("UPDATE responses SET cache_write_5m_tokens=0,cache_write_1h_tokens=100000")
        self.con.execute("UPDATE sources SET claude_usage_version=15")
        for source in (a, b):
            claude.import_claude_file(self.con, str(source))
        rep = report.task_report(self.con, "A")
        self.assertEqual(rep["attributed"]["total_tokens"], 100015)
        self.assertIsNone(rep["total_cost"]["usd"])
        self.assertIn("Estimated cost unknown", self.draft()["body"])

    def test_legacy_trace_uses_events_present_in_surviving_copies(self):
        user = claude_user("p")
        msg = claude_response("reply", "p", "id")
        del msg["parentUuid"]
        a = Path(self.tmp.name, "first.jsonl")
        b = Path(self.tmp.name, "second.jsonl")
        for path, call in ((a, "retired-call"), (b, "surviving-call")):
            msg["message"]["content"] = [{"type": "tool_use", "id": call, "name": "Read"}]
            write_records(path, [user, msg])
            claude.import_claude_file(self.con, str(path))
        self.assign("A", "claude:p")
        msg["uuid"] = "new-reply"
        msg["message"].update(id="independent", content=[{"type":"tool_use", "id":"independent-call", "name":"Read"}])
        write_records(a, [user, msg])
        claude.import_claude_file(self.con, str(a))
        for turn in (None, "claude:p"):
            self.assertEqual({e["native_id"] for e in report.timeline(self.con, "A", turn_id=turn)["events"]},
                             {"independent-call", "surviving-call"})

    def test_legacy_append_resumes_the_prompt_in_its_own_source(self):
        files = [Path(self.tmp.name, task + ".jsonl") for task in ("A", "B")]
        for index, (task, path) in enumerate(zip(("A", "B"), files), 1):
            prompt = dict(claude_user(task), timestamp=f"2026-11-25T08:00:0{index}Z")
            response = claude_response("reply-" + task, task, "id-" + task)
            del response["parentUuid"]
            write_records(path, [prompt, response])
            claude.import_claude_file(self.con, str(path))
            self.assign(task, "claude:" + task)
        late = claude_response("late", "A", "late")
        del late["parentUuid"]
        late["message"]["content"] = [{"type":"tool_use", "id":"late-call", "name":"Read"}]
        write_records(files[0], [late], mode="a")
        claude.import_claude_file(self.con, str(files[0]))
        for task in ("A", "B"):
            self.assertEqual("claude:late" in report.task_report(self.con, task)["measured"]["attributed_responses"],
                             task == "A")
            self.assertEqual("late-call" in [e["native_id"] for e in report.timeline(self.con, task)["events"]],
                             task == "A")
