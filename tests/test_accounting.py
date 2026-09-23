"""Adapter accounting: fallback checkpoints, streaming finalization, duplicate
conflicts, shape quarantine, NULL counters, and excerpt privacy.

Every test imports fixtures shaped like real native records and asserts the
ledger requirement, not implementation detail."""

import os
import shutil

from agent_observer import report
from agent_observer.adapters import claude
from agent_observer.adapters.codex import import_codex_file
from tests.helpers import LedgerCase, fixture

LEGACY = "codex: sess-fixture-legacy-01".replace(" ", "")


def _excerpts(case, extra=""):
    return "".join(
        r["text_excerpt"] or "" for r in case.query(
            "SELECT text_excerpt FROM submissions" + extra))


class CodexFallbackTest(LedgerCase):
    def test_rising_checkpoints_count_once_each(self):
        stats = self.sync("codex-legacy-a.jsonl")
        self.assertEqual(stats["responses_inserted"], 2)
        rows = self.query(
            "SELECT response_id, total_tokens, thread_total_tokens, semantics,"
            " is_overlap FROM responses ORDER BY thread_total_tokens")
        self.assertEqual([r["response_id"] for r in rows],
                         [f"{LEGACY}:tc:450", f"{LEGACY}:tc:1120"])
        self.assertTrue(all("token_count" in r["semantics"] for r in rows))
        self.assertTrue(all(not r["is_overlap"] for r in rows))
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 450 + 670)
        self.assertEqual(totals["responses"], 2)

    def test_growing_file_processes_new_checkpoints_only(self):
        dst = os.path.join(self.tmp.name, "legacy-grow.jsonl")
        shutil.copy(fixture("codex-legacy-a.jsonl"), dst)
        first = import_codex_file(self.con, dst)
        self.assertEqual(first["responses_inserted"], 2)
        with open(fixture("codex-legacy-b.jsonl")) as fh:
            appended = fh.readlines()[7:]
        with open(dst, "a") as fh:
            fh.writelines(appended)
        second = import_codex_file(self.con, dst)
        # Ordinal 7 repeats the 1120 cumulative total and adds nothing; only
        # the rising 2110 checkpoint is a new response.
        self.assertEqual(second["responses_inserted"], 1)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 450 + 670 + 990)
        self.assertEqual(totals["responses"], 3)

    def test_copied_snapshot_and_full_reimport_add_nothing(self):
        self.sync("codex-legacy-a.jsonl")
        copied = self.sync("codex-legacy-b.jsonl")
        self.assertEqual(copied["responses_inserted"], 1)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 450 + 670 + 990)
        self.assertEqual(totals["responses"], 3)
        reimport = import_codex_file(
            self.con, fixture("codex-legacy-b.jsonl"), full=True)
        self.assertEqual(reimport["responses_inserted"], 0)
        again = report.scope_totals(self.con)
        self.assertEqual(again["total_tokens"], 450 + 670 + 990)
        self.assertEqual(again["responses"], 3)

    def test_authoritative_records_reconcile_fallback_without_double_count(self):
        self.sync("codex-transition-a.jsonl")
        before = report.scope_totals(self.con)
        self.assertEqual(before["total_tokens"], 450 + 670)
        stats = self.sync("codex-transition-b.jsonl")
        self.assertEqual(stats["responses_inserted"], 2)
        totals = report.scope_totals(self.con)
        # Authoritative per-response usage only; the fallback checkpoints that
        # covered the same native work are overlap evidence, not usage.
        self.assertEqual(totals["total_tokens"], 1100 + 2250)
        self.assertEqual(totals["responses"], 2)
        self.assertEqual(totals["overlap_responses"], 2)
        rows = self.query(
            "SELECT response_id FROM responses WHERE is_overlap=1 ORDER BY 1")
        self.assertEqual(
            [r["response_id"] for r in rows],
            ["codex:sess-fixture-trans-01:tc:1120",
             "codex:sess-fixture-trans-01:tc:450"])
        auth = self.query(
            "SELECT response_id FROM responses WHERE is_overlap=0 ORDER BY 1")
        self.assertEqual([r["response_id"] for r in auth],
                         ["codex:resp-trans-001", "codex:resp-trans-002"])

    def test_conflicting_cached_bucket_is_quarantined_not_overwritten(self):
        self.sync("codex-conflict-a.jsonl")
        stats = self.sync("codex-conflict-b.jsonl")
        # Same total_tokens, different cached_input_tokens: a conflict.
        self.assertEqual(stats["malformed"], 1)
        self.assertEqual(stats["responses_inserted"], 1)
        errors = self.query("SELECT error FROM import_errors")
        self.assertEqual(len(errors), 1)
        self.assertIn("resp-conf-001", errors[0]["error"])
        row = self.query(
            "SELECT cached_input_tokens, total_tokens FROM responses"
            " WHERE response_id='codex:resp-conf-001'")[0]
        self.assertEqual(row["cached_input_tokens"], 100)
        self.assertEqual(row["total_tokens"], 1100)
        # The later valid record in the same file still imports.
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM responses"
                       " WHERE response_id='codex:resp-conf-002'")[0]["n"], 1)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 1100 + 550)

    def test_exact_duplicate_is_a_no_op(self):
        first = self.sync("codex-conflict-a.jsonl")
        self.assertEqual(first["responses_inserted"], 1)
        second = import_codex_file(
            self.con, fixture("codex-conflict-a.jsonl"), full=True)
        self.assertEqual(second["responses_inserted"], 0)
        self.assertEqual(second["responses_duplicate"], 1)
        self.assertEqual(second["malformed"], 0)

    def test_lists_scalars_and_unknown_types_are_quarantined(self):
        stats = self.sync("codex-shapes.jsonl")
        self.assertEqual(stats["malformed"], 5)
        self.assertEqual(stats["responses_inserted"], 1)
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM responses"
                       " WHERE response_id='codex:resp-shape-001'")[0]["n"], 1)
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM submissions"
                       " WHERE native_id='codex:msg-shape-sub-01'")[0]["n"], 1)
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM import_errors")[0]["n"], 5)


