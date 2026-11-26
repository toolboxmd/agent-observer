"""A Codex rollout an older parser imported keeps its task after Codex archives it (#152).

Before atomic observations (context version 8) an import left each response
owned by its source row and recorded no observation to compare. Codex later
moves the finished rollout to ``archived_sessions`` with the same inode and
bytes. An ordinary sync must then hand those responses to the archived source,
but only when the recorded bytes and inode identify the moved file. A
different file with the same name keeps the responses conflicting.
"""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_observer import db, lookup, report
from agent_observer.adapters import codex, codex_atomic, t3_purpose
from tests.helpers import LedgerCase, fixture
from tests import test_codex_fork as fork

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

    def atom(self, response_id, path=None):
        return next(json.loads(line) for line in (path or self.original).read_text().splitlines()
                    if f'"{response_id}"' in line and '"token_usage_record"' in line)

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

    def test_two_candidates_fail_closed(self):
        old = self.downgrade(self.original)
        columns = [c[1] for c in self.con.execute("PRAGMA table_info(sources)") if c[1] not in ("id", "path")]
        other = str(self.live / "2026" / "09" / "11" / NAME)
        self.con.execute(f"INSERT INTO sources(path,{','.join(columns)}) SELECT ?,{','.join(columns)} "
                         "FROM sources WHERE id=?", (other, old))
        self.con.commit()
        self.original.rename(self.archived)
        codex.sync(self.con)
        self.assert_conflicting()
        self.assert_second_sync_is_a_no_op()

    def test_identical_copy_with_another_inode_stays_conflicting(self):
        self.downgrade(self.original)
        shutil.copyfile(self.original, self.archived)
        self.original.unlink()
        codex.sync(self.con)
        self.assert_conflicting()
        self.assert_second_sync_is_a_no_op()

    def test_hard_link_while_the_old_path_exists_moves_nothing(self):
        self.downgrade(self.original)
        os.link(self.original, self.archived)
        codex.sync(self.con)
        self.assertEqual(set(self.owners().values()), {str(self.original)})

    def test_full_resync_unarchive_and_append_after_adoption(self):
        self.downgrade(self.original)
        self.original.rename(self.archived)
        codex.sync(self.con)
        self.assert_attributed()
        codex.sync(self.con, full=True)
        self.assert_attributed()
        self.assert_second_sync_is_a_no_op()
        self.archived.rename(self.original)
        codex.sync(self.con)
        self.assertEqual(self.native(), (TURN_RESPONSES, [], ["reconciled"]))
        self.assert_second_sync_is_a_no_op()
        self.original.rename(self.archived)
        atom = self.atom("resp-mini-003", self.archived)
        atom["payload"]["response_id"] = "resp-mini-004"
        with self.archived.open("a") as stream:
            stream.write(json.dumps(atom) + "\n")
        codex.sync(self.con)
        self.assertEqual(self.native()[1], [])
        self.assert_second_sync_is_a_no_op()

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


# Fields that say where a source can be read, or when something ran.
IGNORED = {"source_availability", "snapshot_id", "source_cutoff", "imported_at", "import_ms", "created_at",
           "updated_at"}
TASK = "example/repo#152"


def lines(rows):
    return "".join(json.dumps(dict(row, ordinal=i, timestamp=f"2026-09-01T10:{i:02d}:00.000Z")) + "\n"
                   for i, row in enumerate(rows))


FORK_PARENT = "2026/09/01/rollout-2026-09-01T10-00-00-thr-parent.jsonl"
FORK_CHILD = "2026/09/01/rollout-2026-09-01T10-20-00-thr-child.jsonl"


def fork_files():
    parent = [fork._meta(fork.PARENT, thread_source="user")] + fork._parent_history()
    child = ([fork._meta(fork.CHILD, thread_source="subagent", forked_from_id=fork.PARENT,
                         parent_thread_id=fork.PARENT, subagent_history_start_ordinal=1 + len(parent))]
             + parent + fork._child_own())
    return {FORK_PARENT: lines(parent), FORK_CHILD: lines(child)}


