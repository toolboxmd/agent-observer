"""Claude Code adapter: usage once per message, human input kinds, events,
instruction identity, subagents."""

import os

from agent_observer import report
from agent_observer.adapters import claude
from tests.helpers import FIXTURES, LedgerCase, fixture

ROOT = os.path.join(FIXTURES, "claude")


def _write(path, lines):
    with open(path, "w") as fh:
        fh.writelines(lines)
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
        # Rule 6 identifier family: the native skill name field survives in
        # both target and name; titles and free text never do.
        self.assertEqual(by["skill_invoke"][0]["target"], "agentsmd:operations")
        self.assertEqual(by["skill_invoke"][0]["name"], "agentsmd:operations")
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


class ClaudeMetadataTest(LedgerCase):
    """Known benign metadata records are recognized and ignored: no rows,
    no stored contents, no import errors. cost-state is not benign: it
    carries per-model counters pending reconciliation, so it stays
    quarantined as unsupported_schema with nothing stored. Genuinely
    unknown types stay quarantined as unsupported_schema."""

    def test_cost_state_is_quarantined_with_nothing_stored(self):
        stats = claude.import_claude_file(
            self.con, fixture("claude-metadata.jsonl"))
        self.assertEqual(stats["responses_inserted"], 1)
        # cost-state plus the genuinely unknown future type quarantine;
        # every other metadata record (including queue-operation with a
        # non-enqueue operation) is recognized and ignored.
        self.assertEqual(stats["malformed"], 2)
        errors = self.query(
            "SELECT error, line_excerpt FROM import_errors ORDER BY id")
        self.assertEqual(len(errors), 2)
        cost, future = errors
        self.assertEqual(cost["error"], "unsupported_schema")
        self.assertEqual(future["error"], "unsupported_schema")
        self.assertEqual(future["line_excerpt"],
                         "sessionId,timestamp,type,uuid")
        # Privacy rule 5: structure only, key names never values. The
        # cost-state keys appear in sorted order...
        keys = cost["line_excerpt"].split(",")
        self.assertIn("modelUsage", keys)
        self.assertEqual(keys, sorted(keys))
        # ...and no per-model counter value persists anywhere.
        blob = "".join(r["line_excerpt"] or "" for r in errors)
        blob += "".join(r["text_excerpt"] or "" for r in self.query(
            "SELECT text_excerpt FROM submissions"))
        blob += "".join(r["detail_json"] or "" for r in self.query(
            "SELECT detail_json FROM events"))
        for value in ("26343", "703577", "1.0082185", "46843"):
            self.assertNotIn(value, blob)
        # The quarantined snapshot creates no usage row: only the genuine
        # assistant message counts.
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM responses")[0]["n"], 1)
        self.assertEqual(
            self.query("SELECT total_tokens t FROM responses"
                       " WHERE response_id='claude:msg-meta-1'")[0]["t"],
            10 + 100 + 1000 + 50)
        # Existing behavior is intact: the genuine submission still imports.
        self.assertEqual(
            self.query("SELECT kind FROM submissions"
                       " WHERE native_id='claude:u-meta-1'")[0]["kind"],
            "genuine")
        # No metadata contents persist anywhere in the ledger.
        blob += "".join(r["identity_json"] or "" for r in self.query(
            "SELECT identity_json FROM sessions"))
        blob += "".join(
            r["error"] or ""
            for r in self.query("SELECT error FROM import_errors"))
        for sentinel in ("synthetic-bridge", "synthetic-leaf",
                          "synthetic-operation", "Synthetic title probe",
                          "synthetic-permission", "synthetic queue content"):
            self.assertNotIn(sentinel, blob)


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


