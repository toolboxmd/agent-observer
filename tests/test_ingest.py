"""Incremental source reads: resume on growth, full re-read on rewrite."""

import os
import shutil

from agent_observer import report
from agent_observer.adapters.codex import import_codex_file
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