def source_columns(con, table):
    """Columns that name a source row: declared references and source_id."""
    refs = {fk[3] for fk in con.execute(f'PRAGMA foreign_key_list("{table}")') if fk[2] == "sources"}
    return refs | {c[1] for c in con.execute(f'PRAGMA table_info("{table}")') if c[1] == "source_id"}


class Ledger:
    """One ledger and its Codex homes, built through the real codex.sync."""

    def __init__(self, files, moved):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for rel, text in files.items():
            target = self.root / "sessions" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        (self.root / "archived_sessions").mkdir()
        self.original = self.root / "sessions" / moved
        self.archived = self.root / "archived_sessions" / self.original.name
        self.con = db.connect(str(self.root / "observer.db"))
        db.init_db(self.con)

    def close(self):
        self.con.close()
        self.tmp.cleanup()

    @contextmanager
    def homes(self):
        with patch.object(codex, "DEFAULT_ROOT", str(self.root / "sessions")), \
                patch.object(codex, "ARCHIVE_ROOT", str(self.root / "archived_sessions")), \
                patch.dict(os.environ, {"T3CODE_HOME": self.tmp.name}):
            yield

    def sync(self, *, adopt=True):
        with self.homes():
            if adopt:
                return codex.sync(self.con)
            # The released behavior: no older source is taken over.
            with patch.object(codex_atomic, "_moved_before_atoms", lambda *args, **kwargs: []):
                return codex.sync(self.con)

    def purpose(self):
        with closing(sqlite3.connect(":memory:")) as native:
            t3_purpose.refresh(self.con, native, set())
        self.con.execute("INSERT INTO tasks(task_id,project,title,created_at) VALUES(?,?,?,0)",
                         (TASK, "example/repo", TASK))
        self.con.execute("INSERT INTO t3_purpose_runs(run_id,thread_id,task_id,scope,kind,status,active_attempt_id) "
                         "VALUES('a','planner',?,'single','issue','completed','aa')", (TASK,))
        self.con.execute("INSERT INTO t3_purpose_attempts(attempt_id,run_id,provider_turn_id) VALUES('aa','a','ta')")
        self.con.execute("INSERT INTO t3_purpose_turns(provider_turn_id,attempt_id,run_id,status,usage_json) "
                         "VALUES('ta','aa','a','completed',?)",
                         (json.dumps(dict(usageStatus="complete", usageScope="main_agent",
                                          inputTokens=3000, outputTokens=350, hasSubagents=False)),))
        for key, in self.con.execute("SELECT session_key FROM sessions ORDER BY 1").fetchall():
            self.con.execute("INSERT INTO t3_purpose_sessions VALUES('a',?)", (key,))
        for key, turn in self.con.execute("SELECT session_key,turn_id FROM turns ORDER BY 2").fetchall():
            native_id = turn.split(":", 1)[1]
            self.con.execute("INSERT OR IGNORE INTO t3_purpose_inputs(run_id,session_key,native_id,kind,validation,"
                             "attempt_id,provider_turn_id) VALUES('a',?,?,'turn','codex_turn_started','aa',?)",
                             (key, native_id, "ta" if native_id == "turn-mini-aaa" else None))
        self.con.commit()

    def source(self, path=None):
        return self.con.execute("SELECT id FROM sources WHERE path=?", (str(path or self.original),)).fetchone()[0]

    def downgrade(self, version=5):
        """Leave the durable state an older context-version import left."""
        source = self.source()
        self.con.execute("DELETE FROM codex_atomic_observations WHERE source_id=?", (source,))
        if version < 7:
            self.con.execute("DELETE FROM native_response_inputs WHERE source_id=?", (source,))
        self.con.execute("UPDATE sources SET codex_context_version=? WHERE id=?", (version, source))
        return source

    def stored(self, rid, **values):
        self.con.execute("UPDATE responses SET " + ",".join(f"{k}=?" for k in values) + " WHERE response_id=?",
                         (*values.values(), rid))

    def append(self, text, path=None):
        with (path or self.original).open("a") as stream:
            stream.write(text)

    def atom(self, rid, path=None):
        return next(json.loads(row) for row in (path or self.original).read_text().splitlines()
                    if f'"{rid}"' in row and '"token_usage_record"' in row)

    def snapshot(self, released=False, where=None):
        """Every table, with source ids and moved paths written as what they stand for.

        ``released`` compares a ledger a released sync touched first. Its
        separate archive import used AUTOINCREMENT ids that are never reused,
        so ``events.id`` and every column naming an event (``event_checkouts.
        event_id``, ``codex_occurrences.row_key`` of an event) are written as
        the event's natural key, and ``import_errors.id`` is left out. Every
        other column is compared as stored.
        """
        sources = {row["id"]: row["path"] for row in self.con.execute("SELECT id,path FROM sources")}
        events = {row["id"]: f"event {row['session_key']} {row['family']} {row['native_id']}"
                  for row in self.con.execute("SELECT id,session_key,family,native_id FROM events")}
        names = {str(self.original): "<file>", str(self.archived): "<file>", self.tmp.name: "<root>"}

        def plain(value):
            text = json.dumps(value, sort_keys=True, default=str)
            for path, name in names.items():
                text = text.replace(path, name)
            return json.loads(text)

        def surrogate(table, key, row):
            """The natural key for a surrogate event id, or None to keep the value."""
            if (table, key) in (("events", "id"), ("event_checkouts", "event_id")):
                return events.get(row[key], f"<dangling event {row[key]}>")
            if table == "codex_occurrences" and key == "row_key" and row["row_kind"] == "event":
                return events.get(int(row[key]), f"<dangling event {row[key]}>")
            return None

        # Each ledger has its own copies of the files: a source's inode and
        # mtime are compared by whether they match the file at its path.
        stats = {}
        for row in self.con.execute("SELECT id,path FROM sources"):
            try:
                stats[row["id"]] = os.stat(row["path"])
            except OSError:
                pass
        tables = {}
        for table, in self.con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                       "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            refs = source_columns(self.con, table)
            rows = []
            if where is not None and table not in where:
                continue
            condition, params = where[table] if where is not None else ("1", ())
            for row in self.con.execute(f'SELECT * FROM "{table}" WHERE {condition}', params):
                item = {}
                for key in row.keys():
                    if key in IGNORED or (table == "sources" and key == "id"):
                        continue  # Source ids are compared by path.
                    if released and table == "import_errors" and key == "id":
                        continue
                    value = row[key]
                    natural = surrogate(table, key, row) if released else None
                    if natural is not None:
                        value = natural
                    elif key in refs and value is not None:
                        value = sources.get(value, f"<dangling {value}>")
                    elif table == "sources" and key in ("ino", "mtime_ns") and row["id"] in stats:
                        current = getattr(stats[row["id"]], "st_" + key)
                        value = "<its file>" if value == current else "<not its file>"
                    item[key] = value
                rows.append(item)
            tables[table] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str))
        return plain(tables)

    def view(self, released=False):
        def kept(value):
            if isinstance(value, dict):
                return {k: kept(v) for k, v in value.items() if k not in IGNORED}
            return [kept(v) for v in value] if isinstance(value, list) else value

        ids = [r[0] for r in self.con.execute("SELECT id FROM events ORDER BY id")]
        natural = {row["id"]: f"event {row['session_key']} {row['family']} {row['native_id']}"
                   for row in self.con.execute("SELECT id,session_key,family,native_id FROM events")}
        shown = []
        for item in lookup.show(self.con, ids):
            item = {k: v for k, v in item.items() if k != "read_path"}
            if released:
                item["id"] = natural[item["id"]]
            shown.append(item)
        if released:
            shown.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
        view = self.snapshot(released)
        view["task_report"] = kept(json.loads(json.dumps(report.task_report(self.con, TASK), default=str)))
        view["lookup_show"] = shown
        return json.loads(json.dumps(view).replace(str(self.original), "<file>")
                          .replace(str(self.archived), "<file>").replace(self.tmp.name, "<root>"))

    def views(self):
        return {False: self.view(False), True: self.view(True)}

    def sync_signature(self):
        """What one ordinary sync did: its writes and each source's import."""
        seen = []
        real = codex.import_codex_file

        def record(*args, **kwargs):
            seen.append(real(*args, **kwargs))
            return seen[-1]

        changes = self.con.total_changes
        with self.homes(), patch.object(codex, "import_codex_file", side_effect=record):
            codex.sync(self.con)
        return (self.con.total_changes - changes,
                sorted((s["unchanged"], s["incremental"], s["lines"], s["malformed"]) for s in seen))

    def before_replay(self):
        """The ledger just before the moved file is read, taken inside a real sync."""
        taken = []
        real = codex.JsonlSource

        def stop(con, harness, path, *args, **kwargs):
            if os.path.basename(path) == self.original.name:
                taken.append({False: self.snapshot(False), True: self.snapshot(True),
                              "owned": self.snapshot(True, where=self.owned_rows(path))})
                raise OSError("stopped before the replay")
            return real(con, harness, path, *args, **kwargs)

        with self.homes(), patch.object(codex, "JsonlSource", side_effect=stop):
            codex.sync(self.con)
        return taken[0]

    def owned_rows(self, path):
        """Per table, the rows the source at ``path`` owns directly or through a
        chain of foreign-key parents, as SQL conditions."""
        source = self.con.execute("SELECT id FROM sources WHERE path=?", (str(path),)).fetchone()
        if source is None:
            return {}
        tables = [t for t, in self.con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                               "AND name NOT LIKE 'sqlite_%'")]
        owned = {}
        for table in tables:
            refs = sorted(source_columns(self.con, table))
            if refs:
                owned[table] = ("(" + " OR ".join(f'"{c}"=?' for c in refs) + ")", (source[0],) * len(refs))
        for _ in range(len(tables)):
            grown = dict(owned)
            for table in tables:
                links = {}
                for fk in self.con.execute(f'PRAGMA foreign_key_list("{table}")'):
                    links.setdefault((fk[0], fk[2]), []).append((fk[3], fk[4]))
                for (_, parent), pairs in links.items():
                    if parent == "sources" or parent not in owned:
                        continue
                    targets = [to or c[1] for (_, to), c in zip(pairs, [
                        c for c in self.con.execute(f'PRAGMA table_info("{parent}")') if c[5]] or [(0, "rowid")])]
                    clause = (f'({",".join(chr(34) + c + chr(34) for c, _ in pairs)}) IN '
                              f'(SELECT {",".join(chr(34) + t + chr(34) for t in targets)} FROM "{parent}" '
                              f'WHERE {owned[parent][0]})')
                    mine = grown.get(table, ("0", ()))
                    if clause not in mine[0]:
                        grown[table] = (f"({mine[0]} OR {clause})", mine[1] + owned[parent][1])
            if grown == owned:
                break
            owned = grown
        return owned

    def kinds(self):
        """For each table: its source columns, or "session" for a session-keyed table without one."""
        result = {}
        for table, in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            refs = source_columns(self.con, table)
            columns = {c[1] for c in self.con.execute(f'PRAGMA table_info("{table}")')}
            result[table] = refs or ("session" if "session_key" in columns else set())
        return result


