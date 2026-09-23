"""Public CLI: readable and JSON output without ccusage installed."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(db_path, *args):
    env = dict(os.environ, AGENT_OBSERVER_DB=db_path)
    return subprocess.run(
        [sys.executable, "-m", "agent_observer", *args],
        cwd=REPO, capture_output=True, text=True, env=env)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "cli.db")
        self.mini = os.path.join(REPO, "tests", "fixtures", "codex-mini.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sync_and_task_and_trace_round_trip(self):
        r = run(self.db, "sync", "--source", self.mini)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("3 new responses", r.stdout)
        for cmd in (["capture", "create-task", "--task", "T-CLI",
                     "--project", "observer", "--family", "research",
                     "--title", "CLI round trip"],
                    ["capture", "assign", "--submission", "msg-mini-sub-01",
                     "--task", "T-CLI", "--attempt", "a1", "--phase",
                     "research", "--evidence", "cli-test"],
                    ["capture", "assign", "--submission", "msg-mini-sub-02",
                     "--task", "T-CLI", "--attempt", "a1", "--phase",
                     "followup", "--evidence", "cli-test"]):
            r = run(self.db, *cmd)
            self.assertEqual(r.returncode, 0, r.stderr)
        r = run(self.db, "task", "show", "--task", "T-CLI", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["attributed"]["total_tokens"], 5500)
        self.assertTrue(payload["reconciles"])
        r = run(self.db, "trace", "--task", "T-CLI", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreater(len(json.loads(r.stdout)["events"]), 0)

    def test_missing_assignment_is_a_visible_failure(self):
        run(self.db, "sync", "--source", self.mini)
        run(self.db, "capture", "create-task", "--task", "T-PART")
        run(self.db, "capture", "assign", "--submission", "msg-mini-sub-01",
            "--task", "T-PART")
        r = run(self.db, "task", "show", "--task", "T-PART")
        self.assertEqual(r.returncode, 3)
        self.assertIn("missing assignments", r.stdout)

    def test_runtime_never_touches_ccusage(self):
        import re
        banned = re.compile(
            r"(import\s+ccusage|from\s+ccusage|ccusage\s*\.\s*\w+|"
            r"npx\s+ccusage|\.bin/ccusage|require\(['\"]ccusage)")
        for root, _, files in os.walk(os.path.join(REPO, "agent_observer")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(root, name)) as fh:
                    body = fh.read()
                self.assertIsNone(
                    banned.search(body),
                    f"ccusage import/call in runtime file {name}")
        r = run(self.db, "trace", "--capabilities", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        caps = {c["family"]: c["supported"]
                for c in json.loads(r.stdout)["capabilities"]}
        self.assertTrue(caps["model_usage"])


class UnknownCountersCliTest(unittest.TestCase):
    """Unknown native counters stay unknown through the CLI, never zero."""

    def setUp(self):
        from agent_observer import db as _db
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "unknown.db")
        con = _db.connect(self.db)
        _db.init_db(con)
        _db.upsert_session(con, "codex:u1", "codex", "u1", None,
                           project_dir="/p/app", started_at=100.0, ended_at=160.0)
        _db.upsert_session(con, "codex:u2", "codex", "u2", None,
                           project_dir="/p/app", started_at=100.0, ended_at=160.0)
        con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('codex','p','x',0)")
        # u1: one response with an unknown total beside a known one;
        # u2: only an unknown total.
        con.execute(
            "INSERT INTO responses(response_id, source_id, harness, session_key,"
            " turn_id, model, total_tokens, semantics) VALUES"
            " ('codex:ur1', 1, 'codex', 'codex:u1', 't1', 'gpt-6-fixture', NULL, 's'),"
            " ('codex:ur2', 1, 'codex', 'codex:u1', 't2', 'gpt-6-fixture', 100, 's'),"
            " ('codex:ur3', 1, 'codex', 'codex:u2', 't3', 'gpt-6-fixture', NULL, 's')")
        con.execute(
            "INSERT INTO tasks(task_id, project, family, title, created_at)"
            " VALUES('T-U','observer','research','unknown totals',0)")
        con.execute(
            "INSERT INTO session_assignments(session_key, task_id, evidence,"
            " created_at) VALUES('codex:u1','T-U','cli-test',0)")
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_sessions_list_shows_unknown_and_lower_bound(self):
        r = run(self.db, "sessions", "list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("total=unknown", r.stdout)
        self.assertIn("lower bound", r.stdout)

    def test_task_json_keeps_null_and_marks_lower_bound(self):
        r = run(self.db, "task", "show", "--task", "T-U", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        attributed = payload["attributed"]
        self.assertEqual(attributed["total_tokens"], 100)
        self.assertEqual(attributed["unknown_counts"]["total_tokens"], 1)
        self.assertNotEqual(attributed["total_tokens"], 0)
        self.assertTrue(payload["reconciles"])

    def test_task_text_marks_lower_bound(self):
        r = run(self.db, "task", "show", "--task", "T-U")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("lower bound", r.stdout)

    def test_session_show_json_keeps_null(self):
        r = run(self.db, "sessions", "show", "--session", "codex:u2")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertIsNone(payload["usage"]["total_tokens"])
        self.assertEqual(payload["usage"]["unknown_counts"]["total_tokens"], 1)

    def test_compare_by_model_json_attributes_per_response(self):
        r = run(self.db, "compare", "--by", "model", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        groups = {g["group"]: g for g in json.loads(r.stdout)["groups"]}
        self.assertIn("gpt-6-fixture", groups)
        self.assertEqual(groups["gpt-6-fixture"]["sessions"], 2)
        self.assertEqual(
            groups["gpt-6-fixture"]["tokens_per_session"]["total"], 100)
        self.assertEqual(groups["gpt-6-fixture"]["unknown_token_responses"], 2)
