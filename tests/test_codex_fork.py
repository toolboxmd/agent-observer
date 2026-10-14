"""Forked Codex rollouts are their own sessions (#69).

A forked child rollout starts with its own session_meta, then a copy of
the parent's session_meta and the parent's history up to
subagent_history_start_ordinal, then its own records. The adapter used to
take the last session_meta, so the child's work landed in the parent's
session and the child had no session row.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_observer import db, interventions, lookup, placement, report
from agent_observer.adapters import t3
from agent_observer.adapters.codex import import_codex_file, sync

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
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": _usage(900),
            "total_token_usage": _usage(1900)}}},
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
            (f"codex:{CHILD}:tc:800", f"codex:{CHILD}", 300, 0),
            (f"codex:{PARENT}:tc:1900", f"codex:{PARENT}", 900, 0)])
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

    def test_repair_keeps_copies_when_the_parent_rollout_is_missing(self):
        # 1.0.1 imported the copied history into the child; the parent's
        # rollout is gone, so that copy is the ledger's only evidence of
        # the parent's events and usage. A re-read must not delete it.
        merged = self.ledger("orphan.db")
        self.write_child(None)
        import_codex_file(merged, self.child)
        before = {
            "events": sorted(r[0] for r in merged.execute(
                "SELECT native_id FROM events")),
            "responses": sorted(r[0] for r in merged.execute(
                "SELECT total_tokens FROM responses")),
            "user_messages": merged.execute(
                "SELECT COUNT(*) FROM user_messages").fetchone()[0],
        }
        self.assertIn("exec-p1", before["events"])

        self.write_child(self.start)
        import_codex_file(merged, self.child, full=True)
        after = {
            "events": sorted(r[0] for r in merged.execute(
                "SELECT native_id FROM events")),
            "responses": sorted(r[0] for r in merged.execute(
                "SELECT total_tokens FROM responses")),
            "user_messages": merged.execute(
                "SELECT COUNT(*) FROM user_messages").fetchone()[0],
        }
        self.assertEqual(after["events"], before["events"])
        self.assertEqual(after["user_messages"], before["user_messages"])
        # Nothing is lost; the child also regains its own snapshots that
        # 1.0.1 skipped below the copied parent total.
        self.assertEqual(sorted(set(after["responses"])
                                - set(before["responses"])), [300, 400])
        self.assertLessEqual(set(before["responses"]),
                             set(after["responses"]))
        # The copies now sit with the parent, not the child.
        owners = dict(merged.execute(
            "SELECT native_id, session_key FROM events"
            " WHERE native_id IN ('exec-p1', 'exec-c1')"))
        self.assertEqual(owners, {"exec-p1": f"codex:{PARENT}",
                                  "exec-c1": f"codex:{CHILD}"})
        self.assertEqual(
            merged.execute("SELECT session_key FROM responses"
                           " WHERE total_tokens=900").fetchone()[0],
            f"codex:{PARENT}")


def _sparse_rows(history_start, extra=()):
    """Metadata ordinals that are not the complete-line index.

    The copied assistant is ordinal 90 at file position 2. The child's own
    rows start at file position 3, while history_start stays 100.
    """
    meta = {"id": CHILD, "thread_source": "subagent",
            "parent_thread_id": PARENT, "cwd": "/w",
            "cli_version": "0.151.0", "originator": "codex-tui"}
    if history_start is not None:
        meta["subagent_history_start_ordinal"] = history_start
    rows = [
        {"type": "session_meta", "ordinal": 0, "payload": meta},
        {"type": "session_meta", "ordinal": 1, "payload": {
            "id": PARENT, "cwd": "/w", "cli_version": "0.151.0",
            "originator": "codex-tui"}},
        {"type": "response_item", "ordinal": 90, "payload": {
            "type": "message", "role": "assistant", "id": "parent-text",
            "content": [{"type": "output_text", "text": "from the parent"}]}},
        {"type": "response_item", "ordinal": 100, "payload": {
            "type": "message", "role": "user", "id": "child-user",
            "content": [{"type": "input_text", "text": "do the child work"}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["user.text"]}}},
        {"type": "response_item", "ordinal": 101, "payload": {
            "type": "message", "role": "assistant", "id": "child-text",
            "content": [{"type": "output_text", "text": "child answer"}]}},
        {"type": "token_usage_record", "ordinal": 102, "payload": {
            "response_id": "child-usage", "thread_id": CHILD,
            "usage": _usage(40)}},
        {"type": "event_msg", "ordinal": 157, "payload": {
            "type": "thread_goal_updated"}},
    ]
    return rows + list(extra)


def _dump(path, rows):
    with open(path, "w") as fh:
        for rec in rows:
            fh.write(json.dumps(rec) + "\n")


class SparseForkCutoffTest(unittest.TestCase):
    """A fork cutoff has to be the file position of the first own record.

    Comparing stored file positions with the metadata history_start moves
    the child's own rows onto the parent on an append, and a full replay
    then stores them twice. A repair of a ledger imported before the
    boundary was known has the same cutoff.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.child = os.path.join(self.tmp.name, "sparse-child.jsonl")
        self.con = db.connect(os.path.join(self.tmp.name, "sparse.db"))
        db.init_db(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def owners(self):
        found = {}
        for native, key in self.con.execute(
                "SELECT native_id, session_key FROM events"):
            found.setdefault(native, set()).add(key)
        for native, key in self.con.execute(
                "SELECT native_id, session_key FROM user_messages"):
            found.setdefault(native, set()).add(key)
        for native, key in self.con.execute(
                "SELECT response_id, session_key FROM responses"):
            found.setdefault(native, set()).add(key)
        return found

    def assert_child_owns_its_rows(self):
        found = self.owners()
        child = f"codex:{CHILD}"
        for native in ("child-text", "thread_goal_updated:157",
                       "codex:child-user", "codex:child-usage"):
            self.assertEqual(found.get(native), {child}, native)
        self.assertNotIn("thread_goal_updated:6", found)
        self.assertNotIn("thread_goal_updated:7", found)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM responses WHERE response_id=?",
            ("codex:child-usage",)).fetchone()[0], 1)
        self.assertEqual(self.con.execute(
            "SELECT ordinal_num FROM responses WHERE response_id=?",
            ("codex:child-usage",)).fetchone()[0], 5)

    def test_append_full_and_repeat_keep_the_child_rows(self):
        _dump(self.child, _sparse_rows(100))
        import_codex_file(self.con, self.child)
        self.assert_child_owns_its_rows()
        _dump(self.child, _sparse_rows(100, extra=[{
            "type": "response_item", "ordinal": 180, "payload": {
                "type": "message", "role": "assistant", "id": "child-tail",
                "content": [{"type": "output_text", "text": "still the child"}]}}]))
        import_codex_file(self.con, self.child)
        self.assert_child_owns_its_rows()
        self.assertEqual(self.owners().get("child-tail"), {f"codex:{CHILD}"})
        import_codex_file(self.con, self.child, full=True)
        self.assert_child_owns_its_rows()
        self.assertEqual(self.owners().get("child-tail"), {f"codex:{CHILD}"})
        import_codex_file(self.con, self.child, full=True)
        self.assert_child_owns_its_rows()

    def test_legacy_repair_maps_the_cutoff_to_file_positions(self):
        # Imported before the fork boundary was present: copied history and
        # the child's own rows share the child key, at file positions.
        _dump(self.child, _sparse_rows(None))
        import_codex_file(self.con, self.child)
        self.assertEqual(self.owners().get("parent-text"), {f"codex:{CHILD}"})
        _dump(self.child, _sparse_rows(100))
        import_codex_file(self.con, self.child, full=True)
        found = self.owners()
        self.assertEqual(found.get("parent-text"), {f"codex:{PARENT}"})
        self.assert_child_owns_its_rows()
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM events WHERE native_id='parent-text'"
        ).fetchone()[0], 1)

    def test_session_key_repair_returns_file_positions_to_the_child(self):
        _dump(self.child, _sparse_rows(100))
        import_codex_file(self.con, self.child)
        parent = f"codex:{PARENT}"
        child = f"codex:{CHILD}"
        for table in ("events", "responses", "user_messages"):
            self.con.execute(f"UPDATE {table} SET session_key=?"
                             " WHERE session_key=?", (parent, child))
        self.con.execute("UPDATE sources SET thread_id=? WHERE path=?",
                         (PARENT, self.child))
        self.con.commit()
        import_codex_file(self.con, self.child, full=True)
        self.assert_child_owns_its_rows()
        self.assertNotIn("parent-text", self.owners())

    def test_privacy8_sparse_fork_moves_copied_history_before_replay(self):
        # The exact base stores metadata ordinals. The file-position
        # boundary is the wrong cutoff until those rows have been replayed.
        base = "a4ba7cea0b15d6afb2926a931477e5dece1b4d61"
        db_path = os.path.join(self.tmp.name, "privacy8.db")
        _dump(self.child, _sparse_rows(None))
        _import_at_revision(base, db_path, self.child)
        self.con.close()
        self.con = db.connect(db_path)
        db.init_db(self.con)
        self.assertEqual(self.con.execute(
            "SELECT privacy_version FROM sources").fetchone()[0], 8)
        self.assertEqual(dict(self.con.execute(
            "SELECT native_id, ordinal_num FROM events")),
            {"parent-text": 90, "child-text": 101,
             "thread_goal_updated:157": 157})
        _dump(self.child, _sparse_rows(100))
        import_codex_file(self.con, self.child)
        self.assertEqual(self.owners().get("parent-text"), {f"codex:{PARENT}"})
        self.assert_child_owns_its_rows()
        # The metadata cutoff moved the copied assistant before this replay
        # rewrote its locator. The stored position is now the file position,
        # so a later cutoff cannot read metadata ordinal 90 as a file position.
        self.assertEqual(self.con.execute(
            "SELECT ordinal_num FROM events WHERE native_id='parent-text'"
        ).fetchone()[0], 2)
        _dump(self.child, _sparse_rows(100, extra=[{
            "type": "response_item", "ordinal": 180, "payload": {
                "type": "message", "role": "assistant", "id": "child-tail",
                "content": [{"type": "output_text", "text": "appended"}]}}]))
        import_codex_file(self.con, self.child)
        self.assertEqual(self.owners().get("parent-text"), {f"codex:{PARENT}"})
        self.assertEqual(self.owners().get("child-tail"), {f"codex:{CHILD}"})
        self.assert_child_owns_its_rows()
        import_codex_file(self.con, self.child, full=True)
        import_codex_file(self.con, self.child, full=True)
        self.assertEqual(self.owners().get("parent-text"), {f"codex:{PARENT}"})
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM events WHERE native_id='parent-text'"
        ).fetchone()[0], 1)
        self.assertEqual(self.con.execute(
            "SELECT privacy_version FROM sources").fetchone()[0], 9)
        self.assert_child_owns_its_rows()


