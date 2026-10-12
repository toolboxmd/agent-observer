"""Chromeria V2 import (#53): threads, lineage, links, messages, prompts.

Fixtures mirror the real ``orchestration_v2_*`` table and payload shapes
from ``~/.t3/userdata/chromeria-v2.sqlite`` (which also carries the frozen
V1 tables Chromeria seeded it from) with fabricated ids, links and text.
"""

import hashlib
import json
import os
import sqlite3
import unittest
from unittest import mock

from agent_observer import db, misuse
from agent_observer.adapters import claude
from agent_observer.adapters import t3 as t3_adapter
from agent_observer.ingest import text_hash
from tests.helpers import LedgerCase
from tests.test_t3 import (CLSESS, PR7, T1, SUB, U2, assistant_line,
                           user_line, write_state, write_transcript)

# Fabricated V2 identities.
ROOT = "v2root00-0000-4000-8000-000000000001"
CHILD = "thread:delegated-task:command%3Amcp%3Afixture%3Adelegate-task%3Achild-1"
# Real V2 ids for provider-native subagent threads exceed 200 characters.
GRAND = ("thread:provider:claudeAgent:native-thread:provider-thread%3Aprovider"
         "%3AclaudeAgent%3Anative-thread%3Apending%253Arun%253Athread%253A"
         "v2root00-0000-4000-8000-000000000001%253Aordinal%253A1%3Aa443809dc18c0a8c1")
CL2 = "c1aude-5e55-2000-000000000001"
CX9 = "01a1c0de-0000-7000-8000-000000000009"
GK1 = "01a1960c-0000-7000-8000-000000000001"
CX8 = "01a1c0de-0000-7000-8000-000000000008"
ODD = "thread:delegated-task:command%3Amcp%3Afixture%3Adelegate-task%3Aodd-1"
PR5 = "https://github.com/example/beta/pull/5"
P1, P2, P3, P4, P5, P6, P7 = ("u22222222-0000-4000-8000-00000000000%d" % n
                              for n in (1, 2, 3, 4, 5, 6, 7))

V2_TABLES = """
CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT PRIMARY KEY,
 project_id TEXT NOT NULL, title TEXT NOT NULL, default_provider TEXT NOT NULL,
 runtime_mode TEXT NOT NULL, interaction_mode TEXT NOT NULL,
 active_provider_thread_id TEXT, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, archived_at TEXT, deleted_at TEXT,
 payload_json TEXT NOT NULL, provider_instance_id TEXT);
CREATE TABLE orchestration_v2_projection_provider_threads(
 provider_thread_id TEXT PRIMARY KEY, thread_id TEXT, owner_node_id TEXT,
 provider TEXT NOT NULL, provider_session_id TEXT, status TEXT NOT NULL,
 first_run_ordinal INTEGER, last_run_ordinal INTEGER,
 updated_at TEXT NOT NULL, payload_json TEXT NOT NULL, driver TEXT,
 provider_instance_id TEXT);
CREATE TABLE orchestration_v2_projection_runs(run_id TEXT PRIMARY KEY,
 thread_id TEXT NOT NULL, ordinal INTEGER NOT NULL, provider TEXT NOT NULL,
 provider_thread_id TEXT, status TEXT NOT NULL, requested_at TEXT NOT NULL,
 completed_at TEXT, payload_json TEXT NOT NULL, provider_instance_id TEXT);
CREATE TABLE orchestration_v2_projection_messages(message_id TEXT PRIMARY KEY,
 thread_id TEXT NOT NULL, run_id TEXT, node_id TEXT, role TEXT NOT NULL,
 streaming INTEGER NOT NULL, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, payload_json TEXT NOT NULL);
"""


