"""OpenCode adapter: usage arithmetic, unfinished re-sync, parent/child,
tool joins, reads, skills, errors, unchanged skip, idempotent re-sync."""

import json
import os
import sqlite3
import tempfile
import unittest

from agent_observer import db
from agent_observer.adapters import opencode

T0 = 1788000000000

SECRET_READ = "SECRET_READ_OUTPUT_zzz"
SECRET_EDIT = "SECRET_EDIT_OUTPUT_zzz"
SECRET_SKILL = "SECRET_SKILL_OUTPUT_zzz"
SECRET_REASON = "SECRET_REASONING_zzz"
SECRET_DATAURL = "SECRET_DATA_URL_zzz"
SECRET_ERRMSG = "SECRET_ERROR_MESSAGE_zzz"
SECRET_PATCH_TOOL = "SECRET_PATCH_TOOL_zzz"


def _msg(role, created, completed=None, tokens=None, model="mod-1",
         provider="prov-1", variant="v-high", cost=0.25, finish="stop",
         error=None):
    data: dict = {"role": role, "time": {"created": created}}
    if completed is not None:
        data["time"]["completed"] = completed
    if role == "assistant":
        data.update({"modelID": model, "providerID": provider,
                     "variant": variant, "cost": cost, "finish": finish})
        if tokens is not None:
            data["tokens"] = tokens
        if error is not None:
            data["error"] = error
    return json.dumps(data)


def _tokens(i, o, r, cr, cw):
    return {"input": i, "output": o, "reasoning": r,
            "cache": {"read": cr, "write": cw}}


def _tool(tool, call, status, input_d, output, start, end, title=None,
          metadata=None):
    state: dict = {"status": status, "input": input_d, "output": output,
                   "time": {"start": start, "end": end}}
    if title is not None:
        state["title"] = title
    if metadata is not None:
        state["metadata"] = metadata
    return json.dumps({"type": "tool", "tool": tool, "callID": call,
                       "state": state})


