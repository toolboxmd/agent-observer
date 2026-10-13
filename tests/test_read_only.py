"""Read-only commands on an idle ledger (#63).

When the last connection to a WAL ledger closes, SQLite removes its -wal
and -shm files. A `?mode=ro` open cannot recreate -shm, so read-only
commands (export, task, event) failed on an idle machine.
"""

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_observer import cli, db


def _digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class IdleLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "observer.db")
        con = db.connect(self.path)
        db.init_db(con)
        con.commit()
        con.close()
        # Reach the idle state on every build: Apple's SQLite keeps empty
        # -wal and -shm files after the last close, newer builds remove them.
        if os.path.exists(self.path + "-wal"):
            self.assertEqual(os.path.getsize(self.path + "-wal"), 0)
            os.remove(self.path + "-wal")
        if os.path.exists(self.path + "-shm"):
            os.remove(self.path + "-shm")

    def tearDown(self):
        self.tmp.cleanup()

    def sidecars(self):
        return [p for p in (self.path + "-wal", self.path + "-shm")
                if os.path.exists(p)]

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--db", self.path, *argv])
        return code, out.getvalue()

    def test_read_only_commands_open_an_idle_ledger_without_writing(self):
        self.assertEqual(self.sidecars(), [])  # the idle state
        before = _digest(self.path)
        con = db.connect_read_only(self.path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions")
                         .fetchone()[0], 0)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("INSERT INTO tasks(task_id, created_at) VALUES('x', 1)")
        con.close()
        code, out = self.run_cli("export")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["sessions"], [])
        code, _ = self.run_cli("task", "list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(_digest(self.path), before)

    def test_old_sqlite_that_cannot_open_idle_wal_read_only(self):
        # Apple's system SQLite (3.43, /usr/bin/python3 on macOS) refuses a
        # ?mode=ro open of a WAL ledger whose -shm is gone: "unable to open
        # database file". Newer builds open it, so the refusal is simulated
        # at the SQLite boundary; the real check ran on cavallo.
        # The open itself succeeds; the first statement fails, as on cavallo.
        real = sqlite3.connect
        ledger = self.path

        class Unreadable(sqlite3.Connection):
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("unable to open database file")

        def old_sqlite(target, *args, **kwargs):
            if isinstance(target, str) and target.endswith("?mode=ro") \
                    and os.path.exists(ledger) \
                    and not os.path.exists(ledger + "-shm"):
                kwargs["factory"] = Unreadable
            return real(target, *args, **kwargs)

        before = _digest(self.path)
        with mock.patch("agent_observer.db.sqlite3.connect", old_sqlite):
            con = db.connect_read_only(self.path)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions")
                             .fetchone()[0], 0)
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("INSERT INTO tasks(task_id, created_at)"
                            " VALUES('x', 1)")
            con.close()
            code, _ = self.run_cli("export")
            self.assertEqual(code, 0)
            with self.assertRaises(sqlite3.Error):
                db.connect_read_only(os.path.join(self.tmp.name, "absent.db"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name,
                                                     "absent.db")))
        self.assertEqual(_digest(self.path), before)

    def test_fallback_never_creates_a_ledger(self):
        # A ledger moved away between the read-only probe and the reopen
        # must stay missing, and the original read-only error is reported.
        real = sqlite3.connect
        missing = os.path.join(self.tmp.name, "moved.db")

        class Unreadable(sqlite3.Connection):
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("ro probe refused")

        def probe_fails(target, *args, **kwargs):
            if isinstance(target, str) and target.endswith("?mode=ro"):
                return real(":memory:", factory=Unreadable)
            return real(target, *args, **kwargs)

        with mock.patch("agent_observer.db.sqlite3.connect", probe_fails), \
                mock.patch("agent_observer.db.os.path.isfile",
                           return_value=True):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                db.connect_read_only(missing)
        self.assertEqual(str(caught.exception), "ro probe refused")
        self.assertFalse(os.path.exists(missing))

    def test_no_fallback_while_the_wal_holds_frames(self):
        # A reader's close can checkpoint a non-empty WAL into the ledger;
        # a read-only command must refuse rather than write.
        writer = sqlite3.connect(self.path)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO tasks(task_id, created_at) VALUES('t', 1)")
        writer.commit()
        self.assertGreater(os.path.getsize(self.path + "-wal"), 0)
        before = _digest(self.path)
        real = sqlite3.connect

        class Unreadable(sqlite3.Connection):
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("ro probe refused")

        def probe_fails(target, *args, **kwargs):
            if isinstance(target, str) and target.endswith("?mode=ro"):
                return real(":memory:", factory=Unreadable)
            return real(target, *args, **kwargs)

        with mock.patch("agent_observer.db.sqlite3.connect", probe_fails):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                db.connect_read_only(self.path)
        self.assertEqual(str(caught.exception), "ro probe refused")
        self.assertEqual(_digest(self.path), before)
        writer.close()

    def test_missing_ledger_is_never_created(self):
        missing = os.path.join(self.tmp.name, "absent.db")
        with self.assertRaises(sqlite3.Error):
            db.connect_read_only(missing)
        self.assertFalse(os.path.exists(missing))

    def test_incompatible_schema_is_still_refused(self):
        con = sqlite3.connect(self.path)
        con.execute("UPDATE schema_meta SET value='1' WHERE key='schema_version'")
        con.commit()
        con.close()
        with self.assertRaises(RuntimeError):
            db.connect_read_only(self.path)


