"""Timeline: compaction, tool joins, read/skill evidence limits."""

import json
import os

from agent_observer import db, report
from agent_observer.adapters.codex import import_codex_file
from tests.helpers import LedgerCase


class TimelineTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.sync("codex-mini.jsonl")

    def test_compaction_boundary_and_overlap(self):
        tl = report.timeline(self.con, family="compaction")
        self.assertEqual(len(tl["events"]), 2)
        by_id = {e["native_id"]: e for e in tl["events"]}
        self.assertIn("window-mini-01", by_id)
        # The compacted latest usage is overlap, not additional tokens.
        row = self.query(
            "SELECT response_id FROM responses "
            "WHERE response_id='codex:resp-mini-002'")
        self.assertEqual(len(row), 1)
        self.assertEqual(report.scope_totals(self.con)["responses"], 3)
        # Sparse native compaction markers import as boundaries with no
        # free-text detail, never quarantined as malformed.
        markers = [e for e in tl["events"]
                   if e["native_id"] == "ctx-mini-001"]
        self.assertEqual(len(markers), 1)
        for event in tl["events"]:
            self.assertIsNone(event["detail"])

    def test_tool_call_result_join_only_on_equal_call_id(self):
        tl = report.timeline(self.con)
        self.assertEqual(tl["join"]["joined_call_ids"], ["call-mini-001"])
        # Native completed items keep their own ids and stay unmatched
        # rather than guessed into a wrapper/inner match.
        self.assertIn("exec-mini-001", tl["join"]["unmatched_results"])
        self.assertIn("exec-mini-002", tl["join"]["unmatched_results"])
        self.assertIn("call-mini-mcp-09", tl["join"]["unmatched_results"])
        self.assertIn("call-mini-spawn-01", tl["join"]["unanswered_calls"])

    def test_read_evidence_comes_only_from_observed_operations(self):
        reads = report.timeline(self.con, family="read")["events"]
        # Rule 6 identifier family: the observed path stays in target and
        # its native basename stays in name; free text never does.
        self.assertEqual([e["target"] for e in reads], ["AGENTS.md"])
        self.assertEqual([e["name"] for e in reads], ["AGENTS.md"])
        skills = report.timeline(self.con, family="skill_read")["events"]
        # Planner ruling: skill_read target holds only the validated skill
        # identifier; the installed path lives only in detail skill_path.
        self.assertEqual([e["target"] for e in skills], ["wayfinder"])
        self.assertEqual([e["detail"]["skill_path"] for e in skills],
                         ["skills/wayfinder/SKILL.md"])
        # Prose that mentions SKILL.md creates no skill event.
        self.assertEqual(report.timeline(self.con,
                                         family="skill_invocation")["events"], [])

    def test_capability_declaration_states_observed_coverage(self):
        caps = {c["family"]: c for c in report.capabilities()
                if c["harness"] == "codex"}
        self.assertTrue(caps["model_usage"]["supported"])
        self.assertTrue(caps["compaction"]["supported"])
        self.assertTrue(caps["read_evidence"]["supported"])
        self.assertTrue(caps["file_change"]["supported"])
        self.assertFalse(caps["skill_invocation"]["supported"])
        self.assertFalse(caps["quota"]["supported"])

    def test_file_change_keeps_paths_without_contents(self):
        tl = report.timeline(self.con, family="file_change")
        self.assertEqual(len(tl["events"]), 1)
        self.assertEqual(tl["events"][0]["detail"]["paths"],
                         ["/redacted/workspace/notes.md"])
        self.assertNotIn("redacted body",
                         tl["events"][0]["detail"].__repr__())

    def test_aborted_turn_stays_provisional(self):
        turn = self.query(
            "SELECT state FROM turns WHERE turn_id='codex:turn-mini-ccc'")[0]
        self.assertEqual(turn["state"], "cancelled")
        tl = report.timeline(self.con, turn_id="codex:turn-mini-ccc")
        self.assertEqual(tl["events"][0]["name"], "turn_aborted")