ORDERS = ("in place", "moved", "moved after a released sync")


class MoveEqualsInPlaceReplayTest(unittest.TestCase):
    """For the same bytes and older-parser state, a verified move leaves exactly
    what an in-place replay leaves: every table (source path normalized), the
    task report apart from availability, and lookup.show. Before the replay,
    the ledger already equals the in-place ledger. A row from before context
    versions is the one documented exception."""

    def ledger(self, files, order, arrange):
        built = Ledger(files, self.moved)
        self.addCleanup(built.close)
        built.sync()
        built.purpose()
        arrange(built)
        built.con.commit()
        if order != "in place":
            built.original.rename(built.archived)
        built.folded_events = set()
        if order == "moved after a released sync":
            built.sync(adopt=False)
            built.folded_events = {f"event {r[0]} {r[1]} {r[2]}" for r in built.con.execute(
                "SELECT e.session_key,e.family,e.native_id FROM events e JOIN sources s ON s.id=e.source_id "
                "WHERE s.path=?", (str(built.archived),))}
        return built

    def assert_pair(self, arrange=lambda l: l.downgrade(), files=None, later=None, tail_only=False):
        files = files or {f"2026/09/10/{NAME}": Path(fixture("codex-mini.jsonl")).read_text()}
        # The model itself: just before the re-read, the moved ledger is the
        # in-place ledger with the path changed.
        before = {}
        for order in ORDERS:
            built = self.ledger(files, order, arrange)
            before[order] = built.before_replay()
            kinds = built.kinds()
        for order in ORDERS:
            for released in (False, True):
                self.assertNotIn("<dangling", json.dumps(before[order][released]), order)
        owned = before[ORDERS[2]]["owned"]
        with self.subTest(order=ORDERS[1], step="before replay"):
            self.assertEqual(before[ORDERS[1]][False], before["in place"][False])
        with self.subTest(order=ORDERS[2], step="before replay"):
            self.assert_only_folded(before[ORDERS[2]][True], before["in place"][True], kinds, owned)

        steps = {order: [] for order in ORDERS}
        work = {order: [] for order in ORDERS}
        folded = {}
        for order in ORDERS:
            built = self.ledger(files, order, arrange)
            folded[order] = built.folded_events
            built.sync()
            steps[order].append(built.views())
            if later is not None:
                for _ in range(2):
                    work[order].append(built.sync_signature())
                if tail_only:
                    self.assertTrue(all(u or i for _, s in work[order] for u, i, *_ in s), (order, work[order]))
                steps[order].append(built.views())
                later(built, built.original if order == "in place" else built.archived)
                built.sync()
                steps[order].append(built.views())
            for _ in range(2):
                writes, imports = built.sync_signature()
                self.assertEqual(writes, 0, order)
                self.assertTrue(all(unchanged for unchanged, *_ in imports), (order, imports))
            changes = built.con.total_changes
            report.task_report(built.con, TASK)
            self.assertEqual(built.con.total_changes, changes, order)
            steps[order].append(built.views())
            for view in steps[order]:
                self.assertNotIn("<dangling", json.dumps(view), order)
        self.kept_checkouts = 0
        for order in ORDERS[1:]:
            released = order == ORDERS[2]
            self.assertEqual(work[order], work["in place"], order)
            for step, (actual, expected) in enumerate(zip(steps[order], steps["in place"])):
                actual = actual[released]
                if released and "event_checkouts" in actual:
                    actual = dict(actual, event_checkouts=self.without_folded_checkouts(
                        actual.get("event_checkouts", []), expected[released].get("event_checkouts", []),
                        folded[order]))
                with self.subTest(order=order, step=step):
                    self.assertEqual(actual, expected[released])
        return steps["moved"][-1][False]

    def without_folded_checkouts(self, have, want, folded):
        """Checkout diagnostics a separate archive import captured for events
        the older import lacked stay: the replay cannot capture them again and
        they cannot grant ownership. Only those rows are accepted."""
        kept = [row for row in have if row not in want and row["event_id"] in folded]
        self.kept_checkouts = max(self.kept_checkouts, len(kept))
        return [row for row in have if row not in kept]

    moved = f"2026/09/10/{NAME}"

    def assert_only_folded(self, actual, expected, kinds, owned):
        """Before the replay of a ledger a released sync touched first, the only
        rows beyond the in-place ledger are rows its separate archive import
        wrote that either belong to the adopted source, directly or through a
        chain of foreign-key parents (user assignments, delegations and
        intervention evidence may reference them), or sit in a session-keyed
        table without a source column. Nothing is missing."""
        for table in sorted(set(actual) | set(expected)):
            have, want = actual.get(table, []), expected.get(table, [])
            missing = [row for row in want if row not in have]
            extra = [row for row in have if row not in want
                     and not (kinds.get(table) == "session" or row in owned.get(table, []))]
            self.assertEqual((missing, extra), ([], []), table)

    def test_all_counters_agree(self):
        view = self.assert_pair()
        self.assertEqual(view["task_report"]["native_purpose"]["conflicting_responses"], [])

    def test_one_stored_counter_differs(self):
        def arrange(l):
            l.downgrade()
            l.stored("codex:resp-mini-001", output_tokens=109)
        self.assert_pair(arrange)

    def test_stored_model_differs(self):
        def arrange(l):
            l.downgrade()
            l.stored("codex:resp-mini-001", model="other-model")
        self.assert_pair(arrange)

    def test_stored_effort_differs(self):
        def arrange(l):
            l.downgrade()
            l.stored("codex:resp-mini-001", effort="low")
        self.assert_pair(arrange)

    def test_stored_turn_differs(self):
        def arrange(l):
            l.downgrade()
            l.stored("codex:resp-mini-001", turn_id="codex:turn-other")
        self.assert_pair(arrange)

    def test_response_the_file_no_longer_contains(self):
        def arrange(l):
            l.downgrade()
            columns = [r[1] for r in l.con.execute("PRAGMA table_info(responses)")]
            values = ",".join("'codex:resp-mini-gone'" if k == "response_id" else k for k in columns)
            l.con.execute(f"INSERT INTO responses({','.join(columns)}) SELECT {values} FROM responses "
                          "WHERE response_id='codex:resp-mini-001'")
        self.assert_pair(arrange)

    def test_extra_response_only_the_file_contains(self):
        def arrange(l):
            l.downgrade()
            l.con.execute("DELETE FROM responses WHERE response_id='codex:resp-mini-002'")
        self.assert_pair(arrange)

    def with_copy(self, extra=0):
        """The moved rollout and a second rollout holding one of its atoms."""
        text = Path(fixture("codex-mini.jsonl")).read_text()
        atom = next(json.loads(r) for r in text.splitlines() if '"resp-mini-001"' in r and "token_usage_record" in r)
        atom["payload"]["usage"]["output_tokens"] += extra
        return {self.moved: text,
                "2026/09/11/rollout-2026-09-11T10-00-00-copy.jsonl": text.splitlines()[0] + "\n" + json.dumps(atom) + "\n"}

    def test_conflicting_copy_in_a_second_rollout(self):
        self.assert_pair(files=self.with_copy(extra=7))

    def test_agreeing_copy_in_a_second_rollout(self):
        self.assert_pair(files=self.with_copy())

    def test_agreeing_copy_in_a_second_rollout_and_a_differing_stored_counter(self):
        def arrange(l):
            l.downgrade()
            l.stored("codex:resp-mini-001", output_tokens=109)
        self.assert_pair(arrange, files=self.with_copy())

    def usage_conflict(self, l):
        l.downgrade(7)
        l.con.execute("INSERT INTO import_errors(harness,source_path,ordinal_num,error,line_excerpt,created_at) "
                      "VALUES('codex',?,30,'usage_conflict','',0)", (str(l.original),))

    def test_version_7_predecessor(self):
        self.assert_pair(lambda l: l.downgrade(7))

    def test_version_7_predecessor_with_usage_conflict_history(self):
        self.assert_pair(self.usage_conflict)

    def test_version_7_usage_conflict_with_a_torn_tail(self):
        line = next(r for r in Path(fixture("codex-mini.jsonl")).read_text().splitlines()
                    if '"turn_context"' in r) + "\n"

        def arrange(l):
            self.usage_conflict(l)
            l.append(line[:20])
        # In place this legacy receipt replays the whole file until the line completes.
        self.assert_pair(arrange, later=lambda l, path: l.append(line[20:], path))

    def test_stored_thread_identity_differs_from_the_file(self):
        # Before #69 an import could store a copied parent thread on the row.
        def arrange(l):
            l.con.execute("UPDATE sources SET thread_id='thread-copied-parent' WHERE id=?", (l.downgrade(),))
        self.assert_pair(arrange)

    def test_version_7_usage_conflict_with_an_unreadable_record(self):
        def arrange(l):
            self.usage_conflict(l)
            atom = l.atom("resp-mini-003")
            del atom["payload"]["response_id"]
            l.append(json.dumps(atom) + "\n")
        self.assert_pair(arrange)

    def test_conflicting_copies_inside_the_file(self):
        def arrange(l):
            atom = l.atom("resp-mini-003")
            atom["payload"]["usage"]["output_tokens"] += 7
            l.append(json.dumps(atom) + "\n")
            l.sync()
            l.downgrade()
        view = self.assert_pair(arrange)
        self.assertEqual(view["task_report"]["native_purpose"]["conflicting_responses"], ["codex:resp-mini-003"])

    def test_torn_last_line_before_and_after_it_completes(self):
        line = next(r for r in Path(fixture("codex-mini.jsonl")).read_text().splitlines()
                    if '"turn_context"' in r) + "\n"

        def arrange(l):
            l.downgrade()
            l.append(line[:20])
        self.assert_pair(arrange, later=lambda l, path: l.append(line[20:], path), tail_only=True)

    def test_unreadable_usage_record(self):
        def arrange(l):
            l.downgrade()
            atom = l.atom("resp-mini-003")
            del atom["payload"]["response_id"]
            l.append(json.dumps(atom) + "\n")
        self.assert_pair(arrange)

    def test_resume_after_archive(self):
        def later(l, path):
            atom = l.atom("resp-mini-003", path)
            atom["payload"]["response_id"] = "resp-mini-004"
            l.append(json.dumps(atom) + "\n", path)
        self.assert_pair(later=later)

    def test_events_the_older_import_lacked(self):
        def arrange(l):
            l.downgrade()
            for event, in l.con.execute("SELECT id FROM events ORDER BY id DESC LIMIT 3").fetchall():
                l.con.execute("DELETE FROM event_checkouts WHERE event_id=?", (event,))
                l.con.execute("DELETE FROM codex_occurrences WHERE row_kind='event' AND row_key=?", (str(event),))
                l.con.execute("DELETE FROM events WHERE id=?", (event,))
        self.assert_pair(arrange)
        # The released import captured one checkout the replay cannot: exception 4.
        self.assertEqual(self.kept_checkouts, 1)

    def test_user_message_the_older_import_lacked(self):
        def arrange(l):
            l.downgrade()
            message = "codex:msg-mini-sub-02"
            l.con.execute("DELETE FROM intervention_prompt_evidence WHERE native_id=?", (message,))
            l.con.execute("DELETE FROM codex_occurrences WHERE row_kind='user' AND row_key=?", (message,))
            l.con.execute("DELETE FROM user_messages WHERE native_id=?", (message,))
        self.assert_pair(arrange)

    def test_fallback_only_session(self):
        self.moved = "2026/09/12/rollout-2026-09-12T10-00-00-legacy.jsonl"
        self.assert_pair(files={self.moved: Path(fixture("codex-legacy-a.jsonl")).read_text()})

    def test_fork_child_keyed_to_its_parent_before_69(self):
        self.moved = FORK_CHILD

        def arrange(l):
            # The 1.0.1 state: every row of the child sits under the parent key.
            child = l.source()
            old, new = f"codex:{fork.PARENT}", f"codex:{fork.CHILD}"
            for table in ("events", "responses", "turns", "user_messages"):
                l.con.execute(f"UPDATE {table} SET session_key=? WHERE session_key=? AND source_id=?",
                              (old, new, child))
            l.con.execute("UPDATE responses SET response_id=?||substr(response_id,?) WHERE response_id LIKE ?",
                          (old, len(new) + 1, new + ":tc:%"))
            l.con.execute("UPDATE codex_fallback_cumulative SET session_key=? WHERE session_key=?", (old, new))
            l.con.execute("UPDATE sessions SET source_id=? WHERE session_key=?", (child, old))
            l.con.execute("DELETE FROM t3_purpose_sessions WHERE session_key=?", (new,))
            l.con.execute("DELETE FROM sessions WHERE session_key=?", (new,))
            l.con.execute("UPDATE sources SET thread_id=? WHERE id=?", (fork.PARENT, child))
            l.downgrade()
        self.assert_pair(arrange, files=fork_files())


