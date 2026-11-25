"""Public adapter import and read-only literal membership queries."""

from contextlib import closing
import json
import sqlite3
from pathlib import Path

from agent_observer import native_response
from agent_observer.adapters import claude, codex, grok, opencode
from tests.helpers import LedgerCase, fixture
from tests.test_opencode import build_native


def write_records(path, rows, mode="w"):
    with path.open(mode) as out:
        for row in rows:
            out.write(json.dumps(row) + "\n")


def claude_user(uuid, parent=None, content="identical prompt"):
    return {"sessionId": "s", "type": "user", "uuid": uuid, "parentUuid": parent,
            "message": {"role": "user", "content": content}}


def claude_response(uuid, parent, response_id):
    return {"sessionId": "s", "type": "assistant", "uuid": uuid, "parentUuid": parent,
            "message": {"id": response_id, "role": "assistant", "model": "model",
                        "stop_reason": "end_turn", "usage": {"output_tokens": 1}}}


class NativeResponseTest(LedgerCase):
    def test_claude_out_of_order_tool_chain_cycle_and_competing_streams(self):
        path = Path(self.tmp.name, "s.jsonl")
        write_records(path, [
            claude_user("b"),
            claude_response("r1", "a", "msg-1"),
            claude_response("r2", "tool", "msg-2"),
            claude_user("tool", "r1", [{"type": "tool_result", "tool_use_id": "call"}]),
            claude_response("other", "b", "msg-2"),
            claude_response("cycle-1", "cycle-2", "msg-3"),
            claude_response("cycle-2", "cycle-1", "msg-3"),
        ])
        claude.import_claude_file(self.con, str(path))
        self.assertEqual(native_response.inputs(self.con, "claude:s"), {})
        write_records(path, [claude_user("a")], "a")
        claude.import_claude_file(self.con, str(path))
        self.con.execute("UPDATE responses SET turn_id='claude:display-turn'")
        expected = {"claude:msg-1": {"a"}, "claude:msg-2": {"a", "b"}}
        self.assertEqual(native_response.inputs(self.con, "claude:s"), expected)
        # Simulate the durable state of a pre-ancestry import.
        self.con.execute("DROP TABLE claude_response_ancestry")
        self.con.execute("UPDATE sources SET claude_usage_version=5")
        claude.import_claude_file(self.con, str(path))
        self.assertEqual(native_response.inputs(self.con, "claude:s"), expected)
        write_records(path, [claude_response("r1", "gone", "msg-1")])
        claude.import_claude_file(self.con, str(path))
        self.assertEqual(native_response.inputs(self.con, "claude:s"), {})

    def test_codex_atomic_conflict_and_legacy_snapshot(self):
        path = Path(self.tmp.name, "rollout.jsonl")
        rows = [json.loads(line) for line in Path(fixture("codex-mini.jsonl")).read_text().splitlines()]
        atomic = next(row for row in rows if row["type"] == "token_usage_record")
        write_records(path, rows)
        stats = codex.import_codex_file(self.con, str(path))
        key = stats["session_key"]
        rid = "codex:" + atomic["payload"]["response_id"]
        turn = atomic["payload"]["turn_id"]
        self.assertEqual(native_response.inputs(self.con, key)[rid], {turn})
        self.con.execute("DELETE FROM native_response_inputs")
        self.con.execute("UPDATE sources SET codex_context_version=6")
        self.assertEqual(native_response.inputs(self.con, key)[rid], {turn})
        atomic["payload"]["turn_id"] = "another-turn"
        write_records(path, [atomic], "a")
        codex.import_codex_file(self.con, str(path))
        self.assertEqual(native_response.inputs(self.con, key)[rid], {turn, "another-turn"})
        legacy = self.sync("codex-legacy-a.jsonl")
        self.con.execute("UPDATE responses SET turn_id='codex:guessed' WHERE session_key=?",
                         (legacy["session_key"],))
        self.assertEqual(native_response.inputs(self.con, legacy["session_key"]), {})

    def test_opencode_requires_same_session_user_parent_and_replaces_snapshot(self):
        path = Path(self.tmp.name, "opencode.db")
        build_native(str(path))
        with closing(sqlite3.connect(path)) as source, source:
            source.execute("UPDATE message SET data=json_set(data,'$.parentID','msg_u1')"
                           " WHERE id='msg_a1'")
        opencode.sync(self.con, source=str(path))
        self.assertEqual(native_response.inputs(self.con, "opencode:ses_parent"),
                         {"opencode:msg_a1": {"msg_u1"}})
        with closing(sqlite3.connect(path)) as source, source:
            source.execute("UPDATE message SET data=json_set(data,'$.parentID','msg_a1')"
                           " WHERE id='msg_a1'")
        opencode.sync(self.con, source=str(path), full=True)
        self.assertEqual(native_response.inputs(self.con, "opencode:ses_parent"), {})

    def test_grok_only_explicit_input_index_to_completion_id(self):
        root = next(Path(fixture("grok")).glob("*/*0001"))
        path = Path(self.tmp.name, root.name)
        path.mkdir()
        rows = []
        for line in (root / "updates.jsonl").read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("params"), dict):
                rows.append(row)
        write_records(path / "updates.jsonl", reversed(rows))
        key = "grok:" + root.name
        grok.import_grok_session(self.con, str(path))
        self.assertEqual(native_response.inputs(self.con, key)[key + ":p-aaa"], {"prompt:0"})
        # The completion still exists and still names p-aaa after the literal bridge is removed.
        for row in rows:
            row["params"].get("_meta", {}).pop("promptId", None)
        write_records(path / "updates.jsonl", rows)
        grok.import_grok_session(self.con, str(path), full=True)
        self.assertNotIn(key + ":p-aaa", native_response.inputs(self.con, key))

    def test_grok_completion_and_input_in_separate_copies_and_competing_index(self):
        sid = "sid"
        key = "grok:sid"
        source_dirs = [Path(self.tmp.name, name, sid) for name in ("completed", "input", "copy")]
        for path in source_dirs:
            path.mkdir(parents=True)
        def frame(data, **meta):
            return {"method": "session/update",
                    "params": {"sessionId": sid, "update": data, "_meta": meta}}
        completed = frame({"sessionUpdate": "turn_completed", "prompt_id": "p"})
        chunk = {"sessionUpdate": "user_message_chunk", "_meta": {"promptIndex": 3},
                 "content": {"type": "text", "text": "question"}}
        write_records(source_dirs[0] / "updates.jsonl", [completed])
        write_records(source_dirs[1] / "updates.jsonl", [frame(chunk, promptId="p")])
        for path in source_dirs[:2]:
            grok.import_grok_session(self.con, str(path))
        self.assertEqual(native_response.inputs(self.con, key), {"grok:sid:p": {"prompt:3"}})
        write_records(source_dirs[2] / "updates.jsonl", [frame(chunk, promptId="different")])
        grok.import_grok_session(self.con, str(source_dirs[2]))
        self.assertEqual(native_response.inputs(self.con, key), {})

    def test_opencode_different_copies_preserve_competing_native_parents(self):
        for copy, parent in enumerate(("msg_u1", "msg_u2")):
            path = Path(self.tmp.name, f"copy-{copy}.db")
            build_native(str(path))
            with closing(sqlite3.connect(path)) as source, source:
                source.execute("INSERT INTO message SELECT 'msg_u2',session_id,time_created,"
                               "time_updated,data FROM message WHERE id='msg_u1'")
                source.execute("UPDATE message SET data=json_set(data,'$.parentID',?)"
                               " WHERE id='msg_a1'", (parent,))
            opencode.sync(self.con, source=str(path))
        self.con.execute("PRAGMA query_only=ON")
        self.assertEqual(native_response.inputs(self.con, "opencode:ses_parent"),
                         {"opencode:msg_a1": {"msg_u1", "msg_u2"}})

    def test_query_only_legacy_ledger(self):
        self.con.execute("PRAGMA query_only=ON")
        for session in ("claude:s", "codex:s", "opencode:s", "grok:s"):
            self.assertEqual(native_response.inputs(self.con, session), {})
