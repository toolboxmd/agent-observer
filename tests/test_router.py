"""Model Router ledger adapter: jobs to tasks, invocations to attempts.

Builds a small Model Router jobs.db (jobs, invocations, readings, events
with schema_version) in a temp dir, imports it read-only, and checks the
workload mapping, session ownership, schema guard, idempotency, rollout
import, and usage reconciliation. Never invokes a model.
"""

import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_observer import db, privacy
from agent_observer.adapters import router
from agent_observer.adapters import codex as _codex
from tests.helpers import LedgerCase

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "router")
BLOCK = ("<<<AGENTSMD_PROJECT_DIRECTION_V1>>>\n"
         '{"status":"ready","instructions":{"sha256":"secret-hash"}}\n'
         "<<<END_AGENTSMD_PROJECT_DIRECTION_V1>>>")
# Fixed-length native identifiers for fixtures: git commit SHAs are 40
# hex chars, content hashes are 64 hex chars.
BASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
HEAD_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
KIT_HASH = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _router_db(path):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE jobs (request_id TEXT PRIMARY KEY, task_json TEXT,"
        " workspace TEXT, status TEXT, lane TEXT, job_kind TEXT,"
        " replay_of TEXT, planner_session_id TEXT, planner_model TEXT,"
        " planner_harness TEXT, base_commit TEXT, head_commit TEXT,"
        " block_reason TEXT, created_at TEXT, updated_at TEXT)")
    con.execute(
        "CREATE TABLE invocations (invocation_id TEXT UNIQUE,"
        " request_id TEXT, kind TEXT, stage TEXT, requested_route TEXT,"
        " policy_version TEXT, reason TEXT, terminal_class TEXT,"
        " harness_version TEXT, observed_model TEXT, observed_variant TEXT,"
        " elapsed_secs REAL, usage_json TEXT, native_ids_json TEXT,"
        " session_id TEXT, session_kind TEXT, started_at TEXT, ended_at TEXT,"
        " kit TEXT, kit_hash TEXT, direction_supply TEXT, direction_hash TEXT,"
        " skills_json TEXT, tools_json TEXT, schema_version INTEGER)")
    con.execute(
        "CREATE TABLE readings (pool TEXT, model TEXT, window TEXT,"
        " used REAL, limit_value REAL, reset_at TEXT, observed_at TEXT,"
        " source TEXT)")
    con.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, request_id TEXT,"
        " ts TEXT, kind TEXT, payload_json TEXT, schema_version INTEGER)")
    return con


def _codex_usage(total_in, total_out):
    return json.dumps({
        "input_tokens": total_in, "cached_input_tokens": 100,
        "cache_write_input_tokens": 0, "output_tokens": total_out,
        "reasoning_output_tokens": 10, "source": "codex"})


