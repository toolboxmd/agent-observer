"""Incremental source reads: resume on growth, full re-read on rewrite."""

import os
import shutil

from agent_observer import privacy, report
from agent_observer.adapters.codex import import_codex_file
from agent_observer.ingest import TAIL_BYTES, JsonlSource
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


class UnchangedInvalidationTest(LedgerCase):
    """Size/mtime/inode invalidation for the append-only fast path."""

    def live_copy(self, name):
        path = os.path.join(self.tmp.name, "rollout-live.jsonl")
        shutil.copy(fixture(name), path)
        return path

    def test_same_size_prefix_rewrite_forces_full_reread(self):
        path = self.live_copy("codex-mini.jsonl")
        first = import_codex_file(self.con, path)
        self.assertEqual(first["responses_inserted"], 3)
        before_totals = report.scope_totals(self.con)
        self.assertEqual(before_totals["total_tokens"], 5500)
        with open(path, "rb") as fh:
            body = fh.read()
        self.assertGreater(len(body), TAIL_BYTES)
        # Rewrite one authoritative usage counter in the prefix: the usage
        # bucket is compared against the stored row, so a re-read meets it
        # again as a usage conflict, while a skipped prefix would stay
        # silent. (Sibling checkpoint buckets are never compared.)
        old = (b'"usage":{"cache_write_input_tokens":0,'
               b'"cached_input_tokens":0,"input_tokens":1000')
        new = (b'"usage":{"cache_write_input_tokens":0,'
               b'"cached_input_tokens":0,"input_tokens":1001')
        at = body.find(old)
        # The changed byte sits in the prefix, outside the recorded tail,
        # so a size-plus-tail check alone would call this file unchanged.
        self.assertGreaterEqual(at, 0)
        self.assertLess(at, len(body) - TAIL_BYTES)
        rewritten = body[:at] + new + body[at + len(old):]
        self.assertEqual(len(rewritten), len(body))
        self.assertEqual(rewritten[len(body) - TAIL_BYTES:],
                         body[len(body) - TAIL_BYTES:])
        st_before = os.stat(path)
        with open(path, "wb") as fh:
            fh.write(rewritten)
        if os.stat(path).st_mtime_ns == st_before.st_mtime_ns:
            # A real rewrite always moves mtime; pin the precondition
            # deterministically on coarse filesystems.
            os.utime(path, ns=(st_before.st_atime_ns,
                               st_before.st_mtime_ns + 5_000_000))
        again = import_codex_file(self.con, path)
        # Not unchanged and not incremental: the file was read again whole.
        self.assertFalse(again.get("unchanged"))
        self.assertFalse(again.get("incremental"))
        self.assertEqual(again["responses_inserted"], 0)
        # The rewritten prefix was not skipped: the changed counter meets
        # its stored row again and is quarantined as a usage conflict.
        self.assertEqual(again["malformed"], 1)
        # Accounting stays idempotent: conflicts never overwrite stored rows.
        self.assertEqual(report.scope_totals(self.con), before_totals)

    def test_append_racing_the_unchanged_check_is_imported_same_sync(self):
        path = self.live_copy("codex-growing-a.jsonl")
        first = import_codex_file(self.con, path)
        self.assertEqual(first["responses_inserted"], 1)
        with open(fixture("codex-growing-b.jsonl")) as fh:
            grown_lines = fh.read().splitlines(keepends=True)
        with open(fixture("codex-growing-a.jsonl")) as fh:
            known = set(fh.read().splitlines())
        appended = "".join(line for line in grown_lines
                           if line.rstrip("\n") not in known)
        self.assertTrue(appended)
        self.assertTrue(appended.endswith("\n"))
        orig_recheck = JsonlSource.recheck_unchanged

        def raced(self):
            # Deterministic seam: one valid record lands after the initial
            # check but before the fast-path return. An implementation that
            # only checks once at JsonlSource construction misses it.
            with open(self.path, "a") as fh:
                fh.write(appended)
            return orig_recheck(self)

        JsonlSource.recheck_unchanged = raced
        try:
            second = import_codex_file(self.con, path)
        finally:
            JsonlSource.recheck_unchanged = orig_recheck
        self.assertFalse(second.get("unchanged"))
        self.assertEqual(second["responses_inserted"], 1)
        self.assertEqual(report.scope_totals(self.con)["total_tokens"], 3350)
        # finish() persisted the raced file's metadata, not the stale check.
        st = os.stat(path)
        row = self.con.execute(
            "SELECT size_bytes, read_offset, mtime_ns, ino FROM sources"
            " WHERE harness='codex' AND path=?", (path,)).fetchone()
        self.assertEqual(row["size_bytes"], st.st_size)
        self.assertEqual(row["read_offset"], st.st_size)
        self.assertEqual(row["mtime_ns"], st.st_mtime_ns)
        self.assertEqual(row["ino"], st.st_ino)


