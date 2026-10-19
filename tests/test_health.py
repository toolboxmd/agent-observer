"""Session visibility: one compliant and one invisible session per host."""

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_observer import cli, health


class ScanAvailabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = patch.dict(os.environ, {"PATH": self.tmp.name})
        self.path.start()
        self.addCleanup(self.path.stop)
        self.tool("ps", "printf '201 Mon Oct 19 10:00:00 2026 codex\\n'")

    def tool(self, name, body):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o700)

    def check_unavailable(self, reason):
        with patch.object(health, "check_claude", return_value=[{"host": "claude"}]) as claude, \
                patch.object(health, "identity_conflicts", return_value=[]) as identity, \
                patch("agent_observer.cue.health", return_value={"status": "not_imported"}):
            result = health.check(registry=[], con=object())
        self.assertEqual(result["findings"], [{"host": "claude"}])
        self.assertEqual(result["identity_conflicts"], [])
        claude.assert_called_once()
        identity.assert_called_once()
        self.assertTrue(result["unavailable"])
        self.assertIn(reason, result["unavailable"][0]["reason"])
        return result

    def test_path_without_lsof_reports_unavailable_and_continues(self):
        result = self.check_unavailable("lsof")
        self.assertEqual(result["unavailable"][0]["pid"], 201)

    def test_failed_lsof_does_not_accept_partial_output(self):
        self.tool("lsof", "printf 'n/p/app\\n'; echo 'permission denied' >&2; exit 2")
        self.check_unavailable("permission denied")

    def test_empty_lsof_output_is_unknown(self):
        self.tool("lsof", "exit 0")
        self.check_unavailable("no working directory")

    def test_path_without_ps_reports_scan_unavailable(self):
        os.unlink(os.path.join(self.tmp.name, "ps"))
        self.check_unavailable("ps")

    def test_failed_ps_reports_scan_unavailable(self):
        self.tool("ps", "echo 'scan denied' >&2; exit 1")
        self.check_unavailable("scan denied")

    def test_malformed_ps_output_is_unavailable(self):
        for row in ("nope Mon Oct 19 10:00:00 2026 codex",
                    "201 bad date from ps output codex", ""):
            with self.subTest(row=row):
                self.tool("ps", f"printf '%s\\n' '{row}'")
                self.check_unavailable("unparseable ps output")

    def test_skipped_commands_never_need_a_working_directory(self):
        for command in ("/bin/codex app-server", "codex exec --json", "opencode serve"):
            with self.subTest(command=command):
                self.tool("ps", f"printf '201 Mon Oct 19 10:00:00 2026 {command}\\n'")
                # There is no lsof on PATH: any lookup would report unavailable.
                result = health.check(registry=[], bare={})
                self.assertEqual(result["unavailable"], [])
                self.assertEqual(result["findings"], [])

    def test_irrelevant_malformed_rows_do_not_hide_a_needed_process(self):
        check_codex = health.check_codex
        for row in ("garbage", "nope bad date from ps output /bin/unrelated",
                    "201 bad date from ps output codex app-server",
                    "201 bad date from ps output codex exec --json",
                    "201 bad date from ps output opencode serve"):
            with self.subTest(row=row):
                self.tool("ps", f"printf '{row}\\n202 Mon Oct 19 10:00:00 2026 codex\\n'")
                self.tool("lsof", "printf 'n/p/unseen\\n'")
                with patch.object(health, "check_codex", side_effect=lambda processes:
                                  check_codex(processes, self.tmp.name)):
                    result = health.check(registry=[], bare={})
                self.assertEqual(result["unavailable"], [])
                self.assertEqual([f["pid"] for f in result["findings"]], [202])

    def test_ps_uses_c_locale_without_changing_parent_environment(self):
        self.tool("ps", 'if [ "$LC_ALL" = C ]; then '
                  "printf '201 Mon Oct 19 10:00:00 2026 codex\\n'; "
                  "else printf '201 lun oct 19 10:00:00 2026 codex\\n'; fi")
        self.tool("lsof", "printf 'n/p/unseen\\n'")
        with patch.dict(os.environ, {"LC_ALL": "fr_FR.UTF-8", "LC_TIME": "fr_FR.UTF-8"}):
            rows = health.process_table()
            self.assertEqual(os.environ["LC_ALL"], "fr_FR.UTF-8")
            self.assertEqual(os.environ["LC_TIME"], "fr_FR.UTF-8")
        self.assertEqual(rows[0]["pid"], 201)
        self.assertTrue(rows[0]["started"])
        self.assertEqual(rows[0]["cwd"], "/p/unseen")

    def test_malformed_lsof_cwd_is_unavailable(self):
        self.tool("lsof", "printf 'nnot-a-working-directory\\n'")
        self.check_unavailable("lsof")

    def test_hung_scan_tools_are_unavailable(self):
        for name in ("ps", "lsof"):
            with self.subTest(tool=name):
                self.tool("ps", "printf '201 Mon Oct 19 10:00:00 2026 codex\\n'")
                self.tool(name, "exec /bin/sleep 0.3")
                with patch.object(health, "SCAN_TIMEOUT_SECONDS", 0.05, create=True):
                    result = self.check_unavailable("timed out")
                self.assertIn(name, result["unavailable"][0]["reason"])

    def test_readable_cwd_still_finds_invisible_process(self):
        self.tool("lsof", "printf 'p201\\nn/p/unseen\\n'")
        check_codex = health.check_codex
        with patch.object(health, "check_codex", side_effect=lambda processes:
                          check_codex(processes, self.tmp.name)) as codex:
            result = health.check(registry=[])
        self.assertEqual(codex.call_args.args[0][0]["cwd"], "/p/unseen")
        self.assertEqual(result["unavailable"], [])
        self.assertEqual(result["findings"][0]["pid"], 201)

    def test_one_unreadable_process_does_not_hide_another(self):
        self.tool("ps", "printf '201 Mon Oct 19 10:00:00 2026 codex\\n"
                  "202 Mon Oct 19 10:00:00 2026 opencode\\n'")
        self.tool("lsof", 'if [ "$3" = 201 ]; then exit 1; fi; printf "n/p/unseen\\n"')
        check_opencode = health.check_opencode
        with patch.object(health, "check_opencode", side_effect=lambda processes:
                          check_opencode(processes, os.path.join(self.tmp.name, "missing.db"))):
            result = health.check(registry=[])
        self.assertEqual(result["unavailable"][0]["pid"], 201)
        self.assertEqual(result["findings"][0]["pid"], 202)

    def test_cli_shows_gap_and_does_not_return_success(self):
        for as_json in (False, True):
            with self.subTest(as_json=as_json), patch.object(health, "claude_registry", return_value=[]):
                output = io.StringIO()
                args = ["--db", os.path.join(self.tmp.name, "missing.db"), "health"]
                with contextlib.redirect_stdout(output):
                    code = cli.main(args + (["--json"] if as_json else []))
                self.assertEqual(code, 4)
                if as_json:
                    self.assertIn("lsof", json.loads(output.getvalue())["unavailable"][0]["reason"])
                else:
                    self.assertIn("working directory unavailable:", output.getvalue())
                    self.assertIn("lsof", output.getvalue())


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
