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


class PublishSemanticsTest(MixedSemanticsCase):
    """Finding 3 publish: model rows group by semantics, never a mixed sum."""

    def test_model_rows_include_semantics_without_mixed_sum(self):
        from agent_observer import publish as _publish
        summary = _publish.summarize(self.con, {"codex:s1", "claude:s2"},
                                     "task T-M")
        by_key = {(m["harness"], m["model"], m.get("semantics"))
                  for m in summary["models"]}
        self.assertIn(("codex", "m", CODEX_SEM), by_key)
        self.assertIn(("claude", "m", CLAUDE_SEM), by_key)
        for m in summary["models"]:
            self.assertIn("semantics", m)
        # No row sums across semantics: each row's tokens equal its own
        # semantics group, and the usage has no combined total.
        self.assertNotIn("total_tokens", summary["usage"])
        by = summary["usage"]["by_semantics"]
        self.assertEqual(by[CODEX_SEM]["total_tokens"], 1100)
        self.assertEqual(by[CLAUDE_SEM]["total_tokens"], 1160)
        body = _publish.render(summary)
        self.assertIn(CODEX_SEM, body)
        self.assertNotIn("Total tokens", body)

    def test_mixed_codex_session_models_do_not_sum_semantics(self):
        # One Codex session mixing the authoritative and token_count
        # semantics: model rows stay per-semantics, never combined.
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key, model, total_tokens, semantics) VALUES"
            " ('codex:r3', 1, 'codex', 'codex:s1', 'm', 500,"
            f" '{CODEX_SEM};source=token_count')")
        self.con.commit()
        from agent_observer import publish as _publish
        summary = _publish.summarize(self.con, {"codex:s1"}, "one session")
        self.assertNotIn("total_tokens", summary["usage"])
        sems = {m.get("semantics") for m in summary["models"]}
        self.assertGreater(len(sems), 1)
        combined = sum((m["tokens"] or 0) for m in summary["models"])
        # The test asserts absence of a false total, not the combined
        # arithmetic: usage exposes by_semantics only.
        self.assertIn("by_semantics", summary["usage"])
        self.assertEqual(
            summary["usage"]["by_semantics"][CODEX_SEM]["total_tokens"],
            1100)


class SessionsSemanticsTest(MixedSemanticsCase):
    """Finding 3 sessions: list/show omit a false combined total."""

    def _usage_via_cli(self, key):
        from agent_observer import report as _report
        return _report.scope_totals(self.con, {key})

    def test_mixed_session_show_has_by_semantics_no_combined_total(self):
        # A single session mixing semantics (known beside unknown).
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key, total_tokens, semantics)"
            " VALUES('codex:r3', 1, 'codex', 'codex:s1', 100, NULL)")
        self.con.commit()
        usage = self._usage_via_cli("codex:s1")
        for bucket in RAW_BUCKETS:
            self.assertNotIn(bucket, usage, bucket)
        self.assertIn("unknown", usage["by_semantics"])
        self.assertEqual(
            usage["by_semantics"]["unknown"]["total_tokens"], 100)
        self.assertEqual(
            usage["by_semantics"][CODEX_SEM]["total_tokens"], 1100)

    def test_sessions_list_text_says_mixed_semantics(self):
        from agent_observer import cli as _cli
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key, total_tokens, semantics)"
            " VALUES('codex:r3', 1, 'codex', 'codex:s1', 100, NULL)")
        self.con.commit()
        from agent_observer import report as _report
        usage = _report.scope_totals(self.con, {"codex:s1"})
        row = {"session_key": "codex:s1", "project_dir": "/p",
               "agentsmd_version": None, "responses": usage["responses"]}
        if "total_tokens" in usage:
            row["total_tokens"] = usage["total_tokens"]
        if "by_semantics" in usage:
            row["by_semantics"] = usage["by_semantics"]
        self.assertNotIn("total_tokens", row)
        self.assertIn("by_semantics", row)
        text = _cli._fmt_session_total(row)
        self.assertIn("mixed semantics", text)


class CompareSemanticsTest(MixedSemanticsCase):
    """Finding 3 compare: no numeric per-session/group totals across semantics."""

    def test_compare_by_agentsmd_omits_mixed_total(self):
        from agent_observer import analysis as _analysis
        from agent_observer import db as _db
        _db.upsert_session(self.con, "codex:s3", "codex", "s3", None,
                           agentsmd_version="12.1.0")
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key, total_tokens, semantics) VALUES"
            f" ('codex:r3', 1, 'codex', 'codex:s3', 500, '{CLAUDE_SEM}')")
        self.con.commit()
        result = _analysis.compare(self.con, by="harness")
        groups = {g["group"]: g for g in result["groups"]}
        # The codex group holds one codex-semantics session and one
        # claude-semantics response in a codex session: mixed, so no
        # combined total.
        codex = groups["codex"]
        self.assertNotIn("total", codex["tokens_per_session"])
        self.assertNotIn("median", codex["tokens_per_session"])
        self.assertIn("by_semantics", codex["tokens_per_session"])
        by = codex["tokens_per_session"]["by_semantics"]
        self.assertEqual(by[CODEX_SEM]["tokens"], 1100)
        self.assertEqual(by[CLAUDE_SEM]["tokens"], 500)

    def test_compare_by_model_omits_mixed_total(self):
        from agent_observer import analysis as _analysis
        # Same model 'm' with two semantics across sessions: mixed.
        result = _analysis.compare(self.con, by="model")
        groups = {g["group"]: g for g in result["groups"]}
        row = groups["m"]
        self.assertNotIn("total", row["tokens_per_session"])
        self.assertIn("by_semantics", row["tokens_per_session"])
        by = row["tokens_per_session"]["by_semantics"]
        self.assertEqual(by[CODEX_SEM]["tokens"], 1100)
        self.assertEqual(by[CLAUDE_SEM]["tokens"], 1160)
