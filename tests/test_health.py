"""Session visibility: one compliant and one invisible session per host."""

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_observer import cli, db, health, ingest
from agent_observer.adapters import claude, codex, grok, muse, opencode, router


class ScanAvailabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = patch.dict(os.environ, {"PATH": self.tmp.name})
        self.path.start()
        self.addCleanup(self.path.stop)
        self.enterContext(patch.object(health, "_ledger_freshness", return_value={}))
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


class CueHealthExitTest(unittest.TestCase):
    def test_each_cue_status_remains_visible_with_appropriate_exit(self):
        for status,expected in (('ok',0),('missing_database',0),('no_cue',0),
                                ('not_imported',0),('schema_drift',4),('unreadable_database',4)):
            for as_json in (False,True):
                with self.subTest(status=status,json=as_json):
                    result={'checked': {'claude_sessions':0,'processes':0},
                            'findings':[],'unavailable':[],'identity_conflicts':[],
                            'bare_roots':{},'cue':{'status':status},
                            'ledger_freshness':{},'sync_interval_seconds':3600}
                    output=io.StringIO()
                    with patch.object(health,'check',return_value=result), \
                            contextlib.redirect_stdout(output):
                        code=cli.main(['--db','/nonexistent/cue-test.db','health']+
                                      (['--json'] if as_json else []))
                    self.assertEqual(code,expected)
                    if as_json:
                        self.assertEqual(json.loads(output.getvalue())['cue']['status'],status)
                    else:
                        self.assertIn('Cue evidence: '+status,output.getvalue())


class LedgerFreshnessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger = self.root / "observer.db"
        for adapter in (claude, codex, grok, muse, opencode, router):
            self.enterContext(patch.object(adapter, "DEFAULT_ROOT",
                                           str(self.root / adapter.HARNESS)))
        self.enterContext(patch.object(codex, "ARCHIVE_ROOT", str(self.root / "archive")))
        self.enterContext(patch.object(router, "LEGACY_ROOT", str(self.root / "legacy")))
        self.enterContext(patch.dict(os.environ, {"T3CODE_HOME": str(self.root / "t3"),
                                                  "DURABLE_RUNNER_STATE_DIR": ""}))
        self.enterContext(patch.object(health, "process_table", return_value=[]))
        self.enterContext(patch.object(health, "claude_registry", return_value=[]))
        self.enterContext(patch("agent_observer.cue.health", return_value={"status": "no_cue"}))

    def source(self, path, modified_at):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private fixture text that health must never print")
        os.utime(path, (modified_at, modified_at))

    def imports(self, **harness_times):
        with contextlib.closing(db.connect(str(self.ledger))) as con, con:
            db.init_db(con)
            con.executemany(
                "INSERT INTO sources(harness, path, sha256, imported_at) VALUES(?,?,?,?)",
                [(harness, "fixture", "hash", stamp) for harness, stamp in harness_times.items()])

    def run_health(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--db", str(self.ledger), "health", *args])
        self.assertNotIn("private fixture text", out.getvalue())
        return code, out.getvalue()

    def test_stale_harness_is_not_hidden_by_a_fresher_import_from_another(self):
        self.source("claude/project/old.jsonl", 10_000)
        self.source("claude/project/new.jsonl", 22_000)
        self.source("claude/project/new/subagents/agent-child.jsonl", 20_000)
        self.source("archive/rollout-old-thread.jsonl", 18_000)
        self.imports(claude=11_000, codex=23_000)

        code, output = self.run_health("--json")
        self.assertEqual(code, 4)
        result = json.loads(output)
        self.assertEqual(result["sync_interval_seconds"], 3600)
        self.assertEqual(result["ledger_freshness"]["claude"], {
            "newest_native_at": 22_000, "newest_import_at": 11_000,
            "newer_sources": 2, "lag_seconds": 11_000, "status": "stale",
        })
        self.assertEqual(result["ledger_freshness"]["codex"]["newer_sources"], 0)
        self.assertEqual(result["ledger_freshness"]["codex"]["newest_native_at"], 18_000)
        code, output = self.run_health()
        self.assertEqual(code, 4)
        self.assertIn("claude: stale", output)
        self.assertIn("11000 seconds", output)
        self.assertIn("2 newer sources", output)

    def test_current_and_within_interval(self):
        self.source("claude/project/recent.jsonl", 14_600)
        self.source("grok/project/session/updates.jsonl", 11_000)
        self.source("grok/project/session/events.jsonl", 10_000)
        self.imports(claude=11_000, grok=10_000)

        code, output = self.run_health("--json")
        self.assertEqual(code, 0)
        freshness = json.loads(output)["ledger_freshness"]
        self.assertEqual(freshness["claude"]["newer_sources"], 1)
        self.assertEqual(freshness["claude"]["status"], "within_interval")
        self.assertEqual(freshness["grok"]["newer_sources"], 1)
        self.assertEqual(freshness["codex"]["status"], "no_native_records")
        self.assertEqual(freshness["muse"]["status"], "no_native_records")
        self.assertEqual(self.run_health("--sync-interval-seconds", "3599")[0], 4)

    def test_missing_or_empty_ledger_names_never_synced_harness(self):
        self.source("grok/project/session/updates.jsonl", 10_000)
        self.source("grok/project/session/events.jsonl", 11_000)
        for create in (False, True):
            with self.subTest(create=create):
                if create:
                    self.imports()
                code, output = self.run_health("--json")
                self.assertEqual(code, 4)
                self.assertEqual(json.loads(output)["ledger_freshness"]["grok"], {
                    "newest_native_at": 11_000, "newest_import_at": None,
                    "newer_sources": 1, "lag_seconds": None, "status": "never_synced",
                })
        self.assertIn("grok: never synced", self.run_health()[1])

    def test_opencode_counts_sessions_with_newer_parts_in_wal(self):
        path = self.root / "opencode"
        with contextlib.closing(sqlite3.connect(path)) as native:
            native.execute("PRAGMA journal_mode=WAL")
            native.executescript("""
                CREATE TABLE session (id TEXT, time_updated INTEGER);
                CREATE TABLE part (session_id TEXT, time_updated INTEGER, data TEXT);
                INSERT INTO session VALUES ('first', 10000000), ('second', 11000000);
                INSERT INTO part VALUES ('first', 19900000, 'private fixture text'),
                                        ('first', 20000000, 'private fixture text');
            """)
            self.imports(opencode=11_000)
            code, output = self.run_health("--json")
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(output)["ledger_freshness"]["opencode"], {
            "newest_native_at": 20_000, "newest_import_at": 11_000,
            "newer_sources": 1, "lag_seconds": 9_000, "status": "stale",
        })

    def test_unreadable_ledger_is_not_mistaken_for_never_synced(self):
        self.source("observer.db", 10_000)
        self.source("claude/project/new.jsonl", 20_000)
        code, output = self.run_health("--json")
        self.assertEqual(code, 4)
        result = json.loads(output)
        self.assertEqual(result["ledger_freshness"]["claude"]["status"], "unavailable")
        self.assertEqual(result["unavailable"][0]["check"], "ledger freshness")

    def test_router_rollouts_contribute_to_codex_freshness(self):
        self.source("router/jobs.db", 10_000)
        self.source("router/kits/kit/sessions/2026/rollout-kit.jsonl", 20_000)
        self.source("router/codex-sessions/2026/rollout-shared.jsonl", 21_000)
        self.imports(codex=11_000, router=11_000)
        code, output = self.run_health("--json")
        self.assertEqual(code, 4)
        rows = json.loads(output)["ledger_freshness"]
        self.assertEqual(rows["codex"]["status"], "stale")
        self.assertEqual(rows["codex"]["newest_native_at"], 21_000)
        self.assertEqual(rows["codex"]["newer_sources"], 2)
        self.assertEqual(rows["router"]["status"], "current")

    def test_unreadable_grok_directory_is_reported_as_unavailable(self):
        self.source("grok/project/session/updates.jsonl", 20_000)
        self.imports(grok=11_000)
        listdir = os.listdir
        for unreadable in (self.root / "grok", self.root / "grok/project"):
            def checked_listdir(path):
                if Path(path) == unreadable:
                    raise PermissionError("fixture directory unreadable")
                return listdir(path)

            with self.subTest(unreadable=unreadable), \
                    patch.object(grok.os, "listdir", side_effect=checked_listdir):
                code, output = self.run_health("--json")
                self.assertEqual(code, 4)
                result = json.loads(output)
                self.assertEqual(result["ledger_freshness"]["grok"]["status"], "unavailable")
                self.assertIn({"check": "grok native record scan",
                               "reason": "fixture directory unreadable"}, result["unavailable"])

    def test_failed_identity_query_preserves_freshness_json(self):
        self.source("claude/project/new.jsonl", 20_000)
        self.imports(claude=11_000)
        with contextlib.closing(sqlite3.connect(self.ledger)) as con:
            con.execute("DROP TABLE import_errors")
        code, output = self.run_health("--json")
        self.assertEqual(code, 4)
        result = json.loads(output)
        self.assertIsNone(result["identity_conflicts"])
        self.assertEqual(result["unavailable"][0]["check"], "identity conflicts")
        self.assertEqual(result["ledger_freshness"]["claude"]["status"], "stale")

    def test_denied_listing_in_glob_layouts_is_unavailable(self):
        self.source("claude/project/new.jsonl", 20_000)
        self.source("codex/2026/11/rollout-normal.jsonl", 20_000)
        self.source("router/codex-sessions/2026/rollout-owned.jsonl", 20_000)
        self.source("muse/2026/11/25/session/session.jsonl", 20_000)
        self.imports(claude=11_000, codex=11_000, muse=11_000)
        listdir = os.listdir
        scandir = os.scandir
        for harness, folder in (
                ("claude", "claude/project"), ("codex", "codex/2026/11"),
                ("codex", "router/codex-sessions/2026"), ("muse", "muse/2026/11")):
            def checked_listdir(path):
                if path == str(self.root / folder):
                    raise PermissionError("fixture directory unreadable")
                return listdir(path)

            def checked_scandir(path):
                if path == str(self.root / folder):
                    raise PermissionError("fixture directory unreadable")
                return scandir(path)

            with self.subTest(folder=folder), patch.object(os, "listdir", checked_listdir), \
                    patch.object(os, "scandir", checked_scandir):
                code, output = self.run_health("--json")
                self.assertEqual(code, 4)
                result = json.loads(output)
                self.assertEqual(result["ledger_freshness"][harness]["status"], "unavailable")
                self.assertIn({"check": f"{harness} native record scan",
                               "reason": "fixture directory unreadable"}, result["unavailable"])

    def test_grok_file_stat_permission_failure_is_unavailable(self):
        path = "grok/project/session/updates.jsonl"
        self.source(path, 20_000)
        self.imports(grok=11_000)
        stat = os.stat

        def checked_stat(target, *args, **kwargs):
            if target == str(self.root / path):
                raise PermissionError("fixture record unreachable")
            return stat(target, *args, **kwargs)

        with patch.object(os, "stat", checked_stat):
            code, output = self.run_health("--json")
        self.assertEqual(code, 4)
        result = json.loads(output)
        self.assertEqual(result["ledger_freshness"]["grok"]["status"], "unavailable")
        self.assertIn({"check": "grok native record scan",
                       "reason": "fixture record unreachable"}, result["unavailable"])

    def test_denied_chromeria_v2_state_is_unavailable_for_state_and_receipts(self):
        path = self.root / "t3/userdata/chromeria-v2.sqlite"
        path.parent.mkdir(parents=True)
        stat = os.stat

        def checked_stat(target, *args, **kwargs):
            if target == str(path):
                raise PermissionError("fixture V2 state unreachable")
            return stat(target, *args, **kwargs)

        with patch.object(os, "stat", checked_stat):
            code, output = self.run_health("--json")
        self.assertEqual(code, 4)
        rows = json.loads(output)["ledger_freshness"]
        for harness in ("t3", "codex", "grok"):
            self.assertEqual(rows[harness]["status"], "unavailable")

    def test_grok_metadata_only_lag_clears_after_import(self):
        session = self.root / "grok/project/session-one"
        session.mkdir(parents=True)
        summary = session / "summary.json"
        summary.write_text('{"info":{"id":"session-one"}}')
        os.utime(summary, (20_000, 20_000))
        self.imports(grok=11_000)

        code, output = self.run_health("--json")
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(output)["ledger_freshness"]["grok"]["newer_sources"], 1)

        with contextlib.closing(db.connect(str(self.ledger))) as con:
            self.assertEqual(grok.sync(con)["failed"], [])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        code, output = self.run_health("--json")
        self.assertEqual(code, 0)
        row = json.loads(output)["ledger_freshness"]["grok"]
        self.assertEqual(row["status"], "current")
        self.assertGreaterEqual(row["newest_import_at"], 20_000)

        with contextlib.closing(db.connect(str(self.ledger))) as con:
            con.execute("UPDATE grok_session_meta SET imported_at=NULL")
            con.commit()
            self.assertEqual(grok.sync(con)["failed"], [])
        self.assertEqual(self.run_health("--json")[0], 0)

    def test_codex_counts_hard_linked_rollouts_once(self):
        self.source("codex/2026/rollout-first.jsonl", 20_000)
        os.link(self.root / "codex/2026/rollout-first.jsonl",
                self.root / "codex/2026/rollout-alias.jsonl")
        self.imports(codex=11_000)
        code, output = self.run_health("--json")
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(output)["ledger_freshness"]["codex"]["newer_sources"], 1)

    def test_grok_revalidates_identical_metadata_and_retries_denied_reads(self):
        summary = self.root / "grok/project/session-one/summary.json"
        self.source("grok/project/session-one/summary.json", 10_000)
        summary.write_text('{"info":{"id":"session-one"}}')
        os.utime(summary, (10_000, 10_000))
        self.imports()
        with contextlib.closing(db.connect(str(self.ledger))) as con:
            with patch.object(db, "now", return_value=11_000):
                self.assertEqual(grok.sync(con)["failed"], [])
            os.utime(summary, (20_000, 20_000))
            self.assertEqual(self.run_health("--json")[0], 4)
            with patch.object(db, "now", return_value=21_000):
                self.assertEqual(grok.sync(con)["failed"], [])
            self.assertEqual(self.run_health("--json")[0], 0)
            summary.write_text('{"info":{"id":"session-one"},"head_branch":"fresh"}')
            os.utime(summary, (30_000, 30_000))
            real_open = open

            def denied_open(path, *args, **kwargs):
                if path == str(summary):
                    raise PermissionError("fixture refused summary read")
                return real_open(path, *args, **kwargs)

            with patch("builtins.open", denied_open), patch.object(db, "now", return_value=31_000):
                self.assertEqual(len(grok.sync(con)["failed"]), 1)
            self.assertEqual(grok.newest_metadata_import(con), 21_000)
            self.assertEqual(self.run_health("--json")[0], 4)
            with patch.object(db, "now", return_value=32_000):
                self.assertEqual(grok.sync(con)["failed"], [])
            self.assertEqual(self.run_health("--json")[0], 0)

    def test_chromeria_freshness_follows_symlink_to_wal(self):
        link = self.root / "t3/userdata/chromeria-v2.sqlite"
        link.parent.mkdir(parents=True)
        target = self.root / "native.sqlite"
        link.symlink_to(target)
        self.imports(t3=11_000)
        with contextlib.closing(sqlite3.connect(target)) as native:
            native.execute("PRAGMA journal_mode=WAL")
            native.executescript("CREATE TABLE evidence(value); INSERT INTO evidence VALUES (1);")
            os.utime(target, (10_000, 10_000))
            os.utime(str(target) + "-wal", (20_000, 20_000))
            code, output = self.run_health("--json")
        self.assertEqual(code, 4)
        row = json.loads(output)["ledger_freshness"]["t3"]
        self.assertEqual(row["newest_native_at"], 20_000)
        self.assertEqual(row["status"], "stale")

    def test_grok_retry_keeps_recorded_identity_and_counts_alias_once(self):
        record_path = self.root / "grok/project/directory-id/updates.jsonl"
        record_path.parent.mkdir(parents=True)
        summary = record_path.with_name("summary.json")
        summary.write_text(json.dumps({"info": {"id": "native-id"},
                                       "current_model_id": "grok-4",
                                       "reasoning_effort": "high"}))
        os.utime(summary, (10_000, 10_000))
        alias = record_path.parent.with_name("alias")
        alias.symlink_to(record_path.parent, target_is_directory=True)

        def append_completion(prompt, stamp):
            completion = {"timestamp": stamp, "method": "_x.ai/session/update",
                          "params": {"sessionId": "native-id", "update": {
                              "sessionUpdate": "turn_completed", "prompt_id": prompt,
                              "usage": {"inputTokens": 10, "outputTokens": 2}}}}
            with record_path.open("a") as file:
                file.write(json.dumps(completion) + "\n")
            os.utime(record_path, (stamp, stamp))

        append_completion("first", 10_000)
        self.imports()
        real_open = open

        def denied_open(path, *args, **kwargs):
            if os.path.basename(path) == "summary.json":
                raise PermissionError("fixture refused summary read")
            return real_open(path, *args, **kwargs)

        with contextlib.closing(db.connect(str(self.ledger))) as con:
            with patch.object(db, "now", return_value=11_000), patch.object(ingest, "now", return_value=11_000):
                self.assertEqual(grok.sync(con)["failed"], [])
            append_completion("second", 20_000)
            for stamp in (21_000, 21_500):
                with patch("builtins.open", denied_open), patch.object(db, "now", return_value=stamp), \
                        patch.object(ingest, "now", return_value=stamp):
                    self.assertTrue(grok.sync(con)["failed"])
                source = con.execute("SELECT session_id, imported_at FROM sources").fetchone()
                self.assertEqual(tuple(source), ("native-id", 21_000))
            code, output = self.run_health("--json")
            self.assertEqual(code, 4)
            self.assertEqual(json.loads(output)["ledger_freshness"]["grok"]["newer_sources"], 1)
            with patch.object(db, "now", return_value=22_000), patch.object(ingest, "now", return_value=22_000):
                self.assertEqual(grok.sync(con)["failed"], [])
            rows = con.execute("SELECT DISTINCT session_key, model, effort FROM responses").fetchall()
            self.assertEqual([tuple(row) for row in rows], [("grok:native-id", "grok-4", "high")])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 2)
        self.assertEqual(self.run_health("--json")[0], 0)

    def test_bad_grok_summary_keeps_transcript_ingestion(self):
        updates = "grok/project/session-one/updates.jsonl"
        self.source(updates, 20_000)
        completion = {"timestamp": 20_000, "method": "_x.ai/session/update",
                      "params": {"sessionId": "session-one",
                                 "update": {"sessionUpdate": "turn_completed",
                                            "prompt_id": "prompt-one",
                                            "usage": {"inputTokens": 10, "outputTokens": 2}}}}
        (self.root / updates).write_text(json.dumps(completion) + "\n")
        (self.root / "grok/project/session-one/summary.json").write_text('{"incomplete":')
        self.imports()
        with contextlib.closing(db.connect(str(self.ledger))) as con:
            result = grok.sync(con)
            self.assertEqual(result["responses_inserted"], 1)
            self.assertEqual(result["failed"][0]["error"], "invalid Grok summary JSON")
            self.assertIsNone(grok.newest_metadata_import(con))
            self.assertGreater(con.execute(
                "SELECT imported_at FROM sources WHERE harness='grok'").fetchone()[0], 0)
        code, output = self.run_health("--json")
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(output)["ledger_freshness"]["grok"]["status"], "never_synced")

        summary = self.root / "grok/project/session-one/summary.json"
        summary.write_text('{"info":{"id":"session-one"}}')
        with contextlib.closing(db.connect(str(self.ledger))) as con:
            self.assertEqual(grok.sync(con)["failed"], [])
            self.assertIsNotNone(grok.newest_metadata_import(con))
        self.assertEqual(self.run_health("--json")[0], 0)

    def test_grok_first_partial_transcript_uses_native_notification_id(self):
        folder = self.root / "grok/project/directory-id"
        folder.mkdir(parents=True)
        summary = folder / "summary.json"
        summary.write_text('{"info":{"id":"native-id"},"current_model_id":"grok-4"}')
        os.utime(summary, (10_000, 10_000))
        self.imports()
        real_open = open

        def denied_open(path, *args, **kwargs):
            if path == str(summary):
                raise PermissionError("fixture refused summary read")
            return real_open(path, *args, **kwargs)

        with contextlib.closing(db.connect(str(self.ledger))) as con:
            with patch.object(db, "now", return_value=11_000):
                self.assertEqual(grok.sync(con)["failed"], [])
            update = folder / "updates.jsonl"
            completion = {"timestamp": 20_000, "method": "_x.ai/session/update",
                          "params": {"sessionId": "native-id", "update": {
                              "sessionUpdate": "turn_completed", "prompt_id": "first",
                              "usage": {"inputTokens": 10, "outputTokens": 2}}}}
            update.write_text(json.dumps(completion) + "\n")
            os.utime(update, (20_000, 20_000))
            with patch("builtins.open", denied_open), patch.object(db, "now", return_value=21_000), \
                    patch.object(ingest, "now", return_value=21_000):
                self.assertTrue(grok.sync(con)["failed"])
            self.assertEqual(self.run_health("--json")[0], 4)
            with patch.object(db, "now", return_value=22_000), patch.object(ingest, "now", return_value=22_000):
                self.assertEqual(grok.sync(con)["failed"], [])
            rows = con.execute("SELECT session_key, model FROM responses").fetchall()
            self.assertEqual([tuple(row) for row in rows], [("grok:native-id", "grok-4")])
        self.assertEqual(self.run_health("--json")[0], 0)

    def test_grok_waits_for_summary_when_first_log_has_no_native_id(self):
        folder = self.root / "grok/project/directory-id"
        folder.mkdir(parents=True)
        summary = folder / "summary.json"
        summary.write_text('{"info":{"id":"native-id"},"current_model_id":"grok-4"}')
        self.imports()
        (folder / "events.jsonl").write_text("{}\n")
        real_open = open

        def denied_open(path, *args, **kwargs):
            if path == str(summary):
                raise PermissionError("fixture refused summary read")
            return real_open(path, *args, **kwargs)

        with contextlib.closing(db.connect(str(self.ledger))) as con:
            with patch("builtins.open", denied_open):
                self.assertTrue(grok.sync(con)["failed"])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            completion = {"timestamp": 20_000, "method": "_x.ai/session/update",
                          "params": {"update": {"sessionUpdate": "turn_completed",
                                                "prompt_id": "first",
                                                "usage": {"inputTokens": 10, "outputTokens": 2}}}}
            (folder / "updates.jsonl").write_text(json.dumps(completion) + "\n")
            with patch("builtins.open", denied_open):
                self.assertTrue(grok.sync(con)["failed"])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 0)
            summary.write_text('{"info":{"id":"native-id"},"current_model_id":"grok-4"}')
            self.assertEqual(grok.sync(con)["failed"], [])
            rows = con.execute("SELECT session_key FROM sessions").fetchall()
            self.assertEqual([row[0] for row in rows], ["grok:native-id"])
            self.assertEqual(con.execute("SELECT session_key FROM responses").fetchone()[0],
                             "grok:native-id")
        self.assertEqual(self.run_health("--json")[0], 0)

    def test_grok_first_event_supplies_native_id_without_summary(self):
        self.source("grok/project/directory-id/events.jsonl", 20_000)
        event = self.root / "grok/project/directory-id/events.jsonl"
        event.write_text('{"type":"turn_started","session_id":"native-id",'
                         '"ts":"2026-11-25T18:00:00Z"}\n')
        event.with_name("updates.jsonl").write_text(
            '{"method":"session/update","params":{"sessionId":"wrong-id"}}\n'
            '{"method":"session/update","params":{"sessionId":"wrong-id",'
            '"update":{"sessionUpdate":"unrecognized"}}}\n'
            '{"method":"session/update","params":{"sessionId":"wrong-id",'
            '"update":{"sessionUpdate":"tool_call"}}}\n')
        self.imports()
        with contextlib.closing(db.connect(str(self.ledger))) as con:
            result = grok.sync(con)
            self.assertEqual(result["failed"], [])
            self.assertEqual(result["events_inserted"], 1)
            self.assertEqual(con.execute("SELECT session_key FROM events").fetchone()[0],
                             "grok:native-id")

    def test_grok_readable_summary_can_fall_back_to_directory_id(self):
        folder = self.root / "grok/project/directory-id"
        folder.mkdir(parents=True)
        (folder / "summary.json").write_text('{"head_branch":"main"}')
        (folder / "events.jsonl").write_text(
            '{"type":"turn_ended","ts":"2026-11-25T18:00:00Z","outcome":"completed"}\n')
        self.imports()
        with contextlib.closing(db.connect(str(self.ledger))) as con:
            self.assertEqual(grok.sync(con)["failed"], [])
            self.assertEqual(con.execute("SELECT session_key FROM events").fetchone()[0],
                             "grok:directory-id")
            (folder / "summary.json").write_text('{"info":{"id":"native-id"}}')
            with (folder / "events.jsonl").open("a") as file:
                file.write('{"type":"turn_started","ts":"2026-11-25T18:01:00Z"}\n')
            self.assertEqual(grok.sync(con)["failed"], [])
            self.assertEqual(con.execute(
                "SELECT session_key FROM events WHERE name='turn_started'").fetchone()[0],
                "grok:native-id")

    def test_bad_grok_metadata_keeps_health_stale(self):
        cases = (
            ("summary.json", '{"incomplete":', "updates.jsonl", "{}\n", False, None),
            ("summary.json", '{"incomplete":', "events.jsonl", "", False, None),
            ("summary.json", '{"incomplete":', "updates.jsonl", "{}\n", True, None),
            ("summary.json", '{"incomplete":', "updates.jsonl", "{}\n", False, ""),
            ("summary.json", '{"incomplete":', "updates.jsonl", "{}\n", False, "{"),
            ("chat_history.jsonl", "invalid json\n", None, "", False, None),
        )
        for filename, content, log, log_content, full, append in cases:
            with self.subTest(filename=filename, log=log, full=full, append=append), \
                    tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                session = base / "grok/project/session-one"
                session.mkdir(parents=True)
                if log is not None:
                    transcript = session / log
                    transcript.write_text(log_content)
                    os.utime(transcript, (10_000, 10_000))
                summary = session / "summary.json"
                summary.write_text('{"info":{"id":"session-one"},"head_branch":"fixture"}')
                os.utime(summary, (10_000, 10_000))
                ledger = base / "observer.db"
                with contextlib.closing(db.connect(str(ledger))) as con:
                    db.init_db(con)
                    with patch.object(db, "now", return_value=11_000), \
                            patch.object(ingest, "now", return_value=11_000):
                        self.assertEqual(grok.sync(con, root=str(base / "grok"))["failed"], [])
                    metadata = session / filename
                    metadata.write_text(content)
                    os.utime(metadata, (20_000, 20_000))
                    if append is not None:
                        with transcript.open("a") as file:
                            file.write(append)
                        os.utime(transcript, (20_000, 20_000))
                    with patch.object(db, "now", return_value=21_000), \
                            patch.object(ingest, "now", return_value=21_000):
                        result = grok.sync(con, root=str(base / "grok"), full=full)
                        self.assertTrue(result["failed"])
                    self.assertEqual(grok.newest_metadata_import(con), 11_000)
                with patch.object(grok, "DEFAULT_ROOT", str(base / "grok")), \
                        patch.object(self, "ledger", ledger):
                    code, output = self.run_health("--json")
                self.assertEqual(code, 4)
                self.assertEqual(json.loads(output)["ledger_freshness"]["grok"]["status"], "stale")


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