def write_v2(path, legacy=True):
    """A V2 database: frozen V1 tables (optional) plus V2 tables."""
    if legacy:
        write_state(path)
    native = sqlite3.connect(path)
    native.executescript(V2_TABLES)

    def thread(tid, created, parent=None, root=None, prs=(), by="agent",
               source="mcp", raw_prs=None):
        lineage = None if parent is None else {
            "parentThreadId": parent, "relationshipToParent": "subagent",
            "rootThreadId": root}
        payload = {"id": tid, "createdBy": by, "creationSource": source,
                   "title": "Fixture title never stored",
                   "lineage": lineage,
                   "pullRequests": list(prs) if raw_prs is None else raw_prs,
                   "createdAt": created}
        native.execute(
            "INSERT INTO orchestration_v2_projection_threads(thread_id,"
            " project_id, title, default_provider, runtime_mode,"
            " interaction_mode, created_at, updated_at, payload_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (tid, "p1", "Fixture title never stored", "claudeAgent",
             "full-access", "default", created, created, json.dumps(payload)))

    def provider_thread(tid, driver, native_id, updated):
        native.execute(
            "INSERT INTO orchestration_v2_projection_provider_threads("
            " provider_thread_id, thread_id, provider, status, updated_at,"
            " payload_json, driver) VALUES(?,?,?,?,?,?,?)",
            ("provider-thread:" + tid + ":" + native_id, tid, driver, "idle",
             updated,
             json.dumps({"appThreadId": tid, "driver": driver,
                         "nativeThreadRef": {"driver": driver,
                                             "nativeId": native_id,
                                             "strength": "strong"}}),
             driver))

    def message(mid, tid, at, text, by, source, sender=None, ordinal=1,
                provider_thread=None):
        run_id = f"run:{tid}:{ordinal}"
        payload = {"id": mid, "threadId": tid, "runId": run_id,
                   "role": "user", "createdBy": by, "creationSource": source,
                   "text": text, "createdAt": at}
        if sender:
            payload["senderThreadId"] = sender
        native.execute(
            "INSERT INTO orchestration_v2_projection_messages(message_id,"
            " thread_id, run_id, role, streaming, created_at, updated_at,"
            " payload_json) VALUES(?,?,?,?,?,?,?,?)",
            (mid, tid, run_id, "user", 0, at, at, json.dumps(payload)))
        native.execute(
            "INSERT OR IGNORE INTO orchestration_v2_projection_runs(run_id,"
            " thread_id, ordinal, provider, provider_thread_id, status,"
            " requested_at, payload_json) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, tid, ordinal, "claudeAgent", provider_thread,
             "completed", at,
             json.dumps({"id": run_id, "userMessageId": mid})))

    pr5 = {"host": "github.com", "repository": "example/beta", "number": 5,
           "url": PR5, "source": "agent",
           "linkedAt": "2026-10-10T09:00:00Z",
           "snapshot": {"state": "merged", "title": "never stored",
                        "headBranch": "feat/5-fixture"}}
    thread(ROOT, "2026-10-10T08:00:00Z", by="user", source="web", prs=[pr5])
    thread(CHILD, "2026-10-10T08:10:00Z", parent=ROOT, root=ROOT)
    thread(GRAND, "2026-10-10T08:20:00Z", parent=CHILD, root=ROOT)
    provider_thread(ROOT, "claudeAgent", CL2, "2026-10-10T08:00:00Z")
    provider_thread(CHILD, "codex", CX9, "2026-10-10T08:10:00Z")
    provider_thread(GRAND, "grok", GK1, "2026-10-10T08:20:00Z")
    message("vm1", ROOT, "2026-10-10T08:01:00Z", "please build it",
            "user", "web", ordinal=1)
    message("vm2", ROOT, "2026-10-10T08:30:00Z", "Delegated task finished",
            "agent", "server", ordinal=2)
    message("vm3", ROOT, "2026-10-10T08:40:00Z", "yes", "user", "mobile",
            ordinal=3)
    message("vm4", ROOT, "2026-10-10T08:50:00Z", "yes", "agent", "provider",
            ordinal=4)
    # The same text from an agent in a Codex run of the same thread is no
    # evidence about the Claude session's prompt.
    provider_thread(ROOT, "codex", CX8, "2026-10-10T08:00:30Z")
    message("vm7", ROOT, "2026-10-10T08:02:00Z", "please build it", "agent",
            "provider", ordinal=6,
            provider_thread="provider-thread:" + ROOT + ":" + CX8)
    # A client surface outside the known human ones is not a person.
    message("vm8", ROOT, "2026-10-10T08:03:00Z", "automated surface prompt",
            "user", "automation", ordinal=7)
    # A malformed pullRequests value must not lose the thread's lineage.
    thread(ODD, "2026-10-10T08:15:00Z", parent=ROOT, root=ROOT,
           raw_prs="not-a-list")
    # The parent's brief to its own child: not a cross-thread message.
    message("vm5", CHILD, "2026-10-10T08:10:01Z", "child brief", "agent",
            "mcp", sender=ROOT)
    # A child reporting to its parent: a cross-thread message.
    message("vm6", ROOT, "2026-10-10T09:00:00Z", "child report", "agent",
            "mcp", sender=CHILD, ordinal=5)
    if legacy:
        # Chromeria's legacy import copies V1 threads and messages into V2
        # with their V1 ids; legacy messages read createdBy=user from the
        # server and are no human evidence.
        thread(T1, "2026-09-27T09:00:00Z", by="system", source="server",
               prs=[{"host": "github.com", "repository": "example/alpha",
                     "number": 7, "url": PR7, "source": "agent",
                     "snapshot": {"state": "merged"}}])
        thread(SUB, "2026-09-27T09:10:00Z", parent=T1, root=T1,
               by="system", source="server")
        message("lm1", T1, "2026-09-27T10:00:00Z", "dispatched followup",
                "user", "server")
        native.execute(
            "CREATE TABLE orchestration_v2_legacy_imports(thread_id TEXT"
            " PRIMARY KEY, source_updated_at TEXT NOT NULL,"
            " shell_imported_at TEXT NOT NULL)")
        for tid in (T1, SUB):
            native.execute(
                "INSERT INTO orchestration_v2_legacy_imports VALUES(?,?,?)",
                (tid, "2026-09-27T09:00:00Z", "2026-10-08T00:00:00Z"))
    native.commit()
    native.close()
    return path