class CodexExcerptTest(LedgerCase):
    SECRETS = ("SECRET-CODEX-SYNTH-9f8e7d6c5b4a",
               "SECRET-CODEX-SKILL-1a2b3c4d5e6f",
               "SECRET-CODEX-PREFS-4d4e4f505152",
               "SECRET-CODEX-SCAF-77aa88bb99cc")

    def test_only_genuine_input_keeps_an_excerpt(self):
        self.sync("codex-secrets.jsonl")
        rows = {r["native_id"]: r for r in self.query(
            "SELECT native_id, kind, text_excerpt FROM submissions")}
        genuine = rows["codex:msg-secret-sub-01"]
        self.assertEqual(genuine["kind"], "genuine")
        self.assertEqual(len(genuine["text_excerpt"]), 300)
        self.assertTrue(genuine["text_excerpt"].startswith(
            "Please review the quarterly"))
        for native in ("codex:msg-secret-synthetic-01",
                       "codex:msg-secret-skill-01",
                       "codex:msg-secret-scaf-01"):
            self.assertEqual(rows[native]["text_excerpt"], "",
                             native)
        blob = _excerpts(self)
        blob += "".join(r["detail_json"] or "" for r in self.query(
            "SELECT detail_json FROM events"))
        blob += "".join(r["identity_json"] or "" for r in self.query(
            "SELECT identity_json FROM sessions"))
        for secret in self.SECRETS:
            self.assertNotIn(secret, blob)

    def test_identity_still_reads_full_non_genuine_text(self):
        self.sync("codex-secrets.jsonl")
        row = self.query(
            "SELECT instructions_sha256, preferences_sha256 FROM sessions"
            " WHERE session_key='codex:sess-fixture-secret-01'")[0]
        # The direction block arrived inside a scaffolding skill body whose
        # excerpt stays empty, yet its hashes identify the session.
        self.assertEqual(row["instructions_sha256"],
                         "codex-secret-instr-sha")
        self.assertEqual(row["preferences_sha256"],
                         "codex-secret-prefs-sha")


