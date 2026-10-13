"""`event search`: read-only word search over one machine's ledger (#70).

A capture judge on another machine calls it over SSH to check whether a
lesson recurred, so it must never write, and an empty result must be
distinguishable from a failure.
"""

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agent_observer import cli, db

T0 = 1_760_000_000.0  # 2025-10-09T08:53:20Z


def _digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class EventSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "observer.db")
        con = db.connect(self.path)
        db.init_db(con)
        con.execute("INSERT INTO sources(id, harness, path, sha256, imported_at)"
                    " VALUES(1, 'codex', '/r/a.jsonl', 'x', ?)", (T0 + 900,))
        for key, harness, project in (
                ("codex:s1", "codex", "/dev/alpha"),
                ("claude:s2", "claude", "/dev/beta")):
            con.execute("INSERT INTO sessions(session_key, harness, native_id,"
                        " project_dir, updated_at) VALUES(?,?,?,?,?)",
                        (key, harness, key.split(":")[1], project, T0))
        events = (
            # id, session, ts, family, name, target, detail
            (10, "codex:s1", T0 + 10, "tool_call", "exec",
             "sqlite3 observer.db 'PRAGMA wal_checkpoint'", None),
            (11, "codex:s1", T0 + 20, "assistant_message", "assistant_message",
             None, {"excerpt": "The WAL Checkpoint ran in Zürich before sync."}),
            (12, "claude:s2", T0 + 30, "tool_call", "Bash",
             "pytest -k flaky_wal_checkpoint", None),
            (13, "claude:s2", T0 + 40, "read", "notes.md",
             "/dev/beta/100%_done.md", None),
            (14, "claude:s2", T0 + 50, "tool_result", "Bash",
             "echo unrelated", {"exit_code": 0}),
        )
        for eid, key, ts, family, name, target, detail in events:
            con.execute("INSERT INTO events(id, source_id, session_key, ts,"
                        " family, native_id, name, target, detail_json)"
                        " VALUES(?,1,?,?,?,?,?,?,?)",
                        (eid, key, ts, family, f"n{eid}", name, target,
                         json.dumps(detail) if detail else None))
        con.execute("INSERT INTO user_messages(native_id, source_id, session_key,"
                    " ts, kind, text_hash, text_excerpt, is_genuine)"
                    " VALUES(?,?,?,?,?,?,?,1)",
                    ("msg-1", 1, "claude:s2", T0 + 60, "genuine", "h",
                     "Stop: never run a wal checkpoint on the live ledger."))
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def search(self, *argv, path=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--db", path or self.path, "event", "search",
                             *argv, "--json"])
        return code, (json.loads(out.getvalue()) if out.getvalue() else None), \
            err.getvalue()

    def ids(self, payload):
        return [(m["type"], m["id"]) for m in payload["matches"]]

    def test_all_words_match_case_insensitively_newest_first(self):
        code, payload, _ = self.search("wal", "CHECKPOINT")
        self.assertEqual(code, 0)
        self.assertEqual(self.ids(payload), [
            ("user_message", "msg-1"), ("event", 12), ("event", 11),
            ("event", 10)])
        self.assertEqual(payload["total"], 4)
        self.assertFalse(payload["truncated"])
        first = payload["matches"][1]
        self.assertEqual(first["session"], "claude:s2")
        self.assertEqual(first["family"], "tool_call")
        self.assertEqual(first["at"], "2025-10-09T08:53:50Z")
        self.assertIn("flaky_wal_checkpoint", first["anchor"])

    def test_assistant_excerpts_match_beyond_ascii(self):
        self.assertEqual(self.ids(self.search("ZÜRICH")[1]), [("event", 11)])
        code, payload, _ = self.search("zürich")
        self.assertEqual(code, 0)
        self.assertEqual(self.ids(payload), [("event", 11)])
        self.assertIn("Zürich", payload["matches"][0]["anchor"])

    def test_like_wildcards_in_words_are_literal(self):
        self.assertEqual(self.ids(self.search("100%_done")[1]), [("event", 13)])
        self.assertEqual(self.ids(self.search("100%x")[1]), [])
        self.assertEqual(self.ids(self.search("10_%")[1]), [])

    def test_filters_narrow_the_search(self):
        def found(*argv):
            return self.ids(self.search("checkpoint", *argv)[1])
        self.assertEqual(found("--harness", "codex"),
                         [("event", 11), ("event", 10)])
        self.assertEqual(found("--project", "beta"),
                         [("user_message", "msg-1"), ("event", 12)])
        self.assertEqual(found("--since", str(T0 + 15), "--until",
                               str(T0 + 35)), [("event", 12), ("event", 11)])

    def test_limit_reports_the_total_and_truncation(self):
        code, payload, _ = self.search("checkpoint", "--limit", "2")
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["matches"]), 2)
        self.assertEqual(payload["total"], 4)
        self.assertTrue(payload["truncated"])

    def test_empty_result_is_success_with_coverage(self):
        code, payload, err = self.search("nothing-like-this")
        self.assertEqual(code, 0)
        self.assertEqual(payload["matches"], [])
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["ledger_synced_at"], "2025-10-09T09:08:20Z")
        self.assertTrue(payload["machine"])
        self.assertEqual(err, "")

    def test_missing_ledger_is_an_error_and_is_not_created(self):
        absent = os.path.join(self.tmp.name, "absent.db")
        code, payload, err = self.search("wal", path=absent)
        self.assertEqual(code, 2)
        self.assertIsNone(payload)
        self.assertTrue(err)
        self.assertFalse(os.path.exists(absent))

    def test_search_leaves_the_ledger_unchanged_under_a_held_write_lock(self):
        before = _digest(self.path)
        writer = sqlite3.connect(self.path, isolation_level=None, timeout=0)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO tasks(task_id, created_at) VALUES('t', 1)")
        try:
            code, payload, _ = self.search("checkpoint")
        finally:
            writer.execute("ROLLBACK")
            writer.close()
        self.assertEqual(code, 0)
        self.assertEqual(payload["total"], 4)
        self.assertEqual(_digest(self.path), before)


if __name__ == "__main__":
    unittest.main()
