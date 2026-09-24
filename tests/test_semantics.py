"""Counter semantics: raw buckets are never summed across meanings.

A scope mixing counter semantics omits the top-level raw buckets and
keeps complete per-semantics totals instead, in scope_totals and in
every task_report counter section. Reconciliation compares response
partitions and per-semantics sums, never an absent mixed total as zero.
"""

from agent_observer import db, publish, report
from tests.helpers import LedgerCase

CODEX_SEM = "codex:input_includes_cached,output_includes_reasoning"
CLAUDE_SEM = "claude:input_excludes_cache,output_includes_thinking"

RAW_BUCKETS = ("input_tokens", "cached_input_tokens",
               "cache_write_input_tokens", "output_tokens",
               "reasoning_output_tokens", "total_tokens")


class MixedSemanticsCase(LedgerCase):
    def setUp(self):
        super().setUp()
        db.upsert_session(self.con, "codex:s1", "codex", "s1", None)
        db.upsert_session(self.con, "claude:s2", "claude", "s2", None)
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('codex','p','x',0)")
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key, turn_id, model, input_tokens,"
            " cached_input_tokens, cache_write_input_tokens, output_tokens,"
            " reasoning_output_tokens, total_tokens, semantics) VALUES"
            " ('codex:r1', 1, 'codex', 'codex:s1', 't1', 'm',"
            "  1000, 100, 10, 100, 5, 1100, ?),"
            " ('claude:r2', 1, 'claude', 'claude:s2', 't2', 'm',"
            "  10, 100, 1000, 50, 20, 1160, ?)",
            (CODEX_SEM, CLAUDE_SEM))
        self.con.commit()


class ScopeSemanticsTest(MixedSemanticsCase):
    def test_mixed_scope_omits_top_level_raw_buckets(self):
        totals = report.scope_totals(self.con)
        for bucket in RAW_BUCKETS:
            self.assertNotIn(bucket, totals, bucket)
        self.assertNotIn("unknown_counts", totals)
        self.assertEqual(totals["responses"], 2)
        self.assertEqual(totals["overlap_responses"], 0)

    def test_mixed_scope_keeps_complete_per_semantics_totals(self):
        totals = report.scope_totals(self.con)
        by = totals["by_semantics"]
        self.assertEqual(sorted(by), [CLAUDE_SEM, CODEX_SEM])
        self.assertEqual(by[CODEX_SEM]["total_tokens"], 1100)
        self.assertEqual(by[CODEX_SEM]["input_tokens"], 1000)
        self.assertEqual(by[CLAUDE_SEM]["total_tokens"], 1160)
        self.assertEqual(by[CLAUDE_SEM]["input_tokens"], 10)

    def test_homogeneous_scope_keeps_historical_raw_buckets(self):
        totals = report.scope_totals(self.con, {"codex:s1"})
        self.assertEqual(totals["total_tokens"], 1100)
        self.assertEqual(totals["input_tokens"], 1000)
        self.assertNotIn("by_semantics", totals)

    def test_known_beside_missing_semantics_is_mixed(self):
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key, total_tokens, semantics)"
            " VALUES('codex:r3', 1, 'codex', 'codex:s1', 100, NULL)")
        self.con.commit()
        totals = report.scope_totals(self.con, {"codex:s1"})
        for bucket in RAW_BUCKETS:
            self.assertNotIn(bucket, totals, bucket)
        self.assertIn("unknown", totals["by_semantics"])
        self.assertEqual(
            totals["by_semantics"]["unknown"]["total_tokens"], 100)
        self.assertEqual(
            totals["by_semantics"][CODEX_SEM]["total_tokens"], 1100)


class TaskSemanticsTest(MixedSemanticsCase):
    def _bind(self):
        self.con.execute(
            "INSERT INTO tasks(task_id, project, family, title, created_at)"
            " VALUES('T-M','observer','research','mixed',0)")
        self.con.execute(
            "INSERT INTO session_assignments(session_key, task_id, evidence,"
            " created_at) VALUES('codex:s1','T-M','e',0)")
        self.con.execute(
            "INSERT INTO session_assignments(session_key, task_id, evidence,"
            " created_at) VALUES('claude:s2','T-M','e',0)")
        self.con.commit()

    def test_task_sections_never_sum_across_semantics(self):
        self._bind()
        rep = report.task_report(self.con, "T-M")
        # Sections holding rows across semantics omit the raw buckets.
        for section in ("attributed", "scope"):
            for bucket in RAW_BUCKETS:
                self.assertNotIn(bucket, rep[section], (section, bucket))
        # Empty sections sum nothing, so historical zero buckets stand.
        for section in ("shared_joint", "unassigned_in_scope"):
            self.assertEqual(rep[section]["responses"], 0)
            self.assertEqual(rep[section]["total_tokens"], 0)
        by = rep["scope"]["by_semantics"]
        self.assertEqual(by[CODEX_SEM]["total_tokens"], 1100)
        self.assertEqual(by[CLAUDE_SEM]["total_tokens"], 1160)

    def test_mixed_task_report_still_reconciles(self):
        self._bind()
        rep = report.task_report(self.con, "T-M")
        self.assertTrue(rep["reconciles"])

    def test_homogeneous_task_report_still_reconciles_with_totals(self):
        self.con.execute(
            "INSERT INTO tasks(task_id, project, family, title, created_at)"
            " VALUES('T-H','observer','research','homogeneous',0)")
        self.con.execute(
            "INSERT INTO session_assignments(session_key, task_id, evidence,"
            " created_at) VALUES('codex:s1','T-H','e',0)")
        self.con.commit()
        rep = report.task_report(self.con, "T-H")
        self.assertEqual(rep["attributed"]["total_tokens"], 1100)
        self.assertTrue(rep["reconciles"])

    def test_publish_render_mixed_scope_invents_no_total(self):
        summary = publish.summarize(self.con, {"codex:s1", "claude:s2"},
                                    "task T-M")
        self.assertNotIn("total_tokens", summary["usage"])
        body = publish.render(summary)
        self.assertIn(CODEX_SEM, body)
        self.assertIn(CLAUDE_SEM, body)
        self.assertIn("1,100", body)
        self.assertIn("1,160", body)
        self.assertNotIn("Total tokens", body)
