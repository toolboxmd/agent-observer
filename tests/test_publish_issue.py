"""Empty task diagnostics through the public CLI and a synthetic gh boundary."""

import json
import os
import pathlib
import sys
import subprocess
from unittest import mock

from agent_observer import db
from tests.helpers import LedgerCase


FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["GH_CALLS"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
mode = os.environ["GH_MODE"]
endpoint = sys.argv[2]
if mode == "failure" or (mode == "timeline-failure" and "/timeline?" in endpoint):
    sys.stderr.write("synthetic lookup failure")
    sys.exit(1)
if "/timeline?" in endpoint:
    rows = []
    if mode == "references":
        for url, state, pr in [
            ("https://github.com/o/r/pull/36", "open", {}),
            ("https://github.com/o/r/pull/37", "closed", {"merged_at": "2026-01-01"}),
            ("https://github.com/o/r/pull/38", "closed", {}),
            ("https://github.com/o/r/issues/39", "open", None),
            ("https://github.com/other/repo/pull/40", "open", {}),
            ("https://github.com/o/r/pull/36", "open", {}),
        ]:
            issue = {"html_url": url, "state": state}
            if pr is not None:
                issue["pull_request"] = pr
            rows.append({"event": "cross-referenced", "source": {"issue": issue}})
    print(json.dumps(rows))
else:
    print(json.dumps({"number": 13, **({"pull_request": {}} if mode == "pr" else {})}))
'''


class PublishIssueTest(LedgerCase):
    def setUp(self):
        super().setUp()
        root = pathlib.Path(self.tmp.name)
        self.calls = root / "calls"
        gh = root / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o700)
        self.env = {"PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "AGENT_OBSERVER_DB": self.db_path, "GH_CALLS": str(self.calls)}

    def run_publish(self, mode, task="o/r#13"):
        self.con.commit()
        with mock.patch.dict(os.environ, self.env | {"GH_MODE": mode}):
            return subprocess.run(
                [sys.executable, "-m", "agent_observer", "publish", "--task", task,
                 "--repo", "o/r", "--pr", "36", "--dry-run"],
                capture_output=True, text=True)

    def assert_issue_hint(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--task o/r#<PR>", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_issue_lists_open_and_merged_referencing_prs(self):
        result = self.run_publish("references")
        self.assert_issue_hint(result)
        self.assertIn("Issues are not tasks", result.stderr)
        for ref in ("o/r#36", "o/r#37", "other/repo#40"):
            self.assertIn(ref, result.stderr)
        self.assertEqual(result.stderr.count("o/r#36"), 1)
        self.assertNotIn("o/r#38", result.stderr)
        self.assertNotIn("o/r#39", result.stderr)

    def test_issue_without_referencing_prs_has_generic_hint(self):
        result = self.run_publish("none")
        self.assert_issue_hint(result)
        self.assertIn("Issues are not tasks", result.stderr)

    def test_failed_lookup_still_has_generic_pr_form(self):
        self.assert_issue_hint(self.run_publish("failure"))

    def test_failed_timeline_keeps_issue_hint(self):
        result = self.run_publish("timeline-failure")
        self.assert_issue_hint(result)
        self.assertIn("Issues are not tasks", result.stderr)

    def test_empty_pr_retains_existing_message(self):
        result = self.run_publish("pr")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "nothing to publish: no sessions in scope\n")
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_issue_or_pr_with_sessions_does_not_query_github(self):
        db.upsert_session(self.con, "codex:synthetic", "codex", "synthetic", None)
        self.con.execute("INSERT INTO tasks(task_id, created_at) VALUES('o/r#13', 0)")
        self.con.execute("INSERT INTO session_assignments(session_key, task_id, evidence,"
                         " created_at) VALUES('codex:synthetic', 'o/r#13', 'synthetic', 0)")
        for mode in ("none", "pr"):
            with self.subTest(mode=mode):
                result = self.run_publish(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("agent-observer:summary", result.stdout)
                self.assertFalse(self.calls.exists())

    def test_local_task_retains_existing_message_without_github(self):
        result = self.run_publish("failure", task="local-task")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "nothing to publish: no sessions in scope\n")
        self.assertFalse(self.calls.exists())
