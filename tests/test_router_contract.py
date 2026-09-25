"""Router87 contract: proof kind, verification stage, seq joins and whitelisted recovery events.

Builds a faithful Router jobs.db (jobs with cancel_requested,
invocations with rc/meta_json/proof reason, events with recovery
payloads) in a temp dir, imports it read-only, and checks the public
task report. Never invokes a model. Uses disposable DBs only.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_observer import db, report
from agent_observer.adapters import router
from tests.helpers import LedgerCase

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(db_path, *args):
    env = dict(os.environ, AGENT_OBSERVER_DB=db_path)
    return subprocess.run(
        [sys.executable, "-m", "agent_observer", *args],
        cwd=REPO, capture_output=True, text=True, env=env)


def _router_db(path):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE jobs (request_id TEXT PRIMARY KEY, task_json TEXT,"
        " workspace TEXT, status TEXT, lane TEXT, job_kind TEXT,"
        " cancel_requested INTEGER,"
        " created_at TEXT, updated_at TEXT)")
    con.execute(
        "CREATE TABLE invocations (invocation_id TEXT UNIQUE,"
        " request_id TEXT, kind TEXT, stage TEXT, requested_route TEXT,"
        " policy_version TEXT, reason TEXT, terminal_class TEXT,"
        " rc INTEGER, session_id TEXT, session_kind TEXT,"
        " started_at TEXT, ended_at TEXT, elapsed_secs REAL,"
        " meta_json TEXT, usage_json TEXT, native_ids_json TEXT,"
        " schema_version INTEGER)")
    con.execute(
        "CREATE TABLE readings (pool TEXT, model TEXT, window TEXT,"
        " used REAL, limit_value REAL, reset_at TEXT, observed_at TEXT,"
        " source TEXT)")
    con.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, request_id TEXT,"
        " ts TEXT, kind TEXT, payload_json TEXT, schema_version INTEGER)")
    return con


def _meta(seq, extra=None):
    # Faithful shape: worker meta carries a prompt that must never
    # persist; proof meta carries only seq/route/stage/proof_class.
    base = {"seq": seq}
    if extra:
        base.update(extra)
    return json.dumps(base)


class RouterContractImportTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.tmp.name, "router-state")
        os.makedirs(self.state)
        self.db_path = os.path.join(self.state, "jobs.db")
        self._build_source()

    def _build_source(self):
        src = _router_db(self.db_path)
        # One job with a completed worker, a failed proof at
        # verification stage, linked recovery to a second worker/proof
        # pair, all in the same request with actual seq identities.
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status,"
            " lane, job_kind, cancel_requested, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            ("req-verify-1",
             json.dumps({"issue": "toolboxmd/agent-observer#25",
                         "observer_task_id": "T-VERIFY"}),
             "/tmp/plain-ws", "running", "implementation_default",
             "ordinary", 0,
             "2026-09-25T00:00:00+00:00", "2026-09-25T00:10:00+00:00"))
        # Explicit intentional cancellation on a second job.
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status,"
            " lane, job_kind, cancel_requested, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            ("req-cancel-1",
             json.dumps({"issue": "toolboxmd/agent-observer#25",
                         "observer_task_id": "T-VERIFY"}),
             "/tmp/plain-ws", "cancelled", "implementation_default",
             "ordinary", 1,
             "2026-09-25T00:00:00+00:00", "2026-09-25T00:05:00+00:00"))
        # Legacy job without cancel_requested stays unknown intent.
        src.execute(
            "INSERT INTO jobs(request_id, task_json, workspace, status,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("req-legacy-1",
             json.dumps({"observer_task_id": "T-VERIFY"}),
             "/tmp/plain-ws", "cancelled",
             "2026-09-25T00:00:00+00:00", "2026-09-25T00:05:00+00:00"))
        invocations = [
            # Completed worker seq 5.
            ("w5", "req-verify-1", "opencode_control", "implementation",
             "muse-spark-xhigh-free", "2.7.1", "initial", "completed",
             0, "sess-verify-1", "opencode_session_id",
             "2026-09-25T00:00:00+00:00", "2026-09-25T00:01:40+00:00",
             100.0, _meta(5, {"prompt": "SECRET-PROMPT must never persist",
                              "route": "muse-spark-xhigh-free",
                              "stage": "implementation"})),
            # Failed proof seq 5 at verification stage.
            ("p5", "req-verify-1", "proof", "verification",
             "muse-spark-xhigh-free", "2.7.1", "failed", "failed",
             1, None, None,
             "2026-09-25T00:01:40+00:00", "2026-09-25T00:02:00+00:00",
             20.0, _meta(5, {"route": "muse-spark-xhigh-free",
                             "stage": "verification",
                             "proof_class": "failed"})),
            # Correction worker seq 6.
            ("w6", "req-verify-1", "opencode_control", "implementation",
             "muse-spark-xhigh-free", "2.7.1", "correction", "completed",
             0, "sess-verify-2", "opencode_session_id",
             "2026-09-25T00:02:10+00:00", "2026-09-25T00:03:50+00:00",
             100.0, _meta(6, {"route": "muse-spark-xhigh-free",
                              "stage": "implementation"})),
            # Passing proof seq 6.
            ("p6", "req-verify-1", "proof", "verification",
             "muse-spark-xhigh-free", "2.7.1", "pass", "completed",
             0, None, None,
             "2026-09-25T00:03:50+00:00", "2026-09-25T00:04:00+00:00",
             10.0, _meta(6, {"route": "muse-spark-xhigh-free",
                             "stage": "verification",
                             "proof_class": "pass"})),
            # Startup rc124 without proof outcome: infrastructure.
            ("s7", "req-verify-1", "opencode_control", "implementation",
             "muse-spark-xhigh-free", "2.7.1", "initial", "timeout",
             124, "sess-verify-3", "opencode_session_id",
             "2026-09-25T00:05:00+00:00", "2026-09-25T00:05:10+00:00",
             10.0, _meta(7, {"route": "muse-spark-xhigh-free",
                             "stage": "implementation"})),
            # Proof rc124 with proof_class timeout: timeout.
            ("p8", "req-verify-1", "proof", "verification",
             "muse-spark-xhigh-free", "2.7.1", "timeout", "timeout",
             124, None, None,
             "2026-09-25T00:05:10+00:00", "2026-09-25T00:05:30+00:00",
             20.0, _meta(8, {"route": "muse-spark-xhigh-free",
                             "stage": "verification",
                             "proof_class": "timeout"})),
            # Context pressure: provider.
            ("c9", "req-verify-1", "opencode_control", "implementation",
             "muse-spark-xhigh-free", "2.7.1", "initial", "context",
             1, "sess-verify-4", "opencode_session_id",
             "2026-09-25T00:06:00+00:00", "2026-09-25T00:06:10+00:00",
             10.0, _meta(9, {"route": "muse-spark-xhigh-free",
                             "stage": "implementation"})),
            # Launch reason never classifies: pool_move with failed.
            ("m10", "req-verify-1", "opencode_control", "implementation",
             "muse-spark-xhigh-free", "2.7.1", "pool_move", "failed",
             1, "sess-verify-5", "opencode_session_id",
             "2026-09-25T00:07:00+00:00", "2026-09-25T00:07:10+00:00",
             10.0, _meta(10, {"route": "muse-spark-xhigh-free",
                              "stage": "implementation"})),
            # Explicit intentional cancellation.
            ("cx1", "req-cancel-1", "opencode_control", "implementation",
             None, None, "initial", "cancelled",
             143, "sess-cancel-1", "opencode_session_id",
             "2026-09-25T00:00:00+00:00", "2026-09-25T00:01:00+00:00",
             60.0, _meta(1, {"stage": "implementation"})),
            # Legacy cancelled without cancel_requested stays unknown.
            ("cx2", "req-legacy-1", "codex_dispatch", "dispatch",
             None, None, "initial", "cancelled",
             None, "thread-legacy", "codex_task_id",
             "2026-09-25T00:00:00+00:00", "2026-09-25T00:01:00+00:00",
             60.0, None),
            # Legacy unknown kind/stage/reason stays NULL, unknown state.
            ("legacy-bad", "req-legacy-1", "smoke_signals", "review",
             None, None, "do it because I said so", "bogus-class",
             None, "thread-legacy", "claude_session_id",
             "2026-09-25T00:00:00+00:00", "2026-09-25T00:01:00+00:00",
             60.0, None),
        ]
        for (iid, req, kind, stage, route, policy, reason, terminal,
             rc, sid, skind, started, ended, elapsed, meta) in invocations:
            src.execute(
                "INSERT INTO invocations(invocation_id, request_id, kind,"
                " stage, requested_route, policy_version, reason,"
                " terminal_class, rc, session_id, session_kind,"
                " started_at, ended_at, elapsed_secs, meta_json,"
                " usage_json, native_ids_json, schema_version)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, req, kind, stage, route, policy, reason, terminal,
                 rc, sid, skind, started, ended, elapsed, meta, None,
                 None, 2))
        events = [
            ("req-verify-1", "2026-09-25T00:02:01+00:00",
             "recovery_decision",
             {"failures": 1, "rung": "correction", "target": None,
              "failed_seq": 5, "next_attempt_seq": None,
              "failed_at": "2026-09-25T00:02:00+00:00",
              "decided_at": "2026-09-25T00:02:01+00:00",
              "reason": "correction"}),
            ("req-verify-1", "2026-09-25T00:02:10+00:00",
             "recovery_next_attempt",
             {"failed_seq": 5, "next_seq": 6,
              "route": "muse-spark-xhigh-free"}),
            ("req-verify-1", "2026-09-25T00:04:01+00:00",
             "recovery_attempt_result",
             {"failed_seq": 5, "next_seq": 6, "outcome": "ok"}),
            ("req-verify-1", "2026-09-25T00:02:00+00:00",
             "verification_attempt",
             {"seq": 5, "route": "muse-spark-xhigh-free", "rc": 1,
              "proof_class": "failed"}),
            ("req-verify-1", "2026-09-25T00:04:00+00:00",
             "verification_attempt",
             {"seq": 6, "route": "muse-spark-xhigh-free", "rc": 0,
              "proof_class": "pass"}),
            ("req-verify-1", "2026-09-25T00:01:00+00:00",
             "route_switched",
             {"from": "luna/max", "to": "muse-spark-xhigh-free",
              "reason": "pool_move", "scope": "worker",
              "evidence": "provider"}),
            ("req-verify-1", "2026-09-25T00:00:30+00:00",
             "route_switched",
             {"from": "luna/max", "to": "luna/max",
              "reason": "dispatch_exhausted", "scope": "dispatch",
              "evidence": "codex_dispatch"}),
            ("req-verify-1", "2026-09-25T00:03:00+00:00",
             "planner_route_rejected",
             {"requested": "muse-spark-xhigh-free",
              "reason": "route muse-spark-xhigh-free is not dispatcher-assignable: free text dropped"}),
            ("req-verify-1", "2026-09-25T00:03:30+00:00",
             "question_posted", {"qid": "recovery-decision"}),
            # Unknown kind and another qid store nothing.
            ("req-verify-1", "2026-09-25T00:03:31+00:00",
             "mystery_kind", {"seq": 99}),
            ("req-verify-1", "2026-09-25T00:03:32+00:00",
             "question_posted", {"qid": "other-question",
                                 "prompt": "SECRET QUESTION TEXT"}),
            # Payload with secrets must not persist.
            ("req-verify-1", "2026-09-25T00:03:33+00:00",
             "recovery_decision",
             {"failures": 2, "rung": "correction", "target": None,
              "failed_seq": 7, "reason": "correction",
              "prompt": "SECRET PROMPT", "token": "SECRET"}),
        ]
        for req, ts, kind, payload in events:
            src.execute(
                "INSERT INTO events(request_id, ts, kind, payload_json,"
                " schema_version) VALUES(?,?,?,?,?)",
                (req, ts, kind, json.dumps(payload), 2))
        src.commit()
        src.close()

    def test_proof_verification_retained_not_implementation_success(self):
        router.sync(self.con, root=self.state)
        # Proof kind and verification stage persist, not NULL.
        proof = self.con.execute(
            "SELECT * FROM router_invocations WHERE invocation_id='p5'").fetchone()
        self.assertEqual(proof["kind"], "proof")
        self.assertEqual(proof["stage"], "verification")
        self.assertEqual(proof["proof_class"], "failed")
        self.assertIsNone(proof["reason"])
        self.assertEqual(proof["rc"], 1)
        self.assertEqual(proof["meta_seq"], 5)
        worker = self.con.execute(
            "SELECT * FROM router_invocations WHERE invocation_id='w5'").fetchone()
        self.assertEqual(worker["kind"], "opencode_control")
        self.assertEqual(worker["meta_seq"], 5)
        # Secrets never persist.
        blob = ""
        for table in ("router_invocations", "router_events", "attempts"):
            for row in self.con.execute(f"SELECT * FROM {table}"):
                blob += " ".join(str(row[c] or "") for c in row.keys())
        self.assertNotIn("SECRET-PROMPT", blob)
        self.assertNotIn("SECRET QUESTION", blob)
        self.assertNotIn("SECRET", blob)
        # Attempts carry the same evidence.
        attempt = self.con.execute(
            "SELECT * FROM attempts WHERE turn_id='router:p5'").fetchone()
        self.assertEqual(attempt["stage"], "verification")
        self.assertEqual(attempt["proof_class"], "failed")
        self.assertEqual(attempt["meta_seq"], 5)
        rep = report.task_report(self.con, "T-VERIFY")
        by_turn = {a["turn_id"]: a for a in rep["attempt_timing"]["attempts"]}
        # Verification failure is retained as verification, not lost.
        self.assertEqual(by_turn["router:p5"]["failure_class"], "verification")
        self.assertEqual(by_turn["router:w5"]["state"], "complete")
        # Failures count the verification failure once, not doubled.
        self.assertEqual(rep["failures"]["by_class"].get("verification"), 1)
        # No Router/native double count: raw equals reconciled here
        # (no native rows), and executions count all imported attempts.
        self.assertEqual(rep["attempt_timing"]["raw_attempt_count"],
                         rep["attempt_timing"]["reconciled_execution_count"])

    def test_classification_contract(self):
        from agent_observer.timing import failure_class
        router.sync(self.con, root=self.state)
        rep = report.task_report(self.con, "T-VERIFY")
        by_turn = {a["turn_id"]: a for a in rep["attempt_timing"]["attempts"]}
        # Context pressure is provider.
        self.assertEqual(by_turn["router:c9"]["failure_class"], "provider")
        # Startup rc124 without proof outcome is infrastructure.
        self.assertEqual(by_turn["router:s7"]["failure_class"], "infrastructure")
        # Proof rc124 with proof_class timeout stays timeout.
        self.assertEqual(by_turn["router:p8"]["failure_class"], "timeout")
        # Launch reason never classifies: pool_move plus failed stays
        # implementation.
        self.assertEqual(by_turn["router:m10"]["failure_class"], "implementation")
        self.assertEqual(failure_class(
            {"state": "failed", "terminal_class": "failed",
             "reason": "pool_move", "stage": "implementation"}),
            "implementation")
        # Cancellation intent: explicit versus unknown.
        self.assertEqual(rep["failures"]["cancelled"], 2)
        self.assertEqual(rep["failures"]["cancelled_intentional"], 1)
        self.assertEqual(rep["failures"]["cancelled_unknown_intent"], 1)
        jobs = {j["request_id"]: j for j in rep["job_outcomes"]}
        self.assertEqual(jobs["req-cancel-1"]["cancel_intent"], "intentional")
        self.assertEqual(jobs["req-legacy-1"]["cancel_intent"], "unknown")
        # Legacy unknown kind/stage/reason stays NULL and unknown state.
        legacy = self.con.execute(
            "SELECT * FROM router_invocations WHERE invocation_id='legacy-bad'").fetchone()
        self.assertIsNone(legacy["kind"])
        self.assertIsNone(legacy["stage"])
        self.assertIsNone(legacy["reason"])
        self.assertIsNone(legacy["terminal_class"])
        legacy_attempt = self.con.execute(
            "SELECT * FROM attempts WHERE turn_id='router:legacy-bad'").fetchone()
        self.assertEqual(legacy_attempt["state"], "unknown")

    def test_event_projections_scopes_and_identity(self):
        router.sync(self.con, root=self.state)
        rows = self.con.execute(
            "SELECT kind, failed_seq, next_seq, seq, route, outcome, scope,"
            " rung, target, reason, requested, qid FROM router_events"
            " ORDER BY id").fetchall()
        by_kind = {}
        for r in rows:
            by_kind.setdefault(r["kind"], []).append(dict(r))
        # Recovery decision links the failed seq with actual identities.
        decisions = by_kind.get("recovery_decision", [])
        self.assertTrue(any(d["failed_seq"] == 5 and d["rung"] == "correction"
                            and d["reason"] == "correction" for d in decisions))
        # Next attempt carries the actual next seq, same job.
        links = by_kind.get("recovery_next_attempt", [])
        self.assertTrue(any(l["failed_seq"] == 5 and l["next_seq"] == 6 for l in links))
        results = by_kind.get("recovery_attempt_result", [])
        self.assertTrue(any(r["failed_seq"] == 5 and r["next_seq"] == 6
                            and r["outcome"] == "ok" for r in results))
        # Verification attempts retain seq and proof outcome.
        verifs = {v["seq"]: v for v in by_kind.get("verification_attempt", [])}
        self.assertEqual(verifs[5]["route"], "muse-spark-xhigh-free")
        # Route switch scopes stay distinct.
        scopes = sorted(v["scope"] for v in by_kind.get("route_switched", []))
        self.assertEqual(scopes, ["dispatch", "worker"])
        # Planner rejection keeps only the validated requested route.
        rejected = by_kind.get("planner_route_rejected", [])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["requested"], "muse-spark-xhigh-free")
        self.assertIsNone(rejected[0]["reason"])
        # Only the recovery-decision question identity persists.
        questions = by_kind.get("question_posted", [])
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["qid"], "recovery-decision")
        # Unknown kinds and other qids store nothing.
        kinds = {r["kind"] for r in rows}
        self.assertNotIn("mystery_kind", kinds)
        rep = report.task_report(self.con, "T-VERIFY")
        kinds = {e["kind"] for e in rep["router_recovery"]}
        self.assertIn("recovery_decision", kinds)
        self.assertIn("recovery_next_attempt", kinds)
        self.assertIn("recovery_attempt_result", kinds)
        self.assertIn("verification_attempt", kinds)
        self.assertIn("route_switched", kinds)
        self.assertIn("planner_route_rejected", kinds)
        self.assertIn("question_posted", kinds)
        self.assertNotIn("mystery_kind", kinds)
        # Recovery stays meaningful only inside the same compatible
        # request and stage: the verification failure pairs with the
        # next verification in the same request, not with another
        # request or a different stage.
        rec = {r["failed_turn"]: r for r in rep["recovery"]}
        self.assertIn("recovered", rec["router:p5"]["recovery_outcome"])
        self.assertEqual(rec["router:p5"]["compat_scope"], "request req-verify-1")

    def test_public_cli_carries_same_measurements(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "contract.db")
        env = dict(os.environ, AGENT_OBSERVER_DB=db_path)
        repo = REPO
        proc = subprocess.run(
            [sys.executable, "-m", "agent_observer", "sync", "--harness",
             "router", "--root", self.state, "--json"],
            cwd=repo, capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(subprocess.run(
            [sys.executable, "-m", "agent_observer", "task", "show",
             "--task", "T-VERIFY", "--json"],
            cwd=repo, capture_output=True, text=True, env=env).stdout)
        text = subprocess.run(
            [sys.executable, "-m", "agent_observer", "task", "show",
             "--task", "T-VERIFY"],
            cwd=repo, capture_output=True, text=True, env=env)
        self.assertIn(text.returncode, (0, 3), text.stderr)
        self.assertEqual(payload["failures"]["by_class"].get("verification"), 1)
        self.assertIn("verification", text.stdout)
        self.assertIn("recovery_decision", text.stdout)
        self.assertIn("recovery_next_attempt", text.stdout)
        self.assertIn("route_switched", text.stdout)
        self.assertIn("cancel_intent=intentional", text.stdout)
        self.assertIn("proof_class=failed", text.stdout)
        self.assertIn("seq=5", text.stdout)


if __name__ == "__main__":
    unittest.main()