class RetainedHistoryUpgradeTest(unittest.TestCase):
    """A privacy-8 fork upgrade keeps copied history in the parent's namespace.

    The child file is the only copy of the parent input. Its metadata
    ordinal is not a file position, and a later full replay must not move
    that input back onto the child.
    """

    BASE = "a4ba7cea0b15d6afb2926a931477e5dece1b4d61"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "fork-upgrade.jsonl")
        self.db_path = os.path.join(self.tmp.name, "fork-upgrade.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self, history_start, user_id):
        child = {"id": "child", "thread_source": "subagent",
                 "parent_thread_id": "parent", "originator": "codex-tui"}
        if history_start is not None:
            child["subagent_history_start_ordinal"] = history_start
        user = {"type": "message", "role": "user",
                "content": [{"type": "input_text",
                             "text": "retained parent input"}],
                "internal_chat_message_metadata_passthrough": {
                    "content_item_kinds": ["user.text"]}}
        if user_id:
            user["id"] = user_id
        alpha = os.path.join(self.tmp.name, "removed-alpha", "x.py")
        beta = os.path.join(self.tmp.name, "removed-beta", "x.py")
        rows = [
            {"type": "session_meta", "payload": child},
            {"type": "session_meta", "payload": {
                "id": "parent", "thread_source": "user",
                "originator": "codex-tui"}},
            {"type": "response_item", "payload": user},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "id": "read-p1",
                "command": ["cat " + alpha], "status": "completed",
                "exit_code": 0, "parsed_cmd": [
                    {"type": "read", "path": alpha, "cmd": "cat"}]}}},
            {"type": "token_usage_record", "payload": {
                "response_id": "parent-r1", "thread_id": "parent",
                "usage": _usage(12)}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "id": "read-p2",
                "command": ["cat " + beta], "status": "completed",
                "exit_code": 0, "parsed_cmd": [
                    {"type": "read", "path": beta, "cmd": "cat"}]}}},
            {"type": "token_usage_record", "payload": {
                "response_id": "parent-r2", "thread_id": "parent",
                "usage": _usage(12)}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "id": "parent-answer",
                "content": [{"type": "output_text", "text": "parent"}]}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": _usage(12),
                "total_token_usage": _usage(48)}}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "id": "child-answer",
                "content": [{"type": "output_text", "text": "child"}]}},
            {"type": "token_usage_record", "payload": {
                "response_id": "child-r", "thread_id": "child",
                "usage": _usage(12)}},
        ]
        for i, row in enumerate(rows):
            row["ordinal"] = i * 10
        return rows

    def _bind(self, con):
        t3._ensure_tables(con)
        for i, repo in enumerate(("alpha", "beta"), 1):
            task = f"o/{repo}#{i}"
            root = os.path.join(self.tmp.name, f"removed-{repo}")
            con.execute(
                "INSERT OR IGNORE INTO tasks(task_id, title, created_at)"
                " VALUES(?,?,0)", (task, task))
            con.execute(
                "INSERT OR IGNORE INTO session_assignments"
                "(session_key, task_id, evidence, created_at)"
                " VALUES('codex:parent', ?, 'review', 0)", (task,))
            con.execute(
                "INSERT OR IGNORE INTO t3_links(thread_id, kind, host,"
                " repository, number, url, head_branch)"
                " VALUES('t','pr','github.com',?,?,?,?)",
                (f"o/{repo}", i, f"https://github.com/o/{repo}/pull/{i}",
                 "fix/" + repo))
            con.execute(
                "INSERT OR IGNORE INTO checkouts(root, repository, branch,"
                " seen_at) VALUES(?,?,?,0)",
                (root, f"o/{repo}", "fix/" + repo))

    def test_upgrade_keeps_parent_input_and_read_attribution(self):
        for user_id in (None, "parent-input"):
            with self.subTest(user_id=user_id):
                if os.path.exists(self.db_path):
                    os.remove(self.db_path)
                _dump(self.path, self._rows(None, user_id))
                _import_at_revision(self.BASE, self.db_path, self.path)
                _dump(self.path, self._rows(90, user_id))
                con = db.connect(self.db_path)
                try:
                    db.init_db(con)
                    self._bind(con)
                    sub = ("sub:codex:parent-input" if user_id
                           else "sub:codex:ordinal:20")
                    for full in (False, True, False):
                        import_codex_file(con, self.path, full=full)
                        user = con.execute(
                            "SELECT native_id, session_key, ordinal_num"
                            " FROM user_messages").fetchone()
                        self.assertEqual(user["session_key"], "codex:parent")
                        self.assertNotEqual(user["ordinal_num"], 20)
                        shown = interventions.show(
                            con, [sub], turns_before=0, turns_after=0)[0]
                        self.assertEqual(shown["status"], "ok", shown)
                        self.assertIn("retained parent input", shown["text"])
                        placed = placement.place_session(con, "codex:parent")
                        self.assertEqual(placed["codex:parent-r1"], "o/alpha#1")
                        self.assertEqual(placed["codex:parent-r2"], "o/beta#2")
                        for i, repo in enumerate(("alpha", "beta"), 1):
                            measured = report.task_report(
                                con, f"o/{repo}#{i}", schedule={"models": {}})[
                                    "measured"]
                            self.assertIn(f"codex:parent-r{i}",
                                          measured["attributed_responses"])
                        reads = con.execute(
                            "SELECT e.family FROM codex_occurrences o"
                            " JOIN events e ON e.id = o.row_key"
                            " WHERE o.session_key='codex:parent'"
                            " AND o.row_kind='event' AND e.family='read'"
                        ).fetchall()
                        self.assertEqual(len(reads), 2)
                        fallback = con.execute(
                            "SELECT session_key, ordinal_num FROM responses"
                            " WHERE response_id LIKE '%:tc:48'").fetchone()
                        self.assertIsNotNone(fallback)
                        self.assertEqual(fallback["session_key"], "codex:parent")
                        self.assertNotEqual(fallback["ordinal_num"], 80)
                        noted = con.execute(
                            "SELECT 1 FROM codex_occurrences"
                            " WHERE row_kind='response' AND row_key LIKE '%:tc:48'"
                        ).fetchone()
                        self.assertIsNotNone(noted)
                        authoritative = con.execute(
                            "SELECT COUNT(*) n, SUM(total_tokens) tokens"
                            " FROM responses WHERE response_id IN"
                            " ('codex:parent-r1','codex:parent-r2','codex:child-r')"
                        ).fetchone()
                        self.assertEqual(authoritative["n"], 3)
                        self.assertEqual(authoritative["tokens"], 36)
                        self.assertEqual(con.execute(
                            "SELECT COUNT(*) FROM responses"
                            " WHERE response_id LIKE '%:tc:48'").fetchone()[0], 1)
                        self.assertEqual(con.execute(
                            "PRAGMA foreign_key_check").fetchall(), [])
                finally:
                    con.close()


