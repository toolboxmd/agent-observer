"""Import semantics: usage totals, dedup, checkpoints, growing logs, malformed."""

from agent_observer import report
from tests.helpers import LedgerCase


class ImportTest(LedgerCase):
    def test_mini_totals_follow_response_sums(self):
        stats = self.sync("codex-mini.jsonl")
        self.assertEqual(stats["responses_inserted"], 3)
        self.assertEqual(stats["malformed"], 0)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals, {
            "input_tokens": 5000,
            "cached_input_tokens": 2500,
            "cache_write_input_tokens": 0,
            "output_tokens": 500,
            "reasoning_output_tokens": 50,
            "total_tokens": 5500,
            "responses": 3,
            "overlap_responses": 0,
        })

    def test_genuine_submissions_exclude_synthetic_and_scaffolding(self):
        self.sync("codex-mini.jsonl")
        rows = self.query(
            "SELECT native_id FROM submissions WHERE is_genuine=1 ORDER BY 1")
        self.assertEqual([r["native_id"] for r in rows],
                         ["msg-mini-sub-01", "msg-mini-sub-02"])
        # Synthetic reply and skill scaffolding are stored but not genuine.
        non = self.query(
            "SELECT native_id FROM submissions WHERE is_genuine=0 ORDER BY 1")
        self.assertEqual([r["native_id"] for r in non],
                         ["msg-mini-skill-01", "msg-mini-synthetic-01"])

    def test_checkpoints_are_stored_but_never_summed(self):
        self.sync("codex-mini.jsonl")
        row = self.query(
            "SELECT turn_total_tokens FROM responses "
            "WHERE response_id='resp-mini-002'")[0]
        # Last checkpoint of turn aaa equals the sum of its two responses.
        self.assertEqual(row["turn_total_tokens"], 3350)
        totals = report.scope_totals(self.con)
        # Scope total comes from per-response usage, not checkpoint sums.
        self.assertEqual(totals["total_tokens"], 1100 + 2250 + 2150)

    def test_reimport_is_idempotent(self):
        first = self.sync("codex-mini.jsonl")
        second = self.sync("codex-mini.jsonl")
        self.assertEqual(second["responses_inserted"], 0)
        self.assertEqual(second["responses_duplicate"], 3)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 5500)
        self.assertEqual(first["sha256"], second["sha256"])

    def test_growing_log_updates_correctly(self):
        self.sync("codex-growing-a.jsonl")
        self.assertEqual(report.scope_totals(self.con)["total_tokens"], 1100)
        stats = self.sync("codex-growing-b.jsonl")
        self.assertEqual(stats["responses_inserted"], 1)
        self.assertEqual(stats["responses_duplicate"], 1)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 3350)
        self.assertEqual(totals["responses"], 2)

    def test_malformed_lines_do_not_destroy_valid_data(self):
        stats = self.sync("codex-malformed.jsonl")
        self.assertEqual(stats["malformed"], 2)
        self.assertEqual(stats["responses_inserted"], 1)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 550)
        errors = self.query("SELECT error FROM import_errors")
        self.assertEqual(len(errors), 2)
        # The valid submission survives alongside the quarantined lines.
        subs = self.query("SELECT native_id FROM submissions")
        self.assertEqual([r["native_id"] for r in subs], ["msg-bad-sub-01"])
