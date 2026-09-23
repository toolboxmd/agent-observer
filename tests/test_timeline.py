"""Timeline: compaction, tool joins, read/skill evidence limits."""

from agent_observer import report
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
        # Rule 6 keeps the observed path in target; the file basename is
        # an instance value, not a canonical kind, so names stay empty.
        self.assertEqual([e["target"] for e in reads], ["AGENTS.md"])
        self.assertEqual([e["name"] for e in reads], [None])
        skills = report.timeline(self.con, family="skill_read")["events"]
        self.assertEqual([e["target"] for e in skills],
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
