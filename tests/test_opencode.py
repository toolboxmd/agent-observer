"""OpenCode adapter: usage arithmetic, unfinished re-sync, parent/child,
tool joins, reads, skills, errors, unchanged skip, idempotent re-sync."""

import json
import os
import sqlite3
import tempfile
import unittest

from agent_observer import db, privacy
from agent_observer.adapters import opencode
from agent_observer.ingest import text_hash

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
        # Rule 6: the lifecycle family keeps no detail. The numeric status
        # survives in the status column while the error name, message and
        # any status_code key never persist.
        self.assertIsNone(rows[0]["detail_json"])
        self.assertEqual(json.loads(rows[0]["detail_json"] or "{}"), {})
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


    def test_privacy_spec_first_marker_titles_errors_and_shape_only(self):
        # Rewritten under the closed ruling: excerpts keep only the human
        # text before the first tag-like marker ('<' followed by a letter,
        # '/' or '!', or '<<<') with no tag parsing, so everything from
        # the first '<user_rule>' on is dropped fail-closed.
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
        # Note: the first tag-like marker ('<user_rule>') ends the excerpt
        # fail-closed with no tag parsing, so "middle keep", "tail keep"
        # and every later block are dropped, not preserved.
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
        # Genuine excerpt under rule 1: collapsed, bounded, and only the
        # prefix before the first tag-like marker. Everything after that
        # marker is dropped, never parsed for closers or tails.
        sub = self.q("SELECT * FROM submissions WHERE native_id=?",
                     ("opencode:msg_priv_u",))[0]
        self.assertEqual(sub["kind"], "genuine")
        self.assertEqual(sub["is_genuine"], 1)
        excerpt = sub["text_excerpt"] or ""
        self.assertLessEqual(len(excerpt), 300)
        self.assertEqual(excerpt, "Safe human request with whitespace noise here")
        self.assertNotIn("  ", excerpt)
        self.assertNotIn("\n", excerpt)
        self.assertNotIn("middle keep", excerpt)
        self.assertNotIn("tail keep", excerpt)
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
            # No values: an empty excerpt or comma-delimited sorted key
            # names only, never ids, roles, types or paths as values.
            if lx:
                self.assertNotIn(" ", lx)
                self.assertNotIn("=", lx)
                self.assertEqual(lx, ",".join(sorted(lx.split(","))))
        mal_rows = self.q("SELECT * FROM import_errors WHERE error=?",
                          ("missing_id",))
        self.assertTrue(mal_rows)
        self.assertTrue(any(
            (r["line_excerpt"] or "") == "state,tool,type" for r in mal_rows))
        # events.detail_json: only per-family allowlisted keys from
        # privacy.py with correctly typed values. The OpenCode families in
        # this fixture (tool_call, tool_result, lifecycle) keep no detail,
        # so titles, messages, error text and linkage ids never persist.
        for row in self.q("SELECT * FROM events"):
            detail_json = row["detail_json"] or ""
            for secret in secrets:
                self.assertNotIn(secret, detail_json)
            if detail_json:
                detail = json.loads(detail_json)
                allowed = privacy.EVENT_DETAIL_ALLOWLIST.get(
                    row["family"], {})
                self.assertTrue(set(detail.keys()) <= set(allowed.keys()),
                                f"unsafe detail keys: {detail}")
                for key in ("title", "error", "message", "output",
                            "content", "arguments", "args", "input",
                            "message_id", "status_code", "hash"):
                    # Never allowlisted for any family the adapter writes;
                    # "skill" stays valid for skill_read and is checked
                    # against the per-family allowlist above.
                    self.assertNotIn(key, detail)
        call = self.q("SELECT * FROM events WHERE family='tool_call'"
                      " AND native_id=?", ("call_priv1",))[0]
        self.assertNotIn(s_tool_title, call["detail_json"] or "")
        self.assertIsNone(call["detail_json"])
        self.assertEqual(call["target"], "echo hi")
        life = self.q("SELECT * FROM events WHERE family='lifecycle'"
                      " AND native_id=?", ("msg_priv_a",))[0]
        self.assertEqual(life["status"], "500")
        self.assertIsNone(life["detail_json"])
        result = self.q("SELECT * FROM events WHERE family='tool_result'"
                        " AND native_id=?", ("call_priv1",))[0]
        self.assertIsNone(result["detail_json"])
        # Rule 7: native session titles never enter the ledger.
        sess = self.q("SELECT * FROM sessions WHERE session_key=?",
                      ("opencode:ses_priv",))[0]
        self.assertNotIn(s_session_title, json.dumps(dict(sess)))

    def test_first_marker_truncates_dotted_and_namespaced_tags(self):
        # Rewritten under the closed ruling: the excerpt ends at the first
        # tag-like marker with no tag parsing, so dotted and namespaced
        # tags truncate the same way as any other marker. Nothing after
        # the first '<custom.tag' is kept.
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
        self.assertEqual(excerpt, "Head keep")
        self.assertNotIn("middle keep", excerpt)
        self.assertNotIn("tail keep", excerpt)
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


    def test_quoted_greater_than_still_truncates_at_first_marker(self):
        # Review finding 1: a quoted '>' inside a tag attribute must not
        # rescue later text into the excerpt. With no tag parsing, the
        # excerpt ends at the earlier '<'.
        secret = "SECRET_QUOTED_ATTR_zzz_qqq"
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_quote", None, "/repo", "Quote", "1.2.3", None,
                        "build", T0 + 940, T0 + 940, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_quote_u", "ses_quote", T0 + 940, T0 + 940,
                        json.dumps({"role": "user",
                                    "time": {"created": T0 + 940}})))
        text = ('Keep this <a title="quoted > inside"> drop all of this '
                + secret)
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_quote_t", "msg_quote_u", "ses_quote",
                        T0 + 940, T0 + 940,
                        json.dumps({"type": "text", "text": text})))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        self._assert_no_secret_anywhere((secret,))
        sub = self.q("SELECT * FROM submissions WHERE native_id=?",
                     ("opencode:msg_quote_u",))[0]
        self.assertEqual(sub["kind"], "genuine")
        self.assertEqual(sub["text_excerpt"], "Keep this")
        self.assertNotIn("drop all of this", sub["text_excerpt"] or "")

    def test_privacy_stale_reimport_corrects_rows_and_replaces_errors(self):
        # Rule 3: a source whose stored privacy version differs is fully
        # re-imported even though its fingerprint is unchanged; existing
        # rows are corrected in place and import_errors are replaced,
        # never duplicated.
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_stale", None, "/repo", "Stale", "1.2.3", None,
                        "build", T0 + 965, T0 + 965, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_stale_u", "ses_stale", T0 + 965, T0 + 965,
                        json.dumps({"role": "user",
                                    "time": {"created": T0 + 965}})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_stale_t", "msg_stale_u", "ses_stale",
                        T0 + 965, T0 + 965,
                        json.dumps({"type": "text",
                                    "text": "Fresh prompt "
                                            "<tag>hidden</tag> tail"})))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_stale_a", "ses_stale", T0 + 966, T0 + 966,
                        _msg("assistant", T0 + 966, T0 + 976,
                             _tokens(0, 0, 0, 0, 0),
                             error={"name": "APIError",
                                    "data": {"statusCode": 429,
                                             "message": "slow down"}})))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        # Poison rows the way a pre-fix import kept them, then mark every
        # source stale. Native fingerprints are untouched, so only the
        # version mismatch may trigger the re-import.
        self.con.execute(
            "UPDATE submissions SET text_excerpt="
            "'leaked <tag>SECRET-STALE-OLD-zzz', text_hash='oldhash'"
            " WHERE native_id='opencode:msg_stale_u'")
        self.con.execute(
            "UPDATE events SET detail_json=? WHERE family='lifecycle'"
            " AND native_id='msg_stale_a'",
            (json.dumps({"error": "APIError",
                         "message": "slow down"}),))
        self.con.execute(
            "UPDATE sources SET privacy_version=0 WHERE harness='opencode'")
        self.con.commit()
        counts_before = {
            table: self.q(f"SELECT COUNT(*) n FROM {table}")[0]["n"]
            for table in ("submissions", "events", "import_errors",
                          "responses", "sources")}
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertEqual(stats["unchanged"], 0)
        for table in counts_before:
            self.assertEqual(
                self.q(f"SELECT COUNT(*) n FROM {table}")[0]["n"],
                counts_before[table], table)
        sub = self.q("SELECT * FROM submissions WHERE native_id=?",
                     ("opencode:msg_stale_u",))[0]
        self.assertEqual(sub["text_excerpt"], "Fresh prompt")
        self.assertNotEqual(sub["text_hash"], "oldhash")
        self.assertNotIn("SECRET-STALE-OLD-zzz",
                         sub["text_excerpt"] or "")
        life = self.q("SELECT * FROM events WHERE family='lifecycle'"
                      " AND native_id=?", ("msg_stale_a",))[0]
        self.assertEqual(life["status"], "429")
        self.assertIsNone(life["detail_json"])
        self._assert_no_secret_anywhere(
            ("SECRET-STALE-OLD-zzz", "slow down", "APIError"))
        versions = {r["privacy_version"] for r in self.q(
            "SELECT privacy_version FROM sources")}
        self.assertEqual(versions, {privacy.PRIVACY_VERSION})

    def test_stale_resync_clears_excerpt_when_no_valid_text_remains(self):
        # A genuine submission whose native text parts all disappear must
        # not keep its old excerpt: the stale re-import clears the row in
        # place instead of returning early.
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_clear", None, "/repo", "Clear", "1.2.3", None,
                        "build", T0 + 975, T0 + 975, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_clear_u", "ses_clear", T0 + 975, T0 + 975,
                        json.dumps({"role": "user",
                                    "time": {"created": T0 + 975}})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_clear_t", "msg_clear_u", "ses_clear",
                        T0 + 975, T0 + 975,
                        json.dumps({"type": "text",
                                    "text": "Visible prompt here"})))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        first = self.q("SELECT * FROM submissions WHERE native_id=?",
                       ("opencode:msg_clear_u",))[0]
        self.assertIn("Visible prompt here", first["text_excerpt"])
        # Replace the text part with a non-text part without touching any
        # timestamp, so the snapshot fingerprint is unchanged and only the
        # privacy version mismatch can trigger the re-import.
        native = sqlite3.connect(self.db_file)
        native.execute("UPDATE part SET data=? WHERE id=?",
                       (json.dumps({"type": "reasoning",
                                    "text": "no longer user text"}),
                        "p_clear_t"))
        native.commit()
        native.close()
        self.con.execute(
            "UPDATE sources SET privacy_version=0 WHERE harness='opencode'")
        self.con.commit()
        opencode.sync(self.con, source=self.db_file)
        cleared = self.q("SELECT * FROM submissions WHERE native_id=?",
                         ("opencode:msg_clear_u",))[0]
        self.assertEqual(
            self.q("SELECT COUNT(*) n FROM submissions WHERE native_id=?",
                   ("opencode:msg_clear_u",))[0]["n"], 1)
        self.assertEqual(cleared["text_excerpt"], "")
        self.assertEqual(cleared["is_genuine"], 0)
        self.assertNotIn("Visible prompt here",
                         cleared["text_excerpt"] or "")

    def test_non_string_targets_and_native_hashes_are_dropped(self):
        # Review finding 4: dicts, lists and other objects are never
        # stringified into event targets, names or detail. Only
        # fixed-format generated fingerprints persist as fingerprints.
        s_target = "SECRET_DICT_TARGET_zzz_qqq"
        s_hash = "SECRET_NATIVE_HASH_zzz_qqq"
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_types", None, "/repo", "Types", "1.2.3", None,
                        "build", T0 + 985, T0 + 985, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_types_a", "ses_types", T0 + 985, T0 + 985,
                        _msg("assistant", T0 + 985, T0 + 995,
                             _tokens(2, 2, 0, 0, 0))))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_types_tool", "msg_types_a", "ses_types",
                        T0 + 986, T0 + 986,
                        json.dumps({"type": "tool", "tool": "bash",
                                    "callID": "call_types1",
                                    "state": {"status": "completed",
                                              "input": {
                                                  "filePath": {
                                                      "path": s_target},
                                                  "command": ["not",
                                                              "a string"]},
                                              "output": "ok",
                                              "time": {"start": T0 + 986,
                                                       "end": T0 + 996}}})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_types_patch", "msg_types_a", "ses_types",
                        T0 + 987, T0 + 987,
                        json.dumps({"type": "patch",
                                    "hash": {"h": s_hash},
                                    "files": [{"p": s_target}, 42]})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_types_name", "msg_types_a", "ses_types",
                        T0 + 988, T0 + 988,
                        json.dumps({"type": "tool",
                                    "tool": {"name": "evil"},
                                    "callID": "call_types2",
                                    "state": {"status": "completed",
                                              "input": {"command": "echo hi"},
                                              "output": "ok",
                                              "time": {"start": T0 + 988,
                                                       "end": T0 + 998}}})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_types_call", "msg_types_a", "ses_types",
                        T0 + 989, T0 + 989,
                        json.dumps({"type": "tool", "tool": "bash",
                                    "callID": {"id": "evil"},
                                    "state": {"status": "completed",
                                              "input": {"command": "echo hi"},
                                              "output": "ok",
                                              "time": {"start": T0 + 989,
                                                       "end": T0 + 999}}})))
        native.commit()
        native.close()
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(stats["malformed"], 1)
        self._assert_no_secret_anywhere((s_target, s_hash))
        for family, native_id in (("tool_call", "call_types1"),
                                  ("tool_result", "call_types1")):
            row = self.q("SELECT * FROM events WHERE family=?"
                         " AND native_id=?", (family, native_id))[0]
            self.assertIsNone(row["target"])
        # A dict file entry and a dict hash leave no target and no detail;
        # only our own generated fingerprint persists.
        patch = self.q("SELECT * FROM events WHERE family='file_change'"
                       " AND native_id=?", ("p_types_patch",))[0]
        self.assertIsNone(patch["target"])
        self.assertIsNone(patch["detail_json"])
        self.assertRegex(patch["fingerprint"] or "", r"\A[0-9a-f]{16}\Z")
        # A non-string tool name falls back to "unknown", never its repr.
        named = self.q("SELECT * FROM events WHERE family='tool_call'"
                       " AND native_id=?", ("call_types2",))[0]
        self.assertEqual(named["name"], "unknown")
        self.assertNotIn("evil", json.dumps(dict(named), default=str))
        # A non-string callID is quarantined, never an event identity.
        errs = self.q("SELECT * FROM import_errors WHERE error=?",
                      ("missing_id",))
        self.assertTrue(any("callID" in (r["line_excerpt"] or "")
                            for r in errs))
        for row in errs:
            self.assertNotIn(s_target, row["line_excerpt"] or "")
        self.assertEqual(self.q("SELECT * FROM events WHERE native_id LIKE"
                                " '%evil%'"), [])

    def test_repeated_full_resync_never_duplicates_import_errors(self):
        # Review finding 5: every quarantine inserts once per record per
        # version. Same-version full re-syncs add no rows, including for
        # NULL-ordinal errors and unlisted categories (fallback).
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_dup_broken", "ses_parent", T0 + 995, T0 + 995,
                        "not json at all"))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_dup_mal", "msg_a1", "ses_parent", T0 + 996,
                        T0 + 996,
                        json.dumps({"type": "tool", "tool": "bash",
                                    "state": {"status": "completed",
                                              "input": {}, "output": "x",
                                              "time": {"start": T0,
                                                       "end": T0 + 1}}})))
        native.execute("UPDATE session SET time_updated=? WHERE id=?",
                       (T0 + 996, "ses_parent"))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        # NULL ordinals dedup through IS-comparison, and anything outside
        # the closed set maps to the fixed fallback.
        opencode._oops(self.con, {"malformed": 0}, "opencode:probe-src",
                       None, "source_unreadable", "")
        opencode._oops(self.con, {"malformed": 0}, "opencode:probe-src",
                       None, "source_unreadable", "")
        opencode._oops(self.con, {"malformed": 0}, "opencode:probe-src",
                       7, "boom: explode", '{"a": 1}')
        self.con.commit()
        probe = self.q("SELECT * FROM import_errors WHERE source_path=?",
                       ("opencode:probe-src",))
        self.assertEqual(len(probe), 2)
        self.assertEqual(
            [r for r in probe if r["ordinal_num"] is None][0]["error"],
            "source_unreadable")
        fallback = [r for r in probe if r["ordinal_num"] == 7][0]
        self.assertEqual(fallback["error"], privacy.ERROR_FALLBACK)
        self.assertEqual(fallback["line_excerpt"], "a")

        def snapshot():
            return sorted(
                ((r["source_path"] or "",
                  r["ordinal_num"]
                  if r["ordinal_num"] is not None else -1,
                  r["error"], r["line_excerpt"])
                 for r in self.q("SELECT * FROM import_errors")))

        before = snapshot()
        self.assertTrue(before)
        opencode.sync(self.con, source=self.db_file, full=True)
        self.assertEqual(snapshot(), before)
        opencode.sync(self.con, source=self.db_file, full=True)
        self.assertEqual(snapshot(), before)

    def test_stale_reimport_malformed_clears_submission_and_event(self):
        # A privacy-stale re-import must reconcile every owned row: when a
        # previously valid native record turns malformed without changing
        # the snapshot fingerprint, old excerpts, hashes, targets and
        # detail must not survive.
        sentinel = "SECRET_RECON_CLEAR_zzz_qqq"
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_recon", None, "/repo", "Recon", "1.2.3", None,
                        "build", T0 + 1000, T0 + 1000, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_recon_u", "ses_recon", T0 + 1000, T0 + 1000,
                        json.dumps({"role": "user",
                                    "time": {"created": T0 + 1000}})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_recon_t", "msg_recon_u", "ses_recon",
                        T0 + 1000, T0 + 1000,
                        json.dumps({"type": "text",
                                    "text": "Valid recon prompt "
                                            + sentinel})))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_recon_a", "ses_recon", T0 + 1001, T0 + 1001,
                        _msg("assistant", T0 + 1001, T0 + 1011,
                             _tokens(2, 2, 0, 0, 0))))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_recon_tool", "msg_recon_a", "ses_recon",
                        T0 + 1001, T0 + 1001,
                        _tool("read", "call_recon1", "completed",
                              {"filePath": "/repo/notes.md"}, "output ok",
                              T0 + 1001, T0 + 1011)))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        first = self.q("SELECT * FROM submissions WHERE native_id=?",
                       ("opencode:msg_recon_u",))[0]
        self.assertEqual(first["kind"], "genuine")
        self.assertIn(sentinel, first["text_excerpt"] or "")
        call_before = self.q("SELECT * FROM events WHERE family='tool_call'"
                             " AND native_id=?", ("call_recon1",))[0]
        self.assertEqual(call_before["target"], "/repo/notes.md")
        # Give the source-owned event a non-NULL old detail holding a
        # test-only sentinel, the way a pre-fix import could have kept
        # unsafe detail. The stale re-import must clear it.
        event_sentinel = "SECRET_RECON_DETAIL_zzz_qqq"
        self.con.execute(
            "UPDATE events SET detail_json=? WHERE family='tool_call'"
            " AND native_id=?",
            (json.dumps({"note": event_sentinel}), "call_recon1"))
        self.con.commit()
        poisoned = self.q("SELECT * FROM events WHERE family='tool_call'"
                          " AND native_id=?", ("call_recon1",))[0]
        self.assertIsNotNone(poisoned["detail_json"])
        self.assertIn(event_sentinel, poisoned["detail_json"] or "")
        # Malform both native records without touching any timestamp or
        # count, so the snapshot fingerprint is unchanged and only the
        # privacy version mismatch can trigger the re-import.
        native = sqlite3.connect(self.db_file)
        native.execute("UPDATE message SET data=? WHERE id=?",
                       ("not json at all", "msg_recon_u"))
        native.execute("UPDATE part SET data=? WHERE id=?",
                       ("not json at all", "p_recon_tool"))
        native.commit()
        native.close()
        self.con.execute(
            "UPDATE sources SET privacy_version=0 WHERE harness='opencode'")
        self.con.commit()
        opencode.sync(self.con, source=self.db_file)
        cleared = self.q("SELECT * FROM submissions WHERE native_id=?",
                         ("opencode:msg_recon_u",))[0]
        self.assertEqual(
            self.q("SELECT COUNT(*) n FROM submissions WHERE native_id=?",
                   ("opencode:msg_recon_u",))[0]["n"], 1)
        self.assertEqual(cleared["text_excerpt"], "")
        self.assertEqual(cleared["kind"], "synthetic")
        self.assertEqual(cleared["is_genuine"], 0)
        self.assertEqual(cleared["text_hash"], text_hash(""))
        self.assertNotIn(sentinel, cleared["text_excerpt"] or "")
        for family in ("tool_call", "tool_result"):
            rows = self.q("SELECT * FROM events WHERE family=?"
                          " AND native_id=?", (family, "call_recon1"))
            self.assertEqual(len(rows), 1, family)
            self.assertIsNone(rows[0]["target"], family)
            self.assertIsNone(rows[0]["detail_json"], family)
        self._assert_no_secret_anywhere((sentinel, event_sentinel))

    def test_stale_unreadable_source_keeps_old_privacy_version(self):
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_unread", None, "/repo", "Unread", "1.2.3", None,
                        "build", T0 + 1010, T0 + 1010, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_unread_u", "ses_unread", T0 + 1010, T0 + 1010,
                        json.dumps({"role": "user",
                                    "time": {"created": T0 + 1010}})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_unread_t", "msg_unread_u", "ses_unread",
                        T0 + 1010, T0 + 1010,
                        json.dumps({"type": "text",
                                    "text": "unreadable probe"})))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        abs_path = os.path.abspath(self.db_file)
        src_path = f"{abs_path}#ses_unread"
        self.con.execute(
            "UPDATE sources SET privacy_version=0 WHERE harness='opencode'")
        self.con.commit()
        # Make the source partially readable: messages no longer fetch.
        native = sqlite3.connect(self.db_file)
        native.execute("DROP TABLE message")
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        row = self.q("SELECT * FROM sources WHERE harness=? AND path=?",
                     ("opencode", src_path))[0]
        self.assertEqual(row["privacy_version"], 0)
        errs = self.q("SELECT * FROM import_errors WHERE source_path=?",
                      (src_path,))
        self.assertTrue(any(r["error"] == "source_unreadable" for r in errs))

    def test_string_synthetic_flag_is_treated_as_synthetic(self):
        # Only a native JSON boolean counts for provenance: the string
        # "false" must fail closed as synthetic with no excerpt.
        secret = "SECRET_STRING_SYNTH_zzz_qqq"
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_strsyn", None, "/repo", "StrSyn", "1.2.3", None,
                        "build", T0 + 1020, T0 + 1020, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_strsyn_u", "ses_strsyn", T0 + 1020, T0 + 1020,
                        json.dumps({"role": "user",
                                    "time": {"created": T0 + 1020}})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_strsyn_t", "msg_strsyn_u", "ses_strsyn",
                        T0 + 1020, T0 + 1020,
                        json.dumps({"type": "text", "text": secret,
                                    "synthetic": "false"})))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        sub = self.q("SELECT * FROM submissions WHERE native_id=?",
                     ("opencode:msg_strsyn_u",))[0]
        self.assertEqual(sub["kind"], "synthetic")
        self.assertEqual(sub["is_genuine"], 0)
        self.assertEqual(sub["text_excerpt"], "")
        self.assertNotIn(secret, sub["text_excerpt"] or "")
        self._assert_no_secret_anywhere((secret,))

    def test_free_text_tool_name_and_skill_title_never_persist(self):
        # Review P1: every event write passes name/status through the
        # shared privacy filters. Free-text tool names and skill titles
        # must never survive in any event column.
        free_tool = "My Cool Tool!!! With Spaces SECRET_FREE_TOOL_zzz_qqq"
        skill_title = "skill title SECRET_SKILL_TITLE_zzz_qqq"
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_free", None, "/repo", "Free", "1.2.3", None,
                        "build", T0 + 1100, T0 + 1100, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_free_a", "ses_free", T0 + 1100, T0 + 1100,
                        _msg("assistant", T0 + 1100, T0 + 1110,
                             _tokens(2, 2, 0, 0, 0))))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_free_tool", "msg_free_a", "ses_free",
                        T0 + 1101, T0 + 1101,
                        _tool(free_tool, "call_free1", "completed",
                              {"command": "echo hi"}, "ok",
                              T0 + 1101, T0 + 1111)))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_free_skill", "msg_free_a", "ses_free",
                        T0 + 1102, T0 + 1102,
                        _tool("skill", "call_skillfree1", "completed",
                              {"name": skill_title}, "ok",
                              T0 + 1102, T0 + 1112,
                              metadata={"dir": "/tmp/skills/my-skill"})))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        self._assert_no_secret_anywhere(
            ("SECRET_FREE_TOOL_zzz_qqq", "SECRET_SKILL_TITLE_zzz_qqq",
             "My Cool Tool", "skill title"))
        for family, native_id in (("tool_call", "call_free1"),
                                  ("tool_result", "call_free1"),
                                  ("skill_invoke", "call_skillfree1")):
            rows = self.q("SELECT * FROM events WHERE family=?"
                          " AND native_id=?", (family, native_id))
            self.assertEqual(len(rows), 1, f"{family}/{native_id}")
            row = rows[0]
            blob = json.dumps(dict(row), default=str)
            self.assertNotIn("SECRET_FREE_TOOL_zzz_qqq", blob)
            self.assertNotIn("SECRET_SKILL_TITLE_zzz_qqq", blob)
            self.assertNotIn("My Cool Tool", blob)
            # The skill title carries a space so it cannot be an
            # identifier: it must not survive in name, target or detail.
            if native_id == "call_skillfree1":
                self.assertIsNone(row["name"])
                self.assertIsNone(row["target"])
                self.assertIsNone(row["detail_json"])
            else:
                self.assertIsNone(row["name"])
        # The free-text tool produced no read/skill/file rows either.
        self.assertEqual(self.q("SELECT * FROM events WHERE native_id=?",
                                ("call_free1",))[0]["name"], None)

    def test_stale_row_with_unsafe_name_and_status_corrected(self):
        # Review P1: a privacy-stale re-import clears or recomputes name
        # and status as well as target and detail; valid rows are
        # repopulated in place rather than left stale or blank.
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_stalename", None, "/repo", "StaleName", "1.2.3",
                        None, "build", T0 + 1120, T0 + 1120, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_stalename_a", "ses_stalename", T0 + 1120,
                        T0 + 1120,
                        _msg("assistant", T0 + 1120, T0 + 1130,
                             _tokens(2, 2, 0, 0, 0))))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_stalename_tool", "msg_stalename_a", "ses_stalename",
                        T0 + 1121, T0 + 1121,
                        _tool("bash", "call_stalename1", "completed",
                              {"command": "echo hi"}, "ok",
                              T0 + 1121, T0 + 1131)))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        call_before = self.q("SELECT * FROM events WHERE family='tool_call'"
                             " AND native_id=?", ("call_stalename1",))[0]
        self.assertEqual(call_before["name"], "bash")
        res_before = self.q("SELECT * FROM events WHERE family='tool_result'"
                            " AND native_id=?", ("call_stalename1",))[0]
        self.assertEqual(res_before["name"], "bash")
        self.assertEqual(res_before["status"], "ok")
        # Poison rows the way a pre-fix import kept them: unsafe free-text
        # names and statuses that bypassed shared validation.
        poison_name = "Evil Free Text Title SECRET_POISON_NAME_zzz_qqq"
        poison_status = "totally broken SECRET_POISON_STATUS_zzz_qqq"
        self.con.execute(
            "UPDATE events SET name=?, status=?, target=?, detail_json=?"
            " WHERE family='tool_call' AND native_id=?",
            (poison_name, poison_status, "/tmp/poison",
             json.dumps({"note": "poison"}), "call_stalename1"))
        self.con.execute(
            "UPDATE events SET name=?, status=? WHERE family='tool_result'"
            " AND native_id=?",
            (poison_name, poison_status, "call_stalename1"))
        self.con.execute(
            "UPDATE sources SET privacy_version=0 WHERE harness='opencode'")
        self.con.commit()
        counts_before = {
            table: self.q(f"SELECT COUNT(*) n FROM {table}")[0]["n"]
            for table in ("events", "responses", "sources")}
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertEqual(stats["unchanged"], 0)
        for table in counts_before:
            self.assertEqual(
                self.q(f"SELECT COUNT(*) n FROM {table}")[0]["n"],
                counts_before[table], table)
        call_after = self.q("SELECT * FROM events WHERE family='tool_call'"
                            " AND native_id=?", ("call_stalename1",))[0]
        res_after = self.q("SELECT * FROM events WHERE family='tool_result'"
                           " AND native_id=?", ("call_stalename1",))[0]
        # Valid identifier rows are repopulated in place, not left blank.
        self.assertEqual(call_after["name"], "bash")
        self.assertIsNone(call_after["status"])
        self.assertEqual(call_after["target"], "echo hi")
        self.assertIsNone(call_after["detail_json"])
        self.assertEqual(res_after["name"], "bash")
        self.assertEqual(res_after["status"], "ok")
        self.assertIsNone(res_after["detail_json"])
        self._assert_no_secret_anywhere(
            ("SECRET_POISON_NAME_zzz_qqq", "SECRET_POISON_STATUS_zzz_qqq",
             "Evil Free Text Title", "totally broken"))

    def test_list_dict_type_and_status_quarantined_as_schema_error(self):
        # Review P2: non-string or unhashable type/status values must not
        # raise TypeError in a set membership test and abort the import.
        # They are quarantined as schema_error; later records still import.
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_badshape", None, "/repo", "BadShape", "1.2.3",
                        None, "build", T0 + 1140, T0 + 1140, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_badshape_a", "ses_badshape", T0 + 1140,
                        T0 + 1140,
                        _msg("assistant", T0 + 1140, T0 + 1150,
                             _tokens(2, 2, 0, 0, 0))))
        # List and dict part types: unhashable, must not abort.
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_badtype_list", "msg_badshape_a", "ses_badshape",
                        T0 + 1141, T0 + 1141,
                        json.dumps({"type": ["tool"], "tool": "bash",
                                    "callID": "call_badtype_list",
                                    "state": {"status": "completed",
                                              "input": {"command": "echo hi"},
                                              "output": "ok",
                                              "time": {"start": T0 + 1141,
                                                       "end": T0 + 1151}}})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_badtype_dict", "msg_badshape_a", "ses_badshape",
                        T0 + 1142, T0 + 1142,
                        json.dumps({"type": {"t": "tool"}, "tool": "bash",
                                    "callID": "call_badtype_dict",
                                    "state": {"status": "completed",
                                              "input": {"command": "echo hi"},
                                              "output": "ok",
                                              "time": {"start": T0 + 1142,
                                                       "end": T0 + 1152}}})))
        # Valid tool names with list/dict statuses.
        for pid, call, bad_status in (
                ("p_badstatus_list", "call_badstatus_list", ["completed"]),
                ("p_badstatus_dict", "call_badstatus_dict",
                 {"s": "completed"})):
            native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                           (pid, "msg_badshape_a", "ses_badshape",
                            T0 + 1143, T0 + 1143,
                            json.dumps({"type": "tool", "tool": "bash",
                                        "callID": call,
                                        "state": {"status": bad_status,
                                                  "input": {"command":
                                                            "echo hi"},
                                                  "output": "ok",
                                                  "time": {
                                                      "start": T0 + 1143,
                                                      "end": T0 + 1153}}})))
        # A later valid record in the same session must still import.
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_valid_after_shape", "msg_badshape_a",
                        "ses_badshape", T0 + 1144, T0 + 1144,
                        _tool("bash", "call_shape_valid", "completed",
                              {"command": "echo hi"}, "ok",
                              T0 + 1144, T0 + 1154)))
        native.commit()
        native.close()
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(stats["malformed"], 4)
        errs = self.q("SELECT * FROM import_errors WHERE error=?",
                      ("schema_error",))
        self.assertGreaterEqual(len(errs), 4)
        for row in errs:
            self.assertEqual(row["error"], "schema_error")
            self.assertIn(row["error"], opencode.IMPORT_ERROR_CATEGORIES)
            self.assertLessEqual(len(row["line_excerpt"] or ""), 200)
        # Malformed part types produce no events under their call ids.
        self.assertEqual(self.q("SELECT * FROM events WHERE native_id=?",
                                ("call_badtype_list",)), [])
        self.assertEqual(self.q("SELECT * FROM events WHERE native_id=?",
                                ("call_badtype_dict",)), [])
        # Malformed statuses produce no tool_result, but never abort the
        # later valid record.
        for call in ("call_badstatus_list", "call_badstatus_dict"):
            self.assertEqual(self.q("SELECT * FROM events WHERE family=?"
                                    " AND native_id=?",
                                    ("tool_result", call)), [])
        valid_call = self.q("SELECT * FROM events WHERE family='tool_call'"
                            " AND native_id=?", ("call_shape_valid",))
        self.assertEqual(len(valid_call), 1)
        self.assertEqual(valid_call[0]["name"], "bash")
        valid_res = self.q("SELECT * FROM events WHERE family='tool_result'"
                            " AND native_id=?", ("call_shape_valid",))
        self.assertEqual(len(valid_res), 1)
        self.assertEqual(valid_res[0]["status"], "ok")

    def test_multi_file_patch_preserves_every_path(self):
        # Review P2: a patch part with several files must keep every valid
        # path in detail={"paths": [...]}, so reread invalidation and
        # test-edit detection see each changed file, not just files[0].
        from agent_observer import analysis
        s_hash = "SECRET_MULTI_HASH_zzz_qqq"
        first = "/repo/a.py"
        second = "/repo/tests/test_a.py"
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_multipatch", None, "/repo", "Multi", "1.2.3",
                        None, "build", T0 + 1200, T0 + 1200, None))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_multipatch_a", "ses_multipatch",
                        T0 + 1200, T0 + 1200,
                        _msg("assistant", T0 + 1200, T0 + 1210,
                             _tokens(2, 2, 0, 0, 0))))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                       ("p_multi_patch", "msg_multipatch_a",
                        "ses_multipatch", T0 + 1201, T0 + 1201,
                        json.dumps({"type": "patch", "hash": s_hash,
                                    "files": [first, 42, {"p": "evil"},
                                              second]})))
        native.commit()
        native.close()
        opencode.sync(self.con, source=self.db_file)
        row = self.q("SELECT * FROM events WHERE family='file_change'"
                     " AND native_id=?", ("p_multi_patch",))[0]
        # The target stays the first path; the native hash and invalid
        # path values never persist.
        self.assertEqual(row["target"], first)
        detail = json.loads(row["detail_json"] or "{}")
        self.assertEqual(detail.get("paths"), [first, second])
        self.assertNotIn(s_hash, row["detail_json"] or "")
        self.assertNotIn("evil", row["detail_json"] or "")
        self._assert_no_secret_anywhere((s_hash,))
        # The secondary path reaches the downstream analysis path set.
        change = dict(row)
        self.assertIn(second, analysis._changed_paths(change))
        # A reread of the secondary path after the patch is explained by
        # the patch; with only the first-path target (the old bug) the
        # same reread would be flagged as a repeated read.
        session = {"session_key": "opencode:ses_multipatch",
                   "harness": "opencode", "project_dir": "/repo",
                   "agentsmd_version": None}
        read1 = {"family": "read", "target": second, "ts": 1,
                 "detail_json": None, "id": 101}
        read2 = {"family": "read", "target": second, "ts": 3,
                 "detail_json": None, "id": 103}
        explained = dict(change, ts=2, id=102)
        self.assertEqual(analysis._repeated_reads(
            session, [read1, explained, read2]), [])
        target_only = dict(change, ts=2, id=102, target=first,
                           detail_json=None)
        flagged = analysis._repeated_reads(
            session, [read1, target_only, read2])
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["detector"], "repeated_read")

    def test_malformed_token_counter_quarantined_without_response(self):
        # Review P2: a present malformed counter quarantines the record
        # under malformed_usage before any response insert; the import
        # continues to later valid records.
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_maluse", None, "/repo", "MalUse", "1.2.3",
                        None, "build", T0 + 1230, T0 + 1230, None))
        bad_tokens = {"input": "bad", "output": 5, "reasoning": 0,
                      "cache": {"read": 0, "write": 0}}
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_maluse_bad", "ses_maluse", T0 + 1230, T0 + 1230,
                        _msg("assistant", T0 + 1230, T0 + 1240,
                             bad_tokens)))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_maluse_good", "ses_maluse", T0 + 1250, T0 + 1250,
                        _msg("assistant", T0 + 1250, T0 + 1260,
                             _tokens(3, 4, 0, 0, 0))))
        native.commit()
        native.close()
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(stats["malformed"], 1)
        errs = self.q("SELECT * FROM import_errors WHERE error=?",
                      ("malformed_usage",))
        self.assertTrue(errs)
        for err_row in errs:
            self.assertIn(err_row["error"],
                          opencode.IMPORT_ERROR_CATEGORIES)
            excerpt = err_row["line_excerpt"] or ""
            self.assertLessEqual(len(excerpt), 200)
            # Structure only: sorted top-level key names, no values.
            self.assertNotIn("bad", excerpt)
            self.assertEqual(excerpt, ",".join(sorted(excerpt.split(","))))
        self.assertTrue(any(
            (r["line_excerpt"] or "")
            == "cost,finish,modelID,providerID,role,time,tokens,variant"
            for r in errs))
        # No response for the malformed message; the later valid
        # response in the same session still imports.
        self.assertEqual(self.q("SELECT * FROM responses WHERE response_id=?",
                                ("opencode:msg_maluse_bad",)), [])
        good = self.q("SELECT * FROM responses WHERE response_id=?",
                      ("opencode:msg_maluse_good",))
        self.assertEqual(len(good), 1)
        self.assertEqual(good[0]["input_tokens"], 3)
        self.assertEqual(good[0]["output_tokens"], 4)
        self.assertEqual(good[0]["total_tokens"], 7)

    def test_explicit_null_token_counter_quarantined(self):
        # Coordinator repair: key presence decides. A missing counter key
        # stays unknown, but an explicitly present null is malformed and
        # quarantines the record before any response insert.
        native = sqlite3.connect(self.db_file)
        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("ses_nulluse", None, "/repo", "NullUse", "1.2.3",
                        None, "build", T0 + 1270, T0 + 1270, None))
        null_tokens = {"input": None, "output": 5, "reasoning": 0,
                       "cache": {"read": 0, "write": 0}}
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_nulluse_bad", "ses_nulluse",
                        T0 + 1270, T0 + 1270,
                        _msg("assistant", T0 + 1270, T0 + 1280,
                             null_tokens)))
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                       ("msg_nulluse_good", "ses_nulluse",
                        T0 + 1290, T0 + 1290,
                        _msg("assistant", T0 + 1290, T0 + 1300,
                             _tokens(3, 4, 0, 0, 0))))
        native.commit()
        native.close()
        stats = opencode.sync(self.con, source=self.db_file)
        self.assertGreaterEqual(stats["malformed"], 1)
        errs = self.q("SELECT * FROM import_errors WHERE error=?",
                      ("malformed_usage",))
        self.assertTrue(errs)
        self.assertTrue(any(
            (r["line_excerpt"] or "")
            == "cost,finish,modelID,providerID,role,time,tokens,variant"
            for r in errs))
        # No response for the explicitly-null message; a missing key
        # would stay unknown, but presence with null must not insert.
        self.assertEqual(self.q("SELECT * FROM responses WHERE response_id=?",
                                ("opencode:msg_nulluse_bad",)), [])
        good = self.q("SELECT * FROM responses WHERE response_id=?",
                      ("opencode:msg_nulluse_good",))
        self.assertEqual(len(good), 1)
        self.assertEqual(good[0]["total_tokens"], 7)


if __name__ == "__main__":
    unittest.main()
