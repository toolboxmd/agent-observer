"""Mixed legacy/current Codex identities converge without double counting (#108)."""

import json
import os

from agent_observer import cli, interventions
from agent_observer.adapters import codex
from tests.helpers import LedgerCase


class CodexUserIdentityTest(LedgerCase):
    def test_mixed_upgrade_deduplicates_and_preserves_references(self):
        path = os.path.join(self.tmp.name, "rollout-mixed.jsonl")
        records = [{"type": "session_meta", "payload": {
            "id": "mixed", "source": "cli"}}]
        # Equal text is not identity: these are two separate user messages.
        for ordinal in (10, None):
            record = {"type": "response_item", "timestamp": "2026-10-18T10:00:00Z",
                      "payload": {"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": "synthetic input"}],
                                  "internal_chat_message_metadata_passthrough": {
                                      "content_item_kinds": ["user.text"]}}}
            if ordinal is not None:
                record["ordinal"] = ordinal
            records.append(record)
        with open(path, "w") as stream:
            stream.writelines(json.dumps(record) + "\n" for record in records)
        self.assertFalse(codex.sync(self.con, source=path)["failed"])
        keys = [f"codex:mixed:file:rollout-mixed.jsonl:ordinal:{i}" for i in (1, 2)]
        legacy = ["codex:ordinal:10", "codex:ordinal:None"]
        # Rocky has both persisted identities, with privacy_version still 8.
        # Build that mixed ledger from synthetic native records only.
        for old, key in zip(legacy, keys):
            self.con.execute(
                "INSERT INTO user_messages SELECT ?,alias_id,source_id,session_key,"
                "turn_id,ordinal_num,ts,kind,text_hash,text_excerpt,is_genuine"
                " FROM user_messages WHERE native_id=?", (old, key))
        self.con.execute("UPDATE sources SET privacy_version=8")
        for task in ("shared", "legacy-only", "current-only"):
            self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", (task,))
        for key in (legacy[0], keys[0]):
            for task in ("shared", "legacy-only" if key == legacy[0] else "current-only"):
                self.con.execute("INSERT INTO assignments(user_message_id,task_id,created_at)"
                                 " VALUES(?,?,0)", (key, task))
            for child in ("shared", key):
                self.con.execute("INSERT INTO delegations(owning_user_message,child_thread,created_at)"
                                 " VALUES(?,?,0)", (key, child))
            for judge in ("shared", key):
                self.con.execute("INSERT INTO verdicts(intervention_id,behavior,judge,verdict,recorded_at)"
                                 " VALUES(?,'correction',?,'yes',0)", ("sub:" + key, judge))
        self.con.commit()
        for full in (False, True, False):
            result = codex.sync(self.con, source=path, full=full)
            self.assertEqual(result["failed"], [])
            self.assertEqual(result["user_messages_inserted"], 0)
            self.assertEqual(dict(self.query("SELECT native_id,alias_id FROM user_messages")),
                             dict(zip(keys, legacy)))
            self.assertEqual(self.query("SELECT SUM(is_genuine) FROM user_messages")[0][0], 2)
            self.assertEqual({tuple(r) for r in self.query(
                "SELECT user_message_id,task_id FROM assignments")},
                {(keys[0], task) for task in ("shared", "legacy-only", "current-only")})
            self.assertEqual({r[0] for r in self.query("SELECT owning_user_message FROM delegations")},
                             {keys[0]})
            self.assertEqual(len(self.query("SELECT * FROM delegations")), 3)
            self.assertEqual({r[0] for r in self.query("SELECT intervention_id FROM verdicts")},
                             {"sub:" + keys[0]})
            self.assertEqual(len(self.query("SELECT * FROM verdicts")), 3)
            self.assertEqual(self.query("PRAGMA foreign_key_check"), [])
            self.assertEqual({r[0] for r in self.query(
                "SELECT row_key FROM codex_occurrences WHERE row_kind='user'")}, set(keys))
            self.assertEqual(self.query("SELECT privacy_version FROM sources")[0][0], 9)
            for old, key in zip(legacy, keys):
                self.assertEqual(cli._resolve_submission(self.con, old), key)
            shown = interventions.show(self.con, ["sub:" + key for key in keys])
            self.assertEqual([item["status"] for item in shown], ["ok", "ok"])
