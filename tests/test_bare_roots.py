"""Default sync discovers Chromeria bare record roots from receipts (#92).

Each Chromeria bare launch writes a ``system_notice`` turn item whose
``bareReceipt.nativeRecord`` names the harness and its record ``root``
(``<home>/sessions``). A default sync must import those roots next to the
normal homes, count each session once even when an explicit ``--root``
imports the same files through another path, and report every receipt
whose root is missing, unreadable, empty or not recorded as a gap without
failing. No receipts and no T3 state database are reported as such.
"""

import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agent_observer import cli, db, health
from agent_observer.adapters import bare, codex, grok
from agent_observer.ingest import stored_path

from tests.helpers import fixture
from tests.test_codex_archive import _rollout, _write

GROK_GROUP = "%2Fredacted%2Frepo"
GROK_NORMAL = "01fixture1-aaaa-4b5c-8d6e-000000000001"
GROK_BARE = "01fixture2-bbbb-4b5c-8d6e-000000000002"


class BareRootCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.realpath(self.tmp.name)
        self.t3 = os.path.join(self.base, "t3")
        self.state = os.path.join(self.t3, "userdata", "chromeria-v2.sqlite")
        self.codex_home = os.path.join(self.base, "codex")
        self.grok_root = os.path.join(self.base, "grok", "sessions")
        os.makedirs(os.path.join(self.t3, "userdata"))
        patches = [
            patch.dict(os.environ, {"T3CODE_HOME": self.t3}),
            patch.object(codex, "DEFAULT_ROOT",
                         os.path.join(self.codex_home, "sessions")),
            patch.object(codex, "ARCHIVE_ROOT",
                         os.path.join(self.codex_home, "archived_sessions")),
            patch.object(grok, "DEFAULT_ROOT", self.grok_root),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.con = db.connect(os.path.join(self.base, "observer.db"))
        db.init_db(self.con)
        self.addCleanup(self.con.close)
        self.receipt_count = 0

    def receipts(self, *pairs):
        """Write one bare receipt per (harness, root) into T3 state."""
        native = sqlite3.connect(self.state)
        native.execute(
            "CREATE TABLE IF NOT EXISTS orchestration_v2_projection_turn_items"
            " (turn_item_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,"
            " ordinal INTEGER NOT NULL, type TEXT NOT NULL,"
            " status TEXT NOT NULL, updated_at TEXT NOT NULL,"
            " payload_json TEXT NOT NULL)")
        for harness, root in pairs:
            self.receipt_count += 1
            n = self.receipt_count
            payload = {"type": "system_notice", "title": "Bare launch receipt",
                       "bareReceipt": {"driver": harness, "nativeRecord": {
                           "harness": harness, "sessionId": f"s{n}",
                           "root": root}}}
            native.execute(
                "INSERT INTO orchestration_v2_projection_turn_items"
                " VALUES(?,?,0,'system_notice','completed','2026-10-19',?)",
                (f"bare-receipt:{n}", f"thread-{n}", json.dumps(payload)))
        # An ordinary notice is not a receipt.
        native.execute(
            "INSERT OR IGNORE INTO orchestration_v2_projection_turn_items"
            " VALUES('plain',  't', 0, 'system_notice', 'completed', '',"
            " '{\"type\": \"system_notice\"}')")
        native.commit()
        native.close()

    def bare_sessions(self, harness, name):
        return os.path.join(self.base, "bare", harness, name, "sessions")

    def rollout(self, base, thread, response, total):
        name = f"rollout-2026-10-01T10-00-00-{thread}.jsonl"
        path = os.path.join(base, "2026", "10", "01", name)
        _write(path, _rollout(thread, response, total))
        return path

    def grok_session(self, root, session):
        shutil.copytree(os.path.join(fixture("grok"), GROK_GROUP, session),
                        os.path.join(root, GROK_GROUP, session))

    def scalar(self, sql, args=()):
        return self.con.execute(sql, args).fetchone()[0]


class CodexBareRootsTest(BareRootCase):
    def test_receipt_roots_import_once_next_to_a_normal_session(self):
        self.rollout(os.path.join(self.codex_home, "sessions"),
                     "thr-normal", "resp-normal", 1000)
        live = self.bare_sessions("codex", "a")
        self.rollout(live, "thr-bare", "resp-bare", 300)
        # A second home whose only record is archived beside its root.
        other = self.bare_sessions("codex", "b")
        os.makedirs(other)
        self.rollout(os.path.join(os.path.dirname(other), "archived_sessions"),
                     "thr-bare-arch", "resp-bare-arch", 50)
        # Two runs of one thread write two receipts naming one root.
        self.receipts(("codex", live), ("codex", live), ("codex", other),
                      ("grok", self.bare_sessions("grok", "x")))

        stats = codex.sync(self.con)

        keys = {r[0] for r in self.con.execute("SELECT session_key FROM sessions")}
        self.assertEqual(keys, {"codex:thr-normal", "codex:thr-bare",
                                "codex:thr-bare-arch"})
        self.assertEqual(self.scalar(
            "SELECT SUM(total_tokens) FROM responses WHERE is_overlap=0"), 1350)
        self.assertEqual(stats["failed"], [])
        self.assertEqual(stats["bare_roots"],
                         {"state_db": self.state, "state": "used",
                          "receipts": 3, "roots": 2, "gaps": []})

        # The same root imported again through an aliased path adds no
        # session, no response and no second source row (Luna P2).
        alias = os.path.join(self.base, "alias")
        os.symlink(os.path.dirname(live), alias)
        sources = self.scalar("SELECT COUNT(*) FROM sources")
        again = codex.sync(self.con, root=os.path.join(alias, "sessions"))
        self.assertEqual(again["responses_inserted"], 0)
        self.assertNotIn("bare_roots", again)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sources"), sources)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sessions"), 3)

    def test_explicit_root_first_then_default_counts_once(self):
        live = self.bare_sessions("codex", "a")
        self.rollout(live, "thr-bare", "resp-bare", 300)
        self.receipts(("codex", live))
        codex.sync(self.con, root=live)
        stats = codex.sync(self.con)
        self.assertEqual(stats["responses_inserted"], 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sessions"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sources"), 1)
        self.assertEqual(self.scalar(
            "SELECT SUM(total_tokens) FROM responses WHERE is_overlap=0"), 300)

    def test_every_unreadable_receipt_root_is_a_gap(self):
        good = self.bare_sessions("codex", "good")
        self.rollout(good, "thr-bare", "resp-bare", 300)
        missing = self.bare_sessions("codex", "missing")
        empty = self.bare_sessions("codex", "empty")
        os.makedirs(empty)
        locked_home = os.path.dirname(self.bare_sessions("codex", "locked"))
        self.rollout(self.bare_sessions("codex", "locked"), "thr-locked",
                     "resp-locked", 70)
        alias = os.path.join(self.base, "good-alias")
        os.symlink(good, alias)
        self.receipts(("codex", good), ("codex", alias), ("codex", missing),
                      ("codex", empty), ("codex", None),
                      ("codex", self.bare_sessions("codex", "locked")))
        os.chmod(locked_home, 0)
        self.addCleanup(os.chmod, locked_home, 0o700)
        if os.access(locked_home, os.X_OK):
            self.skipTest("running as a user that ignores permissions")

        stats = codex.sync(self.con)

        self.assertEqual(stats["failed"], [])
        self.assertEqual(stats["sources"], 1)
        report = stats["bare_roots"]
        self.assertEqual((report["state"], report["receipts"], report["roots"]),
                         ("used", 6, 1))
        self.assertEqual(sorted(report["gaps"], key=lambda g: g["reason"]), [
            {"root": empty, "reason": "empty"},
            {"root": missing, "reason": "missing on this machine"},
            {"root": None, "reason": "root not recorded"},
            {"root": self.bare_sessions("codex", "locked"),
             "reason": "unreadable"},
        ])

    def test_no_receipts_reads_as_bare_mode_unused(self):
        self.rollout(os.path.join(self.codex_home, "sessions"),
                     "thr-normal", "resp-normal", 1000)
        self.receipts(("grok", self.bare_sessions("grok", "x")))
        stats = codex.sync(self.con)
        self.assertEqual(stats["sources"], 1)
        self.assertEqual(stats["bare_roots"],
                         {"state_db": self.state, "state": "unused",
                          "receipts": 0, "roots": 0, "gaps": []})

    def test_no_t3_state_is_said_explicitly(self):
        self.rollout(os.path.join(self.codex_home, "sessions"),
                     "thr-normal", "resp-normal", 1000)
        stats = codex.sync(self.con)
        self.assertEqual(stats["failed"], [])
        self.assertEqual(stats["sources"], 1)
        self.assertEqual(stats["bare_roots"]["state"], "no T3 state")
        self.assertEqual(stats["bare_roots"]["state_db"],
                         os.path.join(self.t3, "userdata", "state.sqlite"))