def build_native(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE session(id TEXT PRIMARY KEY, parent_id TEXT,"
                " directory TEXT, title TEXT, version TEXT, model TEXT,"
                " agent TEXT, time_created INTEGER, time_updated INTEGER,"
                " time_compacting INTEGER)")
    con.execute("CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT,"
                " time_created INTEGER, time_updated INTEGER, data TEXT)")
    con.execute("CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT,"
                " session_id TEXT, time_created INTEGER, time_updated INTEGER,"
                " data TEXT)")
    # Parent session.
    con.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("ses_parent", None, "/repo", "Parent", "1.2.3", None,
                 "build", T0, T0 + 100000, None))
    # Child session.
    con.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("ses_child", "ses_parent", "/repo", "Child", "1.2.3", None,
                 "build", T0 + 1000, T0 + 90000, None))
    # Compaction session: boundary via time_compacting plus a part.
    con.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("ses_compact", None, "/repo", "Compact", "1.2.3", None,
                 "build", T0 + 2000, T0 + 80000, T0 + 50000))
    block = ("<<<AGENTSMD_PROJECT_DIRECTION_V1>>>"
             '{"status":"ready","instructions":{"sha256":"abc123"},'
             '"preferences":{"sha256":"def456"},'
             '"preferences_text":"SECRET_PREF_zzz"}'
             "<<<END_AGENTSMD_PROJECT_DIRECTION_V1>>>")
    # Parent user message with the direction block.
    con.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                ("msg_u1", "ses_parent", T0 + 10, T0 + 10,
                 json.dumps({"role": "user", "time": {"created": T0 + 10}})))
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                ("p_u1t", "msg_u1", "ses_parent", T0 + 10, T0 + 10,
                 json.dumps({"type": "text",
                             "text": "Do the thing " + block})))
    # Parent assistant message with full usage and tool parts.
    con.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                ("msg_a1", "ses_parent", T0 + 20, T0 + 20,
                 _msg("assistant", T0 + 20, T0 + 30,
                      _tokens(10, 5, 3, 2, 1))))
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                ("p_read1", "msg_a1", "ses_parent", T0 + 21, T0 + 21,
                 _tool("read", "call_read1", "completed",
                       {"filePath": "/repo/notes.md"}, SECRET_READ,
                       T0 + 21, T0 + 31, title="read notes"))),
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                ("p_read2", "msg_a1", "ses_parent", T0 + 22, T0 + 22,
                 _tool("read", "call_read2", "completed",
                       {"filePath": "/tmp/skills/my-skill/doc.md"},
                       "skill doc body", T0 + 22, T0 + 32))),
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                ("p_edit1", "msg_a1", "ses_parent", T0 + 23, T0 + 23,
                 _tool("edit", "call_edit1", "completed",
                       {"filePath": "/repo/a.txt"}, SECRET_EDIT,
                       T0 + 23, T0 + 43))),
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                ("p_skill1", "msg_a1", "ses_parent", T0 + 24, T0 + 24,
                 _tool("skill", "call_skill1", "completed",
                       {"name": "my-skill"}, SECRET_SKILL,
                       T0 + 24, T0 + 34,
                       metadata={"name": "my-skill",
                                 "dir": "/tmp/skills/my-skill"}))),
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                ("p_patch1", "msg_a1", "ses_parent", T0 + 25, T0 + 25,
                 json.dumps({"type": "patch", "hash": "deadbeef",
                             "files": ["/repo/a.txt"]}))),
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                ("p_patchtool1", "msg_a1", "ses_parent", T0 + 28, T0 + 28,
                 _tool("patch", "call_patch1", "completed",
                       {"filePath": "/repo/b.txt"}, SECRET_PATCH_TOOL,
                       T0 + 28, T0 + 38))),
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                ("p_reason1", "msg_a1", "ses_parent", T0 + 26, T0 + 26,
                 json.dumps({"type": "reasoning",
                             "text": SECRET_REASON}))),
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                ("p_file1", "msg_a1", "ses_parent", T0 + 27, T0 + 27,
                 json.dumps({"type": "file", "mime": "image/png",
                             "filename": "shot.png",
                             "url": "data:image/png;base64," + SECRET_DATAURL}))),
    # Unfinished assistant message with a running tool part.
    con.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                ("msg_a_unf", "ses_parent", T0 + 40, T0 + 40,
                 _msg("assistant", T0 + 40, None,
                      _tokens(1, 1, 0, 0, 0)))),
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                ("p_run1", "msg_a_unf", "ses_parent", T0 + 41, T0 + 41,
                 json.dumps({"type": "tool", "tool": "bash",
                             "callID": "call_run1",
                             "state": {"status": "running",
                                       "input": {"command": "sleep 10"},
                                       "time": {"start": T0 + 41}}}))),
    # Error message with all-zero counters: lifecycle only, no response.
    con.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                ("msg_a_err", "ses_parent", T0 + 50, T0 + 50,
                 _msg("assistant", T0 + 50, T0 + 60,
                      _tokens(0, 0, 0, 0, 0),
                      error={"name": "APIError",
                             "data": {"statusCode": 403,
                                      "message": SECRET_ERRMSG}}))),
    # Child messages.
    con.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                ("msg_cu1", "ses_child", T0 + 60, T0 + 60,
                 json.dumps({"role": "user",
                             "time": {"created": T0 + 60}})))
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                ("p_cu1t", "msg_cu1", "ses_child", T0 + 60, T0 + 60,
                 json.dumps({"type": "text", "text": "child prompt"})))
    con.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                ("msg_ca1", "ses_child", T0 + 70, T0 + 70,
                 _msg("assistant", T0 + 70, T0 + 80,
                      _tokens(4, 4, 0, 0, 0), model="mod-c",
                      provider="prov-c", variant="v-low", cost=0.1))),
    # Compaction session message plus compaction part.
    con.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                ("msg_xa1", "ses_compact", T0 + 80, T0 + 80,
                 _msg("assistant", T0 + 80, T0 + 90,
                      _tokens(2, 2, 0, 0, 0)))),
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                ("p_comp1", "msg_xa1", "ses_compact", T0 + 81, T0 + 81,
                 json.dumps({"type": "compaction"}))),
    con.commit()
    con.close()


class OpencodeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.native_dir = os.path.join(self.tmp.name, "native")
        os.makedirs(self.native_dir)
        self.db_file = os.path.join(self.native_dir, "opencode.db")
        build_native(self.db_file)
        self.ledger = os.path.join(self.tmp.name, "test.db")
        self.con = db.connect(self.ledger)
        db.init_db(self.con)
        self.stats = opencode.sync(self.con, source=self.db_file)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def q(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def test_usage_arithmetic_and_all_response_fields(self):
        row = self.q("SELECT * FROM responses WHERE response_id=?",
                     ("opencode:msg_a1",))[0]
        self.assertEqual(row["input_tokens"], 10)
        self.assertEqual(row["output_tokens"], 5)
        self.assertEqual(row["reasoning_output_tokens"], 3)
        self.assertEqual(row["cached_input_tokens"], 2)
        self.assertEqual(row["cache_write_input_tokens"], 1)
        self.assertEqual(row["total_tokens"], 21)
        self.assertEqual(row["model"], "mod-1")
        self.assertEqual(row["provider"], "prov-1")
        self.assertEqual(row["effort"], "v-high")
        self.assertAlmostEqual(row["cost_usd"], 0.25)
        self.assertEqual(row["semantics"], opencode.SEMANTICS)

    def test_unfinished_skipped_then_counted_after_completion(self):
        self.assertEqual(self.q("SELECT * FROM responses WHERE response_id=?",
                                ("opencode:msg_a_unf",)), [])
        self.assertEqual(self.q("SELECT * FROM events WHERE family='tool_result'"
                                " AND native_id=?", ("call_run1",)), [])
        # Finish the message and its running tool part natively.
        native = sqlite3.connect(self.db_file)
        native.execute(
            "UPDATE message SET data=?, time_updated=? WHERE id=?",
            (_msg("assistant", T0 + 40, T0 + 100, _tokens(1, 1, 0, 0, 0)),
             T0 + 100, "msg_a_unf"))
        native.execute("UPDATE part SET data=?, time_updated=? WHERE id=?",
                       (_tool("bash", "call_run1", "completed",
                              {"command": "sleep 10"}, "done output",
                              T0 + 41, T0 + 51), T0 + 100, "p_run1"))
        native.execute("UPDATE session SET time_updated=? WHERE id=?",
                       (T0 + 100, "ses_parent"))
        native.commit()
        native.close()
        again = opencode.sync(self.con, source=self.db_file)
        row = self.q("SELECT * FROM responses WHERE response_id=?",
                     ("opencode:msg_a_unf",))[0]
        self.assertEqual(row["total_tokens"], 2)
        res = self.q("SELECT * FROM events WHERE family='tool_result'"
                     " AND native_id=?", ("call_run1",))[0]
        self.assertEqual(res["status"], "ok")
        self.assertGreater(again["responses_inserted"], 0)

    def test_parent_and_child_sessions(self):
        parent = self.q("SELECT * FROM sessions WHERE session_key=?",
                        ("opencode:ses_parent",))[0]
        child = self.q("SELECT * FROM sessions WHERE session_key=?",
                       ("opencode:ses_child",))[0]
        self.assertEqual(child["parent_session_key"], "opencode:ses_parent")
        self.assertEqual(child["role"], "subagent")
        self.assertEqual(parent["project_dir"], "/repo")
        self.assertEqual(parent["client_version"], "1.2.3")
        kinds = {r["native_id"]: r["kind"] for r in self.q(
            "SELECT native_id, kind, is_genuine FROM submissions")}
        self.assertEqual(kinds["opencode:msg_u1"], "genuine")
        self.assertEqual(kinds["opencode:msg_cu1"], "synthetic")
        gen = {r["native_id"]: r["is_genuine"] for r in self.q(
            "SELECT native_id, is_genuine FROM submissions")}
        self.assertEqual(gen["opencode:msg_u1"], 1)
        self.assertEqual(gen["opencode:msg_cu1"], 0)

    def test_tool_call_result_join_target_status_duration_size(self):
        calls = {r["native_id"] for r in self.q(
            "SELECT native_id FROM events WHERE family='tool_call'")}
        results = {r["native_id"] for r in self.q(
            "SELECT native_id FROM events WHERE family='tool_result'")}
        self.assertIn("call_read1", calls)
        self.assertIn("call_read1", results)
        res = self.q("SELECT * FROM events WHERE family='tool_result'"
                     " AND native_id=?", ("call_read1",))[0]
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["target"], "/repo/notes.md")
        self.assertEqual(res["duration_ms"], 10)
        self.assertEqual(res["size_bytes"], len(SECRET_READ))
        call = self.q("SELECT * FROM events WHERE family='tool_call'"
                      " AND native_id=?", ("call_edit1",))[0]
        self.assertEqual(call["target"], "/repo/a.txt")

    def test_read_skill_read_and_skill_invoke(self):
        fams = {(r["family"], r["native_id"]) for r in self.q(
            "SELECT family, native_id FROM events")}
        self.assertIn(("read", "call_read1"), fams)
        self.assertIn(("skill_read", "call_read2"), fams)
        skill_read = self.q("SELECT * FROM events WHERE family='skill_read'"
                            " AND native_id=?", ("call_read2",))[0]
        self.assertEqual(skill_read["target"],
                         "/tmp/skills/my-skill/doc.md")
        invoke = self.q("SELECT * FROM events WHERE family='skill_invoke'"
                        " AND native_id=?", ("call_skill1",))[0]
        self.assertEqual(invoke["name"], "my-skill")
        changes = {(r["name"], r["native_id"]) for r in self.q(
            "SELECT name, native_id FROM events WHERE family='file_change'")}
        self.assertIn(("edit", "call_edit1"), changes)
        self.assertIn(("patch", "p_patch1"), changes)
        # A tool part named patch is its own file_change row keyed by
        # callID, separable from the native patch part keyed by part id.
        self.assertIn(("patch", "call_patch1"), changes)
        patch_tool = self.q("SELECT * FROM events WHERE family='file_change'"
                            " AND native_id=?", ("call_patch1",))[0]
        self.assertEqual(patch_tool["name"], "patch")
        self.assertEqual(patch_tool["target"], "/repo/b.txt")

    def test_error_message_has_lifecycle_but_no_response(self):
        self.assertEqual(self.q("SELECT * FROM responses WHERE response_id=?",
                                ("opencode:msg_a_err",)), [])
        rows = self.q("SELECT * FROM events WHERE family='lifecycle'"
                      " AND native_id=?", ("msg_a_err",))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "error")
        self.assertIn("403", rows[0]["status"] or "")
        detail = json.loads(rows[0]["detail_json"] or "{}")
        self.assertEqual(detail.get("error"), "APIError")
        self.assertEqual(detail.get("status_code"), 403)
        self.assertNotIn(SECRET_ERRMSG, rows[0]["detail_json"] or "")

    def test_compaction_from_part_and_session_time(self):
        fams = [(r["family"], r["native_id"]) for r in self.q(
            "SELECT family, native_id FROM events"
            " WHERE session_key='opencode:ses_compact'")]
        self.assertIn(("compaction", "p_comp1"), fams)
        self.assertIn(("compaction", "time_compacting"), fams)

    def test_identity_without_preference_contents(self):
        row = self.q("SELECT * FROM sessions WHERE session_key=?",
                     ("opencode:ses_parent",))[0]
        self.assertEqual(row["instructions_sha256"], "abc123")
        self.assertEqual(row["preferences_sha256"], "def456")
        body = json.dumps(dict(row))
        self.assertNotIn("SECRET_PREF_zzz", body)

    def test_no_raw_tool_file_patch_or_error_contents_stored(self):
        blobs = []
        for table in ("events", "responses", "sessions", "import_errors"):
            for r in self.q(f"SELECT * FROM {table}"):
                blobs.append(json.dumps(dict(r), default=str))
        haystack = "\n".join(blobs)
        for secret in (SECRET_READ, SECRET_EDIT, SECRET_SKILL, SECRET_REASON,
                       SECRET_DATAURL, SECRET_ERRMSG, SECRET_PATCH_TOOL,
                       "SECRET_PREF_zzz"):
            self.assertNotIn(secret, haystack)
        subs = "\n".join(r["text_excerpt"] for r in self.q(
            "SELECT text_excerpt FROM submissions"))
        for secret in (SECRET_READ, SECRET_DATAURL):
            self.assertNotIn(secret, subs)

    def test_unchanged_skip_and_idempotent_full_resync(self):
        again = opencode.sync(self.con, root=self.native_dir)
        self.assertEqual(again["sources"], 3)
        self.assertEqual(again["unchanged"], 3)
        self.assertEqual(again["responses_inserted"], 0)
        full = opencode.sync(self.con, source=self.db_file, full=True)
        self.assertEqual(full["responses_inserted"], 0)
        self.assertEqual(full["unchanged"], 0)
        n_resp = self.q("SELECT COUNT(*) n FROM responses")[0]["n"]
        n_evt = self.q("SELECT COUNT(*) n FROM events")[0]["n"]
        full2 = opencode.sync(self.con, root=self.native_dir, full=True)
        self.assertEqual(full2["responses_inserted"], 0)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM responses")[0]["n"],
                         n_resp)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM events")[0]["n"],
                         n_evt)


if __name__ == "__main__":
    unittest.main()
