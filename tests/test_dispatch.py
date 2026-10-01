"""Dispatch-mode comparison over fabricated T3 thread trees (#44).

One planner runs three jobs. Job A is planner mode: the planner starts a
worker and a reviewer. Job B is dispatcher mode: the planner starts a
dispatcher that starts its own worker. Job C has no role-named child, so
its mode stays unknown, and its usage is partly unknown. Every expected
figure below is worked out by hand from the fixture, not from the code.
"""

import io
import json
import os
import sqlite3
from contextlib import redirect_stdout
from unittest import mock

from agent_observer import cli, db, dispatch
from agent_observer.adapters import t3 as t3_adapter
from tests.helpers import LedgerCase

P = "p1anner-0000-4000-8000-000000000001"
WA = f"sub.{P}.worker-aaaa1111"
RV = f"sub.{P}.reviewer-bbbb2222"
DP = f"sub.{P}.dispatcher-cccc3333"
WB = f"sub.{DP}.worker-dddd4444"
OLD = f"sub.{P}.eeee5555"
SEM = "codex:input_includes_cached"
SCHEDULE = {"models": {"m": {"semantics": [SEM], "rates": {
    b: 1.0 for b in ("input_tokens", "cached_input_tokens",
                     "cache_write_input_tokens", "output_tokens",
                     "reasoning_output_tokens")}}}}


def at(clock):
    return f"2026-10-01T{clock}Z"


def epoch(clock):
    return dispatch.iso_ts(at(clock))


def write_state(path):
    native = sqlite3.connect(path)
    native.executescript("""
    CREATE TABLE projection_threads(thread_id TEXT, created_at TEXT);
    CREATE TABLE orchestration_events(sequence INTEGER PRIMARY KEY,
     stream_id TEXT, event_type TEXT, occurred_at TEXT, actor_kind TEXT,
     payload_json TEXT);
    CREATE TABLE projection_thread_messages(message_id TEXT, thread_id TEXT,
     role TEXT, text TEXT);
    CREATE TABLE projection_thread_pull_requests(thread_id TEXT, host TEXT,
     repository TEXT, number INTEGER, url TEXT, source TEXT, linked_at TEXT,
     snapshot_json TEXT);
    CREATE TABLE fork_thread_issue_links(thread_id TEXT, host TEXT,
     repository TEXT, number INTEGER, url TEXT, source TEXT, linked_at TEXT);
    """)
    for thread, clock in ((P, "09:00:00"), (WA, "10:00:30"),
                          (DP, "10:00:40"), (WB, "10:02:40"),
                          (RV, "10:21:00"), (OLD, "08:00:10")):
        native.execute("INSERT INTO projection_threads VALUES(?,?)",
                       (thread, at(clock)))
    n = 0

    def turn(thread, clock, actor, text=""):
        nonlocal n
        n += 1
        mid = f"m{n}"
        native.execute(
            "INSERT INTO orchestration_events(stream_id, event_type,"
            " occurred_at, actor_kind, payload_json) VALUES(?,?,?,?,?)",
            (thread, "thread.turn-start-requested", at(clock), actor,
             json.dumps({"threadId": thread, "messageId": mid})))
        native.execute("INSERT INTO projection_thread_messages VALUES(?,?,?,?)",
                       (mid, thread, "user", text))

    def report(child):
        return f"[Subagent Some title (thread {child}) finished a turn]\n\nbody"

    turn(P, "08:00:00", "client")                  # starts old job C
    turn(OLD, "08:00:10", "server", "do C")
    turn(P, "10:00:00", "client")                  # spawns A and B: shared
    turn(WA, "10:00:30", "server", "do A")
    turn(DP, "10:00:40", "server", "dispatch B")
    turn(DP, "10:05:00", "server", report(WB))     # B's own report: not the planner's
    turn(P, "10:20:00", "server", report(WA))      # A only, and spawns RV
    turn(RV, "10:21:00", "server", "review A")
    turn(P, "10:40:00", "server", report(DP))      # B only
    turn(P, "11:00:00", "client", "unrelated chat")  # no job

    def pr(thread, number, state):
        native.execute(
            "INSERT INTO projection_thread_pull_requests VALUES(?,?,?,?,?,?,?,?)",
            (thread, "github.com", "o/r", number,
             f"https://github.com/o/r/pull/{number}", "agent", None,
             json.dumps({"state": state})))

    pr(WA, 10, "open")
    pr(RV, 10, "open")
    pr(WB, 12, "open")
    pr(OLD, 5, "merged")
    pr(P, 10, "open")  # the planner's own link adds no job thread
    native.execute(
        "INSERT INTO fork_thread_issue_links VALUES(?,?,?,?,?,?,?)",
        (DP, "github.com", "o/r", 11, "https://github.com/o/r/issues/11",
         "agent", None))
    native.commit()
    native.close()