class FoldCoversEveryTableTest(unittest.TestCase):
    def test_a_table_added_later_is_folded_with_the_rest(self):
        built = Ledger({f"2026/09/10/{NAME}": Path(fixture("codex-mini.jsonl")).read_text()}, f"2026/09/10/{NAME}")
        self.addCleanup(built.close)
        built.sync()
        built.downgrade()
        built.con.execute("CREATE TABLE later_rows (id INTEGER PRIMARY KEY, source_id INTEGER REFERENCES sources(id))")
        built.con.execute("CREATE TABLE later_snapshots (source_id INTEGER NOT NULL REFERENCES sources(id), "
                          "item TEXT NOT NULL, PRIMARY KEY(source_id,item))")
        built.con.commit()
        built.original.rename(built.archived)
        built.sync(adopt=False)
        archive = built.source(built.archived)
        built.con.execute("INSERT INTO later_rows(source_id) VALUES(?)", (archive,))
        built.con.execute("INSERT INTO later_snapshots VALUES(?, 'archive-only')", (archive,))
        built.con.commit()
        built.sync()
        kept = built.source(built.archived)
        self.assertNotEqual(kept, archive)
        self.assertEqual(list(map(tuple, built.con.execute("SELECT source_id FROM later_rows"))), [(kept,)])
        self.assertEqual(built.con.execute("SELECT COUNT(*) FROM later_snapshots").fetchone()[0], 0)