class GrokBareRootsTest(BareRootCase):
    def test_receipt_root_imports_once_next_to_a_normal_session(self):
        self.grok_session(self.grok_root, GROK_NORMAL)
        bare_root = self.bare_sessions("grok", "a")
        self.grok_session(bare_root, GROK_BARE)
        self.receipts(("grok", bare_root))

        stats = grok.sync(self.con)

        keys = {r[0] for r in self.con.execute("SELECT session_key FROM sessions")}
        self.assertEqual(keys, {f"grok:{GROK_NORMAL}", f"grok:{GROK_BARE}"})
        self.assertEqual(stats["failed"], [])
        self.assertEqual((stats["bare_roots"]["roots"],
                          stats["bare_roots"]["gaps"]), (1, []))
        self.assertGreater(self.scalar(
            "SELECT COUNT(*) FROM responses WHERE session_key=?",
            (f"grok:{GROK_BARE}",)), 0)
        responses = self.scalar("SELECT COUNT(*) FROM responses")
        events = self.scalar("SELECT COUNT(*) FROM events")
        sources = self.scalar("SELECT COUNT(*) FROM sources")

        alias = os.path.join(self.base, "grok-alias")
        os.symlink(bare_root, alias)
        again = grok.sync(self.con, root=alias)
        self.assertEqual(again["responses_inserted"], 0)
        self.assertEqual(again["events_inserted"], 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sessions"), 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM responses"), responses)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM events"), events)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sources"), sources)

    def test_missing_receipt_root_is_a_gap_not_a_failure(self):
        self.grok_session(self.grok_root, GROK_NORMAL)
        gone = self.bare_sessions("grok", "gone")
        self.receipts(("grok", gone))
        stats = grok.sync(self.con)
        self.assertEqual(stats["failed"], [])
        self.assertEqual(stats["sources"], 1)
        self.assertEqual(stats["bare_roots"]["gaps"],
                         [{"root": gone, "reason": "missing on this machine"}])