class ClaudeStreamingTest(LedgerCase):
    def test_partial_blocks_update_until_the_final_block(self):
        stats = claude.import_claude_file(
            self.con, fixture("claude-streaming.jsonl"))
        self.assertEqual(stats["responses_inserted"], 2)
        # One conflict quarantined; the later valid message still imports.
        self.assertEqual(stats["malformed"], 1)
        row = self.query(
            "SELECT input_tokens, cached_input_tokens,"
            " cache_write_input_tokens, output_tokens,"
            " reasoning_output_tokens, total_tokens FROM responses"
            " WHERE response_id='claude:msg-stream'")[0]
        self.assertEqual(dict(row), {
            "input_tokens": 10, "cached_input_tokens": 1000,
            "cache_write_input_tokens": 100, "output_tokens": 50,
            "reasoning_output_tokens": 20, "total_tokens": 1160})
        self.assertEqual(
            self.query("SELECT total_tokens t FROM responses"
                       " WHERE response_id='claude:msg-after'")[0]["t"], 1135)
        totals = report.scope_totals(self.con, {"claude:sess-stream"})
        self.assertEqual(totals["total_tokens"], 1160 + 1135)
        errors = self.query("SELECT error FROM import_errors")
        self.assertEqual(len(errors), 1)
        self.assertIn("msg-stream", errors[0]["error"])

    def test_full_reimport_of_streamed_session_adds_nothing(self):
        claude.import_claude_file(self.con, fixture("claude-streaming.jsonl"))
        before = report.scope_totals(self.con, {"claude:sess-stream"})
        again = claude.import_claude_file(
            self.con, fixture("claude-streaming.jsonl"), full=True)
        self.assertEqual(again["responses_inserted"], 0)
        after = report.scope_totals(self.con, {"claude:sess-stream"})
        self.assertEqual(after["total_tokens"], before["total_tokens"])
        row = self.query(
            "SELECT output_tokens FROM responses"
            " WHERE response_id='claude:msg-stream'")[0]
        self.assertEqual(row["output_tokens"], 50)

    def test_missing_buckets_stay_null_with_null_total(self):
        stats = claude.import_claude_file(
            self.con, fixture("claude-missing-buckets.jsonl"))
        self.assertEqual(stats["responses_inserted"], 2)
        partial = self.query(
            "SELECT input_tokens, cached_input_tokens,"
            " cache_write_input_tokens, output_tokens, total_tokens"
            " FROM responses WHERE response_id='claude:msg-partial-buckets'")[0]
        self.assertEqual(partial["input_tokens"], 7)
        self.assertEqual(partial["output_tokens"], 9)
        self.assertIsNone(partial["cached_input_tokens"])
        self.assertIsNone(partial["cache_write_input_tokens"])
        self.assertIsNone(partial["total_tokens"])
        full = self.query(
            "SELECT total_tokens FROM responses"
            " WHERE response_id='claude:msg-full-buckets'")[0]
        self.assertEqual(full["total_tokens"], 11)

    def test_lists_scalars_and_unknown_types_are_quarantined(self):
        stats = claude.import_claude_file(
            self.con, fixture("claude-shapes.jsonl"))
        self.assertEqual(stats["malformed"], 3)
        self.assertEqual(
            self.query("SELECT total_tokens t FROM responses"
                       " WHERE response_id='claude:msg-sh-1'")[0]["t"], 10)
        self.assertEqual(
            self.query("SELECT kind FROM submissions"
                       " WHERE native_id='claude:u-sh-1'")[0]["kind"],
            "genuine")
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM import_errors")[0]["n"], 3)


