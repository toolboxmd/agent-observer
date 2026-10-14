"""Default Codex sync reads live and archived rollouts once (#77).

A default sync (no root) must import ``~/.codex/sessions`` and
``~/.codex/archived_sessions``. This rejects an adapter that still scans
only the live tree, and one that imports both copies of the same rollout
basename and sums them. Continuation files keep distinct names, so this
dedup is by basename, not by thread id. An explicit root stays one
directory and is not what these tests call.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from agent_observer import db
from agent_observer.adapters import codex


def _usage(total):
    return {"cache_write_input_tokens": 0, "cached_input_tokens": 0,
            "input_tokens": total - 10, "output_tokens": 10,
            "reasoning_output_tokens": 0, "total_tokens": total}


def _rollout(thread, response_id, total=1000):
    """One session_meta plus one token_usage_record, as a real rollout."""
    turn = f"turn-{thread}"
    return [
        {"type": "session_meta",
         "payload": {"id": thread, "session_id": thread,
                     "cli_version": "0.151.0", "cwd": "/w",
                     "originator": "codex-tui", "thread_source": "user"}},
        {"type": "token_usage_record", "payload": {
            "response_id": response_id, "session_id": thread,
            "thread_id": thread, "turn_id": turn, "root_turn_id": turn,
            "usage": _usage(total), "turn_token_usage": _usage(total),
            "thread_token_usage": _usage(total)}},
    ]


def _write(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for ordinal, rec in enumerate(records):
            rec = dict(rec, ordinal=ordinal,
                       timestamp=f"2026-09-01T10:{ordinal:02d}:00.000Z")
            fh.write(json.dumps(rec) + "\n")


class ArchivedRolloutSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = os.path.join(self.tmp.name, "sessions")
        self.archive = os.path.join(self.tmp.name, "archived_sessions")
        self.addCleanup(self.tmp.cleanup)

    def ledger(self):
        con = db.connect(os.path.join(self.tmp.name, "observer.db"))
        db.init_db(con)
        self.addCleanup(con.close)
        return con

    def sync_defaults(self, con):
        with patch.object(codex, "DEFAULT_ROOT", self.live), \
                patch.object(codex, "ARCHIVE_ROOT", self.archive):
            return codex.sync(con)

    def test_live_and_archived_rollouts_both_import_once(self):
        live_name = "rollout-2026-09-01T10-00-00-thr-live.jsonl"
        arch_name = "rollout-2026-08-01T10-00-00-thr-arch.jsonl"
        _write(os.path.join(self.live, "2026", "09", "01", live_name),
               _rollout("thr-live", "resp-live", 1000))
        _write(os.path.join(self.archive, "2026", "08", "01", arch_name),
               _rollout("thr-arch", "resp-arch", 400))
        con = self.ledger()
        stats = self.sync_defaults(con)
        keys = {row[0] for row in con.execute("SELECT session_key FROM sessions")}
        self.assertEqual(keys, {"codex:thr-live", "codex:thr-arch"})
        counted = con.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(total_tokens), 0) s"
            " FROM responses WHERE is_overlap=0").fetchone()
        self.assertEqual(counted["c"], 2)
        self.assertEqual(counted["s"], 1400)
        self.assertEqual(stats["sources"], 2)
        self.assertEqual(stats["responses_inserted"], 2)

    def test_same_basename_imports_the_live_copy_once(self):
        name = "rollout-2026-09-02T11-00-00-thr-same.jsonl"
        records = _rollout("thr-same", "resp-same", 1000)
        live_path = os.path.join(self.live, "2026", "09", "02", name)
        _write(live_path, records)
        _write(os.path.join(self.archive, "2026", "09", "01", name), records)
        con = self.ledger()
        stats = self.sync_defaults(con)
        keys = [row[0] for row in con.execute("SELECT session_key FROM sessions")]
        sources = [row[0] for row in con.execute("SELECT path FROM sources")]
        counted = con.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(total_tokens), 0) s"
            " FROM responses WHERE is_overlap=0").fetchone()
        self.assertEqual(keys, ["codex:thr-same"])
        self.assertEqual(sources, [live_path])
        self.assertEqual(counted["c"], 1)
        self.assertEqual(counted["s"], 1000)
        self.assertEqual(stats["sources"], 1)
        self.assertEqual(stats["responses_inserted"], 1)
