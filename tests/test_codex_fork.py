"""Forked Codex rollouts are their own sessions (#69).

A forked child rollout starts with its own session_meta, then a copy of
the parent's session_meta and the parent's history up to
subagent_history_start_ordinal, then its own records. The adapter used to
take the last session_meta, so the child's work landed in the parent's
session and the child had no session row.
"""

import json
import os
import tempfile
import unittest

from agent_observer import db
from agent_observer.adapters.codex import import_codex_file

PARENT = "thr-parent"
CHILD = "thr-child"


def _usage(total):
    return {"cache_write_input_tokens": 0, "cached_input_tokens": 0,
            "input_tokens": total - 10, "output_tokens": 10,
            "reasoning_output_tokens": 0, "total_tokens": total}


def _meta(thread, **extra):
    return {"type": "session_meta",
            "payload": {"id": thread, "session_id": PARENT,
                        "cli_version": "0.151.0", "cwd": "/w",
                        "originator": "codex-tui", **extra}}


def _parent_history(turn="turn-p1"):
    """The parent's own records; a fork copies them verbatim."""
    return [
        {"type": "event_msg", "payload": {"type": "task_started",
                                          "turn_id": turn}},
        {"type": "turn_context", "payload": {"turn_id": turn,
                                             "model": "gpt-p", "effort": "high"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user", "id": "msg-p1",
            "content": [{"type": "input_text", "text": "Plan the work."}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["user.text"], "turn_id": turn}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "CommandExecution", "id": "exec-p1", "exit_code": 0,
            "command": ["/bin/zsh", "-lc", "ls parent"], "status": "completed"}}},
        {"type": "token_usage_record", "payload": {
            "response_id": "resp-p1", "session_id": PARENT,
            "thread_id": PARENT, "turn_id": turn, "root_turn_id": turn,
            "usage": _usage(1000), "turn_token_usage": _usage(1000),
            "thread_token_usage": _usage(1000)}},
        {"type": "event_msg", "payload": {"type": "task_complete",
                                          "turn_id": turn}},
    ]


def _child_own(turn="turn-c1"):
    return [
        {"type": "event_msg", "payload": {"type": "task_started",
                                          "turn_id": turn}},
        {"type": "turn_context", "payload": {"turn_id": turn,
                                             "model": "gpt-c", "effort": "low"}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "CommandExecution", "id": "exec-c1", "exit_code": 0,
            "command": ["/bin/zsh", "-lc", "ls child"], "status": "completed"}}},
        {"type": "token_usage_record", "payload": {
            "response_id": "resp-c1", "session_id": PARENT,
            "thread_id": CHILD, "turn_id": turn, "root_turn_id": turn,
            "usage": _usage(500), "turn_token_usage": _usage(500),
            "thread_token_usage": _usage(500)}},
        # A legacy cumulative usage snapshot: its ledger id is derived from
        # the session key, so a re-key must carry it along.
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": _usage(300),
            "total_token_usage": _usage(800)}}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": _usage(400),
            "total_token_usage": _usage(1200)}}},
        {"type": "event_msg", "payload": {"type": "task_complete",
                                          "turn_id": turn}},
    ]


def _write(path, records, start_minute):
    with open(path, "w") as fh:
        for ordinal, rec in enumerate(records):
            rec = dict(rec, ordinal=ordinal,
                       timestamp=f"2026-09-01T10:{start_minute + ordinal:02d}:00.000Z")
            fh.write(json.dumps(rec) + "\n")


class ForkedRolloutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = os.path.join(self.tmp.name, "rollout-parent.jsonl")
        self.child = os.path.join(self.tmp.name, "rollout-child.jsonl")
        _write(self.parent, [_meta(PARENT, thread_source="user")]
               + _parent_history(), 0)
        inherited = [_meta(PARENT, thread_source="user")] + _parent_history()
        start = 1 + len(inherited)
        _write(self.child, [_meta(CHILD, thread_source="subagent",
                                  forked_from_id=PARENT,
                                  parent_thread_id=PARENT,
                                  subagent_history_start_ordinal=start)]
               + inherited + _child_own(), 20)

    def tearDown(self):
        self.tmp.cleanup()

    def ledger(self, name):
        con = db.connect(os.path.join(self.tmp.name, name))
        db.init_db(con)
        self.addCleanup(con.close)
        return con

    def snapshot(self, con):
        """Every row that says which session owns what."""
        def rows(sql):
            return sorted(tuple(r) for r in con.execute(sql))
        return {
            "sessions": rows("SELECT session_key, parent_session_key,"
                             " started_at, ended_at, project_dir,"
                             " (SELECT path FROM sources s"
                             "  WHERE s.id = sessions.source_id)"
                             " FROM sessions"),
            "responses": rows("SELECT response_id, session_key, total_tokens,"
                              " is_overlap FROM responses"),
            "events": rows("SELECT session_key, family, native_id FROM events"),
            "turns": rows("SELECT turn_id, session_key FROM turns"),
            "user_messages": rows("SELECT native_id, session_key,"
                                  " text_excerpt FROM user_messages"),
            "sources": rows("SELECT path, thread_id FROM sources"),
        }

    def assert_split(self, con):
        snap = self.snapshot(con)
        sessions = {r[0]: r for r in snap["sessions"]}
        self.assertEqual(set(sessions), {f"codex:{PARENT}", f"codex:{CHILD}"})
        self.assertEqual(sessions[f"codex:{CHILD}"][1], f"codex:{PARENT}")
        self.assertIsNone(sessions[f"codex:{PARENT}"][1])
        # Each session keeps its own source and time span.
        self.assertEqual(sessions[f"codex:{PARENT}"][5], self.parent)
        self.assertEqual(sessions[f"codex:{CHILD}"][5], self.child)
        self.assertLess(sessions[f"codex:{PARENT}"][3],
                        sessions[f"codex:{CHILD}"][2])
        self.assertEqual(snap["responses"], [
            ("codex:resp-c1", f"codex:{CHILD}", 500, 0),
            ("codex:resp-p1", f"codex:{PARENT}", 1000, 0),
            (f"codex:{CHILD}:tc:1200", f"codex:{CHILD}", 400, 0),
            (f"codex:{CHILD}:tc:800", f"codex:{CHILD}", 300, 0)])
        by_key = {}
        for key, _family, native in snap["events"]:
            by_key.setdefault(key, set()).add(native)
        # The copied parent history stays with the parent, once.
        self.assertIn("exec-p1", by_key[f"codex:{PARENT}"])
        self.assertNotIn("exec-p1", by_key[f"codex:{CHILD}"])
        self.assertIn("exec-c1", by_key[f"codex:{CHILD}"])
        self.assertNotIn("exec-c1", by_key[f"codex:{PARENT}"])
        self.assertEqual(dict(snap["turns"]), {
            "codex:turn-p1": f"codex:{PARENT}",
            "codex:turn-c1": f"codex:{CHILD}"})
        self.assertEqual(snap["user_messages"],
                         [("codex:msg-p1", f"codex:{PARENT}", "Plan the work.")])
        self.assertEqual(dict(snap["sources"]), {
            self.parent: PARENT, self.child: CHILD})
        return snap

    def test_parent_then_child(self):
        con = self.ledger("a.db")
        import_codex_file(con, self.parent)
        stats = import_codex_file(con, self.child)
        self.assertEqual(stats["session_key"], f"codex:{CHILD}")
        self.assert_split(con)

    def test_child_then_parent(self):
        con = self.ledger("b.db")
        import_codex_file(con, self.child)
        import_codex_file(con, self.parent)
        self.assert_split(con)

    def test_child_grows_after_first_import(self):
        con = self.ledger("c.db")
        import_codex_file(con, self.parent)
        with open(self.child) as fh:
            lines = fh.readlines()
        whole = "".join(lines)
        with open(self.child, "w") as fh:
            fh.write("".join(lines[:-2]))
        import_codex_file(con, self.child)
        with open(self.child, "w") as fh:
            fh.write(whole)
        stats = import_codex_file(con, self.child)
        self.assertTrue(stats["incremental"])
        self.assert_split(con)

    def test_full_resync_repairs_a_merged_ledger(self):
        fresh = self.ledger("fresh.db")
        import_codex_file(fresh, self.parent)
        import_codex_file(fresh, self.child)
        expected = self.assert_split(fresh)

        # The 1.0.1 state: the child was imported first and every row it
        # wrote, its own and the copied history, sits under the parent key.
        merged = self.ledger("merged.db")
        import_codex_file(merged, self.child)
        import_codex_file(merged, self.parent)
        child_src = merged.execute("SELECT id FROM sources WHERE path=?",
                                   (self.child,)).fetchone()[0]
        old, new = f"codex:{PARENT}", f"codex:{CHILD}"
        for table in ("events", "responses", "turns", "user_messages"):
            merged.execute(f"UPDATE {table} SET session_key=?"
                           " WHERE session_key=?", (old, new))
        merged.execute("UPDATE responses SET response_id=?||substr("
                       "response_id, ?) WHERE response_id LIKE ?",
                       (old, len(new) + 1, new + ":tc:%"))
        merged.execute("UPDATE codex_fallback_cumulative SET session_key=?"
                       " WHERE session_key=?", (old, new))
        # 1.0.1 also lost child snapshots whose total was below the
        # parent's highest total: the rising-total rule skipped them.
        merged.execute("DELETE FROM responses WHERE response_id=?",
                       (f"{old}:tc:800",))
        ended = merged.execute("SELECT ended_at FROM sessions"
                               " WHERE session_key=?", (new,)).fetchone()[0]
        merged.execute("DELETE FROM sessions WHERE session_key=?", (new,))
        merged.execute("UPDATE sessions SET source_id=?, ended_at=?"
                       " WHERE session_key=?", (child_src, ended, old))
        merged.execute("UPDATE sources SET thread_id=? WHERE id=?",
                       (PARENT, child_src))
        merged.commit()
        self.assertNotEqual(self.snapshot(merged), expected)

        for path in (self.parent, self.child):
            import_codex_file(merged, path, full=True)
        self.assertEqual(self.snapshot(merged), expected)