class RouterAdapterTest(LedgerCase):
    def setUp(self):
        super().setUp()
        if shutil.which("git") is None:
            self.skipTest("git is required for project derivation")
        self.state = os.path.join(self.tmp.name, "router-state")
        os.makedirs(self.state)
        self.ws_git = os.path.join(self.tmp.name, "fixture-ws")
        os.makedirs(self.ws_git)
        subprocess.run(["git", "init", "-q", self.ws_git], check=True)
        self.ws_plain = os.path.join(self.tmp.name, "plain-ws")
        os.makedirs(self.ws_plain)
        self.db_path = os.path.join(self.state, "jobs.db")
        self._build_source()
        shutil.copytree(os.path.join(FIXTURES, "kits"),
                        os.path.join(self.state, "kits"))

    def _build_source(self):
        src = _router_db(self.db_path)
        goal1 = "Short fixture goal for workload mapping"
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status, lane,"
            " job_kind, replay_of, planner_session_id, planner_model,"
            " planner_harness, base_commit, head_commit, block_reason,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("wid1", json.dumps({"issue": "toolboxmd/model-router#17",
                                 "goal": goal1, "branch": "fixture"}),
             self.ws_git, "blocked", "implementation_small", "ordinary", None,
             "plan-sess-1", "fixture-planner", "claude", BASE_COMMIT,
             HEAD_COMMIT,
             "codex_auth_failed: permission denied for /tmp/secret (exit 1)",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:05:00+00:00"))
        goal2 = BLOCK + "\nReal fixture goal text " + "y" * 200
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status, lane,"
            " job_kind, replay_of, planner_session_id, planner_model,"
            " planner_harness, base_commit, head_commit, block_reason,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("wid2", json.dumps({"issue": "toolboxmd/agent-observer#1",
                                 "goal": goal2}),
             self.ws_plain, "running", None, None, None, "plan-sess-2",
             None, None, None, None, None,
             "2026-09-23T18:10:00+00:00", "2026-09-23T18:11:00+00:00"))
        invocations = [
            ("aaa111", "wid1", "codex_dispatch", "dispatch", "luna/max",
             "2.4.0", "initial", "completed", "thread-match-aaaa",
             "codex_task_id", _codex_usage(1000, 250),
             '{"thread_id":"thread-match-aaaa"}'),
            ("plan1", "wid1", "claude_compact", "planning", "fable/max",
             "2.4.0", "compact_after_submit", "completed", "plan-sess-1",
             "planner_session_id", None, None),
            ("mm1", "wid1", "codex_dispatch", "dispatch", "luna/max",
             "2.4.0", "retry", "failed", "thread-mismatch-bbbb",
             "codex_task_id", _codex_usage(1000, 300),
             '{"thread_id":"thread-mismatch-bbbb"}'),
            ("q1", "wid2", "opencode_control", "implementation", "luna/max",
             "2.4.0", "initial", "stalled", "ses-test-1",
             "opencode_session_id",
             json.dumps({"messages": [
                 {"tokens": {"total": 100}},
                 {"tokens": {"total": 50}}], "source": "opencode"}),
             '{"session_id":"ses-test-1"}'),
            ("t-null", "wid2", "codex_resume", "dispatch", None, None, None,
             None, "thread-other", "codex_task_id", None, None),
            ("quota1", "wid2", "codex_dispatch", "dispatch", None, None, None,
             "quota", "thread-quota", "codex_task_id", None, None),
            ("cancel1", "wid2", "codex_dispatch", "dispatch", None, None,
             None, "cancelled", "thread-cancel", "codex_task_id", None, None),
            ("crash1", "wid2", "codex_dispatch", "dispatch", None, None,
             None, "crashed", "thread-crash", "codex_task_id", None, None),
        ]
        for (iid, req, kind, stage, route, policy, reason, terminal, sid,
             skind, usage, natives) in invocations:
            src.execute(
                "INSERT INTO invocations(invocation_id, request_id, kind,"
                " stage, requested_route, policy_version, reason,"
                " terminal_class, session_id, session_kind, usage_json,"
                " native_ids_json, started_at, ended_at, elapsed_secs,"
                " schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, req, kind, stage, route, policy, reason, terminal, sid,
                 skind, usage, natives, "2026-09-23T18:00:00+00:00",
                 "2026-09-23T18:01:00+00:00", 60.0, 2))
        src.execute(
            "INSERT INTO readings(pool, model, window, used, limit_value,"
            " reset_at, observed_at, source) VALUES(?,?,?,?,?,?,?,?)",
            ("codex", "fixture-model", "5h", 19.0, 100.0,
             "2026-09-24T00:00:00+00:00", "2026-09-23T18:00:00+00:00",
             "provider_reported"))
        for i, kind in enumerate(("started", "completed")):
            src.execute(
                "INSERT INTO events(request_id, ts, kind, payload_json,"
                " schema_version) VALUES(?,?,?,?,?)",
                ("wid1", "2026-09-23T18:00:00+00:00", kind, "{}", 2))
        src.commit()
        src.close()

    def _counts(self):
        return {t: self.con.execute(
            f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in (
                "router_jobs", "router_invocations", "router_readings",
                "tasks", "attempts", "outcomes", "session_assignments",
                "responses", "events")}

    def test_job_to_task_fields_and_unknown_outcome(self):
        stats = router.sync(self.con, root=self.state)
        self.assertEqual(stats["jobs"], 2)
        task = self.con.execute(
            "SELECT * FROM tasks WHERE task_id='router:wid1'").fetchone()
        self.assertEqual(task["origin"], "router")
        self.assertEqual(task["project"], "fixture-ws")
        # Fail closed: the title is the validated issue reference, never
        # the native goal.
        self.assertEqual(task["title"], "toolboxmd/model-router#17")
        self.assertEqual(task["issue_url"], "toolboxmd/model-router#17")
        self.assertNotIn("Short fixture goal", task["title"] or "")
        job = self.con.execute(
            "SELECT * FROM router_jobs WHERE request_id='wid1'").fetchone()
        self.assertEqual(job["issue"], "toolboxmd/model-router#17")
        self.assertEqual(job["block_reason"], "codex_auth_failed")
        outcome = self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone()
        # A blocked job never implies acceptance.
        self.assertEqual(outcome["acceptance_state"], "unknown")
        self.assertIn("blocked", outcome["repairs"])

    def test_title_is_issue_or_request_id_never_goal(self):
        router.sync(self.con, root=self.state)
        task1 = self.con.execute(
            "SELECT * FROM tasks WHERE task_id='router:wid1'").fetchone()
        self.assertEqual(task1["title"], "toolboxmd/model-router#17")
        task2 = self.con.execute(
            "SELECT * FROM tasks WHERE task_id='router:wid2'").fetchone()
        self.assertEqual(task2["title"], "toolboxmd/agent-observer#1")
        # No goal text lands in tasks or router_jobs.
        for row in self.con.execute(
                "SELECT title, issue_url FROM tasks"):
            blob = (row["title"] or "") + (row["issue_url"] or "")
            self.assertNotIn("Short fixture goal", blob)
            self.assertNotIn("Real fixture goal text", blob)
            self.assertNotIn("AGENTSMD", blob)
            self.assertNotIn("secret-hash", blob)

    def test_title_strips_direction_block_and_truncates(self):
        router.sync(self.con, root=self.state)
        task = self.con.execute(
            "SELECT * FROM tasks WHERE task_id='router:wid2'").fetchone()
        # The title is the validated issue reference, never the goal, so
        # direction blocks and goal text cannot leak through it.
        self.assertEqual(task["title"], "toolboxmd/agent-observer#1")
        self.assertNotIn("AGENTSMD", task["title"] or "")
        self.assertNotIn("secret-hash", task["title"] or "")
        self.assertNotIn("Real fixture goal text", task["title"] or "")
        self.assertLessEqual(len(task["title"] or ""), 200)
        # A plain directory without git stays unknown, never zero-filled.
        self.assertIsNone(task["project"])

    def test_invocations_to_attempts_and_terminal_mapping(self):
        router.sync(self.con, root=self.state)
        expected = {"aaa111": "complete", "mm1": "failed", "q1": "failed",
                    "t-null": "active", "quota1": "quota_blocked",
                    "cancel1": "cancelled", "crash1": "crashed"}
        for iid, state in expected.items():
            row = self.con.execute(
                "SELECT * FROM attempts WHERE turn_id=?",
                (f"router:{iid}",)).fetchone()
            self.assertIsNotNone(row, iid)
            self.assertEqual(row["state"], state, iid)
            self.assertEqual(row["harness"], "router", iid)
        attempt = self.con.execute(
            "SELECT * FROM attempts WHERE turn_id='router:aaa111'").fetchone()
        self.assertEqual(attempt["role"], "codex_dispatch")
        self.assertEqual(attempt["route_requested"], "luna/max")
        self.assertEqual(attempt["policy_version"], "2.4.0")
        self.assertEqual(attempt["stage"], "dispatch")
        self.assertEqual(attempt["terminal_class"], "completed")
        self.assertEqual(json.loads(attempt["usage_json"])["output_tokens"],
                         250)

    def test_session_assignments_bind_workers_not_planner(self):
        router.sync(self.con, root=self.state)
        rows = self.con.execute(
            "SELECT session_key, task_id, evidence FROM session_assignments"
            " ORDER BY session_key").fetchall()
        by_key = {r["session_key"]: r for r in rows}
        self.assertIn("codex:thread-match-aaaa", by_key)
        self.assertIn("codex:thread-mismatch-bbbb", by_key)
        self.assertIn("opencode:ses-test-1", by_key)
        bound = by_key["codex:thread-match-aaaa"]
        self.assertEqual(bound["task_id"], "router:wid1")
        self.assertEqual(bound["evidence"], "router:wid1:aaa111")
        self.assertEqual(by_key["opencode:ses-test-1"]["evidence"],
                         "router:wid2:q1")
        # The shared planner session is never bound.
        keys = " ".join(by_key)
        self.assertNotIn("plan-sess", keys)
        self.assertNotIn("planner", keys)
        for r in rows:
            self.assertRegex(r["evidence"], r"^router:\S+:\S+$")

    def test_schema_version_guard_refuses_source(self):
        for table, bad_sql, bad_args in (
                ("invocations",
                 "UPDATE invocations SET schema_version=3"
                 " WHERE invocation_id='aaa111'", ()),
                ("events",
                 "UPDATE events SET schema_version=NULL WHERE id=1", ())):
            with self.subTest(table=table):
                state = os.path.join(self.tmp.name, f"bad-{table}")
                shutil.copytree(self.state, state)
                src = sqlite3.connect(os.path.join(state, "jobs.db"))
                src.execute(bad_sql, bad_args)
                src.commit()
                src.close()
                fresh = os.path.join(self.tmp.name, f"obs-{table}.db")
                con = db.connect(fresh)
                db.init_db(con)
                try:
                    stats = router.sync(con, root=state)
                finally:
                    con.close()
                self.assertEqual(len(stats["failed"]), 1)
                # Fixed privacy category only, no ids or versions appended.
                self.assertEqual(stats["failed"][0]["error"],
                                 "unsupported_schema")
                self.assertIn(stats["failed"][0]["error"],
                              privacy.ERROR_CATEGORIES)
                con = db.connect(fresh)
                try:
                    for t in ("router_jobs", "router_invocations",
                              "router_readings", "tasks", "attempts",
                              "outcomes", "session_assignments", "responses",
                              "events"):
                        self.assertEqual(
                            con.execute(
                                f"SELECT COUNT(*) c FROM {t}").fetchone()["c"],
                            0, f"{table}/{t}")

                    self.assertEqual(stats["rollouts"], 0)
                    # Quarantine holds a fixed category plus shape-only
                    # excerpt: sorted source column names, never values.
                    guarded = con.execute(
                        "SELECT error, line_excerpt FROM import_errors"
                        " WHERE harness='router'").fetchall()
                    self.assertEqual(len(guarded), 1)
                    self.assertEqual(guarded[0]["error"],
                                     "unsupported_schema")
                    excerpt = guarded[0]["line_excerpt"] or ""
                    # Shape-only: sorted key names, never values, max 200.
                    # Invocations shape truncates before tail keys.
                    if table == "invocations":
                        self.assertIn("invocation_id", excerpt)
                        self.assertIn("request_id", excerpt)
                    else:
                        self.assertIn("schema_version", excerpt)
                    self.assertNotIn(table, excerpt)
                    self.assertNotIn("aaa111", excerpt)
                    self.assertNotIn("aaa111",
                                     stats["failed"][0]["error"])
                    self.assertNotIn("schema_guard", guarded[0]["error"])
                    self.assertNotIn("schema_guard",
                                     stats["failed"][0]["error"])
                    self.assertNotIn("schema_version=",
                                     guarded[0]["error"])
                    self.assertLessEqual(len(excerpt), 200)
                finally:
                    con.close()

    def _identities(self):
        """Natural-key row identities for every table router sync touches."""
        ids = {}
        ids["router_jobs"] = {
            r["request_id"]: r["rowid"] for r in self.con.execute(
                "SELECT request_id, rowid FROM router_jobs")}
        ids["router_invocations"] = {
            r["invocation_id"]: r["rowid"] for r in self.con.execute(
                "SELECT invocation_id, rowid FROM router_invocations")}
        ids["router_readings"] = {
            (r["pool"], r["model"], r["window"], r["observed_at"]): r["rowid"]
            for r in self.con.execute(
                "SELECT pool, model, window, observed_at, rowid"
                " FROM router_readings")}
        ids["tasks"] = {
            r["task_id"]: r["rowid"] for r in self.con.execute(
                "SELECT task_id, rowid FROM tasks")}
        ids["attempts"] = {
            (r["task_id"], r["turn_id"]): r["id"] for r in self.con.execute(
                "SELECT task_id, turn_id, id FROM attempts")}
        ids["outcomes"] = {
            r["task_id"]: r["rowid"] for r in self.con.execute(
                "SELECT task_id, rowid FROM outcomes")}
        ids["session_assignments"] = {
            (r["session_key"], r["task_id"]): r["rowid"]
            for r in self.con.execute(
                "SELECT session_key, task_id, rowid"
                " FROM session_assignments")}
        ids["sources"] = {
            (r["harness"], r["path"]): r["id"] for r in self.con.execute(
                "SELECT harness, path, id FROM sources")}
        ids["sessions"] = {
            r["session_key"]: r["rowid"] for r in self.con.execute(
                "SELECT session_key, rowid FROM sessions")}
        ids["turns"] = {
            r["turn_id"]: r["rowid"] for r in self.con.execute(
                "SELECT turn_id, rowid FROM turns")}
        ids["responses"] = {
            r["response_id"]: r["rowid"] for r in self.con.execute(
                "SELECT response_id, rowid FROM responses")}
        ids["events"] = {
            (r["session_key"], r["family"], r["native_id"]): r["id"]
            for r in self.con.execute(
                "SELECT session_key, family, native_id, id FROM events")}
        return ids

    def test_idempotent_reimport_updates_in_place(self):
        first = router.sync(self.con, root=self.state)
        self.assertEqual(first["unchanged"], 0)
        before_counts = self._counts()
        before_ids = self._identities()
        # Every natural-key table router touches gained rows, except
        # native event/submission tables the fixtures do not produce.
        for table in ("router_jobs", "router_invocations", "router_readings",
                      "tasks", "attempts", "outcomes", "session_assignments",
                      "sources", "sessions", "turns", "responses"):
            self.assertTrue(before_ids[table], table)
        again = router.sync(self.con, root=self.state)
        self.assertEqual(again["unchanged"], 1)
        self.assertEqual(self._counts(), before_counts)
        # Repeat sync keeps every row identity: no delete and reinsert.
        self.assertEqual(self._identities(), before_ids)
        # Router progress updates the same rows, never duplicates.
        src = sqlite3.connect(self.db_path)
        src.execute("UPDATE jobs SET status='succeeded',"
                    " block_reason='quota_blocked: out of capacity',"
                    " updated_at='2026-09-23T19:00:00+00:00'"
                    " WHERE request_id='wid1'")
        src.execute("UPDATE readings SET used=42.0 WHERE pool='codex'")
        src.execute("UPDATE invocations SET elapsed_secs=99.0"
                    " WHERE invocation_id='aaa111'")
        src.commit()
        src.close()
        third = router.sync(self.con, root=self.state)
        self.assertEqual(third["unchanged"], 0)
        self.assertEqual(self._counts(), before_counts)
        after_ids = self._identities()
        self.assertEqual(after_ids, before_ids)
        job = self.con.execute(
            "SELECT * FROM router_jobs WHERE request_id='wid1'").fetchone()
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["block_reason"], "quota_blocked")
        inv = self.con.execute(
            "SELECT * FROM router_invocations"
            " WHERE invocation_id='aaa111'").fetchone()
        self.assertEqual(inv["elapsed_secs"], 99.0)
        reading = self.con.execute(
            "SELECT * FROM router_readings WHERE pool='codex' AND"
            " model='fixture-model' AND window='5h'").fetchone()
        self.assertEqual(reading["used"], 42.0)
        outcome = self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone()
        self.assertEqual(outcome["acceptance_state"], "unknown")
        self.assertIn("succeeded", outcome["repairs"])
        # A human-recorded outcome survives re-import untouched.
        self.con.execute(
            "UPDATE outcomes SET acceptance_state='complete',"
            " candidate='human-candidate', proof_ref='human-proof'"
            " WHERE task_id='router:wid1'")
        self.con.commit()
        human_before = dict(self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone())
        human_id_before = self._identities()["outcomes"]["router:wid1"]
        router.sync(self.con, root=self.state)
        outcome = self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone()
        self.assertEqual(outcome["acceptance_state"], "complete")
        self.assertEqual(outcome["candidate"], "human-candidate")
        self.assertEqual(outcome["proof_ref"], "human-proof")
        self.assertEqual(dict(outcome), human_before)
        self.assertEqual(self._identities()["outcomes"]["router:wid1"],
                         human_id_before)

    def test_human_unknown_outcome_preserved_byte_for_byte(self):
        router.sync(self.con, root=self.state)
        # A human records fields on an unknown outcome.
        self.con.execute(
            "UPDATE outcomes SET candidate='human-candidate',"
            " proof_ref='human-proof', repairs='human-notes',"
            " corrections='human-fix', updated_at=1234567890.0"
            " WHERE task_id='router:wid1'")
        self.con.commit()
        before = dict(self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone())
        self.assertEqual(before["acceptance_state"], "unknown")
        before_ids = self._identities()
        # Router progress must not touch the human unknown row.
        src = sqlite3.connect(self.db_path)
        src.execute("UPDATE jobs SET status='succeeded'"
                    " WHERE request_id='wid1'")
        src.commit()
        src.close()
        router.sync(self.con, root=self.state)
        after = dict(self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone())
        self.assertEqual(after, before)
        self.assertEqual(self._identities(), before_ids)

    def test_bindings_count_distinct_session_task_pairs(self):
        # Two invocations sharing one session and task bind once.
        src = sqlite3.connect(self.db_path)
        src.execute(
            "INSERT INTO invocations(invocation_id, request_id, kind,"
            " stage, requested_route, policy_version, reason,"
            " terminal_class, session_id, session_kind, usage_json,"
            " native_ids_json, started_at, ended_at, elapsed_secs,"
            " schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("aaa112", "wid1", "codex_dispatch", "dispatch", "luna/max",
             "2.4.0", "retry", "completed", "thread-match-aaaa",
             "codex_task_id", _codex_usage(10, 5),
             '{"thread_id":"thread-match-aaaa"}',
             "2026-09-23T18:02:00+00:00", "2026-09-23T18:03:00+00:00",
             60.0, 2))
        src.commit()
        src.close()
        stats = router.sync(self.con, root=self.state)
        rows = self.con.execute(
            "SELECT session_key, task_id, evidence FROM session_assignments"
            " WHERE session_key='codex:thread-match-aaaa'"
            " AND task_id='router:wid1'").fetchall()
        self.assertEqual(len(rows), 1)
        distinct = self.con.execute(
            "SELECT COUNT(*) c FROM session_assignments").fetchone()["c"]
        self.assertEqual(stats["bindings"], distinct)
        # 7 worker invocations plus one duplicate pair stays 7 bindings.
        self.assertEqual(stats["bindings"], 7)
        self.assertEqual(stats["invocations"], 9)

    def test_shared_session_aggregate_avoids_false_conflict(self):
        # Two invocations share one session; aggregate matches native.
        src = sqlite3.connect(self.db_path)
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("wid-shared", json.dumps({"issue": "x", "goal": "shared goal"}),
             self.ws_plain, "running",
             "2026-09-23T18:10:00+00:00", "2026-09-23T18:11:00+00:00"))
        for iid in ("shared1", "shared2"):
            src.execute(
                "INSERT INTO invocations(invocation_id, request_id, kind,"
                " stage, session_id, session_kind, usage_json,"
                " native_ids_json, started_at, ended_at, schema_version)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (iid, "wid-shared", "codex_dispatch", "dispatch",
                 "thread-shared-xyz", "codex_task_id",
                 _codex_usage(1000, 250),
                 '{"thread_id":"thread-shared-xyz"}',
                 "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        src.commit()
        src.close()
        kit_dir = os.path.join(
            self.state, "kits", "wid-shared.shared",
            "sessions", "2026", "09", "23")
        os.makedirs(kit_dir)
        rollout = os.path.join(kit_dir, "rollout-shared.jsonl")
        with open(rollout, "w") as fh:
            fh.write(json.dumps({
                "ordinal": 0,
                "payload": {"cli_version": "fixture", "cwd": "/redacted/repo",
                            "id": "thread-shared-xyz",
                            "originator": "fixture"},
                "timestamp": "2026-09-23T18:00:00+00:00",
                "type": "session_meta"}) + "\n")
            for n, resp in enumerate(("resp-shared-1", "resp-shared-2")):
                fh.write(json.dumps({
                    "ordinal": n + 1,
                    "payload": {
                        "response_id": resp, "session_id": "sess-shared",
                        "thread_id": "thread-shared-xyz",
                        "turn_id": f"turn-shared-{n}",
                        "usage": {"cached_input_tokens": 0,
                                  "cache_write_input_tokens": 0,
                                  "input_tokens": 1000, "output_tokens": 250,
                                  "reasoning_output_tokens": 0,
                                  "total_tokens": 1250}},
                    "timestamp": "2026-09-23T18:00:02+00:00",
                    "type": "token_usage_record"}) + "\n")
        stats = router.sync(self.con, root=self.state)
        # Aggregate router 2500 matches native 2500: no new conflict.
        native = self.con.execute(
            "SELECT SUM(total_tokens) t FROM responses"
            " WHERE session_key='codex:thread-shared-xyz'").fetchone()["t"]
        self.assertEqual(native, 2500)
        conflicts = self.con.execute(
            "SELECT error, line_excerpt FROM import_errors"
            " WHERE harness='router' AND error='usage_conflict'").fetchall()
        # Only the pre-existing mismatch session conflicts.
        self.assertEqual(len(conflicts), 1)
        for row in conflicts:
            self.assertEqual(row["error"], "usage_conflict")
            self.assertNotIn("thread-shared-xyz", row["line_excerpt"] or "")
            self.assertNotIn("thread-mismatch-bbbb",
                             row["line_excerpt"] or "")
        self.assertEqual(stats["bindings"], 8)

    def test_symlinked_rollout_imports_once(self):
        base = os.path.join(self.tmp.name, "sym-state")
        os.makedirs(os.path.join(base, "codex-sessions", "2026", "09", "23"))
        src = _router_db(os.path.join(base, "jobs.db"))
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("wid1", json.dumps({"issue": "x", "goal": "g"}),
             self.ws_plain, "running",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:00:00+00:00"))
        src.execute(
            "INSERT INTO invocations(invocation_id, request_id, kind,"
            " stage, terminal_class, session_id, session_kind, usage_json,"
            " native_ids_json, started_at, ended_at, schema_version)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("aaa111", "wid1", "codex_dispatch", "dispatch", "completed",
             "thread-match-aaaa", "codex_task_id", _codex_usage(1000, 250),
             '{"thread_id":"thread-match-aaaa"}',
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        src.execute(
            "INSERT INTO events(request_id, ts, kind, payload_json,"
            " schema_version) VALUES(?,?,?,?,?)",
            ("wid1", "2026-09-23T18:00:00+00:00", "started", "{}", 2))
        src.commit()
        src.close()
        real_rollout = os.path.join(
            base, "codex-sessions", "2026", "09", "23",
            "rollout-2026-09-23T18-00-00-thread-match-aaaa.jsonl")
        shutil.copy(
            os.path.join(
                FIXTURES, "kits", "wid1.aaa111.dispatcher", "sessions",
                "2026", "09", "23",
                "rollout-2026-09-23T18-00-00-thread-match-aaaa.jsonl"),
            real_rollout)
        link_parent = os.path.join(base, "kits", "wid1.link", "sessions")
        os.makedirs(os.path.join(base, "kits", "wid1.link"))
        try:
            os.symlink(os.path.join(base, "codex-sessions"), link_parent)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        fresh = os.path.join(self.tmp.name, "obs-sym.db")
        con = db.connect(fresh)
        db.init_db(con)
        try:
            stats = router.sync(con, root=base)
            self.assertEqual(stats["rollouts"], 1)
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) c FROM responses"
                    " WHERE session_key='codex:thread-match-aaaa'")
                .fetchone()["c"], 1)
            # Realpath dedup: the symlinked rollout imports once under one
            # codex source row; the router ledger holds its own source row.
            self.assertEqual(
                con.execute("SELECT COUNT(*) c FROM sources"
                            " WHERE harness='codex'")
                .fetchone()["c"], 1)
            self.assertEqual(
                con.execute("SELECT COUNT(*) c FROM sources"
                            " WHERE harness='router'")
                .fetchone()["c"], 1)
            conflicts = con.execute(
                "SELECT * FROM import_errors WHERE harness='router'"
                " AND error='usage_conflict'").fetchall()
            self.assertEqual(len(conflicts), 0)
        finally:
            con.close()

    def test_kits_rollout_imported_under_matching_session_key(self):
        stats = router.sync(self.con, root=self.state)
        self.assertEqual(stats["rollouts"], 2)
        row = self.con.execute(
            "SELECT * FROM sessions"
            " WHERE session_key='codex:thread-match-aaaa'").fetchone()
        self.assertIsNotNone(row)
        rows = self.con.execute(
            "SELECT total_tokens FROM responses"
            " WHERE session_key='codex:thread-match-aaaa'").fetchall()
        self.assertEqual([r["total_tokens"] for r in rows], [1250])
        # Aggregate router total for the matching session equals native,
        # so only the mismatch session conflicts.
        native = 1250
        self.assertEqual(
            self.con.execute(
                "SELECT SUM(total_tokens) t FROM responses"
                " WHERE session_key='codex:thread-match-aaaa'")
            .fetchone()["t"], native)
        errors = self.con.execute(
            "SELECT error, line_excerpt FROM import_errors"
            " WHERE harness='router'").fetchall()
        for e in errors:
            self.assertIn(e["error"], privacy.ERROR_CATEGORIES)
            blob = (e["error"] or "") + (e["line_excerpt"] or "")
            self.assertNotIn("thread-match-aaaa", blob)
            self.assertNotIn("thread-mismatch-bbbb", blob)
        conflicts = [e for e in errors if e["error"] == "usage_conflict"]
        self.assertEqual(len(conflicts), 1)

    def test_usage_mismatch_recorded_without_router_responses(self):
        router.sync(self.con, root=self.state)
        errors = self.con.execute(
            "SELECT error, line_excerpt FROM import_errors"
            " WHERE harness='router' AND error='usage_conflict'").fetchall()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"], "usage_conflict")
        excerpt = errors[0]["line_excerpt"] or ""
        # Shape-only: sorted key names, never values; invocations shape
        # truncates to 200 chars, so assert early keys that survive.
        self.assertIn("invocation_id", excerpt)
        self.assertIn("request_id", excerpt)
        self.assertNotIn("thread-mismatch-bbbb", excerpt)
        self.assertNotIn("thread-mismatch-bbbb", errors[0]["error"])
        self.assertNotIn("1250", excerpt)
        self.assertNotIn("1300", excerpt)
        self.assertNotIn("1300", errors[0]["error"])
        self.assertNotIn("usage_mismatch", errors[0]["error"])
        self.assertLessEqual(len(excerpt), 200)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) c FROM responses"
                             " WHERE harness='router'").fetchone()["c"], 0)

    def test_source_stays_read_only_and_errors_stay_sanitized(self):
        with open(self.db_path, "rb") as fh:
            digest_before = hashlib.sha256(fh.read()).hexdigest()
        router.sync(self.con, root=self.state)
        with open(self.db_path, "rb") as fh:
            digest_after = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(digest_before, digest_after)
        for row in self.con.execute("SELECT error, line_excerpt"
                                    " FROM import_errors"):
            # Fixed categories only, shape-only excerpts.
            self.assertIn(row["error"], privacy.ERROR_CATEGORIES)
            self.assertEqual(row["error"], row["error"][:200])
            blob = (row["error"] or "") + (row["line_excerpt"] or "")
            self.assertNotIn("Short fixture goal", blob)
            self.assertNotIn("/tmp/secret", blob)
            self.assertNotIn("secret-hash", blob)
            self.assertNotIn("thread-match-aaaa", blob)
            self.assertNotIn("thread-mismatch-bbbb", blob)
            self.assertNotIn("aaa111", blob)
            self.assertLessEqual(len(row["line_excerpt"] or ""), 200)
        for row in self.con.execute(
                "SELECT block_reason FROM router_jobs"):
            self.assertNotIn(":", row["block_reason"] or "")
            self.assertNotIn(" ", row["block_reason"] or "")

    def test_readings_snapshot_in_place(self):
        router.sync(self.con, root=self.state)
        row = self.con.execute(
            "SELECT *, rowid FROM router_readings WHERE pool='codex' AND"
            " model='fixture-model' AND window='5h'").fetchone()
        self.assertEqual(row["used"], 19.0)
        self.assertEqual(row["source"], "provider_reported")
        rowid_before = row["rowid"]
        src = sqlite3.connect(self.db_path)
        src.execute("UPDATE readings SET used=42.0 WHERE pool='codex'")
        src.commit()
        src.close()
        router.sync(self.con, root=self.state)
        rows = self.con.execute(
            "SELECT *, rowid FROM router_readings WHERE pool='codex' AND"
            " model='fixture-model' AND window='5h'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["used"], 42.0)
        self.assertEqual(rows[0]["rowid"], rowid_before)


    def test_arbitrary_goal_and_raw_json_never_stored(self):
        marker_goal = "MARKER-GOAL free text that must never persist 987654"
        src = sqlite3.connect(self.db_path)
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("wid-raw", json.dumps({"issue": "toolboxmd/agent-observer#9",
                                    "goal": marker_goal}),
             self.ws_plain, "running",
             "2026-09-23T18:10:00+00:00", "2026-09-23T18:11:00+00:00"))
        src.execute(
            "INSERT INTO invocations(invocation_id, request_id, kind,"
            " stage, reason, terminal_class, session_id, session_kind,"
            " usage_json, native_ids_json, skills_json, tools_json,"
            " started_at, ended_at, schema_version)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("raw1", "wid-raw", "codex_dispatch", "dispatch",
             "initial", "completed", "thread-raw-1", "codex_task_id",
             json.dumps({"input_tokens": 10, "output_tokens": 5,
                         "source": "codex", "secret": "must-drop"}),
             '{"thread_id":"thread-raw-1","extra":"drop-me"}',
             '["skill-a"]', '{"tool":"x"}',
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        src.commit()
        src.close()
        router.sync(self.con, root=self.state)
        task = self.con.execute(
            "SELECT * FROM tasks WHERE task_id='router:wid-raw'").fetchone()
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "toolboxmd/agent-observer#9")
        self.assertNotIn("MARKER-GOAL", task["title"] or "")
        inv = self.con.execute(
            "SELECT * FROM router_invocations"
            " WHERE invocation_id='raw1'").fetchone()
        self.assertIsNotNone(inv)
        self.assertIsNone(inv["native_ids_json"])
        self.assertIsNone(inv["skills_json"])
        self.assertIsNone(inv["tools_json"])
        stored_usage = json.loads(inv["usage_json"])
        self.assertEqual(stored_usage["output_tokens"], 5)
        self.assertNotIn("source", stored_usage)
        self.assertNotIn("secret", stored_usage)
        attempt = self.con.execute(
            "SELECT * FROM attempts WHERE turn_id='router:raw1'").fetchone()
        self.assertIsNotNone(attempt)
        attempt_usage = json.loads(attempt["usage_json"])
        self.assertNotIn("source", attempt_usage)
        # No table holding the projection keeps the marker or raw blobs.
        for table, cols in (
                ("tasks", ["title", "issue_url"]),
                ("router_jobs", ["status", "issue", "block_reason"]),
                ("router_invocations",
                 ["reason", "usage_json", "requested_route"]),
                ("attempts", ["reason", "usage_json", "route_requested"])):
            for row in self.con.execute(f"SELECT * FROM {table}"):
                blob = " ".join(str(row[c] or "") for c in cols)
                self.assertNotIn("MARKER-GOAL", blob, table)
                self.assertNotIn("must-drop", blob, table)
                self.assertNotIn("drop-me", blob, table)
                self.assertNotIn("skill-a", blob, table)

    def test_invalid_reason_becomes_null_while_valid_reasons_stay(self):
        src = sqlite3.connect(self.db_path)
        src.execute(
            "INSERT INTO invocations(invocation_id, request_id, kind,"
            " stage, reason, terminal_class, session_id, session_kind,"
            " started_at, ended_at, schema_version)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("badreason1", "wid1", "codex_dispatch", "dispatch",
             "do it because I said so", "completed", "thread-badreason",
             "codex_task_id",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        src.commit()
        src.close()
        router.sync(self.con, root=self.state)
        bad = self.con.execute(
            "SELECT * FROM router_invocations"
            " WHERE invocation_id='badreason1'").fetchone()
        self.assertIsNotNone(bad)
        self.assertIsNone(bad["reason"])
        bad_attempt = self.con.execute(
            "SELECT * FROM attempts"
            " WHERE turn_id='router:badreason1'").fetchone()
        self.assertIsNotNone(bad_attempt)
        self.assertIsNone(bad_attempt["reason"])
        good = self.con.execute(
            "SELECT reason FROM router_invocations"
            " WHERE invocation_id='aaa111'").fetchone()
        self.assertEqual(good["reason"], "initial")
        good_attempt = self.con.execute(
            "SELECT reason FROM attempts"
            " WHERE turn_id='router:aaa111'").fetchone()
        self.assertEqual(good_attempt["reason"], "initial")

    def test_closed_projections_match_router_contract(self):
        # Review finding 2: every closed projection derives from Model
        # Router's own contract (installed runner plus the live ledger's
        # distinct values), never guesses. Each valid value persists;
        # anything else fails closed to NULL.
        src = sqlite3.connect(self.db_path)
        statuses = ["pending", "running", "question_pending", "blocked",
                    "cancelling", "succeeded", "failed", "cancelled"]
        lanes = ["implementation_default", "implementation_small",
                 "implementation_hard"]
        job_kinds = ["ordinary", "experiment", "replay"]
        for i, status in enumerate(statuses):
            src.execute(
                "INSERT INTO jobs(request_id, task_json, workspace, status,"
                " lane, job_kind, planner_harness,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"wid-status-{i}",
                 json.dumps({"issue": "toolboxmd/agent-observer#1"}),
                 self.ws_plain, status, lanes[i % len(lanes)],
                 job_kinds[i % len(job_kinds)], "claude",
                 "2026-09-23T18:10:00+00:00", "2026-09-23T18:11:00+00:00"))
        # Observer words and raw lane aliases are never router values.
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status,"
            " lane, job_kind, planner_harness,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("wid-invalid",
             json.dumps({"issue": "toolboxmd/agent-observer#1"}),
             self.ws_plain, "complete", "default", "unknown-kind", "codex",
             "2026-09-23T18:10:00+00:00", "2026-09-23T18:11:00+00:00"))
        kinds = ["codex_dispatch", "codex_resume", "claude_callback",
                 "claude_compact", "opencode_control", "opencode_serve",
                 "grok_control"]
        stages = ["dispatch", "planning", "implementation"]
        reasons = ["initial", "resume", "planner_question",
                   "compact_after_submit", "correction", "escalation",
                   "pool_move", "lateral", "larger_context", "stalled_retry",
                   "dispatch_stalled", "dispatch_exhausted",
                   "preflight_exhausted", "preflight_degraded",
                   "preflight_one_turn", "preflight_concurrent",
                   "pool_move_concurrent", "lateral_concurrent",
                   "larger_context_concurrent", "correction_concurrent",
                   "escalation_concurrent",
                   "preflight_exhausted_concurrent",
                   "preflight_degraded_concurrent",
                   "preflight_one_turn_concurrent"]
        session_kinds = ["codex_task_id", "opencode_session_id",
                         "planner_session_id", "grok_session_id"]
        terminals = {"completed": "complete", "failed": "failed",
                     "timeout": "failed", "overloaded": "failed",
                     "stalled": "failed", "context": "failed",
                     "hard_error": "failed", "cancelled": "cancelled",
                     "crashed": "crashed", "quota": "quota_blocked"}
        for i, reason in enumerate(reasons):
            terminal = sorted(terminals)[i % len(terminals)]
            skind = session_kinds[i % len(session_kinds)]
            src.execute(
                "INSERT INTO invocations(invocation_id, request_id, kind,"
                " stage, reason, terminal_class, session_id, session_kind,"
                " started_at, ended_at, schema_version)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"cov-{i:02d}", "wid-status-0", kinds[i % len(kinds)],
                 stages[i % len(stages)], reason, terminal,
                 f"thread-cov-{i:02d}", skind,
                 "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        # An invalid closed value in every projection fails closed; a
        # non-NULL but unrecognized terminal class is unknown, never
        # active.
        src.execute(
            "INSERT INTO invocations(invocation_id, request_id, kind,"
            " stage, reason, terminal_class, session_id, session_kind,"
            " started_at, ended_at, schema_version)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("cov-bad", "wid-status-0", "smoke_signals", "review", "retry",
             "bogus-class", "thread-cov-bad", "claude_session_id",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        src.commit()
        src.close()
        router.sync(self.con, root=self.state)
        for i, status in enumerate(statuses):
            with self.subTest(status=status):
                job = self.con.execute(
                    "SELECT * FROM router_jobs WHERE request_id=?",
                    (f"wid-status-{i}",)).fetchone()
                self.assertIsNotNone(job)
                self.assertEqual(job["status"], status)
                self.assertEqual(job["lane"], lanes[i % len(lanes)])
                self.assertEqual(job["job_kind"],
                                 job_kinds[i % len(job_kinds)])
                self.assertEqual(job["planner_harness"], "claude")
                outcome = self.con.execute(
                    "SELECT * FROM outcomes WHERE task_id=?",
                    (f"router:wid-status-{i}",)).fetchone()
                self.assertEqual(outcome["acceptance_state"], "unknown")
                self.assertEqual(outcome["repairs"],
                                 f"router_status:{status}")
        bad_job = self.con.execute(
            "SELECT * FROM router_jobs WHERE request_id='wid-invalid'"
            ).fetchone()
        self.assertIsNotNone(bad_job)
        self.assertIsNone(bad_job["status"])
        self.assertIsNone(bad_job["lane"])
        self.assertIsNone(bad_job["job_kind"])
        self.assertIsNone(bad_job["planner_harness"])
        bad_outcome = self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid-invalid'"
            ).fetchone()
        self.assertEqual(bad_outcome["acceptance_state"], "unknown")
        self.assertIsNone(bad_outcome["repairs"])
        for i, reason in enumerate(reasons):
            with self.subTest(reason=reason):
                inv = self.con.execute(
                    "SELECT * FROM router_invocations WHERE invocation_id=?",
                    (f"cov-{i:02d}",)).fetchone()
                self.assertIsNotNone(inv)
                self.assertEqual(inv["reason"], reason)
                self.assertEqual(inv["kind"], kinds[i % len(kinds)])
                self.assertEqual(inv["stage"], stages[i % len(stages)])
                self.assertEqual(inv["session_kind"],
                                 session_kinds[i % len(session_kinds)])
                terminal = sorted(terminals)[i % len(terminals)]
                self.assertEqual(inv["terminal_class"], terminal)
                attempt = self.con.execute(
                    "SELECT * FROM attempts WHERE turn_id=?",
                    (f"router:cov-{i:02d}",)).fetchone()
                self.assertIsNotNone(attempt)
                self.assertEqual(attempt["reason"], reason)
                self.assertEqual(attempt["terminal_class"], terminal)
                self.assertEqual(attempt["state"], terminals[terminal])
        bad = self.con.execute(
            "SELECT * FROM router_invocations WHERE invocation_id='cov-bad'"
            ).fetchone()
        self.assertIsNotNone(bad)
        self.assertIsNone(bad["kind"])
        self.assertIsNone(bad["stage"])
        self.assertIsNone(bad["reason"])
        self.assertIsNone(bad["terminal_class"])
        self.assertIsNone(bad["session_kind"])
        bad_attempt = self.con.execute(
            "SELECT * FROM attempts WHERE turn_id='router:cov-bad'"
            ).fetchone()
        self.assertIsNotNone(bad_attempt)
        self.assertIsNone(bad_attempt["terminal_class"])
        # Unknown but present: the explicit unknown state, never active.
        self.assertEqual(bad_attempt["state"], "unknown")
        # Genuinely absent: still active (the pre-existing t-null row).
        missing = self.con.execute(
            "SELECT * FROM attempts WHERE turn_id='router:t-null'"
            ).fetchone()
        self.assertEqual(missing["state"], "active")

    def test_attempt_state_wrong_typed_terminal_is_unknown(self):
        # Fail-closed edge: the helper reads the raw ledger class, so an
        # unhashable or otherwise wrong-typed value maps to the explicit
        # unknown state instead of raising, and never to active. None
        # stays active and valid mappings are preserved.
        self.assertEqual(router._attempt_state(None), "active")
        for bad in ([], {}, ["failed"], 0, 123, b"failed", True, ""):
            with self.subTest(bad=bad):
                self.assertEqual(router._attempt_state(bad), "unknown")
        self.assertEqual(router._attempt_state("completed"), "complete")
        self.assertEqual(router._attempt_state("cancelled"), "cancelled")
        self.assertEqual(router._attempt_state("crashed"), "crashed")
        self.assertEqual(router._attempt_state("quota"), "quota_blocked")
        self.assertEqual(router._attempt_state("timeout"), "failed")
        self.assertEqual(router._attempt_state("bogus-class"), "unknown")

    def test_router_child_rollout_stores_no_excerpt(self):
        # Review finding 1 confirmation (the core importer fix already
        # landed underneath this branch): a router-owned child/worker
        # Codex rollout carrying a genuine-looking user message keeps no
        # submission excerpt.
        kit_dir = os.path.join(
            self.state, "kits", "wid-child.child", "sessions",
            "2026", "09", "23")
        os.makedirs(kit_dir)
        thread = "thread-router-child-worker"
        session = "sess-router-child-01"
        usage = {"cache_write_input_tokens": 0, "cached_input_tokens": 500,
                 "input_tokens": 700, "output_tokens": 70,
                 "reasoning_output_tokens": 7, "total_tokens": 770}
        records = [
            {"ordinal": 0,
             "payload": {"cli_version": "0.155.0",
                         "cwd": "/redacted/workspace",
                         "session_id": session, "thread_source": "user"},
             "timestamp": "2026-09-23T18:00:00.000Z",
             "type": "session_meta"},
            {"ordinal": 1,
             "payload": {"cwd": "/redacted/workspace", "effort": "medium",
                         "model": "gpt-6-fixture",
                         "root_turn_id": "turn-router-child",
                         "turn_id": "turn-router-child"},
             "timestamp": "2026-09-23T18:00:01.000Z",
             "type": "turn_context"},
            {"ordinal": 2,
             "payload": {
                 "content": [{"text": "Please summarize the worker findings"
                                      " for the status report.",
                              "type": "input_text"}],
                 "id": "msg-router-child-01",
                 "internal_chat_message_metadata_passthrough": {
                     "content_item_kinds": ["user.text"],
                     "turn_id": "turn-router-child"},
                 "role": "user", "type": "message"},
             "timestamp": "2026-09-23T18:00:02.000Z",
             "type": "response_item"},
            {"ordinal": 3,
             "payload": {"response_id": "resp-router-child-1",
                         "root_turn_id": "turn-router-child",
                         "session_id": session, "thread_id": thread,
                         "thread_token_usage": usage,
                         "turn_id": "turn-router-child",
                         "turn_token_usage": usage, "usage": usage},
             "timestamp": "2026-09-23T18:00:03.000Z",
             "type": "token_usage_record"},
        ]
        with open(os.path.join(
                kit_dir,
                "rollout-2026-09-23T18-00-00-thread-router-child.jsonl"),
                "w") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
        router.sync(self.con, root=self.state)
        rows = self.con.execute(
            "SELECT kind, is_genuine, text_excerpt FROM submissions"
            " WHERE session_key=?", (f"codex:{thread}",)).fetchall()
        # The genuine-looking child message imported, but stores nothing.
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["kind"], "genuine")
            self.assertEqual(row["text_excerpt"], "")

    def test_missing_and_dangling_ids_quarantined_later_rows_import(self):
        src = sqlite3.connect(self.db_path)
        # Empty request id: passes SQLite, fails closed validation.
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("", json.dumps({"issue": "toolboxmd/agent-observer#1",
                             "goal": "bad job"}),
             self.ws_plain, "running",
             "2026-09-23T18:10:00+00:00", "2026-09-23T18:11:00+00:00"))
        # Invocation without an invocation id.
        src.execute(
            "INSERT INTO invocations(request_id, kind, stage, reason,"
            " session_id, session_kind, started_at, ended_at,"
            " schema_version) VALUES(?,?,?,?,?,?,?,?,?)",
            ("wid1", "codex_dispatch", "dispatch", "initial",
             "thread-null-iid", "codex_task_id",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        # Invocation with a missing request id.
        src.execute(
            "INSERT INTO invocations(invocation_id, kind, stage,"
            " session_id, session_kind, started_at, ended_at,"
            " schema_version) VALUES(?,?,?,?,?,?,?,?)",
            ("noreq1", "codex_dispatch", "dispatch", "thread-noreq",
             "codex_task_id",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        # Invocation with a dangling request id.
        src.execute(
            "INSERT INTO invocations(invocation_id, request_id, kind,"
            " stage, session_id, session_kind, started_at, ended_at,"
            " schema_version) VALUES(?,?,?,?,?,?,?,?,?)",
            ("dangling1", "wid-missing", "codex_dispatch", "dispatch",
             "thread-dangling", "codex_task_id",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        # A later valid job and invocation must still import.
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("wid-late", json.dumps({"issue": "toolboxmd/agent-observer#2"}),
             self.ws_plain, "running",
             "2026-09-23T18:10:00+00:00", "2026-09-23T18:11:00+00:00"))
        src.execute(
            "INSERT INTO invocations(invocation_id, request_id, kind,"
            " stage, reason, session_id, session_kind, started_at,"
            " ended_at, schema_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("late1", "wid-late", "codex_dispatch", "dispatch", "retry",
             "thread-late-1", "codex_task_id",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        src.commit()
        src.close()
        stats = router.sync(self.con, root=self.state)
        # The late valid rows imported despite earlier malformed rows.
        self.assertIsNotNone(self.con.execute(
            "SELECT * FROM tasks WHERE task_id='router:wid-late'").fetchone())
        self.assertIsNotNone(self.con.execute(
            "SELECT * FROM attempts"
            " WHERE turn_id='router:late1'").fetchone())
        # Malformed rows never land under their natural keys.
        self.assertIsNone(self.con.execute(
            "SELECT * FROM router_invocations"
            " WHERE invocation_id='dangling1'").fetchone())
        self.assertIsNone(self.con.execute(
            "SELECT * FROM router_invocations"
            " WHERE invocation_id='noreq1'").fetchone())
        self.assertIsNone(self.con.execute(
            "SELECT * FROM attempts WHERE turn_id='router:dangling1'")
            .fetchone())
        quarantined = self.con.execute(
            "SELECT error, line_excerpt FROM import_errors"
            " WHERE harness='router' AND error='missing_id'").fetchall()
        # Shape-only dedup collapses identical shapes: one jobs shape and
        # one invocations shape, so assert the requirement (every bad row
        # skipped, quarantine recorded, later rows import) not the row
        # count.
        self.assertGreaterEqual(len(quarantined), 2)
        for row in quarantined:
            self.assertEqual(row["error"], "missing_id")
            excerpt = row["line_excerpt"] or ""
            self.assertLessEqual(len(excerpt), 200)
            self.assertNotIn("wid-missing", excerpt)
            self.assertNotIn("dangling1", excerpt)
        self.assertGreaterEqual(stats["malformed"], 2)

    def test_shared_privacy_helpers_and_no_private_copies(self):
        self.assertFalse(hasattr(router, "ERROR_CATEGORIES"))
        self.assertFalse(hasattr(router, "_shape_from_record"))
        self.assertFalse(hasattr(router, "_shape_from_columns"))
        for name in ("ERROR_CATEGORIES", "_shape_from_record",
                     "_shape_from_columns", "EVENT_DETAIL_ALLOWLIST"):
            self.assertNotIn(name, dir(router))
        import inspect
        source = inspect.getsource(router)
        self.assertIn("privacy.error_category", source)
        self.assertIn("privacy.line_excerpt", source)
        self.assertIn("privacy.filter_detail", source)
        self.assertIn("privacy.filter_target", source)
        # Unknown categories map to the shared fixed fallback.
        router.sync(self.con, root=self.state)
        before = self.con.execute(
            "SELECT COUNT(*) c FROM import_errors").fetchone()["c"]
        router._record_error(self.con, self.db_path, "exploded_bogus", "")
        row = self.con.execute(
            "SELECT error FROM import_errors ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["error"], privacy.ERROR_FALLBACK)
        self.assertEqual(row["error"], "import_error")
        after = self.con.execute(
            "SELECT COUNT(*) c FROM import_errors").fetchone()["c"]
        self.assertEqual(after, before + 1)
        # Shape excerpts hold only sorted key names, never values.
        excerpt = router._shape_excerpt(
            {"zebra": "secret-value", "apple": "other"})
        self.assertEqual(excerpt, "apple,zebra")
        self.assertEqual(router._shape_excerpt([1, 2, 3]), "")
        self.assertEqual(router._shape_excerpt("not json"), "")

    def test_blank_human_unknown_outcome_preserved(self):
        router.sync(self.con, root=self.state)
        # A human leaves a blank unknown outcome: no candidate, proof,
        # repairs or corrections.
        self.con.execute(
            "UPDATE outcomes SET candidate=NULL, proof_ref=NULL,"
            " repairs=NULL, corrections=NULL, acceptance_state='unknown',"
            " updated_at=1234567890.0 WHERE task_id='router:wid1'")
        self.con.commit()
        before = dict(self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone())
        self.assertEqual(before["acceptance_state"], "unknown")
        self.assertIsNone(before["repairs"])
        before_id = self.con.execute(
            "SELECT rowid FROM outcomes"
            " WHERE task_id='router:wid1'").fetchone()["rowid"]
        src = sqlite3.connect(self.db_path)
        src.execute("UPDATE jobs SET status='succeeded'"
                    " WHERE request_id='wid1'")
        src.commit()
        src.close()
        router.sync(self.con, root=self.state)
        after = dict(self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone())
        self.assertEqual(after, before)
        after_id = self.con.execute(
            "SELECT rowid FROM outcomes"
            " WHERE task_id='router:wid1'").fetchone()["rowid"]
        self.assertEqual(after_id, before_id)

    def test_colliding_human_repairs_prefix_preserved(self):
        router.sync(self.con, root=self.state)
        self.con.execute(
            "UPDATE outcomes SET candidate=NULL, proof_ref=NULL,"
            " repairs='router_status:forged by human', corrections=NULL,"
            " acceptance_state='unknown', updated_at=1234567890.0"
            " WHERE task_id='router:wid1'")
        self.con.commit()
        before = dict(self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone())
        src = sqlite3.connect(self.db_path)
        src.execute("UPDATE jobs SET status='succeeded'"
                    " WHERE request_id='wid1'")
        src.commit()
        src.close()
        router.sync(self.con, root=self.state)
        after = dict(self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone())
        self.assertEqual(after, before)
        self.assertEqual(after["repairs"], "router_status:forged by human")

    def test_router_owned_rollouts_go_through_codex_importer(self):
        calls = []
        original = _codex.import_codex_file

        def spy(con, path, full=False):
            calls.append((path, full))
            return original(con, path, full=full)

        router._codex.import_codex_file = spy
        try:
            stats = router.sync(self.con, root=self.state)
        finally:
            router._codex.import_codex_file = original
        self.assertEqual(stats["rollouts"], 2)
        self.assertEqual(len(calls), 2)
        # The sync full flag propagates to the Codex importer.
        for _, full in calls:
            self.assertFalse(full)
        paths = sorted(p for p, _ in calls)
        self.assertTrue(any("thread-match-aaaa" in p for p in paths))
        calls.clear()
        router._codex.import_codex_file = spy
        try:
            router.sync(self.con, root=self.state, full=True)
        finally:
            router._codex.import_codex_file = original
        self.assertEqual(len(calls), 2)
        for _, full in calls:
            self.assertTrue(full)
        paths = sorted(p for p, _ in calls)
        self.assertTrue(any("thread-match-aaaa" in p for p in paths))
        row = self.con.execute(
            "SELECT * FROM sessions"
            " WHERE session_key='codex:thread-match-aaaa'").fetchone()
        self.assertIsNotNone(row)


    def test_block_reason_closed_set_and_direction_supply_enum(self):
        src = sqlite3.connect(self.db_path)
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status,"
            " block_reason, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            ("wid-evil", json.dumps({"issue": "toolboxmd/agent-observer#3"}),
             self.ws_plain, "blocked",
             "evil_class: rm -rf /tmp/x <script>alert(1)</script>",
             "2026-09-23T18:10:00+00:00", "2026-09-23T18:11:00+00:00"))
        src.execute(
            "INSERT INTO invocations(invocation_id, request_id, kind,"
            " stage, direction_supply, direction_hash, session_id,"
            " session_kind, started_at, ended_at, schema_version)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("evil1", "wid-evil", "codex_dispatch", "dispatch",
             "pwned_inline_evil", "not-a-hash!!", "thread-evil-1",
             "codex_task_id",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        src.execute(
            "INSERT INTO invocations(invocation_id, request_id, kind,"
            " stage, direction_supply, direction_hash, kit_hash,"
            " session_id, session_kind, started_at, ended_at,"
            " schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("gooddir1", "wid-evil", "codex_dispatch", "dispatch",
             "hook", KIT_HASH, KIT_HASH, "thread-evil-2", "codex_task_id",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        src.commit()
        src.close()
        router.sync(self.con, root=self.state)
        job = self.con.execute(
            "SELECT * FROM router_jobs WHERE request_id='wid-evil'").fetchone()
        self.assertIsNotNone(job)
        self.assertIsNone(job["block_reason"])
        evil = self.con.execute(
            "SELECT * FROM router_invocations"
            " WHERE invocation_id='evil1'").fetchone()
        self.assertIsNotNone(evil)
        self.assertIsNone(evil["direction_supply"])
        self.assertIsNone(evil["direction_hash"])
        good = self.con.execute(
            "SELECT * FROM router_invocations"
            " WHERE invocation_id='gooddir1'").fetchone()
        self.assertIsNotNone(good)
        self.assertEqual(good["direction_supply"], "hook")
        self.assertEqual(good["direction_hash"], KIT_HASH)
        self.assertEqual(good["kit_hash"], KIT_HASH)
        # The malicious text persists nowhere in the ledger.
        blob = ""
        for table in ("router_jobs", "router_invocations", "attempts"):
            for row in self.con.execute(f"SELECT * FROM {table}"):
                blob += " ".join(str(row[c] or "") for c in row.keys())
        self.assertNotIn("evil_class", blob)
        self.assertNotIn("rm -rf", blob)
        self.assertNotIn("alert(1)", blob)
        self.assertNotIn("pwned_inline_evil", blob)
        self.assertNotIn("not-a-hash", blob)
        # Known classes still project; valid commit SHAs are preserved.
        legit = self.con.execute(
            "SELECT * FROM router_jobs WHERE request_id='wid1'").fetchone()
        self.assertEqual(legit["block_reason"], "codex_auth_failed")
        self.assertEqual(legit["base_commit"], BASE_COMMIT)
        self.assertEqual(legit["head_commit"], HEAD_COMMIT)

    def test_malformed_numerics_hashes_and_timestamps_fail_closed(self):
        src = sqlite3.connect(self.db_path)
        src.execute(
            "INSERT INTO invocations(invocation_id, request_id, kind,"
            " stage, elapsed_secs, session_id, session_kind, started_at,"
            " ended_at, schema_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("badnum1", "wid1", "codex_dispatch", "dispatch", float("inf"),
             "thread-badnum", "codex_task_id",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status,"
            " base_commit, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            ("wid-shorthash", json.dumps({"issue": "x"}),
             self.ws_plain, "running", "abc",
             "2026-09-23T18:10:00+00:00", "2026-09-23T18:11:00+00:00"))
        src.execute(
            "INSERT INTO readings(pool, model, window, used, limit_value,"
            " reset_at, observed_at, source) VALUES(?,?,?,?,?,?,?,?)",
            ("codex", "fixture-model", "9h", float("nan"), float("inf"),
             "not-a-timestamp", "2026-09-23T19:00:00+00:00",
             "provider_reported"))
        src.execute(
            "INSERT INTO readings(pool, model, window, used, limit_value,"
            " reset_at, observed_at, source) VALUES(?,?,?,?,?,?,?,?)",
            ("codex", "fixture-model", "10h", 5.0, 100.0,
             "2026-09-24T00:00:00+00:00", "also-not-a-timestamp",
             "provider_reported"))
        src.commit()
        src.close()
        router.sync(self.con, root=self.state)
        inv = self.con.execute(
            "SELECT * FROM router_invocations"
            " WHERE invocation_id='badnum1'").fetchone()
        self.assertIsNotNone(inv)
        self.assertIsNone(inv["elapsed_secs"])
        attempt = self.con.execute(
            "SELECT * FROM attempts"
            " WHERE turn_id='router:badnum1'").fetchone()
        self.assertIsNotNone(attempt)
        self.assertIsNone(attempt["elapsed_s"])
        job = self.con.execute(
            "SELECT * FROM router_jobs"
            " WHERE request_id='wid-shorthash'").fetchone()
        self.assertIsNotNone(job)
        self.assertIsNone(job["base_commit"])
        nan_row = self.con.execute(
            "SELECT * FROM router_readings WHERE window='9h'").fetchone()
        self.assertIsNotNone(nan_row)
        self.assertIsNone(nan_row["used"])
        self.assertIsNone(nan_row["limit_value"])
        self.assertIsNone(nan_row["reset_at"])
        # A reading with an unparseable observed_at is quarantined, never
        # stored under its natural key.
        self.assertIsNone(self.con.execute(
            "SELECT * FROM router_readings WHERE window='10h'").fetchone())
        quarantined = self.con.execute(
            "SELECT error FROM import_errors WHERE harness='router'"
            " AND error='missing_id'").fetchall()
        self.assertTrue(quarantined)

    def test_router_source_lifecycle_records_version_and_reuses_row(self):
        first = router.sync(self.con, root=self.state)
        self.assertEqual(first["unchanged"], 0)
        canonical = os.path.realpath(os.path.abspath(self.db_path))
        row = self.con.execute(
            "SELECT * FROM sources WHERE harness='router' AND path=?",
            (canonical,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["privacy_version"], privacy.PRIVACY_VERSION)
        self.assertTrue(row["sha256"])
        source_id = row["id"]
        before_counts = self._counts()
        again = router.sync(self.con, root=self.state)
        self.assertEqual(again["unchanged"], 1)
        self.assertEqual(self._counts(), before_counts)
        row2 = self.con.execute(
            "SELECT * FROM sources WHERE harness='router' AND path=?",
            (canonical,)).fetchone()
        self.assertEqual(row2["id"], source_id)
        self.assertEqual(row2["privacy_version"], privacy.PRIVACY_VERSION)
        self.assertEqual(row2["sha256"], row["sha256"])

    def test_unchanged_source_skips_ledger_projection(self):
        first = router.sync(self.con, root=self.state)
        calls = []

        def counting_import_job(con, db_path, job, totals):
            calls.append(job.get("request_id"))
            return orig_import_job(con, db_path, job, totals)

        orig_import_job = router._import_job
        orig_import_inv = router._import_invocation
        orig_import_reading = router._import_reading
        router._import_job = counting_import_job
        router._import_invocation = (
            lambda *a: calls.append("inv") or orig_import_inv(*a))
        router._import_reading = (
            lambda *a: calls.append("reading") or orig_import_reading(*a))
        try:
            stats = router.sync(self.con, root=self.state)
        finally:
            router._import_job = orig_import_job
            router._import_invocation = orig_import_inv
            router._import_reading = orig_import_reading
        self.assertEqual(stats["unchanged"], 1)
        self.assertEqual(calls, [])
        # The ledger counts are still reported from the snapshot read.
        self.assertEqual(stats["jobs"], 2)
        self.assertEqual(stats["readings"], 1)
        # Bindings report consistently with a full projection sync.
        self.assertEqual(stats["bindings"], first["bindings"])
        self.assertEqual(stats["bindings"], 7)

    def test_stale_source_version_refreshes_and_replaces_errors(self):
        src = sqlite3.connect(self.db_path)
        src.execute(
            "INSERT INTO invocations(invocation_id, request_id, kind,"
            " stage, session_id, session_kind, started_at, ended_at,"
            " schema_version) VALUES(?,?,?,?,?,?,?,?,?)",
            ("dangling-stale", "wid-missing", "codex_dispatch", "dispatch",
             "thread-stale", "codex_task_id",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        src.commit()
        src.close()
        router.sync(self.con, root=self.state)
        canonical = os.path.realpath(os.path.abspath(self.db_path))
        errors_before = self.con.execute(
            "SELECT COUNT(*) c FROM import_errors WHERE harness='router'"
            " AND source_path=?", (canonical,)).fetchone()["c"]
        self.assertGreaterEqual(errors_before, 1)
        job_rowid = self.con.execute(
            "SELECT rowid FROM router_jobs"
            " WHERE request_id='wid1'").fetchone()["rowid"]
        # Simulate an import under older privacy rules, plus router
        # progress that the refresh must pick up in place.
        self.con.execute(
            "UPDATE sources SET privacy_version=0"
            " WHERE harness='router' AND path=?", (canonical,))
        self.con.commit()
        src = sqlite3.connect(self.db_path)
        src.execute("UPDATE jobs SET status='succeeded'"
                    " WHERE request_id='wid1'")
        src.commit()
        src.close()
        stats = router.sync(self.con, root=self.state)
        self.assertEqual(stats["failed"], [])
        row = self.con.execute(
            "SELECT * FROM sources WHERE harness='router' AND path=?",
            (canonical,)).fetchone()
        self.assertEqual(row["privacy_version"], privacy.PRIVACY_VERSION)
        # Prior errors for this source were replaced, not duplicated.
        errors_after = self.con.execute(
            "SELECT COUNT(*) c FROM import_errors WHERE harness='router'"
            " AND source_path=?", (canonical,)).fetchone()["c"]
        self.assertEqual(errors_after, errors_before)
        # Projections updated in place, never deleted and reinserted.
        self.assertEqual(
            self.con.execute(
                "SELECT rowid FROM router_jobs"
                " WHERE request_id='wid1'").fetchone()["rowid"], job_rowid)
        job = self.con.execute(
            "SELECT * FROM router_jobs WHERE request_id='wid1'").fetchone()
        self.assertEqual(job["status"], "succeeded")

    def test_stale_version_replaces_errors_before_guard_failure(self):
        router.sync(self.con, root=self.state)
        canonical = os.path.realpath(os.path.abspath(self.db_path))
        # Seed a prior error under the current version.
        src = sqlite3.connect(self.db_path)
        src.execute(
            "INSERT INTO invocations(invocation_id, request_id, kind,"
            " stage, session_id, session_kind, started_at, ended_at,"
            " schema_version) VALUES(?,?,?,?,?,?,?,?,?)",
            ("dangling-guard", "wid-missing", "codex_dispatch", "dispatch",
             "thread-guard", "codex_task_id",
             "2026-09-23T18:00:00+00:00", "2026-09-23T18:01:00+00:00", 2))
        src.commit()
        src.close()
        router.sync(self.con, root=self.state)
        seeded = self.con.execute(
            "SELECT error FROM import_errors WHERE harness='router'"
            " AND source_path=?", (canonical,)).fetchall()
        self.assertTrue(any(r["error"] == "missing_id" for r in seeded))
        # Mark the source stale and poison the schema in the same ledger.
        self.con.execute(
            "UPDATE sources SET privacy_version=0"
            " WHERE harness='router' AND path=?", (canonical,))
        self.con.commit()
        src = sqlite3.connect(self.db_path)
        src.execute("UPDATE invocations SET schema_version=3")
        src.commit()
        src.close()
        stats = router.sync(self.con, root=self.state)
        self.assertEqual(len(stats["failed"]), 1)
        self.assertEqual(stats["failed"][0]["error"], "unsupported_schema")
        # Only the current fixed-category error remains; the version stays
        # stale so a later sync retries the source.
        remaining = self.con.execute(
            "SELECT error FROM import_errors WHERE harness='router'"
            " AND source_path=?", (canonical,)).fetchall()
        self.assertEqual([r["error"] for r in remaining],
                         ["unsupported_schema"])
        row = self.con.execute(
            "SELECT * FROM sources WHERE harness='router' AND path=?",
            (canonical,)).fetchone()
        self.assertEqual(row["privacy_version"], 0)
        # Repairing the ledger retries cleanly under the current version.
        src = sqlite3.connect(self.db_path)
        src.execute("UPDATE invocations SET schema_version=2")
        src.commit()
        src.close()
        recovered = router.sync(self.con, root=self.state)
        self.assertEqual(recovered["failed"], [])
        row = self.con.execute(
            "SELECT * FROM sources WHERE harness='router' AND path=?",
            (canonical,)).fetchone()
        self.assertEqual(row["privacy_version"], privacy.PRIVACY_VERSION)
        retried = self.con.execute(
            "SELECT error FROM import_errors WHERE harness='router'"
            " AND source_path=?", (canonical,)).fetchall()
        self.assertTrue(any(r["error"] == "missing_id" for r in retried))

    def test_unchanged_ledger_still_reconciles_late_rollout(self):
        rollout_paths = sorted(glob.glob(
            os.path.join(self.state, "kits", "*", "sessions", "**",
                         "rollout-*.jsonl"),
            recursive=True))
        self.assertEqual(len(rollout_paths), 2)
        hidden = os.path.join(self.tmp.name, "hidden-rollouts")
        os.makedirs(hidden)
        moved = []
        for path in rollout_paths:
            dest = os.path.join(hidden, os.path.basename(path))
            shutil.move(path, dest)
            moved.append((path, dest))
        try:
            first = router.sync(self.con, root=self.state)
        finally:
            for path, dest in moved:
                shutil.move(dest, path)
        # No rollouts yet: no native sessions, so nothing to reconcile.
        self.assertEqual(first["rollouts"], 0)
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) c FROM import_errors WHERE harness='router'"
                " AND error='usage_conflict'").fetchone()["c"], 0)
        # The second sync skips ledger projections but must still compare
        # router usage once the restored rollouts land.
        projection_calls = []
        orig_job, orig_inv, orig_reading = (
            router._import_job, router._import_invocation,
            router._import_reading)
        router._import_job = (
            lambda *a: projection_calls.append("job") or orig_job(*a))
        router._import_invocation = (
            lambda *a: projection_calls.append("inv") or orig_inv(*a))
        router._import_reading = (
            lambda *a: projection_calls.append("reading") or orig_reading(*a))
        try:
            second = router.sync(self.con, root=self.state)
        finally:
            router._import_job = orig_job
            router._import_invocation = orig_inv
            router._import_reading = orig_reading
        self.assertEqual(projection_calls, [])
        self.assertEqual(second["rollouts"], 2)
        self.assertEqual(second["unchanged"], 0)
        conflicts = self.con.execute(
            "SELECT error, line_excerpt FROM import_errors"
            " WHERE harness='router' AND error='usage_conflict'").fetchall()
        self.assertEqual(len(conflicts), 1)
        self.assertIn("invocation_id", conflicts[0]["line_excerpt"] or "")
        self.assertNotIn("thread-mismatch-bbbb",
                         conflicts[0]["line_excerpt"] or "")

    def test_failed_source_import_stays_stale_for_retry(self):
        state = os.path.join(self.tmp.name, "poison-state")
        shutil.copytree(self.state, state)
        poisoned = os.path.join(state, "jobs.db")
        src = sqlite3.connect(poisoned)
        src.execute("UPDATE invocations SET schema_version=3")
        src.commit()
        src.close()
        fresh = os.path.join(self.tmp.name, "obs-poison.db")
        con = db.connect(fresh)
        db.init_db(con)
        try:
            stats = router.sync(con, root=state)
            self.assertEqual(len(stats["failed"]), 1)
            canonical = os.path.realpath(os.path.abspath(poisoned))
            row = con.execute(
                "SELECT * FROM sources WHERE harness='router' AND path=?",
                (canonical,)).fetchone()
            # No version recorded: the next sync retries the source.
            self.assertTrue(row is None or
                            row["privacy_version"] != privacy.PRIVACY_VERSION)
        finally:
            con.close()


class RouterCliTest(unittest.TestCase):
    def test_sync_through_public_cli(self):
        if shutil.which("git") is None:
            self.skipTest("git is required for project derivation")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        case = RouterAdapterTest("test_job_to_task_fields_and_unknown_outcome")
        case.setUp()
        self.addCleanup(case.tearDown)
        state = case.state
        db_path = os.path.join(tmp.name, "cli-router.db")
        env = dict(os.environ, AGENT_OBSERVER_DB=db_path)
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, "-m", "agent_observer", "sync", "--harness",
             "router", "--root", state, "--json"],
            cwd=repo, capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        entry = [h for h in payload["harnesses"]
                 if h["harness"] == "router"][0]
        self.assertEqual(entry["jobs"], 2)
        self.assertEqual(entry["failed"], [])