class UpgradeKeepsStoredPathsTest(BareRootCase):
    """A ledger written before #92 through a symlinked home never forks."""

    def count(self, table):
        return self.scalar(f"SELECT COUNT(*) FROM {table}")

    def test_codex_rows_stored_through_a_symlinked_home_are_reused(self):
        real = os.path.join(self.base, "data", "codex")
        path = self.rollout(os.path.join(real, "sessions"), "thr-n", "resp-n",
                            1000)
        link = os.path.join(self.base, "home-codex")
        os.symlink(real, link)
        # Written as main stores it: the path discovery globbed, unresolved.
        codex.import_codex_file(self.con, os.path.join(
            link, "sessions", os.path.relpath(path, os.path.join(real, "sessions"))))
        before = {t: self.count(t) for t in
                  ("sources", "codex_occurrences", "sessions", "responses")}
        self.assertEqual(before["sources"], 1)
        stored = self.scalar("SELECT path FROM sources")

        # A default sync through the same symlinked home, then through the
        # real path.
        for home in (link, real):
            with patch.object(codex, "DEFAULT_ROOT",
                              os.path.join(home, "sessions")):
                stats = codex.sync(self.con)
            self.assertEqual(stats["responses_inserted"], 0)
            self.assertEqual(stats["unchanged"], 1)
            self.assertEqual({t: self.count(t) for t in before}, before)
            self.assertEqual(self.scalar("SELECT path FROM sources"), stored)

    def test_grok_rows_stored_through_a_symlinked_home_are_reused(self):
        real = os.path.join(self.base, "data", "grok", "sessions")
        self.grok_session(real, GROK_NORMAL)
        link = os.path.join(self.base, "home-grok")
        os.symlink(real, link)
        grok.import_grok_session(self.con, os.path.join(link, GROK_GROUP,
                                                        GROK_NORMAL))
        self.con.commit()
        before = {t: self.count(t) for t in
                  ("sources", "sessions", "responses", "events")}
        self.assertGreater(before["sources"], 0)

        with patch.object(grok, "DEFAULT_ROOT", real):
            stats = grok.sync(self.con)

        self.assertEqual(stats["responses_inserted"], 0)
        self.assertEqual(stats["events_inserted"], 0)
        self.assertEqual({t: self.count(t) for t in before}, before)
        self.assertEqual(self.scalar(
            "SELECT COUNT(*) FROM sources WHERE path LIKE ?",
            (real + "%",)), 0)