class SharedOrdinalUpgradeTest(unittest.TestCase):
    """Distinct explicit ids may share one raw metadata ordinal.

    A base ledger stores that ordinal as the locator. The upgrade has to
    give each retained user and event the file position a fresh import of
    the same lines stores. A later privacy refresh already holds file
    positions, so it must not read them as metadata ordinals.
    """

    BASE = "a4ba7cea0b15d6afb2926a931477e5dece1b4d61"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "shared-ordinal.jsonl")
        self.db_path = os.path.join(self.tmp.name, "shared-ordinal.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self, history_start):
        child = {"id": "child", "thread_source": "subagent",
                 "parent_thread_id": "parent", "originator": "codex-tui"}
        if history_start is not None:
            child["subagent_history_start_ordinal"] = history_start

        def user(native_id, text):
            return {"type": "response_item", "ordinal": 4, "payload": {
                "type": "message", "role": "user", "id": native_id,
                "content": [{"type": "input_text", "text": text}],
                "internal_chat_message_metadata_passthrough": {
                    "content_item_kinds": ["user.text"]}}}

        def event(native_id, text):
            return {"type": "response_item", "ordinal": 4, "payload": {
                "type": "message", "role": "assistant", "id": native_id,
                "content": [{"type": "output_text", "text": text}]}}

        return [
            {"type": "session_meta", "payload": child},
            {"type": "session_meta", "payload": {
                "id": "parent", "thread_source": "user",
                "originator": "codex-tui"}},
            user("user-a", "alpha input"),
            event("event-a", "alpha answer"),
            user("user-b", "beta input"),
            event("event-b", "beta answer"),
            {"type": "response_item", "ordinal": 100, "payload": {
                "type": "message", "role": "assistant", "id": "child-own",
                "content": [{"type": "output_text", "text": "child own"}]}},
        ]

    def _positions(self, con):
        found = {}
        for row in con.execute(
                "SELECT native_id, ordinal_num FROM user_messages"):
            found[row["native_id"]] = row["ordinal_num"]
        for row in con.execute(
                "SELECT native_id, ordinal_num FROM events"):
            found[row["native_id"]] = row["ordinal_num"]
        for row in con.execute(
                "SELECT row_key, record_position FROM codex_occurrences"
                " WHERE row_kind='user'"):
            found["occ:" + row["row_key"]] = row["record_position"]
        for row in con.execute(
                "SELECT e.native_id AS native_id, o.record_position"
                " FROM codex_occurrences o JOIN events e"
                " ON e.id = o.row_key WHERE o.row_kind='event'"):
            found["occ:" + row["native_id"]] = row["record_position"]
        return found

    def test_shared_raw_ordinal_keeps_each_explicit_identity(self):
        _dump(self.path, self._rows(None))
        fresh_path = os.path.join(self.tmp.name, "fresh.db")
        fresh = db.connect(fresh_path)
        db.init_db(fresh)
        import_codex_file(fresh, self.path)
        expected = self._positions(fresh)
        fresh.close()
        self.assertEqual(expected["codex:user-a"], 2)
        self.assertEqual(expected["codex:user-b"], 4)
        self.assertEqual(expected["event-a"], 3)
        self.assertEqual(expected["event-b"], 5)
        self.assertNotEqual(expected["codex:user-a"], expected["codex:user-b"])

        _import_at_revision(self.BASE, self.db_path, self.path)
        _dump(self.path, self._rows(50))
        con = db.connect(self.db_path)
        try:
            db.init_db(con)
            index = lookup._index_codex(self.path)
            wanted = ("codex:user-a", "codex:user-b", "event-a", "event-b")
            for full in (False, True, False):
                import_codex_file(con, self.path, full=full)
                found = self._positions(con)
                for key in wanted:
                    self.assertEqual(found[key], expected[key], key)
                    self.assertEqual(
                        found["occ:" + key], expected[key], "occ:" + key)
                    bare = key.removeprefix("codex:")
                    self.assertEqual(
                        index.records[found[key]]["payload"]["id"], bare, key)
                self.assertEqual(found["codex:user-a"], 2)
                self.assertNotEqual(found["codex:user-b"], 2)
                for native_id, text in (
                        ("codex:user-a", "alpha input"),
                        ("codex:user-b", "beta input")):
                    shown = interventions.show(
                        con, ["sub:" + native_id],
                        turns_before=0, turns_after=0)[0]
                    self.assertEqual(shown["status"], "ok", shown)
                    self.assertEqual(shown["text"], text)
                self.assertEqual(con.execute(
                    "SELECT session_key FROM events WHERE native_id='child-own'"
                ).fetchone()[0], "codex:child")
            con.execute("UPDATE sources SET privacy_version=10")
            con.commit()
            import_codex_file(con, self.path, full=True)
            found = self._positions(con)
            for key in wanted:
                self.assertEqual(found[key], expected[key], key)
                self.assertEqual(
                    found["occ:" + key], expected[key], "occ:" + key)
                bare = key.removeprefix("codex:")
                self.assertEqual(
                    index.records[found[key]]["payload"]["id"], bare, key)
            self.assertEqual(con.execute(
                "SELECT privacy_version FROM sources").fetchone()[0], 9)
        finally:
            con.close()


class OccurrenceReplayFailureTest(unittest.TestCase):
    """A denied occurrence write must not commit by the next source."""

    def test_failed_replay_keeps_prior_occurrences(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            original = os.path.join(tmp.name, "replay-a.jsonl")
            healthy = os.path.join(tmp.name, "replay-b.jsonl")
            continuation = os.path.join(tmp.name, "replay-continuation.jsonl")
            meta = lambda name: {"type": "session_meta", "payload": {
                "id": name, "thread_source": "user"}}
            call = lambda name: {"type": "response_item", "payload": {
                "type": "function_call", "call_id": name,
                "name": "exec_command", "arguments": '{"cmd":"echo x"}'}}
            _dump(original, [meta("failure"), call("a"), call("b")])
            _dump(healthy, [meta("healthy")])
            _dump(continuation, [meta("failure")]
                  + [{"type": "world_state", "payload": {}}] * 8
                  + [call("a")])
            con = db.connect(os.path.join(tmp.name, "failure.db"))
            db.init_db(con)
            sync(con, source=continuation)
            sync(con, source=original)
            before = report.timeline(
                con, session_key="codex:failure", family="tool_call", limit=1)
            self.assertEqual(
                [e["native_id"] for e in before["events"]], ["a"])

            def deny_occurrence_insert(action, table, column, database, source):
                if (action == sqlite3.SQLITE_INSERT
                        and table == "codex_occurrences"):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            con.set_authorizer(deny_occurrence_insert)
            failed = sync(con, source=original, full=True)
            con.set_authorizer(None)
            self.assertFalse(con.in_transaction)
            self.assertTrue(failed["failed"])
            sync(con, source=healthy)
            retry = sync(con, source=original)
            after = report.timeline(
                con, session_key="codex:failure", family="tool_call", limit=1)
            self.assertEqual(
                [e["native_id"] for e in after["events"]], ["a"])
            self.assertNotIn("chronology", after)
            self.assertGreater(con.execute(
                "SELECT COUNT(*) FROM codex_occurrences"
                " WHERE session_key='codex:failure'").fetchone()[0], 0)
            self.assertFalse(retry["failed"])
            con.close()
        finally:
            tmp.cleanup()


def _import_at_revision(rev: str, db_path: str, rollout: str) -> None:
    """Import one rollout with the adapter at an exact historical commit."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = r"""
import importlib.abc, importlib.util, subprocess, sys
rev, root, db_path, rollout = sys.argv[1:5]
class GitSource(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "agent_observer" and not fullname.startswith("agent_observer."):
            return None
        rel = fullname.replace(".", "/")
        package = fullname in ("agent_observer", "agent_observer.adapters")
        rel += "/__init__.py" if package else ".py"
        return importlib.util.spec_from_loader(fullname, self, origin=rel, is_package=package)
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        rel = module.__spec__.origin
        source = subprocess.check_output(["git", "show", f"{rev}:{rel}"], cwd=root)
        module.__file__ = str(__import__("pathlib").Path(root, rel))
        exec(compile(source, f"git:{rev}:{rel}", "exec"), module.__dict__)
sys.meta_path.insert(0, GitSource())
from agent_observer import db
from agent_observer.adapters.codex import import_codex_file
con = db.connect(db_path)
db.init_db(con)
stats = import_codex_file(con, rollout)
if stats.get("failed"):
    raise SystemExit(stats["failed"])
con.commit()
con.close()
"""
    subprocess.run([sys.executable, "-c", script, rev, root, db_path, rollout],
                   check=True)


if __name__ == "__main__":
    unittest.main()
