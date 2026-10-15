"""Schema-3 queries work before the occurrence-table migration (#98)."""

import hashlib
import io
import os
import shutil
import sqlite3
from contextlib import closing, redirect_stdout

from agent_observer import cli, codex_order, db, lookup, placement, report
from agent_observer.adapters.codex import import_codex_file
from tests.helpers import LedgerCase, fixture
from tests.test_capture import _assign, _setup_tasks


def _state(path):
    with open(path, "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    with closing(db.connect_read_only(path)) as con:
        schema = [tuple(row) for row in con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name")]
        metadata = [tuple(row) for row in con.execute(
            "SELECT * FROM schema_meta ORDER BY key")]
    return digest, schema, metadata


class QueryUpgradeTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.native = os.path.join(self.tmp.name, "rollout-upgrade.jsonl")
        shutil.copyfile(fixture("codex-mini.jsonl"), self.native)
        import_codex_file(self.con, self.native)
        _setup_tasks(self, "T-UPGRADE")
        for native_id in ("codex:msg-mini-sub-01", "codex:msg-mini-sub-02"):
            _assign(self, native_id, "T-UPGRADE")
        self.session = self.con.execute(
            "SELECT session_key FROM sessions").fetchone()[0]
        self.event_ids = {("event", str(row[0])) for row in self.con.execute(
            "SELECT id FROM events WHERE session_key=?", (self.session,))}
        self.old = os.path.join(self.tmp.name, "old-schema3.sqlite")
        with closing(sqlite3.connect(self.old)) as old:
            self.con.backup(old)
            old.execute("DROP TABLE codex_occurrences")
            old.commit()

    def test_old_availability_preserves_evidence_without_writing(self):
        before = _state(self.old)
        with closing(db.connect_read_only(self.old)) as con:
            self.assertEqual(con.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0], "3")
            for harness in (None, "codex", "opencode"):
                with self.subTest(harness=harness):
                    found = lookup.availability(con, harness=harness)
                    self.assertEqual(found["status"], "available")
            found = lookup.availability(con, session_key=self.session)
            self.assertEqual(found["events"]["live"], len(self.event_ids))
        os.remove(self.native)
        with closing(db.connect_read_only(self.old)) as con:
            found = lookup.availability(con, session_key=self.session)
            self.assertEqual(found["status"], "ledger_only")
            self.assertEqual(found["events"]["ledger_only"], len(self.event_ids))
            self.assertFalse(report.task_report(con, "T-UPGRADE")["complete"])
        self.assertEqual(_state(self.old), before)

    def test_old_task_report_and_cli_preserve_the_cutoff_without_writing(self):
        before = _state(self.old)
        expected = report.task_report(self.con, "T-UPGRADE")
        with closing(db.connect_read_only(self.old)) as con:
            self.assertEqual(report._source_cutoff(con, {self.session}),
                             expected["source_cutoff"])
            found = report.task_report(con, "T-UPGRADE")
            self.assertEqual(found["attributed"], expected["attributed"])
            self.assertEqual(found["source_cutoff"], expected["source_cutoff"])
            self.assertEqual(found["source_availability"],
                             expected["source_availability"])
        for args in (("sessions", "availability", "--harness", "opencode"),
                     ("sessions", "list"),
                     ("task", "show", "--task", "T-UPGRADE")):
            with self.subTest(args=args), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--db", self.old, *args, "--json"]), 0)
        self.assertEqual(_state(self.old), before)

    def test_absent_occurrences_do_not_become_a_known_source_order(self):
        before = _state(self.old)
        with closing(db.connect_read_only(self.old)) as con:
            self.assertEqual(codex_order.resolve(
                con, self.session, self.event_ids), ("absent", None, []))
            found = report.timeline(con, session_key=self.session)
            self.assertEqual({row["id"] for row in found["events"]},
                             {int(key) for _, key in self.event_ids})
            self.assertNotIn("source_sequences", found)
            self.assertFalse(placement._has_occurrences(con, self.session))
        self.assertEqual(_state(self.old), before)

    def test_explicit_migration_and_import_restore_occurrence_aware_reads(self):
        with closing(db.connect(self.old)) as con:
            db.init_db(con)
            self.assertEqual(codex_order.resolve(
                con, self.session, self.event_ids), ("absent", None, []))
            import_codex_file(con, self.native, full=True)
            con.commit()
        before = _state(self.old)
        with closing(db.connect_read_only(self.old)) as con:
            state, order, sequences = codex_order.resolve(
                con, self.session, self.event_ids)
            self.assertEqual(state, "known")
            self.assertEqual(set().union(*order), self.event_ids)
            self.assertTrue(sequences)
            self.assertTrue(placement._has_occurrences(con, self.session))
            self.assertEqual(lookup.availability(
                con, session_key=self.session)["status"], "available")
            self.assertEqual(report.task_report(con, "T-UPGRADE")["attributed"],
                             report.task_report(self.con, "T-UPGRADE")["attributed"])
        self.assertEqual(_state(self.old), before)

    def test_migrated_occurrence_only_source_still_affects_reports(self):
        with closing(db.connect(self.old)) as con:
            db.init_db(con)
            import_codex_file(con, self.native, full=True)
            sid = con.execute(
                "INSERT INTO sources(harness, path, sha256, imported_at)"
                " VALUES('codex', ?, 'missing', 9999999999)",
                (os.path.join(self.tmp.name, "missing-native.jsonl"),)).lastrowid
            con.execute(
                "INSERT INTO codex_occurrences(source_id, session_key, row_kind,"
                " row_key, record_position) SELECT ?, session_key, row_kind,"
                " row_key, record_position FROM codex_occurrences LIMIT 1", (sid,))
            con.commit()
        before = _state(self.old)
        with closing(db.connect_read_only(self.old)) as con:
            found = report.task_report(con, "T-UPGRADE")
            self.assertEqual(found["source_cutoff"], 9999999999)
            self.assertIn(sid, found["coverage"]["unavailable_sources"])
            self.assertEqual(found["source_availability"]["status"], "partial")
            self.assertFalse(found["complete"])
        self.assertEqual(_state(self.old), before)