class FirstSyncAliasesTest(BareRootCase):
    def counts(self, con, tables):
        return {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables}

    def assert_sync_once(self, adapter, paths, source_count, tables):
        # Import each native input once as the accounting reference.
        expected = db.connect(os.path.join(self.base, "expected.db"))
        self.addCleanup(expected.close)
        db.init_db(expected)
        for path in paths:
            adapter.sync(expected, source=path)
        counts = self.counts(expected, tables)
        self.assertEqual(counts["sources"], source_count)

        stats = adapter.sync(self.con)
        self.assertEqual(stats["failed"], [])
        # Before #124 this is four Codex rows for three native files.
        self.assertEqual(self.counts(self.con, tables), counts)
        self.assertEqual(stats["sources"], len(paths))
        self.assertEqual(stats["responses_inserted"], counts["responses"])
        self.assertEqual(stats["unchanged"], 0)
        stored = [tuple(row) for row in self.con.execute(
            "SELECT * FROM sources ORDER BY id")]
        self.assertEqual({row[0] for row in self.con.execute(
            "SELECT path FROM sources")},
            {row[0] for row in expected.execute("SELECT path FROM sources")})
        before = list(self.con.iterdump())

        again = adapter.sync(self.con)
        self.assertEqual(again["failed"], [])
        self.assertEqual(again["sources"], len(paths))
        self.assertEqual(again["unchanged"], len(paths))
        for key in ("responses_inserted", "events_inserted",
                    "user_messages_inserted", "malformed"):
            self.assertEqual(again[key], 0)
        self.assertEqual(self.counts(self.con, tables), counts)
        self.assertEqual([tuple(row) for row in self.con.execute(
            "SELECT * FROM sources ORDER BY id")], stored)
        self.assertEqual(list(self.con.iterdump()), before)

    def test_codex_first_sync_keeps_first_sorted_path_for_three_files(self):
        real = os.path.join(self.base, "z-real-codex")
        shared = self.rollout(os.path.join(real, "sessions"),
                              "thr-shared", "resp-shared", 100)
        os.symlink(real, self.codex_home)
        bare_root = self.bare_sessions("codex", "a")
        live = self.rollout(bare_root, "thr-live", "resp-live", 200)
        archived = self.rollout(os.path.join(os.path.dirname(bare_root),
                                            "archived_sessions"),
                                "thr-archived", "resp-archived", 300)
        self.receipts(("codex", os.path.join(real, "sessions")),
                      ("codex", bare_root))
        first = os.path.join(self.codex_home, os.path.relpath(shared, real))
        self.assertLess(first, shared)
        self.assert_sync_once(codex, [first, live, archived], 3,
                              ("sources", "sessions", "responses",
                               "codex_occurrences", "events"))

    def test_grok_first_sync_keeps_default_home_before_receipts(self):
        real = os.path.join(self.base, "a-real-grok-sessions")
        self.grok_session(real, GROK_NORMAL)
        os.makedirs(os.path.dirname(self.grok_root))
        os.symlink(real, self.grok_root)
        self.receipts(("grok", real))
        first = os.path.join(self.grok_root, GROK_GROUP, GROK_NORMAL)
        self.assert_sync_once(grok, [first], 2,
                              ("sources", "sessions", "responses", "events"))

    def test_grok_shared_updates_keeps_distinct_events(self):
        self.grok_session(self.grok_root, GROK_NORMAL)
        first = os.path.join(self.grok_root, GROK_GROUP, GROK_NORMAL)
        second = os.path.join(self.grok_root, "z-other-group", GROK_NORMAL)
        shutil.copytree(first, second)
        shared = os.path.join(second, "updates.jsonl")
        os.unlink(shared)
        os.link(os.path.join(first, "updates.jsonl"), shared)
        events = os.path.join(second, "events.jsonl")
        with open(events, "a") as fh:
            fh.write(json.dumps({"ts": "2026-09-01T10:00:09Z",
                                 "type": "tool_completed",
                                 "tool_name": "read_file",
                                 "tool_call_id": "distinct-event",
                                 "duration_ms": 17, "outcome": "success"})
                     + "\n")

        with patch.object(grok, "_load_jsonl_records",
                          wraps=grok._load_jsonl_records) as load:
            stats = grok.sync(self.con, root=self.grok_root)
        self.assertEqual(stats["failed"], [])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sources"), 3)
        self.assertEqual(self.scalar(
            "SELECT COUNT(*) FROM events WHERE duration_ms=17"), 1)
        self.assertEqual({row[0] for row in self.con.execute(
            "SELECT path FROM sources")},
            {os.path.join(first, "updates.jsonl"),
             os.path.join(first, "events.jsonl"), events})
        self.assertEqual([call.args[0] for call in load.call_args_list].count(
            os.path.join(first, "updates.jsonl")), 1)
        self.assertNotIn(shared, [call.args[0] for call in load.call_args_list])

        before = list(self.con.iterdump())
        again = grok.sync(self.con, root=self.grok_root)
        self.assertEqual(again["failed"], [])
        self.assertEqual((again["sources"], again["unchanged"]), (2, 2))
        self.assertEqual(again["responses_inserted"], 0)
        self.assertEqual(again["events_inserted"], 0)
        self.assertEqual(list(self.con.iterdump()), before)

        with patch.object(grok, "_load_jsonl_records",
                          wraps=grok._load_jsonl_records) as load:
            full = grok.sync(self.con, root=self.grok_root, full=True)
        self.assertEqual(full["failed"], [])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sources"), 3)
        self.assertEqual(self.scalar(
            "SELECT COUNT(*) FROM events WHERE duration_ms=17"), 1)
        self.assertEqual([call.args[0] for call in load.call_args_list],
                         [os.path.join(first, "updates.jsonl"),
                          os.path.join(first, "events.jsonl"), events])

    def test_codex_hardlinks_in_an_explicit_root_import_once(self):
        root = os.path.join(self.base, "hardlinks")
        first = self.rollout(root, "thr-hardlink", "resp-hardlink", 100)
        alias = os.path.join(root, "rollout-z-alias.jsonl")
        os.link(first, alias)
        stats = codex.sync(self.con, root=root)
        self.assertEqual(stats["failed"], [])
        self.assertEqual(stats["sources"], 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM sources"), 1)
        self.assertEqual(self.scalar("SELECT path FROM sources"), first)

    def test_same_inode_on_different_devices_keeps_both_paths(self):
        seen = {}
        stats = [os.stat_result((0, 42, device, 0, 0, 0, 0, 0, 0, 0))
                 for device in (1, 2)]
        with patch("agent_observer.ingest.os.stat", side_effect=stats):
            self.assertEqual(stored_path(self.con, "codex", "first", seen),
                             "first")
            self.assertEqual(stored_path(self.con, "codex", "second", seen),
                             "second")


class ReceiptReadCostTest(BareRootCase):
    def test_one_default_sync_reads_receipts_once(self):
        self.receipts(("codex", self.bare_sessions("codex", "a")),
                      ("grok", self.bare_sessions("grok", "b")))
        real_connect = sqlite3.connect
        opened = []

        def counting(target, *args, **kwargs):
            if self.state in str(target):
                opened.append(target)
            return real_connect(target, *args, **kwargs)

        with patch.object(bare.sqlite3, "connect", counting), \
                redirect_stdout(io.StringIO()):
            cli.main(["--db", os.path.join(self.base, "cli.db"), "sync",
                      "--harness", "codex", "--harness", "grok"])
            self.assertEqual(len(opened), 1)
            health.bare_roots()
            self.assertEqual(len(opened), 2)

    def test_a_receipt_committed_to_the_wal_is_seen_by_the_next_run(self):
        first = self.bare_sessions("codex", "first")
        second = self.bare_sessions("codex", "second")
        self.rollout(first, "thr-first", "resp-first", 100)
        self.rollout(second, "thr-second", "resp-second", 200)
        self.receipts(("codex", first))
        writer = sqlite3.connect(self.state)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        self.assertEqual(codex.sync(self.con)["bare_roots"]["receipts"], 1)
        main_file = os.stat(self.state)

        # Chromeria commits a receipt; it stays in the WAL.
        payload = {"type": "system_notice", "bareReceipt": {"nativeRecord": {
            "harness": "codex", "sessionId": "s9", "root": second}}}
        writer.execute(
            "INSERT INTO orchestration_v2_projection_turn_items"
            " VALUES('bare-receipt:9', 't9', 0, 'system_notice', 'completed',"
            " '', ?)", (json.dumps(payload),))
        writer.commit()
        self.assertEqual(os.stat(self.state).st_mtime_ns, main_file.st_mtime_ns)
        self.assertEqual(os.stat(self.state).st_size, main_file.st_size)

        stats = codex.sync(self.con)
        self.assertEqual(stats["bare_roots"]["receipts"], 2)
        self.assertEqual(self.scalar(
            "SELECT COUNT(*) FROM sessions WHERE session_key='codex:thr-second'"),
            1)

    def test_receipt_query_searches_the_primary_key(self):
        self.receipts(("codex", self.bare_sessions("codex", "a")))
        native = sqlite3.connect(self.state)
        self.addCleanup(native.close)
        native.execute(
            "CREATE TABLE IF NOT EXISTS orchestration_v2_projection_turn_items"
            " (turn_item_id TEXT PRIMARY KEY)")
        plan = " ".join(r[-1] for r in native.execute(
            "EXPLAIN QUERY PLAN " + bare.RECEIPTS))
        self.assertIn("SEARCH", plan)
        self.assertNotIn("SCAN", plan)


class CliBareRootsTest(BareRootCase):
    def sync(self, *harnesses):
        out = io.StringIO()
        args = ["--db", os.path.join(self.base, "cli.db"), "sync"]
        for harness in harnesses:
            args += ["--harness", harness]
        with redirect_stdout(out):
            code = cli.main(args)
        return code, out.getvalue()

    def test_sync_with_gaps_prints_them_and_exits_zero(self):
        self.grok_session(self.grok_root, GROK_NORMAL)
        gone = self.bare_sessions("grok", "gone")
        self.receipts(("grok", gone))
        code, text = self.sync("codex", "grok")
        self.assertEqual(code, 0)
        self.assertIn("  bare roots: 0 read from 1 receipts, 1 gaps\n", text)
        self.assertIn(f"  gap grok bare root {gone}: missing on this machine",
                      text)
        self.assertIn(f"  bare roots: no bare receipts in {self.state}\n", text)

    def test_sync_without_t3_state_says_so(self):
        self.grok_session(self.grok_root, GROK_NORMAL)
        code, text = self.sync("grok")
        self.assertEqual(code, 0)
        state = os.path.join(self.t3, "userdata", "state.sqlite")
        self.assertIn(f"  bare roots: no T3 state in {state}\n", text)


class HealthBareRootsTest(BareRootCase):
    def test_health_reports_gaps_beside_findings(self):
        gone = self.bare_sessions("codex", "gone")
        self.receipts(("codex", gone))
        result = health.check(processes=[], registry=[])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["bare_roots"]["codex"]["gaps"],
                         [{"root": gone, "reason": "missing on this machine"}])
        self.assertEqual(result["bare_roots"]["grok"]["state"], "unused")

    def test_health_without_lsof_reports_both_gaps_in_one_run(self):
        gone = self.bare_sessions("codex", "gone")
        self.receipts(("codex", gone))
        tools = os.path.join(self.base, "bin")
        os.makedirs(tools)
        ps = os.path.join(tools, "ps")
        with open(ps, "w") as fh:
            fh.write("#!/bin/sh\nprintf '201 Mon Oct 19 10:00:00 2026 codex\\n'\n")
        os.chmod(ps, 0o700)
        for as_json in (False, True):
            out = io.StringIO()
            args = ["--db", os.path.join(self.base, "missing.db"), "health"]
            with self.subTest(as_json=as_json), \
                    patch.dict(os.environ, {"PATH": tools}), \
                    patch.object(health, "claude_registry", return_value=[]), \
                    redirect_stdout(out):
                code = cli.main(args + (["--json"] if as_json else []))
            self.assertEqual(code, 4)
            if as_json:
                result = json.loads(out.getvalue())
                self.assertIn("lsof", result["unavailable"][0]["reason"])
                self.assertEqual(result["bare_roots"]["codex"]["gaps"], [
                    {"root": gone, "reason": "missing on this machine"}])
            else:
                text = out.getvalue()
                self.assertIn("working directory unavailable:", text)
                self.assertIn("lsof", text)
                self.assertIn(f"- codex gap codex bare root {gone}:"
                              " missing on this machine", text)


if __name__ == "__main__":
    unittest.main()
