"""Incremental source reads: resume on growth, full re-read on rewrite."""

import os
import shutil

from agent_observer import report
from agent_observer.adapters.codex import import_codex_file
from agent_observer.ingest import JsonlSource
from tests.helpers import LedgerCase, fixture


class IncrementalTest(LedgerCase):
    def live_copy(self, name):
        path = os.path.join(self.tmp.name, "rollout-live.jsonl")
        shutil.copy(fixture(name), path)
        return path

    def test_grown_file_resumes_at_its_offset(self):
        path = self.live_copy("codex-growing-a.jsonl")
        first = import_codex_file(self.con, path)
        self.assertFalse(first["incremental"])
        with open(fixture("codex-growing-b.jsonl")) as src:
            grown = src.read()
        with open(fixture("codex-growing-a.jsonl")) as src:
            prefix = src.read()
        self.assertTrue(grown.startswith(prefix))
        with open(path, "w") as out:
            out.write(grown)
        second = import_codex_file(self.con, path)
        self.assertTrue(second["incremental"])
        self.assertEqual(second["responses_inserted"], 1)
        self.assertEqual(report.scope_totals(self.con)["total_tokens"], 3350)

    def test_rewritten_file_is_read_again_without_double_counting(self):
        path = self.live_copy("codex-growing-b.jsonl")
        import_codex_file(self.con, path)
        with open(path) as fh:
            body = fh.read()
        # Same records, different bytes before the recorded offset: the
        # tail hash no longer matches, so the whole file is read again.
        with open(path, "w") as fh:
            fh.write(body.replace("{", "{ ", 1))
        again = import_codex_file(self.con, path)
        self.assertFalse(again["incremental"])
        self.assertEqual(again["responses_inserted"], 0)
        self.assertEqual(again["responses_duplicate"], 2)
        self.assertEqual(report.scope_totals(self.con)["total_tokens"], 3350)

    def test_partial_trailing_line_waits_for_the_next_import(self):
        path = self.live_copy("codex-growing-a.jsonl")
        with open(fixture("codex-growing-a.jsonl")) as src:
            known = set(src.read().splitlines())
        with open(fixture("codex-growing-b.jsonl")) as src:
            new_usage = [line for line in src.read().splitlines()
                         if line not in known and "token_usage_record" in line]
        self.assertEqual(len(new_usage), 1)
        # A harness still writing the record: no trailing newline yet.
        with open(path, "a") as out:
            out.write(new_usage[0])
        import_codex_file(self.con, path)
        self.assertEqual(report.scope_totals(self.con)["total_tokens"], 1100)
        with open(path, "a") as out:
            out.write("\n")
        later = import_codex_file(self.con, path)
        self.assertTrue(later["incremental"])
        self.assertEqual(later["responses_inserted"], 1)
        self.assertEqual(report.scope_totals(self.con)["total_tokens"], 3350)


SECRET = "SECRET-QUARANTINE-9f8e7d6c"