def v2_transcript(path):
    with open(path, "w") as fh:
        fh.write(user_line(P1, "please build it", session=CL2) + "\n")
        fh.write(assistant_line("b1").replace(CLSESS, CL2) + "\n")
        fh.write(user_line(P2, "Delegated task finished", session=CL2) + "\n")
        fh.write(user_line(P3, "yes", session=CL2) + "\n")
        fh.write(user_line(P4, "yes", session=CL2) + "\n")
        fh.write(user_line(P5, "unmatched prompt with an image",
                           session=CL2) + "\n")
        fh.write(user_line(P6, "child report", session=CL2) + "\n")
        fh.write(user_line(P7, "automated surface prompt", session=CL2)
                 + "\n")
    return path


class ChromeriaV2Test(LedgerCase):
    def setUp(self):
        super().setUp()
        self.v2_path = write_v2(os.path.join(self.tmp.name,
                                             "chromeria-v2.sqlite"))
        self.transcript = v2_transcript(os.path.join(self.tmp.name,
                                                     "v2.jsonl"))

    def kinds(self):
        return {r["native_id"]: dict(r) for r in self.query(
            "SELECT native_id, kind, turn_id, text_excerpt FROM user_messages")}

    def _session(self, harness, native_id):
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES(?,?,?,?)", (harness, "fixture:" + native_id, "x", 1.0))
        source_id = self.con.execute(
            "SELECT last_insert_rowid()").fetchone()[0]
        db.upsert_session(self.con, f"{harness}:{native_id}", harness,
                          native_id, source_id, project_dir="/redacted/proj")

    def test_default_source_prefers_chromeria_v2(self):
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(home, "userdata"))
        v1 = os.path.join(home, "userdata", "state.sqlite")
        open(v1, "wb").close()
        with mock.patch.dict(os.environ, {"T3CODE_HOME": home}):
            self.assertEqual(t3_adapter.state_path(), v1)
            v2 = os.path.join(home, "userdata", "chromeria-v2.sqlite")
            open(v2, "wb").close()
            self.assertEqual(t3_adapter.state_path(), v2)
        explicit = os.path.join(self.tmp.name, "explicit.sqlite")
        self.assertEqual(t3_adapter.state_path(source=explicit), explicit)

    def test_v2_is_never_written(self):
        with open(self.v2_path, "rb") as fh:
            before = hashlib.sha256(fh.read()).hexdigest()
        claude.import_claude_file(self.con, self.transcript)
        t3_adapter.sync(self.con, source=self.v2_path)
        with open(self.v2_path, "rb") as fh:
            self.assertEqual(before, hashlib.sha256(fh.read()).hexdigest())

    def test_lineage_tree_binds_every_native_session(self):
        claude.import_claude_file(self.con, self.transcript)
        self._session("codex", CX9)
        self._session("grok", GK1)
        totals = t3_adapter.sync(self.con, source=self.v2_path)
        self.assertEqual(totals["failed"], [])
        roots = {r["thread_id"]: r["root_thread_id"] for r in self.query(
            "SELECT thread_id, root_thread_id FROM t3_threads")}
        self.assertEqual(roots[CHILD], ROOT)
        self.assertEqual(roots[GRAND], ROOT)
        bound = {(r["session_key"], r["task_id"]) for r in self.query(
            "SELECT session_key, task_id FROM session_assignments")}
        for key in ("claude:" + CL2, "codex:" + CX9, "grok:" + GK1):
            self.assertIn((key, "example/beta#5"), bound)
        outcome = self.query("SELECT acceptance_state, proof_ref FROM"
                             " outcomes WHERE task_id='example/beta#5'")[0]
        self.assertEqual(outcome["acceptance_state"], "complete")
        self.assertEqual(outcome["proof_ref"], PR5)
        head = self.query("SELECT head_branch FROM t3_links"
                          " WHERE repository='example/beta'")[0]
        self.assertEqual(head["head_branch"], "feat/5-fixture")

    def test_prompt_origin_backfills_by_text_in_session(self):
        claude.import_claude_file(self.con, self.transcript)
        self.assertEqual(self.kinds()["claude:" + P1]["kind"], "synthetic")
        t3_adapter.sync(self.con, source=self.v2_path)
        kinds = self.kinds()
        self.assertEqual(kinds["claude:" + P1]["kind"], "genuine")
        self.assertEqual(kinds["claude:" + P1]["turn_id"], "claude:" + P1)
        self.assertEqual(kinds["claude:" + P1]["text_excerpt"], "")
        self.assertEqual(kinds["claude:" + P2]["kind"], "synthetic")
        # Same text from a person and an agent: unknown stays unknown.
        self.assertEqual(kinds["claude:" + P3]["kind"], "synthetic")
        self.assertEqual(kinds["claude:" + P4]["kind"], "synthetic")
        self.assertEqual(kinds["claude:" + P5]["kind"], "synthetic")
        self.assertEqual(kinds["claude:" + P6]["kind"], "synthetic")
        self.assertEqual(kinds["claude:" + P7]["kind"], "synthetic")
        # Responses after the genuine prompt join its turn.
        turn = self.query("SELECT turn_id FROM responses WHERE"
                          " response_id='claude:resp-b1'")[0]["turn_id"]
        self.assertEqual(turn, "claude:" + P1)

    def test_prompt_origin_classifies_at_import_with_excerpt(self):
        t3_adapter.sync(self.con, source=self.v2_path)
        claude.import_claude_file(self.con, self.transcript)
        kinds = self.kinds()
        self.assertEqual(kinds["claude:" + P1]["kind"], "genuine")
        self.assertEqual(kinds["claude:" + P1]["text_excerpt"],
                         "please build it")
        self.assertEqual(kinds["claude:" + P2]["kind"], "synthetic")

    def test_threads_record_who_opened_them(self):
        # Judge moments (#60): only a person-opened top-level thread makes
        # a stop or refusal there human evidence.
        claude.import_claude_file(self.con, self.transcript)
        t3_adapter.sync(self.con, source=self.v2_path)
        opened = {r["thread_id"]: r["opened_by"] for r in self.query(
            "SELECT thread_id, opened_by FROM t3_threads")}
        self.assertEqual(opened[ROOT], "client")
        self.assertEqual(opened[CHILD], "server")
        # Chromeria's legacy import marks every V1 thread system-created;
        # that is no evidence, so the first turn start decides.
        self.assertEqual(opened[T1], "client")
        self.assertIsNone(opened[SUB])

    def test_prompt_origins_cover_every_driver(self):
        # Judge moments (#60) need the origin of Codex, Grok and OpenCode
        # prompts too; the Claude session's prompt stays without evidence.
        t3_adapter.sync(self.con, source=self.v2_path)
        digest = text_hash("please build it")
        self.assertEqual(t3_adapter.prompt_actor(self.con, CX8, digest),
                         "server")
        self.assertEqual(t3_adapter.prompt_actor(self.con, CL2, digest),
                         "client")

    def test_prompt_origins_store_no_text(self):
        t3_adapter.sync(self.con, source=self.v2_path)
        dump = "\n".join(self.con.iterdump())
        for text in ("please build it", "child brief", "child report",
                     "Fixture title never stored"):
            self.assertNotIn(text, dump)

    def test_spawns_messages_and_misuse_relation(self):
        t3_adapter.sync(self.con, source=self.v2_path)
        spawns = {r["thread_id"]: (r["parent_thread_id"], r["depth"])
                  for r in self.query("SELECT * FROM t3_spawns")}
        self.assertEqual(spawns[CHILD], (ROOT, 1))
        self.assertEqual(spawns[GRAND], (CHILD, 2))
        self.assertEqual(spawns[SUB], (T1, 1))
        self.assertEqual(spawns[ODD], (ROOT, 1))
        messages = {r["message_id"]: (r["sender_thread_id"],
                                      r["target_thread_id"])
                    for r in self.query("SELECT * FROM t3_messages")}
        self.assertEqual(messages.get("vm6"), (CHILD, ROOT))
        self.assertNotIn("vm5", messages)
        result = misuse.report(self.con)
        cross = [m for m in result["cross_messages"] if m["sender"] == CHILD]
        self.assertEqual(cross[0]["relation"], "child to parent")

    def test_misuse_chain_follows_mirrored_parents(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(
            "CREATE TABLE t3_spawns(thread_id TEXT PRIMARY KEY,"
            " parent_thread_id TEXT, depth INTEGER, created_at TEXT);"
            "CREATE TABLE t3_messages(message_id TEXT PRIMARY KEY,"
            " sender_thread_id TEXT, target_thread_id TEXT, sent_at TEXT);")
        for row in ((CHILD, ROOT, 1, "2026-10-10T08:10:00Z"),
                    (GRAND, CHILD, 2, "2026-10-10T08:20:00Z"),
                    ("deep-3", GRAND, 3, "2026-10-10T08:30:00Z")):
            con.execute("INSERT INTO t3_spawns VALUES(?,?,?,?)", row)
        con.execute("INSERT INTO t3_messages VALUES(?,?,?,?)",
                    ("x1", "deep-3", ROOT, "2026-10-10T09:00:00Z"))
        result = misuse.report(con)
        self.assertEqual([t["thread_id"] for t in
                          result["deep_spawns"][0]["chain"]],
                         [ROOT, CHILD, GRAND, "deep-3"])
        self.assertEqual(result["cross_messages"][0]["relation"],
                         "to ancestor")

    def test_v1_history_and_v2_never_double_count(self):
        claude.import_claude_file(self.con, write_transcript(
            os.path.join(self.tmp.name, "v1.jsonl")))
        claude.import_claude_file(self.con, self.transcript)
        self._session("codex", CX9)
        t3_adapter.sync(self.con, source=self.v2_path)

        def snapshot():
            return {
                "assignments": sorted(tuple(r) for r in self.query(
                    "SELECT session_key, task_id FROM session_assignments")),
                "tasks": sorted(r["task_id"] for r in self.query(
                    "SELECT task_id FROM tasks")),
                "outcomes": self.query(
                    "SELECT COUNT(*) AS n FROM outcomes")[0]["n"],
                "kinds": sorted((k, v["kind"])
                                for k, v in self.kinds().items()),
            }

        first = snapshot()
        t3_adapter.sync(self.con, source=self.v2_path)
        self.assertEqual(snapshot(), first)
        # V1 history survives through the frozen tables in the V2 file.
        self.assertIn(("claude:" + CLSESS, "example/alpha#7"),
                      first["assignments"])
        self.assertEqual(first["tasks"].count("example/alpha#7"), 1)
        # The legacy copy of a server-dispatched V1 prompt stays synthetic.
        self.assertEqual(dict(first["kinds"])["claude:" + U2], "synthetic")


if __name__ == "__main__":
    unittest.main()
