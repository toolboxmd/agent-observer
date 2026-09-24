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
        errors = self.query("SELECT error, line_excerpt FROM import_errors")
        self.assertEqual(len(errors), 1)
        # Fixed safe category only: no response IDs, counters or record
        # values persist in the quarantined error.
        self.assertEqual(errors[0]["error"], "usage_conflict")
        self.assertNotIn("resp-conf-001", errors[0]["error"])
        self.assertNotIn("999", errors[0]["error"])
        self.assertNotIn("999", errors[0]["line_excerpt"] or "")
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
                         "4da603d140e84bb15f9b4ad76404b1f55c385f19897c025e52236d8ad75b5b6a")
        self.assertEqual(row["preferences_sha256"],
                         "e04fc09a7a517327ef3f26084f199efcae9ba98552e6590e60e8a3302d51029f")


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
        errors = self.query("SELECT error, line_excerpt FROM import_errors")
        self.assertEqual(len(errors), 1)
        # Fixed safe category only: no message IDs or counters persist.
        self.assertEqual(errors[0]["error"], "usage_conflict")
        self.assertNotIn("msg-stream", errors[0]["error"])
        self.assertNotIn("999", errors[0]["error"])

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

    def test_only_genuine_input_keeps_an_excerpt(self):
        claude.import_claude_file(self.con, fixture("claude-secrets.jsonl"))
        claude.import_claude_file(
            self.con, fixture("claude-secrets-side.jsonl"))
        rows = {r["native_id"]: r for r in self.query(
            "SELECT native_id, kind, text_excerpt FROM submissions")}
        self.assertEqual(rows["claude:u-sec-1"]["kind"], "genuine")
        self.assertEqual(len(rows["claude:u-sec-1"]["text_excerpt"]), 300)
        self.assertEqual(rows["claude:u-sec-int"]["kind"], "interrupt")
        # Interrupt text is never stored, though the kind still marks the
        # turn for the human-correction detector.
        self.assertEqual(rows["claude:u-sec-int"]["text_excerpt"], "")
        self.assertEqual(rows["claude:su-sec-1"]["kind"], "synthetic")
        self.assertEqual(rows["claude:u-sec-skill"]["kind"], "scaffolding")
        self.assertEqual(rows["claude:u-sec-cmd"]["kind"], "command")
        self.assertEqual(rows["claude:u-sec-meta"]["kind"], "scaffolding")
        self.assertEqual(rows["claude:ssu-sec-1"]["kind"], "synthetic")
        for native in ("claude:su-sec-1", "claude:u-sec-skill",
                       "claude:u-sec-cmd", "claude:u-sec-meta",
                       "claude:ssu-sec-1", "claude:u-sec-int"):
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
                         "f43c159a1b54439c6db873ed19dc016aef9de09bbcbb770b4312ac1962902eaa")
        self.assertEqual(row["preferences_sha256"],
                         "4ece7d8eb5e77f270aff2ed3912700867a42d20999bb493fa07af5fd19c27e86")


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
        # Fixed safe category only: no message IDs persist.
        self.assertEqual(errors[0]["error"], "usage_conflict")
        self.assertNotIn("msg-incr", errors[0]["error"])
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
        # Two counter semantics share this scope (usage records beside
        # legacy token_count checkpoints), so no top-level raw bucket may
        # sum across them; each semantics keeps its own complete total.
        for bucket in ("input_tokens", "cached_input_tokens",
                       "cache_write_input_tokens", "output_tokens",
                       "reasoning_output_tokens", "total_tokens"):
            self.assertNotIn(bucket, totals, bucket)
        by = totals["by_semantics"]
        self.assertEqual(
            by["codex:input_includes_cached,output_includes_reasoning"]
            ["total_tokens"], 500 + 300)
        self.assertEqual(
            by["codex:input_includes_cached,output_includes_reasoning"
               ";source=token_count"]["total_tokens"], 450 + 670 + 580)
        self.assertEqual(totals["responses"], 5)
        self.assertEqual(totals["overlap_responses"], 0)

    def test_whole_transition_still_fully_reconciles(self):
        self.sync("codex-transition-a.jsonl")
        self.sync("codex-transition-b.jsonl")
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 1100 + 2250)
        self.assertEqual(totals["responses"], 2)
        self.assertEqual(totals["overlap_responses"], 2)