class MultiSessionOrderTest(LedgerCase):
    def test_unrelated_session_does_not_invert_a_known_order(self):
        meta = lambda name: {"type": "session_meta", "payload": {
            "id": name, "thread_source": "user"}}
        call = lambda name: {"type": "response_item", "payload": {
            "type": "function_call", "call_id": name, "name": "exec_command",
            "arguments": '{"cmd":"echo x"}'}}
        usage = lambda name: {"type": "token_usage_record", "payload": {
            "response_id": name, "thread_id": "multi",
            "usage": {"input_tokens": 10, "output_tokens": 2,
                      "total_tokens": 12}}}
        original = os.path.join(self.tmp.name, "multi-a.jsonl")
        continuation = os.path.join(self.tmp.name, "multi-b.jsonl")
        other = os.path.join(self.tmp.name, "multi-other.jsonl")

        def dump(path, rows):
            with open(path, "w") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

        dump(original, [meta("multi"), call("call-a"), usage("r1"),
                        call("call-b"), usage("r2")])
        dump(continuation, [meta("multi")]
             + [{"type": "world_state", "payload": {}}] * 8
             + [call("call-a")])
        dump(other, [meta("other")]
             + [{"type": "world_state", "payload": {}}] * 12
             + [call("unrelated")])
        for full in (False, True, False):
            for path in (continuation, original, other):
                import_codex_file(self.con, path, full=full)
            single = report.timeline(
                self.con, session_key="codex:multi", family="tool_call",
                limit=1)
            global_ = report.timeline(self.con, family="tool_call", limit=1)
            self.assertEqual(
                [e["native_id"] for e in single["events"]], ["call-a"])
            self.assertEqual(
                [e["native_id"] for e in global_["events"]], ["call-a"])
            self.assertEqual(global_["chronology"], "unknown")


class CopiedAnchorOrderTest(LedgerCase):
    def test_copied_call_does_not_become_the_execution_order(self):
        meta = {"type": "session_meta", "payload": {
            "id": "anchor", "thread_source": "user"}}

        def call(name):
            return {"type": "response_item", "payload": {
                "type": "function_call", "call_id": name,
                "name": "exec_command", "arguments": '{"cmd":"echo x"}'}}

        usage = {"type": "token_usage_record", "payload": {
            "response_id": "r1", "thread_id": "anchor",
            "usage": {"input_tokens": 10, "output_tokens": 2,
                      "total_tokens": 12}}}
        original = os.path.join(self.tmp.name, "anchor-a.jsonl")
        copy = os.path.join(self.tmp.name, "anchor-b.jsonl")
        with open(original, "w") as fh:
            for row in (meta, call("old-a"), usage):
                fh.write(json.dumps(row) + "\n")
        with open(copy, "w") as fh:
            for row in (meta, call("new-c"), call("old-a")):
                fh.write(json.dumps(row) + "\n")
        for reverse in (False, True):
            self.con.close()
            self.con = db.connect(
                os.path.join(self.tmp.name, f"anchor-{reverse}.db"))
            db.init_db(self.con)
            for full in (False, True, False):
                for path in ((copy, original) if reverse else (original, copy)):
                    import_codex_file(self.con, path, full=full)
                out = report.timeline(
                    self.con, session_key="codex:anchor", family="tool_call")
                self.assertEqual(out["chronology"], "unknown")
                names = {str(event["id"]): event["native_id"]
                         for event in out["events"]}
                sequences = []
                for sequence in out["source_sequences"]["codex:anchor"]:
                    flat = []
                    for group in sequence:
                        for ident in group:
                            if ident[0] == "event":
                                flat.append(names[ident[1]])
                    sequences.append(flat)
                self.assertIn(["old-a"], sequences)
                self.assertIn(["new-c", "old-a"], sequences)
                self.assertCountEqual(
                    [event["native_id"] for event in out["events"]],
                    ["old-a", "new-c"])


class TimestampInterleaveTest(LedgerCase):
    def test_claude_sessions_stay_in_timestamp_order(self):
        from agent_observer.adapters.claude import import_claude_file

        def answer(session, name, minute):
            return {"type": "assistant", "sessionId": session, "uuid": name,
                    "timestamp": f"2026-10-14T12:{minute:02d}:00Z",
                    "message": {"id": name, "role": "assistant",
                                "content": [{"type": "text", "text": name}]}}

        for session, rows in (
                ("a", [answer("a", "a1", 1), answer("a", "a3", 3)]),
                ("b", [answer("b", "b2", 2)])):
            path = os.path.join(self.tmp.name, f"timeline-{session}.jsonl")
            with open(path, "w") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
            import_claude_file(self.con, path)
        wide = report.timeline(self.con, limit=10)
        narrow = report.timeline(self.con, limit=2)
        self.assertEqual(
            [event["native_id"] for event in wide["events"]],
            ["a1:0", "b2:0", "a3:0"])
        self.assertEqual(
            [event["native_id"] for event in narrow["events"]],
            ["a1:0", "b2:0"])
        self.assertNotIn("chronology", wide)
