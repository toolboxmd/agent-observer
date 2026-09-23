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
            "SELECT * FROM import_errors WHERE error LIKE 'conflicting usage%'")
        self.assertEqual(len(conflicts), 1)
