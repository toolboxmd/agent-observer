"""Conversation coverage and attribution by native identity (#85)."""

import json
import os
import shutil
import sqlite3

from agent_observer import analysis, db, interventions, lookup, report, search, verdicts
from agent_observer.adapters import claude, codex, grok, opencode, t3
from agent_observer.ingest import text_hash
from tests.helpers import LedgerCase
from tests.test_codex_fork import _import_at_revision
from tests.test_opencode import build_native
from tests.test_t3_v2 import CL2, P1, P3, P4, P5, V2_TABLES, write_v2


class NativeEventsTest(LedgerCase):
    def jsonl(self, path, records):
        with open(path, "w") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
        return path

    def codex_overlapping_ordinals(self):
        return [
            {"type": "session_meta", "ordinal": 0, "payload": {"id": "native", "source": "cli"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "first input"}],
             "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["user.text"]}}},
            {"type": "response_item", "ordinal": 1, "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "second input"}],
             "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["user.text"]}}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "first answer"}]}},
            {"type": "response_item", "ordinal": 3, "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "second answer"}]}},
            {"type": "event_msg", "payload": {"type": "turn_aborted", "duration_ms": 7}},
            {"type": "event_msg", "ordinal": 5, "payload": {"type": "turn_aborted", "duration_ms": 11}},
        ]

    def grok_session(self):
        path = os.path.join(self.tmp.name, "grok-session")
        os.mkdir(path)
        with open(os.path.join(path, "summary.json"), "w") as fh:
            json.dump({"info": {"id": "grok-session", "cwd": "/repo"}}, fh)
        def update(kind, event, **fields):
            return {"method": "session/update", "timestamp": "2026-10-14T10:00:00Z",
                    "params": {"sessionId": "grok-session",
                               "_meta": {"eventId": event, "promptId": "p1"},
                               "update": {"sessionUpdate": kind, **fields}}}
        self.jsonl(os.path.join(path, "updates.jsonl"), [
            update("user_message_chunk", "e1", _meta={"promptIndex": 0},
                   content={"type": "text", "text": "do the work"}),
            update("agent_message_chunk", "e2",
                   content={"type": "text", "text": "Native searchable answer"}),
            update("agent_message_chunk", "e3",
                   content={"type": "text", "text": "Repeated distinct answer"}),
            update("tool_call", "e4", toolCallId="c1",
                   _meta={"x.ai/tool": {"name": "run"}}, rawInput={"command": "true"}),
            update("tool_call_update", "e5", toolCallId="c1", status="cancelled"),
        ])
        self.jsonl(os.path.join(path, "events.jsonl"), [
            {"type": "turn_ended", "outcome": "interrupted", "ts": "2026-10-14T10:01:00Z"},
            {"type": "turn_ended", "outcome": "cancelled", "ts": "2026-10-14T10:02:00Z"},
        ])
        return path

    def test_grok_chunks_stops_and_unknown_prompt_survive_resync(self):
        path = self.grok_session()
        grok.sync(self.con, source=path)
        rows = self.query("SELECT * FROM events WHERE family='assistant_message'")
        self.assertEqual({r["native_id"] for r in rows}, {"e2", "e3"})
        self.assertEqual({r["turn_id"] for r in rows}, {"grok:grok-session:p1"})
        self.assertEqual(search.search(self.con, ["searchable"])["total"], 1)
        self.assertEqual(lookup.show(self.con, [rows[0]["id"]])[0]["text"],
                         "Native searchable answer")
        stops = self.query("SELECT status FROM events WHERE name='turn_ended'")
        self.assertEqual({r[0] for r in stops}, {"interrupted", "cancelled"})
        self.assertEqual(self.query("SELECT status FROM events WHERE family='tool_result'")[0][0],
                         "cancelled")
        result = interventions.select(self.con, created_by=None)
        self.assertEqual(result["counts"]["by_created_by"]["unknown"], 3)
        before = [tuple(r) for r in self.query("SELECT id,native_id FROM events ORDER BY id")]
        grok.sync(self.con, source=path, full=True)
        self.assertEqual([tuple(r) for r in self.query("SELECT id,native_id FROM events ORDER BY id")], before)

    def test_opencode_text_parts_update_and_abort_remains_visible(self):
        path = os.path.join(self.tmp.name, "opencode.db")
        build_native(path)
        native = sqlite3.connect(path)
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)", (
            "native-answer", "msg_a1", "ses_parent", 1788000000500, 1788000000500,
            json.dumps({"type": "text", "text": "Native searchable answer"})))
        native.commit()
        opencode.sync(self.con, source=path)
        rows = self.query("SELECT * FROM events WHERE native_id='native-answer'")
        self.assertEqual(len(rows), 1)
        event_id = rows[0]["id"]
        self.assertEqual(rows[0]["family"], "assistant_message")
        self.assertGreater(search.search(self.con, ["searchable"])["total"], 0)
        native.execute("UPDATE part SET time_updated=time_updated+1, data=? WHERE id='native-answer'",
                       (json.dumps({"type": "text", "text": "Final searchable answer"}),))
        native.execute("UPDATE message SET time_updated=time_updated+1, data=? WHERE id='msg_a1'",
                       (json.dumps({"role": "assistant", "parentID": "user1",
                                    "time": {"created": 1788000000500, "completed": 1788000001000},
                                    "error": {"name": "MessageAbortedError", "data": {"message": "private"}}}),))
        native.commit()
        opencode.sync(self.con, source=path)
        native.close()
        row = self.query("SELECT * FROM events WHERE native_id='native-answer'")[0]
        self.assertEqual(row["id"], event_id)
        self.assertIn("Final searchable answer", row["detail_json"])
        stop = self.query("SELECT * FROM events WHERE family='lifecycle' AND native_id='msg_a1'")[0]
        self.assertEqual(stop["status"], "interrupted")
        self.assertNotIn("private", "\n".join(self.con.iterdump()))

    def test_codex_without_item_ids_keeps_distinct_native_ordinals(self):
        path = os.path.join(self.tmp.name, "rollout-native.jsonl")
        def record(kind, payload):
            return {"type": kind, "timestamp": "2026-10-14T10:00:00Z", "payload": payload}
        self.jsonl(path, [record("session_meta", {"id": "native", "source": "cli"}),
                         record("response_item", {"type": "message", "role": "user",
                                                   "content": [{"type": "input_text", "text": "prompt one"}]}),
                         record("response_item", {"type": "message", "role": "user",
                                                   "content": [{"type": "input_text", "text": "prompt two"}]}),
                         record("response_item", {"type": "message", "role": "assistant",
                                                   "content": [{"type": "output_text", "text": "one"}]}),
                         record("response_item", {"type": "message", "role": "assistant",
                                                   "content": [{"type": "output_text", "text": "two"}]}),
                         record("event_msg", {"type": "turn_aborted"}),
                         record("event_msg", {"type": "turn_aborted"})])
        codex.sync(self.con, source=path)
        self.assertEqual(len(self.query("SELECT * FROM events WHERE family='assistant_message'")), 2)
        self.assertEqual(len(self.query("SELECT * FROM events WHERE name='turn_aborted'")), 2)
        # A ledger from the previous adapter retained only the first record
        # under each None placeholder. Upgrade must repair that row in place.
        first = self.query("SELECT id FROM events WHERE family='assistant_message' ORDER BY id")[0][0]
        self.con.execute("DELETE FROM events WHERE family='assistant_message' AND id!=?", (first,))
        self.con.execute("UPDATE events SET native_id='ordinal:None',ordinal_num=NULL WHERE id=?", (first,))
        user = self.query("SELECT native_id FROM user_messages ORDER BY ordinal_num")[0][0]
        self.con.execute("DELETE FROM user_messages WHERE native_id!=?", (user,))
        self.con.execute("UPDATE user_messages SET native_id='codex:ordinal:None',ordinal_num=NULL")
        self.con.execute("UPDATE sources SET privacy_version=8")
        codex.sync(self.con, source=path)
        self.assertEqual(self.query("SELECT id FROM events WHERE native_id='file:rollout-native.jsonl:ordinal:3'")[0][0], first)
        # Both user records derive the collapsed id, so neither may take it:
        # the stored row stays and the gap is recorded (#108).
        self.assertEqual({r[0] for r in self.query("SELECT native_id FROM user_messages")},
                         {"codex:ordinal:None", "codex:native:file:rollout-native.jsonl:ordinal:1",
                          "codex:native:file:rollout-native.jsonl:ordinal:2"})
        self.assertEqual([tuple(r) for r in self.query("SELECT error, ordinal_num FROM import_errors")],
                         [("identity_conflict", 1)])
        second = os.path.join(self.tmp.name, "rollout-second.jsonl")
        self.jsonl(second, [record("session_meta", {"id": "second", "source": "cli"}),
                           record("response_item", {"type": "message", "role": "user",
                                                     "content": [{"type": "input_text", "text": "prompt one"}]})])
        codex.sync(self.con, source=second)
        self.assertEqual(len(self.query("SELECT * FROM user_messages")), 4)

    def test_claude_message_blocks_and_interrupt_have_stable_identity(self):
        path = os.path.join(self.tmp.name, "claude.jsonl")
        self.jsonl(path, [
            {"sessionId": "native", "type": "user", "uuid": "u1", "promptSource": "sdk",
             "message": {"content": "Unmatched SDK prompt"}},
            {"sessionId": "native", "type": "assistant", "uuid": "a1", "message": {
                "id": "m1", "content": [{"type": "text", "text": "answer"},
                                          {"type": "text", "text": "answer"}]}},
            {"sessionId": "native", "type": "user", "uuid": "u2",
             "message": {"content": "[Request interrupted by user]"}},
        ])
        claude.sync(self.con, source=path)
        self.assertEqual({r[0] for r in self.query("SELECT native_id FROM events")}, {"a1:0", "a1:1"})
        self.assertEqual(interventions.select(self.con, created_by=None)["counts"]["by_created_by"]["unknown"], 2)

    def test_claude_nontext_and_mixed_inputs_survive_bad_usage(self):
        path = os.path.join(self.tmp.name, "claude.jsonl")
        self.jsonl(path, [
            {"sessionId": "native", "type": "user", "uuid": "image",
             "message": {"content": [{"type": "image", "source": {"type": "base64", "data": "private"}}]}},
            {"sessionId": "native", "type": "assistant", "uuid": "a1", "message": {
                "id": "m1", "usage": {"input_tokens": True},
                "content": [{"type": "text", "text": "Valid answer with invalid usage"}]}},
            {"sessionId": "native", "type": "user", "uuid": "mixed", "message": {
                "content": [{"type": "tool_result", "tool_use_id": "call", "content": "done"},
                            {"type": "text", "text": "Additional native input"}]}},
        ])
        claude.sync(self.con, source=path)
        self.assertEqual({r[0] for r in self.query("SELECT native_id FROM user_messages")},
                         {"claude:image", "claude:mixed"})
        self.assertEqual({r[0] for r in self.query("SELECT family FROM events")},
                         {"assistant_message", "tool_result"})
        self.assertEqual(self.query("SELECT error FROM import_errors")[0][0], "malformed_usage")
        self.assertNotIn("private", "\n".join(self.con.iterdump()))

    def test_unmatched_claude_input_never_inherits_a_matched_inputs_actor(self):
        native = write_v2(os.path.join(self.tmp.name, "chromeria-v2.sqlite"), legacy=False)
        t3.sync(self.con, source=native)
        path = self.jsonl(os.path.join(self.tmp.name, "claude.jsonl"), [
            {"sessionId": CL2, "type": "user", "uuid": mid, "promptSource": "sdk",
             "message": {"content": text}}
            for mid, text in ((P1, "first native input"), (P5, "unmatched native input"))
        ])
        claude.sync(self.con, source=path)
        self.assertIsNone(t3.prompt_actor(self.con, "claude:" + CL2,
                                         "claude:" + P5, "claude:" + P1))
        for backfill in (False, True):
            if backfill:
                t3.sync(self.con, source=native)
            result = interventions.select(self.con, created_by=None)
            actors = {r["id"]: r["created_by"] for r in result["interventions"]}
            self.assertEqual(actors, {"sub:claude:" + P1: "user", "sub:claude:" + P5: "unknown"})
            self.assertEqual(self.query("SELECT kind FROM user_messages WHERE native_id=?",
                                        ("claude:" + P5,))[0][0], "unknown")
            self.assertEqual(verdicts.recurrence(self.con, "correction")["coverage"]["by_created_by"],
                             {"user": 1, "agent": 0, "unknown": 1})
        native_con = sqlite3.connect(native)
        native_con.execute("DELETE FROM orchestration_v2_projection_provider_turns")
        native_con.commit()
        native_con.close()
        t3.sync(self.con, source=native)
        self.assertEqual(interventions.select(self.con, created_by=None)["counts"]["by_created_by"],
                         {"user": 0, "agent": 0, "unknown": 2})

    def test_codex_explicit_ordinal_upgrade_preserves_ownership_without_duplicates(self):
        records = [{"type": "session_meta", "ordinal": 0, "payload": {"id": "native", "source": "cli"}}]
        records.extend({"type": "response_item", "ordinal": ordinal, "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "native input"}]}}
                       for ordinal in (10, 11))
        path = self.jsonl(os.path.join(self.tmp.name, "rollout-native.jsonl"), records)
        codex.sync(self.con, source=path)
        # Native identities produced by the pre-#85 adapter on this exact
        # rollout. Only the legacy keys and privacy version differ.
        for position, ordinal in enumerate((10, 11), 1):
            self.con.execute("UPDATE user_messages SET native_id=? WHERE native_id=?",
                             (f"codex:ordinal:{ordinal}", f"codex:native:file:rollout-native.jsonl:ordinal:{position}"))
        self.con.execute("UPDATE sources SET privacy_version=8")
        self.con.execute("INSERT INTO tasks(task_id,title,created_at) VALUES('T-85','T-85',0)")
        self.con.execute("INSERT INTO assignments(user_message_id,task_id,created_at) VALUES('codex:ordinal:10','T-85',0)")
        self.con.execute("INSERT INTO delegations(child_thread,owning_user_message,created_at) VALUES('child','codex:ordinal:10',0)")
        self.con.execute("INSERT INTO verdicts(intervention_id,behavior,judge,verdict,recorded_at)"
                         " VALUES('sub:codex:ordinal:10','correction','judge','yes',0)")
        self.con.commit()
        codex.sync(self.con, source=path)
        self.assertEqual({r[0] for r in self.query("SELECT native_id FROM user_messages")},
                         {"codex:native:file:rollout-native.jsonl:ordinal:1", "codex:native:file:rollout-native.jsonl:ordinal:2"})
        self.assertEqual(self.query("SELECT user_message_id FROM assignments")[0][0], "codex:native:file:rollout-native.jsonl:ordinal:1")
        self.assertEqual(self.query("SELECT owning_user_message FROM delegations")[0][0], "codex:native:file:rollout-native.jsonl:ordinal:1")
        self.assertEqual(self.query("SELECT intervention_id FROM verdicts")[0][0], "sub:codex:native:file:rollout-native.jsonl:ordinal:1")

    def test_unmatched_claude_sdk_actor_stays_unknown_at_a_known_entrypoint(self):
        for entrypoint in ("cli", "sdk-cli"):
            session = "native-" + entrypoint
            path = self.jsonl(os.path.join(self.tmp.name, session + ".jsonl"), [
                {"sessionId": session, "type": "user", "uuid": session,
                 "entrypoint": entrypoint, "promptSource": "sdk" if entrypoint == "cli" else "external",
                 "message": {"content": [{"type": "image", "source": {"type": "base64", "data": "private"}}]}}
            ])
            claude.sync(self.con, source=path)
        selected = interventions.select(self.con, created_by=None)
        self.assertEqual(selected["counts"]["by_created_by"], {"user": 0, "agent": 0, "unknown": 2})
        self.assertEqual(verdicts.recurrence(self.con, "repeated_instruction")["coverage"]["by_created_by"],
                         {"user": 0, "agent": 0, "unknown": 2})

    def test_codex_mixed_ordinal_upgrade_keeps_the_original_placeholder_owners(self):
        def message(role, text, ordinal):
            return {"type": "response_item", "ordinal": ordinal, "payload": {
                "type": "message", "role": role, "content": [{"type": "text", "text": text}]}}
        path = self.jsonl(os.path.join(self.tmp.name, "rollout-native.jsonl"), [
            {"type": "session_meta", "ordinal": 0, "payload": {"id": "native", "source": "cli"}},
            message("user", "explicit user", 10), message("assistant", "explicit answer", 11),
            message("user", "implicit user", None), message("assistant", "implicit answer", None),
            {"type": "event_msg", "ordinal": 12, "payload": {"type": "turn_aborted"}},
            {"type": "event_msg", "ordinal": None, "payload": {"type": "turn_aborted"}},
        ])
        codex.sync(self.con, source=path)
        assistant_id = self.query("SELECT id FROM events WHERE native_id='file:rollout-native.jsonl:ordinal:4'")[0][0]
        abort_id = self.query("SELECT id FROM events WHERE native_id='turn_aborted:file:rollout-native.jsonl:ordinal:5'")[0][0]
        for new, old in (("codex:native:file:rollout-native.jsonl:ordinal:1", "codex:ordinal:10"),
                         ("codex:native:file:rollout-native.jsonl:ordinal:3", "codex:ordinal:None")):
            self.con.execute("UPDATE user_messages SET native_id=? WHERE native_id=?", (old, new))
        self.con.execute("UPDATE events SET native_id='ordinal:None',ordinal_num=NULL WHERE id=?", (assistant_id,))
        self.con.execute("UPDATE events SET native_id='ordinal:11' WHERE native_id='file:rollout-native.jsonl:ordinal:2'")
        self.con.execute("DELETE FROM events WHERE native_id='turn_aborted:file:rollout-native.jsonl:ordinal:6'")
        self.con.execute("UPDATE events SET native_id='turn_aborted:None' WHERE id=?", (abort_id,))
        for native in ("ordinal:None", "turn_aborted:None"):
            family = "assistant_message" if native == "ordinal:None" else "lifecycle"
            self.con.execute("INSERT INTO verdicts(intervention_id,behavior,judge,verdict,recorded_at)"
                             " VALUES(?, 'correction', 'judge', 'yes', 0)",
                             (f"ev:codex:native/{family}/{native}",))
        self.con.execute("UPDATE sources SET privacy_version=8")
        self.con.commit()
        for full in (False, True, False):
            result = codex.sync(self.con, source=path, full=full)
            self.assertFalse(result["failed"])
            self.assertEqual({r[0] for r in self.query("SELECT native_id FROM user_messages")},
                             {"codex:native:file:rollout-native.jsonl:ordinal:1", "codex:native:file:rollout-native.jsonl:ordinal:3"})
            self.assertEqual(self.query("SELECT id FROM events WHERE native_id='file:rollout-native.jsonl:ordinal:4'")[0][0], assistant_id)
            self.assertEqual(self.query("SELECT id FROM events WHERE native_id='turn_aborted:file:rollout-native.jsonl:ordinal:5'")[0][0], abort_id)
            self.assertEqual({r[0] for r in self.query("SELECT intervention_id FROM verdicts")},
                             {"ev:codex:native/assistant_message/file:rollout-native.jsonl:ordinal:4",
                              "ev:codex:native/lifecycle/turn_aborted:file:rollout-native.jsonl:ordinal:5"})
            self.assertEqual(len(self.query("SELECT * FROM events")), 4)

    def test_codex_missing_and_null_ordinals_both_hydrate(self):
        records = [{"type": "session_meta", "payload": {"id": "native", "source": "cli"}},
                   {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "missing ordinal"}]}},
                   {"type": "response_item", "ordinal": None, "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "null ordinal"}]}}]
        path = self.jsonl(os.path.join(self.tmp.name, "rollout-native.jsonl"), records)
        codex.sync(self.con, source=path)
        rows = self.query("SELECT id FROM events WHERE family='assistant_message' ORDER BY id")
        hydrated = lookup.show(self.con, [r[0] for r in rows])
        self.assertEqual([r["status"] for r in hydrated], ["ok", "ok"])
        self.assertEqual([r["text"] for r in hydrated], ["missing ordinal", "null ordinal"])

    def test_codex_continuation_files_never_share_fallback_event_identity(self):
        paths = []
        for suffix in ("first", "resumed"):
            path = self.jsonl(os.path.join(self.tmp.name, "rollout-" + suffix + ".jsonl"), [
                {"type": "session_meta", "payload": {"id": "native", "source": "cli"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "same native input"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "same native answer"}]}},
                {"type": "event_msg", "payload": {"type": "turn_aborted"}},
            ])
            paths.append(path)
            codex.sync(self.con, source=path)
        self.assertEqual(len(self.query("SELECT * FROM user_messages")), 2)
        self.assertEqual(len(self.query("SELECT * FROM events WHERE family='assistant_message'")), 2)
        self.assertEqual(len(self.query("SELECT * FROM events WHERE name='turn_aborted'")), 2)
        before = [tuple(r) for r in self.query("SELECT id,native_id FROM events ORDER BY id")]
        for path in paths:
            codex.sync(self.con, source=path, full=True)
        archive = os.path.join(self.tmp.name, "archived_sessions")
        os.mkdir(archive)
        copied = shutil.copyfile(paths[0], os.path.join(archive, os.path.basename(paths[0])))
        codex.sync(self.con, source=copied)
        self.assertEqual(len(self.query("SELECT * FROM user_messages")), 2)
        self.assertEqual([tuple(r) for r in self.query("SELECT id,native_id FROM events ORDER BY id")], before)
        rows = self.query("SELECT id FROM events WHERE family='assistant_message'")
        self.assertEqual([r["text"] for r in lookup.show(self.con, [r[0] for r in rows])],
                         ["same native answer", "same native answer"])

    def codex_user_texts(self):
        rows = self.query("SELECT native_id FROM user_messages ORDER BY native_id")
        shown = interventions.show(self.con, ["sub:" + r[0] for r in rows],
                                   turns_before=0, turns_after=0)
        return [(r["status"], r.get("text")) for r in shown]

    def test_codex_native_ordinals_never_collide_with_record_positions(self):
        path = self.jsonl(os.path.join(self.tmp.name, "rollout-native.jsonl"), self.codex_overlapping_ordinals())
        for full in (False, True, False):
            codex.sync(self.con, source=path, full=full)
            self.assertEqual(len(self.query("SELECT * FROM user_messages")), 2)
            self.assertEqual(self.codex_user_texts(),
                             [("ok", "first input"), ("ok", "second input")])
            assistant = self.query("SELECT id FROM events WHERE family='assistant_message' ORDER BY id")
            self.assertEqual([r["text"] for r in lookup.show(self.con, [r[0] for r in assistant])],
                             ["first answer", "second answer"])
            stops = self.query("SELECT id FROM events WHERE name='turn_aborted' ORDER BY id")
            self.assertEqual([r["record"]["payload"]["duration_ms"] for r in lookup.show(self.con, [r[0] for r in stops])],
                             [7, 11])

    def test_codex_overlapping_legacy_ordinals_keep_ownership_on_upgrade(self):
        records = self.codex_overlapping_ordinals()
        path = self.jsonl(os.path.join(self.tmp.name, "rollout-native.jsonl"), records[:1])
        codex.sync(self.con, source=path)
        source = self.query("SELECT id FROM sources")[0][0]
        # The pre-privacy-9 writer: metadata ordinal in id and ordinal_num,
        # and the hash of the record's own text.
        for native, ordinal, text in (("None", None, "first input"), ("1", 1, "second input")):
            self.con.execute("INSERT INTO user_messages(native_id,source_id,session_key,ordinal_num,kind,text_hash,text_excerpt,is_genuine)"
                             " VALUES(?,?,'codex:native',?,'genuine',?,'old',1)",
                             ("codex:ordinal:" + native, source, ordinal, text_hash(text)))
            self.con.execute("INSERT INTO verdicts(intervention_id,behavior,judge,verdict,recorded_at) VALUES(?,'correction','judge','yes',0)",
                             ("sub:codex:ordinal:" + native,))
        self.con.execute("INSERT INTO tasks(task_id,title,created_at) VALUES('T-85','T-85',0)")
        self.con.execute("INSERT INTO assignments(user_message_id,task_id,created_at) VALUES('codex:ordinal:1','T-85',0)")
        self.con.execute("INSERT INTO delegations(child_thread,owning_user_message,created_at) VALUES('child','codex:ordinal:None',0)")
        self.con.execute("UPDATE sources SET privacy_version=8")
        self.con.commit()
        self.jsonl(path, records)
        for full in (False, True, False):
            result = codex.sync(self.con, source=path, full=full)
            self.assertFalse(result["failed"])
            self.assertEqual({r[0] for r in self.query("SELECT native_id FROM user_messages")},
                             {"codex:native:file:rollout-native.jsonl:ordinal:1", "codex:native:file:rollout-native.jsonl:ordinal:2"})
            self.assertEqual(self.query("SELECT user_message_id FROM assignments")[0][0], "codex:native:file:rollout-native.jsonl:ordinal:2")
            self.assertEqual(self.query("SELECT owning_user_message FROM delegations")[0][0], "codex:native:file:rollout-native.jsonl:ordinal:1")
            self.assertEqual({r[0] for r in self.query("SELECT intervention_id FROM verdicts")},
                             {"sub:codex:native:file:rollout-native.jsonl:ordinal:1", "sub:codex:native:file:rollout-native.jsonl:ordinal:2"})
            self.assertEqual(self.codex_user_texts(),
                             [("ok", "first input"), ("ok", "second input")])
            self.assertFalse(self.query("PRAGMA foreign_key_check"))

    def test_retired_claude_origin_preserves_transcript_person_marker(self):
        native = write_v2(os.path.join(self.tmp.name, "chromeria-v2.sqlite"), legacy=False)
        t3.sync(self.con, source=native)
        path = self.jsonl(os.path.join(self.tmp.name, "claude.jsonl"), [
            {"sessionId": CL2, "type": "user", "uuid": P1, "entrypoint": "cli", "promptSource": "typed",
             "message": {"content": "typed native input"}},
            {"sessionId": CL2, "type": "user", "uuid": P5, "promptSource": "sdk",
             "message": {"content": "unmatched native input"}},
        ])
        claude.sync(self.con, source=path)
        native_con = sqlite3.connect(native)
        native_con.execute("DELETE FROM orchestration_v2_projection_provider_turns")
        native_con.commit()
        native_con.close()
        t3.sync(self.con, source=native)
        self.con.commit()
        unchanged = claude.sync(self.con, source=path)
        self.assertTrue(unchanged["unchanged"])
        self.assertEqual(tuple(self.query("SELECT kind,text_excerpt FROM user_messages WHERE native_id=?", ("claude:" + P1,))[0]),
                         ("genuine", "typed native input"))
        self.assertEqual(tuple(self.query("SELECT kind,text_excerpt FROM user_messages WHERE native_id=?", ("claude:" + P5,))[0]),
                         ("unknown", ""))
        self.assertEqual(verdicts.recurrence(self.con, "correction")["coverage"]["by_created_by"],
                         {"user": 1, "agent": 0, "unknown": 1})

    def test_claude_full_import_restores_native_classification_and_excerpt(self):
        path = self.jsonl(os.path.join(self.tmp.name, "claude.jsonl"), [
            {"sessionId": "native", "type": "user", "uuid": "typed", "entrypoint": "cli", "promptSource": "typed",
             "message": {"content": "typed native input"}},
        ])
        claude.sync(self.con, source=path)
        self.con.execute("UPDATE user_messages SET kind='unknown',is_genuine=0,text_excerpt=''")
        self.con.commit()
        claude.sync(self.con, source=path, full=True)
        self.assertEqual(tuple(self.query("SELECT kind,text_excerpt FROM user_messages")[0]),
                         ("genuine", "typed native input"))

    def test_codex_archived_legacy_rollout_migrates_identity_and_ownership(self):
        path = self.jsonl(os.path.join(self.tmp.name, "rollout-native.jsonl"), [
            {"type": "session_meta", "payload": {"id": "native", "source": "cli"}},
            {"type": "response_item", "ordinal": 10, "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "native input"}]}},
            {"type": "response_item", "ordinal": 11, "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "native answer"}]}},
        ])
        codex.sync(self.con, source=path)
        event_id = self.query("SELECT id FROM events WHERE family='assistant_message'")[0][0]
        # Reproduce the exact legacy rows produced by the privacy-8 adapter.
        self.con.execute("UPDATE user_messages SET native_id='codex:ordinal:10'")
        self.con.execute("UPDATE events SET native_id='ordinal:11' WHERE id=?", (event_id,))
        self.con.execute("UPDATE sources SET privacy_version=8")
        self.con.execute("INSERT INTO tasks(task_id,title,created_at) VALUES('T-85','T-85',0)")
        self.con.execute("INSERT INTO assignments(user_message_id,task_id,created_at) VALUES('codex:ordinal:10','T-85',0)")
        self.con.execute("INSERT INTO delegations(child_thread,owning_user_message,created_at) VALUES('child','codex:ordinal:10',0)")
        for identity in ("sub:codex:ordinal:10", "ev:codex:native/assistant_message/ordinal:11"):
            self.con.execute("INSERT INTO verdicts(intervention_id,behavior,judge,verdict,recorded_at) VALUES(?,'correction','judge','yes',0)",
                             (identity,))
        self.con.commit()
        continuation = shutil.copyfile(path, os.path.join(self.tmp.name, "rollout-continuation.jsonl"))
        codex.sync(self.con, source=continuation)
        self.assertEqual(self.query("SELECT user_message_id FROM assignments")[0][0], "codex:ordinal:10")
        self.assertEqual(self.query("SELECT native_id FROM events WHERE id=?", (event_id,))[0][0], "ordinal:11")
        archive = os.path.join(self.tmp.name, "archived_sessions")
        os.mkdir(archive)
        moved = shutil.move(path, archive)
        for full in (False, True, False):
            codex.sync(self.con, source=moved, full=full)
            self.assertEqual({r[0] for r in self.query("SELECT native_id FROM user_messages")},
                             {"codex:native:file:rollout-native.jsonl:ordinal:1",
                              "codex:native:file:rollout-continuation.jsonl:ordinal:1"})
            self.assertEqual(self.query("SELECT count(*) FROM events WHERE family='assistant_message'")[0][0], 2)
            self.assertEqual(self.query("SELECT native_id FROM events WHERE id=?", (event_id,))[0][0],
                             "file:rollout-native.jsonl:ordinal:2")
            self.assertEqual(self.query("SELECT user_message_id FROM assignments")[0][0],
                             "codex:native:file:rollout-native.jsonl:ordinal:1")
            self.assertEqual(self.query("SELECT owning_user_message FROM delegations")[0][0],
                             "codex:native:file:rollout-native.jsonl:ordinal:1")
            self.assertEqual({r[0] for r in self.query("SELECT intervention_id FROM verdicts")},
                             {"sub:codex:native:file:rollout-native.jsonl:ordinal:1",
                              "ev:codex:native/assistant_message/file:rollout-native.jsonl:ordinal:2"})
            hydrated = lookup.show(self.con, [event_id])[0]
            self.assertEqual((hydrated["status"], hydrated.get("text")), ("ok", "native answer"))
            self.assertFalse(self.query("PRAGMA foreign_key_check"))

    def test_claude_mirror_never_overrides_native_sidechain_restriction(self):
        native = write_v2(os.path.join(self.tmp.name, "chromeria-v2.sqlite"), legacy=False)
        path = self.jsonl(os.path.join(self.tmp.name, "claude.jsonl"), [
            {"sessionId": CL2, "type": "user", "uuid": P1, "isSidechain": True,
             "promptSource": "sdk", "message": {"content": "sidechain input"}},
            {"sessionId": CL2, "type": "user", "uuid": P3, "promptSource": "sdk",
             "message": {"content": "ordinary SDK input"}},
        ])
        for mirror_first in (False, True):
            if mirror_first:
                t3.sync(self.con, source=native)
            claude.sync(self.con, source=path, full=True)
            for _ in range(2):
                t3.sync(self.con, source=native)
                row = self.query("SELECT kind,is_genuine,text_excerpt FROM user_messages WHERE native_id=?", ("claude:" + P1,))[0]
                self.assertEqual(tuple(row), ("synthetic", 0, ""))
                self.assertEqual(self.query("SELECT kind FROM user_messages WHERE native_id=?", ("claude:" + P3,))[0][0], "genuine")
                counts = {"user": 1, "agent": 0, "unknown": 0}
                self.assertEqual(interventions.select(self.con, created_by=None)["counts"]["by_created_by"], counts)
                self.assertEqual(verdicts.recurrence(self.con, "correction")["coverage"]["by_created_by"], counts)
                claude.sync(self.con, source=path, full=True)

    def test_claude_server_sidechain_stays_out_of_counted_interventions(self):
        native = write_v2(os.path.join(self.tmp.name, "chromeria-v2.sqlite"), legacy=False)
        native_con = sqlite3.connect(native)
        payload = json.loads(native_con.execute(
            "SELECT payload_json FROM orchestration_v2_projection_messages WHERE message_id='vm1'").fetchone()[0])
        payload.update(createdBy="agent", creationSource="server")
        native_con.execute(
            "UPDATE orchestration_v2_projection_messages SET payload_json=? WHERE message_id='vm1'",
            (json.dumps(payload),))
        native_con.commit()
        native_con.close()
        path = self.jsonl(os.path.join(self.tmp.name, "claude.jsonl"), [
            {"sessionId": CL2, "type": "user", "uuid": P1, "isSidechain": True,
             "promptSource": "sdk", "message": {"content": "sidechain server input"}},
            {"sessionId": CL2, "type": "user", "uuid": P3, "promptSource": "sdk",
             "message": {"content": "ordinary SDK input"}},
            {"sessionId": CL2, "type": "user", "uuid": P4, "promptSource": "sdk",
             "message": {"content": "ordinary agent SDK input"}},
        ])
        counted = {"user": 1, "agent": 1, "unknown": 0}
        for mirror_first in (False, True):
            self.con.close()
            self.db_path = os.path.join(self.tmp.name, "ledger-" + str(mirror_first) + ".db")
            self.con = db.connect(self.db_path)
            db.init_db(self.con)
            if mirror_first:
                t3.sync(self.con, source=native)
            else:
                claude.sync(self.con, source=path)
                self.assertEqual(interventions.select(self.con, created_by=None)["counts"]["by_created_by"],
                                 {"user": 0, "agent": 0, "unknown": 2})
            claude.sync(self.con, source=path, full=True)
            for _ in range(2):
                t3.sync(self.con, source=native)
                row = self.query(
                    "SELECT kind,is_genuine,text_excerpt FROM user_messages WHERE native_id=?",
                    ("claude:" + P1,))[0]
                self.assertEqual(tuple(row), ("synthetic", 0, ""))
                self.assertEqual(self.query(
                    "SELECT origin_allowed FROM claude_native_kinds WHERE native_id=?",
                    ("claude:" + P1,))[0][0], 0)
                self.assertEqual(self.query("SELECT kind FROM user_messages WHERE native_id=?",
                                            ("claude:" + P4,))[0][0], "synthetic")
                self.assertEqual(interventions.select(self.con, created_by=None)["counts"]["by_created_by"], counted)
                self.assertEqual(verdicts.recurrence(self.con, "correction")["coverage"]["by_created_by"], counted)
                claude.sync(self.con, source=path, full=True)

    def test_claude_retired_agent_origin_recovers_native_excerpt_for_search(self):
        native = write_v2(os.path.join(self.tmp.name, "chromeria-v2.sqlite"), legacy=False)
        native_con = sqlite3.connect(native)
        payload = json.loads(native_con.execute("SELECT payload_json FROM orchestration_v2_projection_messages WHERE message_id='vm1'").fetchone()[0])
        payload.update(createdBy="agent", creationSource="server")
        native_con.execute("UPDATE orchestration_v2_projection_messages SET payload_json=? WHERE message_id='vm1'", (json.dumps(payload),))
        native_con.commit()
        path = self.jsonl(os.path.join(self.tmp.name, "claude.jsonl"), [
            {"sessionId": CL2, "type": "user", "uuid": P1, "entrypoint": "cli", "promptSource": "typed",
             "message": {"content": "Recovered native input\n<system-reminder>private body</system-reminder>"}},
        ])
        claude.sync(self.con, source=path)
        self.assertEqual(search.search(self.con, ["Recovered"])["total"], 1)
        t3.sync(self.con, source=native)
        claude.sync(self.con, source=path, full=True)
        self.assertEqual(tuple(self.query("SELECT kind,text_excerpt FROM user_messages")[0]), ("synthetic", ""))
        self.assertEqual(search.search(self.con, ["Recovered"])["total"], 0)
        native_con.execute("DELETE FROM orchestration_v2_projection_provider_turns")
        native_con.commit()
        native_con.close()
        t3.sync(self.con, source=native)
        self.con.commit()
        self.assertTrue(claude.sync(self.con, source=path)["unchanged"])
        self.assertEqual(tuple(self.query("SELECT kind,text_excerpt FROM user_messages")[0]), ("genuine", "Recovered native input"))
        self.assertEqual(search.search(self.con, ["Recovered"])["total"], 1)
        self.assertNotIn("private body", "\n".join(self.con.iterdump()))

    def test_claude_restored_sdk_origin_recovers_native_excerpt_on_routine_sync(self):
        native = write_v2(os.path.join(self.tmp.name, "chromeria-v2.sqlite"), legacy=False)
        t3.sync(self.con, source=native)
        path = self.jsonl(os.path.join(self.tmp.name, "claude.jsonl"), [
            {"sessionId": CL2, "type": "user", "uuid": P1, "promptSource": "sdk",
             "message": {"content": "Recovered SDK input"}},
        ])
        claude.sync(self.con, source=path)
        self.assertEqual(search.search(self.con, ["Recovered"])["total"], 1)
        native_con = sqlite3.connect(native)
        turns = native_con.execute("SELECT * FROM orchestration_v2_projection_provider_turns").fetchall()
        native_con.execute("DELETE FROM orchestration_v2_projection_provider_turns")
        native_con.commit()
        t3.sync(self.con, source=native)
        self.assertEqual(tuple(self.query("SELECT kind,text_excerpt FROM user_messages")[0]), ("unknown", ""))
        self.assertEqual(search.search(self.con, ["Recovered"])["total"], 0)
        native_con.executemany("INSERT INTO orchestration_v2_projection_provider_turns VALUES(?,?,?)", turns)
        native_con.commit()
        native_con.close()
        t3.sync(self.con, source=native)
        self.con.commit()
        self.assertTrue(claude.sync(self.con, source=path)["unchanged"])
        self.assertEqual(tuple(self.query("SELECT kind,text_excerpt FROM user_messages")[0]), ("genuine", "Recovered SDK input"))
        self.assertEqual(search.search(self.con, ["Recovered"])["total"], 1)

    def test_opencode_cleared_assistant_text_is_no_longer_searchable(self):
        path = os.path.join(self.tmp.name, "opencode.db")
        build_native(path)
        native = sqlite3.connect(path)
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)", (
            "cleared-answer", "msg_a1", "ses_parent", 1788000000500, 1788000000500,
            json.dumps({"type": "text", "text": "Distinct searchable token"})))
        native.commit()
        opencode.sync(self.con, source=path)
        event_id = self.query("SELECT id FROM events WHERE native_id='cleared-answer'")[0][0]
        self.assertEqual(search.search(self.con, ["Distinct"])["total"], 1)

        def cleared():
            rows = self.query(
                "SELECT id,size_bytes,fingerprint,detail_json FROM events WHERE native_id='cleared-answer'")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], event_id)
            self.assertEqual(rows[0]["size_bytes"], 0)
            self.assertEqual(rows[0]["fingerprint"], text_hash(""))
            self.assertIsNone(rows[0]["detail_json"])
            self.assertEqual(search.search(self.con, ["Distinct"])["total"], 0)

        native.execute("UPDATE part SET time_updated=time_updated+1, data=? WHERE id='cleared-answer'",
                       (json.dumps({"type": "text", "text": ""}),))
        native.commit()
        for full in (False, True, False):
            opencode.sync(self.con, source=path, full=full)
            cleared()
        native.execute("UPDATE part SET time_updated=time_updated+1, data=? WHERE id='cleared-answer'",
                       (json.dumps({"type": "text", "text": "Restored searchable token"}),))
        native.commit()
        native.close()
        for full in (False, True, False):
            opencode.sync(self.con, source=path, full=full)
            row = self.query("SELECT id,detail_json FROM events WHERE native_id='cleared-answer'")[0]
            self.assertEqual(row["id"], event_id)
            self.assertIn("Restored searchable token", row["detail_json"])
            self.assertNotIn("Distinct searchable token", row["detail_json"])
            self.assertEqual(search.search(self.con, ["Restored"])["total"], 1)
            self.assertEqual(search.search(self.con, ["Distinct"])["total"], 0)

    def test_opencode_abort_updates_existing_error_event_in_place(self):
        path = os.path.join(self.tmp.name, "opencode.db")
        build_native(path)
        opencode.sync(self.con, source=path)
        event = self.query("SELECT id,name,status FROM events WHERE family='lifecycle' AND native_id='msg_a_err'")[0]
        self.assertEqual(tuple(event)[1:], ("error", "403"))
        native = sqlite3.connect(path)
        data = json.loads(native.execute("SELECT data FROM message WHERE id='msg_a_err'").fetchone()[0])
        data["error"] = {"name": "MessageAbortedError", "data": {"message": "private abort text"}}
        native.execute("UPDATE message SET data=?,time_updated=time_updated+1 WHERE id='msg_a_err'", (json.dumps(data),))
        native.commit()
        native.close()
        for full in (False, True, False):
            opencode.sync(self.con, source=path, full=full)
            rows = self.query("SELECT id,name,status,detail_json FROM events WHERE family='lifecycle' AND native_id='msg_a_err'")
            self.assertEqual([tuple(r) for r in rows], [(event[0], "turn_aborted", "interrupted", None)])
            self.assertNotIn("private abort text", "\n".join(self.con.iterdump()))

    def test_native_actor_identity_distinguishes_equal_prompts_and_drivers(self):
        t3._ensure_tables(self.con)
        self.con.execute("INSERT INTO t3_native_prompt_origins VALUES(?,?,?)",
                         ("codex:native", "codex:turn-user", "client"))
        self.con.execute("INSERT INTO t3_native_prompt_origins VALUES(?,?,?)",
                         ("codex:native", "codex:turn-agent", "server"))
        self.assertEqual(t3.prompt_actor(self.con, "codex:native", "codex:message", "codex:turn-user"), "client")
        self.assertEqual(t3.prompt_actor(self.con, "codex:native", "codex:message", "codex:turn-agent"), "server")
        self.assertIsNone(t3.prompt_actor(self.con, "grok:native", "codex:message", "codex:turn-user"))
        self.assertIsNone(t3.prompt_actor(self.con, "codex:native", "unmatched"))

    def test_opencode_file_only_user_message_is_counted_on_every_import(self):
        path = os.path.join(self.tmp.name, "opencode.db")
        build_native(path)
        native = sqlite3.connect(path)
        native.execute("INSERT INTO message VALUES(?,?,?,?,?)", (
            "file-user", "ses_parent", 1788000010000, 1788000010000,
            json.dumps({"role": "user", "time": {"created": 1788000010000}})))
        native.execute("INSERT INTO part VALUES(?,?,?,?,?,?)", (
            "file-part", "file-user", "ses_parent", 1788000010000, 1788000010000,
            json.dumps({"type": "file", "url": "data:private"})))
        native.commit()
        native.close()
        for full in (False, True, False):
            opencode.sync(self.con, source=path, full=full)
            row = self.query("SELECT kind,text_excerpt FROM user_messages WHERE native_id='opencode:file-user'")[0]
            self.assertEqual(tuple(row), ("unknown", ""))
        result = verdicts.recurrence(self.con, "repeated_instruction")
        self.assertGreater(result["coverage"]["by_created_by"]["unknown"], 0)
        self.assertIn("unknown", verdicts.render(result))

    def test_v2_projection_joins_native_refs_for_all_harnesses(self):
        path = os.path.join(self.tmp.name, "chromeria-v2.sqlite")
        native = sqlite3.connect(path)
        native.executescript(V2_TABLES)
        for driver, harness in (("claudeAgent", "claude"), ("codex", "codex"),
                                ("grok", "grok"), ("opencode2", "opencode")):
            thread, sid, provider = "thread-" + harness, "session-" + harness, "provider-" + harness
            native.execute("INSERT INTO orchestration_v2_projection_provider_threads"
                           "(provider_thread_id,thread_id,provider,status,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
                           (provider, thread, "custom-account", "idle", "2026-10-14T10:00:00Z",
                            json.dumps({"nativeThreadRef": {"driver": driver, "nativeId": sid, "strength": "strong"}})))
            db.upsert_session(self.con, f"{harness}:{sid}", harness, sid, None)
            for i, by in enumerate(("user", "agent")):
                mid, run, attempt, nid = f"message-{harness}-{i}", f"run-{harness}-{i}", f"attempt-{harness}-{i}", f"native-{harness}-{i}"
                native.execute("INSERT INTO orchestration_v2_projection_messages"
                               "(message_id,thread_id,run_id,role,streaming,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                               (mid, thread, run, "user", 0, "2026-10-14", "2026-10-14",
                                json.dumps({"createdBy": by, "creationSource": "web" if by == "user" else "mcp",
                                            "text": "identical wrapped text that differs from native"})))
                native.execute("INSERT INTO orchestration_v2_projection_runs"
                               "(run_id,thread_id,ordinal,provider,status,requested_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                               (run, thread, i + 1, "custom-account", "completed", "2026-10-14",
                                json.dumps({"userMessageId": mid})))
                native.execute("INSERT INTO orchestration_v2_projection_run_attempts VALUES(?,?,?,?)",
                               (attempt, run, thread, provider))
                native.execute("INSERT INTO orchestration_v2_projection_provider_turns VALUES(?,?,?)",
                               (provider, attempt, json.dumps({"nativeTurnRef": {
                                   "driver": driver, "strength": "weak" if harness in ("grok", "opencode") else "strong", "nativeId": nid}})))
                identity = f"{harness}:{sid}:{nid}" if harness == "grok" else f"{harness}:{nid}"
                self.con.execute("INSERT INTO user_messages(native_id,session_key,turn_id,kind,is_genuine,text_hash,text_excerpt) VALUES(?,?,?,?,?,'0123456789abcdef','')",
                                 (identity if harness != "grok" else f"grok:{sid}:prompt:{i}",
                                  f"{harness}:{sid}", identity, "unknown", 0))
        native.commit()
        native.close()
        result = t3.sync(self.con, source=path)
        self.assertEqual(result["prompt_origins"], 8)
        selected = interventions.select(self.con, created_by=None)
        self.assertEqual(selected["counts"]["by_created_by"], {"user": 4, "agent": 4, "unknown": 0})
        # Remove identity evidence. Equal text is never sufficient and stale
        # legacy hash evidence cannot survive the next mirror import.
        native = sqlite3.connect(path)
        native.execute("DELETE FROM orchestration_v2_projection_provider_turns")
        native.commit()
        native.close()
        t3.sync(self.con, source=path)
        self.assertEqual(interventions.select(self.con, created_by=None)["counts"]["by_created_by"]["unknown"], 8)

    def test_codex_chronology_is_one_native_position_sequence(self):
        """File position orders every Codex row, including a missing ordinal
        after an explicit one. A NULL sorts first, and a metadata ordinal is
        a different sequence, so neither may be the stored chronology.
        """
        for tied in (False, True):
            self._assert_codex_position_chronology(tied)

    def _assert_codex_position_chronology(self, tied: bool):
        label = "tied" if tied else "absent"
        path = os.path.join(self.tmp.name, f"chrono-{label}.jsonl")
        stamp = "2026-10-14T10:00:00Z" if tied else None

        def row(kind, payload, ordinal=None):
            record = {"type": kind, "payload": payload}
            if ordinal is not None:
                record["ordinal"] = ordinal
            if stamp is not None:
                record["timestamp"] = stamp
            return record

        def message(role, native_id, text, ordinal=None):
            content_type = "input_text" if role == "user" else "output_text"
            payload = {"type": "message", "role": role, "id": native_id,
                       "content": [{"type": content_type, "text": text}]}
            if role == "user":
                payload["internal_chat_message_metadata_passthrough"] = {
                    "content_item_kinds": ["user.text"]}
            return row("response_item", payload, ordinal)

        # Physical lines: meta 0, u1 1, a1 2, u2 3, a2 4, a3 5, call 6,
        # output 7, thread_goal 8, task_complete 9, world_state 10. a2 omits
        # its ordinal and follows a1's explicit 4, so NULL-first places a2
        # ahead of a1. a3's metadata 0 is a different sequence. The
        # world_state metadata equals the thread_goal line, which is not
        # indexed under its own native id.
        self.jsonl(path, [
            row("session_meta", {"id": "chrono-" + label, "source": "cli",
                                 "originator": "cli"}),
            message("user", "u1", "first user", 4),
            message("assistant", "a1", "first answer", 4),
            message("user", "u2", "second user", 5),
            message("assistant", "a2", "second answer"),
            message("assistant", "a3", "third answer", 0),
            row("response_item", {
                "type": "function_call", "call_id": "call-1",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "echo tool"})}, 9),
            row("response_item", {
                "type": "function_call_output", "call_id": "call-1",
                "output": "tool output"}),
            row("event_msg", {"type": "thread_goal_updated"}, 77),
            row("event_msg", {"type": "task_complete", "turn_id": "t1"}, 50),
            row("world_state", {}, 8),
        ])
        ledger = db.connect(os.path.join(self.tmp.name, f"chrono-{label}.db"))
        db.init_db(ledger)
        try:
            for full in (False, True, False):
                self.assertFalse(codex.sync(ledger, source=path, full=full)["failed"])
                session_key = ledger.execute(
                    "SELECT session_key FROM sessions").fetchone()["session_key"]
                self.assertEqual(dict(ledger.execute(
                    "SELECT native_id, ordinal_num FROM user_messages")),
                    {"codex:u1": 1, "codex:u2": 3})
                self.assertEqual(dict(ledger.execute(
                    "SELECT family || ':' || native_id, ordinal_num FROM events")),
                    {"assistant_message:a1": 2, "assistant_message:a2": 4,
                     "assistant_message:a3": 5, "tool_call:call-1": 6,
                     "tool_result:call-1": 7,
                     "lifecycle:thread_goal_updated:77": 8,
                     "lifecycle:task_complete:t1": 9})
                shown = interventions.show(
                    ledger, ["sub:codex:u1", "sub:codex:u2"],
                    turns_before=1, turns_after=1)

                def activity(turns):
                    return [[(entry.get("type"), entry.get("text"),
                              entry.get("tool"), entry.get("input"),
                              entry.get("output"))
                             for entry in turn["activity"]]
                            for turn in turns]

                self.assertEqual([(item["text"], activity(item["before"]),
                                   activity(item["after"])) for item in shown], [
                    ("first user", [], [[("assistant", "first answer", None, None, None)]]),
                    ("second user", [[("assistant", "first answer", None, None, None)]],
                     [[("assistant", "second answer", None, None, None),
                       ("assistant", "third answer", None, None, None),
                       ("tool", None, "exec_command", '{"cmd": "echo tool"}',
                        "tool output")]]),
                ])

                def excerpts(rows, detail_of):
                    return [detail_of(row) for row in rows
                            if row["family"] == "assistant_message"]

                self.assertEqual(excerpts(
                    analysis._events(ledger, session_key),
                    lambda row: json.loads(row["detail_json"])["excerpt"]),
                    ["first answer", "second answer", "third answer"])
                self.assertEqual(excerpts(
                    report.timeline(ledger, session_key=session_key)["events"],
                    lambda row: row["detail"]["excerpt"]),
                    ["first answer", "second answer", "third answer"])
                task = ledger.execute(
                    "SELECT id FROM events WHERE native_id='task_complete:t1'"
                ).fetchone()["id"]
                goal = ledger.execute(
                    "SELECT id FROM events WHERE native_id='thread_goal_updated:77'"
                ).fetchone()["id"]
                call = ledger.execute(
                    "SELECT id FROM events WHERE family='tool_call'"
                    " AND native_id='call-1'").fetchone()["id"]
                task_item, goal_item, call_item = lookup.show(
                    ledger, [task, goal, call])
                self.assertEqual(task_item["status"], "ok")
                self.assertEqual(task_item["record"]["payload"]["type"],
                                 "task_complete")
                self.assertEqual(goal_item["status"], "ok")
                self.assertEqual(goal_item["record"]["payload"]["type"],
                                 "thread_goal_updated")
                self.assertEqual(call_item["status"], "ok")
                self.assertEqual(call_item["input"]["cmd"], "echo tool")
                self.assertEqual(call_item["result"], "tool output")
        finally:
            ledger.close()

    def test_repeated_user_message_keeps_its_turn(self):
        for tied in (False, True):
            self._assert_repeated_user_context(tied, upgrade=False)

    def test_privacy8_repeated_user_keeps_context_and_ownership(self):
        for tied in (False, True):
            self._assert_repeated_user_context(tied, upgrade=True)

    def _user_repeat_rows(self, session, tied):
        stamp = "2026-10-14T10:00:00Z" if tied else None

        def message(role, native_id, text, ordinal):
            payload = {"type": "message", "role": role, "id": native_id,
                       "content": [{"type": "text", "text": text}]}
            if role == "user":
                payload["internal_chat_message_metadata_passthrough"] = {
                    "content_item_kinds": ["user.text"]}
            record = {"type": "response_item", "ordinal": ordinal,
                      "payload": payload}
            if stamp is not None:
                record["timestamp"] = stamp
            return record

        meta = {"type": "session_meta", "ordinal": 0, "payload": {
            "id": session, "source": "cli", "originator": "cli"}}
        if stamp is not None:
            meta["timestamp"] = stamp
        rows = [
            meta,
            message("user", "u1", "first user", 1),
            message("assistant", "a1", "first answer", 2),
            message("user", "u2", "second user", 3),
            message("assistant", "a2", "second answer", 4),
        ]
        rows.append(dict(rows[1]))
        return rows

    def _assert_repeated_user_context(self, tied, upgrade):
        label = ("upgrade-" if upgrade else "fresh-") + ("tied" if tied else "absent")
        path = os.path.join(self.tmp.name, f"user-repeat-{label}.jsonl")
        db_path = os.path.join(self.tmp.name, f"user-repeat-{label}.db")
        self.jsonl(path, self._user_repeat_rows("context-" + label, tied))
        if upgrade:
            _import_at_revision(
                "a4ba7cea0b15d6afb2926a931477e5dece1b4d61", db_path, path)
        ledger = db.connect(db_path)
        db.init_db(ledger)
        try:
            if upgrade:
                self.assertEqual(ledger.execute(
                    "SELECT privacy_version FROM sources").fetchone()[0], 8)
                original = ledger.execute(
                    "SELECT source_id FROM user_messages WHERE native_id=?",
                    ("codex:u1",)).fetchone()
                ledger.execute(
                    "INSERT INTO tasks(task_id, title, created_at)"
                    " VALUES('T-repeat','T-repeat',0)")
                ledger.execute(
                    "INSERT INTO assignments(user_message_id, task_id, created_at)"
                    " VALUES('codex:u1','T-repeat',0)")
                ledger.execute(
                    "INSERT INTO delegations(child_thread, owning_user_message,"
                    " created_at) VALUES('repeat-child','codex:u1',0)")
                ledger.execute(
                    "INSERT INTO verdicts(intervention_id, behavior, judge,"
                    " verdict, recorded_at) VALUES('sub:codex:u1','correction',"
                    "'judge','yes',0)")
                ledger.commit()
            for full in (False, True, False):
                self.assertFalse(codex.sync(ledger, source=path, full=full)["failed"])
                self.assertEqual(dict(ledger.execute(
                    "SELECT native_id, ordinal_num FROM user_messages")),
                    {"codex:u1": 1, "codex:u2": 3})
                shown = interventions.show(
                    ledger, ["sub:codex:u1", "sub:codex:u2"],
                    turns_before=1, turns_after=1)

                def activity(turns):
                    return [[(entry.get("type"), entry.get("text"))
                             for entry in turn["activity"]] for turn in turns]

                self.assertEqual([(item["status"], item["text"],
                                   activity(item["before"]),
                                   activity(item["after"])) for item in shown], [
                    ("ok", "first user", [], [[("assistant", "first answer")]]),
                    ("ok", "second user", [[("assistant", "first answer")]],
                     [[("assistant", "second answer")]]),
                ])
            if upgrade:
                kept = ledger.execute(
                    "SELECT source_id FROM user_messages WHERE native_id=?",
                    ("codex:u1",)).fetchone()
                self.assertEqual(kept["source_id"], original["source_id"])
                self.assertEqual(ledger.execute(
                    "SELECT user_message_id FROM assignments").fetchone()[0],
                    "codex:u1")
                self.assertEqual(ledger.execute(
                    "SELECT owning_user_message FROM delegations").fetchone()[0],
                    "codex:u1")
                self.assertEqual(ledger.execute(
                    "SELECT intervention_id FROM verdicts").fetchone()[0],
                    "sub:codex:u1")
                self.assertEqual(ledger.execute(
                    "PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(ledger.execute(
                    "SELECT privacy_version FROM sources").fetchone()[0], 9)
        finally:
            ledger.close()

    def test_continuation_user_keeps_its_source_and_turn(self):
        for tied in (False, True):
            self._assert_continued_user(tied)

    def _assert_continued_user(self, tied):
        label = "tied" if tied else "absent"
        path = os.path.join(self.tmp.name, f"user-main-{label}.jsonl")
        continuation = os.path.join(
            self.tmp.name, f"user-continuation-{label}.jsonl")
        rows = self._user_repeat_rows("continued-" + label, tied)
        self.jsonl(path, rows[:-1])
        continued = [rows[0]] + [
            {"type": "world_state", "ordinal": 20 + i, "payload": {}}
            for i in range(8)] + [dict(rows[1])]
        if tied:
            for record in continued[1:-1]:
                record["timestamp"] = rows[0]["timestamp"]
        self.jsonl(continuation, continued)
        db_path = os.path.join(self.tmp.name, f"user-continued-{label}.db")
        ledger = db.connect(db_path)
        db.init_db(ledger)
        try:
            self._assert_continued_user_on(ledger, path, continuation)
        finally:
            ledger.close()

    def _assert_continued_user_on(self, ledger, path, continuation):
        for full in (False, True, False):
            self.assertFalse(codex.sync(ledger, source=path, full=full)["failed"])
            self.assertFalse(codex.sync(
                ledger, source=continuation, full=full)["failed"])
            owned = {row["native_id"]: row for row in ledger.execute(
                "SELECT user_messages.native_id, user_messages.ordinal_num,"
                " sources.path FROM user_messages"
                " JOIN sources ON sources.id=user_messages.source_id")}
            self.assertEqual(owned["codex:u1"]["ordinal_num"], 1)
            self.assertEqual(os.path.basename(owned["codex:u1"]["path"]),
                             os.path.basename(path))
            self.assertEqual(owned["codex:u2"]["ordinal_num"], 3)
            self.assertEqual(os.path.basename(owned["codex:u2"]["path"]),
                             os.path.basename(path))
            shown = interventions.show(
                ledger, ["sub:codex:u1", "sub:codex:u2"],
                turns_before=1, turns_after=1)

            def activity(turns):
                return [[(entry.get("type"), entry.get("text"))
                         for entry in turn["activity"]] for turn in turns]

            self.assertEqual([(item["status"], item["text"],
                               activity(item["before"]),
                               activity(item["after"])) for item in shown], [
                ("ok", "first user", [], [[("assistant", "first answer")]]),
                ("ok", "second user", [[("assistant", "first answer")]],
                 [[("assistant", "second answer")]]),
            ])

    def test_privacy8_archive_move_keeps_original_ownership(self):
        path = self.jsonl(os.path.join(self.tmp.name, "rollout-native.jsonl"), [
            {"type": "session_meta", "payload": {"id": "native", "source": "cli"}},
            {"type": "response_item", "ordinal": 10, "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "native input"}]}},
            {"type": "response_item", "ordinal": 11, "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "native answer"}]}},
            {"type": "token_usage_record", "ordinal": 12, "payload": {
                "response_id": "r-archive", "thread_id": "native",
                "usage": {"input_tokens": 10, "output_tokens": 2,
                          "total_tokens": 12}}},
        ])
        db_path = os.path.join(self.tmp.name, "archive-upgrade.db")
        _import_at_revision(
            "a4ba7cea0b15d6afb2926a931477e5dece1b4d61", db_path, path)
        ledger = db.connect(db_path)
        db.init_db(ledger)
        try:
            self.assertEqual(ledger.execute(
                "SELECT privacy_version FROM sources").fetchone()[0], 8)
            seeded = {
                "user": ledger.execute(
                    "SELECT native_id, ordinal_num, source_id FROM user_messages"
                ).fetchone(),
                "event": ledger.execute(
                    "SELECT id, native_id, ordinal_num, source_id FROM events"
                    " WHERE family='assistant_message'").fetchone(),
                "response": ledger.execute(
                    "SELECT response_id, ordinal_num, source_id FROM responses"
                ).fetchone(),
            }
            self.assertEqual(seeded["user"]["native_id"], "codex:ordinal:10")
            self.assertEqual(seeded["user"]["ordinal_num"], 10)
            self.assertEqual(seeded["event"]["native_id"], "ordinal:11")
            self.assertEqual(seeded["event"]["ordinal_num"], 11)
            self.assertEqual(seeded["response"]["response_id"], "codex:r-archive")
            self.assertEqual(seeded["response"]["ordinal_num"], 12)
            ledger.execute(
                "INSERT INTO tasks(task_id,title,created_at) VALUES('T-85','T-85',0)")
            ledger.execute(
                "INSERT INTO assignments(user_message_id,task_id,created_at)"
                " VALUES('codex:ordinal:10','T-85',0)")
            ledger.execute(
                "INSERT INTO delegations(child_thread,owning_user_message,created_at)"
                " VALUES('child','codex:ordinal:10',0)")
            for identity in ("sub:codex:ordinal:10",
                             "ev:codex:native/assistant_message/ordinal:11"):
                ledger.execute(
                    "INSERT INTO verdicts(intervention_id,behavior,judge,verdict,"
                    " recorded_at) VALUES(?,'correction','judge','yes',0)",
                    (identity,))
            ledger.commit()
            continuation = shutil.copyfile(
                path, os.path.join(self.tmp.name, "rollout-continuation.jsonl"))
            self.assertFalse(codex.sync(ledger, source=continuation)["failed"])
            self.assertEqual(ledger.execute(
                "SELECT native_id, ordinal_num, source_id FROM user_messages"
                " WHERE native_id='codex:ordinal:10'").fetchone(), seeded["user"])
            self.assertEqual(ledger.execute(
                "SELECT id, native_id, ordinal_num, source_id FROM events"
                " WHERE id=?", (seeded["event"]["id"],)).fetchone(),
                seeded["event"])
            self.assertEqual(ledger.execute(
                "SELECT response_id, ordinal_num, source_id FROM responses"
            ).fetchone(), seeded["response"])
            self.assertEqual(ledger.execute(
                "SELECT user_message_id FROM assignments").fetchone()[0],
                "codex:ordinal:10")
            archive = os.path.join(self.tmp.name, "archived_sessions")
            os.mkdir(archive)
            moved = shutil.move(path, archive)
            for full in (False, True, False):
                self.assertFalse(codex.sync(
                    ledger, source=moved, full=full)["failed"])
                user = ledger.execute(
                    "SELECT native_id, ordinal_num, source_id FROM user_messages"
                    " WHERE native_id LIKE '%rollout-native.jsonl%'").fetchone()
                event = ledger.execute(
                    "SELECT id, native_id, ordinal_num, source_id FROM events"
                    " WHERE id=?", (seeded["event"]["id"],)).fetchone()
                response = ledger.execute(
                    "SELECT response_id, ordinal_num, source_id FROM responses"
                    " WHERE response_id='codex:r-archive'").fetchone()
                moved_source = ledger.execute(
                    "SELECT id FROM sources WHERE path=?", (moved,)).fetchone()[0]
                self.assertEqual(user["native_id"],
                                 "codex:native:file:rollout-native.jsonl:ordinal:1")
                self.assertEqual(user["ordinal_num"], 1)
                self.assertEqual(user["source_id"], moved_source)
                self.assertEqual(event["id"], seeded["event"]["id"])
                self.assertEqual(event["native_id"],
                                 "file:rollout-native.jsonl:ordinal:2")
                self.assertEqual(event["ordinal_num"], 2)
                self.assertEqual(event["source_id"], moved_source)
                self.assertEqual(response["ordinal_num"], seeded["response"]["ordinal_num"])
                self.assertEqual(response["source_id"], seeded["response"]["source_id"])
                self.assertIsNotNone(ledger.execute(
                    "SELECT 1 FROM codex_occurrences WHERE source_id=? AND row_kind='response'"
                    " AND row_key=? AND record_position=3",
                    (moved_source, "codex:r-archive")).fetchone())
                self.assertEqual(ledger.execute(
                    "SELECT user_message_id FROM assignments").fetchone()[0],
                    user["native_id"])
                self.assertEqual(ledger.execute(
                    "SELECT owning_user_message FROM delegations").fetchone()[0],
                    user["native_id"])
                self.assertEqual(
                    {row[0] for row in ledger.execute(
                        "SELECT intervention_id FROM verdicts")},
                    {"sub:" + user["native_id"],
                     "ev:codex:native/assistant_message/" + event["native_id"]})
                self.assertEqual(lookup.show(ledger, [event["id"]])[0]["text"],
                                 "native answer")
                self.assertEqual(ledger.execute(
                    "PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(ledger.execute(
                    "SELECT privacy_version FROM sources WHERE id=?",
                    (moved_source,)).fetchone()[0], 9)
        finally:
            ledger.close()

    def test_explicit_ordinal_lifecycle_ids_survive_privacy_upgrade(self):
        """A privacy-8 row whose native id is its metadata ordinal stays
        that row. The upgrade rewrites chronology to the file position and
        leaves the verdict on the same event.
        """
        path = self.jsonl(os.path.join(self.tmp.name, "rollout-goal.jsonl"), [
            {"type": "session_meta", "payload": {"id": "goal", "source": "cli",
                                                 "originator": "cli"}},
            {"type": "event_msg", "ordinal": 77, "payload": {
                "type": "thread_goal_updated"}},
            {"type": "compacted", "ordinal": 88, "payload": {}},
            {"type": "event_msg", "ordinal": 33, "payload": {
                "type": "item_completed", "item": {"type": "Plan"}}},
        ])
        codex.sync(self.con, source=path)
        session = self.query("SELECT session_key FROM sessions")[0][0]
        before = {
            row["native_id"]: row["id"] for row in self.query(
                "SELECT id, native_id FROM events")}
        self.assertEqual(set(before), {
            "thread_goal_updated:77", "compacted:88", "Plan:33"})
        self.con.execute("UPDATE events SET ordinal_num=CASE native_id"
                         " WHEN 'thread_goal_updated:77' THEN 77"
                         " WHEN 'compacted:88' THEN 88"
                         " WHEN 'Plan:33' THEN 33 END")
        self.con.execute("UPDATE sources SET privacy_version=8")
        for native_id in before:
            family = "compaction" if native_id.startswith("compacted:") else "lifecycle"
            self.con.execute(
                "INSERT INTO verdicts(intervention_id,behavior,judge,verdict,recorded_at)"
                " VALUES(?,'correction','judge','yes',0)",
                (f"ev:{session}/{family}/{native_id}",))
        self.con.commit()
        for full in (False, True, False):
            self.assertFalse(codex.sync(self.con, source=path, full=full)["failed"])
            rows = self.query("SELECT id, native_id, ordinal_num, family FROM events")
            self.assertEqual({row["native_id"]: row["id"] for row in rows}, before)
            self.assertEqual(
                {row["native_id"]: row["ordinal_num"] for row in rows},
                {"thread_goal_updated:77": 1, "compacted:88": 2, "Plan:33": 3})
            self.assertEqual(
                {row[0] for row in self.query("SELECT intervention_id FROM verdicts")},
                {f"ev:{session}/lifecycle/thread_goal_updated:77",
                 f"ev:{session}/compaction/compacted:88",
                 f"ev:{session}/lifecycle/Plan:33"})
            shown = {item["id"]: item for item in lookup.show(
                self.con, [row["id"] for row in rows])}
            self.assertEqual(shown[before["thread_goal_updated:77"]]["record"]["payload"]["type"],
                             "thread_goal_updated")
            self.assertEqual(shown[before["compacted:88"]]["record"]["type"], "compacted")
            self.assertEqual(shown[before["Plan:33"]]["record"]["payload"]["item"]["type"], "Plan")
            self.assertTrue(all(item["status"] == "ok" for item in shown.values()))


if __name__ == "__main__":
    import unittest
    unittest.main()
