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
            health.bare_roots()
        self.assertEqual(len(opened), 1)

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


if __name__ == "__main__":
    unittest.main()
