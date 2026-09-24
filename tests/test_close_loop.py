"""Issue 23 close-loop slice: attribution, sourced cost, snapshot, privacy.

Public CLI plus native fixture import through temporary ledgers, no
network. Covers two tasks in one session, multi-session scope, shared,
unassigned and unknown usage, native buckets and semantics, repeat
idempotence, exact local/published agreement, sourced cost with
caller-supplied offline schedules, partial versus complete coverage,
unknown pricing, invalid rates, TTL and tier ambiguity, native versus
estimate versus subscription, snapshot stability, task-scoped time and
diagnostics, explicit unknown acceptance, sanitized references,
session-only scope, target validation and privacy boundaries.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CODEX_SEM = "codex:input_includes_cached,output_includes_reasoning"


def run(db_path, *args):
    env = dict(os.environ, AGENT_OBSERVER_DB=db_path)
    return subprocess.run(
        [sys.executable, "-m", "agent_observer", *args],
        cwd=REPO, capture_output=True, text=True, env=env)


def write_prices(path, models, extra=None):
    data = {"source_url": "https://example.com/pricing",
            "as_of": "2026-09-24",
            "effective_date": "2026-09-24",
            "currency": "USD",
            "unit": "USD per million tokens",
            "models": models}
    if extra:
        data.update(extra)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


def fixture_rates():
    return {"gpt-6-fixture": {
        "semantics": [CODEX_SEM],
        "rates": {"input_tokens": 2.0, "cached_input_tokens": 1.0,
                  "cache_write_input_tokens": 3.0, "output_tokens": 8.0,
                  "reasoning_output_tokens": 8.0}}}


class CloseLoopCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "close.db")
        self.mini = os.path.join(REPO, "tests", "fixtures", "codex-mini.jsonl")
        self.prices = os.path.join(self.tmp.name, "prices.json")
        write_prices(self.prices, fixture_rates())

    def tearDown(self):
        self.tmp.cleanup()

    def _two_tasks(self):
        self.assertEqual(run(self.db, "sync", "--source", self.mini).returncode, 0)
        for task, sub in (("T-A", "msg-mini-sub-01"), ("T-B", "msg-mini-sub-02")):
            self.assertEqual(
                run(self.db, "capture", "create-task", "--task", task).returncode, 0)
            self.assertEqual(
                run(self.db, "capture", "assign", "--task", task,
                    "--submission", sub).returncode, 0)

    def _task_json(self, task, *extra):
        r = run(self.db, "task", "show", "--task", task, "--json", *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def _publish_json(self, task, *extra):
        r = run(self.db, "publish", "--task", task, "--dry-run", "--json", *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)


class TwoTaskAttributionTest(CloseLoopCase):
    def test_tasks_stay_separate_with_shared_outside_headline(self):
        self._two_tasks()
        a = self._task_json("T-A")
        b = self._task_json("T-B")
        self.assertEqual(a["attributed"]["total_tokens"], 3350)
        self.assertEqual(b["attributed"]["total_tokens"], 2150)
        self.assertEqual(a["models"][0]["input_tokens"], 3000)
        self.assertEqual(a["models"][0]["output_tokens"], 350)
        self.assertEqual(a["models"][0]["semantics"], CODEX_SEM)
        # Shared rows stay separate and never enter the attributed headline.
        self.assertEqual(a["shared_joint"]["responses"], 0)
        self.assertEqual(a["shared_models"], [])
        self.assertTrue(a["reconciles"])
        # Exact local/published agreement without pricing.
        pub = self._publish_json("T-A")
        self.assertEqual(pub["summary"]["usage"], a["attributed"])
        self.assertEqual(pub["summary"]["models"], a["models"])
        self.assertEqual(pub["summary"]["acceptance_state"], a["acceptance_state"])
        self.assertEqual(pub["summary"]["snapshot_id"], a["snapshot_id"])
        self.assertEqual(pub["summary"]["measured"], a["measured"])
        self.assertNotIn("5,500", pub["body"])

    def test_repeat_rendering_is_stable_and_changes_move_snapshot(self):
        self._two_tasks()
        first = self._task_json("T-A")
        second = self._task_json("T-A")
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["source_cutoff"], second["source_cutoff"])
        pub1 = self._publish_json("T-A")
        pub2 = self._publish_json("T-A")
        self.assertEqual(pub1["body"], pub2["body"])
        self.assertEqual(pub1["summary"]["snapshot_id"], first["snapshot_id"])
        # New evidence changes the cutoff identity.
        run(self.db, "capture", "outcome", "--task", "T-A",
            "--state", "complete", "--candidate", "abc1234")
        third = self._task_json("T-A")
        self.assertNotEqual(third["snapshot_id"], first["snapshot_id"])

    def test_repeat_import_adds_nothing(self):
        self.assertEqual(run(self.db, "sync", "--source", self.mini).returncode, 0)
        again = run(self.db, "sync", "--source", self.mini)
        self.assertEqual(again.returncode, 0)
        self.assertIn("0 new responses", again.stdout)


class SourcedCostTest(CloseLoopCase):
    def test_complete_estimate_with_caller_rates(self):
        self._two_tasks()
        a = self._task_json("T-A", "--prices", self.prices)
        est = a["estimated_cost"]
        self.assertEqual(est["responses"], 2)
        self.assertEqual(est["priced_responses"], 2)
        self.assertEqual(est["unpriced_responses"], 0)
        self.assertIsNotNone(est["estimated_cost_usd_total"])
        # Per-response scope under codex subset semantics:
        # resp-001 0.0028, resp-002 0.0045.
        self.assertAlmostEqual(est["estimated_cost_usd_total"], 0.0073, places=6)
        self.assertAlmostEqual(est["estimated_cost_usd_partial"], 0.0073, places=6)
        self.assertTrue(est["complete"])
        pub = self._publish_json("T-A", "--prices", self.prices)
        self.assertAlmostEqual(
            pub["summary"]["estimated_cost"]["estimated_cost_usd_total"],
            0.0073, places=6)
        self.assertIn("partial", pub["body"])
        self.assertIn("Complete total", pub["body"])
        self.assertIn("example.com", pub["body"])

    def test_partial_when_cached_rate_missing(self):
        partial = os.path.join(self.tmp.name, "partial.json")
        write_prices(partial, {"gpt-6-fixture": {
            "semantics": [CODEX_SEM],
            "rates": {"input_tokens": 2.0, "output_tokens": 8.0,
                      "reasoning_output_tokens": 8.0}}})
        self._two_tasks()
        a = self._task_json("T-A", "--prices", partial)
        est = a["estimated_cost"]
        # resp-001 has cached 0 so it prices; resp-002 has cached 1500
        # with no cached rate, so it stays unknown.
        self.assertEqual(est["priced_responses"], 1)
        self.assertEqual(est["unpriced_responses"], 1)
        self.assertIsNone(est["estimated_cost_usd_total"])
        self.assertAlmostEqual(est["estimated_cost_usd_partial"], 0.0028, places=6)
        self.assertFalse(est["complete"])
        pub = self._publish_json("T-A", "--prices", partial)
        self.assertIn("No complete total", pub["body"])
        self.assertIn("unknown", pub["body"].lower())

    def test_unknown_model_and_semantics_stay_unknown(self):
        other = os.path.join(self.tmp.name, "other.json")
        write_prices(other, {"some-other-model": {
            "semantics": [CODEX_SEM],
            "rates": {"input_tokens": 1.0, "cached_input_tokens": 1.0,
                      "cache_write_input_tokens": 1.0, "output_tokens": 1.0,
                      "reasoning_output_tokens": 1.0}}})
        self._two_tasks()
        a = self._task_json("T-A", "--prices", other)
        self.assertEqual(a["estimated_cost"]["priced_responses"], 0)
        self.assertIsNone(a["estimated_cost"]["estimated_cost_usd_total"])
        wrong_sem = os.path.join(self.tmp.name, "wrongsem.json")
        write_prices(wrong_sem, {"gpt-6-fixture": {
            "semantics": ["claude:input_excludes_cache,output_includes_thinking"],
            "rates": {"input_tokens": 1.0, "cached_input_tokens": 1.0,
                      "cache_write_input_tokens": 1.0, "output_tokens": 1.0,
                      "reasoning_output_tokens": 1.0}}})
        b = self._task_json("T-A", "--prices", wrong_sem)
        self.assertEqual(b["estimated_cost"]["priced_responses"], 0)
        self.assertIn("unsupported semantics",
                      json.dumps(b["estimated_cost"]["unpriced_reasons"]))

    def test_invalid_rates_fail_closed(self):
        self._two_tasks()
        for bad in ({"input_tokens": -1.0}, {"input_tokens": True},
                    {"input_tokens": float("inf")}):
            path = os.path.join(self.tmp.name, f"bad{len(str(bad))}.json")
            write_prices(path, {"gpt-6-fixture": {
                "semantics": [CODEX_SEM],
                "rates": dict({"cached_input_tokens": 1.0,
                               "cache_write_input_tokens": 1.0,
                               "output_tokens": 1.0,
                               "reasoning_output_tokens": 1.0}, **bad)}})
            r = run(self.db, "task", "show", "--task", "T-A",
                    "--json", "--prices", path)
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn("invalid price schedule", r.stderr)

    def test_default_schedule_leaves_everything_unknown(self):
        self._two_tasks()
        a = self._task_json("T-A")
        self.assertEqual(a["estimated_cost"]["status"], "unknown")
        self.assertIsNone(a["estimated_cost"]["estimated_cost_usd_total"])
        pub = self._publish_json("T-A")
        self.assertIn("unknown", pub["body"].lower())
        self.assertIn("unknown model", pub["body"].lower())


class PricingUnitTest(unittest.TestCase):
    def test_ttl_dict_and_tier_ambiguity_stay_unknown(self):
        from agent_observer import pricing as _pricing
        schedule = _pricing.validate_schedule({
            "source_url": "https://example.com/pricing",
            "as_of": "2026-09-24", "currency": "USD",
            "unit": "USD per million tokens",
            "models": {"m": {
                "semantics": [CODEX_SEM],
                "rates": {"input_tokens": 1.0, "cached_input_tokens": 1.0,
                          "cache_write_input_tokens": {"5m": 1.0, "1h": 2.0},
                          "output_tokens": 1.0, "reasoning_output_tokens": 1.0}}}})
        resp = {"model": "m", "semantics": CODEX_SEM, "input_tokens": 100,
                "cached_input_tokens": 10, "cache_write_input_tokens": 5,
                "output_tokens": 20, "reasoning_output_tokens": 2,
                "total_tokens": 120}
        cost, reason = _pricing.price_response(resp, schedule)
        self.assertIsNone(cost)
        self.assertIn("TTL", reason)
        tiered = _pricing.validate_schedule({
            "source_url": "https://example.com/pricing",
            "as_of": "2026-09-24", "currency": "USD",
            "unit": "USD per million tokens",
            "models": {"m": {
                "semantics": [CODEX_SEM],
                "long_context_threshold": 50,
                "rates": {"input_tokens": 1.0, "cached_input_tokens": 1.0,
                          "cache_write_input_tokens": 1.0, "output_tokens": 1.0,
                          "reasoning_output_tokens": 1.0}}}})
        cost, reason = _pricing.price_response(resp, tiered)
        self.assertIsNone(cost)
        self.assertIn("long-context", reason)
        # Unknown counters never become zero.
        missing = dict(resp, cached_input_tokens=None)
        cost, reason = _pricing.price_response(
            missing, _pricing.validate_schedule({
                "source_url": "https://example.com/pricing",
                "as_of": "2026-09-24", "currency": "USD",
                "unit": "USD per million tokens",
                "models": {"m": {
                    "semantics": [CODEX_SEM],
                    "rates": {"input_tokens": 1.0, "cached_input_tokens": 1.0,
                              "cache_write_input_tokens": 1.0,
                              "output_tokens": 1.0,
                              "reasoning_output_tokens": 1.0}}}}))
        self.assertIsNone(cost)
        self.assertIn("unknown counter", reason)

    def test_never_prices_from_total(self):
        from agent_observer import pricing as _pricing
        schedule = _pricing.validate_schedule({
            "source_url": "https://example.com/pricing",
            "as_of": "2026-09-24", "currency": "USD",
            "unit": "USD per million tokens",
            "models": {"m": {
                "semantics": [CODEX_SEM],
                "rates": {"input_tokens": 2.0, "cached_input_tokens": 1.0,
                          "cache_write_input_tokens": 3.0, "output_tokens": 8.0,
                          "reasoning_output_tokens": 8.0}}}})
        # Same total, different buckets: different prices, so totals alone
        # can never stand in for per-response scope.
        a = {"model": "m", "semantics": CODEX_SEM, "input_tokens": 1000,
             "cached_input_tokens": 0, "cache_write_input_tokens": 0,
             "output_tokens": 100, "reasoning_output_tokens": 10,
             "total_tokens": 1100}
        b = {"model": "m", "semantics": CODEX_SEM, "input_tokens": 500,
             "cached_input_tokens": 500, "cache_write_input_tokens": 0,
             "output_tokens": 100, "reasoning_output_tokens": 10,
             "total_tokens": 1100}
        ca, _ = _pricing.price_response(a, schedule)
        cb, _ = _pricing.price_response(b, schedule)
        self.assertNotEqual(ca, cb)


class TaskScopeEvidenceTest(CloseLoopCase):
    def test_shared_unassigned_unknown_and_multi_session(self):
        self.assertEqual(run(self.db, "sync", "--source", self.mini).returncode, 0)
        parent = os.path.join(REPO, "tests", "fixtures", "codex-parent.jsonl")
        child = os.path.join(REPO, "tests", "fixtures", "codex-child.jsonl")
        self.assertEqual(run(self.db, "sync", "--source", parent).returncode, 0)
        self.assertEqual(run(self.db, "sync", "--source", child).returncode, 0)
        for task in ("T-A", "T-B"):
            self.assertEqual(
                run(self.db, "capture", "create-task", "--task", task).returncode, 0)
        # T-A owns the first mini submission plus the whole parent session.
        self.assertEqual(run(self.db, "capture", "assign", "--task", "T-A",
                             "--submission", "msg-mini-sub-01").returncode, 0)
        r = run(self.db, "task", "show", "--task", "T-A", "--json")
        # Missing second mini submission stays visible, exit 3.
        self.assertEqual(r.returncode, 3)
        payload = json.loads(r.stdout)
        self.assertIn("codex:msg-mini-sub-02", payload["missing_assignments"])
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["unassigned_in_scope"]["responses"], 1)
        self.assertEqual(payload["scope_kind"], "task")
        self.assertTrue(payload["snapshot_id"])
        self.assertIsNotNone(payload["source_cutoff"])
        # Task diagnostics are turn-scoped: the interrupt on turn ccc is
        # outside T-A, so it stays session context, never task-only.
        self.assertEqual(payload["diagnostics"]["task_scoped_counts"], {})
        self.assertEqual(
            payload["diagnostics"]["session_context_counts"].get("human_correction"), 1)
        self.assertIn("task turns", payload["time"]["task_elapsed_source"])
        # Shared with --shared stays outside the attributed headline.
        for task in ("T-S1", "T-S2"):
            self.assertEqual(
                run(self.db, "capture", "create-task", "--task", task).returncode, 0)
            self.assertEqual(run(self.db, "capture", "assign", "--task", task,
                                 "--submission", "msg-mini-sub-02",
                                 "--shared").returncode, 0)
        shared = json.loads(run(self.db, "task", "show", "--task", "T-S1",
                                "--json").stdout)
        self.assertEqual(shared["attributed"]["responses"], 0)
        self.assertEqual(shared["shared_joint"]["total_tokens"], 2150)
        self.assertEqual(len(shared["joint_assignments"]), 1)

    def test_explicit_unknown_acceptance_and_active_work(self):
        self._two_tasks()
        a = self._task_json("T-A")
        self.assertEqual(a["acceptance_state"], "unknown")
        self.assertIsNone(a["outcome"])
        pub = self._publish_json("T-A")
        self.assertIn("Acceptance: unknown", pub["body"])
        # A successful attempt never implies acceptance.
        self.assertEqual(run(self.db, "capture", "attempt", "--task", "T-A",
                             "--turn", "codex:turn-mini-aaa", "--role", "parent",
                             "--state", "complete").returncode, 0)
        again = self._task_json("T-A")
        self.assertEqual(again["acceptance_state"], "unknown")
        # Active attempts remain visible.
        self.assertEqual(run(self.db, "capture", "attempt", "--task", "T-A",
                             "--turn", "codex:turn-mini-bbb", "--role", "worker",
                             "--state", "active").returncode, 0)
        active = self._task_json("T-A")
        self.assertTrue(active["has_active_work"])
        pub2 = self._publish_json("T-A")
        self.assertIn("Active work remains visible", pub2["body"])

    def test_sanitized_refs_and_session_only_scope(self):
        self._two_tasks()
        self.assertEqual(run(self.db, "capture", "outcome", "--task", "T-A",
                             "--state", "complete", "--candidate",
                             "https://github.com/o/r/pull/7",
                             "--proof", "abc1234",
                             "--repairs", "<script>alert(1)</script>",
                             "--corrections", "/tmp/secret prompt text").returncode, 0)
        pub = self._publish_json("T-A")
        self.assertIn("https://github.com/o/r/pull/7", pub["body"])
        self.assertIn("abc1234", pub["body"])
        self.assertIn("[withheld", pub["body"])
        self.assertNotIn("<script>", pub["body"])
        self.assertNotIn("/tmp/secret", pub["body"])
        # Session-only summaries name their limited scope.
        r = run(self.db, "sessions", "list", "--json")
        session = json.loads(r.stdout)["sessions"][0]["session_key"]
        s = json.loads(run(self.db, "publish", "--session", session,
                           "--dry-run", "--json").stdout)
        self.assertEqual(s["summary"]["scope_kind"], "session")
        self.assertIn("Session scope only", s["body"])
        self.assertIsNone(s["summary"]["acceptance_state"])

    def test_target_validation_and_privacy(self):
        from agent_observer import publish as _publish
        with self.assertRaises(ValueError):
            _publish.validate_target("not-a-repo", pr=1)
        with self.assertRaises(ValueError):
            _publish.validate_target("o/r", pr=-3)
        with self.assertRaises(ValueError):
            _publish.validate_target("o/r", commit="not a sha!!")
        with self.assertRaises(ValueError):
            _publish.post("o/r", "b")
        with self.assertRaises(ValueError):
            _publish.post("o/r", "b", pr=1, commit="abc123")
        with self.assertRaises(ValueError):
            _publish.post("bad repo", "b", pr=1)
        self._two_tasks()
        pub = self._publish_json("T-A", "--prices", self.prices)
        body = pub["body"]
        for secret in ("Please summarize the project status",
                       "redacted summary", "/redacted/workspace"):
            self.assertNotIn(secret, body)
        # Native cost stays separate from the list-price estimate and
        # subscription readings are never posted as spend.
        self.assertIn("Native harness-reported cost", body)
        self.assertIn("Subscription spending is separate", body)
        local = self._task_json("T-A", "--prices", self.prices)
        self.assertIn("native_cost", local)
        self.assertIn("subscription_note", local)
        self.assertNotIn("router_readings", body)


if __name__ == "__main__":
    unittest.main()


class DedicatedSessionCaptureTest(CloseLoopCase):
    def test_exclusive_session_capture_and_conflict(self):
        self.assertEqual(run(self.db, 'sync', '--source', self.mini).returncode, 0)
        for task in ('dedicated', 'other'):
            self.assertEqual(run(self.db, 'capture', 'create-task', '--task', task).returncode, 0)
        args = ('capture', 'assign-session', '--task', 'dedicated',
                '--session', 'codex:mini-session-123', '--exclusive',
                '--evidence', 'dedicated-dispatch-record')
        # Read the actual session identity from the fixture import, not a guessed UUID.
        sessions = json.loads(run(self.db, 'sessions', 'list', '--json').stdout)
        session = sessions['sessions'][0]['session_key']
        args = tuple(session if x == 'codex:mini-session-123' else x for x in args)
        a = run(self.db, *args)
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertEqual(run(self.db, *args).returncode, 0)
        self.assertEqual(self._task_json('dedicated')['attributed']['total_tokens'], 5500)
        conflict = run(self.db, 'capture', 'assign-session', '--task', 'other',
                       '--session', session, '--exclusive', '--evidence', 'other-dispatch')
        self.assertEqual(conflict.returncode, 2)
        self.assertIn('ownership conflict', conflict.stderr)
        unknown = run(self.db, 'capture', 'assign-session', '--task', 'dedicated',
                      '--session', 'claude:missing', '--exclusive', '--evidence', 'missing')
        self.assertEqual(unknown.returncode, 2)
        no_attestation = run(self.db, 'capture', 'assign-session', '--task', 'dedicated',
                             '--session', session, '--evidence', 'no-attestation')
        self.assertEqual(no_attestation.returncode, 2)


class IntegratedCostEvidenceTest(CloseLoopCase):
    def test_price_schedule_changes_snapshot(self):
        self._two_tasks()
        first = self._task_json('T-A', '--prices', self.prices)
        rates = fixture_rates()
        rates['gpt-6-fixture']['rates']['input_tokens'] = 9.0
        write_prices(self.prices, rates)
        second = self._task_json('T-A', '--prices', self.prices)
        self.assertNotEqual(first['snapshot_id'], second['snapshot_id'])

    def test_bundled_astra_standard_rate_and_long_context(self):
        from agent_observer import pricing
        schedule = pricing.load_schedule()
        response = dict(model='gpt-6-astra', semantics=CODEX_SEM,
                        input_tokens=1000, cached_input_tokens=800,
                        cache_write_input_tokens=0, output_tokens=100,
                        reasoning_output_tokens=40)
        self.assertAlmostEqual(pricing.price_response(response, schedule)[0], .0078)
        response.update(input_tokens=300000, cached_input_tokens=200000)
        self.assertAlmostEqual(pricing.price_response(response, schedule)[0], 2.4075)

    def test_native_partial_is_not_complete_total(self):
        from agent_observer import report
        result = report._native_cost([dict(cost_usd=.5), dict(cost_usd=None)])
        self.assertIsNone(result['total_usd'])
        self.assertEqual(result['known_subtotal_usd'], .5)

    def test_attempt_can_advance_without_losing_model(self):
        self._two_tasks()
        for state in ('active', 'complete'):
            r = run(self.db, 'capture', 'attempt', '--task', 'T-A',
                    '--turn', 'codex:aaa', '--role', 'parent', '--state', state,
                    *(['--model', 'gpt-6-astra'] if state == 'active' else []))
            self.assertEqual(r.returncode, 0, r.stderr)
        attempt = self._task_json('T-A')['attempts'][0]
        self.assertEqual(attempt['state'], 'complete')
        self.assertEqual(attempt['model_observed'], 'gpt-6-astra')