class ClaudeDurableFinalizationTest(LedgerCase):
    """Per-response finality persists across imports and source paths."""

    def test_stale_copied_source_never_overwrites_final_row(self):
        first = claude.import_claude_file(
            self.con, fixture("claude-streaming.jsonl"))
        self.assertEqual(first["responses_inserted"], 2)
        self.assertEqual(_row(self, "claude:msg-stream")["output_tokens"], 50)
        # A stale copy from another path carries only a partial non-final
        # block for the already-finalized response, plus one later valid
        # message that must still import.
        stale = claude.import_claude_file(
            self.con, fixture("claude-stale-copy.jsonl"))
        self.assertEqual(stale["malformed"], 1)
        self.assertEqual(stale["responses_inserted"], 1)
        self.assertEqual(_row(self, "claude:msg-stream"), {
            "input_tokens": 10, "cached_input_tokens": 1000,
            "cache_write_input_tokens": 100, "output_tokens": 50,
            "reasoning_output_tokens": 20, "total_tokens": 1160})
        self.assertEqual(
            self.query("SELECT total_tokens t FROM responses"
                       " WHERE response_id='claude:msg-stale-after'")[0]["t"],
            5 + 0 + 1100 + 30)
        errors = self.query("SELECT error FROM import_errors ORDER BY id")
        # One conflict from the original streaming replay plus one from the
        # stale partial: both fixed categories, never the message ID.
        self.assertEqual(len(errors), 2)
        for row in errors:
            self.assertEqual(row["error"], "usage_conflict")
            self.assertNotIn("msg-stream", row["error"])

    def test_malformed_final_never_finalizes_and_valid_final_follows(self):
        stats = claude.import_claude_file(
            self.con, fixture("claude-malformed-final.jsonl"))
        # One malformed final quarantined; the later valid final for the
        # same response plus the trailing message still import.
        self.assertEqual(stats["malformed"], 1)
        self.assertEqual(stats["responses_inserted"], 2)
        self.assertEqual(_row(self, "claude:msg-mf-1"), {
            "input_tokens": 10, "cached_input_tokens": 1000,
            "cache_write_input_tokens": 100, "output_tokens": 50,
            "reasoning_output_tokens": 20, "total_tokens": 1160})
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM responses"
                       " WHERE response_id='claude:msg-mf-after'")[0]["n"], 1)
        errors = self.query("SELECT error FROM import_errors")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"], "malformed_usage")
        self.assertNotIn("msg-mf-1", errors[0]["error"])


class ClaudeMalformedUsageTest(LedgerCase):
    def test_falsey_and_typed_buckets_quarantine_before_sql(self):
        stats = claude.import_claude_file(
            self.con, fixture("claude-malformed-usage.jsonl"))
        # Six malformed usage blocks (string, boolean, list, empty string,
        # bad details, boolean thinking) quarantine before any response SQL;
        # the later valid message still imports.
        self.assertEqual(stats["malformed"], 6)
        self.assertEqual(stats["responses_inserted"], 1)
        for rid in ("claude:msg-bad-str", "claude:msg-bad-bool",
                    "claude:msg-bad-list", "claude:msg-bad-empty",
                    "claude:msg-bad-details", "claude:msg-bad-think"):
            self.assertEqual(
                self.query("SELECT COUNT(*) n FROM responses"
                           " WHERE response_id=?", (rid,))[0]["n"], 0, rid)
        self.assertEqual(
            self.query("SELECT total_tokens t FROM responses"
                       " WHERE response_id='claude:msg-mu-good'")[0]["t"], 10)
        errors = self.query("SELECT error FROM import_errors")
        self.assertEqual(len(errors), 6)
        for row in errors:
            self.assertEqual(row["error"], "malformed_usage")
            self.assertNotIn("msg-bad", row["error"])


