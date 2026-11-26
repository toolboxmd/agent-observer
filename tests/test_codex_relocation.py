"""A Codex rollout an older parser imported keeps its task after Codex archives it (#152).

Before atomic observations (context version 8) an import left each response
owned by its source row and recorded no observation to compare. Codex later
moves the finished rollout to ``archived_sessions`` with the same inode and
bytes. An ordinary sync must then hand those responses to the archived source,
but only when the recorded bytes and inode identify the moved file. A
different file with the same name keeps the responses conflicting.
"""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
from unittest.mock import patch

from agent_observer import report
from agent_observer.adapters import codex, codex_atomic, t3_purpose
from tests.helpers import LedgerCase, fixture

NAME = "rollout-2026-09-10T10-00-00-sess-fixture-mini-01.jsonl"
TURN_RESPONSES = ["codex:resp-mini-001", "codex:resp-mini-002"]
RESPONSES = TURN_RESPONSES + ["codex:resp-mini-003"]


class PreAtomicArchiveTest(LedgerCase):
    session = "codex:sess-fixture-mini-01"
    task = "example/repo#152"

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.live = root / "sessions"
        self.archive = root / "archived_sessions"
        self.original = self.live / "2026" / "09" / "10" / NAME
        self.archived = self.archive / NAME
        self.original.parent.mkdir(parents=True)
        self.archive.mkdir()
        shutil.copyfile(fixture("codex-mini.jsonl"), self.original)
        for active in (patch.object(codex, "DEFAULT_ROOT", str(self.live)),
                       patch.object(codex, "ARCHIVE_ROOT", str(self.archive)),
                       patch.dict(os.environ, {"T3CODE_HOME": self.tmp.name})):
            active.start()
            self.addCleanup(active.stop)
        codex.sync(self.con)
        with closing(sqlite3.connect(":memory:")) as native:
            t3_purpose.refresh(self.con, native, set())
        self.con.execute("INSERT INTO tasks(task_id,project,title,created_at) VALUES(?,?,?,0)",
                         (self.task, "example/repo", self.task))
        self.con.execute("INSERT INTO t3_purpose_runs(run_id,thread_id,task_id,scope,kind,status,"
                         "active_attempt_id) VALUES('a','planner',?,'single','issue','completed','aa')",
                         (self.task,))
        self.con.execute("INSERT INTO t3_purpose_sessions VALUES('a',?)", (self.session,))
        self.con.execute("INSERT INTO t3_purpose_inputs(run_id,session_key,native_id,kind,validation,"
                         "attempt_id,provider_turn_id) VALUES('a',?,'turn-mini-aaa','turn',"
                         "'codex_turn_started','aa','ta')", (self.session,))
        self.con.execute("INSERT INTO t3_purpose_attempts(attempt_id,run_id,provider_turn_id) VALUES('aa','a','ta')")
        self.con.execute("INSERT INTO t3_purpose_turns(provider_turn_id,attempt_id,run_id,status,usage_json) "
                         "VALUES('ta','aa','a','completed',?)",
                         (json.dumps(dict(usageStatus="complete", usageScope="main_agent",
                                          inputTokens=3000, outputTokens=350, hasSubagents=False)),))
        self.con.commit()

    def downgrade(self, path):
        """Leave the durable state of a context-version 5 import of ``path``."""
        source, = self.con.execute("SELECT id FROM sources WHERE path=?", (str(path),)).fetchone()
        self.con.execute("DELETE FROM codex_atomic_observations WHERE source_id=?", (source,))
        self.con.execute("DELETE FROM native_response_inputs WHERE source_id=?", (source,))
        self.con.execute("UPDATE sources SET codex_context_version=5 WHERE id=?", (source,))
        self.con.commit()
        return source

    def owners(self):
        return {rid: path for rid, path in self.con.execute(
            "SELECT r.response_id,s.path FROM responses r JOIN sources s ON s.id=r.source_id")}

    def native(self):
        rep = report.task_report(self.con, self.task)
        native = rep["native_purpose"]
        return (rep["measured"]["attributed_responses"], native["conflicting_responses"],
                [c["status"] for c in native["turn_checks"]])

    def assert_attributed(self):
        self.assertEqual(self.native(), (TURN_RESPONSES, [], ["reconciled"]))
        self.assertEqual(set(self.owners().values()), {str(self.archived)})

    def assert_conflicting(self):
        responses, conflicts, checks = self.native()
        self.assertEqual(responses, [])
        self.assertEqual(conflicts, RESPONSES)
        self.assertEqual(checks, ["no_joined_responses"])
        self.assertEqual(set(self.owners().values()), {str(self.original)})

    def assert_second_sync_is_a_no_op(self):
        tables = ("responses", "codex_atomic_observations", "native_response_inputs")
        before = {t: self.con.execute(f"SELECT * FROM {t} ORDER BY 1,2,3").fetchall() for t in tables}
        changes = self.con.total_changes
        totals = codex.sync(self.con)
        self.assertEqual(totals["unchanged"], totals["sources"])
        self.assertEqual(self.con.total_changes, changes)
        self.assertEqual({t: self.con.execute(f"SELECT * FROM {t} ORDER BY 1,2,3").fetchall()
                          for t in tables}, before)
        changes = self.con.total_changes
        self.native()
        self.assertEqual(self.con.total_changes, changes)

    def test_older_import_reconciles_after_its_rollout_is_archived(self):
        self.downgrade(self.original)
        self.assertEqual(self.native(), (TURN_RESPONSES, [], ["reconciled"]))
        self.original.rename(self.archived)
        codex.sync(self.con)
        self.assert_attributed()
        self.assert_second_sync_is_a_no_op()
        self.assert_attributed()

    def test_archive_indexed_before_the_repair_reconciles_on_the_next_sync(self):
        # A ledger a released version synced after the move: the archive has
        # current observations and the older row still owns the responses.
        self.original.rename(self.archived)
        codex.sync(self.con)
        self.downgrade(self.original)
        self.assertEqual(self.native()[1], RESPONSES)
        codex.sync(self.con)
        self.assert_attributed()
        self.assert_second_sync_is_a_no_op()

    def test_leftover_conflict_stays_conflicting_and_the_handoff_is_not_repeated(self):
        # Two copies of one response in the moved file disagree. The other
        # responses still move, and later syncs do not reread the rollout.
        atom = next(json.loads(line) for line in self.original.read_text().splitlines()
                    if '"resp-mini-003"' in line and '"token_usage_record"' in line)
        atom["payload"]["usage"]["output_tokens"] += 7
        with self.original.open("a") as stream:
            stream.write(json.dumps(atom) + "\n")
        codex.sync(self.con)
        self.downgrade(self.original)
        self.original.rename(self.archived)
        codex.sync(self.con)
        self.assertEqual(self.native(), (TURN_RESPONSES, ["codex:resp-mini-003"], ["reconciled"]))
        owners = self.owners()
        self.assertEqual(owners.pop("codex:resp-mini-003"), str(self.original))
        self.assertEqual(set(owners.values()), {str(self.archived)})
        self.assert_second_sync_is_a_no_op()
        self.assertEqual(self.native(), (TURN_RESPONSES, ["codex:resp-mini-003"], ["reconciled"]))

    def sync_imports(self):
        """Run an ordinary sync and return each source's import stats."""
        seen = []
        real = codex.import_codex_file

        def record(*args, **kwargs):
            seen.append(real(*args, **kwargs))
            return seen[-1]

        with patch.object(codex, "import_codex_file", side_effect=record):
            codex.sync(self.con)
        return seen

    def atom(self, response_id):
        return next(json.loads(line) for line in self.original.read_text().splitlines()
                    if f'"{response_id}"' in line and '"token_usage_record"' in line)

    def test_unreadable_usage_record_keeps_conflicts_without_repeating_the_replay(self):
        self.downgrade(self.original)
        atom = self.atom("resp-mini-003")
        del atom["payload"]["response_id"]
        self.original.rename(self.archived)
        with self.archived.open("a") as stream:
            stream.write(json.dumps(atom) + "\n")
        codex.sync(self.con)
        self.assert_conflicting()
        self.assert_second_sync_is_a_no_op()
        self.assert_conflicting()

    def test_torn_archive_reads_only_its_tail_until_the_line_completes(self):
        self.downgrade(self.original)
        self.original.rename(self.archived)
        line = next(row for row in self.archived.read_text().splitlines() if '"turn_context"' in row) + "\n"
        with self.archived.open("a") as stream:
            stream.write(line[:20])
        codex.sync(self.con)
        self.assert_conflicting()
        imports = self.sync_imports()
        self.assertTrue(imports)
        self.assertTrue(all(stats["unchanged"] or stats["incremental"] for stats in imports), imports)
        self.assert_conflicting()
        with self.archived.open("a") as stream:
            stream.write(line[20:])
        codex.sync(self.con)
        self.assert_attributed()
        self.assert_second_sync_is_a_no_op()

    def test_source_identity_that_differs_from_the_archive_is_handed_off_once(self):
        # Before #69 an import could store a copied parent thread on the row.
        source = self.downgrade(self.original)
        self.con.execute("UPDATE sources SET thread_id='thread-copied-parent' WHERE id=?", (source,))
        self.con.commit()
        self.original.rename(self.archived)
        codex.sync(self.con)
        self.assert_second_sync_is_a_no_op()
        self.assert_attributed()

    def test_row_from_before_context_versions_stays_conflicting(self):
        source = self.downgrade(self.original)
        self.con.execute("UPDATE sources SET codex_context_version=NULL,codex_context_offset=NULL WHERE id=?",
                         (source,))
        self.con.commit()
        self.original.rename(self.archived)
        codex.sync(self.con)
        self.assert_conflicting()
        self.assert_second_sync_is_a_no_op()
        self.assert_conflicting()

    def test_different_file_with_the_same_name_stays_conflicting(self):
        # The inode filter rejects this copy before any bytes are compared.
        self.downgrade(self.original)
        moved = self.original.read_bytes()
        self.archived.write_bytes(moved.replace(b'"redacted body"', b'"another body"'))
        self.original.unlink()
        codex.sync(self.con)
        self.assert_conflicting()

    def test_moved_inode_with_rewritten_bytes_stays_conflicting(self):
        # Older imports hashed only the 4 KiB before their read offset, so the
        # rewrite sits inside that window. An earlier rewrite would pass.
        self.downgrade(self.original)
        self.original.rename(self.archived)
        with self.archived.open("r+b") as stream:
            data = stream.read()
            stream.seek(data.index(b'"redacted body"'))
            stream.write(b'"redacted bodY"')
        codex.sync(self.con)
        self.assert_conflicting()

    # A verified move gives exactly the verdict an in-place replay gives.
    def stored_counters(self):
        names = ",".join(("response_id", "turn_id") + codex_atomic.COUNTERS)
        return self.con.execute(f"SELECT {names} FROM responses ORDER BY response_id").fetchall()

    def replay(self, move, third=False):
        # An older parser stored a counter the native bytes do not hold.
        self.downgrade(self.original)
        self.con.execute("UPDATE responses SET output_tokens=output_tokens+9 WHERE response_id='codex:resp-mini-001'")
        self.con.commit()
        if third:
            # Another rollout observes the same atom with the native counters.
            # Its name sorts before the archived rollout.
            other = self.archive / "rollout-2026-09-09T10-00-00-copy.jsonl"
            meta = self.original.read_text().splitlines()[0]
            other.write_text(meta + "\n" + json.dumps(self.atom("resp-mini-001")) + "\n")
        if move:
            self.original.rename(self.archived)
        codex.sync(self.con)
        owner = self.owners()["codex:resp-mini-001"]
        return self.native(), [tuple(r) for r in self.stored_counters()], owner

    def in_place(self, third=False):
        other = PreAtomicArchiveTest("test_differing_stored_counter_keeps_its_conflict")
        other.setUp()
        try:
            verdict = other.replay(False, third)
            other.assert_second_sync_is_a_no_op()
            return verdict, str(other.original)
        finally:
            other.tearDown()
            other.doCleanups()

    def test_differing_stored_counter_keeps_its_conflict(self):
        (expected, counters, owner), original = self.in_place()
        self.assertEqual(expected, (["codex:resp-mini-002"], ["codex:resp-mini-001"], ["counter_mismatch"]))
        self.assertEqual(owner, original)
        moved, moved_counters, moved_owner = self.replay(True)
        self.assertEqual((moved, moved_counters), (expected, counters))
        self.assertEqual(moved_owner, str(self.original))
        self.assert_second_sync_is_a_no_op()
        self.assertEqual((self.native(), [tuple(r) for r in self.stored_counters()]), (expected, counters))

    def test_agreeing_copy_elsewhere_does_not_take_the_moved_files_response(self):
        (expected, counters, owner), original = self.in_place(third=True)
        self.assertEqual(owner, original)
        moved, moved_counters, moved_owner = self.replay(True, third=True)
        self.assertEqual((moved, moved_counters), (expected, counters))
        self.assertEqual(moved_owner, str(self.archived))
        self.assert_second_sync_is_a_no_op()
