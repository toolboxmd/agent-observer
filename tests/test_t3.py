"""Chromeria/T3 attribution: turn origins, thread trees, rates, unbound flag.

Fixtures mirror the real T3 schema shapes (table and column names from
``~/.t3/userdata/state.sqlite``) with fabricated ids, links and rates:
no transcript content and no private data. The Claude transcript lines
mirror the real ``promptSource: "sdk"`` record shape.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_observer import db, report
from agent_observer.adapters import claude
from agent_observer.adapters import t3 as t3_adapter
from agent_observer.adapters.t3 import thread_root
from tests.helpers import LedgerCase

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fabricated identities. Nothing here is a real thread, session or message.
T1 = "t1-root-0000-0000-000000000001"
SUB = "sub.t1-root-0000-0000-000000000001.w1abcd12"
NESTED = "sub.sub.t1-root-0000-0000-000000000001.w1abcd12.q9ef3456"
T2 = "t2-root-0000-0000-000000000002"
CLSESS = "c1aude-5e55-1000-000000000001"
CXSESS = "c0de8-7e55-1000-000000000002"
CX2 = "c0de8-7e55-1000-000000000003"
OPSESS = "ses_fixture00000000000000001"
ROUTER_SESS = "d1a9109-4000-4000-8000-000000000001"
U1, U2, U3, U4, U5, U6 = ("u11111111-0000-4000-8000-00000000000%d" % n
                          for n in (1, 2, 3, 4, 5, 6))
M1, M2, M3 = ("9ead9e99-0000-4000-8000-00000000000%d" % n for n in (1, 2, 3))
PR7 = "https://github.com/example/alpha/pull/7"
ISS3 = "https://github.com/example/alpha/issues/3"
PR9 = "https://github.com/example/alpha/pull/9"

T3_TABLES = """
CREATE TABLE orchestration_events(sequence INTEGER, event_id TEXT,
 aggregate_kind TEXT, stream_id TEXT, stream_version INTEGER,
 event_type TEXT, occurred_at TEXT, command_id TEXT,
 causation_event_id TEXT, correlation_id TEXT, actor_kind TEXT,
 payload_json TEXT, metadata_json TEXT);
CREATE TABLE provider_session_runtime(thread_id TEXT, provider_name TEXT,
 adapter_key TEXT, runtime_mode TEXT, status TEXT, last_seen_at TEXT,
 resume_cursor_json TEXT, runtime_payload_json TEXT,
 provider_instance_id TEXT);
CREATE TABLE projection_thread_pull_requests(thread_id TEXT, host TEXT,
 repository TEXT, number INTEGER, url TEXT, source TEXT, linked_at TEXT,
 snapshot_json TEXT, stack_json TEXT);
CREATE TABLE fork_thread_issue_links(thread_id TEXT, host TEXT,
 repository TEXT, number INTEGER, url TEXT, source TEXT, linked_at TEXT);
CREATE TABLE projection_turns(row_id INTEGER, thread_id TEXT, turn_id TEXT,
 pending_message_id TEXT, assistant_message_id TEXT, state TEXT,
 requested_at TEXT, started_at TEXT, completed_at TEXT,
 checkpoint_turn_count INTEGER, checkpoint_ref TEXT, checkpoint_status TEXT,
 checkpoint_files_json TEXT, source_proposed_plan_thread_id TEXT,
 source_proposed_plan_id TEXT);