class CodexMalformedUsageTest(LedgerCase):
    def test_falsey_and_typed_buckets_quarantine_before_sql(self):
        stats = self.sync("codex-malformed-usage.jsonl")
        # Five malformed buckets (string counter, boolean counter, list
        # usage, empty-string usage, string turn bucket) quarantine before
        # any response SQL; the later valid record still imports.
        self.assertEqual(stats["malformed"], 5)
        self.assertEqual(stats["responses_inserted"], 1)
        for rid in ("codex:resp-mu-bad-str", "codex:resp-mu-bad-bool",
                    "codex:resp-mu-bad-list", "codex:resp-mu-bad-empty",
                    "codex:resp-mu-bad-turn"):
            self.assertEqual(
                self.query("SELECT COUNT(*) n FROM responses"
                           " WHERE response_id=?", (rid,))[0]["n"], 0, rid)
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM responses"
                       " WHERE response_id='codex:resp-mu-good-after'")[0]["n"],
            1)
        errors = self.query("SELECT error FROM import_errors")
        self.assertEqual(len(errors), 5)
        for row in errors:
            self.assertEqual(row["error"], "malformed_usage")
            self.assertNotIn("resp-mu", row["error"])


class CodexFallbackConflictTest(LedgerCase):
    def test_same_total_with_changed_counter_is_quarantined(self):
        self.sync("codex-legacy-a.jsonl")
        before = self.query(
            "SELECT input_tokens, total_tokens, thread_total_tokens"
            " FROM responses WHERE response_id=?",
            (f"{LEGACY}:tc:450",))[0]
        self.assertEqual(before["input_tokens"], 400)
        stats = self.sync("codex-legacy-conflict.jsonl")
        # Same cumulative 450 with a changed fallback input counter is a
        # usage conflict; the later valid 2110 checkpoint still imports.
        self.assertEqual(stats["malformed"], 1)
        self.assertEqual(stats["responses_inserted"], 1)
        after = self.query(
            "SELECT input_tokens, total_tokens, thread_total_tokens"
            " FROM responses WHERE response_id=?",
            (f"{LEGACY}:tc:450",))[0]
        self.assertEqual(dict(after), dict(before))
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM responses WHERE response_id=?",
                       (f"{LEGACY}:tc:2110",))[0]["n"], 1)
        errors = self.query(
            "SELECT error, line_excerpt FROM import_errors")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"], "usage_conflict")
        self.assertNotIn("450", errors[0]["error"])
        # Deferred fallback errors keep structure only: the sorted
        # top-level key names of the record, never values.
        self.assertEqual(errors[0]["line_excerpt"],
                         "ordinal,payload,timestamp,type")
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 450 + 670 + 990)
        self.assertEqual(totals["responses"], 3)

    def test_conflict_fixture_standalone_contains_one_same_total_conflict(self):
        # The fixture is self-contained: importing it alone quarantines
        # exactly one same-total accounting conflict, so the genuine-conflict
        # requirement does not depend on cross-file import order.
        stats = self.sync("codex-legacy-conflict.jsonl")
        self.assertEqual(stats["malformed"], 1)
        self.assertEqual(stats["responses_inserted"], 2)
        errors = self.query("SELECT error FROM import_errors")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"], "usage_conflict")


