"""Session visibility: one compliant and one invisible session per host."""

import json
import os
import sqlite3
import tempfile
import time
import unittest

from agent_observer import health


class ClaudeVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = os.path.join(self.tmp.name, "projects")
        os.makedirs(os.path.join(self.projects, "-p-app"))
        with open(os.path.join(self.projects, "-p-app", "visible.jsonl"), "w") as fh:
            fh.write("{}\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_session_without_transcript_is_reported_with_the_fix(self):
        old = (time.time() - 600) * 1000
        registry = [
            {"pid": 101, "sessionId": "visible", "cwd": "/p/app", "startedAt": old},
            {"pid": 102, "sessionId": "invisible", "cwd": "/p/app", "startedAt": old},
            {"pid": 103, "sessionId": "gone", "cwd": "/p/app", "startedAt": old},
            {"pid": 104, "sessionId": "just-started", "cwd": "/p/app",
             "startedAt": time.time() * 1000},
        ]
        findings = health.check_claude(registry, self.projects,
                                       alive=lambda pid: pid != 103)
        self.assertEqual([f["session"] for f in findings], ["invisible"])
        self.assertEqual(findings[0]["pid"], 102)
        self.assertIn("CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1", findings[0]["fix"])


class ProcessVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "opencode.db")
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE session (id TEXT, directory TEXT, time_updated INTEGER)")
        con.execute("INSERT INTO session VALUES ('s1', '/p/seen', ?)", (int(time.time() * 1000),))
        con.commit()
        con.close()
        self.rollouts = os.path.join(self.tmp.name, "sessions", "2026", "09", "23")
        os.makedirs(self.rollouts)
        with open(os.path.join(self.rollouts, "rollout-x-t1.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "session_meta", "payload": {"cwd": "/p/seen"}}) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def procs(self, name, command):
        started = time.time() - 60
        return [{"pid": 201, "name": name, "command": command, "started": started, "cwd": "/p/seen"},
                {"pid": 202, "name": name, "command": command, "started": started, "cwd": "/p/unseen"}]

    def test_opencode_process_without_session_record(self):
        findings = health.check_opencode(self.procs("opencode", "opencode"), self.db)
        self.assertEqual([f["pid"] for f in findings], [202])

    def test_codex_cli_without_rollout(self):
        findings = health.check_codex(self.procs("codex", "codex"),
                                      os.path.join(self.tmp.name, "sessions"))
        self.assertEqual([f["pid"] for f in findings], [202])

    def test_servers_and_exec_runs_are_not_judged_by_process(self):
        self.assertEqual(health.check_opencode(self.procs("opencode", "opencode serve"), self.db), [])
        self.assertEqual(health.check_codex(self.procs("codex", "codex exec --json"),
                                            os.path.join(self.tmp.name, "sessions")), [])