class ClaudeExcerptTest(LedgerCase):
    SECRETS = ("SECRET-CLAUDE-SYNTH-fedcba987654",
               "SECRET-CLAUDE-SIDE-c0ffee11aa22",
               "SECRET-CLAUDE-SKILL-deadbeef0011",
               "SECRET-CLAUDE-PREFS-9a8b7c6d5e4f",
               "SECRET-CLAUDE-CMD-5f6e7d8c9b0a",
               "SECRET-CLAUDE-META-112233445566")

    def test_only_genuine_and_interrupt_input_keep_excerpts(self):
        claude.import_claude_file(self.con, fixture("claude-secrets.jsonl"))
        claude.import_claude_file(
            self.con, fixture("claude-secrets-side.jsonl"))
        rows = {r["native_id"]: r for r in self.query(
            "SELECT native_id, kind, text_excerpt FROM submissions")}
        self.assertEqual(rows["claude:u-sec-1"]["kind"], "genuine")
        self.assertEqual(len(rows["claude:u-sec-1"]["text_excerpt"]), 300)
        self.assertEqual(rows["claude:u-sec-int"]["kind"], "interrupt")
        self.assertEqual(len(rows["claude:u-sec-int"]["text_excerpt"]), 300)
        self.assertTrue(rows["claude:u-sec-int"]["text_excerpt"].startswith(
            "[Request interrupted"))
        self.assertEqual(rows["claude:su-sec-1"]["kind"], "synthetic")
        self.assertEqual(rows["claude:u-sec-skill"]["kind"], "scaffolding")
        self.assertEqual(rows["claude:u-sec-cmd"]["kind"], "command")
        self.assertEqual(rows["claude:u-sec-meta"]["kind"], "scaffolding")
        self.assertEqual(rows["claude:ssu-sec-1"]["kind"], "synthetic")
        for native in ("claude:su-sec-1", "claude:u-sec-skill",
                       "claude:u-sec-cmd", "claude:u-sec-meta",
                       "claude:ssu-sec-1"):
            self.assertEqual(rows[native]["text_excerpt"], "", native)
        blob = _excerpts(self)
        blob += "".join(r["detail_json"] or "" for r in self.query(
            "SELECT detail_json FROM events"))
        blob += "".join(r["identity_json"] or "" for r in self.query(
            "SELECT identity_json FROM sessions"))
        for secret in self.SECRETS:
            self.assertNotIn(secret, blob)

    def test_identity_still_reads_full_skill_body_text(self):
        claude.import_claude_file(self.con, fixture("claude-secrets.jsonl"))
        row = self.query(
            "SELECT instructions_sha256, preferences_sha256 FROM sessions"
            " WHERE session_key='claude:sess-secrets'")[0]
        self.assertEqual(row["instructions_sha256"],
                         "claude-secret-instr-sha")
        self.assertEqual(row["preferences_sha256"],
                         "claude-secret-prefs-sha")


def _write_lines(path, lines):
    with open(path, "w") as fh:
        fh.writelines(lines)


def _append_lines(path, lines):
    with open(path, "a") as fh:
        fh.writelines(lines)


def _row(case, rid):
    rows = case.query(
        "SELECT input_tokens, cached_input_tokens, cache_write_input_tokens,"
        " output_tokens, reasoning_output_tokens, total_tokens"
        " FROM responses WHERE response_id=?", (rid,))
    assert len(rows) == 1, rid
    return dict(rows[0])


