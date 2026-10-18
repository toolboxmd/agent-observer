"""Mixed legacy/current Codex identities converge without losing evidence (#108).

Before privacy 9 the adapter stored an id-less user message as
``codex:ordinal:<metadata ordinal>`` with ``ordinal_num`` set to that
metadata ordinal (see ``_old_writer_row``). The current writer keys the same
record by file position. A ledger can hold both rows for one record.
"""

import json
import os

from agent_observer import cli, privacy, report
from agent_observer.adapters import codex
from agent_observer.ingest import iso_ts, text_hash
from tests.helpers import LedgerCase

TURN = "turn-1"


def _user(text, ordinal=None, ts="2026-10-18T10:00:00Z", turn=TURN):
    record = {"type": "response_item", "timestamp": ts, "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": text}],
        "internal_chat_message_metadata_passthrough": {
            "content_item_kinds": ["user.text"], "turn_id": turn}}}
    if ordinal is not None:
        record["ordinal"] = ordinal
    return record


def _usage(total):
    return {"cache_write_input_tokens": 0, "cached_input_tokens": 0,
            "input_tokens": total - 10, "output_tokens": 10,
            "reasoning_output_tokens": 0, "total_tokens": total}


USAGE = {"type": "token_usage_record", "timestamp": "2026-10-18T10:00:05Z",
         "payload": {"response_id": "resp-1", "session_id": "mixed",
                     "thread_id": "mixed", "turn_id": TURN, "root_turn_id": TURN,
                     "usage": _usage(100), "turn_token_usage": _usage(100),
                     "thread_token_usage": _usage(100)}}


def _old_writer_row(con, record, source_id, session_key="codex:mixed",
                    ts_shift=0.0):
    """The row the pre-privacy-9 adapter inserted for an id-less user record."""
    payload = record["payload"]
    text = codex._text_of_message(payload)
    kind = codex._submission_kind(payload)
    turn = payload["internal_chat_message_metadata_passthrough"].get("turn_id")
    con.execute(
        "INSERT INTO user_messages(native_id, source_id, session_key, turn_id,"
        " ordinal_num, ts, kind, text_hash, text_excerpt, is_genuine)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"codex:ordinal:{record.get('ordinal')}", source_id, session_key,
         f"codex:{turn}" if turn else None, record.get("ordinal"),
         iso_ts(record["timestamp"]) + ts_shift, kind, text_hash(text),
         privacy.submission_excerpt(text, is_genuine=kind == "genuine",
                                    is_main_session=True),
         1 if kind == "genuine" else 0))


