"""A session says when its indexed events no longer have a native source (#86)."""

import os
import shutil
import subprocess
import sys

from agent_observer import lookup, report
from agent_observer.adapters.codex import import_codex_file
from tests.helpers import LedgerCase, fixture
from tests.test_capture import _assign, _setup_tasks
from tests.test_lookup import _codex_rollout

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
