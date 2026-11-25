"""Projection fixtures use Chromeria #377's v1 JSON and relational identities."""
import json
import os
import sqlite3
from pathlib import Path

from agent_observer import report
from agent_observer.adapters import grok, t3
from tests.helpers import LedgerCase
from tests.test_t3_v2 import write_v2

PREFIX = "orchestration_v2_projection_"
TIME = "2026-11-25T01:00:00.000Z"
REF = dict(host="github.com", repository="Example/Project", kind="issue", number=7)
PURPOSE = dict(version=1, scope=dict(type="single", reference=REF),
               provenance=dict(type="selection", revision=2))


def strong(driver, native_id):
    return dict(driver=driver, strength="strong", nativeId=native_id)


class NativePurposeTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp.name, "chromeria-v2.sqlite")
        self.native = sqlite3.connect(self.path)
        self.addCleanup(self.native.close)
        tables = {
            "threads": "thread_id,created_at",
            "runs": "run_id,thread_id,status",
            "run_attempts": "attempt_id,run_id,thread_id,provider_thread_id,provider_turn_id,root_node_id",
            "provider_turns": "provider_turn_id,thread_id,provider_thread_id,run_attempt_id,node_id",
            "provider_threads": "provider_thread_id,thread_id,driver,provider,updated_at",
            "subagents": "subagent_id,thread_id,run_id,origin,driver,child_thread_id",
        }
        for name, columns in tables.items():
            self.native.execute(f"CREATE TABLE {PREFIX}{name} ({columns},payload_json)")
        self.row("threads", ["t", TIME], {
            "id": "t", "purposeDeliveries": [
                {"version": 1, "purpose": REF, "pullRequest": dict(REF, kind="pr", number=99),
                 "declaredAt": TIME}]})
        self.provider("codex")
        self.run_turn("a", "codex", PURPOSE, [{"version": 1, "kind": "turn",
                      "nativeId": "cx-A", "validation": "codex_turn_started"}])
        self.run_turn("b", "codex", dict(PURPOSE, scope=dict(type="unscoped", reason="mixed")), [])
        self.provider("grok")
        self.run_turn("g", "grok", dict(PURPOSE, scope=dict(type="single", reference=dict(
            REF, host="git.example.org"))), [], strong("grok", "prompt:3"))

    def provider(self, driver):
        pt = "pt-" + driver
        self.row("provider_threads", [pt, "t", driver, driver + "-custom", TIME], {
            "id": pt, "appThreadId": "t", "driver": driver, "providerInstanceId": driver + "-custom",
            "nativeThreadRef": strong(driver, "current-" + driver)})

    def run_turn(self, run, driver, purpose, inputs, turn_ref=None):
        pt, att, turn, root = "pt-" + driver, "attempt-" + run, "turn-" + run, "node-" + run
        instance = driver + "-custom"
        self.row("runs", [run, "t", "completed"], dict(
            id=run, threadId="t", providerThreadId=pt, providerInstanceId=instance,
            purpose=purpose, status="completed", output={"text": "private sentence"}))
        self.row("run_attempts", [att, run, "t", pt, turn, root], dict(
            id=att, runId=run, providerThreadId=pt, rootNodeId=root, providerInstanceId=instance,
            nativeThreadId="historic-" + driver, providerTurnId=turn, status="completed"))
        self.row("provider_turns", [turn, "t", pt, att, root], dict(
            id=turn, nodeId=root, providerThreadId=pt, runAttemptId=att, nativeInputs=inputs,
            nativeTurnRef=turn_ref or strong(driver, "display-only"),
            status="completed", turnTokenUsage=dict(usageScope="main_agent", usageStatus="complete",
            hasSubagents=False, inputTokens=22, outputTokens=4)))

    def row(self, table, columns, payload):
        values = [*columns, json.dumps(payload)]
        self.native.execute(f"INSERT INTO {PREFIX}{table} VALUES({','.join('?' for _ in values)})",
                            values)

    def refresh(self):
        self.native.commit()
        return t3.sync(self.con, source=self.path)

    def values(self, sql):
        return [tuple(r) for r in self.query(sql)]

    def test_literal_inputs_and_deliveries_survive_without_local_sessions(self):
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT run_id,session_key,native_id FROM t3_purpose_inputs"
                                    " ORDER BY run_id"), [
            ("a", "codex:historic-codex", "cx-A"),
            ("g", "grok:historic-grok", "prompt:3")])
        self.assertEqual(self.values("SELECT task_id,scope,reason FROM t3_purpose_runs WHERE run_id='b'"),
                         [(None, "unscoped", "mixed")])
        self.assertEqual(self.values("SELECT session_key FROM t3_purpose_sessions WHERE run_id='b'"),
                         [("codex:historic-codex",)])
        self.assertEqual(self.values("SELECT task_id FROM tasks ORDER BY task_id"), [
            ("example/project#7",), ("git.example.org/example/project#7",)])
        self.assertEqual(self.values("SELECT task_id,url FROM t3_purpose_deliveries"), [
            ("example/project#7", "https://github.com/example/project/pull/99")])
        self.assertNotIn("private sentence", "\n".join(self.con.iterdump()))

    def test_corrupt_joins_and_unvalidated_grok_stay_gaps(self):
        self.native.execute(f"UPDATE {PREFIX}provider_turns SET thread_id='other' WHERE run_attempt_id='attempt-a'")
        self.native.execute(f"""UPDATE {PREFIX}provider_turns SET payload_json=json_set(
            payload_json,'$.nativeTurnRef.strength','weak') WHERE run_attempt_id='attempt-g'""")
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT * FROM t3_purpose_inputs"), [])

    def test_grok_call_identifies_the_completion_and_reused_calls_fail_closed(self):
        self.native.execute(f"CREATE TABLE {PREFIX}turn_items (turn_item_id,thread_id,run_id,"
                            "provider_thread_id,provider_turn_id,type,payload_json)")
        cols = ["item", "t", "g", "pt-grok", "turn-g", "command_execution"]
        item = dict(id="item", threadId="t", runId="g", providerThreadId="pt-grok",
                    providerTurnId="turn-g", type="command_execution", nativeItemRef=strong("grok", "call"))
        self.row("turn_items", cols, item)
        sid = "historic-grok"
        root = Path(self.tmp.name, sid)
        root.mkdir()
        records = []
        def frame(update, **meta):
            records.append(json.dumps(dict(method="session/update",
                params=dict(sessionId=sid, update=update, _meta=meta))))
        def ingest():
            (root / "updates.jsonl").write_text("\n".join(records) + "\n")
            grok.import_grok_session(self.con, str(root), full=True)
        def membership():
            return report.task_report(self.con, "git.example.org/example/project#7")["native_purpose"]["membership"]

        frame(dict(sessionUpdate="turn_completed", prompt_id="done"))
        frame(dict(sessionUpdate="tool_call", toolCallId="call"), promptId="done")
        ingest()
        self.refresh()
        rid = "grok:historic-grok:done"
        self.assertEqual(membership()[rid]["validation"], "grok_tool_call")
        legacy = write_v2(os.path.join(self.tmp.name, "legacy.sqlite"))
        self.assertEqual(t3.sync(self.con, source=legacy)["failed"], [])
        self.assertEqual(membership()[rid]["validation"], "grok_tool_call")
        # Independent copies can contradict either the item's literal call
        # or its provider turn's literal prompt index.
        other_path = os.path.join(self.tmp.name, "other.sqlite")
        other = sqlite3.connect(other_path)
        self.addCleanup(other.close)
        self.native.backup(other)
        other.execute(f"""UPDATE {PREFIX}turn_items SET payload_json=json_set(
            payload_json,'$.nativeItemRef.nativeId','a-different-call')""")
        other.commit()
        self.assertEqual(t3.sync(self.con, source=other_path)["failed"], [])
        self.assertEqual(membership(), {})
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(membership(), {})
        other.execute(f"UPDATE {PREFIX}turn_items SET payload_json=?", (json.dumps(item),))
        other.execute(f"""UPDATE {PREFIX}provider_turns SET payload_json=json_set(
            payload_json,'$.nativeTurnRef.nativeId','prompt:4') WHERE provider_turn_id='turn-g'""")
        other.commit()
        self.assertEqual(t3.sync(self.con, source=other_path)["failed"], [])
        self.assertEqual(membership(), {})
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(membership(), {})
        other.execute(f"DELETE FROM {PREFIX}runs WHERE run_id='g'")
        other.commit()
        self.assertEqual(t3.sync(self.con, source=other_path)["failed"], [])
        self.assertEqual(membership()[rid]["validation"], "grok_tool_call")
        self.native.execute(f"UPDATE {PREFIX}turn_items SET provider_thread_id='wrong'")
        self.refresh()
        self.assertEqual(membership(), {})
        self.native.execute(f"UPDATE {PREFIX}turn_items SET provider_thread_id='pt-grok',"
                            "payload_json=json_set(payload_json,'$.nativeItemRef.strength','weak')")
        self.refresh()
        self.assertEqual(membership(), {})
        self.native.execute(f"UPDATE {PREFIX}turn_items SET payload_json=?", (json.dumps(item),))
        self.refresh()
        frame(dict(sessionUpdate="tool_call", toolCallId="call"), promptId="another")
        ingest()
        self.assertEqual(membership(), {})

    def test_invalid_scope_and_cross_driver_evidence(self):
        self.provider("claudeAgent")
        self.run_turn("c", "claudeAgent", dict(PURPOSE, version=True), [
            dict(version=1, kind="message", nativeId="echo-c", validation="claude_prompt_echo"),
            dict(version=True, kind="message", nativeId="bad", validation="claude_prompt_echo"),
            dict(version=1, kind="turn", nativeId="bad", validation="codex_turn_started")])
        self.provider("opencode")
        self.run_turn("o", "opencode", PURPOSE, [
            dict(version=1, kind="message", nativeId="msg-native", validation="opencode_admitted_message")])
        self.provider("opencode2")
        self.run_turn("oo", "opencode2", dict(PURPOSE, provenance=dict(type="parent_run")), [
            dict(version=1, kind="message", nativeId="forged", validation="opencode_admitted_message")])
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT run_id,task_id,scope FROM t3_purpose_runs WHERE scope='invalid'"
                                    " ORDER BY run_id"), [("c", None, "invalid"), ("oo", None, "invalid")])
        self.assertEqual(self.values("SELECT run_id,native_id FROM t3_purpose_inputs"
                                    " WHERE run_id IN ('c','o','oo') ORDER BY run_id"),
                         [("c", "echo-c"), ("o", "msg-native")])
        self.assertEqual(self.values("SELECT session_key FROM t3_purpose_sessions WHERE run_id='oo'"),
                         [("opencode:historic-opencode2",)])

    def test_segment_resume_and_background_never_expand_a_child_session(self):
        source_a = dict(type="app_run", threadId="t", runId="a")
        source_b = dict(type="app_run", threadId="t", runId="b")
        unscoped = dict(PURPOSE, scope=dict(type="unscoped", reason="mixed"))
        segment = dict(version=1, id="historic-codex:child:call", nativeSessionId="historic-codex",
                       nativeChildId="child", nativeOperationId="call", kind="start", purpose=PURPOSE,
                       source=source_a, boundary=dict(state="native_turns", nativeTurnIds=["first-turn"]),
                       status="ended", usageFinality="unreconciled", noticeIds=[], wakeDispositions=[],
                       observedAt=TIME)
        resume = dict(segment, id="segment-b", nativeOperationId="call-b", kind="resume",
                      source=source_b, purpose=unscoped, boundary=dict(state="unresolved",
                      reason="codex_resume_response_boundary_missing"))
        conflict = dict(segment, id="wrong", source=source_b,
                        boundary=dict(state="native_turns", nativeTurnIds=["stolen"]))
        self.row("subagents", ["agent-1", "t", "a", "provider_native", "codex", "child-thread"], {
            "id": "agent-1", "threadId": "t", "runId": "a", "driver": "codex",
            "origin": "provider_native", "childThreadId": "child-thread",
            "providerThreadId": "child-pt", "nativeSegments": [resume, segment, conflict],
            "prompt": "keep privately", "result": "do not store"})
        self.row("provider_threads", ["child-pt", "child-thread", "codex", "codex-custom", TIME],
                 dict(id="child-pt", appThreadId="child-thread", driver="codex", providerInstanceId="codex-custom",
                      nativeThreadRef=strong("codex", "child")))
        for turn in ("first-turn", "later-turn"):
            self.row("provider_turns", [turn, "child-thread", "child-pt", None, "child-node"], {
                "id": turn, "nodeId": "child-node", "providerThreadId": "child-pt", "runAttemptId": None,
                "nativeInputs": [dict(version=1, kind="turn", nativeId=turn, validation="codex_turn_started")],
                "turnTokenUsage": dict(usageScope="main_agent", usageStatus="partial",
                                      hasSubagents=False, inputTokens=6)})
        background = dict(segment, id="bg", kind="background", nativeChildId="shell",
                          boundary=dict(state="native_turns", nativeTurnIds=["parent-turn"]))
        self.native.execute(f"""UPDATE {PREFIX}provider_threads SET payload_json=json_set(
            payload_json,'$.nativeOperations',json(?)) WHERE provider_thread_id='pt-codex'""",
                            (json.dumps([background]),))
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT run_id,segment_id,usage_finality FROM t3_purpose_segments"
                                    " ORDER BY segment_id"),
                         [("a", "bg", "unreconciled"), ("a", segment["id"], "unreconciled"),
                          ("b", "segment-b", "unreconciled"), (None, "wrong", "unreconciled")])
        self.assertEqual(self.values("SELECT run_id,session_key,native_id FROM t3_purpose_inputs"
                                    " WHERE segment_id IS NOT NULL"), [("a", "codex:child", "first-turn")])
        self.assertEqual(self.values("SELECT run_id,provider_turn_id FROM t3_purpose_child_turns"),
                         [("a", "first-turn")])
        self.assertEqual(self.values("SELECT run_id FROM t3_purpose_sessions WHERE session_key='codex:child'"
                                    " ORDER BY run_id"), [("a",), ("b",)])
        self.assertNotIn("keep privately", "\n".join(self.con.iterdump()))

    def test_expected_child_and_turn_counter_have_independent_sources(self):
        self.native.execute(f"CREATE TABLE {PREFIX}turn_items (turn_item_id,thread_id,run_id,type,payload_json)")
        self.row("turn_items", ["item", "t", "a", "subagent"], dict(id="item", threadId="t", runId="a",
                 type="subagent", subagentId="job", origin="app_owned", childThreadId=None,
                 remoteChild=dict(environmentId="other", threadId="remote-thread", runId="remote-run",
                                  label="private sentence", availability="stale")))
        self.native.execute(f"CREATE TABLE {PREFIX}provider_sessions (provider_session_id,payload_json)")
        self.row("provider_sessions", ["ps"], dict(id="ps", driver="codex", capabilities=dict(
            purposeEvidence=dict(version=1, nativeInputs="native_turn", childSegments="initial_turn_only",
                                 backgroundInventory="observed_causes", nativeUsageFinality="main_agent_only"))))
        self.native.execute(f"""UPDATE {PREFIX}runs SET payload_json=json_set(
            payload_json,'$.delegatedCompletion',json(?)) WHERE run_id='a'""",
            (json.dumps(dict(disposition="open", nextGeneration=2, delivery=dict(
                generation=1, messageId="m", taskIds=["job", "missing"], purposeCauses=[]))),))
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT kind,participant_id,environment_id,child_run_id"
                                    " FROM t3_purpose_participants ORDER BY kind,participant_id"),
                         [("completion_member", "job", None, None), ("completion_member", "missing", None, None),
                          ("subagent_item", "job", "other", "remote-run")])
        self.assertEqual(self.values("SELECT background_inventory,native_usage_finality"
                                    " FROM t3_purpose_capabilities"), [("observed_causes", "main_agent_only")])
        usage = self.values("SELECT usage_json FROM t3_purpose_turns WHERE run_id='a'")[0][0]
        self.assertEqual(json.loads(usage), dict(usageScope="main_agent", usageStatus="complete",
                         inputTokens=22, outputTokens=4, hasSubagents=False))
        self.native.execute(f"DELETE FROM {PREFIX}provider_turns WHERE run_attempt_id='attempt-a'")
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT usage_json FROM t3_purpose_turns WHERE run_id='a'"), [])
        self.assertEqual(self.values("SELECT provider_turn_id FROM t3_purpose_attempts WHERE run_id='a'"),
                         [("turn-a",)])

    def test_duplicate_snapshots_conflict_and_recover_independently(self):
        other_path = os.path.join(self.tmp.name, "other.sqlite")
        other = sqlite3.connect(other_path)
        self.addCleanup(other.close)
        self.native.commit()
        self.native.backup(other)
        self.sync("codex-mini.jsonl")
        for source in (self.native, other):
            source.execute(f"""UPDATE {PREFIX}provider_turns SET payload_json=json_set(
                payload_json,'$.nativeInputs[0].nativeId','turn-mini-aaa') WHERE run_attempt_id='attempt-a'""")
            source.execute(f"""UPDATE {PREFIX}run_attempts SET payload_json=json_set(
                payload_json,'$.nativeThreadId','sess-fixture-mini-01') WHERE run_id='a'""")
            source.commit()

        def refresh_other():
            other.commit()
            self.assertEqual(t3.sync(self.con, source=other_path)["failed"], [])

        def credited(n):
            return report.task_report(self.con, f"example/project#{n}")["measured"]["attributed_responses"]

        refresh_other()
        self.assertEqual(self.refresh()["failed"], [])
        expected = ["codex:resp-mini-001", "codex:resp-mini-002"]
        self.assertEqual(credited(7), expected)
        self.assertEqual(self.values("SELECT count(*) FROM t3_purpose_inputs WHERE run_id='a'"), [(1,)])
        other.execute(f"UPDATE {PREFIX}runs SET status='running' WHERE run_id='a'")
        refresh_other()
        self.assertEqual(self.values("SELECT status FROM t3_purpose_runs WHERE run_id='a'"), [(None,)])
        self.assertEqual(credited(7), expected)
        self.con.execute("INSERT INTO session_assignments VALUES"
                         "('codex:historic','example/project#7','t3:old:https://github.com/example/project/issues/7',1)")
        other.execute(f"""UPDATE {PREFIX}runs SET payload_json=json_set(
            payload_json,'$.purpose.scope.reference.number',8) WHERE run_id='a'""")
        refresh_other()
        self.assertEqual(self.values("SELECT task_id FROM session_assignments WHERE session_key='codex:historic'"), [])
        self.assertEqual((credited(7), credited(8)), ([], []))
        for n in (7, 8):
            native = report.task_report(self.con, f"example/project#{n}")["native_purpose"]
            self.assertEqual(native["conflicting_source_runs"], ["a"])
            self.assertIn("conflicting_native_run_records", native["coverage_gaps"])
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual((credited(7), credited(8)), ([], []))

        other.execute(f"DELETE FROM {PREFIX}runs WHERE run_id='a'")
        refresh_other()
        self.assertEqual((credited(7), credited(8)), (expected, []))
        self.native.execute(f"UPDATE {PREFIX}runs SET status='interrupted' WHERE run_id='a'")
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT status FROM t3_purpose_runs WHERE run_id='a'"),
                         [("interrupted",)])
        self.native.execute(f"DELETE FROM {PREFIX}runs")
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT run_id FROM t3_purpose_runs ORDER BY run_id"), [("b",), ("g",)])

    def test_pre_source_mirror_survives_first_refresh_of_a_different_path(self):
        self.assertEqual(self.refresh()["failed"], [])
        # A ledger from the earlier schema has the materialized projection and
        # its import receipt, but no per-source tables.
        for name, in self.values("SELECT name FROM sqlite_master WHERE type='table'"
                                 " AND name GLOB 't3_source_*'"):
            self.con.execute(f"DROP TABLE {name}")
        legacy = write_v2(os.path.join(self.tmp.name, "legacy.sqlite"))
        self.assertEqual(t3.sync(self.con, source=legacy)["failed"], [])
        self.assertEqual(self.values("SELECT native_id FROM t3_purpose_inputs ORDER BY native_id"),
                         [("cx-A",), ("prompt:3",)])
        self.native.execute(f"DELETE FROM {PREFIX}runs WHERE run_id='a'")
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT native_id FROM t3_purpose_inputs"), [("prompt:3",)])

    def test_failed_refresh_preserves_previous_projection_and_outer_transaction(self):
        self.assertEqual(self.refresh()["failed"], [])
        self.con.execute("CREATE TRIGGER abort_input BEFORE INSERT ON t3_purpose_inputs"
                         " BEGIN SELECT RAISE(ABORT, 'fixture interrupted'); END")
        self.native.execute(f"DELETE FROM {PREFIX}runs WHERE run_id='g'")
        self.assertTrue(self.refresh()["failed"])
        self.assertEqual(self.values("SELECT native_id FROM t3_purpose_inputs ORDER BY native_id"),
                         [("cx-A",), ("prompt:3",)])
        self.con.execute("DROP TRIGGER abort_input")
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT native_id FROM t3_purpose_inputs"), [("cx-A",)])
        self.assertTrue(self.con.in_transaction)

    def test_explicitly_unscoped_run_retires_legacy_tree_but_keeps_other_bindings(self):
        from agent_observer import db
        from tests.test_t3_v2 import CHILD, CX9
        self.path = write_v2(os.path.join(self.tmp.name, "formerly-legacy.sqlite"))
        self.native.close()
        self.native = sqlite3.connect(self.path)
        self.addCleanup(self.native.close)
        self.con.execute("INSERT INTO sources(harness,path,sha256,imported_at) VALUES('codex','fixture','x',1)")
        source = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        session = "codex:" + CX9
        db.upsert_session(self.con, session, "codex", CX9, source)
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT task_id FROM session_assignments"
                                    " WHERE task_id='example/beta#5'"),
                         [("example/beta#5",)])
        self.con.execute("INSERT INTO tasks(task_id,origin,created_at) VALUES('separate','capture',1)")
        self.con.execute("INSERT INTO session_assignments VALUES(?, 'separate', 'manual', 1)", (session,))
        self.native.execute(f"""UPDATE {PREFIX}runs SET payload_json=json_set(payload_json,
            '$.threadId',?,'$.purpose',json(?)) WHERE thread_id=?""",
            (CHILD, json.dumps(dict(version=1, scope=dict(type="unscoped", reason="deliberate"),
                                    provenance=dict(type="declared"))), CHILD))
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT task_id FROM session_assignments"), [("separate",)])
        self.assertEqual(self.values("SELECT DISTINCT task_id,reason FROM t3_purpose_runs"),
                         [(None, "deliberate")])

    def test_selection_in_another_source_keeps_legacy_tree_unbound_until_removed(self):
        from agent_observer import db
        from tests.test_t3_v2 import CHILD, CX9
        legacy = write_v2(os.path.join(self.tmp.name, "legacy.sqlite"))
        self.con.execute("INSERT INTO sources(harness,path,sha256,imported_at) VALUES('codex','fixture','x',1)")
        source = self.con.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.upsert_session(self.con, "codex:" + CX9, "codex", CX9, source)
        self.row("threads", [CHILD, TIME], dict(id=CHILD, purposeSelection={"version": 1}))

        def bindings():
            return self.values("SELECT task_id FROM session_assignments WHERE task_id='example/beta#5'")

        self.assertEqual(t3.sync(self.con, source=legacy)["failed"], [])
        self.assertEqual(bindings(), [("example/beta#5",)])
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(bindings(), [])
        self.assertEqual(t3.sync(self.con, source=legacy)["failed"], [])
        self.assertEqual(bindings(), [])
        self.native.execute(f"DELETE FROM {PREFIX}threads WHERE thread_id=?", (CHILD,))
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(t3.sync(self.con, source=legacy)["failed"], [])
        self.assertEqual(bindings(), [("example/beta#5",)])

    def test_navigation_migration_and_same_issue_numbers_on_two_hosts(self):
        self.con.execute("CREATE TABLE t3_links(thread_id,kind,host,repository,number,url,source,"
                         "state,linked_at,head_branch,PRIMARY KEY(thread_id,kind,repository,number))")
        self.con.execute("INSERT INTO t3_links VALUES('retired','pr','github.com','fixture/repo',2,"
                         "'https://github.com/fixture/repo/pull/2','agent','merged',NULL,'original-head')")
        self.native.execute("CREATE TABLE fork_thread_issue_links(thread_id,host,repository,number,"
                            "url,source,linked_at)")
        for host in ("github.com", "git.example.org"):
            self.native.execute("INSERT INTO fork_thread_issue_links VALUES('t',?,'example/project',7,"
                                "?,'agent',?)", (host, f"https://{host}/example/project/issues/7", TIME))
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual(self.values("SELECT host FROM t3_links WHERE thread_id='t' ORDER BY host"),
                         [("git.example.org",), ("github.com",)])
        self.assertEqual(self.values("SELECT head_branch FROM t3_links WHERE thread_id='retired'"),
                         [("original-head",)])

    def test_existing_task_key_keeps_original_repository_capitalization(self):
        self.con.execute("INSERT INTO tasks(task_id,origin,created_at) VALUES('Example/Project#7','capture',1)")
        result = self.refresh()
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["tasks"], 1)
        self.assertEqual(self.values("SELECT task_id FROM t3_purpose_runs WHERE run_id='a'"),
                         [("Example/Project#7",)])
        self.assertEqual(self.values("SELECT origin FROM tasks WHERE task_id='Example/Project#7'"), [("capture",)])

    def test_provenance_keeps_exact_typed_cause_without_copying_free_text(self):
        variants = [
            dict(type="parent_run", threadId="t", runId="p"),
            dict(type="target_run", threadId="t", runId="p"),
            dict(type="source_run", threadId="t", runId="p"),
            dict(type="delegated_launch", threadId="t", runId="p", taskId="original-launch"),
            dict(type="remote_launch", environmentId="env", threadId="source", runId="p", launchId="launch"),
            dict(type="native_operation", operationIds=["op-1", "op-2"]),
            dict(type="delegated_completion", operationIds=["done"]), dict(type="schedule", scheduledTaskId="job"),
            dict(type="pull_request_watch", reference=dict(REF, repository="example/project"), startedAt=TIME)]
        for i, variant in enumerate(variants):
            key = f"p-{i}"
            self.row("runs", [key, "t", "interrupted"], dict(id=key, threadId="t",
                     purpose=dict(PURPOSE, provenance=dict(variant, note="do not copy me"))))
        self.assertEqual(self.refresh()["failed"], [])
        self.assertEqual([json.loads(r[0]) for r in self.values("SELECT provenance_json FROM t3_purpose_runs"
                                                                " WHERE run_id LIKE 'p-%' ORDER BY run_id")], variants)
        self.assertNotIn("do not copy me", "\n".join(self.con.iterdump()))
