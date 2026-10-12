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
from contextlib import redirect_stdout
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


if __name__ == "__main__":
    unittest.main()