class ClaudeIncrementalTest(LedgerCase):
    """Finality must survive append-only incremental imports: partial blocks
    update across imports, and once the native final block lands, every later
    same-ID duplicate is validation only, even without a stop_reason."""

    def test_partial_final_and_conflict_across_separate_imports(self):
        with open(fixture("claude-incr.jsonl")) as fh:
            lines = fh.readlines()
        dst = os.path.join(self.tmp.name, "sess-incr.jsonl")
        _write_lines(dst, lines[0:2])
        first = claude.import_claude_file(self.con, dst)
        self.assertEqual(first["responses_inserted"], 1)
        self.assertEqual(_row(self, "claude:msg-incr")["output_tokens"], 12)

        _append_lines(dst, lines[2:3])
        second = claude.import_claude_file(self.con, dst)
        self.assertFalse(second["unchanged"])
        # A later non-final block updates the same row across imports.
        self.assertEqual(_row(self, "claude:msg-incr")["output_tokens"], 40)
        self.assertEqual(
            _row(self, "claude:msg-incr")["reasoning_output_tokens"], 15)

        _append_lines(dst, lines[3:4])
        claude.import_claude_file(self.con, dst)
        self.assertEqual(_row(self, "claude:msg-incr"), {
            "input_tokens": 10, "cached_input_tokens": 1000,
            "cache_write_input_tokens": 100, "output_tokens": 50,
            "reasoning_output_tokens": 20, "total_tokens": 1160})

        _append_lines(dst, lines[4:7])
        last = claude.import_claude_file(self.con, dst)
        # An exact repeat without stop_reason is a validation no-op; the
        # conflicting post-final repeat is quarantined without overwriting;
        # the later valid message still imports.
        self.assertEqual(last["malformed"], 1)
        self.assertEqual(last["responses_inserted"], 1)
        self.assertEqual(_row(self, "claude:msg-incr")["output_tokens"], 50)
        self.assertEqual(_row(self, "claude:msg-next")["total_tokens"], 1135)
        errors = self.query("SELECT error FROM import_errors")
        self.assertEqual(len(errors), 1)
        self.assertIn("msg-incr", errors[0]["error"])
        totals = report.scope_totals(self.con, {"claude:sess-incr"})
        self.assertEqual(totals["total_tokens"], 1160 + 1135)
        self.assertEqual(totals["responses"], 2)

    def test_copied_snapshot_replays_to_the_same_final_row(self):
        first = claude.import_claude_file(
            self.con, fixture("claude-streaming.jsonl"))
        self.assertEqual(first["responses_inserted"], 2)
        dst = os.path.join(self.tmp.name, "copy.jsonl")
        shutil.copy(fixture("claude-streaming.jsonl"), dst)
        again = claude.import_claude_file(self.con, dst)
        self.assertEqual(again["responses_inserted"], 0)
        # The replayed conflict is still quarantined, never applied.
        self.assertEqual(_row(self, "claude:msg-stream")["output_tokens"], 50)
        totals = report.scope_totals(self.con, {"claude:sess-stream"})
        self.assertEqual(totals["total_tokens"], 1160 + 1135)


class CodexMixedTransitionTest(LedgerCase):
    def test_uncovered_legacy_checkpoints_survive_partial_transition(self):
        stats = self.sync("codex-mixed.jsonl")
        self.assertEqual(stats["responses_inserted"], 5)
        self.assertEqual(stats["malformed"], 0)
        rows = {r["response_id"]: r for r in self.query(
            "SELECT response_id, is_overlap FROM responses")}
        # The authoritative spans [1120,1620] and [1620,1920] cover none of
        # the legacy cumulative points 450, 1120, 2500, so every fallback
        # checkpoint stays counted alongside the authoritative responses.
        for rid in ("codex:sess-fixture-mixed-01:tc:450",
                    "codex:sess-fixture-mixed-01:tc:1120",
                    "codex:sess-fixture-mixed-01:tc:2500",
                    "codex:resp-mix-001", "codex:resp-mix-002"):
            self.assertIn(rid, rows, rid)
            self.assertEqual(rows[rid]["is_overlap"], 0, rid)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 450 + 670 + 580 + 500 + 300)
        self.assertEqual(totals["responses"], 5)
        self.assertEqual(totals["overlap_responses"], 0)

    def test_whole_transition_still_fully_reconciles(self):
        self.sync("codex-transition-a.jsonl")
        self.sync("codex-transition-b.jsonl")
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 1100 + 2250)
        self.assertEqual(totals["responses"], 2)
        self.assertEqual(totals["overlap_responses"], 2)