def approvals(item):
    return {"o/r#10": {"approved_at": epoch("10:50:00"), "error": None},
            "o/r#12": {"approved_at": epoch("11:10:00"), "error": None},
            "o/r#5": {"approved_at": None, "error": None}}[item]


class DispatchModesTest(LedgerCase):
    def setUp(self):
        super().setUp()
        t3_adapter._ensure_tables(self.con)
        self.state = os.path.join(self.tmp.name, "state.sqlite")
        write_state(self.state)
        self.source = self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('x','p','s',0)").lastrowid
        self.n = 0
        self.thread(P, "claude", "pl", [
            ("07:59:00", 9000),     # before any planner turn: no job
            ("08:00:05", 50),       # old job C's start turn
            ("10:00:10", 1000), ("10:00:50", 1000),   # shared A/B: 1000 each
            ("10:20:30", 600),      # A
            ("10:40:10", 400),      # B
            ("11:00:10", 5000)])    # unrelated: no job
        self.thread(WA, "codex", "wa", [("10:01:00", 2000)])
        self.thread(RV, "codex", "rv", [("10:22:00", 500)])
        self.thread(DP, "codex", "dp", [("10:01:00", 300)])
        self.thread(WB, "claude", "wb", [("10:03:00", 3000)])
        self.thread(OLD, "codex", "c", [("08:01:00", 70), ("08:02:00", None)])

    def thread(self, thread_id, harness, native, responses):
        key = f"{harness}:{native}"
        db.upsert_session(self.con, key, harness, native, None)
        self.con.execute(
            "INSERT INTO t3_threads(thread_id, root_thread_id, provider,"
            " adapter_key, native_session) VALUES(?,?,?,?,?)",
            (thread_id, t3_adapter.thread_root(thread_id), harness, harness,
             native))
        for clock, tokens in responses:
            self.n += 1
            self.con.execute(
                "INSERT INTO responses(response_id, source_id, harness,"
                " session_key, ts, model, semantics, input_tokens,"
                " cached_input_tokens, cache_write_input_tokens,"
                " output_tokens, reasoning_output_tokens, total_tokens)"
                " VALUES(?,?,?,?,?,'m',?,?,0,0,0,0,?)",
                (f"r{self.n}", self.source, harness, key, epoch(clock), SEM,
                 tokens, tokens))

    def compare(self, **kw):
        return dispatch.compare(self.con, self.state, schedule=SCHEDULE,
                                approval=approvals, **kw)

    def jobs(self, payload):
        return {j["job"]: j for j in payload["jobs"]}

    def test_modes_come_from_the_thread_tree(self):
        jobs = self.jobs(self.compare())
        self.assertEqual(set(jobs), {"o/r#10", "o/r#12", "o/r#5"})
        self.assertEqual(jobs["o/r#10"]["mode"], "planner")
        self.assertEqual(jobs["o/r#10"]["children"], sorted([WA, RV]))
        # The dispatcher subtree delivers PR 12 for Issue 11: one job.
        self.assertEqual(jobs["o/r#12"]["mode"], "dispatcher")
        self.assertEqual(jobs["o/r#12"]["children"], [DP])
        self.assertEqual(jobs["o/r#5"]["mode"], "unknown")

    def test_timings_start_at_the_planner_turn_that_started_the_job(self):
        jobs = self.jobs(self.compare())
        self.assertEqual(jobs["o/r#10"]["start"], epoch("10:00:00"))
        self.assertAlmostEqual(jobs["o/r#10"]["first_worker_s"], 30)
        self.assertAlmostEqual(jobs["o/r#12"]["first_worker_s"], 160)
        self.assertAlmostEqual(jobs["o/r#10"]["approval_s"], 50 * 60)
        self.assertAlmostEqual(jobs["o/r#12"]["approval_s"], 70 * 60)
        self.assertIsNone(jobs["o/r#5"]["first_worker_s"])

    def test_tokens_and_cost_cover_every_job_thread_and_planner_turn(self):
        jobs = self.jobs(self.compare())
        # A: planner 1000 (half of the shared spawn turn) + 600, worker
        # 2000, reviewer 500.
        self.assertEqual(jobs["o/r#10"]["tokens"], 4100)
        self.assertAlmostEqual(jobs["o/r#10"]["cost_usd"], 4100 / 1e6)
        self.assertEqual(jobs["o/r#10"]["planner_turns"], 2)
        self.assertEqual(jobs["o/r#10"]["shared_planner_turns"], 1)
        # B: planner 1000 + 400, dispatcher 300, nested worker 3000.
        self.assertEqual(jobs["o/r#12"]["tokens"], 4700)
        self.assertAlmostEqual(jobs["o/r#12"]["cost_usd"], 4700 / 1e6)
        self.assertEqual(jobs["o/r#12"]["planner_turns"], 2)

    def test_missing_usage_is_unknown_not_zero(self):
        payload = self.compare()
        old = self.jobs(payload)["o/r#5"]
        self.assertIsNone(old["tokens"])
        self.assertEqual(old["unknown_token_responses"], 1)
        unknown = payload["modes"]["unknown"]
        self.assertEqual(unknown["tokens_per_job"], {"value": None, "n": 0})
        self.assertEqual(unknown["median_first_worker_s"],
                         {"value": None, "n": 0})
        text = dispatch.render(payload)
        self.assertIn("tokens per job unknown (n=0)", text)
        self.assertNotIn("tokens per job 0", text)

    def test_mode_summaries_carry_sample_sizes(self):
        modes = self.compare()["modes"]
        planner, disp = modes["planner"], modes["dispatcher"]
        self.assertEqual(planner["jobs"], 1)
        self.assertEqual(planner["succeeded"], {"value": 1, "n": 1})
        self.assertEqual(planner["success_rate"], {"value": 1.0, "n": 1})
        self.assertEqual(planner["tokens_per_job"], {"value": 4100, "n": 1})
        self.assertEqual(disp["median_approval_s"], {"value": 4200, "n": 1})
        text = dispatch.render(self.compare())
        self.assertIn("dispatcher mode: 1 jobs, succeeded 1 (n=1), success rate"
                      " 100% (n=1), median time to first worker 2.7 min (n=1)",
                      text)

    def test_unreachable_github_leaves_success_unknown(self):
        def offline(item):
            return {"approved_at": None, "error": "gh: network"}
        payload = dispatch.compare(self.con, self.state, schedule=SCHEDULE,
                                   approval=offline)
        jobs = self.jobs(payload)
        self.assertIsNone(jobs["o/r#10"]["succeeded"])
        self.assertTrue(jobs["o/r#5"]["succeeded"])  # merged needs no GitHub
        self.assertEqual(payload["modes"]["planner"]["success_rate"],
                         {"value": None, "n": 0})

    def test_job_set_and_window_select_jobs(self):
        # An Issue picks the job that delivers it.
        picked = self.compare(jobs=["o/r#11"])
        self.assertEqual([j["job"] for j in picked["jobs"]], ["o/r#12"])
        later = self.compare(since=epoch("09:00:00"))
        self.assertEqual(sorted(j["job"] for j in later["jobs"]),
                         ["o/r#10", "o/r#12"])
        none = self.compare(planners=["other-planner"])
        self.assertEqual(none["jobs"], [])

    def test_cli_reports_both_modes(self):
        self.con.commit()
        out = io.StringIO()
        with mock.patch.object(dispatch, "fetch_approval", approvals), \
                mock.patch.object(dispatch.pricing, "default_schedule",
                                  return_value=SCHEDULE), redirect_stdout(out):
            code = cli.main(["--db", self.db_path, "dispatch-modes",
                             "--t3-state", self.state, "--planner", P,
                             "--since", "2026-10-01T09:00:00Z"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("planner mode: 1 jobs", text)
        self.assertIn("dispatcher mode: 1 jobs", text)
        self.assertIn("- o/r#12 [dispatcher] succeeded yes", text)


class UnmappedThreadTest(DispatchModesTest):
    """A job thread without a ledger session makes that job's usage unknown."""

    def add_grok_reviewer(self):
        grok = f"sub.{P}.reviewer-ffff6666"
        native = sqlite3.connect(self.state)
        native.execute("INSERT INTO projection_threads VALUES(?,?)",
                       (grok, at("10:25:00")))
        native.execute(
            "INSERT INTO projection_thread_pull_requests VALUES(?,?,?,?,?,?,?,?)",
            (grok, "github.com", "o/r", 10, "https://github.com/o/r/pull/10",
             "agent", None, json.dumps({"state": "open"})))
        native.execute(
            "CREATE TABLE provider_session_runtime(thread_id TEXT,"
            " provider_name TEXT, adapter_key TEXT, resume_cursor_json TEXT)")
        native.execute(
            "INSERT INTO provider_session_runtime VALUES(?,?,?,?)",
            (grok, "grok", "grok", json.dumps({"sessionId": "grok-1"})))
        native.commit()
        # The real T3 import maps no Grok cursor to a ledger session.
        native.row_factory = sqlite3.Row
        totals = {"malformed": 0}
        t3_adapter._import_threads(self.con, native, totals)
        native.close()
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM t3_threads WHERE thread_id=?", (grok,)).fetchone())

    def test_unmapped_child_makes_job_usage_unknown(self):
        self.add_grok_reviewer()
        payload = self.compare()
        a = self.jobs(payload)["o/r#10"]
        self.assertIsNone(a["tokens"])
        self.assertIsNone(a["cost_usd"])
        self.assertEqual(a["unmapped_threads"], 1)
        # The job leaves the known-value mean and its sample size.
        planner = payload["modes"]["planner"]
        self.assertEqual(planner["tokens_per_job"], {"value": None, "n": 0})
        self.assertEqual(planner["cost_per_job_usd"], {"value": None, "n": 0})
        # Other jobs keep their known usage.
        self.assertEqual(self.jobs(payload)["o/r#12"]["tokens"], 4700)
        self.assertIn("tokens per job unknown (n=0)", dispatch.render(payload))

    def test_unmapped_planner_makes_coordinated_jobs_unknown(self):
        self.con.execute("DELETE FROM t3_threads WHERE thread_id=?", (P,))
        jobs = self.jobs(self.compare())
        self.assertIsNone(jobs["o/r#10"]["tokens"])
        self.assertIsNone(jobs["o/r#12"]["tokens"])
        self.assertEqual(jobs["o/r#12"]["unmapped_threads"], 1)


class ApprovalTest(LedgerCase):
    def test_first_success_status_or_approving_review_wins(self):
        replies = {
            ("pr", "view"): {"reviews": [
                {"state": "COMMENTED", "submittedAt": "2026-10-01T10:00:00Z"},
                {"state": "APPROVED", "submittedAt": "2026-10-01T12:00:00Z"}],
                "commits": [{"oid": "a1"}, {"oid": "b2"}]},
            "a1": [{"context": "review/independent", "state": "failure",
                    "created_at": "2026-10-01T10:30:00Z"}],
            "b2": [{"context": "review/independent", "state": "success",
                    "created_at": "2026-10-01T11:00:00Z"},
                   {"context": "ci", "state": "success",
                    "created_at": "2026-10-01T10:45:00Z"}]}

        def fake(args):
            if args[0] == "pr":
                return replies[("pr", "view")]
            return replies[args[1].split("/")[-2]]

        with mock.patch.object(dispatch, "_gh_json", side_effect=fake):
            got = dispatch.fetch_approval("o/r#10")
        self.assertEqual(got, {"approved_at": epoch("11:00:00"), "error": None})

    def test_gh_failure_is_an_error_not_a_missing_approval(self):
        with mock.patch.object(dispatch, "_gh_json",
                               side_effect=RuntimeError("no network")):
            got = dispatch.fetch_approval("o/r#10")
        self.assertIsNone(got["approved_at"])
        self.assertEqual(got["error"], "no network")
