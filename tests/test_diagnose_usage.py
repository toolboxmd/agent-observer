"""Counterexamples for context and cache observations at the CLI boundary."""

import contextlib
import io
import json
from pathlib import Path

from agent_observer import cli, db, native_response
from agent_observer.adapters import claude, codex, grok
from tests.helpers import LedgerCase

FIXTURES = Path(__file__).parent / "fixtures"


class DiagnoseUsageTest(LedgerCase):
    def session(self, harness, name):
        key = f"{harness}:{name}"
        source = self.con.execute(
            "INSERT INTO sources(harness,path,sha256,imported_at) VALUES(?,?,?,0)",
            (harness, f"/fixture/{name}", name)).lastrowid
        db.upsert_session(self.con, key, harness, name, source, started_at=10, ended_at=40)
        return key, source

    def response(self, session, rid, ts, turn, sem, input_tokens, read, write=None, overlap=0):
        key, source = session
        self.con.execute(
            "INSERT INTO responses(response_id,session_key,source_id,harness,ts,ordinal_num,"
            " turn_id,semantics,input_tokens,cached_input_tokens,cache_write_input_tokens,is_overlap)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, key, source, key.split(":")[0], ts, ts, turn, sem, input_tokens, read, write, overlap))

    def run_diagnose(self, *options, as_json=True):
        self.con.commit()
        args = ["--db", self.db_path, "diagnose", *options]
        if as_json:
            args.append("--json")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(args), 0)
        return json.loads(out.getvalue()) if as_json else out.getvalue()

    def test_context_samples_preserve_turns_compaction_and_source_references(self):
        session = self.session("codex", "context")
        key, source = session
        sem = "codex:input_includes_cached,output_includes_reasoning"
        self.response(session, "after", 25, "turn-1", sem, 30, 10)
        self.response(session, "first", 12, "turn-1", sem, 100, 20)
        self.response(session, "before", 18, "turn-1", sem, 160, 60)
        self.response(session, "overlap", 19, "turn-1", sem, 900, 900, overlap=1)
        self.response(session, "unknown", 27, None, sem, None, None)
        self.response(session, "next", 30, "turn-2", sem, 50, 20)
        boundary = self.con.execute(
            "INSERT INTO events(session_key,source_id,family,native_id,turn_id,ts,ordinal_num)"
            " VALUES(?,?,'compaction','boundary','turn-1',NULL,20)", session).lastrowid

        result = self.run_diagnose()
        timeline = result["context_growth"][0]["timeline"]
        self.assertEqual(result["context_growth"][0]["session"], key)
        self.assertEqual([point["ts"] for point in timeline], [12, 18, None, 25, 27, 30])
        self.assertEqual([point["turn_id"] for point in timeline],
                         ["turn-1", "turn-1", "turn-1", "turn-1", None, "turn-2"])
        self.assertEqual([point.get("input_tokens") for point in timeline],
                         [100, 160, None, 30, None, 50])
        self.assertEqual(timeline[2]["event_id"], boundary)
        self.assertEqual(timeline[3]["response_id"], "after")
        self.assertEqual(timeline[3]["source_id"], source)
        self.assertEqual(timeline[3]["ordinal_num"], 25)
        self.assertEqual(self.run_diagnose(), result)
        rendered = self.run_diagnose(as_json=False)
        self.assertIn("160 input tokens", rendered)
        self.assertIn(f"event {boundary}", rendered)
        self.assertIn("unknown input tokens", rendered)

    def test_cache_uses_native_denominators_and_keeps_missing_distinct_from_zero(self):
        codex = self.session("codex", "inclusive")
        claude = self.session("claude", "exclusive")
        self.session("muse", "unmeasured")
        incomplete = self.session("opencode", "incomplete")
        c_sem = "codex:input_includes_cached,output_includes_reasoning"
        a_sem = "claude:input_excludes_cache,output_includes_thinking"
        o_sem = "opencode:input_excludes_cache,reasoning_separate"
        self.response(codex, "c1", 11, "turn", c_sem, 100, 60)
        self.response(codex, "c2", 12, "turn", c_sem, 300, 0)
        self.response(codex, "other", 13, "turn", c_sem + ";source=token_count", 20, 20)
        self.response(codex, "duplicate", 14, "turn", c_sem, 10000, 10000, overlap=1)
        self.response(claude, "a1", 15, "turn", a_sem, 20, 60, 20)
        self.response(claude, "a2", 16, "turn", a_sem, 50, 0, 0)
        self.response(incomplete, "o1", 17, None, o_sem, 20, 0, 0)
        self.response(incomplete, "o2", 18, None, o_sem, 10, None, 0)

        result = self.run_diagnose()
        observed = {(item["harness"], item["semantics"]): item
                    for item in result["cache_observations"]}
        self.assertEqual(set(observed), {("codex", c_sem), ("codex", c_sem + ";source=token_count"),
                                         ("claude", a_sem), ("opencode", o_sem), ("muse", None)})
        native = observed["codex", c_sem]
        self.assertEqual(native["cached_input_share"], 0.15)
        self.assertEqual(native["cached_input_tokens"], 60)
        self.assertIsNone(native["cache_write_input_tokens"])
        self.assertEqual(observed["codex", c_sem + ";source=token_count"]["cached_input_share"], 1)
        native = observed["claude", a_sem]
        self.assertEqual(native["cached_input_share"], 0.4)
        self.assertEqual(native["cache_write_input_tokens"], 20)
        self.assertEqual(native["cached_input_tokens"], 60)
        self.assertIsNone(observed["opencode", o_sem]["cached_input_share"])
        self.assertIsNone(observed["opencode", o_sem]["cached_input_tokens"])
        self.assertEqual(observed["opencode", o_sem]["cache_write_input_tokens"], 0)
        self.assertIsNone(observed["muse", None]["cached_input_tokens"])
        self.assertEqual(self.run_diagnose("--harness", "claude")["cache_observations"], [native])

    def test_opencode_turn_needs_one_proved_parent(self):
        session = self.session("opencode", "parents")
        key, source = session
        sem = "opencode:input_excludes_cache,reasoning_separate"
        self.response(session, "owned", 11, None, sem, 10, 20, 30)
        self.response(session, "ambiguous", 12, None, sem, 10, 20, 30)
        native_response.prepare(self.con, source, replace=False)
        native_response.record(self.con, source, key, "owned", "prompt-1")
        native_response.record(self.con, source, key, "ambiguous", "prompt-1")
        native_response.record(self.con, source, key, "ambiguous", "prompt-2")
        timeline = self.run_diagnose()["context_growth"][0]["timeline"]
        self.assertEqual([point["input_tokens"] for point in timeline], [60, 60])
        self.assertEqual([point["turn_id"] for point in timeline], ["opencode:prompt-1", None])

    def test_legacy_codex_usage_uses_recorded_turn_bounds(self):
        codex.import_codex_file(self.con, str(FIXTURES / "codex-legacy-a.jsonl"))
        key = "codex:sess-fixture-legacy-01"
        result = self.run_diagnose("--session", key)
        timeline = result["context_growth"][0]["timeline"]
        self.assertEqual([(p["ordinal_num"], p["input_tokens"], p["turn_id"]) for p in timeline],
                         [(4, 400, "codex:turn-legacy-aaa"), (5, 600, "codex:turn-legacy-aaa")])
        self.con.execute(
            "INSERT INTO turns(turn_id, session_key, source_id, started_at, completed_at)"
            " VALUES('codex:overlapping-turn',?,?,?,?)",
            (key, timeline[0]["source_id"], timeline[0]["ts"], timeline[0]["ts"]))
        timeline = self.run_diagnose("--session", key)["context_growth"][0]["timeline"]
        self.assertEqual([p["turn_id"] for p in timeline], [None, "codex:turn-legacy-aaa"])

    def test_legacy_codex_usage_is_attributed_during_an_active_turn(self):
        codex.import_codex_file(self.con, str(FIXTURES / "codex-transition-a.jsonl"))
        result = self.run_diagnose("--session", "codex:sess-fixture-trans-01")
        timeline = result["context_growth"][0]["timeline"]
        self.assertEqual([(p["input_tokens"], p["turn_id"]) for p in timeline],
                         [(400, "codex:turn-trans-aaa"), (600, "codex:turn-trans-aaa")])

    def test_explicit_session_can_select_grok_subagent(self):
        grok.sync(self.con, root=str(FIXTURES / "grok"))
        key = "grok:01fixture2-bbbb-4b5c-8d6e-000000000002"
        self.assertNotIn(key, [item["session"] for item in self.run_diagnose()["context_growth"]])
        result = self.run_diagnose("--session", key)
        session, = result["context_growth"]
        self.assertEqual(session["session"], key)
        self.assertIn(f"{key}:p-sub1", [p.get("response_id") for p in session["timeline"]])

    def test_claude_streaming_sample_keeps_its_turn_and_source_together(self):
        session = "streaming-fixture"
        for index, reads in ((1, 20), (2, 80)):
            source = Path(self.tmp.name) / f"source-{index}.jsonl"
            records = [
                {"type": "user", "sessionId": session, "uuid": f"turn-{index}",
                 "message": {"role": "user", "content": "Fixture prompt"}},
                {"type": "assistant", "sessionId": session, "uuid": f"reply-{index}",
                 "timestamp": f"2026-10-01T12:00:0{index}Z", "message": {
                     "id": "streamed-response", "model": "fixture-model", "content": [],
                     "usage": {"input_tokens": 20, "output_tokens": 5,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": reads}}},
            ]
            source.write_text("\n".join(map(json.dumps, records)) + "\n")
            source_id = claude.import_claude_file(self.con, str(source))["source_id"]
        result = self.run_diagnose("--session", f"claude:{session}")
        point, = result["context_growth"][0]["timeline"]
        self.assertEqual((point["input_tokens"], point["source_id"], point["ordinal_num"], point["turn_id"]),
                         (100, source_id, 1, "claude:turn-2"))
