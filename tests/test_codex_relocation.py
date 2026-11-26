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
from agent_observer.adapters import codex, t3_purpose
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
