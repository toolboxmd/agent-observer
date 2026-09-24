"""Issue 25: completion, attempt, failure and recovery timing.

Exercises the public task report with known serial and parallel
timelines, retries, timeouts, stalls, quota exhaustion with pool
moves, generic failures after launch reasons, cancellations of
unknown intent, active attempts, shared sessions, unrelated Router
requests, duplicate Router/native rows, missing timing and ownership,
and explicit acceptance. Asserts exact durations, qualified labels
and denominators without double counting, in both human and JSON
output.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from agent_observer import db, report
from tests.helpers import LedgerCase

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(db_path, *args):
    env = dict(os.environ, AGENT_OBSERVER_DB=db_path)
    return subprocess.run(
        [sys.executable, "-m", "agent_observer", *args],
        cwd=REPO, capture_output=True, text=True, env=env)


def _task(con, task_id="T-TIME", acceptance="unknown", updated_at=None):
    con.execute(
        "INSERT INTO tasks(task_id, project, title, created_at)"
        " VALUES(?,?,?,?)", (task_id, "observer", task_id, 900.0))
    if acceptance is not None:
        con.execute(
            "INSERT INTO outcomes(task_id, acceptance_state, updated_at)"
            " VALUES(?,?,?)", (task_id, acceptance, updated_at if updated_at is not None else 900.0))
    con.commit()


def _submission(con, native_id, session_key=None, turn_id=None, ts=None):
    con.execute(
        "INSERT INTO sources(harness, path, sha256, imported_at)"
        " VALUES('codex',?, 'x',0) ON CONFLICT DO NOTHING", (native_id,))
    src = con.execute("SELECT id FROM sources WHERE path=?", (native_id,)).fetchone()["id"]
    con.execute(
        "INSERT OR IGNORE INTO submissions(native_id, source_id, session_key,"
        " turn_id, ts, kind, text_hash, text_excerpt, is_genuine)"
        " VALUES(?,?,?,?,?,'genuine','h','',1)",
        (native_id, src, session_key, turn_id, ts))
    con.commit()


def _assign(con, sub, task):
    con.execute(
        "INSERT OR IGNORE INTO assignments(submission_native_id, task_id,"
        " evidence, shared, created_at) VALUES(?,?,?,0,?)",
        (sub, task, "test", db.now()))
    con.commit()


def _attempt(con, task, turn, role="worker", state="complete", terminal=None,
             started=None, ended=None, elapsed=None, session=None,
             model=None, route=None, reason=None, stage=None, harness="router",
             usage=None):
    con.execute(
        "INSERT INTO attempts(task_id, turn_id, role, harness, session_key,"
        " stage, route_requested, reason, model_observed, started_at, ended_at,"
        " elapsed_s, state, terminal_class, usage_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task, turn, role, harness, session, stage, route, reason, model,
         started, ended, elapsed, state, terminal, usage))
    con.commit()


def _router_job(con, request_id, status="running", invocation=None,
                terminal=None, started=None, ended=None, elapsed=None,
                updated_at=None):
    con.execute(
        "INSERT OR IGNORE INTO router_jobs(request_id, status, created_at, updated_at)"
        " VALUES(?,?,?,?)", (request_id, status, started,
                             updated_at if updated_at is not None else ended))
    if invocation is not None:
        con.execute(
            "INSERT OR IGNORE INTO router_invocations(invocation_id, request_id,"
            " terminal_class, started_at, ended_at, elapsed_secs)"
            " VALUES(?,?,?,?,?,?)",
            (invocation, request_id, terminal, started, ended, elapsed))
    con.commit()


def _link_router(con, turn, request_id, status="running",
                 terminal=None, started=None, ended=None, elapsed=None,
                 updated_at=None):
    invocation = turn.split("router:", 1)[1] if turn.startswith("router:") else turn
    _router_job(con, request_id, status=status, invocation=invocation,
                terminal=terminal, started=started, ended=ended,
                elapsed=elapsed, updated_at=updated_at)


class CompletionTimingTest(LedgerCase):
    def test_accepted_completion_exact(self):
        _task(self.con, "T-A", "complete", 1500.0)
        _submission(self.con, "codex:sub-1", "codex:s1", "codex:t1", 1000.0)
        _submission(self.con, "codex:sub-2", "codex:s1", "codex:t2", 1100.0)
        _assign(self.con, "codex:sub-1", "T-A")
        _assign(self.con, "codex:sub-2", "T-A")
        rep = report.task_report(self.con, "T-A")
        timing = rep["timing"]
        self.assertEqual(timing["submission_time"], 1000.0)
        self.assertEqual(timing["accepted_completion_time"], 1500.0)
        self.assertEqual(timing["completion_elapsed_s"], 500.0)
        self.assertEqual(timing["completion_label"], "submission-to-accepted-completion")
        self.assertEqual(timing["completion_status"], "complete")

    def test_active_elapsed_so_far_at_named_cutoff(self):
        _task(self.con, "T-B", "active", 1200.0)
        _submission(self.con, "codex:sub-1", "codex:s1", "codex:t1", 1000.0)
        _assign(self.con, "codex:sub-1", "T-B")
        # The submission source predates the submission clock in this
        # fixture, so elapsed-so-far stays unavailable with the gap
        # visible rather than reporting a negative duration.
        rep = report.task_report(self.con, "T-B")
        self.assertIsNone(rep["timing"]["completion_elapsed_s"])
        self.assertIn("cutoff", rep["timing"]["completion_label"])

    def test_failed_never_acquires_accepted_time(self):
        _task(self.con, "T-C", "failed", 1500.0)
        _submission(self.con, "codex:sub-1", "codex:s1", "codex:t1", 1000.0)
        _assign(self.con, "codex:sub-1", "T-C")
        rep = report.task_report(self.con, "T-C")
        self.assertIsNone(rep["timing"]["accepted_completion_time"])
        self.assertIsNone(rep["timing"]["completion_elapsed_s"])
        self.assertIn("never acquire an invented", rep["timing"]["completion_label"])

    def test_cancelled_never_acquires_accepted_time(self):
        _task(self.con, "T-C2", "cancelled", 1500.0)
        _submission(self.con, "codex:sub-1", "codex:s1", "codex:t1", 1000.0)
        _assign(self.con, "codex:sub-1", "T-C2")
        rep = report.task_report(self.con, "T-C2")
        self.assertIsNone(rep["timing"]["accepted_completion_time"])
        self.assertIsNone(rep["timing"]["completion_elapsed_s"])

    def test_active_with_cutoff_reports_so_far(self):
        _task(self.con, "T-D", "active", 1200.0)
        _submission(self.con, "codex:sub-1", "codex:s1", "codex:t1", 1000.0)
        _assign(self.con, "codex:sub-1", "T-D")
        self.con.execute(
            "INSERT INTO sessions(session_key, harness, native_id, updated_at)"
            " VALUES('codex:s1','codex','s1',0)")
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('codex','cut','x',2000.0)")
        src = self.con.execute("SELECT id FROM sources WHERE path='cut'").fetchone()["id"]
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness, session_key,"
            " total_tokens, semantics) VALUES('codex:r1',?,'codex','codex:s1',10,'s')", (src,))
        self.con.commit()
        rep = report.task_report(self.con, "T-D")
        self.assertEqual(rep["timing"]["completion_elapsed_s"], 1000.0)
        self.assertEqual(rep["timing"]["cutoff"], 2000.0)
        self.assertIn("elapsed-so-far", rep["timing"]["completion_label"])

    def test_accepted_without_submission_time_is_known_not_missing(self):
        _task(self.con, "T-E", "complete", 1500.0)
        _submission(self.con, "codex:sub-1", "codex:s1", "codex:t1", None)
        _assign(self.con, "codex:sub-1", "T-E")
        rep = report.task_report(self.con, "T-E")
        self.assertEqual(rep["timing"]["accepted_completion_time"], 1500.0)
        self.assertIsNone(rep["timing"]["completion_elapsed_s"])
        self.assertIn("accepted completion known but elapsed unavailable",
                      rep["timing"]["completion_label"])
        self.assertEqual(rep["timing"]["completion_status"], "accepted-no-elapsed")

    def test_negative_endpoints_are_rejected(self):
        _task(self.con, "T-N", "complete", 900.0)
        _submission(self.con, "codex:sub-1", "codex:s1", "codex:t1", 1000.0)
        _assign(self.con, "codex:sub-1", "T-N")
        rep = report.task_report(self.con, "T-N")
        self.assertIsNone(rep["timing"]["completion_elapsed_s"])
        self.assertIn("negative endpoint difference", rep["timing"]["completion_label"])

    def test_acceptance_recapture_keeps_first_timestamp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "recap.db")
        self.assertEqual(run(db_path, "capture", "create-task", "--task", "T-RC",
                             "--project", "observer", "--title", "recap").returncode, 0)
        self.assertEqual(run(db_path, "capture", "outcome", "--task", "T-RC",
                             "--state", "complete", "--candidate",
                             "0123456789abcdef0123456789abcdef01234567").returncode, 0)
        first = json.loads(run(db_path, "task", "show", "--task", "T-RC", "--json").stdout)
        first_at = first["timing"]["accepted_completion_time"]
        self.assertIsNotNone(first_at)
        self.assertEqual(run(db_path, "capture", "outcome", "--task", "T-RC",
                             "--state", "complete", "--candidate",
                             "0123456789abcdef0123456789abcdef01234567",
                             "--proof", "https://github.com/o/r/pull/1").returncode, 0)
        second = json.loads(run(db_path, "task", "show", "--task", "T-RC", "--json").stdout)
        self.assertEqual(second["timing"]["accepted_completion_time"], first_at)


class ParallelTimingTest(LedgerCase):
    def test_parallel_durations_do_not_become_task_elapsed(self):
        _task(self.con, "T-P", "complete", 150.0)
        _submission(self.con, "codex:sub-1", "codex:s1", "codex:t1", 0.0)
        _assign(self.con, "codex:sub-1", "T-P")
        _attempt(self.con, "T-P", "router:a1", started=0.0, ended=100.0, elapsed=100.0,
                 session="codex:s1", state="complete", terminal="completed")
        _attempt(self.con, "T-P", "router:a2", started=10.0, ended=110.0, elapsed=100.0,
                 session="codex:s2", state="complete", terminal="completed")
        rep = report.task_report(self.con, "T-P")
        self.assertEqual(rep["timing"]["completion_elapsed_s"], 150.0)
        per = {s["session_key"]: s for s in rep["attempt_timing"]["per_session"]}
        self.assertEqual(per["codex:s1"]["wall_sum_s"], 100.0)
        self.assertEqual(per["codex:s2"]["wall_sum_s"], 100.0)
        # The sum of parallel walls is 200, but task elapsed stays 150.
        total_walls = sum(s["wall_sum_s"] for s in rep["attempt_timing"]["per_session"])
        self.assertEqual(total_walls, 200.0)
        self.assertEqual(rep["timing"]["completion_elapsed_s"], 150.0)
        # Parallel work across sessions is not a waiting interval.
        self.assertEqual(rep["attempt_timing"]["waiting_intervals"], [])

    def test_same_session_serial_gap_is_waiting(self):
        _task(self.con, "T-W", "active", 0.0)
        _attempt(self.con, "T-W", "router:w1", started=0.0, ended=10.0, elapsed=10.0,
                 session="codex:s1", state="complete", terminal="completed")
        _attempt(self.con, "T-W", "router:w2", started=30.0, ended=40.0, elapsed=10.0,
                 session="codex:s1", state="complete", terminal="completed")
        rep = report.task_report(self.con, "T-W")
        waiting = rep["attempt_timing"]["waiting_intervals"]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["from_turn"], "router:w1")
        self.assertEqual(waiting[0]["to_turn"], "router:w2")
        self.assertEqual(waiting[0]["gap_s"], 20.0)
        self.assertEqual(waiting[0]["kind"], "waiting interval")

    def test_union_span_is_merged_covered_duration(self):
        _task(self.con, "T-U", "active", 0.0)
        _attempt(self.con, "T-U", "router:u1", started=0.0, ended=10.0, elapsed=10.0,
                 session="codex:s1", state="complete", terminal="completed")
        _attempt(self.con, "T-U", "router:u2", started=1000.0, ended=1010.0, elapsed=10.0,
                 session="codex:s1", state="complete", terminal="completed")
        rep = report.task_report(self.con, "T-U")
        per = {s["session_key"]: s for s in rep["attempt_timing"]["per_session"]}
        # First start to last end would be 1010; merged covered time is 20.
        self.assertEqual(per["codex:s1"]["union_span_s"], 20.0)
        self.assertEqual(per["codex:s1"]["wall_sum_s"], 20.0)


class FailureClassificationTest(LedgerCase):
    def test_failures_by_class_and_denominator(self):
        _task(self.con, "T-F", "active", 0.0)
        cases = [
            ("router:t-timeout", "timeout", None, "failed"),
            ("router:t-stall", "stalled", None, "failed"),
            ("router:t-provider", "overloaded", None, "failed"),
            ("router:t-infra", "hard_error", None, "failed"),
            ("router:t-impl", "failed", None, "failed"),
            ("router:t-verify", "failed", None, "failed"),
        ]
        for turn, terminal, reason, state in cases:
            stage = "verification" if turn == "router:t-verify" else "implementation"
            _attempt(self.con, "T-F", turn, state=state, terminal=terminal,
                     reason=reason, stage=stage, started=0.0, ended=10.0, elapsed=10.0,
                     role="worker", model="m-%s" % turn)
        # A launch reason never classifies a later generic failure:
        # pool_move plus terminal failed is an implementation failure.
        _attempt(self.con, "T-F", "router:t-pool", state="failed", terminal="failed",
                 reason="pool_move", stage="implementation", started=0.0, ended=5.0, elapsed=5.0)
        _attempt(self.con, "T-F", "router:ok", state="complete", terminal="completed",
                 started=0.0, ended=5.0, elapsed=5.0)
        _attempt(self.con, "T-F", "router:cancel", state="cancelled", terminal="cancelled",
                 started=0.0, ended=1.0, elapsed=1.0)
        _attempt(self.con, "T-F", "router:crash", state="crashed", terminal="crashed",
                 started=0.0, ended=1.0, elapsed=1.0)
        _attempt(self.con, "T-F", "router:active", state="active", terminal=None,
                 started=0.0, ended=None, elapsed=None)
        # Router quota exhaustion imports as quota_blocked and counts as
        # a provider failure inside failed/production with separate
        # quota visibility.
        _attempt(self.con, "T-F", "router:quota", state="quota_blocked", terminal="quota",
                 started=0.0, ended=1.0, elapsed=1.0)
        rep = report.task_report(self.con, "T-F")
        failures = rep["failures"]
        self.assertEqual(failures["failed_attempts"], 8)
        # Production is complete plus failed plus quota_blocked.
        self.assertEqual(failures["production_attempts"], 9)
        self.assertEqual(failures["by_class"]["timeout"], 1)
        self.assertEqual(failures["by_class"]["stall"], 1)
        self.assertEqual(failures["by_class"]["provider"], 2)
        self.assertEqual(failures["by_class"]["infrastructure"], 1)
        self.assertEqual(failures["by_class"]["implementation"], 2)
        self.assertEqual(failures["by_class"]["verification"], 1)
        # Bare Router cancelled stays intent unknown, outside production.
        self.assertEqual(failures["cancelled"], 1)
        self.assertEqual(failures["cancelled_intentional"], 0)
        self.assertEqual(failures["cancelled_unknown_intent"], 1)
        self.assertEqual(failures["crashed_separate"], 1)
        self.assertEqual(failures["active_attempts"], 1)
        self.assertEqual(failures["quota_blocked"], 1)

    def test_launch_reasons_never_classify(self):
        from agent_observer.timing import failure_class
        for reason in ("pool_move", "lateral", "dispatch_stalled",
                       "preflight_exhausted", "preflight_degraded",
                       "dispatch_exhausted"):
            attempt = {"state": "failed", "terminal_class": "failed",
                       "reason": reason, "stage": "implementation"}
            self.assertEqual(failure_class(attempt), "implementation",
                             "reason %s must not classify" % reason)
        # Missing terminal evidence stays unknown, never guessed.
        self.assertIsNone(failure_class(
            {"state": "failed", "terminal_class": None,
             "reason": None, "stage": "implementation"}))
        self.assertEqual(failure_class(
            {"state": "quota_blocked", "terminal_class": "quota"}), "provider")

    def test_pool_move_success_is_not_a_failure(self):
        _task(self.con, "T-M", "complete", 200.0)
        _attempt(self.con, "T-M", "router:exhaust", state="quota_blocked", terminal="quota",
                 reason="initial", started=0.0, ended=10.0, elapsed=10.0)
        _link_router(self.con, "router:exhaust", "req-pool", status="failed",
                     terminal="quota", started=0.0, ended=10.0, elapsed=10.0)
        _attempt(self.con, "T-M", "router:moved", state="complete", terminal="completed",
                 reason="pool_move", started=11.0, ended=20.0, elapsed=9.0)
        _link_router(self.con, "router:moved", "req-pool", status="succeeded",
                     terminal="completed", started=11.0, ended=20.0, elapsed=9.0)
        rep = report.task_report(self.con, "T-M")
        self.assertEqual(rep["failures"]["failed_attempts"], 1)
        self.assertEqual(rep["failures"]["by_class"]["provider"], 1)
        # The task itself is accepted complete despite the attempt failure.
        self.assertEqual(rep["acceptance_state"], "complete")


class RecoveryTest(LedgerCase):
    def test_failure_to_next_start_and_first_progress_at_end(self):
        _task(self.con, "T-R", "active", 0.0)
        _attempt(self.con, "T-R", "router:A", state="failed", terminal="failed",
                 stage="implementation",
                 started=0.0, ended=100.0, elapsed=100.0)
        _attempt(self.con, "T-R", "router:B", state="failed", terminal="timeout",
                 stage="implementation",
                 started=110.0, ended=120.0, elapsed=10.0)
        _attempt(self.con, "T-R", "router:C", state="complete", terminal="completed",
                 stage="implementation",
                 started=130.0, ended=140.0, elapsed=10.0)
        for turn, s, e in (("router:A", 0.0, 100.0), ("router:B", 110.0, 120.0),
                           ("router:C", 130.0, 140.0)):
            _link_router(self.con, turn, "req-R", status="running",
                         started=s, ended=e)
        rep = report.task_report(self.con, "T-R")
        rec = {r["failed_turn"]: r for r in rep["recovery"]}
        # Failure to next start uses the next compatible start.
        self.assertEqual(rec["router:A"]["failure_to_next_start_s"], 10.0)
        self.assertEqual(rec["router:A"]["first_progress_turn"], "router:C")
        self.assertEqual(rec["router:A"]["first_progress_stage"], "implementation")
        self.assertEqual(rec["router:A"]["failed_stage"], "implementation")
        # First progress is measured at completion end, not candidate start.
        self.assertEqual(rec["router:A"]["time_to_first_progress_s"], 40.0)
        self.assertEqual(rec["router:A"]["compat_scope"], "request req-R")
        self.assertIn("recovered", rec["router:A"]["recovery_outcome"])
        self.assertEqual(rec["router:A"]["later_failed_attempts"], 1)
        self.assertEqual(rec["router:B"]["failure_to_next_start_s"], 10.0)
        self.assertEqual(rec["router:B"]["first_progress_turn"], "router:C")
        self.assertEqual(rec["router:B"]["first_progress_stage"], "implementation")
        self.assertEqual(rec["router:B"]["time_to_first_progress_s"], 20.0)
        self.assertIn("recovered", rec["router:B"]["recovery_outcome"])

    def test_start_alone_is_not_recovery_and_unresolved_stays(self):
        _task(self.con, "T-U", "active", 0.0)
        _attempt(self.con, "T-U", "router:D", state="failed", terminal="failed",
                 stage="implementation",
                 started=0.0, ended=50.0, elapsed=50.0)
        _attempt(self.con, "T-U", "router:E", state="active", terminal=None,
                 stage="implementation",
                 started=60.0, ended=None, elapsed=None)
        _link_router(self.con, "router:D", "req-U", started=0.0, ended=50.0)
        _link_router(self.con, "router:E", "req-U", started=60.0, ended=None)
        rep = report.task_report(self.con, "T-U")
        rec = {r["failed_turn"]: r for r in rep["recovery"]}
        self.assertEqual(rec["router:D"]["failure_to_next_start_s"], 10.0)
        self.assertIsNone(rec["router:D"]["first_progress_turn"])
        self.assertIn("active", rec["router:D"]["recovery_outcome"])
        _task(self.con, "T-V", "active", 0.0)
        _attempt(self.con, "T-V", "router:F", state="failed", terminal="failed",
                 started=0.0, ended=50.0, elapsed=50.0)
        _link_router(self.con, "router:F", "req-V", started=0.0, ended=50.0)
        rep2 = report.task_report(self.con, "T-V")
        self.assertIn("unknown", rep2["recovery"][0]["recovery_outcome"])

    def test_unrelated_requests_never_pair(self):
        _task(self.con, "T-X", "active", 0.0)
        # Worker A fails over [0, 100] on request A.
        _attempt(self.con, "T-X", "router:a", state="failed", terminal="failed",
                 started=0.0, ended=100.0, elapsed=100.0, session="codex:sa")
        # Unrelated parallel worker B completes over [10, 50] on request B.
        _attempt(self.con, "T-X", "router:b", state="complete", terminal="completed",
                 started=10.0, ended=50.0, elapsed=40.0, session="codex:sb")
        _link_router(self.con, "router:a", "req-A", started=0.0, ended=100.0)
        _link_router(self.con, "router:b", "req-B", started=10.0, ended=50.0)
        rep = report.task_report(self.con, "T-X")
        rec = {r["failed_turn"]: r for r in rep["recovery"]}
        # No compatible retry exists, so no invented negative recovery.
        self.assertIsNone(rec["router:a"]["failure_to_next_start_s"])
        self.assertIsNone(rec["router:a"]["first_progress_turn"])
        self.assertIsNone(rec["router:a"]["time_to_first_progress_s"])
        self.assertIn("unknown", rec["router:a"]["recovery_outcome"])
        self.assertEqual(rec["router:a"]["later_failed_attempts"], 0)

    def test_quota_exhaustion_recovers_in_compatible_request(self):
        _task(self.con, "T-Q", "active", 0.0)
        _attempt(self.con, "T-Q", "router:q1", state="quota_blocked", terminal="quota",
                 reason="initial", stage="implementation",
                 started=0.0, ended=10.0, elapsed=10.0)
        _attempt(self.con, "T-Q", "router:q2", state="complete", terminal="completed",
                 reason="pool_move", stage="implementation",
                 started=11.0, ended=20.0, elapsed=9.0)
        _link_router(self.con, "router:q1", "req-Q", terminal="quota",
                     started=0.0, ended=10.0, elapsed=10.0)
        _link_router(self.con, "router:q2", "req-Q", terminal="completed",
                     started=11.0, ended=20.0, elapsed=9.0)
        rep = report.task_report(self.con, "T-Q")
        rec = {r["failed_turn"]: r for r in rep["recovery"]}
        self.assertEqual(rec["router:q1"]["failed_class"], "provider")
        self.assertEqual(rec["router:q1"]["failure_to_next_start_s"], 1.0)
        self.assertEqual(rec["router:q1"]["first_progress_turn"], "router:q2")
        self.assertEqual(rec["router:q1"]["first_progress_stage"], "implementation")
        self.assertEqual(rec["router:q1"]["failed_stage"], "implementation")
        # Progress measured at the completed end (20) minus failed end (10).
        self.assertEqual(rec["router:q1"]["time_to_first_progress_s"], 10.0)
        self.assertIn("recovered", rec["router:q1"]["recovery_outcome"])

    def test_cross_job_pair_is_not_recovery(self):
        # Mirrors the old T3 fixture error: a dispatch failure must not
        # pair with a control completion from another request.
        _task(self.con, "T-T3X", "active", 0.0)
        _attempt(self.con, "T-T3X", "router:d1", role="codex_dispatch",
                 state="failed", terminal="stalled",
                 started=0.0, ended=578.674, elapsed=578.674,
                 session="codex:w1", route="luna/max")
        _attempt(self.con, "T-T3X", "router:c1", role="opencode_control",
                 state="complete", terminal="completed",
                 started=578.944, ended=621.342, elapsed=42.399,
                 session="opencode:w1")
        _link_router(self.con, "router:d1", "req-dispatch",
                     started=0.0, ended=578.674)
        _link_router(self.con, "router:c1", "req-control",
                     started=578.944, ended=621.342)
        rep = report.task_report(self.con, "T-T3X")
        rec = {r["failed_turn"]: r for r in rep["recovery"]}
        self.assertIsNone(rec["router:d1"]["failure_to_next_start_s"])
        self.assertIsNone(rec["router:d1"]["first_progress_turn"])
        self.assertIn("unknown", rec["router:d1"]["recovery_outcome"])

    def test_compatible_serial_recovery_with_real_gaps(self):
        _task(self.con, "T-T3", "active", 0.0)
        _attempt(self.con, "T-T3", "router:d1", role="codex_dispatch",
                 state="failed", terminal="stalled", stage="dispatch",
                 started=0.0, ended=578.674, elapsed=578.674,
                 session="codex:w1", route="luna/max")
        _attempt(self.con, "T-T3", "router:c1", role="opencode_control",
                 state="complete", terminal="completed", stage="dispatch",
                 started=578.944, ended=621.342, elapsed=42.399,
                 session="opencode:w1")
        _attempt(self.con, "T-T3", "router:f1", role="opencode_control",
                 state="failed", terminal="failed", stage="implementation",
                 started=621.572, ended=1874.382, elapsed=1252.81,
                 session="opencode:w1")
        _attempt(self.con, "T-T3", "router:r1", role="opencode_control",
                 state="complete", terminal="completed", reason="pool_move",
                 stage="implementation",
                 started=1875.812, ended=3663.207, elapsed=1787.395,
                 session="opencode:w2")
        # All four share the real same-job Router request
        # t3-fleet-pane-1-complete: dispatch retries then the
        # implementation retry chain in one compatible identity.
        _link_router(self.con, "router:d1", "t3-fleet-pane-1-complete",
                     started=0.0, ended=578.674)
        _link_router(self.con, "router:c1", "t3-fleet-pane-1-complete",
                     started=578.944, ended=621.342)
        _link_router(self.con, "router:f1", "t3-fleet-pane-1-complete",
                     started=621.572, ended=1874.382)
        _link_router(self.con, "router:r1", "t3-fleet-pane-1-complete",
                     started=1875.812, ended=3663.207)
        rep = report.task_report(self.con, "T-T3")
        rec = {r["failed_turn"]: r for r in rep["recovery"]}
        # d1 recovers to c1 inside the same request with exact gaps.
        self.assertAlmostEqual(rec["router:d1"]["failure_to_next_start_s"],
                               578.944 - 578.674, places=2)
        self.assertEqual(rec["router:d1"]["first_progress_turn"], "router:c1")
        self.assertEqual(rec["router:d1"]["first_progress_stage"], "dispatch")
        self.assertEqual(rec["router:d1"]["failed_stage"], "dispatch")
        self.assertEqual(rec["router:d1"]["same_stage_progress_turn"], "router:c1")
        self.assertAlmostEqual(rec["router:d1"]["time_to_first_progress_s"],
                               621.342 - 578.674, places=2)
        self.assertIn("recovered", rec["router:d1"]["recovery_outcome"])
        self.assertEqual(rec["router:d1"]["later_failed_attempts"], 0)
        # f1 recovers to r1 inside the same request with exact gaps.
        # The pool_move reason on r1 stays an attempt detail; f1 still
        # classifies as an implementation failure.
        self.assertEqual(rec["router:f1"]["failed_class"], "implementation")
        self.assertAlmostEqual(rec["router:f1"]["failure_to_next_start_s"], 1.43, places=2)
        self.assertEqual(rec["router:f1"]["first_progress_turn"], "router:r1")
        self.assertEqual(rec["router:f1"]["first_progress_stage"], "implementation")
        self.assertEqual(rec["router:f1"]["failed_stage"], "implementation")
        self.assertEqual(rec["router:f1"]["same_stage_progress_turn"], "router:r1")
        self.assertAlmostEqual(rec["router:f1"]["time_to_first_progress_s"],
                               3663.207 - 1874.382, places=2)
        self.assertIn("recovered", rec["router:f1"]["recovery_outcome"])

    def test_dispatcher_completion_is_not_implementation_recovery(self):
        # Faithful Router shape: implementation failure, then a
        # completed dispatch step, then a failed implementation retry,
        # all inside one compatible Router request.
        _task(self.con, "T-STAGE", "active", 0.0)
        _attempt(self.con, "T-STAGE", "router:f1", role="worker",
                 state="failed", terminal="failed", stage="implementation",
                 started=0.0, ended=100.0, elapsed=100.0,
                 session="codex:s1")
        _attempt(self.con, "T-STAGE", "router:d2", role="codex_dispatch",
                 state="complete", terminal="completed", stage="dispatch",
                 started=110.0, ended=120.0, elapsed=10.0,
                 session="codex:s1")
        _attempt(self.con, "T-STAGE", "router:f3", role="worker",
                 state="failed", terminal="failed", stage="implementation",
                 started=130.0, ended=140.0, elapsed=10.0,
                 session="codex:s1")
        for turn, s, e in (("router:f1", 0.0, 100.0),
                           ("router:d2", 110.0, 120.0),
                           ("router:f3", 130.0, 140.0)):
            _link_router(self.con, turn, "req-stage", started=s, ended=e)
        rep = report.task_report(self.con, "T-STAGE")
        rec = {r["failed_turn"]: r for r in rep["recovery"]}
        # Failure to next start still uses the compatible identity.
        self.assertEqual(rec["router:f1"]["failure_to_next_start_s"], 10.0)
        # The dispatcher completion stays visible as first progress.
        self.assertEqual(rec["router:f1"]["first_progress_turn"], "router:d2")
        self.assertEqual(rec["router:f1"]["first_progress_stage"], "dispatch")
        self.assertEqual(rec["router:f1"]["failed_stage"], "implementation")
        # First progress timing is measured at the dispatcher end.
        self.assertEqual(rec["router:f1"]["time_to_first_progress_s"], 20.0)
        # A dispatcher completion never recovers implementation work.
        self.assertNotIn("recovered", rec["router:f1"]["recovery_outcome"])
        self.assertIn("repeated failed recovery",
                      rec["router:f1"]["recovery_outcome"])
        # Only the later implementation failure counts as repeated.
        self.assertEqual(rec["router:f1"]["later_failed_attempts"], 1)

    def test_failure_after_successful_recovery_is_new_failure(self):
        # f1 fails, r1 completes the same stage, then f5 fails the
        # same stage again. f5 is a new failure with its own recovery
        # row, not repeated failed recovery for f1.
        _task(self.con, "T-REC2", "active", 0.0)
        _attempt(self.con, "T-REC2", "router:f1", role="worker",
                 state="failed", terminal="failed", stage="implementation",
                 started=0.0, ended=100.0, elapsed=100.0,
                 session="codex:s1")
        _attempt(self.con, "T-REC2", "router:r1", role="worker",
                 state="complete", terminal="completed",
                 stage="implementation",
                 started=110.0, ended=120.0, elapsed=10.0,
                 session="codex:s1")
        _attempt(self.con, "T-REC2", "router:f5", role="worker",
                 state="failed", terminal="failed", stage="implementation",
                 started=130.0, ended=140.0, elapsed=10.0,
                 session="codex:s1")
        for turn, s, e in (("router:f1", 0.0, 100.0),
                           ("router:r1", 110.0, 120.0),
                           ("router:f5", 130.0, 140.0)):
            _link_router(self.con, turn, "req-rec2", started=s, ended=e)
        rep = report.task_report(self.con, "T-REC2")
        rec = {r["failed_turn"]: r for r in rep["recovery"]}
        self.assertEqual(rec["router:f1"]["failure_to_next_start_s"], 10.0)
        self.assertEqual(rec["router:f1"]["first_progress_turn"], "router:r1")
        self.assertEqual(rec["router:f1"]["first_progress_stage"], "implementation")
        self.assertEqual(rec["router:f1"]["time_to_first_progress_s"], 20.0)
        self.assertIn("recovered", rec["router:f1"]["recovery_outcome"])
        self.assertEqual(rec["router:f1"]["same_stage_progress_turn"], "router:r1")
        self.assertEqual(rec["router:f1"]["later_failed_attempts"], 0)
        self.assertNotIn("router:f5",
                         rec["router:f1"]["evidence"]["same_stage_failed_turns"])
        # f5 stays its own failed row, not folded into f1.
        self.assertIn("router:f5", rec)
        self.assertEqual(rec["router:f5"]["failed_stage"], "implementation")
        self.assertIn("unknown", rec["router:f5"]["recovery_outcome"])
        self.assertEqual(rec["router:f5"]["later_failed_attempts"], 0)

    def test_unknown_stage_never_recovers(self):
        _task(self.con, "T-UNK", "active", 0.0)
        _attempt(self.con, "T-UNK", "router:u1", role="worker",
                 state="failed", terminal="failed", stage=None,
                 started=0.0, ended=10.0, elapsed=10.0,
                 session="codex:s1")
        _attempt(self.con, "T-UNK", "router:u2", role="worker",
                 state="complete", terminal="completed",
                 stage="implementation",
                 started=11.0, ended=20.0, elapsed=9.0,
                 session="codex:s1")
        _link_router(self.con, "router:u1", "req-unk",
                     started=0.0, ended=10.0)
        _link_router(self.con, "router:u2", "req-unk",
                     started=11.0, ended=20.0)
        rep = report.task_report(self.con, "T-UNK")
        rec = {r["failed_turn"]: r for r in rep["recovery"]}
        self.assertEqual(rec["router:u1"]["first_progress_turn"], "router:u2")
        self.assertEqual(rec["router:u1"]["first_progress_stage"],
                         "implementation")
        self.assertIsNone(rec["router:u1"]["failed_stage"])
        self.assertNotIn("recovered", rec["router:u1"]["recovery_outcome"])
        self.assertIn("unknown", rec["router:u1"]["recovery_outcome"])
        self.assertEqual(rec["router:u1"]["later_failed_attempts"], 0)


class ReconcileDuplicateTest(LedgerCase):
    def test_router_native_turn_overlap_is_second_source(self):
        _task(self.con, "T-D", "active", 0.0)
        self.con.execute(
            "INSERT INTO sessions(session_key, harness, native_id, updated_at)"
            " VALUES('codex:s1','codex','s1',0)")
        self.con.execute(
            "INSERT INTO turns(turn_id, session_key, started_at, completed_at,"
            " duration_ms, state) VALUES('codex:nt1','codex:s1',10.0,90.0,80000,'complete')")
        self.con.commit()
        _attempt(self.con, "T-D", "router:r1", state="complete", terminal="completed",
                 started=0.0, ended=100.0, elapsed=100.0, session="codex:s1")
        rep = report.task_report(self.con, "T-D")
        att = rep["attempt_timing"]["attempts"][0]
        self.assertTrue(att["duplicate_native"])
        self.assertEqual(att["reconciled_sources"], ["attempt", "native"])
        self.assertEqual(att["reconciled_wall_time_s"], 100.0)
        self.assertEqual(rep["attempt_timing"]["raw_attempt_count"], 1)
        # A turn overlap is a second source, not a duplicate attempt row.
        self.assertEqual(rep["attempt_timing"]["reconciled_execution_count"], 1)
        self.assertEqual(rep["attempt_timing"]["duplicate_router_native_groups"], 0)
        per = {s["session_key"]: s for s in rep["attempt_timing"]["per_session"]}
        self.assertEqual(per["codex:s1"]["union_span_s"], 100.0)
        self.assertEqual(per["codex:s1"]["attempts"], 1)

    def test_duplicate_router_native_attempt_rows_count_once(self):
        _task(self.con, "T-DD", "active", 0.0)
        self.con.execute(
            "INSERT INTO sessions(session_key, harness, native_id, updated_at)"
            " VALUES('codex:s1','codex','s1',0)")
        self.con.execute(
            "INSERT INTO turns(turn_id, session_key, started_at, completed_at,"
            " duration_ms, state) VALUES('codex:nt1','codex:s1',10.0,90.0,80000,'complete')")
        self.con.commit()
        _attempt(self.con, "T-DD", "router:r1", state="failed", terminal="timeout",
                 started=0.0, ended=100.0, elapsed=100.0, session="codex:s1")
        # Native capture row for the same turn, as the skill instructs,
        # with no session or timestamps of its own.
        _attempt(self.con, "T-DD", "codex:nt1", role="worker", state="failed",
                 terminal="timeout", harness="codex")
        rep = report.task_report(self.con, "T-DD")
        self.assertEqual(rep["attempt_timing"]["raw_attempt_count"], 2)
        self.assertEqual(rep["attempt_timing"]["reconciled_execution_count"], 1)
        self.assertEqual(rep["attempt_timing"]["duplicate_router_native_groups"], 1)
        # Failure counts use the deduplicated execution, not both rows.
        self.assertEqual(rep["failures"]["failed_attempts"], 1)
        self.assertEqual(rep["failures"]["production_attempts"], 1)
        groups = rep["attempt_timing"]["reconciliation_groups"]
        dup = [g for g in groups if g["is_duplicate_group"]]
        self.assertEqual(len(dup), 1)
        self.assertEqual(sorted(dup[0]["members"]), ["codex:nt1", "router:r1"])

    def test_shared_session_never_auto_merges(self):
        _task(self.con, "T-SH", "active", 0.0)
        self.con.execute(
            "INSERT INTO sessions(session_key, harness, native_id, updated_at)"
            " VALUES('codex:shared','codex','shared',0)")
        self.con.execute(
            "INSERT INTO tasks(task_id, created_at) VALUES('T-OTHER',0)")
        self.con.execute(
            "INSERT INTO session_assignments(session_key, task_id, evidence, created_at)"
            " VALUES('codex:shared','T-SH','e1',0)")
        self.con.execute(
            "INSERT INTO session_assignments(session_key, task_id, evidence, created_at)"
            " VALUES('codex:shared','T-OTHER','e2',0)")
        self.con.execute(
            "INSERT INTO turns(turn_id, session_key, started_at, completed_at,"
            " duration_ms, state) VALUES('codex:nt1','codex:shared',10.0,90.0,80000,'complete')")
        self.con.commit()
        _attempt(self.con, "T-SH", "router:r1", state="complete", terminal="completed",
                 started=0.0, ended=100.0, elapsed=100.0, session="codex:shared")
        _attempt(self.con, "T-SH", "codex:nt1", role="worker", state="complete",
                 terminal="completed", harness="codex")
        rep = report.task_report(self.con, "T-SH")
        # Shared ownership stays qualified and never auto-merges.
        self.assertEqual(rep["attempt_timing"]["raw_attempt_count"], 2)
        self.assertEqual(rep["attempt_timing"]["reconciled_execution_count"], 2)
        self.assertEqual(rep["attempt_timing"]["duplicate_router_native_groups"], 0)


class UsageCoverageTest(LedgerCase):
    def test_null_router_usage_with_attributable_native_is_coverage(self):
        _task(self.con, "T-G", "active", 0.0)
        self.con.execute(
            "INSERT INTO sessions(session_key, harness, native_id, updated_at)"
            " VALUES('codex:s1','codex','s1',0)")
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('codex','p','x',0)")
        src = self.con.execute("SELECT id FROM sources WHERE path='p'").fetchone()["id"]
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness, session_key, turn_id, ts,"
            " total_tokens, semantics) VALUES('codex:r1',?,'codex','codex:s1','codex:t1',5.0,50,'s')",
            (src,))
        self.con.execute(
            "INSERT INTO turns(turn_id, session_key, started_at, completed_at,"
            " duration_ms, state) VALUES('codex:t1','codex:s1',0.0,10.0,10000,'complete')")
        self.con.commit()
        _attempt(self.con, "T-G", "router:r1", state="failed", terminal="timeout",
                 started=0.0, ended=10.0, elapsed=10.0, session="codex:s1", usage=None)
        _attempt(self.con, "T-G", "router:r2", state="complete", terminal="completed",
                 started=11.0, ended=20.0, elapsed=9.0, session="codex:s2",
                 usage='{"input_tokens": 10}')
        rep = report.task_report(self.con, "T-G")
        cov = rep["usage_coverage"]
        self.assertEqual(cov["router_attempts"], 2)
        self.assertEqual(cov["router_with_usage"], 1)
        self.assertEqual(cov["null_with_reconciled_native"], 1)
        self.assertEqual(cov["null_without_native"], 0)
        self.assertIn("not zero usage", cov["note"])

    def test_session_alone_does_not_credit_shared_responses(self):
        _task(self.con, "T-H", "active", 0.0)
        self.con.execute(
            "INSERT INTO sessions(session_key, harness, native_id, updated_at)"
            " VALUES('codex:s1','codex','s1',0)")
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('codex','p2','x',0)")
        src = self.con.execute("SELECT id FROM sources WHERE path='p2'").fetchone()["id"]
        # Response far outside the attempt window with an unrelated turn.
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness, session_key, turn_id, ts,"
            " total_tokens, semantics) VALUES('codex:r9',?,'codex','codex:s1','codex:other',500.0,50,'s')",
            (src,))
        self.con.commit()
        _attempt(self.con, "T-H", "router:r1", state="failed", terminal="timeout",
                 started=0.0, ended=10.0, elapsed=10.0, session="codex:s1", usage=None)
        rep = report.task_report(self.con, "T-H")
        cov = rep["usage_coverage"]
        self.assertEqual(cov["null_with_reconciled_native"], 0)
        self.assertEqual(cov["null_without_native"], 1)


class OwnershipQualificationTest(LedgerCase):
    def test_shared_and_unknown_stay_qualified(self):
        _task(self.con, "T-S", "active", 0.0)
        self.con.execute(
            "INSERT INTO sessions(session_key, harness, native_id, updated_at)"
            " VALUES('codex:shared','codex','shared',0)")
        self.con.execute(
            "INSERT INTO tasks(task_id, created_at) VALUES('T-OTHER',0)")
        self.con.execute(
            "INSERT INTO session_assignments(session_key, task_id, evidence, created_at)"
            " VALUES('codex:shared','T-S','e1',0)")
        self.con.execute(
            "INSERT INTO session_assignments(session_key, task_id, evidence, created_at)"
            " VALUES('codex:shared','T-OTHER','e2',0)")
        self.con.commit()
        _attempt(self.con, "T-S", "router:shared1", state="complete", terminal="completed",
                 started=0.0, ended=5.0, elapsed=5.0, session="codex:shared")
        _attempt(self.con, "T-S", "router:unknown1", state="complete", terminal="completed",
                 started=6.0, ended=8.0, elapsed=2.0, session=None, harness="router")
        rep = report.task_report(self.con, "T-S")
        by_turn = {a["turn_id"]: a for a in rep["attempt_timing"]["attempts"]}
        self.assertTrue(by_turn["router:shared1"]["shared_session"])
        self.assertTrue(by_turn["router:unknown1"]["unknown_ownership"])


class MissingEvidenceTest(LedgerCase):
    def test_missing_timing_and_ownership_stay_visible(self):
        _task(self.con, "T-Z", "active", 0.0)
        _attempt(self.con, "T-Z", "router:z1", state="failed", terminal=None,
                 started=None, ended=None, elapsed=None, session=None)
        rep = report.task_report(self.con, "T-Z")
        att = rep["attempt_timing"]["attempts"][0]
        self.assertIsNone(att["wall_time_s"])
        self.assertIn("missing timing", att["wall_time_source"])
        self.assertTrue(att["unknown_ownership"])
        rec = rep["recovery"][0]
        self.assertIsNone(rec["failure_to_next_start_s"])
        self.assertIn("compatible identity", " ".join(rec["gap_missing"]))
        self.assertEqual(rep["failures"]["by_class"].get("unknown"), 1)
        self.assertEqual(rep["failures"]["missing_classification"], ["router:z1"])


class SnapshotIdentityTest(LedgerCase):
    def test_snapshot_covers_report_inputs(self):
        _task(self.con, "T-SNAP", "active", 0.0)
        _attempt(self.con, "T-SNAP", "router:s1", state="complete", terminal="completed",
                 started=0.0, ended=10.0, elapsed=10.0, session="codex:s1",
                 reason="initial", model="m-a")
        _link_router(self.con, "router:s1", "req-snap", status="running",
                     started=0.0, ended=10.0, updated_at=10.0)
        first = report.task_report(self.con, "T-SNAP")["snapshot_id"]
        # Changing reason changes the report, so the snapshot must move.
        self.con.execute(
            "UPDATE attempts SET reason='pool_move' WHERE turn_id='router:s1'")
        self.con.commit()
        second = report.task_report(self.con, "T-SNAP")["snapshot_id"]
        self.assertNotEqual(first, second)
        # Changing the Router job status/updated_at also moves it.
        self.con.execute(
            "UPDATE attempts SET reason='initial' WHERE turn_id='router:s1'")
        self.con.execute(
            "UPDATE router_jobs SET status='succeeded', updated_at=99.0"
            " WHERE request_id='req-snap'")
        self.con.commit()
        third = report.task_report(self.con, "T-SNAP")["snapshot_id"]
        self.assertNotEqual(second, third)
        self.assertNotEqual(first, third)
        # Changing session_key and model_observed moves it as well.
        self.con.execute(
            "UPDATE attempts SET session_key='codex:s2', model_observed='m-b'"
            " WHERE turn_id='router:s1'")
        self.con.commit()
        fourth = report.task_report(self.con, "T-SNAP")["snapshot_id"]
        self.assertNotEqual(third, fourth)


class CliParityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "parity.db")

    def test_human_and_json_carry_same_measurements(self):
        self.assertEqual(run(self.db, "sync", "--source",
                             os.path.join(REPO, "tests", "fixtures", "codex-mini.jsonl")).returncode, 0)
        self.assertEqual(run(self.db, "capture", "create-task", "--task", "T-PAR",
                             "--project", "observer", "--title", "parity").returncode, 0)
        self.assertEqual(run(self.db, "capture", "assign", "--submission", "msg-mini-sub-01",
                             "--task", "T-PAR", "--evidence", "parity").returncode, 0)
        self.assertEqual(run(self.db, "capture", "assign", "--submission", "msg-mini-sub-02",
                             "--task", "T-PAR", "--evidence", "parity").returncode, 0)
        self.assertEqual(run(self.db, "capture", "attempt", "--task", "T-PAR",
                             "--turn", "codex:turn-mini-aaa", "--role", "parent",
                             "--state", "complete").returncode, 0)
        self.assertEqual(run(self.db, "capture", "outcome", "--task", "T-PAR",
                             "--state", "complete", "--candidate",
                             "0123456789abcdef0123456789abcdef01234567").returncode, 0)
        text = run(self.db, "task", "show", "--task", "T-PAR")
        self.assertIn(text.returncode, (0, 3), text.stderr)
        payload = json.loads(run(self.db, "task", "show", "--task", "T-PAR", "--json").stdout)
        self.assertEqual(payload["timing"]["completion_label"], "submission-to-accepted-completion")
        self.assertIn("submission-to-accepted-completion", text.stdout)
        self.assertIn("reconciled", text.stdout)
        self.assertIn("failed/production", text.stdout)
        self.assertIn("usage source coverage", text.stdout)
        self.assertEqual(payload["failures"]["failed_attempts"], 0)
        self.assertIn(str(payload["timing"]["completion_elapsed_s"]), text.stdout)

    def test_cli_retry_quota_cancel_parallel_shared(self):
        # Full public CLI path: serial retry in one Router request,
        # quota exhaustion with pool move, unknown-intent cancellation,
        # parallel sessions, and a shared session stay qualified.
        self.assertEqual(run(self.db, "sync", "--source",
                             os.path.join(REPO, "tests", "fixtures", "codex-mini.jsonl")).returncode, 0)
        self.assertEqual(run(self.db, "capture", "create-task", "--task", "T-CLI",
                             "--project", "observer", "--title", "cli").returncode, 0)
        self.assertEqual(run(self.db, "capture", "assign", "--submission", "msg-mini-sub-01",
                             "--task", "T-CLI", "--evidence", "cli").returncode, 0)
        self.assertEqual(run(self.db, "capture", "outcome", "--task", "T-CLI",
                             "--state", "active").returncode, 0)
        import sqlite3
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        con.execute(
            "INSERT INTO attempts(task_id, turn_id, role, harness, session_key, stage,"
            " started_at, ended_at, elapsed_s, state, terminal_class, reason)"
            " VALUES('T-CLI','router:q1','worker','router','codex:s1','implementation',"
            " 0.0,10.0,10.0,"
            " 'quota_blocked','quota','initial')")
        con.execute(
            "INSERT INTO attempts(task_id, turn_id, role, harness, session_key, stage,"
            " started_at, ended_at, elapsed_s, state, terminal_class, reason)"
            " VALUES('T-CLI','router:q2','worker','router','codex:s1','implementation',"
            " 11.0,20.0,9.0,"
            " 'complete','completed','pool_move')")
        con.execute(
            "INSERT INTO attempts(task_id, turn_id, role, harness, session_key,"
            " started_at, ended_at, elapsed_s, state, terminal_class)"
            " VALUES('T-CLI','router:c1','worker','router','codex:s2',0.0,5.0,5.0,"
            " 'cancelled','cancelled')")
        con.execute("INSERT OR IGNORE INTO router_jobs(request_id, status) VALUES('req-cli','running')")
        con.execute("INSERT OR IGNORE INTO router_invocations(invocation_id, request_id)"
                    " VALUES('q1','req-cli'),('q2','req-cli'),('c1','req-other')")
        con.commit()
        con.close()
        payload = json.loads(run(self.db, "task", "show", "--task", "T-CLI", "--json").stdout)
        text = run(self.db, "task", "show", "--task", "T-CLI")
        self.assertIn(text.returncode, (0, 3), text.stderr)
        # Quota counts as a provider failure inside production.
        self.assertEqual(payload["failures"]["failed_attempts"], 1)
        self.assertEqual(payload["failures"]["production_attempts"], 2)
        self.assertEqual(payload["failures"]["by_class"]["provider"], 1)
        self.assertEqual(payload["failures"]["cancelled"], 1)
        self.assertEqual(payload["failures"]["cancelled_unknown_intent"], 1)
        # Compatible quota recovery inside req-cli with exact gaps.
        rec = {r["failed_turn"]: r for r in payload["recovery"]}
        self.assertEqual(rec["router:q1"]["failure_to_next_start_s"], 1.0)
        self.assertEqual(rec["router:q1"]["time_to_first_progress_s"], 10.0)
        self.assertEqual(rec["router:q1"]["first_progress_stage"], "implementation")
        self.assertEqual(rec["router:q1"]["failed_stage"], "implementation")
        self.assertIn("recovered", rec["router:q1"]["recovery_outcome"])
        # Human text carries the same measurements and evidence links.
        self.assertIn("failed/production", text.stdout)
        self.assertIn("failure_to_next_start=1.0", text.stdout)
        self.assertIn("stage=implementation", text.stdout)
        self.assertIn("unknown_intent=1", text.stdout)
        self.assertIn("union_span", text.stdout)
        self.assertIn("usage source coverage", text.stdout)


if __name__ == "__main__":
    unittest.main()