class ClaudeQueueOperationTest(LedgerCase):
    """Mid-turn typed prompts: an enqueue record's prompt text is a genuine
    human submission, deduplicated against the user record it later
    becomes so each prompt counts once. dequeue, remove and every other
    operation store nothing and never error."""

    MAIN_TEXT = ("Please summarize the quarterly status in plain words"
                 " for the review")

    def test_enqueue_and_matching_user_count_once(self):
        stats = claude.import_claude_file(
            self.con, fixture("claude-queue.jsonl"))
        self.assertEqual(stats["malformed"], 0)
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM import_errors")[0]["n"], 0)
        # Three prompts: queued-then-submitted, queue-only, direct.
        rows = {r["native_id"]: r for r in self.query(
            "SELECT native_id, alias_id, kind, text_excerpt, text_hash,"
            " turn_id, session_key FROM submissions")}
        self.assertEqual(
            sorted(rows),
            ["claude:queue:sess-queue:3", "claude:u-q-1", "claude:u-q-2"])
        merged = rows["claude:u-q-1"]
        self.assertEqual(merged["alias_id"], "claude:queue:sess-queue:0")
        self.assertEqual(merged["kind"], "genuine")
        self.assertEqual(merged["text_excerpt"], self.MAIN_TEXT)
        self.assertEqual(merged["turn_id"], "claude:p-q-1")
        self.assertEqual(merged["session_key"], "claude:sess-queue")
        self.assertEqual(stats["submissions_inserted"], 3)
        self.assertEqual(stats.get("submissions_dedup"), 1)

    def test_queue_only_submission_truncates_at_markers(self):
        claude.import_claude_file(self.con, fixture("claude-queue.jsonl"))
        row = self.query(
            "SELECT kind, text_excerpt FROM submissions"
            " WHERE native_id='claude:queue:sess-queue:3'")[0]
        self.assertEqual(row["kind"], "genuine")
        self.assertEqual(row["text_excerpt"], "Check the other file")
        blob = "".join(r["text_excerpt"] or "" for r in self.query(
            "SELECT text_excerpt FROM submissions"))
        blob += "".join(r["detail_json"] or "" for r in self.query(
            "SELECT detail_json FROM events"))
        blob += "".join(r["identity_json"] or "" for r in self.query(
            "SELECT identity_json FROM sessions"))
        blob += "".join(
            (r["error"] or "") + (r["line_excerpt"] or "")
            for r in self.query("SELECT error, line_excerpt FROM import_errors"))
        self.assertNotIn("SECRET-QUEUE-TAIL-zzz999", blob)
        self.assertNotIn("A removed queued prompt is never stored", blob)
        self.assertNotIn("An unknown queue operation is never stored", blob)

    def test_noop_operations_store_nothing_and_never_error(self):
        stats = claude.import_claude_file(
            self.con, fixture("claude-queue.jsonl"))
        self.assertEqual(stats["malformed"], 0)
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM submissions")[0]["n"], 3)
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM import_errors")[0]["n"], 0)

    def test_full_reimport_adds_no_second_row(self):
        claude.import_claude_file(self.con, fixture("claude-queue.jsonl"))
        again = claude.import_claude_file(
            self.con, fixture("claude-queue.jsonl"), full=True)
        self.assertEqual(again["submissions_inserted"], 0)
        self.assertEqual(again.get("submissions_dedup", 0), 0)
        self.assertEqual(again["malformed"], 0)
        rows = {r["native_id"]: r for r in self.query(
            "SELECT native_id, alias_id FROM submissions")}
        self.assertEqual(
            sorted(rows),
            ["claude:queue:sess-queue:3", "claude:u-q-1", "claude:u-q-2"])
        self.assertEqual(rows["claude:u-q-1"]["alias_id"],
                         "claude:queue:sess-queue:0")

    def test_reverse_order_user_then_enqueue_merges(self):
        dst = os.path.join(self.tmp.name, "sess-rev.jsonl")
        _write(dst, [
            '{"sessionId": "sess-rev", "cwd": "/redacted/repo",'
            ' "version": "2.1.280", "isSidechain": false, "type": "user",'
            ' "uuid": "u-rev-1", "promptId": "p-rev-1",'
            ' "origin": {"kind": "human"}, "promptSource": "typed",'
            ' "timestamp": "2026-09-14T10:00:01Z",'
            ' "message": {"role": "user",'
            ' "content": "A prompt queued after it was submitted"}}\n',
            '{"sessionId": "sess-rev", "cwd": "/redacted/repo",'
            ' "version": "2.1.280", "isSidechain": false,'
            ' "type": "queue-operation", "operation": "enqueue",'
            ' "content": "A prompt queued after it was submitted",'
            ' "timestamp": "2026-09-14T10:00:02Z"}\n',
        ])
        stats = claude.import_claude_file(self.con, dst)
        self.assertEqual(stats["malformed"], 0)
        rows = self.query("SELECT native_id, alias_id FROM submissions")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["native_id"], "claude:u-rev-1")
        self.assertEqual(rows[0]["alias_id"], "claude:queue:sess-rev:1")

    def test_subagent_enqueue_keeps_no_excerpt(self):
        dst = os.path.join(self.tmp.name, "sess-subq.jsonl")
        _write(dst, [
            '{"sessionId": "sess-subq", "cwd": "/redacted/repo",'
            ' "version": "2.1.280", "isSidechain": true,'
            ' "agentId": "ag-9", "type": "user", "uuid": "u-sub-1",'
            ' "timestamp": "2026-09-14T10:00:01Z",'
            ' "message": {"role": "user",'
            ' "content": "Child work item SECRET-SUBQ-aaa111"}}\n',
            '{"sessionId": "sess-subq", "cwd": "/redacted/repo",'
            ' "version": "2.1.280", "type": "queue-operation",'
            ' "operation": "enqueue",'
            ' "content": "Subagent queued SECRET-SUBQ-bbb222",'
            ' "timestamp": "2026-09-14T10:00:02Z"}\n',
        ])
        claude.import_claude_file(self.con, dst)
        rows = {r["native_id"]: r for r in self.query(
            "SELECT native_id, kind, text_excerpt FROM submissions")}
        queued = rows["claude:queue:sess-subq:1"]
        self.assertEqual(queued["kind"], "genuine")
        self.assertEqual(queued["text_excerpt"], "")
        blob = "".join(r["text_excerpt"] or "" for r in self.query(
            "SELECT text_excerpt FROM submissions"))
        self.assertNotIn("SECRET-SUBQ-bbb222", blob)
