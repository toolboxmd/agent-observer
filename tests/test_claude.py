"""Claude Code adapter: usage once per message, human input kinds, events,
instruction identity, subagents."""

import os

from agent_observer import report
from agent_observer.adapters import claude
from tests.helpers import FIXTURES, LedgerCase, fixture

ROOT = os.path.join(FIXTURES, "claude")
MAIN = "claude:sess-main"
SUB = "claude:sess-main:agent:ag-1"


class ClaudeAdapterTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.stats = claude.sync(self.con, root=ROOT)

    def test_streamed_blocks_count_one_response_and_synthetic_errors_none(self):
        self.assertEqual(self.stats["sources"], 2)
        rows = self.query(
            "SELECT response_id, total_tokens, reasoning_output_tokens, model"
            " FROM responses WHERE session_key=? ORDER BY response_id", (MAIN,))
        self.assertEqual([r["response_id"] for r in rows],
                         ["claude:msg-1", "claude:msg-2"])
        # input + cache creation + cache read + output; thinking stays inside output.
        self.assertEqual(rows[0]["total_tokens"], 10 + 100 + 1000 + 50)
        self.assertEqual(rows[0]["reasoning_output_tokens"], 20)
        self.assertEqual(report.scope_totals(self.con, {MAIN})["total_tokens"],
                         1160 + 1135)

    def test_human_input_kinds_are_told_apart(self):
        kinds = {r["native_id"]: r["kind"] for r in self.query(
            "SELECT native_id, kind FROM submissions WHERE session_key=?", (MAIN,))}
        self.assertEqual(kinds["claude:u-1"], "genuine")
        self.assertEqual(kinds["claude:q-1"], "genuine")
        self.assertEqual(kinds["claude:q-2"], "synthetic")
        self.assertEqual(kinds["claude:u-cmd"], "command")
        self.assertEqual(kinds["claude:u-meta"], "scaffolding")
        self.assertEqual(kinds["claude:u-int"], "interrupt")
        self.assertNotIn("claude:u-tr1", kinds)
        sub_kinds = {r["kind"] for r in self.query(
            "SELECT kind FROM submissions WHERE session_key=?", (SUB,))}
        self.assertEqual(sub_kinds, {"synthetic"})

    def test_reads_skills_and_tool_joins(self):
        events = self.query(
            "SELECT family, native_id, name, target, status FROM events"
            " WHERE session_key=? ORDER BY id", (MAIN,))
        by = {}
        for e in events:
            by.setdefault(e["family"], []).append(e)
        self.assertEqual([e["target"] for e in by["read"]], ["/redacted/repo/notes.md"])
        # Rule 6 keeps the skill identity in target; the skill name is an
        # instance value, not a canonical kind, so names stay empty.
        self.assertEqual(by["skill_invoke"][0]["target"], "agentsmd:operations")
        self.assertIsNone(by["skill_invoke"][0]["name"])
        self.assertEqual(len(by["compaction"]), 1)
        calls = {e["native_id"] for e in by["tool_call"]}
        results = {e["native_id"] for e in by["tool_result"]}
        self.assertEqual(calls, results)
        final = [e for e in self.query(
            "SELECT detail_json FROM events WHERE family='assistant_message'"
            " AND session_key=?", (MAIN,)) if "changelog" in e["detail_json"]]
        self.assertEqual(len(final), 1)

    def test_identity_comes_from_the_clean_hook_block_without_contents(self):
        row = self.query("SELECT * FROM sessions WHERE session_key=?", (MAIN,))[0]
        self.assertEqual(row["instructions_sha256"], "fixture-instructions-sha")
        self.assertEqual(row["preferences_sha256"], "fixture-preferences-sha")
        self.assertEqual(row["direction_status"], "ready")
        self.assertEqual(row["project_dir"], "/redacted/repo")
        self.assertNotIn("secret", row["identity_json"])
        # The skill read names the plugin release.
        self.assertIn("12.1.0", row["identity_json"])

    def test_subagent_is_a_child_session_with_its_own_usage(self):
        row = self.query("SELECT * FROM sessions WHERE session_key=?", (SUB,))[0]
        self.assertEqual(row["parent_session_key"], MAIN)
        self.assertEqual(row["role"], "subagent")
        self.assertEqual(report.scope_totals(self.con, {SUB})["total_tokens"], 10)

    def test_resync_is_idempotent(self):
        again = claude.sync(self.con, root=ROOT)
        self.assertEqual(again["unchanged"], 2)
        again = claude.sync(self.con, root=ROOT, full=True)
        self.assertEqual(again["responses_inserted"], 0)
        self.assertEqual(report.scope_totals(self.con)["responses"], 3)