class CodexPostCompactionFallbackTest(LedgerCase):
    """Post-compaction repeats of a cumulative checkpoint are duplicate
    evidence when only last-token metadata changed; genuine accounting
    changes still quarantine."""

    DUP = "codex:sess-fixture-comp-01"
    CONFLICT = "codex:sess-fixture-comp-02"

    def test_post_compaction_same_total_changed_last_is_ignored(self):
        stats = self.sync("codex-compaction-dup.jsonl")
        self.assertEqual(stats["malformed"], 0)
        self.assertEqual(stats["responses_inserted"], 3)
        rows = {r["response_id"]: r for r in self.query(
            "SELECT response_id, input_tokens, total_tokens,"
            " thread_total_tokens FROM responses")}
        # No extra response for the repeated checkpoint, and the stored row
        # keeps the first checkpoint's counters: nothing counted twice.
        self.assertEqual(
            sorted(rows),
            [f"{self.DUP}:tc:1120", f"{self.DUP}:tc:2110",
             f"{self.DUP}:tc:450"])
        self.assertEqual(rows[f"{self.DUP}:tc:1120"]["input_tokens"], 600)
        self.assertEqual(rows[f"{self.DUP}:tc:1120"]["total_tokens"], 670)
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM import_errors")[0]["n"], 0)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 450 + 670 + 990)
        self.assertEqual(totals["responses"], 3)

    def test_post_compaction_ignore_is_idempotent(self):
        self.sync("codex-compaction-dup.jsonl")
        again = import_codex_file(
            self.con, fixture("codex-compaction-dup.jsonl"), full=True)
        self.assertEqual(again["responses_inserted"], 0)
        self.assertEqual(again["malformed"], 0)
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM import_errors")[0]["n"], 0)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 450 + 670 + 990)
        self.assertEqual(totals["responses"], 3)

    def test_post_compaction_changed_cumulative_bucket_stays_quarantined(self):
        stats = self.sync("codex-compaction-conflict.jsonl")
        # Same cumulative identity after a compaction boundary, but the
        # cumulative bucket itself changed: a genuine usage conflict, even
        # though the repeat arrived after compaction.
        self.assertEqual(stats["malformed"], 1)
        self.assertEqual(stats["responses_inserted"], 3)
        errors = self.query("SELECT error FROM import_errors")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"], "usage_conflict")
        row = self.query(
            "SELECT input_tokens, total_tokens FROM responses"
            " WHERE response_id=?",
            (f"{self.CONFLICT}:tc:1120",))[0]
        self.assertEqual(row["input_tokens"], 600)
        self.assertEqual(row["total_tokens"], 670)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 450 + 670 + 990)
        self.assertEqual(totals["responses"], 3)

    def test_pre_compaction_conflict_before_later_compaction_stays_quarantined(self):
        # The changed same-total repeat arrives before the compaction
        # boundary in the same file: a genuine pre-compaction conflict. The
        # later compaction must not suppress it.
        stats = self.sync("codex-precompaction-conflict.jsonl")
        self.assertEqual(stats["malformed"], 1)
        self.assertEqual(stats["responses_inserted"], 2)
        errors = self.query("SELECT error FROM import_errors")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"], "usage_conflict")
        row = self.query(
            "SELECT input_tokens, total_tokens FROM responses"
            " WHERE response_id='codex:sess-fixture-comp-03:tc:450'")[0]
        self.assertEqual(row["input_tokens"], 400)
        self.assertEqual(row["total_tokens"], 450)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 450 + 990)
        self.assertEqual(totals["responses"], 2)

    def test_incremental_later_compaction_does_not_rewrite_conflict(self):        # Same file imported as a growing log: the pre-compaction conflict
        # quarantines in the first prefix, and the later compaction plus the
        # rising checkpoint in the appended prefix change nothing about it.
        with open(fixture("codex-precompaction-conflict.jsonl")) as fh:
            lines = fh.readlines()
        dst = os.path.join(self.tmp.name, "preconf-grow.jsonl")
        _write_lines(dst, lines[0:4])
        first = import_codex_file(self.con, dst)
        self.assertEqual(first["malformed"], 1)
        self.assertEqual(first["responses_inserted"], 1)
        _append_lines(dst, lines[4:])
        second = import_codex_file(self.con, dst)
        self.assertFalse(second["unchanged"])
        self.assertEqual(second["malformed"], 0)
        self.assertEqual(second["responses_inserted"], 1)
        errors = self.query("SELECT error FROM import_errors")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"], "usage_conflict")
        row = self.query(
            "SELECT input_tokens, total_tokens FROM responses"
            " WHERE response_id LIKE '%:tc:450'")[0]
        self.assertEqual(row["input_tokens"], 400)
        self.assertEqual(row["total_tokens"], 450)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 450 + 990)
        self.assertEqual(totals["responses"], 2)


    def test_split_import_suffix_starting_with_post_compaction_repeat(self):
        # The first import ends before the post-compaction repeat and the
        # second starts with it. The persisted cumulative signature makes
        # the suffix behave exactly like a full import: the repeat is
        # duplicate evidence across all cumulative counters, not a
        # usage_conflict, and only the rising checkpoint inserts.
        with open(fixture("codex-compaction-dup.jsonl")) as fh:
            lines = fh.readlines()
        dst = os.path.join(self.tmp.name, "split-grow.jsonl")
        _write_lines(dst, lines[0:5])
        first = import_codex_file(self.con, dst)
        self.assertEqual(first["responses_inserted"], 2)
        self.assertEqual(first["malformed"], 0)
        _append_lines(dst, lines[5:])
        second = import_codex_file(self.con, dst)
        self.assertFalse(second["unchanged"])
        self.assertEqual(second["malformed"], 0)
        self.assertEqual(second["responses_inserted"], 1)
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM import_errors")[0]["n"], 0)
        rows = {r["response_id"]: r for r in self.query(
            "SELECT response_id, input_tokens, total_tokens,"
            " thread_total_tokens FROM responses")}
        self.assertEqual(
            sorted(rows),
            [f"{self.DUP}:tc:1120", f"{self.DUP}:tc:2110",
             f"{self.DUP}:tc:450"])
        # The repeated checkpoint kept the first import's counters.
        self.assertEqual(rows[f"{self.DUP}:tc:1120"]["input_tokens"], 600)
        self.assertEqual(rows[f"{self.DUP}:tc:1120"]["total_tokens"], 670)
        totals = report.scope_totals(self.con)
        self.assertEqual(totals["total_tokens"], 450 + 670 + 990)
        self.assertEqual(totals["responses"], 3)
        # All six cumulative counters persisted per total, not only the
        # total or the last-token metadata.
        sigs = {r["thread_total"]: r for r in self.query(
            "SELECT thread_total, input_tokens, cached_input_tokens,"
            " cache_write_input_tokens, output_tokens,"
            " reasoning_output_tokens, total_tokens"
            " FROM codex_fallback_cumulative"
            " WHERE session_key=?", (self.DUP,))}
        self.assertEqual(sorted(sigs), [450, 1120, 2110])
        self.assertEqual(
            {k: sigs[1120][k] for k in (
                "input_tokens", "cached_input_tokens",
                "cache_write_input_tokens", "output_tokens",
                "reasoning_output_tokens", "total_tokens")},
            {"input_tokens": 1000, "cached_input_tokens": 100,
             "cache_write_input_tokens": 0, "output_tokens": 120,
             "reasoning_output_tokens": 12, "total_tokens": 1120})

    def test_split_import_changed_cumulative_bucket_stays_quarantined(self):
        # Same split shape, but the suffix repeat changes the cumulative
        # bucket: the persisted signature proves a genuine accounting
        # change and the repeat quarantines as usage_conflict.
        with open(fixture("codex-compaction-conflict.jsonl")) as fh:
            lines = fh.readlines()
        dst = os.path.join(self.tmp.name, "split-conflict.jsonl")
        _write_lines(dst, lines[0:5])
        first = import_codex_file(self.con, dst)
        self.assertEqual(first["malformed"], 0)
        _append_lines(dst, lines[5:])
        second = import_codex_file(self.con, dst)
        self.assertEqual(second["malformed"], 1)
        errors = self.query(
            "SELECT error, line_excerpt FROM import_errors")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"], "usage_conflict")
        self.assertEqual(errors[0]["line_excerpt"],
                         "ordinal,payload,timestamp,type")