class QuarantineRedactionTest(LedgerCase):
    """Quarantined lines keep structural metadata only, never raw text.

    The fixture holds a malformed non-JSON line and an unknown-type JSON
    line, both carrying a canary secret in tool-output, file-content and
    preference shape, followed by a valid usage record.
    """

    def test_secrets_never_persist_and_valid_records_continue(self):
        stats = self.sync("codex-secrets-quarantine.jsonl")
        self.assertEqual(stats["malformed"], 2)
        # Both valid usage records import: the bad lines destroy nothing
        # and the later record is not skipped.
        self.assertEqual(stats["responses_inserted"], 2)
        self.assertEqual(report.scope_totals(self.con)["total_tokens"], 1540)
        subs = self.query("SELECT native_id FROM submissions")
        self.assertEqual([r["native_id"] for r in subs], ["codex:msg-q-sub-01"])

    def test_excerpts_hold_no_raw_text(self):
        self.sync("codex-secrets-quarantine.jsonl")
        errors = self.query(
            "SELECT error, line_excerpt FROM import_errors ORDER BY ordinal_num")
        self.assertEqual(len(errors), 2)
        for row in errors:
            self.assertNotIn(SECRET, row["line_excerpt"] or "")
            self.assertNotIn(SECRET, row["error"] or "")
            self.assertLessEqual(len(row["line_excerpt"] or ""), 200)
            # The error column holds only a fixed safe category.
            self.assertRegex(row["error"] or "", r"\A[a-z_]{1,40}\Z")
        by_error = {r["error"]: r["line_excerpt"] for r in errors}
        # Invalid JSON keeps only a safe category, no raw excerpt.
        self.assertEqual(by_error["json_error"], "json_error")
        # A JSON line keeps the category plus sorted top-level keys, but
        # no record values: the unknown type value is never retained and
        # payload contents never persist.
        excerpt = next(v for k, v in by_error.items()
                       if k.startswith("schema_error"))
        self.assertIn("schema_error", excerpt)
        self.assertIn("keys=ordinal,payload,timestamp,type", excerpt)
        self.assertNotIn("future_unknown_type", excerpt)

    def test_secret_in_type_and_exception_never_reaches_ledger(self):
        """An unknown type carrying a secret and the exception built from
        it must leave no occurrence in any ledger text."""
        type_secret = "SECRET-TYPE-7c3a9e1b2f"
        payload_secret = "SECRET-PAYLOAD-2b7e9a01"
        exc_secret = "SECRET-EXC-9d4c2f6a"
        stats = self.sync("codex-secret-type-quarantine.jsonl")
        self.assertEqual(stats["malformed"], 1)
        self.assertEqual(stats["responses_inserted"], 2)
        for secret in (type_secret, payload_secret):
            for table, column in (("import_errors", "line_excerpt"),
                                  ("import_errors", "error"),
                                  ("submissions", "text_excerpt"),
                                  ("events", "detail_json"),
                                  ("responses", "model"),
                                  ("responses", "semantics")):
                rows = self.query(f"SELECT {column} FROM {table}")
                for row in rows:
                    self.assertNotIn(secret, row[column] or "",
                                     f"{table}.{column} leaks type/payload secret")
        # The quarantined row keeps only a safe category and key names.
        row = self.query(
            "SELECT error, line_excerpt FROM import_errors")[0]
        self.assertEqual(row["error"], "schema_error")
        self.assertRegex(row["error"], r"\A[a-z_]{1,40}\Z")
        self.assertIn("schema_error", row["line_excerpt"])
        self.assertIn("keys=", row["line_excerpt"])
        self.assertNotIn(type_secret, row["line_excerpt"])
        self.assertNotIn(payload_secret, row["line_excerpt"])
        self.assertLessEqual(len(row["line_excerpt"]), 200)
        # An exception message carrying a secret is reduced to its safe
        # leading category before it reaches the ledger.
        path = os.path.join(self.tmp.name, "direct.jsonl")
        with open(path, "w") as fh:
            fh.write("{}\n")
        src = JsonlSource(self.con, "codex", path)
        list(src.records())
        src.error(0, f"schema_error: boom {exc_secret} leaked", '{"type": "x"}')
        self.con.commit()
        for table, column in (("import_errors", "error"),
                              ("import_errors", "line_excerpt")):
            rows = self.query(f"SELECT {column} FROM {table}")
            for r in rows:
                self.assertNotIn(exc_secret, r[column] or "",
                                 f"{table}.{column} leaks exception secret")
        direct = self.query(
            "SELECT error FROM import_errors ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(direct["error"], "schema_error")

    def test_secret_in_no_ledger_text(self):
        self.sync("codex-secrets-quarantine.jsonl")
        for table, column in (("import_errors", "line_excerpt"),
                              ("import_errors", "error"),
                              ("submissions", "text_excerpt"),
                              ("events", "detail_json")):
            rows = self.query(f"SELECT {column} FROM {table}")
            for row in rows:
                self.assertNotIn(SECRET, row[column] or "")
