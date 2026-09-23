"""Grok Build adapter: per-prompt usage, synthetic prompts, tool joins,
skill reads, permissions, lifecycle, idempotent re-sync and growing logs."""

import hashlib
import json
import os
import shutil

from agent_observer import report
from agent_observer.adapters import grok
from tests.helpers import FIXTURES, LedgerCase

ROOT = os.path.join(FIXTURES, "grok")
S1 = "grok:01fixture1-aaaa-4b5c-8d6e-000000000001"
S2 = "grok:01fixture2-bbbb-4b5c-8d6e-000000000002"


class GrokAdapterTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.stats = grok.sync(self.con, root=ROOT)

    def test_usage_is_one_row_per_prompt_with_native_semantics(self):
        self.assertEqual(self.stats["sources"], 2)
        rows = self.query(
            "SELECT response_id, input_tokens, cached_input_tokens,"
            " cache_write_input_tokens, output_tokens,"
            " reasoning_output_tokens, total_tokens, model, effort, semantics"
            " FROM responses WHERE session_key=? ORDER BY response_id", (S1,))
        self.assertEqual([r["response_id"] for r in rows],
                         [f"{S1}:p-aaa", f"{S1}:p-bbb", f"{S1}:p-ccc",
                          f"{S1}:p-ddd"])
        first = rows[0]
        self.assertEqual(
            (first["input_tokens"], first["cached_input_tokens"],
             first["cache_write_input_tokens"], first["output_tokens"],
             first["reasoning_output_tokens"], first["total_tokens"]),
            (1000, 800, 50, 200, 60, 1200))
        # The harness total is kept as reported, never recomputed.
        self.assertEqual(first["total_tokens"], 1200)
        self.assertEqual(first["semantics"],
                         "grok:input_includes_cached,output_includes_reasoning")
        # Model comes from the turn_started stream, effort from summary.
        self.assertEqual(first["model"], "grok-4.6")
        self.assertEqual(first["effort"], "medium")
        # usage.json session totals are cross-check only: never imported.
        totals = [r["total_tokens"] for r in rows]
        self.assertNotIn(1099998, totals)
        self.assertEqual(report.scope_totals(self.con, {S1})["total_tokens"],
                         1200 + 600 + 330)

    def test_completion_without_usage_keeps_null_counters(self):
        row = self.query(
            "SELECT input_tokens, cached_input_tokens,"
            " cache_write_input_tokens, output_tokens,"
            " reasoning_output_tokens, total_tokens, model FROM responses"
            " WHERE response_id=?", (f"{S1}:p-ddd",))[0]
        self.assertEqual(
            (row["input_tokens"], row["cached_input_tokens"],
             row["cache_write_input_tokens"], row["output_tokens"],
             row["reasoning_output_tokens"], row["total_tokens"]),
            (None, None, None, None, None, None))
        self.assertEqual(row["model"], "grok-4.6")

    def test_session_project_branch_and_identity(self):
        row = self.query("SELECT * FROM sessions WHERE session_key=?", (S1,))[0]
        self.assertEqual(row["project_dir"], "/redacted/repo")
        self.assertEqual(row["git_branch"], "main")
        self.assertEqual(row["instructions_sha256"], "fixture-grok-instructions")
        self.assertEqual(row["preferences_sha256"], "fixture-grok-preferences")
        self.assertEqual(row["direction_status"], "ready")
        self.assertNotIn("Implement the widget", row["identity_json"])
        # The <user_rule> body resolves through the release map.
        body = "INSTALLED BODY"
        digest = hashlib.sha256((body + "\n").encode()).hexdigest()
        self.con.execute(
            "INSERT INTO agentsmd_versions(sha256, version) VALUES(?, ?)",
            (digest, "9.9.9"))
        self.con.commit()
        grok.sync(self.con, root=ROOT, full=True)
        row = self.query("SELECT * FROM sessions WHERE session_key=?", (S1,))[0]
        self.assertEqual(row["instructions_sha256"], "fixture-grok-instructions")

    def test_submissions_genuine_synthetic_and_open(self):
        kinds = {r["native_id"]: (r["kind"], r["turn_id"]) for r in self.query(
            "SELECT native_id, kind, turn_id FROM submissions"
            " WHERE session_key=?", (S1,))}
        self.assertEqual(kinds[f"{S1}:prompt:0"], ("genuine", f"{S1}:p-aaa"))
        self.assertEqual(kinds[f"{S1}:prompt:1"], ("genuine", f"{S1}:p-bbb"))
        self.assertEqual(kinds[f"{S1}:prompt:2"], ("synthetic", f"{S1}:p-ccc"))
        self.assertEqual(kinds[f"{S1}:prompt:3"], ("genuine", f"{S1}:p-ddd"))
        # Prompt 4 has chunks but no completion yet: kept, unbound.
        self.assertEqual(kinds[f"{S1}:prompt:4"], ("genuine", None))
        genuine = self.query(
            "SELECT is_genuine FROM submissions WHERE native_id=?",
            (f"{S1}:prompt:2",))[0]
        self.assertEqual(genuine["is_genuine"], 0)

    def test_tool_call_joins_result_and_skill_read(self):
        events = self.query(
            "SELECT family, native_id, name, target, status, duration_ms,"
            " detail_json FROM events WHERE session_key=? ORDER BY id", (S1,))
        by = {}
        for e in events:
            by.setdefault(e["family"], []).append(e)
        calls = {e["native_id"] for e in by["tool_call"]}
        self.assertIn("call-read-1", calls)
        call = [e for e in by["tool_call"]
                if e["native_id"] == "call-read-1"][0]
        self.assertEqual(call["name"], "read_file")
        self.assertEqual(call["target"], "/redacted/repo/skills/ops/SKILL.md")
        self.assertNotIn("target_file", call["detail_json"])
        results = {e["native_id"]: e for e in by["tool_result"]}
        self.assertIn("call-read-1", results)
        # tool_completed duration enriches the update-stream result in place.
        self.assertEqual(results["call-read-1"]["status"], "ok")
        self.assertEqual(results["call-read-1"]["duration_ms"], 12)
        self.assertEqual(by["skill_read"][0]["target"],
                         "/redacted/repo/skills/ops/SKILL.md")
        self.assertEqual(by["skill_read"][0]["name"], "ops")
        self.assertNotIn("read", by)

    def test_permission_decision_and_skipped_noise(self):
        perms = self.query(
            "SELECT name, status, duration_ms FROM events"
            " WHERE session_key=? AND family='permission'", (S1,))
        self.assertEqual(len(perms), 2)
        resolved = [p for p in perms if p["status"] == "allow"]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["name"], "read_file")
        self.assertEqual(resolved[0]["duration_ms"], 0)
        names = {e["name"] for e in self.query(
            "SELECT name FROM events WHERE session_key=?", (S1,))}
        self.assertNotIn("phase_changed", names)
        lifecycles = {e["name"] for e in self.query(
            "SELECT name FROM events WHERE session_key=? AND family='lifecycle'",
            (S1,))}
        self.assertIn("turn_started", lifecycles)
        self.assertIn("turn_ended", lifecycles)
        compactions = self.query(
            "SELECT name FROM events WHERE session_key=?"
            " AND family='compaction'", (S1,))
        self.assertEqual([c["name"] for c in compactions],
                         ["auto_compact_started"])
        kinds = {e["name"] for e in self.query(
            "SELECT name FROM events WHERE session_key=? AND family='lifecycle'"
            " AND name IN ('retry_state', 'subagent_spawned')", (S1,))}
        self.assertEqual(kinds, {"retry_state", "subagent_spawned"})

    def test_subagent_child_links_to_parent(self):
        row = self.query("SELECT * FROM sessions WHERE session_key=?", (S2,))[0]
        self.assertEqual(row["parent_session_key"], S1)
        self.assertEqual(row["role"], "subagent")
        nulls = self.query(
            "SELECT total_tokens FROM responses WHERE session_key=?", (S2,))[0]
        self.assertIsNone(nulls["total_tokens"])

    def test_malformed_line_is_quarantined(self):
        self.assertEqual(self.stats["malformed"], 1)
        errors = self.query(
            "SELECT error, line_excerpt FROM import_errors WHERE harness='grok'")
        self.assertEqual(len(errors), 1)
        self.assertLessEqual(len(errors[0]["line_excerpt"]), 200)

    def test_resync_is_idempotent(self):
        again = grok.sync(self.con, root=ROOT)
        self.assertEqual(again["unchanged"], 2)
        self.assertEqual(again["responses_inserted"], 0)
        self.assertEqual(again["submissions_inserted"], 0)
        self.assertEqual(again["events_inserted"], 0)
        full = grok.sync(self.con, root=ROOT, full=True)
        self.assertEqual(full["responses_inserted"], 0)
        self.assertEqual(report.scope_totals(self.con)["responses"], 5)

    def test_growing_file_finalizes_the_open_prompt(self):
        tmp = os.path.join(self.tmp.name, "grow")
        shutil.copytree(ROOT, tmp)
        grok.sync(self.con, root=tmp)
        sid = S1
        before = self.query(
            "SELECT COUNT(*) n FROM responses WHERE session_key=?", (sid,))[0]
        self.assertEqual(before["n"], 4)
        completion = {"timestamp": 1788800100,
                      "method": "_x.ai/session/update",
                      "params": {"sessionId": sid.split(":", 1)[1],
                                 "update": {"sessionUpdate": "turn_completed",
                                            "prompt_id": "p-eee",
                                            "stop_reason": "end_turn",
                                            "usage": {"inputTokens": 700,
                                                      "outputTokens": 70,
                                                      "totalTokens": 770,
                                                      "cachedReadTokens": 600,
                                                      "cacheCreationTokens": 0,
                                                      "reasoningTokens": 7,
                                                      "modelUsage": {}}},
                                 "_meta": {"eventId": "grow-1"}}}
        path = os.path.join(
            tmp, "%2Fredacted%2Frepo",
            "01fixture1-aaaa-4b5c-8d6e-000000000001", "updates.jsonl")
        with open(path, "a") as fh:
            fh.write(json.dumps(completion) + "\n")
        grown = grok.sync(self.con, root=tmp)
        self.assertEqual(grown["responses_inserted"], 1)
        self.assertEqual(grown["submissions_inserted"], 0)
        row = self.query(
            "SELECT total_tokens FROM responses WHERE response_id=?",
            (f"{sid}:p-eee",))[0]
        self.assertEqual(row["total_tokens"], 770)
        bound = self.query(
            "SELECT turn_id FROM submissions WHERE native_id=?",
            (f"{sid}:prompt:4",))[0]
        self.assertEqual(bound["turn_id"], f"{sid}:p-eee")
        repeat = grok.sync(self.con, root=tmp)
        self.assertEqual(repeat["responses_inserted"], 0)
        self.assertEqual(repeat["unchanged"], 2)

    def test_conflicting_usage_is_quarantined_not_duplicated(self):
        tmp = os.path.join(self.tmp.name, "conflict")
        shutil.copytree(ROOT, tmp)
        grok.sync(self.con, root=tmp)
        path = os.path.join(
            tmp, "%2Fredacted%2Frepo",
            "01fixture1-aaaa-4b5c-8d6e-000000000001", "updates.jsonl")
        with open(path) as fh:
            text = fh.read()
        text = text.replace('"totalTokens": 1200', '"totalTokens": 1201')
        with open(path, "w") as fh:
            fh.write(text)
        # A full re-read meets the rewritten counters under the same key.
        result = grok.sync(self.con, root=tmp, full=True)
        self.assertEqual(result["responses_inserted"], 0)
        rows = self.query(
            "SELECT total_tokens FROM responses WHERE response_id=?",
            (f"{S1}:p-aaa",))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_tokens"], 1200)
        conflicts = self.query(
            "SELECT * FROM import_errors WHERE error='conflicting_usage'")
        self.assertEqual(len(conflicts), 1)
        # Fixed category only, safe shape only, no record values, bounded.
        err = conflicts[0]
        self.assertEqual(err["error"], "conflicting_usage")
        self.assertLessEqual(len(err["line_excerpt"] or ""), 200)
        self.assertNotIn("1201", err["line_excerpt"] or "")
        self.assertNotIn("1201", err["error"] or "")
        self.assertNotIn("p-aaa", err["error"] or "")
        self.assertNotIn("p-aaa", err["line_excerpt"] or "")
        self.assertIn("method=", err["line_excerpt"] or "")
        self.assertIn("update=", err["line_excerpt"] or "")
        self.assertIn("keys=", err["line_excerpt"] or "")

    # --- Requirement-level privacy and excerpt tests ---

    def _isolated_con(self, name):
        from agent_observer import db as _db
        path = os.path.join(self.tmp.name, f"{name}.db")
        con = _db.connect(path)
        _db.init_db(con)
        return con

    def _all_text_values(self, con):
        tables = [r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")]
        found = []
        for table in tables:
            cols = con.execute(f"PRAGMA table_info({table})").fetchall()
            text_cols = [c["name"] for c in cols if c["type"] == "TEXT"]
            for col in text_cols:
                for row in con.execute(
                        f'SELECT "{col}" v FROM "{table}" WHERE "{col}"'
                        " IS NOT NULL"):
                    found.append((table, col, row["v"] or ""))
        return found

    def test_privacy_no_injected_or_secret_in_any_text_column(self):
        from agent_observer import db as _db
        tmp = os.path.join(self.tmp.name, "privacy")
        shutil.copytree(ROOT, tmp)
        # Inject secret-looking and preference text inside injected wrappers
        # plus a direction block, on a new prompt that must never leak.
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        secret_prompt = {
            "timestamp": 1788800050,
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {
                        "type": "text",
                        "text": "<user_query>\nPrivacy probe human text\n</user_query>\n"
                                "<user_rule>INSTALLED BODY SECRET-TOKEN-abc123"
                                " sk-fake-secret-12345</user_rule>\n"
                                "<<<AGENTSMD_PROJECT_DIRECTION_V1>>>\n"
                                "{\"status\":\"ready\"}\n"
                                "<<<END_AGENTSMD_PROJECT_DIRECTION_V1>>>",
                    },
                    "_meta": {"modelId": "grok-4.6", "promptIndex": 9},
                },
                "_meta": {"eventId": "privacy-1", "promptId": "p-priv"},
            },
        }
        secret_completion = {
            "timestamp": 1788800051,
            "method": "_x.ai/session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "turn_completed",
                    "prompt_id": "p-priv",
                    "stop_reason": "end_turn",
                    "usage": {"inputTokens": 10, "outputTokens": 1,
                              "totalTokens": 11},
                },
                "_meta": {"eventId": "privacy-2"},
            },
        }
        bad_line = {
            "timestamp": 1788800052,
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {
                        "type": "text",
                        "text": "malformed probe INSTALLED BODY"
                                " sk-fake-secret-12345",
                    },
                },
            },
        }
        # bad_line lacks promptIndex on purpose to force a quarantined shape.
        with open(os.path.join(sdir, "updates.jsonl"), "a") as fh:
            fh.write(json.dumps(secret_prompt) + "\n")
            fh.write(json.dumps(secret_completion) + "\n")
            fh.write(json.dumps(bad_line) + "\n")
        con = self._isolated_con("privacy")
        grok.sync(con, root=tmp)
        texts = self._all_text_values(con)
        self.assertTrue(texts)
        for table, col, val in texts:
            self.assertNotIn("INSTALLED BODY", val,
                             f"{table}.{col} leaks preference")
            self.assertNotIn("sk-fake-secret-12345", val,
                             f"{table}.{col} leaks secret")
            self.assertNotIn("SECRET-TOKEN-abc123", val,
                             f"{table}.{col} leaks secret")
            self.assertNotIn("AGENTSMD_PROJECT_DIRECTION_V1", val,
                             f"{table}.{col} leaks direction block")
            self.assertNotIn("<user_rule>", val,
                             f"{table}.{col} leaks wrapper")
        # import_errors specifically must hold only shapes, never raw lines.
        for row in con.execute("SELECT error, line_excerpt FROM import_errors"):
            for field in (row["error"] or "", row["line_excerpt"] or ""):
                self.assertNotIn("INSTALLED BODY", field)
                self.assertNotIn("sk-fake-secret", field)
                self.assertNotIn("Privacy probe", field)
            self.assertLessEqual(len(row["line_excerpt"] or ""), 200)
        con.close()

    def test_excerpt_sanitized_length_and_empty_for_non_genuine(self):
        rows = {r["native_id"]: r for r in self.query(
            "SELECT native_id, kind, is_genuine, text_excerpt FROM submissions"
            " WHERE session_key=?", (S1,))}
        # Genuine prompts keep at most 300 chars of human text, sanitized.
        for idx in ("0", "1", "3", "4"):
            nid = f"{S1}:prompt:{idx}"
            row = rows[nid]
            self.assertEqual(row["kind"], "genuine")
            self.assertEqual(row["is_genuine"], 1)
            excerpt = row["text_excerpt"] or ""
            self.assertLessEqual(len(excerpt), 300)
            self.assertNotIn("<user_query>", excerpt)
            self.assertNotIn("<user_rule>", excerpt)
            self.assertNotIn("INSTALLED BODY", excerpt)
            self.assertNotIn("AGENTSMD_PROJECT_DIRECTION_V1", excerpt)
        self.assertIn("Implement the widget", rows[f"{S1}:prompt:0"]["text_excerpt"])
        self.assertIn("Now the second prompt",
                      rows[f"{S1}:prompt:1"]["text_excerpt"])
        # Synthetic prompts have empty excerpts.
        synth = rows[f"{S1}:prompt:2"]
        self.assertEqual(synth["kind"], "synthetic")
        self.assertEqual(synth["is_genuine"], 0)
        self.assertEqual(synth["text_excerpt"], "")

    def test_duplicate_turn_completed_keeps_prompt_id_bindings(self):
        tmp = os.path.join(self.tmp.name, "dupid")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        path = os.path.join(tmp, "%2Fredacted%2Frepo", sid, "updates.jsonl")
        with open(path) as fh:
            lines = fh.read().splitlines()
        # Duplicate the first completion (p-aaa) at the end with new eventId.
        first = None
        for line in lines:
            if '"prompt_id": "p-aaa"' in line or '"prompt_id":"p-aaa"' in line:
                first = line
                break
        self.assertIsNotNone(first)
        dup = json.loads(first)
        dup["params"]["_meta"]["eventId"] = "dup-p-aaa"
        with open(path, "a") as fh:
            fh.write(json.dumps(dup) + "\n")
        con = self._isolated_con("dupid")
        grok.sync(con, root=tmp)
        subs = {r["native_id"]: r["turn_id"] for r in con.execute(
            "SELECT native_id, turn_id FROM submissions"
            " WHERE session_key=?", (S1,))}
        self.assertEqual(subs[f"{S1}:prompt:0"], f"{S1}:p-aaa")
        self.assertEqual(subs[f"{S1}:prompt:1"], f"{S1}:p-bbb")
        self.assertEqual(subs[f"{S1}:prompt:2"], f"{S1}:p-ccc")
        self.assertEqual(subs[f"{S1}:prompt:3"], f"{S1}:p-ddd")
        # Open prompt stays unbound; duplicate never shifts bindings.
        self.assertIsNone(subs[f"{S1}:prompt:4"])
        resps = list(con.execute(
            "SELECT response_id FROM responses WHERE session_key=?"
            " ORDER BY response_id", (S1,)))
        self.assertEqual([r["response_id"] for r in resps],
                         [f"{S1}:p-aaa", f"{S1}:p-bbb", f"{S1}:p-ccc",
                          f"{S1}:p-ddd"])
        con.close()

    def test_late_usage_fills_null_counters(self):
        tmp = os.path.join(self.tmp.name, "lateusage")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        con = self._isolated_con("lateusage")
        grok.sync(con, root=tmp)
        row = con.execute(
            "SELECT input_tokens, total_tokens FROM responses"
            " WHERE response_id=?", (f"{S1}:p-ddd",)).fetchone()
        self.assertIsNone(row["input_tokens"])
        self.assertIsNone(row["total_tokens"])
        late = {
            "timestamp": 1788800060,
            "method": "_x.ai/session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "turn_completed",
                    "prompt_id": "p-ddd",
                    "stop_reason": "end_turn",
                    "usage": {"inputTokens": 400, "outputTokens": 40,
                              "totalTokens": 440, "cachedReadTokens": 300,
                              "cacheCreationTokens": 5, "reasoningTokens": 4,
                              "modelUsage": {}},
                },
                "_meta": {"eventId": "late-p-ddd"},
            },
        }
        with open(os.path.join(sdir, "updates.jsonl"), "a") as fh:
            fh.write(json.dumps(late) + "\n")
        second = grok.sync(con, root=tmp)
        self.assertEqual(second["responses_inserted"], 0)
        filled = con.execute(
            "SELECT input_tokens, cached_input_tokens,"
            " cache_write_input_tokens, output_tokens,"
            " reasoning_output_tokens, total_tokens FROM responses"
            " WHERE response_id=?", (f"{S1}:p-ddd",)).fetchone()
        self.assertEqual(
            (filled["input_tokens"], filled["cached_input_tokens"],
             filled["cache_write_input_tokens"], filled["output_tokens"],
             filled["reasoning_output_tokens"], filled["total_tokens"]),
            (400, 300, 5, 40, 4, 440))
        # No duplicate row and no conflict for a NULL->value fill.
        n = con.execute(
            "SELECT COUNT(*) n FROM responses WHERE response_id=?",
            (f"{S1}:p-ddd",)).fetchone()["n"]
        self.assertEqual(n, 1)
        conflicts = list(con.execute(
            "SELECT * FROM import_errors WHERE error='conflicting_usage'"))
        self.assertEqual(len(conflicts), 0)
        con.close()

    def test_unsupported_method_quarantined_with_safe_shape(self):
        tmp = os.path.join(self.tmp.name, "badmethod")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        before_subs = self.query(
            "SELECT COUNT(*) n FROM submissions WHERE session_key=?",
            (S1,))[0]["n"]
        probe_text = ("should never persist sk-fake-secret-999"
                      " INSTALLED BODY probe")
        bad = {
            "timestamp": 1788800070,
            "method": "session/unknown",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": probe_text},
                    "_meta": {"promptIndex": 99},
                },
                "_meta": {"eventId": "bad-method-1"},
            },
        }
        with open(os.path.join(sdir, "updates.jsonl"), "a") as fh:
            fh.write(json.dumps(bad) + "\n")
        con = self._isolated_con("badmethod")
        grok.sync(con, root=tmp)
        # No submission/response/event for the unsupported method payload.
        self.assertEqual(
            con.execute(
                "SELECT COUNT(*) n FROM submissions WHERE native_id=?",
                (f"{S1}:prompt:99",)).fetchone()["n"], 0)
        for table, col, val in self._all_text_values(con):
            self.assertNotIn("sk-fake-secret-999", val,
                             f"{table}.{col} leaks unsupported payload")
            self.assertNotIn("should never persist", val,
                             f"{table}.{col} leaks unsupported payload")
        errors = list(con.execute("SELECT error, line_excerpt FROM import_errors"))
        self.assertTrue(errors)
        for row in errors:
            self.assertLessEqual(len(row["line_excerpt"] or ""), 200)
            self.assertNotIn("sk-fake-secret-999", row["line_excerpt"] or "")
            self.assertNotIn("should never persist", row["line_excerpt"] or "")
            self.assertNotIn("sk-fake-secret-999", row["error"] or "")
            self.assertNotIn("session/unknown", row["error"] or "")
            self.assertNotIn("session/unknown", row["line_excerpt"] or "")
        # Unknown method value never appears anywhere; error is fixed only.
        for table, col, val in self._all_text_values(con):
            self.assertNotIn("session/unknown", val,
                             f"{table}.{col} leaks unknown method value")
        self.assertTrue(any((r["error"] or "") == "unknown_method"
                            for r in errors))
        # Fixed categories only, never exception text or record values.
        allowed = {"malformed_json", "schema_error", "unknown_method",
                   "unknown_update", "missing_prompt_index",
                   "missing_prompt_id", "conflicting_prompt",
                   "conflicting_usage", "unknown_event"}
        for row in errors:
            self.assertIn(row["error"], allowed)
        con.close()

    def _copy_session_without_chat(self, dest_root, sid):
        src = os.path.join(
            ROOT, "%2Fredacted%2Frepo", sid)
        dst = os.path.join(dest_root, "%2Fredacted%2Frepo", sid)
        os.makedirs(dst, exist_ok=True)
        for name in ("updates.jsonl", "events.jsonl", "summary.json"):
            shutil.copy(os.path.join(src, name), os.path.join(dst, name))

    def test_missing_chat_then_valid_updates_row(self):
        tmp = os.path.join(self.tmp.name, "missingchat")
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        self._copy_session_without_chat(tmp, sid)
        con = self._isolated_con("missingchat")
        grok.sync(con, root=tmp)
        rows = {r["native_id"]: r for r in con.execute(
            "SELECT native_id, kind, is_genuine, text_excerpt, turn_id"
            " FROM submissions")}
        # Without chat evidence every prompt stays provisional/non-genuine.
        self.assertTrue(rows)
        for nid, row in rows.items():
            self.assertEqual(row["is_genuine"], 0)
            self.assertEqual(row["text_excerpt"], "")
            self.assertIn(row["kind"], ("unknown", "synthetic"))
        n_before = len(rows)
        # Later arrival of complete chat metadata updates rows in place.
        shutil.copy(
            os.path.join(ROOT, "%2Fredacted%2Frepo", sid, "chat_history.jsonl"),
            os.path.join(tmp, "%2Fredacted%2Frepo", sid, "chat_history.jsonl"))
        second = grok.sync(con, root=tmp)
        after = {r["native_id"]: r for r in con.execute(
            "SELECT native_id, kind, is_genuine, text_excerpt, turn_id"
            " FROM submissions")}
        self.assertEqual(len(after), n_before)
        self.assertEqual(after[f"{S1}:prompt:0"]["kind"], "genuine")
        self.assertEqual(after[f"{S1}:prompt:0"]["is_genuine"], 1)
        self.assertIn("Implement the widget",
                      after[f"{S1}:prompt:0"]["text_excerpt"])
        self.assertEqual(after[f"{S1}:prompt:0"]["turn_id"], f"{S1}:p-aaa")
        self.assertEqual(after[f"{S1}:prompt:2"]["kind"], "synthetic")
        self.assertEqual(after[f"{S1}:prompt:2"]["text_excerpt"], "")
        con.close()

    def test_malformed_chat_then_valid_updates_row(self):
        tmp = os.path.join(self.tmp.name, "badchat")
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        self._copy_session_without_chat(tmp, sid)
        dst_chat = os.path.join(tmp, "%2Fredacted%2Frepo", sid,
                                "chat_history.jsonl")
        # Malformed JSON plus an unterminated trailing line: unreliable.
        with open(dst_chat, "w") as fh:
            fh.write('{"type": "user", "prompt_index": 0}\n')
            fh.write('{not valid json\n')
            fh.write('{"type": "user", "content": "trailing without newline",'
                     ' "prompt_index": 1}')
        con = self._isolated_con("badchat")
        grok.sync(con, root=tmp)
        rows = {r["native_id"]: r for r in con.execute(
            "SELECT native_id, kind, is_genuine, text_excerpt FROM submissions")}
        for row in rows.values():
            self.assertEqual(row["is_genuine"], 0)
            self.assertEqual(row["text_excerpt"], "")
        n_before = len(rows)
        # Valid metadata later reclassifies the same rows without duplicates.
        shutil.copy(
            os.path.join(ROOT, "%2Fredacted%2Frepo", sid, "chat_history.jsonl"),
            dst_chat)
        grok.sync(con, root=tmp)
        after = {r["native_id"]: r for r in con.execute(
            "SELECT native_id, kind, is_genuine, text_excerpt, turn_id"
            " FROM submissions")}
        self.assertEqual(len(after), n_before)
        self.assertEqual(after[f"{S1}:prompt:1"]["kind"], "genuine")
        self.assertIn("Now the second prompt",
                      after[f"{S1}:prompt:1"]["text_excerpt"])
        con.close()

    def test_unterminated_blocks_of_each_kind_leak_nothing(self):
        tmp = os.path.join(self.tmp.name, "unterm")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        cases = [
            (20, "p-unterm-dir",
             "Human prefix DIR-HUMAN\n"
             "<<<AGENTSMD_PROJECT_DIRECTION_V1>>>\n"
             "UNTERM-DIRECTION-SENTINEL-aaa {\"status\":\"ready\"} trailing",
             "DIR-HUMAN", "UNTERM-DIRECTION-SENTINEL-aaa"),
            (21, "p-unterm-rule",
             "Human prefix RULE-HUMAN\n"
             "<user_rule>UNTERM-USERRULE-SENTINEL-bbb secret tail",
             "RULE-HUMAN", "UNTERM-USERRULE-SENTINEL-bbb"),
            (22, "p-unterm-instr",
             "Human prefix INSTR-HUMAN\n"
             "<INSTRUCTIONS>\nUNTERM-INSTRUCTIONS-SENTINEL-ccc secret tail",
             "INSTR-HUMAN", "UNTERM-INSTRUCTIONS-SENTINEL-ccc"),
            (23, "p-unterm-gen",
             "Human prefix GEN-HUMAN\n"
             "<<<UNTERM-GENERIC-SENTINEL-ddd injected tail",
             "GEN-HUMAN", "UNTERM-GENERIC-SENTINEL-ddd"),
        ]
        with open(os.path.join(sdir, "updates.jsonl"), "a") as fh:
            for idx, pid, text, _human, _sent in cases:
                fh.write(json.dumps({
                    "timestamp": 1788800200 + idx,
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "user_message_chunk",
                            "content": {"type": "text", "text": text},
                            "_meta": {"modelId": "grok-4.6",
                                      "promptIndex": idx},
                        },
                        "_meta": {"eventId": f"unterm-{idx}",
                                  "promptId": pid},
                    },
                }) + "\n")
                fh.write(json.dumps({
                    "timestamp": 1788800210 + idx,
                    "method": "_x.ai/session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {"sessionUpdate": "turn_completed",
                                   "prompt_id": pid,
                                   "stop_reason": "end_turn",
                                   "usage": {"inputTokens": 10,
                                             "outputTokens": 1,
                                             "totalTokens": 11}},
                        "_meta": {"eventId": f"unterm-c-{idx}"},
                    },
                }) + "\n")
        with open(os.path.join(sdir, "chat_history.jsonl"), "a") as fh:
            for idx, _pid, text, _human, _sent in cases:
                fh.write(json.dumps({
                    "type": "user",
                    "content": [{"type": "text", "text": text}],
                    "prompt_index": idx,
                }) + "\n")
        con = self._isolated_con("unterm")
        grok.sync(con, root=tmp)
        texts = self._all_text_values(con)
        self.assertTrue(texts)
        for _idx, _pid, _text, human, sentinel in cases:
            for table, col, val in texts:
                self.assertNotIn(sentinel, val,
                                 f"{table}.{col} leaks unterminated block")
            row = con.execute(
                "SELECT kind, is_genuine, text_excerpt FROM submissions"
                " WHERE native_id=?", (f"{S1}:prompt:{_idx}",)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["kind"], "genuine")
            self.assertEqual(row["is_genuine"], 1)
            self.assertIn(human, row["text_excerpt"] or "")
            self.assertNotIn(sentinel, row["text_excerpt"] or "")
            self.assertNotIn("<user_rule>", row["text_excerpt"] or "")
            self.assertNotIn("<INSTRUCTIONS>", row["text_excerpt"] or "")
            self.assertNotIn("<<<", row["text_excerpt"] or "")
        con.close()

    def test_secret_output_in_event_never_persists(self):
        tmp = os.path.join(self.tmp.name, "secrevent")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        secret = "SECRET-EVENT-SENTINEL-xyz sk-fake-secret-event-777"
        known_extra = {
            "ts": "2026-09-01T10:00:09Z",
            "type": "tool_completed",
            "tool_name": "read_file",
            "tool_call_id": "call-read-1",
            "duration_ms": 5,
            "outcome": "success",
            "output": secret,
            "content": secret,
            "message": secret,
        }
        unknown_evt = {
            "ts": "2026-09-01T10:00:10Z",
            "type": "mystery_harness_event",
            "output": secret,
            "detail": secret,
        }
        with open(os.path.join(sdir, "events.jsonl"), "a") as fh:
            fh.write(json.dumps(known_extra) + "\n")
            fh.write(json.dumps(unknown_evt) + "\n")
        con = self._isolated_con("secrevent")
        grok.sync(con, root=tmp)
        for table, col, val in self._all_text_values(con):
            self.assertNotIn(secret, val,
                             f"{table}.{col} leaks event free text")
            self.assertNotIn("sk-fake-secret-event-777", val,
                             f"{table}.{col} leaks event secret")
            self.assertNotIn("mystery_harness_event", val,
                             f"{table}.{col} leaks unknown event value")
        unknowns = list(con.execute(
            "SELECT error, line_excerpt FROM import_errors"
            " WHERE error='unknown_event'"))
        self.assertTrue(unknowns)
        for row in unknowns:
            self.assertNotIn(secret, row["line_excerpt"] or "")
            self.assertNotIn("mystery_harness_event",
                             row["line_excerpt"] or "")
            self.assertNotIn(secret, row["error"] or "")
        con.close()

    def test_late_chat_with_growing_events_reclassifies(self):
        tmp = os.path.join(self.tmp.name, "latechatgrow")
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        self._copy_session_without_chat(tmp, sid)
        con = self._isolated_con("latechatgrow")
        grok.sync(con, root=tmp)
        before = {r["native_id"]: r for r in con.execute(
            "SELECT native_id, kind, is_genuine, text_excerpt, turn_id"
            " FROM submissions")}
        self.assertTrue(before)
        for row in before.values():
            self.assertEqual(row["is_genuine"], 0)
            self.assertEqual(row["text_excerpt"], "")
        n_before = len(before)
        # Events grow while chat metadata arrives late.
        with open(os.path.join(
                tmp, "%2Fredacted%2Frepo", sid, "events.jsonl"), "a") as fh:
            fh.write(json.dumps({
                "ts": "2026-09-01T10:00:09Z",
                "type": "turn_ended",
                "outcome": "completed",
            }) + "\n")
        shutil.copy(
            os.path.join(ROOT, "%2Fredacted%2Frepo", sid, "chat_history.jsonl"),
            os.path.join(tmp, "%2Fredacted%2Frepo", sid, "chat_history.jsonl"))
        grok.sync(con, root=tmp)
        after = {r["native_id"]: r for r in con.execute(
            "SELECT native_id, kind, is_genuine, text_excerpt, turn_id"
            " FROM submissions")}
        self.assertEqual(len(after), n_before)
        self.assertEqual(after[f"{S1}:prompt:0"]["kind"], "genuine")
        self.assertEqual(after[f"{S1}:prompt:0"]["is_genuine"], 1)
        self.assertIn("Implement the widget",
                      after[f"{S1}:prompt:0"]["text_excerpt"])
        self.assertEqual(after[f"{S1}:prompt:0"]["turn_id"], f"{S1}:p-aaa")
        sess = con.execute(
            "SELECT instructions_sha256, preferences_sha256,"
            " direction_status FROM sessions WHERE session_key=?",
            (S1,)).fetchone()
        self.assertEqual(sess["instructions_sha256"],
                         "fixture-grok-instructions")
        self.assertEqual(sess["preferences_sha256"],
                         "fixture-grok-preferences")
        self.assertEqual(sess["direction_status"], "ready")
        con.close()