class SingleMetaForkTest(ForkedRolloutTest):
    """Some forks and guardian reviews copy the parent's history without
    copying its session_meta. 1.0.1 kept them as their own session but
    imported the copy into it, counting the parent's work twice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = os.path.join(self.tmp.name, "rollout-parent.jsonl")
        self.child = os.path.join(self.tmp.name, "rollout-child.jsonl")
        _write(self.parent, [_meta(PARENT, thread_source="user")]
               + _parent_history(), 0)
        self.inherited = _parent_history()
        self.start = 1 + len(self.inherited)
        self.write_child(self.start)

    def write_child(self, start):
        meta = {"thread_source": "guardian_review", "parent_thread_id": PARENT}
        if start is not None:
            meta["subagent_history_start_ordinal"] = start
        _write(self.child, [_meta(CHILD, **meta)] + self.inherited
               + _child_own(), 20)

    def test_full_resync_repairs_a_merged_ledger(self):
        fresh = self.ledger("fresh.db")
        import_codex_file(fresh, self.parent)
        import_codex_file(fresh, self.child)
        expected = self.assert_split(fresh)

        # The 1.0.1 state: the copied history imported into the child,
        # here with the child read first so it also owns the parent's
        # rows that have a global id.
        merged = self.ledger("merged.db")
        self.write_child(None)
        import_codex_file(merged, self.child)
        import_codex_file(merged, self.parent)
        # A first import claims a copied user message back for the parent
        # (privacy rule 3); a parent imported earlier did not, as on cavallo.
        merged.execute("UPDATE user_messages SET session_key=?, source_id="
                       "(SELECT id FROM sources WHERE path=?)",
                       (f"codex:{CHILD}", self.child))
        merged.commit()
        self.assertNotEqual(self.snapshot(merged), expected)

        self.write_child(self.start)
        for path in (self.parent, self.child):
            import_codex_file(merged, path, full=True)
        self.assertEqual(self.snapshot(merged), expected)


if __name__ == "__main__":
    unittest.main()
