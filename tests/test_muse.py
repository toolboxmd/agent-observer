"""Muse native sessions (#127): import, reconciliation and reporting.

The fixture tree under tests/fixtures/muse mirrors real Muse 1.4.11 frame
shapes with invented ids and text. Expected values below are literal:
counts, statuses, UTF-8 sizes and identities, never the output of the
code under test.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_observer import db, lookup
from agent_observer.ingest import insert_event

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TREE = os.path.join(REPO, "tests", "fixtures", "muse", "sessions")
ROOT = "0a000000-0000-7000-8000-000000000001"
RUN = "0a000000-0000-7000-8000-0000000000a1"
C1 = "0a000000-0000-7000-8000-0000000000c1"
C2 = "0a000000-0000-7000-8000-0000000000c2"
ROOT_KEY = "muse:" + ROOT
DAY = os.path.join("2026", "10", "19")
ROOT_LOG = os.path.join(TREE, DAY, ROOT, "session.jsonl")
CHILD_LOG = os.path.join(TREE, DAY, ROOT, "subagent", C1, "session.jsonl")
MINI = os.path.join(REPO, "tests", "fixtures", "codex-mini.jsonl")
CLAUDE = os.path.join(REPO, "tests", "fixtures", "claude-shapes.jsonl")

# (family, native id) -> (name, target, status, size_bytes, tool_source,
# mcp_server, mcp_tool). Sizes are UTF-8 bytes of the committed text:
# "missing" 7, "café — résumé" 18 (13 characters), "über" 5,
# "skill body" 10, "ok" 2.
EXPECTED = {
    ("tool_call", "call_r1"): ("read_file", "/work/project/missing.md", None, None, "builtin", None, None),
    ("tool_call", "call_m1"): ("mcp__t3_code__web_read", None, None, None, "mcp", "t3-code", "web_read"),
    ("tool_call", "call_r2"): ("read_file", "/work/project/README.md", None, None, "builtin", None, None),
    ("tool_call", "call_s1"): ("read_skill", None, None, None, "builtin", None, None),
    ("tool_call", "call_u1"): ("mcp__ghost__tool", None, None, None, "unknown", None, None),
    ("tool_call", "call_b1"): ("mcp.lookalike", None, None, None, "builtin", None, None),
    ("tool_call", "call_p1"): ("bash", "ls -la", None, None, "builtin", None, None),
    ("tool_result", "call_r1"): ("read_file", None, "error", 7, "builtin", None, None),
    ("tool_result", "call_m1"): ("mcp__t3_code__web_read", None, "ok", 18, "mcp", "t3-code", "web_read"),
    ("tool_result", "call_r2"): ("read_file", None, "ok", 5, "builtin", None, None),
    ("tool_result", "call_s1"): ("read_skill", None, "ok", 10, "builtin", None, None),
    ("tool_result", "call_u1"): ("mcp__ghost__tool", None, "ok", None, "unknown", None, None),
    ("tool_result", "call_b1"): ("mcp.lookalike", None, "ok", 2, "builtin", None, None),
    ("read", "call_r1"): ("read_file", "/work/project/missing.md", "error", None, None, None, None),
    ("read", "call_r2"): ("read_file", "/work/project/README.md", "ok", None, None, None, None),
    ("skill_invoke", "call_s1"): ("read_skill", "plugin:agentsmd:operations", None, None, None, None, None),
    ("lifecycle", C1): ("subagent_spawned", None, None, None, None, None, None),
    ("lifecycle", C2): ("subagent_spawned", None, None, None, None, None, None),
    ("lifecycle", "0b000000-0000-7000-8000-000000000004"): ("turn_started", None, None, None, None, None, None),
    ("lifecycle", "0b000000-0000-7000-8000-000000000019"): ("turn_ended", None, "completed", None, None, None, None),
}


def run(db_path, *args):
    env = dict(os.environ, AGENT_OBSERVER_DB=db_path)
    return subprocess.run([sys.executable, "-m", "agent_observer", *args],
                          cwd=REPO, capture_output=True, text=True, env=env)


def ok(db_path, *args):
    result = run(db_path, *args)
    if result.returncode not in (0, 3):
        raise AssertionError(f"{args}: {result.returncode} {result.stderr}")
    return result


def root_events(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    out = {}
    for r in con.execute("SELECT * FROM events WHERE session_key=?", (ROOT_KEY,)):
        detail = json.loads(r["detail_json"] or "{}")
        out[(r["family"], r["native_id"])] = (
            r["name"], r["target"], r["status"], r["size_bytes"],
            detail.get("tool_source"), detail.get("mcp_server"), detail.get("mcp_tool"))
    con.close()
    return out


def comparable(db_path):
    """Ledger content under the order-test equality (#127 Spec)."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    events = sorted(tuple(r) for r in con.execute(
        "SELECT session_key, family, native_id, ordinal_num, ts, turn_id, name,"
        " target, status, duration_ms, size_bytes, truncated, fingerprint,"
        " detail_json FROM events"))
    sessions = sorted(tuple(r) for r in con.execute(
        "SELECT session_key, harness, native_id, parent_session_key, role,"
        " project_dir, git_branch, started_at, ended_at FROM sessions"))
    turns = sorted(tuple(r) for r in con.execute(
        "SELECT turn_id, session_key, model_observed, started_at, completed_at,"
        " duration_ms, state FROM turns"))
    errors = sorted(tuple(r) for r in con.execute(
        "SELECT harness, error, ordinal_num, line_excerpt FROM import_errors"))
    con.close()
    return events, sessions, turns, errors


class MuseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "muse.db")

    def tearDown(self):
        self.tmp.cleanup()

    def copy_tree(self, name="tree"):
        dest = os.path.join(self.tmp.name, name)
        shutil.copytree(TREE, dest)
        return dest

    def sync_tree(self, tree=TREE, db_path=None):
        return ok(db_path or self.db, "sync", "--harness", "muse", "--root", tree)

    def own_root(self, task="T-MUSE", db_path=None):
        db_path = db_path or self.db
        ok(db_path, "capture", "create-task", "--task", task)
        ok(db_path, "capture", "assign-session", "--task", task,
           "--session", ROOT_KEY, "--exclusive", "--evidence", "fixture")

    def task(self, task="T-MUSE"):
        return json.loads(ok(self.db, "task", "show", "--task", task, "--json").stdout)

    def publish(self, task="T-MUSE"):
        return json.loads(ok(self.db, "publish", "--task", task, "--dry-run", "--json").stdout)


class ImportTest(MuseCase):
    def test_each_call_is_one_call_and_one_result_with_literal_evidence(self):
        result = self.sync_tree()
        self.assertIn("muse: 2 sources (0 unchanged), 0 new responses, 24 new events",
                      result.stdout)
        self.assertEqual(root_events(self.db), EXPECTED)
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 0)
        turns = con.execute("SELECT turn_id, state, duration_ms FROM turns"
                            " WHERE session_key=?", (ROOT_KEY,)).fetchall()
        self.assertEqual(turns, [("muse:" + RUN, "completed", 21000)])
        # Internal task starts add no turn; every root event carries the run.
        self.assertEqual(con.execute(
            "SELECT DISTINCT turn_id FROM events WHERE session_key=?",
            (ROOT_KEY,)).fetchall(), [("muse:" + RUN,)])
        con.close()

    def test_unchanged_resync_writes_nothing(self):
        self.sync_tree()
        before = comparable(self.db)
        again = json.loads(ok(self.db, "sync", "--harness", "muse", "--root", TREE,
                              "--json").stdout)
        row = next(h for h in again["harnesses"] if h["harness"] == "muse")
        self.assertEqual((row["unchanged"], row["events_inserted"], row["events_updated"]),
                         (2, 0, 0))
        self.assertEqual(comparable(self.db), before)

    def test_append_after_the_initial_check_is_imported_same_sync(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from agent_observer import cli
        from agent_observer.ingest import JsonlSource
        tree = self.copy_tree()
        path = os.path.join(tree, DAY, ROOT, "session.jsonl")
        with open(ROOT_LOG, "rb") as fh:
            lines = fh.readlines()
        with open(path, "wb") as fh:
            fh.writelines(lines[:8])

        def sync():
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["--db", self.db, "sync", "--harness", "muse",
                                 "--source", path, "--json"])
            self.assertEqual(code, 0)
            row = next(h for h in json.loads(out.getvalue())["harnesses"]
                       if h["harness"] == "muse")
            return row["unchanged"], row["events_inserted"], row["events_updated"]

        self.assertEqual(sync(), (0, 4, 0))
        self.assertEqual(len(root_events(self.db)), 4)
        original = JsonlSource.__init__
        appended = []

        def raced(src, *args, **kwargs):
            # The rest of the log lands right after the real initial check
            # found the source unchanged, before the adapter's fast path.
            original(src, *args, **kwargs)
            if src.path == path and src.unchanged and not appended:
                with open(path, "ab") as fh:
                    fh.writelines(lines[8:])
                appended.append(True)

        with mock.patch.object(JsonlSource, "__init__", raced):
            self.assertEqual(sync(), (0, 16, 3))
        self.assertEqual(appended, [True])
        self.assertEqual(root_events(self.db), EXPECTED)
        con = sqlite3.connect(self.db)
        offsets = con.execute("SELECT read_offset, size_bytes FROM sources").fetchone()
        con.close()
        self.assertEqual(offsets, (len(b"".join(lines)), len(b"".join(lines))))
        self.assertEqual(sync(), (1, 0, 0))

    def test_split_appends_reach_the_same_literal_result(self):
        tree = self.copy_tree()
        path = os.path.join(tree, DAY, ROOT, "session.jsonl")
        with open(ROOT_LOG, "rb") as fh:
            lines = fh.readlines()
        split_db = os.path.join(self.tmp.name, "split.db")
        with open(path, "wb") as fh:
            fh.truncate()
        statuses = []
        for index, line in enumerate(lines):
            with open(path, "ab") as fh:
                fh.write(line[:len(line) // 2])
            # A partial trailing line is left for the next sync.
            ok(split_db, "sync", "--harness", "muse", "--source", path)
            with open(path, "ab") as fh:
                fh.write(line[len(line) // 2:])
            ok(split_db, "sync", "--harness", "muse", "--source", path)
            events = root_events(split_db)
            statuses.append(events.get(("tool_result", "call_r2"), (None,) * 4)[2:4])
        # call_r2: result committed (size 5, no status) before its terminal.
        self.assertIn((None, 5), statuses)
        self.assertEqual(statuses[-1], ("ok", 5))
        self.assertEqual(root_events(split_db), EXPECTED)
        ok(self.db, "sync", "--harness", "muse", "--source", ROOT_LOG)
        self.assertEqual(comparable(split_db), comparable(self.db))

    def test_partial_line_is_not_imported(self):
        tree = self.copy_tree()
        path = os.path.join(tree, DAY, ROOT, "session.jsonl")
        with open(ROOT_LOG, "rb") as fh:
            lines = fh.readlines()
        with open(path, "wb") as fh:
            fh.writelines(lines[:-1])
            fh.write(lines[-1][:20])
        ok(self.db, "sync", "--harness", "muse", "--source", path)
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM import_errors").fetchone()[0], 0)
        con.close()

    def test_consumed_unsupported_schema_is_quarantined_by_key_names(self):
        tree = self.copy_tree()
        path = os.path.join(tree, DAY, ROOT, "session.jsonl")
        with open(path, "a") as fh:
            for ptype, payload in (
                    ("tool_batch.effect.terminal", {"kind": "tool_batch_effect", "run_id": RUN,
                                                    "record": {"call_id": "call_p1",
                                                               "outcome": {"kind": "completed"}}}),
                    ("runtime.unconsumed.kind", {"kind": "other"})):
                fh.write(json.dumps({
                    "id": "0b000000-0000-7000-8000-0000000000ff", "payload": payload,
                    "payload_schema_version": 1, "payload_type": ptype,
                    "record_type": "event", "recorded_at": 1792442499000000,
                    "schema_version": 2, "sequence": 99,
                    "stream": {"id": ROOT, "kind": "session"}}) + "\n")
        ok(self.db, "sync", "--harness", "muse", "--source", path)
        con = sqlite3.connect(self.db)
        errors = con.execute("SELECT error, ordinal_num, line_excerpt FROM import_errors").fetchall()
        self.assertEqual(errors, [("unsupported_schema", 27, "id,payload,payload_schema_version,"
                                   "payload_type,record_type,recorded_at,schema_version,sequence,stream")])
        # The unsupported terminal did not answer call_p1.
        self.assertNotIn(("tool_result", "call_p1"), root_events(self.db))
        con.close()

    def edited_import(self, line, edit):
        """Sync a copy of the root log whose 0-based line was edited."""
        tree = self.copy_tree()
        path = os.path.join(tree, DAY, ROOT, "session.jsonl")
        with open(path) as fh:
            records = [json.loads(text) for text in fh]
        edit(records[line])
        with open(path, "w") as fh:
            fh.writelines(json.dumps(r, sort_keys=True) + "\n" for r in records)
        result = run(self.db, "sync", "--harness", "muse", "--source", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        con = sqlite3.connect(self.db)
        errors = con.execute("SELECT error, ordinal_num, line_excerpt FROM import_errors").fetchall()
        con.close()
        return errors, root_events(self.db)

    FRAME_KEYS = ("causation_id,durability,id,payload,payload_schema_version,payload_type,"
                  "record_type,recorded_at,schema_version,sequence,stream")

    def test_wrong_type_terminal_outcome_is_quarantined(self):
        # Line 9 is call_r1's failed terminal.
        errors, events = self.edited_import(
            9, lambda r: r["payload"]["record"]["outcome"].update(kind=[]))
        self.assertEqual(errors, [("schema_error", 9, self.FRAME_KEYS)])
        expected = dict(EXPECTED)
        expected[("tool_result", "call_r1")] = ("read_file", None, None, 7, "builtin", None, None)
        expected[("read", "call_r1")] = ("read_file", "/work/project/missing.md", None, None,
                                         None, None, None)
        self.assertEqual(events, expected)

    def test_missing_terminal_outcome_leaves_status_unknown(self):
        errors, events = self.edited_import(
            9, lambda r: r["payload"]["record"].update(outcome={}))
        self.assertEqual(errors, [("schema_error", 9, self.FRAME_KEYS)])
        self.assertEqual(events[("tool_result", "call_r1")][2:4], (None, 7))
        self.assertEqual(events[("tool_result", "call_m1")][2:4], ("ok", 18))

    def test_wrong_type_run_discriminator_is_quarantined(self):
        # Line 4 is the run's start frame; every other event survives.
        errors, events = self.edited_import(4, lambda r: r["payload"]["event"].update(kind=[]))
        self.assertEqual(errors, [("schema_error", 4, self.FRAME_KEYS)])
        expected = {k: v for k, v in EXPECTED.items()
                    if k != ("lifecycle", "0b000000-0000-7000-8000-000000000004")}
        self.assertEqual(events, expected)

    def test_rejected_first_record_cannot_claim_the_session(self):
        # A malformed terminal from another invented stream, inserted before
        # every valid consumed frame, is quarantined alone.
        tree = self.copy_tree()
        path = os.path.join(tree, DAY, ROOT, "session.jsonl")
        with open(path) as fh:
            records = [json.loads(text) for text in fh]
        bad = json.loads(json.dumps(records[9]))
        bad["payload"]["record"]["outcome"]["kind"] = []
        bad["stream"]["id"] = "0a000000-0000-7000-8000-0000000000ee"
        records.insert(1, bad)
        with open(path, "w") as fh:
            fh.writelines(json.dumps(r, sort_keys=True) + "\n" for r in records)
        result = run(self.db, "sync", "--harness", "muse", "--source", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        con = sqlite3.connect(self.db)
        errors = con.execute("SELECT error, ordinal_num, line_excerpt FROM import_errors").fetchall()
        sessions = [r[0] for r in con.execute("SELECT session_key FROM sessions ORDER BY 1")]
        source_session = con.execute("SELECT session_id FROM sources").fetchone()[0]
        con.close()
        self.assertEqual(errors, [("schema_error", 1, self.FRAME_KEYS)])
        self.assertEqual(sessions, [ROOT_KEY, "muse:" + C1, "muse:" + C2])
        self.assertEqual(source_session, ROOT)
        self.assertEqual(root_events(self.db), EXPECTED)

    def test_rejected_record_does_not_move_the_span(self):
        errors, events = self.edited_import(9, lambda r: (
            r["payload"]["record"]["outcome"].update(kind=[]), r.update(recorded_at=1)))
        self.assertEqual(errors, [("schema_error", 9, self.FRAME_KEYS)])
        self.assertEqual(events[("tool_result", "call_r1")][2:4], (None, 7))
        con = sqlite3.connect(self.db)
        span = con.execute("SELECT started_at, ended_at FROM sessions WHERE session_key=?",
                           (ROOT_KEY,)).fetchone()
        con.close()
        # The valid frames span sequence 1 to 26 at one second each.
        self.assertEqual(span, (1792442401.0, 1792442426.0))

    def inserted_import(self, line, edit):
        """Sync the root log with an edited copy of line inserted at line 1."""
        tree = self.copy_tree()
        path = os.path.join(tree, DAY, ROOT, "session.jsonl")
        with open(path) as fh:
            records = [json.loads(text) for text in fh]
        bad = json.loads(json.dumps(records[line]))
        edit(bad)
        records.insert(1, bad)
        with open(path, "w") as fh:
            fh.writelines(json.dumps(r, sort_keys=True) + "\n" for r in records)
        result = run(self.db, "sync", "--harness", "muse", "--source", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        con = sqlite3.connect(self.db)
        state = {
            "errors": con.execute("SELECT error, ordinal_num, line_excerpt FROM import_errors").fetchall(),
            "sessions": [r[0] for r in con.execute("SELECT session_key FROM sessions ORDER BY 1")],
            "source": con.execute("SELECT session_id FROM sources").fetchone()[0],
            "span": con.execute("SELECT started_at, ended_at FROM sessions WHERE session_key=?",
                                (ROOT_KEY,)).fetchone(),
        }
        con.close()
        state["events"] = root_events(self.db)
        return state

    # (fixture line, batch list key) for the call batch and the result batch.
    BATCHES = ((7, "tool_calls"), (13, "results"))

    def test_rejected_batch_from_another_stream_cannot_claim_the_session(self):
        for line, key in self.BATCHES:
            with self.subTest(key=key):
                self.tearDown()
                self.setUp()
                got = self.inserted_import(line, lambda r: (
                    r["payload"]["event"].update({key: [{}]}),
                    r["stream"].update(id="0a000000-0000-7000-8000-0000000000ee")))
                self.assertEqual(got["errors"], [("missing_id", 1, self.FRAME_KEYS)])
                self.assertEqual(got["sessions"], [ROOT_KEY, "muse:" + C1, "muse:" + C2])
                self.assertEqual(got["source"], ROOT)
                self.assertEqual(got["span"], (1792442401.0, 1792442426.0))
                self.assertEqual(got["events"], EXPECTED)

    def test_rejected_batch_does_not_move_the_span(self):
        for line, key in self.BATCHES:
            with self.subTest(key=key):
                self.tearDown()
                self.setUp()
                got = self.inserted_import(line, lambda r: (
                    r["payload"]["event"].update({key: [{}]}), r.update(recorded_at=1)))
                self.assertEqual(got["errors"], [("missing_id", 1, self.FRAME_KEYS)])
                self.assertEqual(got["span"], (1792442401.0, 1792442426.0))
                self.assertEqual(got["events"], EXPECTED)

    def test_mixed_call_batch_is_rejected_whole(self):
        # Line 7 commits call_r1 and call_m1; a bad second item rejects both.
        errors, events = self.edited_import(7, lambda r: r["payload"]["event"].update(
            tool_calls=[r["payload"]["event"]["tool_calls"][0], {"name": "read_file"}]))
        self.assertEqual(errors, [("missing_id", 7, self.FRAME_KEYS)])
        expected = {k: v for k, v in EXPECTED.items() if k[1] not in ("call_r1", "call_m1")}
        self.assertEqual(events, expected)

    def test_mixed_result_batch_is_rejected_whole(self):
        # Line 13 commits results for call_r1 and call_m1; the terminals stay.
        errors, events = self.edited_import(13, lambda r: r["payload"]["event"].update(
            results=[r["payload"]["event"]["results"][0], {"text": "x"}]))
        self.assertEqual(errors, [("missing_id", 13, self.FRAME_KEYS)])
        expected = dict(EXPECTED)
        expected[("tool_result", "call_r1")] = ("read_file", None, "error", None, "builtin", None, None)
        expected[("tool_result", "call_m1")] = ("mcp__t3_code__web_read", None, "ok", None,
                                                "mcp", "t3-code", "web_read")
        self.assertEqual(events, expected)

    def test_unconsumed_task_frame_stays_silent_even_when_odd(self):
        # Line 6 is an internal kind=task start: deliberately not consumed.
        errors, events = self.edited_import(6, lambda r: r["payload"]["event"].update(kind=[]))
        self.assertEqual(errors, [])
        self.assertEqual(events, EXPECTED)

    def test_effect_started_is_version_gated(self):
        # Line 8 is call_r1's started effect; it adds no evidence itself.
        errors, events = self.edited_import(8, lambda r: r.update(schema_version=2))
        self.assertEqual(errors, [("unsupported_schema", 8, self.FRAME_KEYS)])
        self.assertEqual(events, EXPECTED)

    def test_no_free_text_reaches_the_ledger(self):
        self.sync_tree()
        con = sqlite3.connect(self.db)
        dump = "\n".join(con.iterdump())
        con.close()
        for text in ("synthetic reason text", "synthetic prompt", "café",
                     "skill body", "https://example.invalid", "\"decision\""):
            self.assertNotIn(text, dump)

    def test_identity_detail_is_closed_and_validated(self):
        from agent_observer import privacy
        self.assertEqual(privacy.filter_detail("tool_result", {
            "tool_source": "mcp", "mcp_server": "t3-code", "mcp_tool": "web_read"}),
            {"tool_source": "mcp", "mcp_server": "t3-code", "mcp_tool": "web_read"})
        self.assertEqual(privacy.filter_detail("tool_call", {
            "tool_source": "MCP", "mcp_server": "server with spaces",
            "mcp_tool": "<b>tool</b>"}), {})
        self.assertEqual(privacy.filter_detail("read", {"tool_source": "mcp"}), {})

    def test_existing_lookup_reports_not_found_without_an_indexer(self):
        self.sync_tree()
        con = db.connect(self.db)
        event_id = con.execute("SELECT id FROM events WHERE family='tool_call'"
                               " AND native_id='call_m1'").fetchone()[0]
        self.assertEqual(lookup.show(con, [event_id])[0]["status"], "not_found")
        con.close()


class ChildTest(MuseCase):
    def availability(self, db_path, session=None):
        args = ["sessions", "availability", "--json"]
        if session:
            args += ["--session", session]
        return json.loads(ok(db_path, *args).stdout)

    def test_children_report_their_own_availability(self):
        self.sync_tree()
        machine = self.availability(self.db)
        self.assertEqual(machine["sessions"], {"available": 2, "partial": 0, "ledger_only": 1})
        self.assertEqual(self.availability(self.db, "muse:" + C2)["status"], "ledger_only")
        self.assertEqual(self.availability(self.db, "muse:" + C1)["status"], "available")
        con = sqlite3.connect(self.db)
        parents = dict(con.execute("SELECT session_key, parent_session_key FROM sessions"
                                   " WHERE role='subagent'").fetchall())
        con.close()
        self.assertEqual(parents, {"muse:" + C1: ROOT_KEY, "muse:" + C2: ROOT_KEY})

    def test_parent_first_and_child_first_converge(self):
        other = os.path.join(self.tmp.name, "child-first.db")
        ok(self.db, "sync", "--harness", "muse", "--source", ROOT_LOG)
        ok(self.db, "sync", "--harness", "muse", "--source", CHILD_LOG)
        ok(other, "sync", "--harness", "muse", "--source", CHILD_LOG)
        ok(other, "sync", "--harness", "muse", "--source", ROOT_LOG)
        self.assertEqual(comparable(self.db), comparable(other))
        self.assertEqual(self.availability(self.db), self.availability(other))

    def test_a_late_child_log_becomes_available_without_duplicates(self):
        tree = self.copy_tree()
        self.sync_tree(tree)
        before = comparable(self.db)
        late = os.path.join(tree, DAY, ROOT, "subagent", C2)
        os.makedirs(late)
        with open(CHILD_LOG) as src, open(os.path.join(late, "session.jsonl"), "w") as dst:
            dst.write(src.read().replace(C1, C2).replace("call_c1", "call_c2"))
        self.sync_tree(tree)
        self.assertEqual(self.availability(self.db, "muse:" + C2)["status"], "available")
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 3)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM events WHERE family='lifecycle' AND name='subagent_spawned'"
        ).fetchone()[0], 2)
        con.close()
        self.assertEqual(len(comparable(self.db)[0]), len(before[0]) + 4)

    def test_directory_and_stream_mismatch_links_no_parent(self):
        tree = self.copy_tree()
        wrong = os.path.join(tree, DAY, ROOT, "subagent", C2)
        os.rename(os.path.join(tree, DAY, ROOT, "subagent", C1), wrong)
        ok(self.db, "sync", "--harness", "muse", "--source", os.path.join(wrong, "session.jsonl"))
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT session_key, parent_session_key FROM sessions").fetchall(),
                         [("muse:" + C1, None)])
        self.assertEqual(con.execute("SELECT error FROM import_errors").fetchall(),
                         [("identity_conflict",)])
        con.close()

    def test_grok_child_without_log_is_not_available(self):
        con = db.connect(self.db)
        db.init_db(con)
        db.upsert_session(con, "grok:child-1", "grok", "child-1", None,
                          parent_session_key="grok:parent-1", role="subagent")
        con.commit()
        con.close()
        self.assertEqual(self.availability(self.db, "grok:child-1")["status"], "ledger_only")


class ReportTest(MuseCase):
    def test_muse_only_task_reports_activity_and_unknown_usage(self):
        self.sync_tree()
        self.own_root()
        rep = self.task()
        nulls = {b: None for b in ("input_tokens", "cached_input_tokens",
                                   "cache_write_input_tokens", "output_tokens",
                                   "reasoning_output_tokens", "total_tokens")}
        for section in (rep["attributed"], rep["scope"]):
            self.assertEqual({k: section[k] for k in nulls}, nulls)
            self.assertEqual(section["usage_status"], "unknown")
            self.assertEqual(section["unmeasured_sessions"], [ROOT_KEY])
        # Nothing is shared: that partition stays a known zero.
        self.assertEqual(rep["shared_joint"]["total_tokens"], 0)
        self.assertNotIn("usage_status", rep["shared_joint"])
        [phase] = rep["phases"]
        self.assertEqual(phase["usage"]["total_tokens"], None)
        self.assertEqual(phase["usage"]["usage_status"], "unknown")
        self.assertFalse(phase["estimated_cost"]["complete"])
        self.assertIsNone(phase["estimated_cost"]["estimated_cost_usd_total"])
        self.assertEqual(phase["estimated_cost"]["status"], "unknown")
        self.assertEqual(phase["activity"], {
            "tool_calls": 7, "mcp_results": 1, "reads": 2, "file_changes": None,
            "failed_tool_results": 1, "skill_invocations": 1})
        self.assertEqual(phase["activity_coverage"], {"file_changes": "unavailable"})
        self.assertEqual(rep["coverage"]["unbound_child_sessions"], ["muse:" + C1, "muse:" + C2])
        self.assertFalse(rep["complete"])
        self.assertEqual(rep["total_cost"]["text"], "unknown")
        text = ok(self.db, "task", "show", "--task", "T-MUSE").stdout
        self.assertIn("attributed responses: 0 total=unknown; 1 unmeasured sessions", text)
        self.assertIn("phase unclassified: responses=0 total=unknown; 1 unmeasured sessions", text)
        published = self.publish()
        self.assertEqual(published["summary"]["usage"]["usage_status"], "unknown")
        self.assertEqual(published["summary"]["phases"], rep["phases"])
        body = published["body"]
        self.assertIn("| unclassified | unmeasured sessions (1) | 0 | unknown | unknown | unknown"
                      " | unknown | unknown | unknown | unknown |", body)
        self.assertIn("| unclassified | 7 | 1 | 2 | 1 | unavailable | 1 |", body)
        self.assertIn("2 worker sessions without an owner", body)
        self.assertNotIn("$0.00", body)

    def test_muse_beside_measured_codex_is_a_lower_bound(self):
        self.sync_tree()
        ok(self.db, "sync", "--source", MINI)
        self.own_root()
        ok(self.db, "capture", "assign-session", "--task", "T-MUSE", "--session",
           "codex:sess-fixture-mini-01", "--exclusive", "--evidence", "fixture")
        rep = self.task()
        self.assertEqual(rep["attributed"]["total_tokens"], 5500)
        self.assertEqual(rep["attributed"]["usage_status"], "lower_bound")
        [phase] = rep["phases"]
        self.assertEqual(phase["usage"]["total_tokens"], 5500)
        self.assertEqual(phase["usage"]["usage_status"], "lower_bound")
        self.assertFalse(phase["estimated_cost"]["complete"])
        self.assertIsNone(phase["estimated_cost"]["estimated_cost_usd_total"])
        # Codex records skill invocations as unsupported and Muse records
        # no file changes: the measured counts stay, marked partial.
        self.assertEqual(phase["activity_coverage"],
                         {"file_changes": "partial", "skill_invocations": "partial"})
        self.assertEqual(phase["activity"]["skill_invocations"], 1)
        text = ok(self.db, "task", "show", "--task", "T-MUSE").stdout
        self.assertIn("total=>=5,500 (lower bound); 1 unmeasured sessions", text)
        body = self.publish()["body"]
        self.assertIn("unmeasured sessions (1)", body)
        self.assertIn(">=1 (partial)", body)

    def test_mixed_semantics_keep_by_semantics_only(self):
        self.sync_tree()
        ok(self.db, "sync", "--source", MINI)
        ok(self.db, "sync", "--harness", "claude", "--source", CLAUDE)
        self.own_root()
        con = sqlite3.connect(self.db)
        keys = [r[0] for r in con.execute(
            "SELECT DISTINCT session_key FROM responses WHERE session_key LIKE 'codex:%'"
            " OR session_key LIKE 'claude:%'")]
        con.close()
        for key in keys:
            ok(self.db, "capture", "assign-session", "--task", "T-MUSE", "--session", key,
               "--exclusive", "--evidence", "fixture")
        rep = self.task()
        self.assertNotIn("total_tokens", rep["attributed"])
        self.assertIn("by_semantics", rep["attributed"])
        self.assertEqual(rep["attributed"]["usage_status"], "lower_bound")
        text = ok(self.db, "task", "show", "--task", "T-MUSE").stdout
        self.assertIn("total=mixed semantics (see --json); 1 unmeasured sessions", text)

    def test_measured_only_and_known_zero_controls_are_unchanged(self):
        ok(self.db, "sync", "--source", MINI)
        ok(self.db, "capture", "create-task", "--task", "T-CX")
        ok(self.db, "capture", "assign-session", "--task", "T-CX", "--session",
           "codex:sess-fixture-mini-01", "--exclusive", "--evidence", "fixture")
        rep = self.task("T-CX")
        self.assertEqual(rep["attributed"]["total_tokens"], 5500)
        self.assertNotIn("usage_status", rep["attributed"])
        self.assertEqual(rep["shared_joint"]["total_tokens"], 0)
        for phase in rep["phases"]:
            self.assertNotIn("usage_status", phase["usage"])
            self.assertNotIn("file_changes", phase["activity_coverage"])
        # A response-less session of a harness that imports usage keeps
        # today's exact zero.
        con = db.connect(self.db)
        source = con.execute("INSERT INTO sources(harness, path, sha256, imported_at)"
                             " VALUES('codex','fixture:empty','x',1.0)").lastrowid
        db.upsert_session(con, "codex:empty", "codex", "empty", source)
        con.commit()
        con.close()
        ok(self.db, "capture", "create-task", "--task", "T-EMPTY")
        ok(self.db, "capture", "assign-session", "--task", "T-EMPTY", "--session",
           "codex:empty", "--exclusive", "--evidence", "fixture")
        empty = self.task("T-EMPTY")
        self.assertEqual(empty["attributed"]["total_tokens"], 0)
        self.assertNotIn("usage_status", empty["attributed"])
        listing = ok(self.db, "sessions", "list").stdout
        self.assertIn("codex:empty", listing)
        self.assertIn("responses=0 total=0 ", listing)

    def test_sessions_list_and_show_keep_unknown_usage(self):
        self.sync_tree()
        listing = ok(self.db, "sessions", "list", "--harness", "muse").stdout
        self.assertIn(f"{ROOT_KEY}  /work/project  agentsmd=unknown  responses=0 total=unknown",
                      listing)
        rows = {r["session_key"]: r for r in json.loads(
            ok(self.db, "sessions", "list", "--harness", "muse", "--json").stdout)["sessions"]}
        self.assertEqual(rows[ROOT_KEY]["usage_status"], "unknown")
        self.assertIsNone(rows[ROOT_KEY]["total_tokens"])
        show = json.loads(ok(self.db, "sessions", "show", "--session", ROOT_KEY).stdout)
        self.assertEqual(show["usage"]["usage_status"], "unknown")
        self.assertEqual(show["usage"]["unmeasured_sessions"], [ROOT_KEY])

    def ledger_cell(self, sessions):
        """One task whole-owning sessions built directly in the ledger."""
        con = db.connect(self.db)
        db.init_db(con)
        for key, harness, family in sessions:
            source = con.execute("INSERT INTO sources(harness, path, sha256, imported_at)"
                                 " VALUES(?,?,?,?)", (harness, "fixture:" + key, "x", 1.0)).lastrowid
            db.upsert_session(con, key, harness, key.split(":", 1)[1], source)
            if family:
                insert_event(con, {}, source_id=source, session_key=key, family=family,
                             native_id="e-" + key, name="Edit" if family == "file_change" else "skill",
                             target="/work/a.py" if family == "file_change" else "agentsmd:operations")
        con.execute("INSERT INTO tasks(task_id, created_at) VALUES('T-CELL', 1.0)")
        for key, _, _ in sessions:
            con.execute("INSERT INTO session_assignments VALUES(?,?,?,?)",
                        (key, "T-CELL", "fixture", 1.0))
        con.commit()
        con.close()

    def test_partial_cells_keep_measured_counts(self):
        self.sync_tree()
        self.ledger_cell([("opencode:oc-1", "opencode", "file_change")])
        con = db.connect(self.db)
        con.execute("INSERT INTO session_assignments VALUES(?,?,?,?)",
                    (ROOT_KEY, "T-CELL", "fixture", 1.0))
        con.commit()
        con.close()
        [phase] = self.task("T-CELL")["phases"]
        self.assertEqual(phase["activity"]["file_changes"], 1)
        self.assertEqual(phase["activity_coverage"]["file_changes"], "partial")
        body = self.publish("T-CELL")["body"]
        self.assertIn("| unclassified | 7 | 1 | 2 | 1 | >=1 (partial) | 1 |", body)

    def test_claude_skill_beside_codex_is_partial(self):
        # A session contributes to a cell through its events.
        self.ledger_cell([("claude:cl-1", "claude", "skill_invoke"),
                          ("codex:cx-1", "codex", "tool_call")])
        [phase] = self.task("T-CELL")["phases"]
        self.assertEqual(phase["activity"]["skill_invocations"], 1)
        self.assertEqual(phase["activity_coverage"], {"skill_invocations": "partial"})

    def test_router_owned_native_session_keeps_native_capabilities(self):
        self.ledger_cell([("codex:cx-r", "codex", "file_change")])
        con = db.connect(self.db)
        con.execute("INSERT INTO task_turns(task_id, turn_id, role, harness, session_key, stage)"
                    " VALUES('T-CELL','router:inv-1','child','router','codex:cx-r','implementation')")
        con.commit()
        con.close()
        phases = {p["phase"]: p for p in self.task("T-CELL")["phases"]}
        [phase] = phases.values()
        self.assertEqual(phase["activity"]["tool_calls"], 0)
        self.assertEqual(phase["activity"]["file_changes"], 1)
        self.assertEqual(phase["activity_coverage"], {"skill_invocations": "unavailable"})


class T3Test(MuseCase):
    def test_strong_muse_thread_binds_the_root_and_weak_binds_nothing(self):
        from agent_observer.adapters import t3 as t3_adapter
        from tests.test_t3_v2 import PR5, ROOT as THREAD, write_v2
        state = write_v2(os.path.join(self.tmp.name, "chromeria-v2.sqlite"))
        native = sqlite3.connect(state)
        for tid, native_id, strength in ((THREAD, ROOT, "strong"),
                                         ("thread:weak-muse", C2, "weak")):
            native.execute(
                "INSERT INTO orchestration_v2_projection_provider_threads("
                " provider_thread_id, thread_id, provider, status, updated_at,"
                " payload_json, driver) VALUES(?,?,?,?,?,?,?)",
                (f"provider-thread:{tid}:{native_id}", tid, "muse", "idle",
                 "2026-10-19T20:00:00Z",
                 json.dumps({"appThreadId": tid, "driver": "muse",
                             "nativeThreadRef": {"driver": "muse", "nativeId": native_id,
                                                 "strength": strength}}), "muse"))
        native.commit()
        native.close()
        self.sync_tree()
        con = db.connect(self.db)
        t3_adapter.sync(con, source=state)
        bound = {(r[0], r[1]) for r in con.execute(
            "SELECT session_key, task_id FROM session_assignments WHERE session_key LIKE 'muse:%'")}
        con.close()
        self.assertEqual(bound, {(ROOT_KEY, "example/beta#5")})
        self.assertTrue(PR5.endswith("/5"))


if __name__ == "__main__":
    unittest.main()