class ClaudeContradictoryMarkersTest(LedgerCase):
    """Human-origin metadata never makes a known marker genuine."""

    SENTINELS = (
        "SECRET-CONTRAD-SYS-aaa111",
        "SECRET-CONTRAD-SKILL-bbb222",
        "SECRET-CONTRAD-LOCAL-ccc333",
        "SECRET-CONTRAD-CMD-ddd444",
        "SECRET-CONTRAD-CAVEAT-eee555",
        "SECRET-CONTRAD-TASK-fff666",
        "SECRET-CONTRAD-BASH-ggg777",
        "SECRET-CONTRAD-META-hhh888",
        "SECRET-CONTRAD-HOOK-jjj000",
        "SECRET-CONTRAD-QUEUE-iii999",
    )

    def test_markers_with_human_metadata_stay_non_genuine(self):
        claude.import_claude_file(
            self.con, fixture("claude-contradictory.jsonl"))
        rows = {r["native_id"]: r for r in self.query(
            "SELECT native_id, kind, text_excerpt FROM submissions")}
        # Contradictory scaffolding markers fail closed even with
        # origin.kind=human or promptSource=typed.
        self.assertEqual(rows["claude:u-contra-sys"]["kind"], "scaffolding")
        self.assertEqual(rows["claude:u-contra-skill"]["kind"], "scaffolding")
        self.assertEqual(rows["claude:u-contra-local"]["kind"], "scaffolding")
        self.assertEqual(rows["claude:u-contra-cmd"]["kind"], "command")
        self.assertEqual(rows["claude:u-contra-caveat"]["kind"], "scaffolding")
        self.assertEqual(rows["claude:u-contra-task"]["kind"], "scaffolding")
        self.assertEqual(rows["claude:u-contra-bash"]["kind"], "scaffolding")
        self.assertEqual(rows["claude:u-contra-meta"]["kind"], "scaffolding")
        self.assertEqual(rows["claude:u-contra-hook"]["kind"], "scaffolding")
        self.assertEqual(rows["claude:q-contra-1"]["kind"], "synthetic")
        # Legitimate plain typed input stays genuine.
        self.assertEqual(rows["claude:u-contra-real"]["kind"], "genuine")
        self.assertTrue(rows["claude:u-contra-real"]["text_excerpt"])
        for native in ("claude:u-contra-sys", "claude:u-contra-skill",
                       "claude:u-contra-local", "claude:u-contra-cmd",
                       "claude:u-contra-caveat", "claude:u-contra-task",
                       "claude:u-contra-bash", "claude:u-contra-meta",
                       "claude:u-contra-hook", "claude:q-contra-1"):
            self.assertEqual(rows[native]["text_excerpt"], "", native)
        blob = "".join(r["text_excerpt"] or "" for r in self.query(
            "SELECT text_excerpt FROM submissions"))
        blob += "".join(r["detail_json"] or "" for r in self.query(
            "SELECT detail_json FROM events"))
        blob += "".join(r["identity_json"] or "" for r in self.query(
            "SELECT identity_json FROM sessions"))
        blob += "".join(
            (r["error"] or "") + (r["line_excerpt"] or "")
            for r in self.query("SELECT error, line_excerpt FROM import_errors"))
        for sentinel in self.SENTINELS:
            self.assertNotIn(sentinel, blob)
