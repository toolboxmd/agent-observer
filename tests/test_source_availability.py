"""A session says when its indexed events no longer have a native source (#86)."""

import os
import shutil
import sqlite3
import subprocess
import sys

from agent_observer import db, lookup, report
from agent_observer.adapters import grok, opencode
from agent_observer.adapters.codex import import_codex_file
from tests.helpers import LedgerCase, fixture
from tests.test_capture import _assign, _setup_tasks
from tests.test_lookup import _codex_rollout, _grok_session
from tests.test_opencode import build_native

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "rollout-2026-10-01T10-00-00-thr-avail.jsonl"


def _place(root, kind):
    path = os.path.join(root, kind, "2026", "10", "01", NAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


class SourceAvailabilityTest(LedgerCase):
    def _import(self):
        self.live = _place(self.tmp.name, "sessions")
        _codex_rollout(self.live)
        import_codex_file(self.con, self.live)
        self.con.commit()
        self.event_id = self.con.execute(
            "SELECT id FROM events WHERE family='tool_call' LIMIT 1"
        ).fetchone()["id"]
        self.session_key = self.con.execute(
            "SELECT session_key FROM events WHERE id=?",
            (self.event_id,)).fetchone()["session_key"]

    def test_missing_file_fails_hydration_and_marks_the_session(self):
        self._import()
        os.remove(self.live)
        [item] = lookup.show(self.con, [self.event_id])
        self.assertEqual(item["status"], "source_missing")
        self.assertEqual(item["reason"], "gone")
        for key in ("input", "result", "text", "record", "assistant_text"):
            self.assertNotIn(key, item)
        session = lookup.availability(self.con, session_key=self.session_key)
        self.assertEqual(session["status"], "ledger_only")
        self.assertGreater(session["events"]["ledger_only"], 0)
        self.assertEqual(session["events"]["live"], 0)
        self.assertEqual(session["reasons"].get("gone"),
                         session["events"]["ledger_only"])
        text = lookup.render([item])
        self.assertIn("source_missing", text)
        self.assertIn("reason: gone", text)
        self.assertNotIn("secret body line", text)

    def test_archived_copy_is_read_and_a_changed_copy_is_not(self):
        self._import()
        archived = _place(self.tmp.name, "archived_sessions")
        shutil.copy(self.live, archived)
        os.remove(self.live)
        [item] = lookup.show(self.con, [self.event_id])
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["reason"], "relocated")
        self.assertEqual(item["read_path"], archived)
        self.assertIn("cmd", item["input"])
        session = lookup.availability(self.con, session_key=self.session_key)
        self.assertEqual(session["status"], "available")
        self.assertGreater(session["events"]["relocated"], 0)
        self.assertEqual(session["events"]["ledger_only"], 0)
        with open(archived, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        [changed] = lookup.show(self.con, [self.event_id])
        self.assertEqual(changed["status"], "source_changed")
        self.assertEqual(changed["reason"], "replaced")
        self.assertNotIn("input", changed)

    def test_two_archive_copies_are_not_silently_chosen(self):
        self._import()
        first = _place(self.tmp.name, "archived_sessions")
        second = os.path.join(
            self.tmp.name, "archived_sessions", "other", NAME)
        os.makedirs(os.path.dirname(second), exist_ok=True)
        shutil.copy(self.live, first)
        shutil.copy(self.live, second)
        os.remove(self.live)
        [item] = lookup.show(self.con, [self.event_id])
        self.assertEqual(item["status"], "source_missing")
        self.assertEqual(item["reason"], "ambiguous")
        self.assertNotIn("input", item)

    def test_task_is_not_complete_when_its_source_is_gone(self):
        path = os.path.join(self.tmp.name, "codex-mini.jsonl")
        shutil.copy(fixture("codex-mini.jsonl"), path)
        import_codex_file(self.con, path)
        self.con.commit()
        _setup_tasks(self, "T-A")
        _assign(self, "codex:msg-mini-sub-01", "T-A")
        _assign(self, "codex:msg-mini-sub-02", "T-A")
        before = report.task_report(self.con, "T-A")
        self.assertEqual(before["source_availability"]["status"], "available")
        self.assertEqual(before["coverage"]["ledger_only_events"], 0)
        self.assertTrue(before["complete"])
        os.remove(path)
        after = report.task_report(self.con, "T-A")
        self.assertEqual(after["source_availability"]["status"], "ledger_only")
        self.assertGreater(after["coverage"]["ledger_only_events"], 0)
        self.assertFalse(after["complete"])
        self.assertNotEqual(before["snapshot_id"], after["snapshot_id"])

    def test_cli_reports_the_machine_gap(self):
        self._import()
        os.remove(self.live)
        env = dict(os.environ, AGENT_OBSERVER_DB=self.db_path)
        result = subprocess.run(
            [sys.executable, "-m", "agent_observer", "sessions",
             "availability", "--json"],
            cwd=REPO, capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('"ledger_only"', result.stdout)
        text = subprocess.run(
            [sys.executable, "-m", "agent_observer", "sessions",
             "availability", "--session", self.session_key],
            cwd=REPO, capture_output=True, text=True, env=env)
        self.assertEqual(text.returncode, 3, text.stderr)
        self.assertIn("ledger-only", text.stdout)
        self.assertNotIn(self.live, text.stdout)

    def _bind_all(self, task):
        _setup_tasks(self, task)
        rows = self.con.execute("SELECT native_id FROM user_messages").fetchall()
        self.assertGreater(len(rows), 0)
        for row in rows:
            _assign(self, row["native_id"], task)

    def test_deleted_opencode_session_is_not_available(self):
        native_path = os.path.join(self.tmp.name, "opencode.db")
        build_native(native_path)
        opencode.sync(self.con, source=native_path)
        self.con.commit()
        self._bind_all("T-OC")
        session_key = self.con.execute(
            "SELECT session_key FROM events WHERE session_key LIKE 'opencode:%'"
            " LIMIT 1"
        ).fetchone()["session_key"]
        event_id = self.con.execute(
            "SELECT id FROM events WHERE session_key=? LIMIT 1",
            (session_key,)).fetchone()["id"]
        before = report.task_report(self.con, "T-OC")
        self.assertEqual(before["source_availability"]["status"], "available")
        self.assertTrue(before["complete"])
        native = sqlite3.connect(native_path)
        native.execute("DELETE FROM session")
        native.execute("DELETE FROM message")
        native.execute("DELETE FROM part")
        native.commit()
        native.close()
        [item] = lookup.show(self.con, [event_id])
        self.assertEqual(item["status"], "source_missing")
        self.assertEqual(item["reason"], "gone")
        self.assertNotIn("input", item)
        self.assertNotIn("record", item)
        session = lookup.availability(self.con, session_key=session_key)
        self.assertEqual(session["status"], "ledger_only")
        after = report.task_report(self.con, "T-OC")
        self.assertFalse(after["complete"])
        self.assertNotEqual(before["snapshot_id"], after["snapshot_id"])

    def test_grok_without_updates_is_not_available(self):
        root = os.path.join(self.tmp.name, "grok")
        _grok_session(root)
        grok.sync(self.con, root=root)
        self.con.commit()
        session_key = self.con.execute(
            "SELECT session_key FROM sessions WHERE harness='grok'"
        ).fetchone()["session_key"]
        self.con.execute(
            "INSERT INTO tasks(task_id, project, family, title, created_at)"
            " VALUES(?,?,?,?,?)",
            ("T-G", "observer", "research", "T-G", db.now()))
        self.con.execute(
            "INSERT INTO session_assignments(session_key, task_id, evidence,"
            " created_at) VALUES(?,?,?,?)",
            (session_key, "T-G", "test binding", db.now()))
        self.con.commit()
        updates = self.con.execute(
            "SELECT path FROM sources WHERE path LIKE '%updates.jsonl'"
        ).fetchone()["path"]
        event_id = self.con.execute(
            "SELECT id FROM events WHERE session_key=? LIMIT 1",
            (session_key,)).fetchone()["id"]
        before = report.task_report(self.con, "T-G")
        self.assertEqual(before["source_availability"]["status"], "available")
        self.assertTrue(before["complete"])
        os.remove(updates)
        [item] = lookup.show(self.con, [event_id])
        self.assertEqual(item["status"], "source_missing")
        self.assertEqual(item["reason"], "gone")
        self.assertNotIn("record", item)
        self.assertNotIn("input", item)
        session = lookup.availability(self.con, session_key=session_key)
        self.assertEqual(session["status"], "ledger_only")
        after = report.task_report(self.con, "T-G")
        self.assertFalse(after["complete"])
        self.assertNotEqual(before["snapshot_id"], after["snapshot_id"])