class FoldGuardTest(unittest.TestCase):
    def released(self):
        built = Ledger({f"2026/09/10/{NAME}": Path(fixture("codex-mini.jsonl")).read_text()}, f"2026/09/10/{NAME}")
        self.addCleanup(built.close)
        built.sync()
        built.downgrade()
        built.con.commit()
        return built

    def test_a_keyless_table_naming_a_source_stops_the_fold(self):
        built = self.released()
        built.con.execute("CREATE TABLE later_log (source_id INTEGER REFERENCES sources(id), note TEXT)")
        built.con.commit()
        built.original.rename(built.archived)
        built.sync(adopt=False)
        built.con.execute("INSERT INTO later_log VALUES(?, 'written by the released import')",
                          (built.source(built.archived),))
        built.con.commit()
        before = list(built.con.iterdump())
        totals = built.sync()
        self.assertEqual([(f["path"], f["error"]) for f in totals["failed"]],
                         [(str(built.archived), "cannot fold later_log.source_id: the table has no primary or unique key")])
        self.assertEqual(list(built.con.iterdump()), before)

    def test_only_codex_import_errors_follow_the_path(self):
        built = self.released()
        built.con.execute("CREATE TABLE other_adapter (id INTEGER PRIMARY KEY, source_path TEXT)")
        for path in (built.original, built.archived):
            built.con.execute("INSERT INTO other_adapter(source_path) VALUES(?)", (str(path),))
            built.con.execute("INSERT INTO import_errors(harness,source_path,ordinal_num,error,line_excerpt,created_at)"
                              " VALUES('claude',?,1,'other harness','',0)", (str(path),))
        built.con.commit()
        untouched = ("SELECT source_path FROM other_adapter UNION ALL "
                     "SELECT source_path FROM import_errors WHERE harness='claude' ORDER BY 1")
        expected = built.con.execute(untouched).fetchall()
        older = built.source()
        built.original.rename(built.archived)
        built.sync(adopt=False)
        built.sync()
        self.assertEqual(built.source(built.archived), older)  # The older row was adopted.
        self.assertEqual(built.con.execute(untouched).fetchall(), expected)