class SourceMetadataMigrationTest(LedgerCase):
    """The new size/mtime/inode columns are additive and fail closed."""

    def test_pre_metadata_rows_reread_once_then_resume(self):
        path = os.path.join(self.tmp.name, "rollout-live.jsonl")
        shutil.copy(fixture("codex-mini.jsonl"), path)
        first = import_codex_file(self.con, path)
        self.assertEqual(first["responses_inserted"], 3)
        cols = {row["name"]
                for row in self.con.execute("PRAGMA table_info(sources)")}
        self.assertTrue({"mtime_ns", "ino"} <= cols)
        # Ledgers written before the columns existed keep working: an
        # explicit INSERT naming only the old columns still succeeds.
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES(?,?,?,?)", ("codex", "dummy-legacy-path", "", 0))
        self.con.execute(
            "DELETE FROM sources WHERE path='dummy-legacy-path'")
        # Simulate a pre-metadata row: no mtime/inode on record.
        self.con.execute(
            "UPDATE sources SET mtime_ns=NULL, ino=NULL"
            " WHERE harness='codex' AND path=?", (path,))
        self.con.commit()
        before_totals = report.scope_totals(self.con)
        second = import_codex_file(self.con, path)
        # Fail closed: no unsafe fast path before metadata is refreshed.
        self.assertFalse(second.get("unchanged"))
        self.assertFalse(second.get("incremental"))
        self.assertEqual(second["responses_inserted"], 0)
        self.assertEqual(report.scope_totals(self.con), before_totals)
        # The re-read refreshed the metadata, so the next sync is cheap.
        third = import_codex_file(self.con, path)
        self.assertTrue(third.get("unchanged"))
        self.assertEqual(report.scope_totals(self.con), before_totals)


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
            # The error column holds only a closed privacy category.
            self.assertIn(row["error"],
                          set(privacy.ERROR_CATEGORIES)
                          | {privacy.ERROR_FALLBACK})
        by_error = {r["error"]: r["line_excerpt"] for r in errors}
        # Invalid JSON keeps no excerpt at all, only the category.
        self.assertEqual(by_error["malformed_json"], "")
        # A JSON line keeps only sorted top-level key names, never values:
        # the unknown type value and payload contents never persist.
        self.assertEqual(by_error["unsupported_schema"],
                         "ordinal,payload,timestamp,type")

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
        # The quarantined row keeps only a closed category and key names.
        row = self.query(
            "SELECT error, line_excerpt FROM import_errors")[0]
        self.assertEqual(row["error"], "unsupported_schema")
        self.assertIn(row["error"], privacy.ERROR_CATEGORIES)
        self.assertEqual(row["line_excerpt"],
                         "ordinal,payload,timestamp,type")
        self.assertNotIn(type_secret, row["line_excerpt"])
        self.assertNotIn(payload_secret, row["line_excerpt"])
        self.assertLessEqual(len(row["line_excerpt"]), 200)
        # An exception message carrying a secret is reduced to the fixed
        # fallback before it reaches the ledger: only an exact closed
        # category persists, never a suffixed or decorated one.
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
            "SELECT error, line_excerpt FROM import_errors"
            " ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(direct["error"], privacy.ERROR_FALLBACK)
        self.assertEqual(direct["line_excerpt"], "type")

    def test_secret_in_no_ledger_text(self):
        self.sync("codex-secrets-quarantine.jsonl")
        for table, column in (("import_errors", "line_excerpt"),
                              ("import_errors", "error"),
                              ("submissions", "text_excerpt"),
                              ("events", "detail_json")):
            rows = self.query(f"SELECT {column} FROM {table}")
            for row in rows:
                self.assertNotIn(SECRET, row[column] or "")
