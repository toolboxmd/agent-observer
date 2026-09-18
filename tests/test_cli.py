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
