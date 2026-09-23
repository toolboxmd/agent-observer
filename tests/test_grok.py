"""Grok Build adapter: per-prompt usage, synthetic prompts, tool joins,
skill reads, permissions, lifecycle, idempotent re-sync and growing logs."""

import hashlib
import json
import os
import shutil

from agent_observer import privacy, report
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
        # Rule 6: tool_call keeps no detail; raw argument keys never persist.
        self.assertIsNone(call["detail_json"])
        for row in (call,):
            blob = " ".join(v or "" for v in
                            (row["name"], row["target"], row["detail_json"]))
            self.assertNotIn("target_file", blob)
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
            "SELECT * FROM import_errors WHERE error='usage_conflict'")
        self.assertEqual(len(conflicts), 1)
        # Closed category only with a key-only excerpt: sorted top-level key
        # names, never counters, ids or other record values, bounded.
        err = conflicts[0]
        self.assertEqual(err["error"], "usage_conflict")
        self.assertEqual(err["line_excerpt"], "method,params,timestamp")
        self.assertLessEqual(len(err["line_excerpt"] or ""), 200)
        self.assertNotIn("1201", err["line_excerpt"] or "")
        self.assertNotIn("1201", err["error"] or "")
        self.assertNotIn("p-aaa", err["error"] or "")
        self.assertNotIn("p-aaa", err["line_excerpt"] or "")

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

    def test_excerpt_first_marker_truncation_and_empty_for_non_genuine(self):
        rows = {r["native_id"]: r for r in self.query(
            "SELECT native_id, kind, is_genuine, text_excerpt FROM submissions"
            " WHERE session_key=?", (S1,))}
        # Spec rule 1: the fixture prompts start with a tag-like marker, so
        # genuine human text keeps nothing after it. Classification still
        # distinguishes genuine from synthetic; excerpts stay bounded and
        # marker-free.
        for idx in ("0", "1", "3", "4"):
            nid = f"{S1}:prompt:{idx}"
            row = rows[nid]
            self.assertEqual(row["kind"], "genuine")
            self.assertEqual(row["is_genuine"], 1)
            excerpt = row["text_excerpt"] or ""
            self.assertEqual(excerpt, "")
            self.assertLessEqual(len(excerpt), 300)
            self.assertNotIn("<user_query>", excerpt)
            self.assertNotIn("<user_rule>", excerpt)
            self.assertNotIn("INSTALLED BODY", excerpt)
            self.assertNotIn("AGENTSMD_PROJECT_DIRECTION_V1", excerpt)
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
            "SELECT * FROM import_errors WHERE error='usage_conflict'"))
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
        # Unknown record values never appear anywhere; the error is the
        # closed unknown_record category with a key-only line excerpt.
        for table, col, val in self._all_text_values(con):
            self.assertNotIn("session/unknown", val,
                             f"{table}.{col} leaks unknown method value")
        self.assertTrue(any((r["error"] or "") == "unknown_record"
                            for r in errors))
        unknowns = [r for r in errors if r["error"] == "unknown_record"]
        self.assertTrue(unknowns)
        for row in unknowns:
            self.assertEqual(row["line_excerpt"], "method,params,timestamp")
        # Closed categories only, never exception text or record values.
        allowed = set(privacy.ERROR_CATEGORIES) | {privacy.ERROR_FALLBACK}
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
        # The fixture prompt starts with a tag-like marker, so the genuine
        # excerpt is empty under first-marker truncation.
        self.assertEqual(after[f"{S1}:prompt:0"]["text_excerpt"], "")
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
        self.assertEqual(after[f"{S1}:prompt:1"]["is_genuine"], 1)
        # The fixture prompt starts with a tag-like marker, so the genuine
        # excerpt is empty under first-marker truncation.
        self.assertEqual(after[f"{S1}:prompt:1"]["text_excerpt"], "")
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
            " WHERE error='unknown_record'"))
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
        # The fixture prompt starts with a tag-like marker, so the genuine
        # excerpt is empty under first-marker truncation.
        self.assertEqual(after[f"{S1}:prompt:0"]["text_excerpt"], "")
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

    def test_malformed_tool_and_location_shapes_never_persist(self):
        tmp = os.path.join(self.tmp.name, "evilshape")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        tool_secret = "SECRET-TOOL-SENTINEL-a1b2c3 sk-fake-secret-tool-111"
        meta_secret = "SECRET-META-SENTINEL-d4e5f6"
        target_secret = "SECRET-TARGET-SENTINEL-777"
        loc_secret = "SECRET-LOC-SENTINEL-eee sk-fake-secret-loc-222"
        loc_msg = "SECRET-LOCMSG-fff output payload free text"
        callid_secret = "SECRET-CALLID-SENTINEL-999"
        evil_tool = {
            "timestamp": 1788800300,
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call-evil-1",
                    "title": {"message": tool_secret},
                    "rawInput": {"target_file": {"message": target_secret}},
                    "_meta": {"x.ai/tool": {
                        "name": {"nested": meta_secret},
                        "kind": ["SECRET-KIND-ddd"]}},
                },
                "_meta": {"eventId": "evil-1", "promptId": "p-aaa"},
            },
        }
        evil_read = {
            "timestamp": 1788800301,
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-evil-1",
                    "kind": "read",
                    "locations": [
                        {"path": {"message": loc_secret},
                         "message": loc_msg},
                        {"path": "/redacted/repo/ok.txt",
                         "message": "SECRET-LOCBAD-ggg should not persist",
                         "output": "SECRET-LOCFAIL-hhh"},
                    ],
                },
                "_meta": {"eventId": "evil-2", "promptId": "p-aaa"},
            },
        }
        evil_callid = {
            "timestamp": 1788800302,
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": {"id": callid_secret},
                    "title": "read_file",
                    "rawInput": {"target_file": "/redacted/repo/ok.txt"},
                },
                "_meta": {"eventId": "evil-3", "promptId": "p-aaa"},
            },
        }
        evil_event = {
            "ts": "2026-09-01T10:00:11Z",
            "type": "tool_started",
            "tool_name": {"message": tool_secret},
        }
        with open(os.path.join(sdir, "updates.jsonl"), "a") as fh:
            fh.write(json.dumps(evil_tool) + "\n")
            fh.write(json.dumps(evil_read) + "\n")
            fh.write(json.dumps(evil_callid) + "\n")
        with open(os.path.join(sdir, "events.jsonl"), "a") as fh:
            fh.write(json.dumps(evil_event) + "\n")
        con = self._isolated_con("evilshape")
        grok.sync(con, root=tmp)
        sentinels = (tool_secret, "sk-fake-secret-tool-111", meta_secret,
                     target_secret, loc_secret, "sk-fake-secret-loc-222",
                     loc_msg, callid_secret, "SECRET-KIND-ddd",
                     "SECRET-LOCBAD-ggg", "SECRET-LOCFAIL-hhh")
        texts = self._all_text_values(con)
        self.assertTrue(texts)
        for table, col, val in texts:
            for sentinel in sentinels:
                self.assertNotIn(sentinel, val,
                                 f"{table}.{col} leaks malformed shape")
        # The non-string tool name must not survive as an event name,
        # target, fingerprint, or detail value; the read locations must
        # not survive as paths, names, targets, or detail either.
        for row in con.execute(
                "SELECT name, target, fingerprint, detail_json FROM events"
                " WHERE session_key=?", (S1,)):
            blob = " ".join(v or "" for v in
                            (row["name"], row["target"], row["fingerprint"],
                             row["detail_json"]))
            for sentinel in sentinels:
                self.assertNotIn(sentinel, blob,
                                 "events row leaks malformed shape")
        # Valid fixture behavior is preserved.
        call = con.execute(
            "SELECT name, target FROM events WHERE session_key=?"
            " AND family='tool_call' AND native_id='call-read-1'",
            (S1,)).fetchone()
        self.assertIsNotNone(call)
        self.assertEqual(call["name"], "read_file")
        self.assertEqual(call["target"],
                         "/redacted/repo/skills/ops/SKILL.md")
        # Any quarantine uses only a closed category and a key-only excerpt.
        allowed = set(privacy.ERROR_CATEGORIES) | {privacy.ERROR_FALLBACK}
        errors = list(con.execute(
            "SELECT error, line_excerpt FROM import_errors"))
        for row in errors:
            self.assertIn(row["error"], allowed)
            self.assertLessEqual(len(row["line_excerpt"] or ""), 200)
            for sentinel in sentinels:
                self.assertNotIn(sentinel, row["line_excerpt"] or "")
                self.assertNotIn(sentinel, row["error"] or "")
            self.assertNotIn("target_file", row["line_excerpt"] or "")
        con.close()

    def test_growing_updates_does_not_duplicate_replay_errors(self):
        tmp = os.path.join(self.tmp.name, "replaydedup")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        updates_path = os.path.join(sdir, "updates.jsonl")
        malformed = {
            "timestamp": 1788800400,
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "dedup probe"},
                },
            },
        }
        with open(updates_path, "a") as fh:
            fh.write(json.dumps(malformed) + "\n")
        con = self._isolated_con("replaydedup")
        grok.sync(con, root=tmp)
        first = list(con.execute(
            "SELECT source_path, ordinal_num, error FROM import_errors"
            " WHERE error='missing_id'"))
        self.assertEqual(len(first), 1)
        first_path = first[0]["source_path"]
        first_ordinal = first[0]["ordinal_num"]
        n_errors_first = con.execute(
            "SELECT COUNT(*) n FROM import_errors").fetchone()["n"]
        # Grow the same file with a valid completion for the open prompt.
        completion = {"timestamp": 1788800401,
                      "method": "_x.ai/session/update",
                      "params": {"sessionId": sid,
                                 "update": {"sessionUpdate": "turn_completed",
                                            "prompt_id": "p-eee",
                                            "stop_reason": "end_turn",
                                            "usage": {"inputTokens": 700,
                                                      "outputTokens": 70,
                                                      "totalTokens": 770}},
                                 "_meta": {"eventId": "dedup-grow-1"}}}
        with open(updates_path, "a") as fh:
            fh.write(json.dumps(completion) + "\n")
        second = grok.sync(con, root=tmp)
        self.assertEqual(second["responses_inserted"], 1)
        again = list(con.execute(
            "SELECT source_path, ordinal_num, error FROM import_errors"
            " WHERE error='missing_id'"))
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["source_path"], first_path)
        self.assertEqual(again[0]["ordinal_num"], first_ordinal)
        self.assertEqual(
            con.execute(
                "SELECT COUNT(*) n FROM import_errors").fetchone()["n"],
            n_errors_first)
        # A genuinely new malformed ordinal still gets its own row.
        malformed2 = dict(malformed)
        malformed2["timestamp"] = 1788800402
        with open(updates_path, "a") as fh:
            fh.write(json.dumps(malformed2) + "\n")
        grok.sync(con, root=tmp)
        final = list(con.execute(
            "SELECT ordinal_num FROM import_errors"
            " WHERE error='missing_id' ORDER BY ordinal_num"))
        self.assertEqual(len(final), 2)
        self.assertNotEqual(final[0]["ordinal_num"], final[1]["ordinal_num"])
        con.close()

    def test_token_shaped_free_text_in_enum_fields_never_persists(self):
        # Token-shaped strings (no whitespace) pass a lexical check but are
        # not closed-enum values, so they must be dropped from every event
        # detail and status column.
        tmp = os.path.join(self.tmp.name, "tokenenum")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        evil_outcome = "EVIL-OUTCOME-abc123"
        evil_decision = "EVIL-DECISION-abc123"
        evil_type = "EVIL-TYPE-abc123"
        evil_err = "EVIL-ERR-abc123"
        evil_rel = "EVIL-REL-abc123"
        evil_status = "EVIL-STATUS-abc123"
        retry = {
            "timestamp": 1788800500,
            "method": "_x.ai/session/update",
            "params": {
                "sessionId": sid,
                "update": {"sessionUpdate": "retry_state",
                           "type": evil_type, "error_type": evil_err},
                "_meta": {"eventId": "token-retry-1"},
            },
        }
        with open(os.path.join(sdir, "updates.jsonl"), "a") as fh:
            fh.write(json.dumps(retry) + "\n")
        with open(os.path.join(sdir, "events.jsonl"), "a") as fh:
            fh.write(json.dumps({
                "ts": "2026-09-01T10:00:11Z", "type": "turn_ended",
                "outcome": evil_outcome}) + "\n")
            fh.write(json.dumps({
                "ts": "2026-09-01T10:00:12Z", "type": "permission_resolved",
                "tool_name": "read_file", "decision": evil_decision,
                "wait_ms": 3}) + "\n")
            fh.write(json.dumps({
                "ts": "2026-09-01T10:00:13Z", "type": "turn_started",
                "turn_number": 9, "model_id": "grok-4.6",
                "session_relationship": evil_rel}) + "\n")
            fh.write(json.dumps({
                "ts": "2026-09-01T10:00:14Z", "type": "tool_completed",
                "tool_name": "read_file", "tool_call_id": "call-evil-enum-1",
                "duration_ms": 9, "outcome": evil_outcome}) + "\n")
            fh.write(json.dumps({
                "ts": "2026-09-01T10:00:15Z", "type": "tool_started",
                "tool_name": "read_file"}) + "\n")
        con = self._isolated_con("tokenenum")
        grok.sync(con, root=tmp)
        sentinels = (evil_outcome, evil_decision, evil_type, evil_err,
                     evil_rel, evil_status)
        for table, col, val in self._all_text_values(con):
            for sentinel in sentinels:
                self.assertNotIn(sentinel, val,
                                 f"{table}.{col} leaks enum free text")
        # The evil outcome/decision survive in no status column either.
        for row in con.execute(
                "SELECT status, detail_json FROM events"
                " WHERE session_key=?", (S1,)):
            blob = (row["status"] or "") + (row["detail_json"] or "")
            for sentinel in sentinels:
                self.assertNotIn(sentinel, blob,
                                 "events row leaks enum free text")
        # Valid statuses still persist in ledger columns while rule 6 keeps
        # lifecycle and permission detail empty: only the per-family
        # allowlist survives, so outcomes, decisions, durations and model
        # metadata live in status/duration columns or not at all.
        ended = [r for r in con.execute(
            "SELECT status, detail_json FROM events WHERE session_key=?"
            " AND family='lifecycle' AND name='turn_ended'", (S1,))]
        self.assertTrue(any(r["status"] == "completed" for r in ended))
        for r in ended:
            self.assertIsNone(r["detail_json"])
        resolved = [r for r in con.execute(
            "SELECT status, duration_ms, detail_json FROM events"
            " WHERE session_key=? AND family='permission'"
            " AND status='allow'", (S1,))]
        self.assertEqual(len(resolved), 1)
        self.assertIsNone(resolved[0]["detail_json"])
        waited = [r for r in con.execute(
            "SELECT status, duration_ms, detail_json FROM events"
            " WHERE session_key=? AND family='permission'"
            " AND duration_ms=3", (S1,))]
        self.assertEqual(len(waited), 1)
        self.assertIsNone(waited[0]["status"])
        self.assertIsNone(waited[0]["detail_json"])
        started = list(con.execute(
            "SELECT detail_json FROM events WHERE session_key=?"
            " AND family='lifecycle' AND name='turn_started'", (S1,)))
        self.assertTrue(len(started) >= 2)
        for r in started:
            self.assertIsNone(r["detail_json"])
        retry_rows = list(con.execute(
            "SELECT detail_json FROM events WHERE session_key=?"
            " AND family='lifecycle' AND name='retry_state'", (S1,)))
        self.assertEqual(len(retry_rows), 2)
        for r in retry_rows:
            self.assertIsNone(r["detail_json"])
        con.close()

    def test_legacy_malformed_detail_is_scrubbed_and_persisted(self):
        # A tool_result row written before privacy.py rule 6 keeps unknown
        # keys, enum free text and arbitrary nested contents until a
        # tool_completed enrichment arrives. The rule-6 filter must drop
        # everything the tool_result allowlist does not name (it names only
        # exit codes, so tool/outcome/paths/lines go too) and persist the
        # correction even when neither duration nor status changes.
        tmp = os.path.join(self.tmp.name, "legacydetail")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        con = self._isolated_con("legacydetail")
        grok.sync(con, root=tmp)
        dirty = {"tool": "read_file", "outcome": "success",
                 "message": "EVIL-LEGACY-MSG-abc123",
                 "mystery_key": "EVIL-LEGACY-KEY-abc123",
                 "status": "EVIL-LEGACY-STATUS-abc123",
                 "paths": ["/ok/path", {"nested": "EVIL-LEGACY-NEST-abc123"},
                           123],
                 "lines": {"/ok/path": "notanint", "/other": 7}}
        con.execute(
            "INSERT INTO events(source_id, session_key, ordinal_num, ts,"
            " family, native_id, name, status, duration_ms, detail_json)"
            " VALUES(NULL, ?, 999, 1788800600.0, 'tool_result',"
            " 'call-legacy-1', 'read_file', 'ok', 5, ?)",
            (S1, json.dumps(dirty, sort_keys=True)))
        con.commit()
        with open(os.path.join(sdir, "events.jsonl"), "a") as fh:
            fh.write(json.dumps({
                "ts": "2026-09-01T10:00:16Z", "type": "tool_completed",
                "tool_name": "read_file", "tool_call_id": "call-legacy-1",
                "duration_ms": 5, "outcome": "success"}) + "\n")
        grok.sync(con, root=tmp)
        row = con.execute(
            "SELECT status, duration_ms, detail_json FROM events"
            " WHERE session_key=? AND family='tool_result'"
            " AND native_id='call-legacy-1'", (S1,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["duration_ms"], 5)
        for sentinel in ("EVIL-LEGACY-MSG-abc123", "EVIL-LEGACY-KEY-abc123",
                         "EVIL-LEGACY-STATUS-abc123",
                         "EVIL-LEGACY-NEST-abc123"):
            self.assertNotIn(sentinel, row["detail_json"] or "")
            for table, col, val in self._all_text_values(con):
                self.assertNotIn(sentinel, val,
                                 f"{table}.{col} leaks legacy detail")
        # Rule 6 keeps no tool_result detail here: the scrubbed payload is
        # empty, persisted as NULL like a fresh insert.
        self.assertIsNone(row["detail_json"])
        con.close()

    # --- Privacy-spec regression coverage (planner ruling 2026-09-23) ---

    def _append_prompt(self, sdir, sid, idx, pid, text, chat_text=None):
        with open(os.path.join(sdir, "updates.jsonl"), "a") as fh:
            fh.write(json.dumps({
                "timestamp": 1788801000 + idx,
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": text},
                        "_meta": {"modelId": "grok-4.6",
                                  "promptIndex": idx},
                    },
                    "_meta": {"eventId": f"reg-{idx}",
                              "promptId": pid},
                },
            }) + "\n")
            fh.write(json.dumps({
                "timestamp": 1788801010 + idx,
                "method": "_x.ai/session/update",
                "params": {
                    "sessionId": sid,
                    "update": {"sessionUpdate": "turn_completed",
                               "prompt_id": pid,
                               "stop_reason": "end_turn",
                               "usage": {"inputTokens": 10,
                                         "outputTokens": 1,
                                         "totalTokens": 11}},
                    "_meta": {"eventId": f"reg-c-{idx}"},
                },
            }) + "\n")
        with open(os.path.join(sdir, "chat_history.jsonl"), "a") as fh:
            fh.write(json.dumps({
                "type": "user",
                "content": [{"type": "text",
                             "text": chat_text if chat_text is not None
                             else text}],
                "prompt_index": idx,
            }) + "\n")

    def test_genuine_excerpt_truncates_first_marker_collapses_whitespace(self):
        # Rule 1: human text up to the first tag-like marker only, with
        # whitespace collapsed before the 300-character limit.
        tmp = os.path.join(self.tmp.name, "excerpt")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        self._append_prompt(
            sdir, sid, 30, "p-t30",
            "  Fix the widget\n\tnext line  with   spaces "
            "<user_rule>INSTALLED-BODY-SENTINEL-qqq</user_rule> tail "
            "<<<GEN-SENTINEL-qqq>>> more")
        # Collapse-before-truncate proof: 200 "ab" lines are 600 raw chars
        # but collapse to a 599-char single line, so the stored 300-char
        # excerpt keeps newlines out and differs from truncating raw first.
        self._append_prompt(
            sdir, sid, 31, "p-t31", "ab\n" * 200 + "TAIL <b> injected")
        con = self._isolated_con("excerpt")
        grok.sync(con, root=tmp)
        row = con.execute(
            "SELECT kind, is_genuine, text_excerpt FROM submissions"
            " WHERE native_id=?", (f"{S1}:prompt:30",)).fetchone()
        self.assertEqual(row["kind"], "genuine")
        self.assertEqual(row["is_genuine"], 1)
        self.assertEqual(row["text_excerpt"],
                         "Fix the widget next line with spaces")
        for table, col, val in self._all_text_values(con):
            self.assertNotIn("INSTALLED-BODY-SENTINEL-qqq", val,
                             f"{table}.{col} leaks post-marker text")
            self.assertNotIn("GEN-SENTINEL-qqq", val,
                             f"{table}.{col} leaks post-marker text")
        wide = con.execute(
            "SELECT kind, is_genuine, text_excerpt FROM submissions"
            " WHERE native_id=?", (f"{S1}:prompt:31",)).fetchone()
        self.assertEqual(wide["kind"], "genuine")
        self.assertEqual(wide["is_genuine"], 1)
        self.assertEqual(wide["text_excerpt"], "ab " * 100)
        self.assertEqual(len(wide["text_excerpt"]), 300)
        self.assertNotIn("\n", wide["text_excerpt"])
        self.assertNotIn("TAIL", wide["text_excerpt"])
        con.close()

    def test_child_session_prompts_are_never_genuine(self):
        # Rule 1: subagent and child sessions store no excerpt even when
        # chat history marks their prompts as user prompts, including a
        # parent link discovered in the session's own updates.
        tmp = os.path.join(self.tmp.name, "child")
        group = os.path.join(tmp, "%2Fchildgroup")
        sid = "02childsession-cccc-4b5c-8d6e-000000000003"
        sdir = os.path.join(group, sid)
        os.makedirs(sdir)
        with open(os.path.join(sdir, "summary.json"), "w") as fh:
            fh.write(json.dumps({
                "info": {"id": sid, "cwd": "/redacted/repo"},
                "created_at": "2026-09-01T11:00:00Z",
                "current_model_id": "grok-4.6",
                "session_kind": "subagent",
            }))
        text = "Human leading child text <tag> injected tail"
        with open(os.path.join(sdir, "updates.jsonl"), "w") as fh:
            fh.write(json.dumps({
                "timestamp": 1788802000,
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": text},
                        "_meta": {"modelId": "grok-4.6",
                                  "promptIndex": 0},
                    },
                    "_meta": {"eventId": "child-1", "promptId": "p-c1"},
                },
            }) + "\n")
            fh.write(json.dumps({
                "timestamp": 1788802001,
                "method": "_x.ai/session/update",
                "params": {
                    "sessionId": sid,
                    "update": {"sessionUpdate": "subagent_spawned",
                               "subagent_id": sid,
                               "parent_session_id": "parent-sid-aaa",
                               "parent_prompt_id": "p-x",
                               "child_session_id": sid},
                    "_meta": {"eventId": "child-link"},
                },
            }) + "\n")
            fh.write(json.dumps({
                "timestamp": 1788802002,
                "method": "_x.ai/session/update",
                "params": {
                    "sessionId": sid,
                    "update": {"sessionUpdate": "turn_completed",
                               "prompt_id": "p-c1",
                               "stop_reason": "end_turn",
                               "usage": {"inputTokens": 5,
                                         "outputTokens": 1,
                                         "totalTokens": 6}},
                    "_meta": {"eventId": "child-2"},
                },
            }) + "\n")
        with open(os.path.join(sdir, "chat_history.jsonl"), "w") as fh:
            fh.write(json.dumps({
                "type": "user",
                "content": [{"type": "text", "text": text}],
                "prompt_index": 0,
            }) + "\n")
        con = self._isolated_con("child")
        grok.sync(con, root=tmp)
        key = f"grok:{sid}"
        row = con.execute(
            "SELECT kind, is_genuine, text_excerpt, turn_id FROM submissions"
            " WHERE session_key=?", (key,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "synthetic")
        self.assertEqual(row["is_genuine"], 0)
        self.assertEqual(row["text_excerpt"], "")
        sess = con.execute(
            "SELECT parent_session_key, role FROM sessions"
            " WHERE session_key=?", (key,)).fetchone()
        self.assertEqual(sess["parent_session_key"], "grok:parent-sid-aaa")
        self.assertEqual(sess["role"], "subagent")
        for table, col, val in self._all_text_values(con):
            self.assertNotIn("Human leading child text", val,
                             f"{table}.{col} leaks child prompt text")
        con.close()

    def test_resync_rewrites_text_and_downgrades_unknown_in_place(self):
        # Rule 3: non-prefix text changes recompute hash and excerpt in
        # place, and evidence that becomes unknown downgrades the row
        # instead of preserving the stale genuine values.
        tmp = os.path.join(self.tmp.name, "rewrite")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        con = self._isolated_con("rewrite")
        grok.sync(con, root=tmp)
        nid = f"{S1}:prompt:3"
        before = con.execute(
            "SELECT text_hash, text_excerpt, kind, is_genuine FROM submissions"
            " WHERE native_id=?", (nid,)).fetchone()
        n_errors = con.execute(
            "SELECT COUNT(*) n FROM import_errors").fetchone()["n"]
        path = os.path.join(sdir, "updates.jsonl")
        with open(path) as fh:
            lines = fh.read().splitlines()
        rewritten = []
        for line in lines:
            try:
                obj = json.loads(line)
            except ValueError:
                rewritten.append(line)
                continue
            params = obj.get("params") if isinstance(
                obj.get("params"), dict) else {}
            update = params.get("update") if isinstance(params, dict) \
                else {}
            meta = update.get("_meta") if isinstance(
                update.get("_meta"), dict) else {}
            if isinstance(update, dict) and update.get("sessionUpdate") == \
                    "user_message_chunk" and meta.get("promptIndex") == 3:
                update["content"] = {
                    "type": "text",
                    "text": "Rewritten human leading <b>tail"}
            rewritten.append(json.dumps(obj))
        with open(path, "w") as fh:
            fh.write("\n".join(rewritten) + "\n")
        grok.sync(con, root=tmp, full=True)
        after = con.execute(
            "SELECT text_hash, text_excerpt, kind, is_genuine, turn_id"
            " FROM submissions WHERE native_id=?", (nid,)).fetchone()
        self.assertEqual(
            con.execute("SELECT COUNT(*) n FROM submissions"
                        " WHERE native_id=?", (nid,)).fetchone()["n"], 1)
        self.assertNotEqual(after["text_hash"], before["text_hash"])
        self.assertEqual(after["text_excerpt"], "Rewritten human leading")
        self.assertEqual(after["kind"], "genuine")
        self.assertEqual(after["is_genuine"], 1)
        self.assertEqual(after["turn_id"], f"{S1}:p-ddd")
        self.assertEqual(
            con.execute(
                "SELECT COUNT(*) n FROM import_errors").fetchone()["n"],
            n_errors)
        # Chat evidence disappears: the row downgrades to unknown in place.
        os.remove(os.path.join(sdir, "chat_history.jsonl"))
        grok.sync(con, root=tmp)
        downgraded = con.execute(
            "SELECT text_hash, text_excerpt, kind, is_genuine FROM submissions"
            " WHERE native_id=?", (nid,)).fetchone()
        self.assertEqual(downgraded["kind"], "unknown")
        self.assertEqual(downgraded["is_genuine"], 0)
        self.assertEqual(downgraded["text_excerpt"], "")
        self.assertEqual(
            con.execute("SELECT COUNT(*) n FROM submissions"
                        " WHERE native_id=?", (nid,)).fetchone()["n"], 1)
        con.close()

    def test_privacy_version_reimport_corrects_rows_in_place(self):
        # Rule 3: a version mismatch fully re-imports the source, updates
        # excerpts, kinds, hashes, genuineness and event detail in place,
        # and replaces that source's import_errors instead of duplicating.
        tmp = os.path.join(self.tmp.name, "versionbump")
        shutil.copytree(ROOT, tmp)
        con = self._isolated_con("versionbump")
        grok.sync(con, root=tmp)
        before = {table: con.execute(
            f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
            for table in ("submissions", "events", "import_errors",
                          "responses")}
        errors_before = sorted(
            (r["error"], r["ordinal_num"]) for r in con.execute(
                "SELECT error, ordinal_num FROM import_errors"))
        con.execute(
            "UPDATE submissions SET text_hash='0000000000000000',"
            " text_excerpt='STALE-EXCERPT', kind='unknown', is_genuine=0"
            " WHERE native_id=?", (f"{S1}:prompt:0",))
        con.execute(
            "UPDATE events SET target='STALE-TARGET',"
            " detail_json='{\"stale\": true}'"
            " WHERE session_key=? AND family='tool_call'"
            " AND native_id='call-read-1'", (S1,))
        con.execute("UPDATE sources SET privacy_version=0"
                    " WHERE harness='grok'")
        con.commit()
        session_dir = os.path.join(
            tmp, "%2Fredacted%2Frepo",
            "01fixture1-aaaa-4b5c-8d6e-000000000001")
        stats = grok.import_grok_session(con, session_dir)
        after = {table: con.execute(
            f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
            for table in ("submissions", "events", "import_errors",
                          "responses")}
        self.assertEqual(after, before)
        sub = con.execute(
            "SELECT text_hash, text_excerpt, kind, is_genuine FROM submissions"
            " WHERE native_id=?", (f"{S1}:prompt:0",)).fetchone()
        self.assertEqual(sub["kind"], "genuine")
        self.assertEqual(sub["is_genuine"], 1)
        self.assertEqual(sub["text_excerpt"], "")
        self.assertNotEqual(sub["text_hash"], "0000000000000000")
        evt = con.execute(
            "SELECT target, detail_json FROM events WHERE session_key=?"
            " AND family='tool_call' AND native_id='call-read-1'",
            (S1,)).fetchone()
        self.assertEqual(evt["target"],
                         "/redacted/repo/skills/ops/SKILL.md")
        self.assertIsNone(evt["detail_json"])
        errors_after = sorted(
            (r["error"], r["ordinal_num"]) for r in con.execute(
                "SELECT error, ordinal_num FROM import_errors"))
        self.assertEqual(errors_after, errors_before)
        for row in con.execute("SELECT error FROM import_errors"):
            self.assertIn(row["error"], set(privacy.ERROR_CATEGORIES)
                          | {privacy.ERROR_FALLBACK})
        # The re-imported session's sources carry the current version; a
        # whole-root sync then converges the remaining session the same way.
        versions = {r["privacy_version"] for r in con.execute(
            "SELECT privacy_version FROM sources WHERE harness='grok'"
            " AND path LIKE '%01fixture1-aaaa%'")}
        self.assertEqual(versions, {privacy.PRIVACY_VERSION})
        grok.sync(con, root=tmp)
        versions = {r["privacy_version"] for r in con.execute(
            "SELECT privacy_version FROM sources WHERE harness='grok'")}
        self.assertEqual(versions, {privacy.PRIVACY_VERSION})
        self.assertGreaterEqual(stats.get("submissions_updated", 0), 1)
        self.assertGreaterEqual(stats.get("events_updated", 0), 1)
        con.close()

    def test_title_never_becomes_event_name(self):
        # Rule 7: native free-text titles are not stored; only a validated
        # native tool name becomes the event name, else safe "unknown".
        tmp = os.path.join(self.tmp.name, "titles")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        calls = [
            ("call-title-1", "EVIL-TITLE-SENTINEL-token",
             {"version": 1, "name": "read_file", "kind": "read"}),
            ("call-title-2", "Only Title Here", None),
            ("call-title-3", None, None),
        ]
        with open(os.path.join(sdir, "updates.jsonl"), "a") as fh:
            for call_id, title, tool in calls:
                update = {"sessionUpdate": "tool_call",
                          "toolCallId": call_id,
                          "rawInput": {"target_file": "/redacted/repo/ok.txt"}}
                if title is not None:
                    update["title"] = title
                if tool is not None:
                    update["_meta"] = {"x.ai/tool": tool}
                fh.write(json.dumps({
                    "timestamp": 1788803000,
                    "method": "session/update",
                    "params": {"sessionId": sid, "update": update,
                               "_meta": {"eventId": f"title-{call_id}"}},
                }) + "\n")
        con = self._isolated_con("titles")
        grok.sync(con, root=tmp)
        names = {r["native_id"]: r["name"] for r in con.execute(
            "SELECT native_id, name FROM events WHERE session_key=?"
            " AND family='tool_call' AND native_id LIKE 'call-title-%'",
            (S1,))}
        self.assertEqual(names, {"call-title-1": "read_file",
                                 "call-title-2": "unknown",
                                 "call-title-3": "unknown"})
        for table, col, val in self._all_text_values(con):
            self.assertNotIn("EVIL-TITLE-SENTINEL-token", val,
                             f"{table}.{col} leaks title free text")
            self.assertNotIn("Only Title Here", val,
                             f"{table}.{col} leaks title free text")
        con.close()

    def test_pattern_and_url_never_become_targets(self):
        # Rule 6: event targets are validated paths and commands only;
        # pattern and url values are dropped entirely.
        tmp = os.path.join(self.tmp.name, "targets")
        shutil.copytree(ROOT, tmp)
        sid = "01fixture1-aaaa-4b5c-8d6e-000000000001"
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", sid)
        with open(os.path.join(sdir, "updates.jsonl"), "a") as fh:
            fh.write(json.dumps({
                "timestamp": 1788803100,
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "call-pat-1",
                        "rawInput": {
                            "pattern": "PATTERN-SENTINEL-1-*.py",
                            "url": "https://example.invalid/URL-SENTINEL-1",
                        },
                        "_meta": {"x.ai/tool": {"version": 1,
                                                "name": "search_files"}},
                    },
                    "_meta": {"eventId": "target-pat-1"},
                },
            }) + "\n")
            fh.write(json.dumps({
                "timestamp": 1788803101,
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "call-pat-2",
                        "rawInput": {
                            "target_file": "/redacted/repo/ok.txt",
                            "pattern": "PATTERN-SENTINEL-2-*.py",
                        },
                        "_meta": {"x.ai/tool": {"version": 1,
                                                "name": "search_files"}},
                    },
                    "_meta": {"eventId": "target-pat-2"},
                },
            }) + "\n")
        con = self._isolated_con("targets")
        grok.sync(con, root=tmp)
        targets = {r["native_id"]: (r["target"], r["detail_json"])
                   for r in con.execute(
                       "SELECT native_id, target, detail_json FROM events"
                       " WHERE session_key=? AND native_id LIKE 'call-pat-%'",
                       (S1,))}
        self.assertEqual(set(targets), {"call-pat-1", "call-pat-2"})
        self.assertIsNone(targets["call-pat-1"][0])
        self.assertEqual(targets["call-pat-2"][0], "/redacted/repo/ok.txt")
        for target, detail in targets.values():
            self.assertIsNone(detail)
        for table, col, val in self._all_text_values(con):
            for sentinel in ("PATTERN-SENTINEL-1", "PATTERN-SENTINEL-2",
                             "URL-SENTINEL-1", "example.invalid"):
                self.assertNotIn(sentinel, val,
                                 f"{table}.{col} leaks pattern/url value")
        con.close()

    # --- Review repair regression coverage (2026-09-23 findings) ---

    def _write_session(self, root_group, sid, summary, updates, events=None,
                       chat=None):
        sdir = os.path.join(root_group, sid)
        os.makedirs(sdir, exist_ok=True)
        with open(os.path.join(sdir, "summary.json"), "w") as fh:
            fh.write(json.dumps(summary))
        with open(os.path.join(sdir, "updates.jsonl"), "w") as fh:
            for obj in updates:
                fh.write(json.dumps(obj) + "\n")
        with open(os.path.join(sdir, "events.jsonl"), "w") as fh:
            for obj in (events or []):
                fh.write(json.dumps(obj) + "\n")
        if chat is not None:
            with open(os.path.join(sdir, "chat_history.jsonl"), "w") as fh:
                for obj in chat:
                    fh.write(json.dumps(obj) + "\n")
        return sdir

    def test_child_detected_only_by_session_relationship(self):
        # P1: a child proven solely by turn_started
        # session_relationship='subagent' is synthetic with an empty excerpt,
        # even with valid chat history and no summary kind or parent link.
        tmp = os.path.join(self.tmp.name, "relchild")
        group = os.path.join(tmp, "%2Frelgroup")
        sid = "03relchild-cccc-4b5c-8d6e-000000000003"
        text = "Human leading relationship text"
        sdir = self._write_session(
            group, sid,
            {"info": {"id": sid, "cwd": "/redacted/repo"},
             "created_at": "2026-09-01T12:00:00Z",
             "current_model_id": "grok-4.6",
             "reasoning_effort": "medium"},
            [{"timestamp": 1788804000, "method": "session/update",
              "params": {"sessionId": sid,
                         "update": {"sessionUpdate": "user_message_chunk",
                                    "content": {"type": "text", "text": text},
                                    "_meta": {"modelId": "grok-4.6",
                                              "promptIndex": 0}},
                         "_meta": {"eventId": "rel-1", "promptId": "p-r1"}}},
             {"timestamp": 1788804001, "method": "_x.ai/session/update",
              "params": {"sessionId": sid,
                         "update": {"sessionUpdate": "turn_completed",
                                    "prompt_id": "p-r1",
                                    "stop_reason": "end_turn",
                                    "usage": {"inputTokens": 5,
                                              "outputTokens": 1,
                                              "totalTokens": 6}},
                         "_meta": {"eventId": "rel-2"}}}],
            [{"ts": "2026-09-01T12:00:01Z", "type": "turn_started",
              "session_id": sid, "turn_number": 0, "model_id": "grok-4.6",
              "session_relationship": "subagent"},
             {"ts": "2026-09-01T12:00:02Z", "type": "turn_ended",
              "outcome": "completed"}],
            [{"type": "user",
              "content": [{"type": "text", "text": text}],
              "prompt_index": 0}])
        con = self._isolated_con("relchild")
        grok.sync(con, root=tmp)
        key = f"grok:{sid}"
        row = con.execute(
            "SELECT kind, is_genuine, text_excerpt FROM submissions"
            " WHERE session_key=?", (key,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "synthetic")
        self.assertEqual(row["is_genuine"], 0)
        self.assertEqual(row["text_excerpt"], "")
        sess = con.execute(
            "SELECT role FROM sessions WHERE session_key=?",
            (key,)).fetchone()
        self.assertEqual(sess["role"], "subagent")
        for table, col, val in self._all_text_values(con):
            self.assertNotIn("Human leading relationship text", val,
                             f"{table}.{col} leaks child prompt text")
        # Idempotent: a second sync writes nothing new.
        again = grok.sync(con, root=tmp)
        self.assertEqual(again["responses_inserted"], 0)
        self.assertEqual(again["submissions_inserted"], 0)
        con.close()

    def test_child_import_order_parent_link_reclassifies(self):
        # P1: the child is imported before its parent exists on disk. The
        # parent's later subagent_spawned dispatch reclassifies the child's
        # existing rows in place, and a later child re-sync converges via
        # the persisted parent link without duplicates.
        tmp = os.path.join(self.tmp.name, "orderchild")
        group = os.path.join(tmp, "%2Fordergroup")
        os.makedirs(group, exist_ok=True)
        child_sid = "04orderchild-cccc-4b5c-8d6e-000000000004"
        parent_sid = "04orderparent-aaaa-4b5c-8d6e-000000000005"
        child_text = "Human leading order child text"
        child_dir = self._write_session(
            group, child_sid,
            {"info": {"id": child_sid, "cwd": "/redacted/repo"},
             "created_at": "2026-09-01T13:00:00Z",
             "current_model_id": "grok-4.6"},
            [{"timestamp": 1788805000, "method": "session/update",
              "params": {"sessionId": child_sid,
                         "update": {"sessionUpdate": "user_message_chunk",
                                    "content": {"type": "text",
                                                "text": child_text},
                                    "_meta": {"promptIndex": 0}},
                         "_meta": {"eventId": "ord-c-1",
                                   "promptId": "p-o1"}}},
             {"timestamp": 1788805001, "method": "_x.ai/session/update",
              "params": {"sessionId": child_sid,
                         "update": {"sessionUpdate": "turn_completed",
                                    "prompt_id": "p-o1",
                                    "stop_reason": "end_turn",
                                    "usage": {"inputTokens": 5,
                                              "outputTokens": 1,
                                              "totalTokens": 6}},
                         "_meta": {"eventId": "ord-c-2"}}}],
            [{"ts": "2026-09-01T13:00:01Z", "type": "turn_started",
              "session_id": child_sid, "turn_number": 0,
              "model_id": "grok-4.6",
              "session_relationship": "primary"}],
            [{"type": "user",
              "content": [{"type": "text", "text": child_text}],
              "prompt_index": 0}])
        con = self._isolated_con("orderchild")
        grok.import_grok_session(con, child_dir)
        child_key = f"grok:{child_sid}"
        first = con.execute(
            "SELECT kind, is_genuine, text_excerpt FROM submissions"
            " WHERE session_key=?", (child_key,)).fetchone()
        # No child evidence yet: a genuine main-session prompt is kept.
        # The fixture text has no tag-like marker, so the excerpt is the
        # human text itself under privacy.py rule 1.
        self.assertEqual(first["kind"], "genuine")
        self.assertEqual(first["is_genuine"], 1)
        self.assertEqual(first["text_excerpt"], child_text)
        # The parent arrives later with a spawn record naming the child.
        parent_dir = self._write_session(
            group, parent_sid,
            {"info": {"id": parent_sid, "cwd": "/redacted/repo"},
             "created_at": "2026-09-01T13:05:00Z",
             "current_model_id": "grok-4.6"},
            [{"timestamp": 1788805100, "method": "_x.ai/session/update",
              "params": {"sessionId": parent_sid,
                         "update": {"sessionUpdate": "subagent_spawned",
                                    "subagent_id": child_sid,
                                    "parent_session_id": parent_sid,
                                    "parent_prompt_id": "p-x",
                                    "child_session_id": child_sid},
                         "_meta": {"eventId": "ord-p-1"}}}],
            [],
            None)
        grok.import_grok_session(con, parent_dir)
        sess = con.execute(
            "SELECT parent_session_key FROM sessions WHERE session_key=?",
            (child_key,)).fetchone()
        self.assertEqual(sess["parent_session_key"], f"grok:{parent_sid}")
        fixed = con.execute(
            "SELECT kind, is_genuine, text_excerpt FROM submissions"
            " WHERE session_key=?", (child_key,)).fetchone()
        self.assertEqual(fixed["kind"], "synthetic")
        self.assertEqual(fixed["is_genuine"], 0)
        self.assertEqual(fixed["text_excerpt"], "")
        self.assertEqual(
            con.execute("SELECT COUNT(*) n FROM submissions"
                        " WHERE session_key=?", (child_key,)).fetchone()["n"],
            1)
        # A later child re-sync with no new bytes stays synthetic via the
        # persisted parent link, without duplicates or leaks.
        grok.import_grok_session(con, child_dir)
        again = con.execute(
            "SELECT kind, is_genuine, text_excerpt FROM submissions"
            " WHERE session_key=?", (child_key,)).fetchone()
        self.assertEqual(again["kind"], "synthetic")
        self.assertEqual(again["is_genuine"], 0)
        self.assertEqual(again["text_excerpt"], "")
        for table, col, val in self._all_text_values(con):
            self.assertNotIn("Human leading order child text", val,
                             f"{table}.{col} leaks child prompt text")
        con.close()

    def test_late_model_evidence_updates_response_in_place(self):
        # P2: summary model/effort arriving after the first sync updates the
        # existing response row in place, without duplicates, even when no
        # JSONL bytes changed.
        tmp = os.path.join(self.tmp.name, "latemodel")
        group = os.path.join(tmp, "%2Flatemodel")
        sid = "05latemodel-cccc-4b5c-8d6e-000000000006"
        text = "Late model probe"
        sdir = self._write_session(
            group, sid,
            {"info": {"id": sid, "cwd": "/redacted/repo"},
             "created_at": "2026-09-01T14:00:00Z",
             "current_model_id": "grok-4.6",
             "reasoning_effort": "medium"},
            [{"timestamp": 1788806000, "method": "session/update",
              "params": {"sessionId": sid,
                         "update": {"sessionUpdate": "user_message_chunk",
                                    "content": {"type": "text", "text": text},
                                    "_meta": {"promptIndex": 0}},
                         "_meta": {"eventId": "lm-1", "promptId": "p-lm1"}}},
             {"timestamp": 1788806001, "method": "_x.ai/session/update",
              "params": {"sessionId": sid,
                         "update": {"sessionUpdate": "turn_completed",
                                    "prompt_id": "p-lm1",
                                    "stop_reason": "end_turn",
                                    "usage": {"inputTokens": 10,
                                              "outputTokens": 2,
                                              "totalTokens": 12}},
                         "_meta": {"eventId": "lm-2"}}}],
            [{"ts": "2026-09-01T14:00:01Z", "type": "turn_ended",
              "outcome": "completed"}],
            [{"type": "user",
              "content": [{"type": "text", "text": text}],
              "prompt_index": 0}])
        con = self._isolated_con("latemodel")
        grok.sync(con, root=tmp)
        key = f"grok:{sid}"
        first = con.execute(
            "SELECT model, effort, input_tokens, total_tokens FROM responses"
            " WHERE response_id=?", (f"{key}:p-lm1",)).fetchone()
        self.assertEqual(first["model"], "grok-4.6")
        self.assertEqual(first["effort"], "medium")
        self.assertEqual((first["input_tokens"], first["total_tokens"]),
                         (10, 12))
        # Late summary evidence changes model and effort with no JSONL
        # growth. Full re-read is not required; the re-sync reconciles.
        with open(os.path.join(sdir, "summary.json")) as fh:
            summary = json.load(fh)
        summary["current_model_id"] = "grok-4.7"
        summary["reasoning_effort"] = "high"
        with open(os.path.join(sdir, "summary.json"), "w") as fh:
            fh.write(json.dumps(summary))
        second = grok.sync(con, root=tmp)
        self.assertEqual(second["responses_inserted"], 0)
        rows = list(con.execute(
            "SELECT model, effort, input_tokens, total_tokens FROM responses"
            " WHERE session_key=?", (key,)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "grok-4.7")
        self.assertEqual(rows[0]["effort"], "high")
        self.assertEqual((rows[0]["input_tokens"], rows[0]["total_tokens"]),
                         (10, 12))
        # A missing new value never clears a known one: removing effort
        # from summary keeps the last valid effort.
        del summary["reasoning_effort"]
        with open(os.path.join(sdir, "summary.json"), "w") as fh:
            fh.write(json.dumps(summary))
        # Chat still lacks effort, so no valid new effort arrives.
        grok.sync(con, root=tmp)
        kept = con.execute(
            "SELECT model, effort FROM responses WHERE response_id=?",
            (f"{key}:p-lm1",)).fetchone()
        self.assertEqual(kept["model"], "grok-4.7")
        con.close()

    def test_malformed_present_usage_quarantined(self):
        # P2: present malformed usage (wrong types, booleans, non-dict
        # shapes) is quarantined under malformed_usage with no partial row,
        # while missing/null/empty usage stays absent (NULL, no error) and
        # later valid records still import.
        tmp = os.path.join(self.tmp.name, "badusage")
        group = os.path.join(tmp, "%2Fbadusage")
        sid = "06badusage-cccc-4b5c-8d6e-000000000007"
        sdir = os.path.join(group, sid)
        os.makedirs(sdir)
        with open(os.path.join(sdir, "summary.json"), "w") as fh:
            fh.write(json.dumps({
                "info": {"id": sid, "cwd": "/redacted/repo"},
                "created_at": "2026-09-01T15:00:00Z",
                "current_model_id": "grok-4.6"}))
        with open(os.path.join(sdir, "events.jsonl"), "w") as fh:
            pass
        with open(os.path.join(sdir, "chat_history.jsonl"), "w") as fh:
            for idx in range(6):
                fh.write(json.dumps({
                    "type": "user",
                    "content": [{"type": "text",
                                 "text": f"probe {idx}"}],
                    "prompt_index": idx}) + "\n")
        completions = [
            ("p-good1", {"inputTokens": 10, "outputTokens": 2,
                         "totalTokens": 12}),
            ("p-bad-str", {"inputTokens": "10", "outputTokens": 2,
                           "totalTokens": 12}),
            ("p-bad-bool", {"inputTokens": True, "outputTokens": 2,
                            "totalTokens": 12}),
            ("p-bad-shape", ["not", "a", "dict"]),
            ("p-absent-null", None),
            ("p-absent-empty", {}),
        ]
        with open(os.path.join(sdir, "updates.jsonl"), "w") as fh:
            for idx, (pid, usage) in enumerate(completions):
                fh.write(json.dumps({
                    "timestamp": 1788807000 + idx,
                    "method": "session/update",
                    "params": {"sessionId": sid,
                               "update": {"sessionUpdate":
                                          "user_message_chunk",
                                          "content": {"type": "text",
                                                      "text": f"probe {idx}"},
                                          "_meta": {"promptIndex": idx}},
                               "_meta": {"eventId": f"bu-{idx}",
                                         "promptId": pid}}}) + "\n")
            for idx, (pid, usage) in enumerate(completions):
                update = {"sessionUpdate": "turn_completed",
                          "prompt_id": pid, "stop_reason": "end_turn"}
                if usage is not None:
                    update["usage"] = usage
                # p-absent-null carries an explicit null usage value.
                if pid == "p-absent-null":
                    update["usage"] = None
                fh.write(json.dumps({
                    "timestamp": 1788807100 + idx,
                    "method": "_x.ai/session/update",
                    "params": {"sessionId": sid, "update": update,
                               "_meta": {"eventId": f"bu-c-{idx}"}}}) + "\n")
        con = self._isolated_con("badusage")
        grok.sync(con, root=tmp)
        key = f"grok:{sid}"
        ids = sorted(
            r["response_id"] for r in con.execute(
                "SELECT response_id FROM responses WHERE session_key=?",
                (key,)))
        self.assertEqual(ids, sorted([
            f"{key}:p-good1", f"{key}:p-absent-null",
            f"{key}:p-absent-empty"]))
        # Absent usages keep NULL counters without an error.
        for pid in ("p-absent-null", "p-absent-empty"):
            row = con.execute(
                "SELECT input_tokens, total_tokens FROM responses"
                " WHERE response_id=?", (f"{key}:{pid}",)).fetchone()
            self.assertIsNone(row["input_tokens"])
            self.assertIsNone(row["total_tokens"])
        good = con.execute(
            "SELECT input_tokens, output_tokens, total_tokens FROM responses"
            " WHERE response_id=?", (f"{key}:p-good1",)).fetchone()
        self.assertEqual((good["input_tokens"], good["output_tokens"],
                          good["total_tokens"]), (10, 2, 12))
        errors = list(con.execute(
            "SELECT error, line_excerpt FROM import_errors"
            " WHERE error='malformed_usage'"))
        self.assertEqual(len(errors), 3)
        allowed = set(privacy.ERROR_CATEGORIES) | {privacy.ERROR_FALLBACK}
        for row in errors:
            self.assertIn(row["error"], allowed)
            self.assertEqual(row["error"], "malformed_usage")
            self.assertEqual(row["line_excerpt"], "method,params,timestamp")
            self.assertNotIn("p-bad", row["line_excerpt"] or "")
            self.assertNotIn("10", row["line_excerpt"] or "")
        # No record values, exception text or class names in errors: only
        # the fixed category and key-only excerpts. Native prompt ids may
        # still appear as submission turn_id identifiers (allowed ledger
        # keys), but never inside import_errors.
        for row in errors:
            self.assertNotIn("p-bad", row["line_excerpt"] or "")
            self.assertNotIn("p-bad", row["error"] or "")
            self.assertNotIn("ValueError", row["error"] or "")
            self.assertNotIn("_AdapterError", row["error"] or "")
            self.assertNotIn("malformed_usage ", row["error"] or "")
        # A later valid record for a new prompt still imports; replay does
        # not duplicate the quarantine.
        with open(os.path.join(sdir, "updates.jsonl"), "a") as fh:
            fh.write(json.dumps({
                "timestamp": 1788807200, "method": "session/update",
                "params": {"sessionId": sid,
                           "update": {"sessionUpdate": "user_message_chunk",
                                      "content": {"type": "text",
                                                  "text": "probe 6"},
                                      "_meta": {"promptIndex": 6}},
                           "_meta": {"eventId": "bu-6",
                                     "promptId": "p-good2"}}}) + "\n")
            fh.write(json.dumps({
                "timestamp": 1788807201, "method": "_x.ai/session/update",
                "params": {"sessionId": sid,
                           "update": {"sessionUpdate": "turn_completed",
                                      "prompt_id": "p-good2",
                                      "stop_reason": "end_turn",
                                      "usage": {"inputTokens": 7,
                                                "outputTokens": 1,
                                                "totalTokens": 8}},
                           "_meta": {"eventId": "bu-c-6"}}}) + "\n")
        with open(os.path.join(sdir, "chat_history.jsonl"), "a") as fh:
            fh.write(json.dumps({
                "type": "user",
                "content": [{"type": "text", "text": "probe 6"}],
                "prompt_index": 6}) + "\n")
        second = grok.sync(con, root=tmp)
        self.assertEqual(second["responses_inserted"], 1)
        self.assertIsNotNone(con.execute(
            "SELECT 1 FROM responses WHERE response_id=?",
            (f"{key}:p-good2",)).fetchone())
        self.assertEqual(len(list(con.execute(
            "SELECT 1 FROM import_errors WHERE error='malformed_usage'"))), 3)
        con.close()