class LookupsDoNotWriteTest(unittest.TestCase):
    """Lookups read the ledger without the write lock (#71, #73).

    Opening lookups read-write ran init_db, which writes schema_meta, so
    every lookup needed the write lock and failed with "database is locked"
    while sync held it.
    """

    LOOKUPS = (
        ("trace", "--session", "codex:mini-session", "--json"),
        ("interventions", "--json"),
        ("intervention", "show", "codex:none", "--json"),
        ("sessions", "list", "--json"),
        ("sessions", "show", "--session", "codex:mini-session"),
        ("verdict", "list", "--behavior", "b", "--json"),
        ("verdict", "groups", "--behavior", "b", "--json"),
        ("recurrence", "--behavior", "b", "--json"),
        ("diagnose", "--json"),
        ("compare", "--json"),
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "ledger")
        os.mkdir(self.dir)
        self.path = os.path.join(self.dir, "observer.db")
        mini = os.path.join(os.path.dirname(__file__), "fixtures",
                            "codex-mini.jsonl")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--db", self.path, "sync", "--source",
                                       mini, "--harness", "codex"]), 0)
        con = sqlite3.connect(self.path)
        self.session = con.execute(
            "SELECT session_key FROM sessions").fetchone()[0]
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def lookups(self):
        for argv in self.LOOKUPS:
            yield tuple(self.session if a == "codex:mini-session" else a
                        for a in argv)

    def run_cli(self, path, argv):
        with redirect_stdout(io.StringIO()):
            return cli.main(["--db", path, *argv])

    def state(self):
        # A reader may recreate SQLite's -shm index and an empty -wal, as
        # any concurrent reader does; neither holds ledger content.
        state = {}
        for name in sorted(os.listdir(self.dir)):
            path = os.path.join(self.dir, name)
            if name.endswith("-shm"):
                continue
            if name.endswith("-wal"):
                self.assertEqual(os.path.getsize(path), 0)
                continue
            state[name] = (_digest(path), os.stat(path).st_mode)
        return state

    def test_lookups_leave_the_ledger_unchanged(self):
        before = self.state()
        for argv in self.lookups():
            with self.subTest(argv=argv):
                self.assertIn(self.run_cli(self.path, argv), (0, 3))
                self.assertEqual(self.state(), before)

    def test_lookups_succeed_while_a_writer_holds_the_lock(self):
        writer = sqlite3.connect(self.path, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO tasks(task_id, created_at)"
                       " VALUES('held', 1)")
        try:
            for argv in self.lookups():
                with self.subTest(argv=argv), \
                        mock.patch("agent_observer.db.sqlite3.connect",
                                   _without_busy_wait):
                    self.assertIn(self.run_cli(self.path, argv), (0, 3))
        finally:
            writer.execute("ROLLBACK")
            writer.close()

    def test_lookups_never_create_a_ledger(self):
        absent = os.path.join(self.dir, "absent.db")
        for argv in self.lookups():
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                self.assertEqual(self.run_cli(absent, argv), 2)
                self.assertFalse(os.path.exists(absent))

    def test_capabilities_need_no_ledger(self):
        absent = os.path.join(self.dir, "absent.db")
        self.assertEqual(self.run_cli(absent, ("trace", "--capabilities")), 0)
        self.assertFalse(os.path.exists(absent))


_real_connect = sqlite3.connect


def _without_busy_wait(*args, **kwargs):
    # A lookup that waits on the lock would block for the default 5 s and
    # then fail; with no wait it fails at once, so the test stays fast.
    kwargs["timeout"] = 0
    return _real_connect(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
