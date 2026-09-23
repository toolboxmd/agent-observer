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
SECRET_PREF = "SECRET_PREF_zzz"
SECRET_MAL_INPUT = "SECRET_MAL_INPUT_zzz_qqq"
SECRET_MAL_OUTPUT = "SECRET_MAL_OUTPUT_zzz_qqq"


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

    def test_child_session_excerpt_is_empty(self):
        rows = self.q("SELECT * FROM submissions WHERE native_id=?",
                      ("opencode:msg_cu1",))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "synthetic")
        self.assertEqual(rows[0]["is_genuine"], 0)
        # Child prompts are agent-generated: no child text enters excerpts.
        self.assertEqual(rows[0]["text_excerpt"], "")
        self.assertNotIn("child prompt", rows[0]["text_excerpt"] or "")

    def test_truncated_direction_block_leaks_nothing(self):
        sentinel = "SECRET_TRUNCATED_DIRECTION_zzz_qqq"
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_trunc", None, "/repo", "Trunc", "1.2.3", None,
                        "build", T0 + 700, T0 + 700, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_trunc_u", "ses_trunc", T0 + 700, T0 + 700,
                        json.dumps({"role": "user",
                                    "time": {"created": T0 + 700}})))
        truncated = ("Please do work "
                     "<<<AGENTSMD_PROJECT_DIRECTION_V1>>>"
                     '{"status":"ready"} ' + sentinel +
                     " trailing without close")
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_trunc_t", "msg_trunc_u", "ses_trunc",
                        T0 + 700, T0 + 700,
                        json.dumps({"type": "text", "text": truncated})))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        # The sentinel appears in no ledger table and no submission excerpt.
        self._assert_no_secret_anywhere((sentinel,))
        subs = self.q("SELECT native_id, text_excerpt FROM submissions")
        for row in subs:
            self.assertNotIn(sentinel, row["text_excerpt"] or "")
        trunc = [r for r in subs
                 if r["native_id"] == "opencode:msg_trunc_u"]
        self.assertEqual(len(trunc), 1)
        self.assertNotIn(sentinel, trunc[0]["text_excerpt"] or "")
        self.assertNotIn("<<<AGENTSMD_PROJECT_DIRECTION_V1>>>",
                         trunc[0]["text_excerpt"] or "")
        self.assertIn("Please do work", trunc[0]["text_excerpt"] or "")

    def test_missing_id_part_quarantined_and_later_parts_import(self):
        sentinel = "SECRET_MISSING_ID_HASH_zzz_qqq"
        native = sqlite3.connect(self.db_file)
        # Malformed patch part without an id: empty string is the only
        # missing-id value the PRIMARY KEY column can store.
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("", "msg_a1", "ses_parent", T0 + 800, T0 + 800,
                        json.dumps({"type": "patch", "hash": sentinel,
                                    "files": ["/repo/a.txt"]})))
        # A later valid part in the same session must still import.
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_valid_after", "msg_a1", "ses_parent",
                        T0 + 801, T0 + 801,
                        json.dumps({"type": "compaction"})))
        # A later session (sorts after ses_parent) must still import.
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_zzz_late", None, "/repo", "Late", "1.2.3",
                        None, "build", T0 + 802, T0 + 802, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_zzz_a", "ses_zzz_late", T0 + 802, T0 + 802,
                        _msg("assistant", T0 + 802, T0 + 812,
                             _tokens(2, 2, 0, 0, 0))))
        native.execute("UPDATE session SET time_updated=? WHERE id=?",
                       (T0 + 802, "ses_parent"))
        native.commit()
        native.close()
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(stats["malformed"], 1)
        errs = self.q("SELECT * FROM import_errors WHERE error=?",
                      ("missing_id",))
        self.assertTrue(errs)
        for row in errs:
            # Closed category, structure-only excerpt: sorted top-level
            # keys, no values.
            self.assertIn(row["error"], opencode.IMPORT_ERROR_CATEGORIES)
            self.assertLessEqual(len(row["line_excerpt"] or ""), 200)
            self.assertNotIn(sentinel, row["line_excerpt"] or "")
            self.assertNotIn("type=patch", row["line_excerpt"] or "")
            self.assertNotIn("patch", row["line_excerpt"] or "")
            self.assertNotIn("/repo/a.txt", row["line_excerpt"] or "")
        # The patch record contributes exactly its sorted key names.
        self.assertTrue(any(
            (r["line_excerpt"] or "") == "files,hash,type" for r in errs))
        self._assert_no_secret_anywhere((sentinel,))
        # The later valid part in the same session imported.
        late_part = self.q("SELECT * FROM events WHERE family='compaction'"
                           " AND native_id=?", ("p_valid_after",))
        self.assertEqual(len(late_part), 1)
        # The later session imported.
        late_resp = self.q("SELECT * FROM responses WHERE response_id=?",
                           ("opencode:msg_zzz_a",))
        self.assertEqual(len(late_resp), 1)
        late_sess = self.q("SELECT * FROM sessions WHERE session_key=?",
                           ("opencode:ses_zzz_late",))
        self.assertEqual(len(late_sess), 1)

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
        # Whitelisted safe metadata only: numeric status code, no error
        # name, message or other free text.
        self.assertEqual(detail, {"status_code": 403})
        self.assertNotIn("APIError", rows[0]["detail_json"] or "")
        self.assertNotIn(SECRET_ERRMSG, rows[0]["detail_json"] or "")
        self._assert_no_secret_anywhere(("APIError", SECRET_ERRMSG))

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

    def _ledger_text_columns(self):
        tables = [r["name"] for r in self.q(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")]
        cols_by_table = {}
        for table in tables:
            info = self.q(f"PRAGMA table_info({table})")
            text_cols = [c["name"] for c in info
                         if "TEXT" in (c["type"] or "").upper()
                         or "CHAR" in (c["type"] or "").upper()]
            cols_by_table[table] = text_cols
        return cols_by_table

    def _assert_no_secret_anywhere(self, secrets):
        cols_by_table = self._ledger_text_columns()
        # Every ledger table with text content is inspected, including
        # submissions and import_errors.
        self.assertIn("submissions", cols_by_table)
        self.assertIn("import_errors", cols_by_table)
        for table, cols in cols_by_table.items():
            rows = self.q(f"SELECT * FROM {table}")
            for row in rows:
                d = dict(row)
                for col in cols:
                    val = d.get(col)
                    if val is None:
                        continue
                    for secret in secrets:
                        self.assertNotIn(
                            secret, str(val),
                            f"secret leaked in {table}.{col}")

    def test_privacy_every_table_no_preference_contents_and_excerpt_bounds(self):
        secrets = (SECRET_READ, SECRET_EDIT, SECRET_SKILL, SECRET_REASON,
                   SECRET_DATAURL, SECRET_ERRMSG, SECRET_PATCH_TOOL,
                   SECRET_PREF)
        self._assert_no_secret_anywhere(secrets)
        # Submissions keep a short human-only excerpt: the direction block
        # (which carries preference contents) is stripped, synthetic parts
        # are excluded, and identity still observes the full text.
        subs = self.q("SELECT native_id, text_excerpt FROM submissions")
        self.assertTrue(subs)
        for row in subs:
            excerpt = row["text_excerpt"] or ""
            self.assertLessEqual(len(excerpt), 300)
            self.assertNotIn(SECRET_PREF, excerpt)
            self.assertNotIn("<<<AGENTSMD_PROJECT_DIRECTION_V1>>>", excerpt)
            self.assertNotIn("preferences_text", excerpt)
        parent = [r for r in subs
                  if r["native_id"] == "opencode:msg_u1"][0]
        self.assertIn("Do the thing", parent["text_excerpt"])
        self.assertNotIn("abc123", parent["text_excerpt"])
        # import_errors excerpts stay within 200 characters.
        for row in self.q("SELECT line_excerpt FROM import_errors"):
            self.assertLessEqual(len(row["line_excerpt"] or ""), 200)

    def test_malformed_tool_part_secrets_never_stored(self):
        native = sqlite3.connect(self.db_file)
        bad = json.dumps({"type": "tool", "tool": "bash",
                          "state": {"status": "completed",
                                    "input": {"command": SECRET_MAL_INPUT},
                                    "output": SECRET_MAL_OUTPUT,
                                    "time": {"start": T0 + 200,
                                             "end": T0 + 210}}})
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_mal1", "msg_a1", "ses_parent", T0 + 200, T0 + 200,
                        bad))
        native.execute("UPDATE session SET time_updated=? WHERE id=?",
                       (T0 + 200, "ses_parent"))
        native.commit()
        native.close()
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(stats["malformed"], 1)
        errs = self.q("SELECT * FROM import_errors WHERE error=?",
                      ("missing_id",))
        self.assertTrue(errs)
        for row in errs:
            self.assertIn(row["error"], opencode.IMPORT_ERROR_CATEGORIES)
            self.assertLessEqual(len(row["line_excerpt"] or ""), 200)
            # Structure only: sorted keys, no secret values.
            self.assertNotIn(SECRET_MAL_INPUT, row["line_excerpt"] or "")
            self.assertNotIn(SECRET_MAL_OUTPUT, row["line_excerpt"] or "")
        self._assert_no_secret_anywhere(
            (SECRET_MAL_INPUT, SECRET_MAL_OUTPUT, SECRET_READ, SECRET_PREF))

    def test_pending_tool_becomes_completed_on_resync(self):
        native = sqlite3.connect(self.db_file)
        native.execute(
            "INSERT INTO message VALUES(?,?,?,?,?)",
            ("msg_pend", "ses_parent", T0 + 300, T0 + 300,
             _msg("assistant", T0 + 300, T0 + 310,
                  _tokens(1, 1, 0, 0, 0))))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_pend1", "msg_pend", "ses_parent", T0 + 301,
                        T0 + 301,
                        json.dumps({"type": "tool", "tool": "bash",
                                    "callID": "call_pend1",
                                    "state": {"status": "pending",
                                              "input": {"command": "sleep 5"},
                                              "time": {"start": T0 + 301}}})))
        native.execute("UPDATE session SET time_updated=? WHERE id=?",
                       (T0 + 310, "ses_parent"))
        native.commit()
        native.close()
        first = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(first["events_inserted"], 1)
        calls = self.q("SELECT * FROM events WHERE family='tool_call'"
                       " AND native_id=?", ("call_pend1",))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.q("SELECT * FROM events WHERE family='tool_result'"
                                " AND native_id=?", ("call_pend1",)), [])
        # Later snapshot completes the same call.
        native = sqlite3.connect(self.db_file)
        native.execute("UPDATE part SET data=?, time_updated=? WHERE id=?",
                       (_tool("bash", "call_pend1", "completed",
                              {"command": "sleep 5"}, "done",
                              T0 + 301, T0 + 311), T0 + 311, "p_pend1"))
        native.execute("UPDATE session SET time_updated=? WHERE id=?",
                       (T0 + 311, "ses_parent"))
        native.commit()
        native.close()
        second = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(second["events_inserted"], 1)
        res = self.q("SELECT * FROM events WHERE family='tool_result'"
                     " AND native_id=?", ("call_pend1",))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "ok")
        n_evt = self.q("SELECT COUNT(*) n FROM events")[0]["n"]
        third = opencode.sync(self.con, source=self.db_file, full=True)
        self.assertEqual(third["events_inserted"], 0)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM events")[0]["n"],
                         n_evt)

    def test_response_mutable_fields_update_in_place(self):
        before = self.q("SELECT * FROM responses WHERE response_id=?",
                        ("opencode:msg_a1",))[0]
        self.assertEqual(before["input_tokens"], 10)
        native = sqlite3.connect(self.db_file)
        new_data = json.loads(_msg("assistant", T0 + 20, T0 + 999,
                                   _tokens(99, 88, 7, 6, 5), model="mod-2",
                                   provider="prov-2", variant="v-low",
                                   cost=0.99))
        native.execute("UPDATE message SET data=?, time_updated=? WHERE id=?",
                       (json.dumps(new_data), T0 + 999, "msg_a1"))
        native.execute("UPDATE session SET time_updated=? WHERE id=?",
                       (T0 + 999, "ses_parent"))
        native.commit()
        native.close()
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(stats["responses_inserted"], 1)
        after = self.q("SELECT * FROM responses WHERE response_id=?",
                       ("opencode:msg_a1",))[0]
        self.assertEqual(after["input_tokens"], 99)
        self.assertEqual(after["output_tokens"], 88)
        self.assertEqual(after["reasoning_output_tokens"], 7)
        self.assertEqual(after["cached_input_tokens"], 6)
        self.assertEqual(after["cache_write_input_tokens"], 5)
        self.assertEqual(after["total_tokens"], 99 + 88 + 7 + 6 + 5)
        self.assertEqual(after["model"], "mod-2")
        self.assertEqual(after["provider"], "prov-2")
        self.assertEqual(after["effort"], "v-low")
        self.assertAlmostEqual(after["cost_usd"], 0.99)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM responses"
                                " WHERE response_id=?",
                                ("opencode:msg_a1",))[0]["n"], 1)
        # No immutable conflict for a mutable change.
        conflicts = self.q("SELECT * FROM import_errors WHERE error=?",
                           ("usage_conflict",))
        self.assertEqual(conflicts, [])

    def test_tool_payload_status_updates_in_place(self):
        call_before = self.q("SELECT * FROM events WHERE family='tool_call'"
                             " AND native_id=?", ("call_read1",))[0]
        self.assertEqual(call_before["target"], "/repo/notes.md")
        res_before = self.q("SELECT * FROM events WHERE family='tool_result'"
                            " AND native_id=?", ("call_read1",))[0]
        self.assertEqual(res_before["status"], "ok")
        native = sqlite3.connect(self.db_file)
        new_part = json.loads(_tool("read", "call_read1", "error",
                                    {"filePath": "/repo/notes.md"},
                                    "short failure", T0 + 21, T0 + 41,
                                    title="read notes v2"))
        native.execute("UPDATE part SET data=?, time_updated=? WHERE id=?",
                       (json.dumps(new_part), T0 + 400, "p_read1"))
        native.execute("UPDATE session SET time_updated=? WHERE id=?",
                       (T0 + 400, "ses_parent"))
        native.commit()
        native.close()
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(stats["events_inserted"], 1)
        res_after = self.q("SELECT * FROM events WHERE family='tool_result'"
                           " AND native_id=?", ("call_read1",))[0]
        self.assertEqual(res_after["status"], "error")
        self.assertEqual(res_after["size_bytes"], len("short failure"))
        self.assertEqual(res_after["duration_ms"], 20)
        call_after = self.q("SELECT * FROM events WHERE family='tool_call'"
                            " AND native_id=?", ("call_read1",))[0]
        # Tool titles never enter the ledger, but the payload update still
        # lands in place on the same joined rows.
        self.assertNotIn("read notes v2",
                         (call_after["detail_json"] or ""))
        detail = json.loads(call_after["detail_json"] or "{}")
        self.assertNotIn("title", detail)
        for family in ("tool_call", "tool_result"):
            n = self.q("SELECT COUNT(*) n FROM events WHERE family=?"
                       " AND native_id=? AND session_key=?",
                       (family, "call_read1", "opencode:ses_parent"))[0]["n"]
            self.assertEqual(n, 1)

    def test_immutable_response_conflict_recorded(self):
        native = sqlite3.connect(self.db_file)
        native.execute("UPDATE message SET session_id=? WHERE id=?",
                       ("ses_child", "msg_a1"))
        native.execute("UPDATE session SET time_updated=? WHERE id IN (?,?)",
                       (T0 + 500, "ses_parent", "ses_child"))
        native.commit()
        native.close()
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(stats["malformed"], 1)
        conflicts = self.q("SELECT * FROM import_errors WHERE error=?",
                           ("usage_conflict",))
        self.assertTrue(conflicts)
        for row in conflicts:
            self.assertLessEqual(len(row["line_excerpt"] or ""), 200)
        # The existing row is kept; no duplicate response appears.
        rows = self.q("SELECT * FROM responses WHERE response_id=?",
                      ("opencode:msg_a1",))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_key"], "opencode:ses_parent")
        self._assert_no_secret_anywhere((SECRET_READ, SECRET_PREF))

    def test_schema_variant_missing_optional_column_still_imports(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            variant = os.path.join(tmp.name, "opencode.db")
            con = sqlite3.connect(variant)
            # Session table without the optional `version` column.
            con.execute("CREATE TABLE session(id TEXT PRIMARY KEY,"
                        " parent_id TEXT, directory TEXT, title TEXT,"
                        " time_created INTEGER, time_updated INTEGER,"
                        " time_compacting INTEGER)")
            con.execute("CREATE TABLE message(id TEXT PRIMARY KEY,"
                        " session_id TEXT, time_created INTEGER,"
                        " time_updated INTEGER, data TEXT)")
            con.execute("CREATE TABLE part(id TEXT PRIMARY KEY,"
                        " message_id TEXT, session_id TEXT,"
                        " time_created INTEGER, time_updated INTEGER,"
                        " data TEXT)")
            con.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?)",
                        ("ses_v", None, "/repo", "V", T0, T0 + 50, None))
            con.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                        ("msg_vu", "ses_v", T0 + 10, T0 + 10,
                         json.dumps({"role": "user",
                                     "time": {"created": T0 + 10}})))
            con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                        ("p_vu", "msg_vu", "ses_v", T0 + 10, T0 + 10,
                         json.dumps({"type": "text", "text": "hello"})))
            con.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                        ("msg_va", "ses_v", T0 + 20, T0 + 20,
                         _msg("assistant", T0 + 20, T0 + 30,
                              _tokens(2, 3, 0, 0, 0))))
            con.commit()
            con.close()
            stats = opencode.sync(self.con, source=variant)
            self.assertEqual(stats["failed"], [])
            row = self.q("SELECT * FROM responses WHERE response_id=?",
                         ("opencode:msg_va",))
            self.assertEqual(len(row), 1)
            sess = self.q("SELECT * FROM sessions WHERE session_key=?",
                          ("opencode:ses_v",))[0]
            self.assertIsNone(sess["client_version"])
        finally:
            tmp.cleanup()

    def test_unknown_role_type_and_malformed_rows_quarantined(self):
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_alien", "ses_parent", T0 + 600, T0 + 600,
                        json.dumps({"role": "alien",
                                    "time": {"created": T0 + 600}})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_weird", "msg_a1", "ses_parent", T0 + 601, T0 + 601,
                        json.dumps({"type": "weird_type", "blob": "x"})))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_broken", "ses_parent", T0 + 602, T0 + 602,
                        "not json at all"))
        native.execute("UPDATE session SET time_updated=? WHERE id=?",
                       (T0 + 602, "ses_parent"))
        native.commit()
        native.close()
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(stats["malformed"], 3)
        errors = [r["error"] for r in self.q("SELECT error FROM import_errors")]
        # Exact closed categories, no appended exception names or values.
        for err in errors:
            self.assertIn(err, opencode.IMPORT_ERROR_CATEGORIES)
        self.assertIn("unknown_record", errors)
        self.assertIn("unsupported_schema", errors)
        self.assertIn("malformed_json", errors)
        for row in self.q("SELECT * FROM import_errors"):
            self.assertLessEqual(len(row["line_excerpt"] or ""), 200)
            self.assertNotIn("not json at all", row["line_excerpt"] or "")
        # Unknown message produced no response or submission.
        self.assertEqual(self.q("SELECT * FROM responses WHERE response_id=?",
                                ("opencode:msg_alien",)), [])
        self.assertEqual(self.q("SELECT * FROM submissions WHERE native_id=?",
                                ("opencode:msg_alien",)), [])


    def test_privacy_spec_tagged_blocks_titles_errors_and_shape_only(self):
        s_user_rule = "SECRET_USER_RULE_zzz_qqq"
        s_sys_rem = "SECRET_SYS_REM_zzz_qqq"
        s_unterm_tag = "SECRET_UNTERM_TAG_zzz_qqq"
        s_unterm_triple = "SECRET_UNTERM_TRIPLE_zzz_qqq"
        s_tool_title = "SECRET_TOOL_TITLE_zzz_qqq"
        s_lifecycle = "SECRET_LIFECYCLE_ERR_zzz_qqq"
        s_mal = "SECRET_MAL_VAL_zzz_qqq"
        s_session_title = "SECRET_SESSION_TITLE_zzz_qqq"
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_priv", None, "/repo", s_session_title, "1.2.3",
                        None, "build", T0 + 900, T0 + 900, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_priv_u", "ses_priv", T0 + 900, T0 + 900,
                        json.dumps({"role": "user",
                                    "time": {"created": T0 + 900}})))
        human = ("Safe human request   with \n  whitespace noise\t here "
                 f"<user_rule>do not keep {s_user_rule}</user_rule>"
                 " middle keep "
                 f"<system-reminder foo=\"bar\">hide {s_sys_rem}</system-reminder>"
                 " tail keep "
                 f"<mytag attr=\"x\">leak {s_unterm_tag}"
                 f"<<<MYSTUFF>>>leak {s_unterm_triple}")
        # Note: the unterminated generic tag swallows the unterminated
        # triple block too (fail-closed through end of text), so the
        # excerpt must end at "tail keep".
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_priv_t", "msg_priv_u", "ses_priv",
                        T0 + 900, T0 + 900,
                        json.dumps({"type": "text", "text": human})))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_priv_a", "ses_priv", T0 + 910, T0 + 910,
                        _msg("assistant", T0 + 910, T0 + 920,
                             _tokens(2, 2, 0, 0, 0),
                             error={"name": s_lifecycle,
                                    "data": {"statusCode": 500,
                                             "message": "boom " + s_lifecycle}})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_priv_tool", "msg_priv_a", "ses_priv",
                        T0 + 911, T0 + 911,
                        _tool("bash", "call_priv1", "completed",
                              {"command": "echo hi"}, "output ok",
                              T0 + 911, T0 + 921,
                              title="title " + s_tool_title)))
        # Malformed tool part without callID carrying secret values.
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_priv_mal", "msg_priv_a", "ses_priv",
                        T0 + 912, T0 + 912,
                        json.dumps({"type": "tool", "tool": "bash",
                                    "state": {"status": "completed",
                                              "input": {"command": s_mal},
                                              "output": s_mal,
                                              "time": {"start": T0 + 912,
                                                       "end": T0 + 922}}})))
        native.commit()
        native.close()
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(stats["malformed"], 1)
        secrets = (s_user_rule, s_sys_rem, s_unterm_tag, s_unterm_triple,
                   s_tool_title, s_lifecycle, s_mal, s_session_title)
        self._assert_no_secret_anywhere(secrets)
        # Sanitized genuine excerpt: collapsed, bounded, safe prefix kept.
        sub = self.q("SELECT * FROM submissions WHERE native_id=?",
                     ("opencode:msg_priv_u",))[0]
        self.assertEqual(sub["kind"], "genuine")
        self.assertEqual(sub["is_genuine"], 1)
        excerpt = sub["text_excerpt"] or ""
        self.assertLessEqual(len(excerpt), 300)
        self.assertIn("Safe human request", excerpt)
        self.assertIn("whitespace noise here", excerpt)
        self.assertIn("middle keep", excerpt)
        self.assertIn("tail keep", excerpt)
        self.assertNotIn("  ", excerpt)
        self.assertNotIn("\n", excerpt)
        for marker in ("<user_rule>", "</user_rule>", "<system-reminder>",
                       "</system-reminder>", "<mytag", "<<<MYSTUFF>>>",
                       s_user_rule, s_sys_rem, s_unterm_tag, s_unterm_triple):
            self.assertNotIn(marker, excerpt)
        # import_errors: exact closed categories, keys-only excerpts.
        for row in self.q("SELECT * FROM import_errors"):
            self.assertIn(row["error"], opencode.IMPORT_ERROR_CATEGORIES)
            self.assertNotIn(":", row["error"])
            lx = row["line_excerpt"] or ""
            self.assertLessEqual(len(lx), 200)
            for secret in secrets:
                self.assertNotIn(secret, lx)
            # No values: ids, roles, types, paths must not appear as values.
            self.assertNotIn("id=", lx)
            self.assertNotIn("role=", lx)
            self.assertNotIn("type=", lx)
            self.assertNotIn("shape=", lx)
            self.assertNotIn("keys=", lx)
            if lx != "[]":
                # Only comma-delimited key names.
                self.assertNotIn(" ", lx)
                self.assertNotIn("=", lx)
        mal_rows = self.q("SELECT * FROM import_errors WHERE error=?",
                          ("missing_id",))
        self.assertTrue(mal_rows)
        self.assertTrue(any(
            (r["line_excerpt"] or "") == "state,tool,type" for r in mal_rows))
        # events.detail_json: whitelisted safe metadata only.
        allowed_keys = {"message_id", "status_code", "skill", "hash"}
        for row in self.q("SELECT * FROM events"):
            detail_json = row["detail_json"] or ""
            for secret in secrets:
                self.assertNotIn(secret, detail_json)
            if detail_json:
                detail = json.loads(detail_json)
                self.assertTrue(set(detail.keys()) <= allowed_keys,
                                f"unsafe detail keys: {detail}")
                for key in ("title", "error", "message", "output",
                            "content", "arguments", "args", "input"):
                    self.assertNotIn(key, detail)
        call = self.q("SELECT * FROM events WHERE family='tool_call'"
                      " AND native_id=?", ("call_priv1",))[0]
        self.assertNotIn(s_tool_title, call["detail_json"] or "")
        call_detail = json.loads(call["detail_json"] or "{}")
        self.assertEqual(set(call_detail.keys()), {"message_id"})
        self.assertEqual(call_detail["message_id"], "msg_priv_a")
        life = self.q("SELECT * FROM events WHERE family='lifecycle'"
                      " AND native_id=?", ("msg_priv_a",))[0]
        self.assertEqual(json.loads(life["detail_json"] or "{}"),
                         {"status_code": 500})
        result = self.q("SELECT * FROM events WHERE family='tool_result'"
                        " AND native_id=?", ("call_priv1",))[0]
        self.assertIsNone(result["detail_json"])
        # Rule 7: native session titles never enter the ledger.
        sess = self.q("SELECT * FROM sessions WHERE session_key=?",
                      ("opencode:ses_priv",))[0]
        self.assertNotIn(s_session_title, json.dumps(dict(sess)))

    def test_dotted_and_namespaced_tags_remove_exact_block_and_keep_tail(self):
        s_dot = "SECRET_DOT_TAG_zzz_qqq"
        s_ns = "SECRET_NS_TAG_zzz_qqq"
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_dottag", None, "/repo", "DotTag", "1.2.3", None,
                        "build", T0 + 930, T0 + 930, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_dottag_u", "ses_dottag", T0 + 930, T0 + 930,
                        json.dumps({"role": "user",
                                    "time": {"created": T0 + 930}})))
        human = ("Head keep "
                 f"<custom.tag attr=\"x\">hide {s_dot}</custom.tag>"
                 " middle keep "
                 f"<x:y>hide {s_ns}</x:y>"
                 " tail keep")
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_dottag_t", "msg_dottag_u", "ses_dottag",
                        T0 + 930, T0 + 930,
                        json.dumps({"type": "text", "text": human})))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        self._assert_no_secret_anywhere((s_dot, s_ns))
        sub = self.q("SELECT * FROM submissions WHERE native_id=?",
                     ("opencode:msg_dottag_u",))[0]
        self.assertEqual(sub["kind"], "genuine")
        excerpt = sub["text_excerpt"] or ""
        self.assertIn("Head keep", excerpt)
        self.assertIn("middle keep", excerpt)
        self.assertIn("tail keep", excerpt)
        for marker in ("<custom.tag", "</custom.tag>", "<x:y>", "</x:y>",
                       s_dot, s_ns):
            self.assertNotIn(marker, excerpt)

    def test_resync_updates_stale_submission_excerpt_kind_hash_genuine(self):
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_resync", None, "/repo", "Resync", "1.2.3", None,
                        "build", T0 + 950, T0 + 950, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_resync_u", "ses_resync", T0 + 950, T0 + 950,
                        json.dumps({"role": "user",
                                    "time": {"created": T0 + 950}})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_resync_t", "msg_resync_u", "ses_resync",
                        T0 + 950, T0 + 950,
                        json.dumps({"type": "text",
                                    "text": "Original safe prompt Alpha"})))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        first = self.q("SELECT * FROM submissions WHERE native_id=?",
                       ("opencode:msg_resync_u",))[0]
        self.assertEqual(first["kind"], "genuine")
        self.assertEqual(first["is_genuine"], 1)
        self.assertIn("Original safe prompt Alpha", first["text_excerpt"])
        old_hash = first["text_hash"]
        old_excerpt = first["text_excerpt"]
        # Mutate the native prompt; bump timestamps so the fingerprint
        # path re-syncs the session.
        native = sqlite3.connect(self.db_file)
        native.execute("UPDATE part SET data=?, time_updated=? WHERE id=?",
                       (json.dumps({"type": "text",
                                    "text": "Updated safe prompt Beta "
                                            "<user_rule>hide me</user_rule>"}),
                        T0 + 960, "p_resync_t"))
        native.execute("UPDATE session SET time_updated=? WHERE id=?",
                       (T0 + 960, "ses_resync"))
        native.commit()
        native.close()
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(stats["submissions_inserted"], 1)
        second = self.q("SELECT * FROM submissions WHERE native_id=?",
                        ("opencode:msg_resync_u",))[0]
        self.assertEqual(
            self.q("SELECT COUNT(*) n FROM submissions WHERE native_id=?",
                   ("opencode:msg_resync_u",))[0]["n"], 1)
        self.assertEqual(second["kind"], "genuine")
        self.assertEqual(second["is_genuine"], 1)
        self.assertIn("Updated safe prompt Beta", second["text_excerpt"])
        self.assertNotIn("hide me", second["text_excerpt"])
        self.assertNotEqual(second["text_excerpt"], old_excerpt)
        self.assertNotEqual(second["text_hash"], old_hash)
        # Flip to synthetic: the same row must clear its excerpt.
        native = sqlite3.connect(self.db_file)
        native.execute("UPDATE part SET data=?, time_updated=? WHERE id=?",
                       (json.dumps({"type": "text", "text": "now synthetic",
                                    "synthetic": True}),
                        T0 + 970, "p_resync_t"))
        native.execute("UPDATE session SET time_updated=? WHERE id=?",
                       (T0 + 970, "ses_resync"))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        third = self.q("SELECT * FROM submissions WHERE native_id=?",
                       ("opencode:msg_resync_u",))[0]
        self.assertEqual(third["kind"], "synthetic")
        self.assertEqual(third["is_genuine"], 0)
        self.assertEqual(third["text_excerpt"], "")
        self.assertNotIn("now synthetic", third["text_excerpt"] or "")


if __name__ == "__main__":
    unittest.main()
