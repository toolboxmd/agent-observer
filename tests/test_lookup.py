"""event show: full content behind ledger events, read from the native log."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_observer import db, lookup
from agent_observer.adapters import claude, codex, grok, opencode
from tests.test_opencode import SECRET_READ, T0, _tool, build_native

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
HEREDOC = ("cat > /tmp/pr.md <<'EOF'\n**What:** adds the lookup\nEOF\n"
           "gh pr create --body-file /tmp/pr.md")
FINAL = "Done. " + "The PR is open and the proof is attached. " * 20


def _write(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def _claude_line(kind, uuid, content, msg_id=None, **extra):
    record = {"sessionId": "sess-a", "cwd": "/repo", "version": "2.1.280",
              "type": kind, "uuid": uuid,
              "timestamp": "2026-10-01T10:00:00Z",
              "message": {"role": kind, "content": content}}
    if msg_id:
        record["message"].update({"id": msg_id, "model": "claude-opus-5-5"})
    record.update(extra)
    return record


def _claude_tree(root):
    _write(os.path.join(root, "proj", "sess-a.jsonl"), [
        _claude_line("user", "u1", "open the PR", promptId="p1"),
        _claude_line("assistant", "a1",
                     [{"type": "text", "text": "I will open the PR now."}],
                     msg_id="m1"),
        _claude_line("assistant", "a2",
                     [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                       "input": {"command": HEREDOC,
                                 "description": "open PR"}}],
                     msg_id="m1"),
        _claude_line("user", "u2",
                     [{"type": "tool_result", "tool_use_id": "toolu_1",
                       "content": "https://github.com/o/r/pull/9\n"
                                  f"GH_TOKEN={TOKEN}"}]),
        _claude_line("assistant", "a3", [{"type": "text", "text": FINAL}],
                     msg_id="m2"),
    ])
    _write(os.path.join(root, "proj", "sess-a", "subagents",
                        "agent-s1.jsonl"), [
        _claude_line("user", "su1", "Explore the notes", isSidechain=True,
                     agentId="s1"),
        _claude_line("assistant", "sa1",
                     [{"type": "tool_use", "id": "toolu_s1", "name": "Read",
                       "input": {"file_path": "/repo/notes.md"}}],
                     msg_id="sm1", isSidechain=True, agentId="s1"),
        _claude_line("user", "su2",
                     [{"type": "tool_result", "tool_use_id": "toolu_s1",
                       "content": [{"type": "text", "text": "line one"}]}],
                     isSidechain=True, agentId="s1"),
    ])


def _codex_rollout(path):
    def line(ordinal, rtype, payload):
        return {"ordinal": ordinal, "type": rtype, "payload": payload,
                "timestamp": f"2026-10-01T10:00:{ordinal:02d}.000Z"}
    _write(path, [
        line(0, "session_meta", {"session_id": "cx-1", "id": "cx-1",
                                 "cli_version": "0.155.0", "cwd": "/repo",
                                 "thread_source": "user"}),
        line(1, "event_msg", {"type": "task_started", "turn_id": "t1"}),
        line(2, "turn_context", {"turn_id": "t1", "model": "gpt-6",
                                 "effort": "high", "cwd": "/repo"}),
        line(3, "response_item", {"type": "message", "role": "user",
                                  "id": "msg-u1", "content": [
                                      {"type": "input_text",
                                       "text": "write the body"}]}),
        line(4, "response_item", {"type": "message", "role": "assistant",
                                  "id": "msg-a1", "content": [
                                      {"type": "output_text",
                                       "text": "Writing the body file."}]}),
        line(5, "response_item", {
            "type": "function_call", "call_id": "call-1",
            "name": "exec_command", "id": "fc-1",
            "arguments": json.dumps({"cmd": "cat > /tmp/body.md <<'EOF'\n"
                                            "secret body line\nEOF",
                                     "api_key": "hunter2value",
                                     "env": {"GH_TOKEN": "plainvalue1"}})}),
        line(6, "response_item", {"type": "function_call_output",
                                  "call_id": "call-1", "output": "written"}),
        line(7, "event_msg", {"type": "item_completed", "turn_id": "t1",
                              "item": {"type": "CommandExecution",
                                       "id": "exec-1",
                                       "command": ["/bin/zsh", "-lc",
                                                   "echo hi"],
                                       "status": "completed",
                                       "stdout": "hi\n"}}),
        line(20, "event_msg", {"type": "task_complete", "turn_id": "t1"}),
    ])


def _grok_session(root):
    sid = "01lookup0-aaaa-4b5c-8d6e-000000000001"
    session = os.path.join(root, "%2Frepo", sid)
    os.makedirs(session)
    with open(os.path.join(session, "summary.json"), "w") as fh:
        json.dump({"info": {"id": sid, "cwd": "/repo"},
                   "created_at": "2026-10-01T10:00:00Z",
                   "last_active_at": "2026-10-01T10:05:00Z",
                   "current_model_id": "grok-4.7-build",
                   "git_root_dir": "/repo", "head_branch": "main"}, fh)

    def update(ts, body, method="session/update"):
        return {"timestamp": ts, "method": method,
                "params": {"sessionId": sid, "update": body,
                           "_meta": {"eventId": f"{sid}-{ts}",
                                     "promptId": "p1"}}}
    tool = {"x.ai/tool": {"version": 1, "name": "run_terminal_command",
                          "kind": "execute", "namespace": "grok_build"}}
    _write(os.path.join(session, "updates.jsonl"), [
        update(1, {"sessionUpdate": "user_message_chunk",
                   "content": {"type": "text", "text": "open the PR"}}),
        update(2, {"sessionUpdate": "agent_message_chunk",
                   "content": {"type": "text", "text": "Opening "}}),
        update(3, {"sessionUpdate": "agent_message_chunk",
                   "content": {"type": "text", "text": "the PR."}}),
        update(4, {"sessionUpdate": "tool_call", "toolCallId": "call-g1",
                   "title": "Run", "rawInput": {"command": HEREDOC},
                   "_meta": tool}),
        update(5, {"sessionUpdate": "tool_call_update",
                   "toolCallId": "call-g1", "status": "completed",
                   "rawOutput": {"stdout": "https://github.com/o/r/pull/3"},
                   "_meta": tool}),
        update(6, {"sessionUpdate": "turn_completed", "prompt_id": "p1",
                   "stop_reason": "end_turn"}, method="_x.ai/session/update"),
    ])
    _write(os.path.join(session, "events.jsonl"), [
        {"ts": "2026-10-01T10:00:01Z", "type": "turn_started",
         "session_id": sid, "turn_number": 0, "model_id": "grok-4.7-build",
         "session_relationship": "primary"},
        {"ts": "2026-10-01T10:00:06Z", "type": "turn_ended",
         "outcome": "completed"},
    ])


class LookupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.ledger = os.path.join(self.root, "observer.db")
        self.con = db.connect(self.ledger)
        db.init_db(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def event_id(self, family, native_id):
        row = self.con.execute(
            "SELECT id FROM events WHERE family=? AND native_id=?",
            (family, native_id)).fetchone()
        self.assertIsNotNone(row, f"no {family} event {native_id}")
        return row["id"]

    def show(self, *ids):
        return lookup.show(self.con, list(ids))


class ClaudeLookupTest(LookupCase):
    def setUp(self):
        super().setUp()
        self.claude_root = os.path.join(self.root, "claude")
        _claude_tree(self.claude_root)
        claude.sync(self.con, root=self.claude_root)
        self.con.commit()

    def test_tool_call_returns_full_input_result_and_narration(self):
        call = self.event_id("tool_call", "toolu_1")
        stored = self.con.execute("SELECT target FROM events WHERE id=?",
                                  (call,)).fetchone()["target"]
        self.assertNotIn("adds the lookup", stored or "")
        [item] = self.show(call)
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["harness"], "claude")
        self.assertEqual(item["input"]["command"], HEREDOC)
        self.assertIn("https://github.com/o/r/pull/9", item["result"])
        self.assertEqual(item["assistant_text"], "I will open the PR now.")

    def test_result_event_resolves_the_same_call(self):
        [item] = self.show(self.event_id("tool_result", "toolu_1"))
        self.assertEqual(item["input"]["command"], HEREDOC)
        self.assertIn("pull/9", item["result"])

    def test_secrets_are_redacted(self):
        [item] = self.show(self.event_id("tool_call", "toolu_1"))
        self.assertNotIn(TOKEN, json.dumps(item))
        self.assertIn("GH_TOKEN=[redacted]", item["result"])

    def test_assistant_message_returns_full_text(self):
        [item] = self.show(self.event_id("assistant_message", "a3:0"))
        self.assertEqual(item["text"], FINAL)
        self.assertGreater(len(item["text"]), 400)

    def test_subagent_transcript(self):
        [item] = self.show(self.event_id("tool_call", "toolu_s1"))
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["input"], {"file_path": "/repo/notes.md"})
        self.assertEqual(item["result"], "line one")
        self.assertTrue(item["source"].endswith("agent-s1.jsonl"))

    def test_missing_source_is_reported_without_content(self):
        call = self.event_id("tool_call", "toolu_1")
        os.remove(os.path.join(self.claude_root, "proj", "sess-a.jsonl"))
        [item] = self.show(call)
        self.assertEqual(item["status"], "source_missing")
        self.assertNotIn("input", item)
        self.assertNotIn("result", item)

    def test_shrunk_source_is_reported_changed(self):
        call = self.event_id("tool_call", "toolu_1")
        path = os.path.join(self.claude_root, "proj", "sess-a.jsonl")
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines[:3])
        [item] = self.show(call)
        self.assertEqual(item["status"], "source_changed")
        self.assertNotIn("input", item)

    def test_rewritten_prefix_is_reported_changed(self):
        call = self.event_id("tool_call", "toolu_1")
        path = os.path.join(self.claude_root, "proj", "sess-a.jsonl")
        with open(path, "r+b") as fh:
            data = fh.read()
            at = data.rindex(b"attached")
            fh.seek(at)
            fh.write(b"ATTACHED")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_claude_line("user", "u9", "thanks")) + "\n")
        [item] = self.show(call)
        self.assertEqual(item["status"], "source_changed")

    def test_grown_source_still_resolves(self):
        call = self.event_id("tool_call", "toolu_1")
        path = os.path.join(self.claude_root, "proj", "sess-a.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_claude_line("user", "u9", "thanks")) + "\n")
        [item] = self.show(call)
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["input"]["command"], HEREDOC)

    def test_unknown_event_id(self):
        [item] = self.show(987654)
        self.assertEqual(item, {"id": 987654, "status": "no_such_event"})

    def test_batch_keeps_request_order(self):
        a = self.event_id("tool_call", "toolu_s1")
        b = self.event_id("tool_call", "toolu_1")
        self.assertEqual([i["id"] for i in self.show(a, b, a)], [a, b, a])


class CodexLookupTest(LookupCase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.root, "rollout-cx-1.jsonl")
        _codex_rollout(self.path)
        codex.import_codex_file(self.con, self.path)
        self.con.commit()

    def test_function_call_with_heredoc_body(self):
        [item] = self.show(self.event_id("tool_call", "call-1"))
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["input"]["cmd"],
                         "cat > /tmp/body.md <<'EOF'\nsecret body line\nEOF")
        self.assertEqual(item["result"], "written")
        self.assertEqual(item["assistant_text"], "Writing the body file.")

    def test_structured_secrets_are_redacted(self):
        [item] = self.show(self.event_id("tool_call", "call-1"))
        self.assertEqual(item["input"]["api_key"], "[redacted]")
        self.assertEqual(item["input"]["env"]["GH_TOKEN"], "[redacted]")
        self.assertNotIn("hunter2value", json.dumps(item))
        self.assertNotIn("plainvalue1", json.dumps(item))

    def test_lifecycle_record_by_native_ordinal(self):
        row = self.con.execute(
            "SELECT id, ordinal_num FROM events WHERE family='lifecycle'"
            " AND native_id LIKE 'task_complete:%'").fetchone()
        self.assertEqual(row["ordinal_num"], 20)
        [item] = self.show(row["id"])
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["record"]["payload"]["type"], "task_complete")

    def test_id_absent_from_intact_source_is_not_found(self):
        call = self.event_id("tool_call", "call-1")
        self.con.execute("UPDATE events SET native_id='call-gone',"
                         " ordinal_num=99 WHERE id=?", (call,))
        [item] = self.show(call)
        self.assertEqual(item["status"], "not_found")
        self.assertNotIn("input", item)

    def test_command_execution_item(self):
        [item] = self.show(self.event_id("tool_result", "exec-1"))
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["input"]["command"],
                         ["/bin/zsh", "-lc", "echo hi"])
        self.assertEqual(item["result"], "hi\n")

    def test_assistant_message(self):
        [item] = self.show(self.event_id("assistant_message", "msg-a1"))
        self.assertEqual(item["text"], "Writing the body file.")

    def test_replaced_source_is_reported_changed(self):
        call = self.event_id("tool_call", "call-1")
        # Write the copy while the original exists so it gets a new inode
        # (Linux reuses a freed inode for a file created after removal).
        with open(self.path, encoding="utf-8") as fh:
            body = fh.read()
        copy = self.path + ".new"
        with open(copy, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(copy, self.path)
        [item] = self.show(call)
        self.assertEqual(item["status"], "source_changed")


class GrokLookupTest(LookupCase):
    def setUp(self):
        super().setUp()
        self.grok_root = os.path.join(self.root, "grok")
        _grok_session(self.grok_root)
        grok.sync(self.con, root=self.grok_root)
        self.con.commit()

    def test_tool_call_input_output_and_streamed_narration(self):
        [item] = self.show(self.event_id("tool_call", "call-g1"))
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["input"], {"command": HEREDOC})
        self.assertEqual(item["result"],
                         {"stdout": "https://github.com/o/r/pull/3"})
        self.assertEqual(item["assistant_text"], "Opening the PR.")

    def test_completion_from_events_log_checks_updates_log(self):
        session = os.path.dirname(self.con.execute(
            "SELECT path FROM sources WHERE path LIKE '%updates.jsonl'"
        ).fetchone()["path"])
        row = self.con.execute(
            "SELECT id FROM events WHERE family='lifecycle'"
            " AND native_id LIKE 'turn_started:%'").fetchone()
        with open(os.path.join(session, "updates.jsonl"), "w") as fh:
            fh.write("")
        [item] = self.show(row["id"])
        self.assertEqual(item["status"], "source_changed")

    def test_lifecycle_event_returns_its_native_record(self):
        row = self.con.execute(
            "SELECT id FROM events WHERE family='lifecycle'"
            " AND native_id LIKE 'turn_started:%'").fetchone()
        [item] = self.show(row["id"])
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["record"]["type"], "turn_started")


class OpenCodeLookupTest(LookupCase):
    def setUp(self):
        super().setUp()
        self.native = os.path.join(self.root, "opencode.db")
        build_native(self.native)
        native = sqlite3.connect(self.native)
        native.execute(
            "INSERT INTO part VALUES(?,?,?,?,?,?)",
            ("p_text0", "msg_a1", "ses_parent", T0 + 20, T0 + 20,
             json.dumps({"type": "text", "text": "Reading the notes."})))
        native.execute(
            "INSERT INTO part VALUES(?,?,?,?,?,?)",
            ("p_ctool", "msg_ca1", "ses_child", T0 + 71, T0 + 71,
             _tool("bash", "call_child1", "completed", {"command": "ls"},
                   "a.txt", T0 + 71, T0 + 72)))
        native.execute(
            "INSERT INTO message VALUES(?,?,?,?,?)",
            ("msg_err", "ses_parent", T0 + 90, T0 + 90,
             json.dumps({"role": "assistant", "time": {"created": T0 + 90},
                         "error": {"name": "APIError",
                                   "data": {"statusCode": 500}}})))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.native)
        self.con.commit()

    def test_tool_part(self):
        [item] = self.show(self.event_id("tool_call", "call_read1"))
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["input"], {"filePath": "/repo/notes.md"})
        self.assertEqual(item["result"], SECRET_READ)
        self.assertEqual(item["assistant_text"], "Reading the notes.")

    def test_child_session(self):
        [item] = self.show(self.event_id("tool_call", "call_child1"))
        self.assertEqual(item["status"], "ok")
        self.assertTrue(item["source"].endswith("#ses_child"))
        self.assertEqual(item["input"], {"command": "ls"})
        self.assertEqual(item["result"], "a.txt")

    def test_message_error_lifecycle_record(self):
        [item] = self.show(self.event_id("lifecycle", "msg_err"))
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["record"]["error"]["data"]["statusCode"], 500)

    def test_missing_database(self):
        call = self.event_id("tool_call", "call_read1")
        os.remove(self.native)
        [item] = self.show(call)
        self.assertEqual(item["status"], "source_missing")


def _digest(path):
    """Database plus write-ahead log content. A missing log equals an empty
    one; SQLite readers create an empty log and a shared-memory index."""
    h = hashlib.sha256()
    for suffix in ("", "-wal"):
        if os.path.exists(path + suffix):
            with open(path + suffix, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


class EventShowCliTest(LookupCase):
    def setUp(self):
        super().setUp()
        claude_root = os.path.join(self.root, "claude")
        _claude_tree(claude_root)
        claude.sync(self.con, root=claude_root)
        self.con.commit()
        self.call = self.event_id("tool_call", "toolu_1")
        self.sub = self.event_id("tool_call", "toolu_s1")
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.con.close()
        self.con = db.connect(self.ledger)

    def run_cli(self, *args, stdin=None):
        env = dict(os.environ, AGENT_OBSERVER_DB=self.ledger)
        return subprocess.run(
            [sys.executable, "-m", "agent_observer", *args], cwd=REPO,
            capture_output=True, text=True, env=env, input=stdin)

    def test_json_batch_from_stdin_leaves_ledger_unchanged(self):
        before = _digest(self.ledger)
        r = self.run_cli("event", "show", "--ids-from", "-", "--json",
                         stdin=f"{self.call}\n{self.sub}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertEqual([i["id"] for i in payload], [self.call, self.sub])
        self.assertEqual(payload[0]["input"]["command"], HEREDOC)
        self.assertEqual(_digest(self.ledger), before)

    def test_text_output(self):
        r = self.run_cli("event", "show", str(self.call))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"event {self.call}", r.stdout)
        self.assertIn("**What:** adds the lookup", r.stdout)
        self.assertIn("I will open the PR now.", r.stdout)

    def test_missing_ids_file_is_a_usage_error(self):
        r = self.run_cli("event", "show", "--ids-from",
                         os.path.join(self.root, "absent.txt"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot read event ids", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_unresolved_event_exits_nonzero(self):
        r = self.run_cli("event", "show", "987654", "--json")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(json.loads(r.stdout)[0]["status"], "no_such_event")


if __name__ == "__main__":
    unittest.main()