class ClaudeServiceTierTest(LedgerCase):
    def test_string_service_tier_does_not_quarantine_valid_response(self):
        stats = claude.import_claude_file(
            self.con, fixture("claude-service-tier.jsonl"))
        self.assertEqual(stats["malformed"], 0)
        self.assertEqual(stats["responses_inserted"], 1)
        self.assertEqual(_row(self, "claude:msg-service-tier"), {
            "input_tokens": 10, "cached_input_tokens": 1000,
            "cache_write_input_tokens": 100, "output_tokens": 50,
            "reasoning_output_tokens": 20, "total_tokens": 1160})


class ClaudeMalformedPrefixIncrementalTest(LedgerCase):
    """A malformed final prefix never finalizes an absent response row."""

    def test_malformed_final_then_valid_across_incremental_imports(self):
        dst = os.path.join(self.tmp.name, "sess-prefix.jsonl")
        _write_lines(dst, [
            '{"sessionId": "sess-prefix", "cwd": "/redacted/repo",'
            ' "version": "2.1.280", "isSidechain": false, "type": "user",'
            ' "uuid": "u-p1", "promptId": "p-p1", "origin": {"kind": "human"},'
            ' "promptSource": "typed", "timestamp": "2026-09-14T10:00:01Z",'
            ' "message": {"role": "user", "content": "Check prefix handling"}}\n',
            '{"sessionId": "sess-prefix", "cwd": "/redacted/repo",'
            ' "version": "2.1.280", "isSidechain": false, "type": "assistant",'
            ' "uuid": "as-p-bad", "requestId": "req-p",'
            ' "timestamp": "2026-09-14T10:00:02Z",'
            ' "message": {"id": "msg-prefix", "model": "claude-fable-5-1",'
            ' "stop_reason": "end_turn",'
            ' "usage": {"service_tier": "standard"}, "content": []}}\n',
        ])
        first = claude.import_claude_file(self.con, dst)
        self.assertEqual(first["malformed"], 1)
        self.assertEqual(first["responses_inserted"], 0)
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM responses"
                       " WHERE response_id='claude:msg-prefix'")[0]["n"], 0)
        _append_lines(dst, [
            '{"sessionId": "sess-prefix", "cwd": "/redacted/repo",'
            ' "version": "2.1.280", "isSidechain": false, "type": "assistant",'
            ' "uuid": "as-p-good", "requestId": "req-p",'
            ' "timestamp": "2026-09-14T10:00:03Z",'
            ' "message": {"id": "msg-prefix", "model": "claude-fable-5-1",'
            ' "stop_reason": "end_turn",'
            ' "usage": {"input_tokens": 10,'
            ' "cache_creation_input_tokens": 100,'
            ' "cache_read_input_tokens": 1000, "output_tokens": 50,'
            ' "output_tokens_details": {"thinking_tokens": 20}},'
            ' "content": []}}\n',
        ])
        second = claude.import_claude_file(self.con, dst)
        self.assertEqual(second["responses_inserted"], 1)
        self.assertEqual(second["malformed"], 0)
        self.assertEqual(_row(self, "claude:msg-prefix"), {
            "input_tokens": 10, "cached_input_tokens": 1000,
            "cache_write_input_tokens": 100, "output_tokens": 50,
            "reasoning_output_tokens": 20, "total_tokens": 1160})
        errors = self.query(
            "SELECT error, line_excerpt FROM import_errors ORDER BY id")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"], "malformed_usage")
        self.assertNotIn("msg-prefix", errors[0]["error"])
        self.assertNotIn("standard", errors[0]["line_excerpt"] or "")