"""


def write_state(path):
    """A fabricated T3 state database with the real schema shapes."""
    if os.path.exists(path):
        os.remove(path)
    native = sqlite3.connect(path)
    native.executescript(T3_TABLES)

    def turn(thread, message, actor, at):
        native.execute(
            "INSERT INTO orchestration_events(stream_id, event_type,"
            " occurred_at, actor_kind, payload_json) VALUES(?,?,?,?,?)",
            (thread, "thread.turn-start-requested", at, actor,
             json.dumps({"threadId": thread, "messageId": message})))

    turn(T1, M1, "client", "2026-09-27T10:00:00Z")
    turn(T1, M2, "server", "2026-09-27T10:05:00Z")
    turn(T2, M3, "client", "2026-09-27T11:00:00Z")
    for thread, turn_id, pending in (
            (T1, U1, M1), (T1, U2, M2), (T1, U3, None), (T1, U4, M1),
            (T2, "u99999999-0000-4000-8000-000000000009", M3)):
        native.execute(
            "INSERT INTO projection_turns(thread_id, turn_id,"
            " pending_message_id, state) VALUES(?,?,?,?)",
            (thread, turn_id, pending, "completed"))

    def runtime(thread, adapter, cursor):
        native.execute(
            "INSERT INTO provider_session_runtime(thread_id, provider_name,"
            " adapter_key, resume_cursor_json) VALUES(?,?,?,?)",
            (thread, adapter, adapter, json.dumps(cursor)))

    runtime(T1, "claudeAgent",
            {"threadId": T1, "resume": CLSESS,
             "turnStartMessageIds": [U1, U2]})
    runtime(SUB, "codex", {"threadId": CXSESS})
    runtime(NESTED, "codex", {"threadId": CX2})
    runtime(T2, "opencode", {"schemaVersion": 1, "sessionId": OPSESS})
    runtime("ghost-thread-1", "ghostty", {"opaque": "cursor"})
    runtime("broken-thread-1", "claudeAgent", {"threadId": "broken-thread-1"})

    def pr(thread, repo, number, url, state):
        native.execute(
            "INSERT INTO projection_thread_pull_requests(thread_id, host,"
            " repository, number, url, source, snapshot_json)"
            " VALUES(?,?,?,?,?,?,?)",
            (thread, "github.com", repo, number, url, "agent",
             json.dumps({"state": state})))

    pr(T1, "example/alpha", 7, PR7, "merged")
    pr(T2, "example/alpha", 9, PR9, "open")
    native.execute(
        "INSERT INTO fork_thread_issue_links(thread_id, host, repository,"
        " number, url, source) VALUES(?,?,?,?,?,?)",
        (SUB, "github.com", "example/alpha", 3, ISS3, "agent"))
    native.commit()
    native.close()
    return path


def user_line(uuid, text, session=CLSESS, prompt_source="sdk",
              entrypoint="sdk-ts"):
    return json.dumps({
        "sessionId": session, "cwd": "/redacted/proj",
        "entrypoint": entrypoint,
        "version": "9.9.9", "type": "user", "uuid": uuid,
        "timestamp": "2026-09-27T10:00:00Z", "isSidechain": False,
        "promptSource": prompt_source, "promptId": "pp-" + uuid[:4],
        "turnOrigin": "sdk", "userType": "external",
        "message": {"role": "user",
                    "content": [{"type": "text", "text": text}]}})


def assistant_line(uid, model="fixture-claude-model"):
    return json.dumps({
        "sessionId": CLSESS, "cwd": "/redacted/proj", "entrypoint": "sdk-ts",
        "version": "9.9.9", "type": "assistant", "uuid": uid,
        "timestamp": "2026-09-27T10:01:00Z",
        "message": {"id": "resp-" + uid, "model": model,
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10,
                              "cache_creation_input_tokens": 1,
                              "cache_read_input_tokens": 2,
                              "output_tokens": 5},
                    "content": []}})


def write_transcript(path):
    with open(path, "w") as fh:
        fh.write(user_line(U1, "first question") + "\n")
        fh.write(assistant_line("a1") + "\n")
        fh.write(user_line(U2, "dispatched followup") + "\n")
        fh.write(assistant_line("a2") + "\n")
        fh.write(user_line(U3, "continuation without event") + "\n")
        fh.write(user_line(
            U4, "Base directory for this skill: /tmp/fixture-skills/demo")
            + "\n")
        fh.write(user_line(U5, "typed elsewhere", session="other-sess",
                           prompt_source="typed") + "\n")
    return path


def write_router_transcript(path):
    """A Router `claude -p` worker session: sdk-cli, sdk prompt, no T3 turn.

    Router workers never appear as T3 turn starts, so their uuids stay
    unlisted and the T3 mirror must never move them.
    """
    with open(path, "w") as fh:
        fh.write(user_line(U6, "worker instruction", session=ROUTER_SESS,
                           prompt_source="sdk", entrypoint="sdk-cli") + "\n")
    return path


class T3SyncTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.t3_path = write_state(os.path.join(self.tmp.name, "state.sqlite"))
        self.transcript = write_transcript(
            os.path.join(self.tmp.name, "sess.jsonl"))

    def kinds(self):
        return {r["native_id"]: (r["kind"], r["turn_id"]) for r in self.query(
            "SELECT native_id, kind, turn_id FROM submissions")}

    def test_missing_state_is_skipped_not_failed(self):
        totals = t3_adapter.sync(
            self.con, source=os.path.join(self.tmp.name, "absent.sqlite"))
        self.assertEqual(totals["sources"], 0)
        self.assertIn("skipped", totals)
        self.assertEqual(totals["failed"], [])

    def test_t3code_home_is_honored(self):
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(home, "userdata"))
        dest = os.path.join(home, "userdata", "state.sqlite")
        with open(self.t3_path, "rb") as src, open(dest, "wb") as out:
            out.write(src.read())
        with mock.patch.dict(os.environ, {"T3CODE_HOME": home}):
            self.assertEqual(t3_adapter.state_path(), dest)
            totals = t3_adapter.sync(self.con)
        self.assertEqual(totals["sources"], 1)
        self.assertEqual(totals["failed"], [])

    def test_state_is_never_written(self):
        with open(self.t3_path, "rb") as fh:
            before = hashlib.sha256(fh.read()).hexdigest()
        claude.import_claude_file(self.con, self.transcript)
        t3_adapter.sync(self.con, source=self.t3_path)
        with open(self.t3_path, "rb") as fh:
            after = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(before, after)

    def test_backfill_reclassifies_sdk_prompts_by_turn_origin(self):
        claude.import_claude_file(self.con, self.transcript)
        router_transcript = write_router_transcript(
            os.path.join(self.tmp.name, "router-sess.jsonl"))
        claude.import_claude_file(self.con, router_transcript)
        before = self.kinds()
        self.assertEqual(before["claude:" + U1][0], "synthetic")
        self.assertEqual(before["claude:" + U2][0], "synthetic")
        self.assertEqual(before["claude:" + U6][0], "synthetic")
        totals = t3_adapter.sync(self.con, source=self.t3_path)
        self.assertGreaterEqual(totals["submissions_reclassified"], 1)
        kinds = self.kinds()
        # Client turn typed by a person: genuine with its turn for joining.
        self.assertEqual(kinds["claude:" + U1][0], "genuine")
        self.assertEqual(kinds["claude:" + U1][1], "claude:" + U1)
        # Server dispatch: synthetic. No event: unknown stays synthetic.
        self.assertEqual(kinds["claude:" + U2][0], "synthetic")
        self.assertEqual(kinds["claude:" + U3][0], "synthetic")
        # Contradictory scaffolding fails closed even when T3 lists it.
        self.assertEqual(kinds["claude:" + U4][0], "scaffolding")
        # Router-style typed prompts outside T3 are unaffected.
        self.assertEqual(kinds["claude:" + U5][0], "genuine")
        # Router sdk-cli workers never appear as T3 turn starts: unlisted
        # and untouched by the mirror, before and after.
        self.assertEqual(kinds["claude:" + U6][0], "synthetic")
        self.assertIsNone(t3_adapter.turn_actor(self.con, U6))
        # Orphaned responses join the reclassified turn by ordinal.
        turns = {r["response_id"]: r["turn_id"] for r in self.query(
            "SELECT response_id, turn_id FROM responses")}
        self.assertEqual(turns["claude:resp-a1"], "claude:" + U1)
        self.assertEqual(turns["claude:resp-a2"], "claude:" + U1)
        # Backfill never invents excerpts: it never reads prompt text.
        excerpt = self.query(
            "SELECT text_excerpt FROM submissions WHERE native_id=?",
            ("claude:" + U1,))[0]["text_excerpt"]
        self.assertEqual(excerpt, "")

    def test_fresh_import_classifies_directly_with_excerpt(self):
        t3_adapter.sync(self.con, source=self.t3_path)
        claude.import_claude_file(self.con, self.transcript)
        kinds = self.kinds()
        self.assertEqual(kinds["claude:" + U1][0], "genuine")
        self.assertEqual(kinds["claude:" + U2][0], "synthetic")
        excerpt = self.query(
            "SELECT text_excerpt FROM submissions WHERE native_id=?",
            ("claude:" + U1,))[0]["text_excerpt"]
        self.assertEqual(excerpt, "first question")

    def _codex_session(self, native_id, response_id):
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES(?,?,?,?)", ("codex", "fixture:" + native_id, "x", 1.0))
        source_id = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.upsert_session(self.con, "codex:" + native_id, "codex",
                          native_id, source_id, project_dir="/redacted/proj")
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key, ordinal_num, model, semantics, input_tokens,"
            " output_tokens, total_tokens) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (response_id, source_id, "codex", "codex:" + native_id, 1,
             "fixture-codex", "codex:input_includes_cached,"
             "output_includes_reasoning", 100, 50, 150))
        return source_id

    def _opencode_session(self, native_id, response_id):
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES(?,?,?,?)", ("opencode", "fixture:" + native_id, "x", 1.0))
        source_id = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.upsert_session(self.con, "opencode:" + native_id, "opencode",
                          native_id, source_id,
                          project_dir="/redacted/proj")
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key, ordinal_num, model, semantics, input_tokens,"
            " cached_input_tokens, cache_write_input_tokens, output_tokens,"
            " reasoning_output_tokens, total_tokens)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (response_id, source_id, "opencode", "opencode:" + native_id, 1,
             "fixture-opencode",
             "opencode:input_excludes_cache,reasoning_separate",
             100, 10, 5, 50, 5, 170))
        return source_id

    def test_tree_tasks_and_shared_ownership(self):
        claude.import_claude_file(self.con, self.transcript)
        self._codex_session(CXSESS, "codex:resp-cx")
        self._codex_session(CX2, "codex:resp-cx2")
        self._opencode_session(OPSESS, "opencode:resp-op")
        t3_adapter.sync(self.con, source=self.t3_path)
        tasks = {r["task_id"]: dict(r) for r in self.query(
            "SELECT * FROM tasks")}
        self.assertEqual(tasks["example/alpha#7"]["origin"], "t3")
        self.assertEqual(tasks["example/alpha#7"]["title"], "example/alpha#7")
        self.assertEqual(tasks["example/alpha#7"]["issue_url"], PR7)
        self.assertIn("example/alpha#3", tasks)
        self.assertIn("example/alpha#9", tasks)
        bound = {(r["session_key"], r["task_id"]) for r in self.query(
            "SELECT session_key, task_id FROM session_assignments")}
        tree_sessions = {"claude:" + CLSESS, "codex:" + CXSESS,
                         "codex:" + CX2}
        # The multi-link tree (PR plus Issue across parent, child and
        # nested child threads) binds every tree session to both tasks.
        for session in tree_sessions:
            self.assertIn((session, "example/alpha#7"), bound)
            self.assertIn((session, "example/alpha#3"), bound)
        # The single-link tree attributes exclusively.
        self.assertIn(("opencode:" + OPSESS, "example/alpha#9"), bound)
        self.assertNotIn(("opencode:" + OPSESS, "example/alpha#7"), bound)
        shared = report.task_report(self.con, "example/alpha#7",
                                    schedule={"source_url":
                                              "https://example.com/p",
                                              "as_of": "2026-09-27",
                                              "currency": "USD",
                                              "unit": "USD per million tokens",
                                              "models": {}})
        self.assertEqual(shared["attributed"]["responses"], 0)
        self.assertEqual(shared["shared_joint"]["responses"], 4)
        self.assertEqual(shared["unassigned_in_scope"]["responses"], 0)
        self.assertTrue(shared["reconciles"])
        # Never divided: the same whole responses sit under both tasks.
        other = report.task_report(self.con, "example/alpha#3",
                                   schedule=shared["price_schedule"])
        self.assertEqual(
            sorted(shared["measured"]["shared_responses"]),
            sorted(other["measured"]["shared_responses"]))
        solo = report.task_report(self.con, "example/alpha#9",
                                  schedule=shared["price_schedule"])
        self.assertEqual(solo["attributed"]["responses"], 1)
        self.assertEqual(solo["shared_joint"]["responses"], 0)

    def test_changed_link_drops_stale_t3_bindings_only(self):
        self._opencode_session(OPSESS, "opencode:resp-op")
        t3_adapter.sync(self.con, source=self.t3_path)
        self.con.execute(
            "INSERT INTO tasks(task_id, project, title, created_at)"
            " VALUES('example/other#1','example/other','manual',"
            " '2026-09-27T12:00:00Z')")
        self.con.execute(
            "INSERT INTO session_assignments(session_key, task_id, evidence,"
            " created_at) VALUES(?,?,?,?)",
            ("opencode:" + OPSESS, "example/other#1", "manual",
             "2026-09-27T12:00:00Z"))
        native = sqlite3.connect(self.t3_path)
        native.execute(
            "UPDATE projection_thread_pull_requests SET number=10, url=?"
            " WHERE thread_id=?",
            ("https://github.com/example/alpha/pull/10", T2))
        native.commit()
        native.close()
        t3_adapter.sync(self.con, source=self.t3_path)
        bound = {(r["session_key"], r["task_id"]) for r in self.query(
            "SELECT session_key, task_id FROM session_assignments")}
        self.assertIn(("opencode:" + OPSESS, "example/alpha#10"), bound)
        self.assertNotIn(("opencode:" + OPSESS, "example/alpha#9"), bound)
        # Bindings this adapter did not make survive.
        self.assertIn(("opencode:" + OPSESS, "example/other#1"), bound)

    def test_outcomes_follow_pr_snapshots_and_keep_human_rows(self):
        claude.import_claude_file(self.con, self.transcript)
        self._opencode_session(OPSESS, "opencode:resp-op")
        t3_adapter.sync(self.con, source=self.t3_path)
        outcomes = {r["task_id"]: dict(r) for r in self.query(
            "SELECT * FROM outcomes")}
        self.assertEqual(outcomes["example/alpha#7"]["acceptance_state"],
                         "complete")
        self.assertEqual(outcomes["example/alpha#7"]["proof_ref"], PR7)
        # Other states stay explicit: the open PR is unknown with its link.
        self.assertEqual(outcomes["example/alpha#9"]["acceptance_state"],
                         "unknown")
        self.assertEqual(outcomes["example/alpha#9"]["proof_ref"], PR9)
        # A human verdict survives re-import byte-for-byte.
        self.con.execute(
            "UPDATE outcomes SET acceptance_state=?, proof_ref=?"
            " WHERE task_id=?", ("failed", "https://example.com/review",
                                 "example/alpha#9"))
        t3_adapter.sync(self.con, source=self.t3_path)
        row = self.query("SELECT * FROM outcomes WHERE task_id=?",
                         ("example/alpha#9",))[0]
        self.assertEqual(row["acceptance_state"], "failed")
        self.assertEqual(row["proof_ref"], "https://example.com/review")

    def test_schema_variants_do_not_crash(self):
        slim = os.path.join(self.tmp.name, "slim.sqlite")
        native = sqlite3.connect(slim)
        native.execute("CREATE TABLE provider_session_runtime(thread_id TEXT,"
                       " provider_name TEXT, adapter_key TEXT,"
                       " runtime_mode TEXT, status TEXT, last_seen_at TEXT,"
                       " resume_cursor_json TEXT, runtime_payload_json TEXT,"
                       " provider_instance_id TEXT)")
        native.execute(
            "INSERT INTO provider_session_runtime(thread_id, provider_name,"
            " adapter_key, resume_cursor_json) VALUES(?,?,?,?)",
            (T1, "claudeAgent", "claudeAgent",
             json.dumps({"threadId": T1, "resume": CLSESS})))
        native.commit()
        native.close()
        totals = t3_adapter.sync(self.con, source=slim)
        self.assertEqual(totals["failed"], [])
        self.assertNotIn("turns_mapped", totals)
        rows = self.query("SELECT * FROM t3_threads WHERE thread_id=?", (T1,))
        self.assertEqual(rows[0]["native_session"], CLSESS)

    def test_thread_roots(self):
        self.assertEqual(thread_root(T1), T1)
        self.assertEqual(thread_root(SUB), T1)
        self.assertEqual(thread_root(NESTED), T1)
        self.assertEqual(thread_root("import:codex:01a0d264-x"),
                         "import:codex:01a0d264-x")


class T3PricingTest(LedgerCase):
    RATES = {
        "fetchedAtMs": 1790516478998,
        "document": {
            "fixture-model-a": {
                "input_cost_per_token": 2e-07,
                "output_cost_per_token": 1.2e-06,
                "output_cost_per_reasoning_token": 1.2e-06,
                "cache_read_input_token_cost": 2e-08,
                "cache_creation_input_token_cost": 2.5e-07,
                "cache_creation_input_token_cost_above_1hr": 4e-07,
                "input_cost_per_token_above_200k_tokens": 4e-07,
                "output_cost_per_token_above_200k_tokens": 2.4e-06,
                "litellm_provider": "openai", "mode": "chat"},
            "us.anthropic.fixture-model-b": {
                "input_cost_per_token": 4e-06,
                "output_cost_per_token": 2e-05,
                "cache_read_input_token_cost": 2e-07,
                "cache_creation_input_token_cost": 5e-06,
                "litellm_provider": "bedrock_converse", "mode": "chat"},
            "anthropic.fixture-model-b": {
                "input_cost_per_token": 4e-06,
                "output_cost_per_token": 2e-05,
                "cache_read_input_token_cost": 2e-07,
                "cache_creation_input_token_cost": 5e-06,
                "cache_creation_input_token_cost_above_1hr": 8e-06,
                "litellm_provider": "bedrock_converse", "mode": "chat"},
            "fixture-model-image": {
                "input_cost_per_token": 1e-06,
                "output_cost_per_token": 1e-06,
                "litellm_provider": "x", "mode": "image_generation"},
        },
    }

    def rates_file(self):
        path = os.path.join(self.tmp.name, "usage-model-rates.json")
        with open(path, "w") as fh:
            json.dump(self.RATES, fh)
        return path

    def test_conversion_prefers_exact_keys_and_ttl_for_claude(self):
        from agent_observer import pricing
        schedule = pricing.load_t3_schedule(
            self.rates_file(),
            {"fixture-model-a": {"codex:input_includes_cached,"
                                 "output_includes_reasoning"},
             "fixture-model-b": {"claude:input_excludes_cache,"
                                 "output_includes_thinking"}})
        self.assertTrue(schedule["source_url"].startswith("file://"))
        entry_a = schedule["models"]["fixture-model-a"]
        self.assertAlmostEqual(entry_a["rates"]["input_tokens"], 0.2)
        # Codex semantics price cache writes flat, never TTL-ambiguous.
        self.assertEqual(entry_a["rates"]["cache_write_input_tokens"], 0.25)
        self.assertEqual(entry_a["long_context_threshold"], 200000)
        entry_b = schedule["models"]["fixture-model-b"]
        # The non-regional key wins; Claude semantics keep the TTL split.
        self.assertEqual(entry_b["t3_key"], "anthropic.fixture-model-b")
        self.assertEqual(entry_b["rates"]["cache_write_input_tokens"],
                         {"5m": 5.0, "1h": 8.0})
        self.assertNotIn("fixture-model-image", schedule["models"])

    def test_mixed_semantics_keep_claude_ttl_split(self):
        from agent_observer import pricing
        schedule = pricing.load_t3_schedule(
            self.rates_file(),
            {"fixture-model-b": {"claude:input_excludes_cache,"
                                 "output_includes_thinking",
                                 "opencode:input_excludes_cache,"
                                 "reasoning_separate"}})
        # A flat rate would price Claude 1h cache writes at the 5m rate.
        self.assertEqual(
            schedule["models"]["fixture-model-b"]["rates"]
            ["cache_write_input_tokens"], {"5m": 5.0, "1h": 8.0})

    def test_unusable_table_falls_back(self):
        from agent_observer import pricing
        self.assertIsNone(pricing.load_t3_schedule(
            os.path.join(self.tmp.name, "absent.json"), {"m": {"s"}}))
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{}")
        self.assertIsNone(pricing.load_t3_schedule(bad, {"m": {"s"}}))

    def test_default_schedule_merges_t3_over_bundled(self):
        from agent_observer import pricing
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES(?,?,?,?)", ("codex", "fixture:rates", "x", 1.0))
        source_id = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key, model, semantics, input_tokens, output_tokens,"
            " total_tokens) VALUES(?,?,?,?,?,?,?,?,?)",
            ("codex:rates-resp", source_id, "codex", "codex:rates-sess",
             "fixture-model-a",
             "codex:input_includes_cached,output_includes_reasoning",
             100, 50, 150))
        with mock.patch.dict(os.environ, {"T3CODE_HOME": self.tmp.name}):
            home_rates = os.path.join(self.tmp.name, "userdata",
                                      "usage-model-rates.json")
            os.makedirs(os.path.dirname(home_rates))
            with open(home_rates, "w") as fh:
                json.dump(self.RATES, fh)
            schedule = pricing.default_schedule(self.con)
        self.assertTrue(schedule["source_url"].startswith("file://"))
        self.assertIn("fallback_source_url", schedule)
        self.assertIn("fixture-model-a", schedule["models"])
        # Bundled coverage survives for models T3 does not name.
        self.assertIn("muse-spark-1.3-contributor-free",
                      schedule["models"])
        with mock.patch.dict(os.environ,
                             {"T3CODE_HOME": os.path.join(
                                 self.tmp.name, "empty-home")}):
            fallback = pricing.default_schedule(self.con)
        self.assertEqual(fallback["source_url"],
                         pricing.load_schedule()["source_url"])

    def test_converted_rates_price_end_to_end(self):
        from agent_observer import pricing
        schedule = pricing.load_t3_schedule(
            self.rates_file(),
            {"fixture-model-a": {"codex:input_includes_cached,"
                                 "output_includes_reasoning"}})
        cost, reason = pricing.price_response(
            {"model": "fixture-model-a",
             "semantics": "codex:input_includes_cached,"
                          "output_includes_reasoning",
             "input_tokens": 1000, "cached_input_tokens": 100,
             "cache_write_input_tokens": 10, "output_tokens": 100,
             "reasoning_output_tokens": 10, "total_tokens": 1100},
            schedule)
        self.assertEqual(reason, "priced")
        self.assertAlmostEqual(
            cost, (900 * 0.2 + 100 * 0.02 + 10 * 0.25
                   + 90 * 1.2 + 10 * 1.2) / 1_000_000.0)


class UnboundUsageTest(LedgerCase):
    def test_known_sessions_without_bound_usage_are_visible(self):
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES(?,?,?,?)", ("codex", "fixture:unbound", "x", 1.0))
        source_id = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.upsert_session(self.con, "codex:unbound-sess", "codex",
                          "unbound-sess", source_id,
                          project_dir="/redacted/proj")
        self.con.execute(
            "INSERT INTO submissions(native_id, source_id, session_key,"
            " turn_id, ordinal_num, ts, kind, text_hash, text_excerpt,"
            " is_genuine) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("codex:lonely-sub", source_id, "codex:unbound-sess",
             "codex:lonely-turn", 1, 1.0, "genuine", "h", "", 1))
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key, turn_id, ordinal_num, model, semantics,"
            " input_tokens, output_tokens, total_tokens)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("codex:lonely-resp", source_id, "codex", "codex:unbound-sess",
             None, 2, "fixture-codex",
             "codex:input_includes_cached,output_includes_reasoning",
             100, 50, 150))
        self.con.execute(
            "INSERT INTO tasks(task_id, project, origin, created_at)"
            " VALUES(?,?,?,?)", ("T-LONELY", "proj", "capture", 1.0))
        self.con.execute(
            "INSERT INTO assignments(submission_native_id, task_id,"
            " evidence, created_at) VALUES(?,?,?,?)",
            ("codex:lonely-sub", "T-LONELY", "test", 1.0))
        rep = report.task_report(self.con, "T-LONELY")
        self.assertEqual(rep["missing_assignments"], [])
        self.assertEqual(rep["attributed"]["responses"], 0)
        self.assertEqual(rep["shared_joint"]["responses"], 0)
        self.assertEqual(rep["unbound_usage"], ["codex:unbound-sess"])
        self.assertEqual(rep["coverage"]["unbound_usage"],
                         ["codex:unbound-sess"])

    def test_unbound_usage_is_a_visible_cli_failure(self):
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES(?,?,?,?)", ("codex", "fixture:unbound-cli", "x", 1.0))
        source_id = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.upsert_session(self.con, "codex:unbound-cli", "codex",
                          "unbound-cli", source_id,
                          project_dir="/redacted/proj")
        # The only genuine prompt is bound, yet the measured response sits
        # on no bound turn: sessions known, no bound usage, no missing
        # assignment to report. The old text read "missing: none", exit 0.
        self.con.execute(
            "INSERT INTO submissions(native_id, source_id, session_key,"
            " turn_id, ordinal_num, ts, kind, text_hash, text_excerpt,"
            " is_genuine) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("codex:cli-sub", source_id, "codex:unbound-cli",
             "codex:cli-turn", 1, 1.0, "genuine", "h", "", 1))
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key, turn_id, ordinal_num, model, semantics,"
            " input_tokens, output_tokens, total_tokens)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("codex:unbound-resp", source_id, "codex", "codex:unbound-cli",
             None, 2, "fixture-codex",
             "codex:input_includes_cached,output_includes_reasoning",
             100, 50, 150))
        self.con.execute(
            "INSERT INTO tasks(task_id, project, origin, created_at)"
            " VALUES(?,?,?,?)", ("T-UNBOUND", "proj", "capture", 1.0))
        self.con.execute(
            "INSERT INTO assignments(submission_native_id, task_id,"
            " evidence, created_at) VALUES(?,?,?,?)",
            ("codex:cli-sub", "T-UNBOUND", "test", 1.0))
        self.con.commit()
        self.con.close()
        path = os.path.join(self.tmp.name, "test.db")
        env = dict(os.environ, AGENT_OBSERVER_DB=path,
                   T3CODE_HOME=os.path.join(self.tmp.name, "no-t3"))
        proc = subprocess.run(
            [sys.executable, "-m", "agent_observer", "task", "show",
             "--task", "T-UNBOUND"], cwd=REPO, capture_output=True,
            text=True, env=env)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("unbound usage", proc.stdout)


if __name__ == "__main__":
    unittest.main()