class AdoptionIsOneTransactionTest(unittest.TestCase):
    def check(self, released):
        built = Ledger({f"2026/09/10/{NAME}": Path(fixture("codex-mini.jsonl")).read_text()}, f"2026/09/10/{NAME}")
        self.addCleanup(built.close)
        built.sync()
        built.purpose()
        built.downgrade()
        built.con.commit()
        built.original.rename(built.archived)
        if released:
            built.sync(adopt=False)
        before = list(built.con.iterdump())
        adopted = []

        def fail(reader):
            # The older row already stands at the new path, alone, inside this transaction.
            adopted.append(built.con.execute("SELECT path,codex_context_version FROM sources WHERE path LIKE ?",
                                             (f"%{NAME}",)).fetchall())
            raise sqlite3.DatabaseError("injected after adoption")

        with patch.object(codex_atomic, "prepare", side_effect=fail):
            totals = built.sync()
        self.assertEqual([[tuple(r) for r in rows] for rows in adopted], [[(str(built.archived), 5)]])
        self.assertEqual([f["path"] for f in totals["failed"]], [str(built.archived)])
        self.assertEqual(list(built.con.iterdump()), before)
        built.sync()
        rep = report.task_report(built.con, TASK)["native_purpose"]
        self.assertEqual((rep["conflicting_responses"], [c["status"] for c in rep["turn_checks"]]), ([], ["reconciled"]))
        self.assertEqual(built.sync_signature()[0], 0)

    def test_failure_after_adoption_rolls_back_to_the_moved_state(self):
        self.check(False)

    def test_failure_after_folding_a_released_import_rolls_back(self):
        self.check(True)

