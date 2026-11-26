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

    def downgrade(self, path, version=5):
        """Leave the durable state of an older context-version import of ``path``."""
        source, = self.con.execute("SELECT id FROM sources WHERE path=?", (str(path),)).fetchone()
        self.con.execute("DELETE FROM codex_atomic_observations WHERE source_id=?", (source,))
        if version < 7:
            # Version 7 recorded literal turn inputs; earlier versions did not.
            self.con.execute("DELETE FROM native_response_inputs WHERE source_id=?", (source,))
        self.con.execute("UPDATE sources SET codex_context_version=? WHERE id=?", (version, source))
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


IGNORED = ("source_availability", "snapshot_id", "source_cutoff")


class MoveEqualsInPlaceReplayTest(LedgerCase):
    """For the same bytes and older-parser state, a verified move followed by an
    ordinary sync leaves exactly what an in-place replay leaves: the task report
    (apart from source availability), every responses column and every native
    response input. A row from before context versions is the one exception."""

    def replay(self, arrange, move, later, settled):
        """Every view one ledger passes through. Each ledger runs to the end
        before the next starts, because the Codex roots are patched globally."""
        case = PreAtomicArchiveTest("test_older_import_reconciles_after_its_rollout_is_archived")
        case.setUp()
        try:
            arrange(case)
            case.con.commit()
            if move:
                case.original.rename(case.archived)
            codex.sync(case.con)
            views = [self.view(case)]
            if later is not None:
                # Only the unfinished tail is read until the line completes.
                imports = case.sync_imports()
                self.assertTrue(all(stats["unchanged"] or stats["incremental"] for stats in imports), imports)
                views.append(self.view(case))
                later(case, case.archived if move else case.original)
                codex.sync(case.con)
                views.append(self.view(case))
            if settled:
                case.assert_second_sync_is_a_no_op()
                views.append(self.view(case))
            return views
        finally:
            case.tearDown()
            case.doCleanups()

    def view(self, case):
        paths = {str(case.original): "<file>", str(case.archived): "<file>", case.tmp.name: "<root>"}

        def plain(value):
            text = json.dumps(value, sort_keys=True, default=str)
            for path, name in paths.items():
                text = text.replace(path, name)
            return json.loads(text)

        def kept(value):
            if isinstance(value, dict):
                return {k: kept(v) for k, v in value.items() if k not in IGNORED}
            return [kept(v) for v in value] if isinstance(value, list) else value

        rep = kept(report.task_report(case.con, case.task))
        tables = {}
        for table, order in (("responses", "response_id"), ("native_response_inputs", "response_id,input_id")):
            rows = case.con.execute(f"SELECT t.*,s.path AS source_path FROM {table} t JOIN sources s "
                                    f"ON s.id=t.source_id ORDER BY s.path!=?,{order}", (str(case.archived),))
            tables[table] = sorted((sorted((k, row[k]) for k in row.keys() if k != "source_id") for row in rows),
                                   key=json.dumps)
        return plain({"report": rep, **tables})

    def assert_pair(self, arrange, later=None, settled=True):
        in_place, moved = (self.replay(arrange, move, later, settled) for move in (False, True))
        for step, (expected, actual) in enumerate(zip(in_place, moved)):
            with self.subTest(step=step):
                self.assertEqual(actual, expected)
        return moved[-1]

    @staticmethod
    def stored(case, rid, **values):
        case.con.execute("UPDATE responses SET " + ",".join(f"{k}=?" for k in values) + " WHERE response_id=?",
                         (*values.values(), rid))

    @staticmethod
    def append(path, *lines):
        with path.open("a") as stream:
            stream.write("".join(lines))

    def copy_elsewhere(self, case, extra=0):
        atom = case.atom("resp-mini-001")
        atom["payload"]["usage"]["output_tokens"] += extra
        other = case.live / "2026" / "09" / "11" / "rollout-2026-09-11T10-00-00-copy.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text(case.original.read_text().splitlines()[0] + "\n" + json.dumps(atom) + "\n")

    def test_all_counters_agree(self):
        view = self.assert_pair(lambda c: c.downgrade(c.original))
        self.assertEqual(view["report"]["native_purpose"]["conflicting_responses"], [])

    def test_one_stored_counter_differs(self):
        def arrange(c):
            c.downgrade(c.original)
            self.stored(c, "codex:resp-mini-001", output_tokens=109)
        self.assert_pair(arrange)

    def test_stored_model_differs(self):
        def arrange(c):
            c.downgrade(c.original)
            self.stored(c, "codex:resp-mini-001", model="other-model")
        self.assert_pair(arrange)

    def test_stored_effort_differs(self):
        def arrange(c):
            c.downgrade(c.original)
            self.stored(c, "codex:resp-mini-001", effort="low")
        self.assert_pair(arrange)

    def test_stored_turn_differs(self):
        def arrange(c):
            c.downgrade(c.original)
            self.stored(c, "codex:resp-mini-001", turn_id="codex:turn-other")
        self.assert_pair(arrange)

    def test_response_the_file_no_longer_contains(self):
        def arrange(c):
            c.downgrade(c.original)
            columns = [r[1] for r in c.con.execute("PRAGMA table_info(responses)")]
            values = ",".join("'codex:resp-mini-gone'" if k == "response_id" else k for k in columns)
            c.con.execute(f"INSERT INTO responses({','.join(columns)}) SELECT {values} FROM responses "
                          "WHERE response_id='codex:resp-mini-001'")
        self.assert_pair(arrange)

    def test_extra_response_only_the_file_contains(self):
        def arrange(c):
            c.downgrade(c.original)
            c.con.execute("DELETE FROM responses WHERE response_id='codex:resp-mini-002'")
        self.assert_pair(arrange)

    def test_conflicting_copy_in_a_second_rollout(self):
        def arrange(c):
            c.downgrade(c.original)
            self.copy_elsewhere(c, extra=7)
        self.assert_pair(arrange)

    def test_agreeing_copy_in_a_second_rollout(self):
        def arrange(c):
            c.downgrade(c.original)
            self.copy_elsewhere(c)
        self.assert_pair(arrange)

    def test_agreeing_copy_in_a_second_rollout_and_a_differing_stored_counter(self):
        def arrange(c):
            c.downgrade(c.original)
            self.copy_elsewhere(c)
            self.stored(c, "codex:resp-mini-001", output_tokens=109)
        self.assert_pair(arrange)

    def test_version_7_predecessor(self):
        self.assert_pair(lambda c: c.downgrade(c.original, 7))

    def test_version_7_predecessor_with_usage_conflict_history(self):
        def arrange(c):
            c.downgrade(c.original, 7)
            c.con.execute("INSERT INTO import_errors(harness,source_path,ordinal_num,error,line_excerpt,created_at) "
                          "VALUES('codex',?,30,'usage_conflict','',0)", (str(c.original),))
        self.assert_pair(arrange)

    def test_conflicting_copies_inside_the_file(self):
        def arrange(c):
            atom = c.atom("resp-mini-003")
            atom["payload"]["usage"]["output_tokens"] += 7
            self.append(c.original, json.dumps(atom) + "\n")
            codex.sync(c.con)
            c.downgrade(c.original)
        view = self.assert_pair(arrange)
        self.assertEqual(view["report"]["native_purpose"]["conflicting_responses"], ["codex:resp-mini-003"])

    def test_torn_last_line_before_and_after_it_completes(self):
        line = []

        def arrange(c):
            c.downgrade(c.original)
            line.append(next(r for r in c.original.read_text().splitlines() if '"turn_context"' in r) + "\n")
            self.append(c.original, line[0][:20])
        self.assert_pair(arrange, later=lambda c, path: self.append(path, line[0][20:]))

    def test_unreadable_usage_record(self):
        def arrange(c):
            c.downgrade(c.original)
            atom = c.atom("resp-mini-003")
            del atom["payload"]["response_id"]
            self.append(c.original, json.dumps(atom) + "\n")
        self.assert_pair(arrange)

