"""Schema 3 (#65): unified terms, migrated in place from schema 2.

Capture and verdict rows exist only in the ledger, so the migration must
keep every row. The fixture is a schema-2 ledger dumped by the schema-2
code with one row in every capture table.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent_observer import db, interventions, verdicts
from tests.helpers import fixture

OLD_COUNTS = {"submissions": 2, "tasks": 2, "assignments": 1,
              "session_assignments": 1, "dispatches": 1, "attempts": 2,
              "outcomes": 1, "verdicts": 2, "session_attributes": 1,
              "events": 1, "t3_threads": 1, "t3_turn_origins": 1}
RENAMED = {"submissions": "user_messages", "dispatches": "delegations",
           "attempts": "task_turns"}


def schema2_ledger(path):
    con = sqlite3.connect(path)
    with open(fixture("ledger-schema-2.sql"), encoding="utf-8") as fh:
        con.executescript(fh.read())
    con.close()


def version(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT value FROM schema_meta"
                           " WHERE key='schema_version'").fetchone()[0]
    finally:
        con.close()


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "observer.db")
        schema2_ledger(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def migrate(self):
        con = db.connect(self.path)
        db.init_db(con)
        return con

    def test_every_row_survives_under_its_new_name(self):
        con = self.migrate()
        self.assertEqual(version(self.path), "3")
        for old, count in OLD_COUNTS.items():
            table = RENAMED.get(old, old)
            self.assertEqual(con.execute(f"SELECT COUNT(*) FROM {table}")
                             .fetchone()[0], count, table)
        delegation = con.execute("SELECT * FROM delegations").fetchone()
        self.assertEqual((delegation["owning_user_message"],
                          delegation["child_thread"], delegation["child_turn"]),
                         ("claude:u1", "th-w", "tw"))
        turns = {r["turn_id"]: r for r in con.execute("SELECT * FROM task_turns")}
        self.assertEqual(turns["turn-w"]["role"], "child")
        self.assertEqual(turns["turn-p"]["role"], "parent")
        self.assertEqual(turns["turn-w"]["color_requested"], "route-b")
        assignment = con.execute("SELECT * FROM assignments").fetchone()
        self.assertEqual((assignment["user_message_id"], assignment["task_turn"]),
                         ("claude:u1", "a1"))
        self.assertEqual(
            {r["intervention_id"] for r in con.execute("SELECT * FROM verdicts")},
            {"sub:claude:u1", "sub:claude:u2"})
        self.assertEqual(con.execute("SELECT origin FROM tasks WHERE"
                                     " task_id='o/r#2'").fetchone()[0], "t3")
        con.close()

    def test_a_backup_is_taken_before_migrating(self):
        self.migrate().close()
        backup = self.path + ".schema-2.bak"
        self.assertEqual(version(backup), "2")
        con = sqlite3.connect(backup)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM attempts")
                         .fetchone()[0], 2)
        con.close()

    def test_old_names_stay_readable_for_one_release(self):
        # contextmd capture reads `submissions` directly.
        con = self.migrate()
        self.assertEqual(
            {r[0] for r in con.execute("SELECT native_id FROM submissions")},
            {"claude:u1", "claude:u2"})
        self.assertEqual(con.execute("SELECT worker_thread, owning_submission"
                                     " FROM dispatches").fetchone()[:],
                         ("th-w", "claude:u1"))
        self.assertEqual({r[0] for r in con.execute("SELECT role FROM attempts")},
                         {"parent", "worker"})
        with self.assertRaises(sqlite3.Error):
            con.execute("INSERT INTO submissions(native_id) VALUES('x')")
        con.close()

    def test_failed_migration_leaves_schema_2_untouched(self):
        real = db._MIGRATION_3
        broken = real[:3] + ("ALTER TABLE no_such_table RENAME TO boom",) \
            + real[3:]
        with mock.patch.object(db, "_MIGRATION_3", broken):
            con = db.connect(self.path)
            with self.assertRaises(sqlite3.Error):
                db.init_db(con)
            con.close()
        self.assertEqual(version(self.path), "2")
        con = sqlite3.connect(self.path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM attempts")
                         .fetchone()[0], 2)
        self.assertIsNone(con.execute(
            "SELECT name FROM sqlite_master WHERE name='task_turns'").fetchone())
        con.close()

    def test_verdicts_from_schema_2_still_report(self):
        con = self.migrate()
        con.row_factory = sqlite3.Row
        report = verdicts.recurrence(con, "correction")
        self.assertEqual((report["occurrences"], report["problems"]), (1, 1))
        found = interventions.select(con, created_by=None)
        self.assertIn("sub:claude:u2", {m["id"] for m in found["interventions"]})
        con.close()

    def test_migration_runs_once(self):
        self.migrate().close()
        con = self.migrate()
        self.assertEqual(version(self.path), "3")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM task_turns")
                         .fetchone()[0], 2)
        con.close()

    def test_read_only_commands_ask_for_a_sync_on_schema_2(self):
        with self.assertRaises(RuntimeError):
            db.connect_read_only(self.path)


class FreshLedgerTest(unittest.TestCase):
    def test_new_ledger_starts_at_schema_3_with_old_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "observer.db")
            con = db.connect(path)
            db.init_db(con)
            self.assertEqual(version(path), "3")
            con.execute("SELECT native_id FROM submissions").fetchall()
            self.assertFalse(os.path.exists(path + ".schema-2.bak"))
            con.close()


if __name__ == "__main__":
    unittest.main()


from tests.test_interventions import JUDGE, InterventionsCase  # noqa: E402


class OldNamesTest(InterventionsCase):
    """Schema-2 command names, flags and values work for one release."""

    def run_cli(self, *argv, stdin=None):
        import io
        from contextlib import redirect_stdout
        from agent_observer import cli
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin",
                                              io.StringIO(stdin or "")):
            code = cli.main(["--db", os.path.join(self.root, "observer.db"),
                             *argv])
        return code, out.getvalue()

    def test_old_judge_commands_and_values(self):
        import json
        self.con.commit()
        new_code, new = self.run_cli("interventions", "--json")
        old_code, old = self.run_cli("moments", "--origin", "human", "--json")
        self.assertEqual((new_code, old_code), (0, 0))
        users = [m for m in json.loads(new)["interventions"]
                 if m["created_by"] == "user"]
        self.assertEqual(json.loads(old)["interventions"], users)
        code, out = self.run_cli("moment", "show", "sub:claude:u2", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["interventions"][0]["status"], "ok")
        line = json.dumps({"moment": "sub:claude:u2", "behavior": "correction",
                           "judge": JUDGE, "verdict": "no"})
        code, out = self.run_cli("verdict", "add", "--json", stdin=line)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.con.execute(
            "SELECT intervention_id FROM verdicts").fetchone()[0],
            "sub:claude:u2")

    def test_old_capture_commands_and_role(self):
        self.con.execute("INSERT INTO tasks(task_id, created_at)"
                         " VALUES('o/r#9', 1)")
        self.con.commit()
        code, _ = self.run_cli("capture", "dispatch", "--submission",
                               "claude:u1", "--worker", "th-child-9",
                               "--worker-turn", "t9")
        self.assertEqual(code, 0)
        code, _ = self.run_cli("capture", "attempt", "--task", "o/r#9",
                               "--turn", "codex:t9", "--role", "worker")
        self.assertEqual(code, 0)
        code, _ = self.run_cli("capture", "assign", "--submission",
                               "claude:u1", "--task", "o/r#9",
                               "--attempt", "a9")
        self.assertEqual(code, 0)
        self.assertEqual(self.con.execute(
            "SELECT child_thread, child_turn FROM delegations").fetchone()[:],
            ("th-child-9", "t9"))
        self.assertEqual(self.con.execute(
            "SELECT role FROM task_turns").fetchone()[0], "child")
        self.assertEqual(self.con.execute(
            "SELECT task_turn FROM assignments").fetchone()[0], "a9")