class CodexUserIdentityTest(LedgerCase):
    def _rollout(self, *records, name="rollout-mixed.jsonl"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as stream:
            for record in ({"type": "session_meta", "payload": {
                    "id": "mixed", "source": "cli", "thread_source": "user"}},) + records:
                stream.write(json.dumps(record) + "\n")
        self.assertEqual(codex.sync(self.con, source=path)["failed"], [])
        self.source_id = self.query("SELECT id FROM sources WHERE path=?", (path,))[0][0]
        return path

    def _current(self, position, name="rollout-mixed.jsonl"):
        return f"codex:mixed:file:{name}:ordinal:{position}"

    def _make_stale(self):
        self.con.execute("UPDATE sources SET privacy_version=8")
        self.con.commit()

    def _snapshot(self, table):
        return sorted(tuple(r) for r in self.query(f"SELECT * FROM {table}"))

    def _task(self, *tasks):
        for task in tasks:
            self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES(?,0)", (task,))

    def _buckets(self, *tasks):
        out = {}
        for task in tasks:
            got = report.task_report(self.con, task)
            out[task] = tuple((got[b]["responses"], got[b]["total_tokens"]) for b in
                              ("attributed", "shared_joint", "unassigned_in_scope"))
        return out

    def _resync(self, path, conflicts=0):
        for full in (False, True, False):
            result = codex.sync(self.con, source=path, full=full)
            self.assertEqual(result["failed"], [])
            self.assertEqual(result["user_messages_inserted"], 0)
            self.assertEqual(self.query("SELECT privacy_version FROM sources")[0][0], 9)
            self.assertEqual(self.query("PRAGMA foreign_key_check"), [])
            self.assertEqual([tuple(r) for r in self.query(
                "SELECT error, COUNT(*) FROM import_errors GROUP BY error")],
                [("identity_conflict", conflicts)] if conflicts else [])

    def _assert_merged(self, path, legacy, key):
        self._resync(path)
        self.assertEqual([tuple(r) for r in self.query(
            "SELECT native_id, alias_id, ordinal_num, source_id FROM user_messages")],
            [(key, legacy, 1, self.source_id)])
        self.assertEqual(cli._resolve_submission(self.con, legacy), key)
        self.assertEqual({r[0] for r in self.query(
            "SELECT row_key FROM codex_occurrences WHERE row_kind='user'")}, {key})

    def test_legacy_copy_merges_across_ordinal_namespace_drift_and_archive(self):
        cases = {
            "rocky clone": dict(ordinal=1),
            "metadata ordinal": dict(ordinal=10),
            "timestamp drift": dict(ordinal=10, ts_shift=0.001),
            "archived basename": dict(ordinal=10, archived=True),
        }
        for label, case in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                record = _user("synthetic input", ordinal=case["ordinal"])
                path = self._rollout(record)
                source = self.source_id
                if case.get("archived"):
                    self.con.execute(
                        "INSERT INTO sources(harness,path,sha256,imported_at,privacy_version)"
                        " VALUES('codex',?,'',0,8)",
                        (os.path.join(self.tmp.name, "archived", os.path.basename(path)),))
                    source = self.query("SELECT MAX(id) FROM sources")[0][0]
                _old_writer_row(self.con, record, source, ts_shift=case.get("ts_shift", 0.0))
                self._make_stale()
                self._assert_merged(path, f"codex:ordinal:{case['ordinal']}", self._current(1))

    def test_earlier_none_sibling_does_not_roll_back_the_exact_copy(self):
        first, second = _user("first input"), _user("second input", turn="turn-2")
        path = self._rollout(first, second)
        # The old stale update rewrote the collapsed row with the last record.
        _old_writer_row(self.con, second, self.source_id)
        self._make_stale()
        self._resync(path)
        self.assertEqual([tuple(r) for r in self.query(
            "SELECT native_id, alias_id FROM user_messages ORDER BY ordinal_num")],
            [(self._current(1), None), (self._current(2), "codex:ordinal:None")])

    def test_unmatched_collision_keeps_both_rows_and_records_a_gap(self):
        record = _user("synthetic input", ordinal=1)
        path = self._rollout(record)
        _old_writer_row(self.con, _user("other input", ordinal=1), self.source_id)
        self._make_stale()
        before = self._snapshot("user_messages")
        result = codex.sync(self.con, source=path)
        self.assertEqual(result["identity_conflicts"], 1)
        self._resync(path, conflicts=1)
        self.assertEqual(self._snapshot("user_messages"), before)

    def _conflict_case(self, legacy_evidence, current_evidence):
        record = _user("synthetic input", ordinal=10)
        path = self._rollout(record, USAGE)
        _old_writer_row(self.con, record, self.source_id)
        self._task("parser", "duplicate-capture")
        for key, rows in (("codex:ordinal:10", legacy_evidence),
                          (self._current(1), current_evidence)):
            for sql, args in rows:
                self.con.execute(sql, (key,) + args)
        self._make_stale()
        return path

    ASSIGN = ("INSERT INTO assignments(user_message_id,task_id,phase,evidence,shared,created_at)"
              " VALUES(?,?,?,?,0,?)")
    DELEGATE = ("INSERT INTO delegations(owning_user_message,child_thread,parent_turn,reason,"
                "created_at) VALUES(?,?,?,?,?)")
    VERDICT = ("INSERT INTO verdicts(intervention_id,behavior,judge,verdict,problem,group_id,"
               "recorded_at) VALUES('sub:'||?,'correction','luke',?,?,?,?)")

    def test_conflicting_ownership_and_verdict_keep_all_evidence(self):
        path = self._conflict_case(
            [(self.ASSIGN, ("parser", "build", "legacy evidence", 300)),
             (self.DELEGATE, ("child-review", "codex:turn-1", "legacy reason", 300)),
             (self.VERDICT, ("no", "not a correction", "parser-bug", 300))],
            [(self.ASSIGN, ("duplicate-capture", None, None, 100)),
             (self.DELEGATE, ("child-review", None, None, 100)),
             (self.VERDICT, ("yes", "stale yes", None, 100))])
        tables = ("user_messages", "assignments", "delegations", "verdicts")
        before = {table: self._snapshot(table) for table in tables}
        buckets = self._buckets("parser", "duplicate-capture")
        self._resync(path, conflicts=1)
        self.assertEqual({table: self._snapshot(table) for table in tables}, before)
        after = self._buckets("parser", "duplicate-capture")
        self.assertEqual(after, buckets)
        # Attributed plus shared spend across both tasks never exceeds the turn.
        self.assertLessEqual(sum(b[0][1] + b[1][1] for b in after.values()), 100)
        self.assertEqual(cli._resolve_submission(self.con, "codex:ordinal:10"),
                         "codex:ordinal:10")

    def test_disagreeing_verdict_on_one_key_is_a_conflict(self):
        path = self._conflict_case([(self.VERDICT, ("no", "newer", None, 300))],
                                   [(self.VERDICT, ("yes", "older", None, 100))])
        before = self._snapshot("verdicts")
        self._resync(path, conflicts=1)
        self.assertEqual(self._snapshot("verdicts"), before)
        self.assertEqual(len(self.query("SELECT * FROM user_messages")), 2)

    def test_one_sided_evidence_moves_intact_and_counts_once(self):
        path = self._conflict_case(
            [(self.ASSIGN, ("parser", "build", "legacy evidence", 300)),
             (self.DELEGATE, ("child-review", "codex:turn-1", "legacy reason", 300)),
             (self.VERDICT, ("no", "not a correction", "parser-bug", 300))],
            [(self.VERDICT, ("no", "not a correction", "parser-bug", 300))])
        key = self._current(1)
        moved = {table: [tuple(key if v == "codex:ordinal:10" else
                               f"sub:{key}" if v == "sub:codex:ordinal:10" else v for v in row)
                         for row in self._snapshot(table)]
                 for table in ("assignments", "delegations")}
        self._assert_merged(path, "codex:ordinal:10", key)
        for table, rows in moved.items():
            self.assertEqual(self._snapshot(table), rows)
        self.assertEqual([tuple(r) for r in self.query(
            "SELECT intervention_id, verdict, problem, group_id, recorded_at FROM verdicts")],
            [(f"sub:{key}", "no", "not a correction", "parser-bug", 300)])
        buckets = self._buckets("parser", "duplicate-capture")
        self.assertEqual(buckets["parser"][0], (1, 100))
        self.assertEqual(sum(b[0][1] + b[1][1] for b in buckets.values()), 100)

    def test_existing_alias_keeps_the_legacy_id_resolvable(self):
        record = _user("synthetic input", ordinal=10)
        path = self._rollout(record)
        _old_writer_row(self.con, record, self.source_id)
        self.con.execute("UPDATE user_messages SET alias_id='codex:ordinal:earlier'"
                         " WHERE native_id=?", (self._current(1),))
        self._make_stale()
        self._resync(path, conflicts=1)
        self.assertEqual(cli._resolve_submission(self.con, "codex:ordinal:10"),
                         "codex:ordinal:10")
        self.assertEqual(cli._resolve_submission(self.con, "codex:ordinal:earlier"),
                         self._current(1))
