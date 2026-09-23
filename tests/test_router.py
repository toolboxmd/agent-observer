"""Model Router ledger adapter: jobs to tasks, invocations to attempts.

Builds a small Model Router jobs.db (jobs, invocations, readings, events
with schema_version) in a temp dir, imports it read-only, and checks the
workload mapping, session ownership, schema guard, idempotency, rollout
import, and usage reconciliation. Never invokes a model.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_observer import db
from agent_observer.adapters import router
from tests.helpers import LedgerCase

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "router")
BLOCK = ("<<<AGENTSMD_PROJECT_DIRECTION_V1>>>\n"
         '{"status":"ready","instructions":{"sha256":"secret-hash"}}\n'
         "<<<END_AGENTSMD_PROJECT_DIRECTION_V1>>>")


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
             "plan-sess-1", "fixture-planner", "claude", "abc", "def",
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
        self.assertEqual(task["title"],
                         "Short fixture goal for workload mapping")
        self.assertEqual(task["issue_url"], "toolboxmd/model-router#17")
        job = self.con.execute(
            "SELECT * FROM router_jobs WHERE request_id='wid1'").fetchone()
        self.assertEqual(job["issue"], "toolboxmd/model-router#17")
        self.assertEqual(job["block_reason"], "codex_auth_failed")
        outcome = self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone()
        # A blocked job never implies acceptance.
        self.assertEqual(outcome["acceptance_state"], "unknown")
        self.assertIn("blocked", outcome["repairs"])

    def test_title_strips_direction_block_and_truncates(self):
        router.sync(self.con, root=self.state)
        task = self.con.execute(
            "SELECT * FROM tasks WHERE task_id='router:wid2'").fetchone()
        self.assertNotIn("AGENTSMD", task["title"])
        self.assertNotIn("secret-hash", task["title"])
        self.assertLessEqual(len(task["title"]), 120)
        self.assertTrue(task["title"].startswith("Real fixture goal text"))
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
                self.assertIn("schema_guard", stats["failed"][0]["error"])
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
                    # The quarantine holds a category plus the row shape:
                    # table, source column names, native id. No row content.
                    guarded = con.execute(
                        "SELECT error, line_excerpt FROM import_errors"
                        " WHERE harness='router'").fetchall()
                    self.assertEqual(len(guarded), 1)
                    self.assertIn("schema_guard", guarded[0]["error"])
                    excerpt = guarded[0]["line_excerpt"] or ""
                    self.assertIn(table, excerpt)
                    self.assertIn("schema_version", excerpt)
                    if table == "invocations":
                        self.assertIn("aaa111", excerpt)
                        self.assertIn("aaa111", stats["failed"][0]["error"])
                    self.assertLessEqual(len(excerpt), 200)
                finally:
                    con.close()

    def test_idempotent_reimport_updates_in_place(self):
        first = router.sync(self.con, root=self.state)
        self.assertEqual(first["unchanged"], 0)
        before = self._counts()
        again = router.sync(self.con, root=self.state)
        self.assertEqual(again["unchanged"], 1)
        self.assertEqual(self._counts(), before)
        # Router progress updates the same rows, never duplicates.
        src = sqlite3.connect(self.db_path)
        src.execute("UPDATE jobs SET status='complete',"
                    " block_reason='quota_blocked: out of capacity',"
                    " updated_at='2026-09-23T19:00:00+00:00'"
                    " WHERE request_id='wid1'")
        src.commit()
        src.close()
        third = router.sync(self.con, root=self.state)
        self.assertEqual(third["unchanged"], 0)
        self.assertEqual(self._counts(), before)
        job = self.con.execute(
            "SELECT * FROM router_jobs WHERE request_id='wid1'").fetchone()
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["block_reason"], "quota_blocked")
        outcome = self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone()
        self.assertEqual(outcome["acceptance_state"], "unknown")
        self.assertIn("complete", outcome["repairs"])
        # A human-recorded outcome survives re-import untouched.
        self.con.execute(
            "UPDATE outcomes SET acceptance_state='complete',"
            " candidate='human-candidate', proof_ref='human-proof'"
            " WHERE task_id='router:wid1'")
        self.con.commit()
        router.sync(self.con, root=self.state)
        outcome = self.con.execute(
            "SELECT * FROM outcomes WHERE task_id='router:wid1'").fetchone()
        self.assertEqual(outcome["acceptance_state"], "complete")
        self.assertEqual(outcome["candidate"], "human-candidate")
        self.assertEqual(outcome["proof_ref"], "human-proof")

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
        # The matching session raises no mismatch.
        errors = self.con.execute(
            "SELECT error FROM import_errors WHERE harness='router'").fetchall()
        self.assertFalse(
            [e for e in errors
             if "thread-match-aaaa" in e["error"]],
            [e["error"] for e in errors])

    def test_usage_mismatch_recorded_without_router_responses(self):
        router.sync(self.con, root=self.state)
        errors = self.con.execute(
            "SELECT error, line_excerpt FROM import_errors"
            " WHERE harness='router' AND error LIKE 'usage_mismatch:%'").fetchall()
        self.assertEqual(len(errors), 1)
        self.assertIn("thread-mismatch-bbbb", errors[0]["error"])
        self.assertLessEqual(len(errors[0]["line_excerpt"]), 200)
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
            blob = (row["error"] or "") + (row["line_excerpt"] or "")
            self.assertNotIn("Short fixture goal", blob)
            self.assertNotIn("/tmp/secret", blob)
            self.assertNotIn("secret-hash", blob)
            self.assertLessEqual(len(row["line_excerpt"] or ""), 200)
        for row in self.con.execute(
                "SELECT block_reason FROM router_jobs"):
            self.assertNotIn(":", row["block_reason"] or "")
            self.assertNotIn(" ", row["block_reason"] or "")

    def test_readings_snapshot_in_place(self):
        router.sync(self.con, root=self.state)
        row = self.con.execute(
            "SELECT * FROM router_readings WHERE pool='codex' AND"
            " model='fixture-model' AND window='5h'").fetchone()
        self.assertEqual(row["used"], 19.0)
        self.assertEqual(row["source"], "provider_reported")
        src = sqlite3.connect(self.db_path)
        src.execute("UPDATE readings SET used=42.0 WHERE pool='codex'")
        src.commit()
        src.close()
        router.sync(self.con, root=self.state)
        rows = self.con.execute(
            "SELECT * FROM router_readings WHERE pool='codex' AND"
            " model='fixture-model' AND window='5h'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["used"], 42.0)


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
