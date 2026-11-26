"""Counterexamples for context and cache observations at the CLI boundary."""

import contextlib
import io
import json
import shutil
import sqlite3
from pathlib import Path

from agent_observer import cli, db, native_response
from agent_observer.adapters import claude, codex, opencode
from tests.helpers import LedgerCase, fixture
from tests.test_opencode import T0, _msg, _tokens, build_native


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

    def test_codex_dispute_does_not_supply_input_or_cache_counters(self):
        lines = Path(fixture("codex-mini.jsonl")).read_text().splitlines()
        atom = next(json.loads(line) for line in lines if
                    '"resp-mini-001"' in line and '"token_usage_record"' in line)
        self.sync("codex-mini.jsonl")
        atom["payload"]["usage"]["input_tokens"] += 7
        copy = Path(self.tmp.name) / "rollout-copy.jsonl"
        missing = json.loads(json.dumps(atom))
        missing["payload"]["response_id"] = "missing-usage"
        missing["payload"]["usage"] = {"input_tokens": -1}
        copy.write_text("\n".join((lines[0], json.dumps(atom), json.dumps(missing))) + "\n")
        codex.import_codex_file(self.con, str(copy))
        result = self.run_diagnose("--harness", "codex")
        timeline = result["context_growth"][0]["timeline"]
        disputed = next(p for p in timeline if p.get("response_id") == "codex:resp-mini-001")
        self.assertIsNone(disputed["input_tokens"])
        gap = next((p for p in timeline if p.get("response_id") == "codex:missing-usage"), None)
        self.assertIsNotNone(gap)
        self.assertIsNone(gap["input_tokens"])
        self.assertTrue(any(p.get("input_tokens") for p in timeline))
        native = next(group for group in result["cache_observations"] if
                      group["semantics"] == codex.SEMANTICS)
        self.assertIsNone(native["cached_input_share"])
        self.assertIsNone(native["cached_input_tokens"])

    def test_codex_unindexed_atom_keeps_same_source_compaction_order(self):
        lines = [json.loads(line) for line in Path(fixture("codex-mini.jsonl")).read_text().splitlines()]
        valid = next(row for row in lines if row["type"] == "token_usage_record")
        boundary = next(row for row in lines if row["type"] == "compacted")
        boundary.pop("timestamp")
        malformed = json.loads(json.dumps(valid))
        malformed["payload"]["response_id"] = "unindexed"
        malformed["payload"]["usage"] = {"input_tokens": -1}
        log = Path(self.tmp.name) / "rollout.jsonl"
        log.write_text("\n".join(map(json.dumps, (lines[0], valid, boundary, malformed))) + "\n")
        codex.import_codex_file(self.con, str(log))
        session, = self.run_diagnose("--harness", "codex")["context_growth"]
        self.assertEqual([p["kind"] for p in session["timeline"]], ["input", "compaction", "input"])
        self.assertEqual(session["timeline"][-1]["response_id"], "codex:unindexed")
        self.assertEqual(session["order"], "native")

        copied = Path(self.tmp.name) / "rollout-boundary-only.jsonl"
        copied.write_text("\n".join(map(json.dumps, (lines[0], boundary))) + "\n")
        codex.import_codex_file(self.con, str(copied))
        session, = self.run_diagnose("--harness", "codex")["context_growth"]
        self.assertEqual(session["order"], "timestamp")

    def test_claude_disputed_cache_reads_stay_unknown_in_both_import_orders(self):
        for index, reads in enumerate(((20, 80), (80, 20))):
            with self.subTest(reads=reads):
                session = f"claude-cache-{index}"
                for copy, read in enumerate(reads):
                    record = {"type": "assistant", "sessionId": session,
                              "uuid": f"{session}-assistant", "message": {
                                  "id": f"{session}-response", "model": "synthetic-model",
                                  "stop_reason": "end_turn",
                                  "usage": {"input_tokens": 20, "output_tokens": 5,
                                            "cache_creation_input_tokens": 0,
                                            "cache_read_input_tokens": read},
                                  "content": [{"type": "text", "text": "Fixture"}]}}
                    path = Path(self.tmp.name) / f"{session}-{copy}.jsonl"
                    path.write_text(json.dumps(record) + "\n")
                    claude.import_claude_file(self.con, str(path))
                result = self.run_diagnose("--session", f"claude:{session}")
                point, = result["context_growth"][0]["timeline"]
                self.assertIsNone(point["input_tokens"])
                cache, = result["cache_observations"]
                self.assertIsNone(cache["cached_input_tokens"])
                self.assertIsNone(cache["cached_input_share"])

    def test_claude_final_without_usage_has_a_sourced_unknown_sample(self):
        session = "claude-missing-final"
        records = []
        for index in (1, 2):
            records.append({"type": "assistant", "sessionId": session, "uuid": f"block-{index}",
                            "timestamp": f"2026-10-01T12:00:0{index}Z", "message": {
                                "id": f"final-{index}", "model": "synthetic-model",
                                "stop_reason": "end_turn", "content": []}})
        records[0]["message"]["usage"] = {"input_tokens": 20, "output_tokens": 5,
                                           "cache_creation_input_tokens": 0,
                                           "cache_read_input_tokens": 80}
        log = Path(self.tmp.name) / f"{session}.jsonl"
        log.write_text("\n".join(map(json.dumps, records)) + "\n")
        claude.import_claude_file(self.con, str(log))
        result = self.run_diagnose("--session", f"claude:{session}")
        timeline = result["context_growth"][0]["timeline"]
        self.assertEqual([p["response_id"] for p in timeline], ["claude:final-1", "claude:final-2"])
        self.assertEqual([p["input_tokens"] for p in timeline], [100, None])
        self.assertIsNotNone(timeline[1]["source_id"])
        self.assertIsNone(timeline[1]["ordinal_num"])
        cache, = result["cache_observations"]
        self.assertEqual(cache["responses"], 2)
        self.assertIsNone(cache["cached_input_share"])

    def test_opencode_import_surfaces_completed_response_without_counters(self):
        path = str(Path(self.tmp.name) / "opencode.db")
        build_native(path)
        with contextlib.closing(sqlite3.connect(path)) as native:
            native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                           ("cache_gap", None, "/fixture", "Gap", "1", None,
                            "build", T0, T0 + 1000, None))
            prompt = _msg("user", T0 + 10)
            measured = _msg("assistant", T0 + 20, T0 + 30, _tokens(0, 1, 0, 40, 0))
            missing = json.loads(_msg("assistant", T0 + 50, T0 + 60))
            missing["parentID"] = "cache_user"
            native.executemany("INSERT INTO message VALUES(?,'cache_gap',?,?,?)", [
                ("cache_user", T0 + 10, T0 + 10, prompt),
                ("cache_measured", T0 + 20, T0 + 30, measured),
                ("cache_missing", T0 + 50, T0 + 60, json.dumps(missing)),
            ])
            native.commit()
        opencode.sync(self.con, source=path)
        result = self.run_diagnose("--session", "opencode:cache_gap")
        timeline = result["context_growth"][0]["timeline"]
        self.assertEqual([p["response_id"] for p in timeline],
                         ["opencode:cache_measured", "opencode:cache_missing"])
        self.assertEqual([p["input_tokens"] for p in timeline], [40, None])
        self.assertEqual(timeline[1]["turn_id"], "opencode:cache_user")
        cache, = result["cache_observations"]
        self.assertEqual(cache["responses"], 2)
        self.assertIsNone(cache["cached_input_share"])
        self.assertIsNone(cache["cached_input_tokens"])

        copied = str(Path(self.tmp.name) / "opencode-copy.db")
        shutil.copyfile(path, copied)
        with contextlib.closing(sqlite3.connect(copied)) as native:
            missing_copy = _msg("assistant", T0 + 20, T0 + 30)
            native.execute("UPDATE message SET data=? WHERE id='cache_measured'", (missing_copy,))
            native.commit()
        opencode.sync(self.con, source=copied)
        result = self.run_diagnose("--session", "opencode:cache_gap",
                                   "--until", str((T0 + 40) / 1000))
        timeline = result["context_growth"][0]["timeline"]
        self.assertEqual([p["response_id"] for p in timeline], ["opencode:cache_measured"])
        self.assertIsNone(timeline[0]["input_tokens"])
        cache, = result["cache_observations"]
        self.assertEqual(cache["responses"], 1)
        self.assertIsNone(cache["cached_input_share"])

        with contextlib.closing(sqlite3.connect(copied)) as native:
            native.execute("UPDATE message SET data='{' WHERE id='cache_measured'")
            native.commit()
        opencode.sync(self.con, source=copied, full=True)
        result = self.run_diagnose("--session", "opencode:cache_gap",
                                   "--since", str((T0 + 25) / 1000),
                                   "--until", str((T0 + 40) / 1000))
        timeline = result["context_growth"][0]["timeline"]
        self.assertEqual([p["response_id"] for p in timeline], ["opencode:cache_measured"])
        self.assertIsNone(timeline[0]["input_tokens"])
        self.assertEqual(timeline[0]["ts"], (T0 + 30) / 1000)
        self.assertNotEqual(timeline[0]["source_id"], timeline[0]["usage_gap"]["source_id"])
        cache, = result["cache_observations"]
        self.assertEqual(cache["responses"], 1)
        self.assertIsNone(cache["cached_input_share"])

    def test_opencode_role_conflict_qualifies_usage_in_both_import_orders(self):
        for index, roles in enumerate((("assistant", "user"), ("user", "assistant"))):
            with self.subTest(roles=roles):
                session = f"role-conflict-{index}"
                paths = {}
                for role in roles:
                    path = str(Path(self.tmp.name) / f"{session}-{role}.db")
                    paths[role] = path
                    build_native(path)
                    with contextlib.closing(sqlite3.connect(path)) as native:
                        native.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                                       (session, None, "/fixture", "Role", "1", None,
                                        "build", T0, T0 + 1000, None))
                        data = _msg(role, T0 + 20, T0 + 30, _tokens(0, 1, 0, 40, 0))
                        native.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                                       (session, session, T0 + 20, T0 + 30, data))
                        native.commit()
                    opencode.sync(self.con, source=path)
                result = self.run_diagnose("--session", f"opencode:{session}")
                point, = result["context_growth"][0]["timeline"]
                self.assertIsNone(point["input_tokens"])
                gap_source = self.con.execute("SELECT id FROM sources WHERE path=?",
                                              (f"{paths['user']}#{session}",)).fetchone()[0]
                self.assertEqual(point["usage_gap"]["source_id"], gap_source)
                cache, = result["cache_observations"]
                self.assertEqual(cache["responses"], 1)
                self.assertIsNone(cache["cached_input_tokens"])
                self.assertIsNone(cache["cached_input_share"])
