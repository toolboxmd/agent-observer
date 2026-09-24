"""Ledger privacy: one shared module, fail-closed excerpts, closed categories.

Syncs every committed fixture plus the privacy fixtures, then scans every
table and text column for each planted secret: a tag with a quoted '>' in
an attribute, an unterminated block, a '<<<' block, an aiTitle, an unknown
error category, and a non-string event target. A version-bump test proves
rows written under older rules are corrected in place without duplicating
import_errors. Unit tests cover every privacy.py function.
"""

import glob
import os
import stat
import tempfile
import unittest
from unittest import mock

from agent_observer import db, privacy, report
from agent_observer.adapters import claude
from agent_observer.adapters.codex import import_codex_file
from agent_observer.ingest import (JsonlSource, MissingNativeId,
                                   insert_event)
from tests.helpers import FIXTURES, LedgerCase, fixture


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class LedgerPermissionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "ledger")
        self.db_path = os.path.join(self.dir, "observer.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_ledger_is_owner_only_under_a_permissive_umask(self):
        old = os.umask(0o022)
        try:
            con = db.connect(self.db_path)
            db.init_db(con)
            con.close()
        finally:
            os.umask(old)
        self.assertEqual(mode(self.dir), 0o700)
        self.assertEqual(mode(self.db_path), 0o600)

    def test_existing_loose_ledger_is_tightened_and_stays_usable(self):
        os.makedirs(self.dir)
        os.chmod(self.dir, 0o755)
        with open(self.db_path, "w"):
            pass
        os.chmod(self.db_path, 0o644)
        con = db.connect(self.db_path)
        db.init_db(con)
        db.upsert_session(con, "codex:s", "codex", "s", None)
        con.commit()
        con.close()
        self.assertEqual(mode(self.dir), 0o700)
        self.assertEqual(mode(self.db_path), 0o600)
        again = db.connect(self.db_path)
        row = again.execute(
            "SELECT session_key FROM sessions WHERE session_key='codex:s'").fetchone()
        again.close()
        self.assertIsNotNone(row)

    def test_live_sidecars_are_owner_only(self):
        con = db.connect(self.db_path)
        db.init_db(con)
        con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('codex','p','x',0)")
        for i in range(50):
            con.execute(
                "INSERT INTO responses(response_id, source_id, harness,"
                " session_key, total_tokens) VALUES(?,?,?,?,?)",
                (f"codex:r{i}", 1, "codex", "codex:s", i))
        con.commit()
        sidecars = [self.db_path + "-wal", self.db_path + "-shm"]
        self.assertTrue(any(os.path.exists(p) for p in sidecars),
                        "expected live SQLite sidecars while connected")
        for path in sidecars:
            if os.path.exists(path):
                self.assertEqual(mode(path), 0o600, path)
        con.close()


class LedgerLocationTest(unittest.TestCase):
    def test_default_ledger_lives_outside_any_repo(self):
        home = os.path.expanduser("~")
        self.assertTrue(db.default_path().startswith(
            os.path.join(home, ".local", "state", "agent-observer")))


PRIVACY_SECRETS = (
    # codex-privacy.jsonl
    "SECRET-PRIV-CODEX-TAG-aaaa1111",
    "SECRET-PRIV-CODEX-UNTERM-bbbb2222",
    "SECRET-PRIV-CODEX-UNKNOWN-cccc3333",
    "SECRET-PRIV-CODEX-BADJSON-dddd4444",
    "SECRET-PRIV-CODEX-LIST-eeee5555",
    "SECRET-PRIV-CODEX-CMD-ffff6666",
    "SECRET-PRIV-CODEX-REASON-gggg7777",
    "SECRET-PRIV-CODEX-PARSED-qqqq1111",
    # claude-privacy.jsonl
    "SECRET-PRIV-CLAUDE-TAG-hhhh8888",
    "SECRET-PRIV-CLAUDE-TITLE-iiii9999",
    "SECRET-PRIV-CLAUDE-BLOCK-jjjj0000",
    "SECRET-PRIV-CLAUDE-CMD-kkkk1212",
    "SECRET-PRIV-CLAUDE-DENIAL-llll3434",
    "SECRET-PRIV-CLAUDE-INT-mmmm5656",
    "SECRET-PRIV-CLAUDE-UNKNOWN-nnnn7878",
    # edge writes below
    "SECRET-PRIV-EDGE-CATEGORY-zzzz0000",
    "SECRET-PRIV-EDGE-TARGET-yyyy1111",
    "SECRET-PRIV-EDGE-DETAIL-xxxx2222",
)

GENUINE_EXCERPT = "Please summarize the project status for the team."


def sync_every_fixture(con):
    """Import each committed fixture through its harness adapter."""
    for path in sorted(glob.glob(os.path.join(FIXTURES, "*.jsonl"))):
        name = os.path.basename(path)
        if name.startswith("codex-"):
            import_codex_file(con, path)
        elif name.startswith("claude-"):
            claude.import_claude_file(con, path)
    claude.sync(con, root=os.path.join(FIXTURES, "claude"))


def text_columns(con):
    """Every (table, column) holding TEXT in the ledger."""
    found = []
    tables = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%'").fetchall()
    for table in tables:
        for col in con.execute(f"PRAGMA table_info({table['name']})"):
            if (col["type"] or "").upper() == "TEXT":
                found.append((table["name"], col["name"]))
    return found


def write_edge_cases(con):
    """Secrets through the sink paths directly: an unknown error category,
    a non-string event target, and free prose in an event detail."""
    edge = os.path.join(tempfile.gettempdir(), "agent-observer-privacy-edge.jsonl")
    with open(edge, "w") as fh:
        fh.write("{}\n")
    src = JsonlSource(con, "codex", edge)
    list(src.records())
    src.error(0, "SECRET-PRIV-EDGE-CATEGORY-zzzz0000",
              '{"type": "x", "note": "SECRET-PRIV-EDGE-CATEGORY-zzzz0000"}')
    source_id = con.execute(
        "SELECT id FROM sources WHERE harness='codex' LIMIT 1").fetchone()
    stats: dict = {}
    insert_event(con, stats, source_id=source_id["id"] if source_id else 1,
                 session_key="codex:edge", family="tool_result",
                 native_id="edge-1",
                 target=["SECRET-PRIV-EDGE-TARGET-yyyy1111"],
                 detail={"exit_code": 1,
                         "note": "SECRET-PRIV-EDGE-DETAIL-xxxx2222 free prose"})
    con.commit()
    os.remove(edge)


def scan_secrets(case):
    for table, column in text_columns(case.con):
        for row in case.query(f"SELECT {column} FROM {table}"):
            for secret in PRIVACY_SECRETS:
                case.assertNotIn(secret, row[column] or "",
                                 f"{table}.{column} leaks {secret}")


class PrivacyFixtureScanTest(LedgerCase):
    def test_no_planted_secret_in_any_text_column(self):
        sync_every_fixture(self.con)
        write_edge_cases(self.con)
        scan_secrets(self)

    def test_error_column_holds_only_closed_categories(self):
        sync_every_fixture(self.con)
        write_edge_cases(self.con)
        allowed = set(privacy.ERROR_CATEGORIES) | {privacy.ERROR_FALLBACK}
        for row in self.query("SELECT DISTINCT error FROM import_errors"):
            self.assertIn(row["error"], allowed, row["error"])
        for row in self.query("SELECT error FROM import_errors"):
            self.assertRegex(row["error"] or "", r"\A[a-z_]+\Z")

    def test_unknown_category_maps_to_the_fallback(self):
        sync_every_fixture(self.con)
        write_edge_cases(self.con)
        rows = self.query(
            "SELECT error, line_excerpt FROM import_errors"
            " WHERE source_path LIKE '%privacy-edge%'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["error"], privacy.ERROR_FALLBACK)
        self.assertEqual(rows[0]["line_excerpt"], "note,type")

    def test_no_native_free_text_title_is_stored(self):
        sync_every_fixture(self.con)
        for row in self.query("SELECT session_key, title FROM sessions"):
            self.assertIsNone(row["title"], row["session_key"])

    def test_genuine_excerpts_truncate_at_tag_markers(self):
        sync_every_fixture(self.con)
        rows = {r["native_id"]: r for r in self.query(
            "SELECT native_id, kind, text_excerpt FROM submissions"
            " WHERE native_id IN ('codex:msg-priv-sub-01', 'claude:u-priv-1',"
            " 'claude:u-priv-int')")}
        # A tag with a quoted '>' in an attribute still truncates at the
        # first '<': no tag parsing, fail closed.
        self.assertEqual(rows["codex:msg-priv-sub-01"]["text_excerpt"],
                         GENUINE_EXCERPT)
        self.assertEqual(rows["claude:u-priv-1"]["text_excerpt"],
                         GENUINE_EXCERPT)
        # Interrupt text is never stored, though the kind is kept.
        self.assertEqual(rows["claude:u-priv-int"]["kind"], "interrupt")
        self.assertEqual(rows["claude:u-priv-int"]["text_excerpt"], "")

    def test_assistant_excerpts_keep_only_marker_free_spans(self):
        sync_every_fixture(self.con)
        details = {r["native_id"]: r["detail_json"] for r in self.query(
            "SELECT native_id, detail_json FROM events"
            " WHERE family='assistant_message' AND (native_id LIKE 'msg-priv-%'"
            " OR native_id LIKE 'as-priv-%')")}
        import json as _json
        # An unterminated block and a '<<<' block store no excerpt.
        for native in ("msg-priv-as-01", "as-priv-block:0"):
            self.assertIn(native, details, native)
            self.assertIsNone(details[native], native)
        # A marker-free span keeps its excerpt.
        for native in ("msg-priv-as-02", "as-priv-clean:0"):
            self.assertIn(native, details, native)
            self.assertEqual(_json.loads(details[native])["excerpt"],
                             "Summary ready for review.")

    def test_non_string_target_and_prose_detail_are_dropped(self):
        sync_every_fixture(self.con)
        write_edge_cases(self.con)
        row = self.query(
            "SELECT target, detail_json FROM events WHERE native_id='edge-1'"
            " AND session_key='codex:edge'")[0]
        self.assertIsNone(row["target"])
        import json as _json
        self.assertEqual(_json.loads(row["detail_json"]), {"exit_code": 1})

    def test_parsed_non_string_read_target_is_dropped(self):
        import_codex_file(self.con, fixture("codex-privacy.jsonl"))
        rows = self.query(
            "SELECT family, native_id, target FROM events"
            " WHERE native_id LIKE 'exec-priv-002%'")
        families = {r["family"] for r in rows}
        # The command itself is kept as a tool_result; the parsed read with
        # a non-string path stores no read event, target, or identity.
        self.assertIn("tool_result", families)
        self.assertNotIn("read", families)
        self.assertNotIn("skill_read", families)
        for row in rows:
            self.assertNotIn("SECRET-PRIV-CODEX-PARSED",
                             row["target"] or "", row["native_id"])

    def test_overlong_assistant_detail_is_bounded_before_serialization(self):
        import json as _json
        # A direct overlong clean value is reduced to the rule-2 span.
        kept = privacy.filter_detail("assistant_message",
                                     {"excerpt": "z" * 5000})
        self.assertEqual(kept, {"excerpt": "z" * 400})
        # A detail that would exceed the JSON budget stays valid JSON:
        # paths shrink pre-serialization, never slice mid-value.
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('codex','p','x',0)")
        stats: dict = {}
        insert_event(self.con, stats, source_id=1, session_key="codex:s",
                     family="file_change", native_id="big-1",
                     detail={"paths": [f"/p/f{i:04d}.py" for i in range(600)]})
        stored = self.query(
            "SELECT detail_json FROM events WHERE native_id='big-1'")[0]
        self.assertLessEqual(len(stored["detail_json"]),
                             privacy.DETAIL_JSON_CHARS)
        parsed = _json.loads(stored["detail_json"])
        self.assertTrue(parsed["paths"])
        self.assertLess(len(parsed["paths"]), 600)
        self.assertEqual(parsed["paths"], sorted(parsed["paths"]))


class CodexMainSessionTest(LedgerCase):
    """Rule 1 main-session gate: parent keeps an excerpt, child/worker and
    unknown rollouts keep none, even for genuine-looking user messages."""

    def test_gate_requires_a_proven_main_thread(self):
        main = {"thread_source": "user", "session_id": "s",
                "thread_id": "s"}
        self.assertTrue(privacy.is_main_session(**main))
        self.assertTrue(privacy.is_main_session(
            **{**main, "observed_thread_ids": ("s",)}))
        # Unknown thread identity fails closed, even with session and
        # source known.
        for bad in (None, "", 123, ["s"]):
            self.assertFalse(privacy.is_main_session(
                thread_source="user", session_id="s", thread_id=bad))
        # A divergent own thread fails closed.
        self.assertFalse(privacy.is_main_session(
            **{**main, "thread_id": "worker"}))
        self.assertFalse(privacy.is_main_session(
            **{**main, "observed_thread_ids": ("worker",)}))
        self.assertFalse(privacy.is_main_session(
            **{**main, "observed_thread_ids": (None,)}))
        self.assertFalse(privacy.is_main_session(
            **{**main, "observed_thread_ids": "s"}))
        # Any other source, or a missing session, fails closed.
        self.assertFalse(privacy.is_main_session(
            **{**main, "thread_source": "spawned"}))
        self.assertFalse(privacy.is_main_session(
            **{**main, "thread_source": None}))
        self.assertFalse(privacy.is_main_session(
            **{**main, "session_id": ""}))
        self.assertFalse(privacy.is_main_session(
            **{**main, "session_id": None}))

    def test_parent_main_session_keeps_its_excerpt(self):
        self.sync("codex-parent.jsonl")
        row = self.query(
            "SELECT kind, text_excerpt, is_genuine FROM submissions"
            " WHERE native_id='codex:msg-scope-sub-p1'")[0]
        self.assertEqual(row["kind"], "genuine")
        self.assertEqual(row["text_excerpt"],
                         "Research how we track usage across harnesses.")

    def test_child_worker_message_keeps_no_excerpt(self):
        self.sync("codex-child-message.jsonl")
        row = self.query(
            "SELECT kind, text_excerpt, is_genuine, text_hash, session_key"
            " FROM submissions"
            " WHERE native_id='codex:msg-child-sub-01'")[0]
        # Authorship is still genuine human input, but a divergent own
        # thread proves a worker rollout, so no excerpt is stored. Hash,
        # kind and accounting behavior are preserved.
        self.assertEqual(row["kind"], "genuine")
        self.assertEqual(row["text_excerpt"], "")
        self.assertEqual(row["session_key"],
                         "codex:thread-fixture-childmsg-worker")
        self.assertTrue(row["text_hash"])
        self.assertEqual(
            report.scope_totals(self.con)["responses"], 1)

    def test_unknown_metadata_keeps_no_excerpt(self):
        self.sync("codex-unknown-message.jsonl")
        row = self.query(
            "SELECT kind, text_excerpt FROM submissions"
            " WHERE native_id='codex:msg-unknown-sub-01'")[0]
        self.assertEqual(row["kind"], "genuine")
        self.assertEqual(row["text_excerpt"], "")

    def test_message_before_thread_identity_keeps_its_excerpt(self):
        # The user message is the first line; the session_meta proving the
        # main session comes after. The prescan judges with the whole
        # portion known, so the excerpt survives the ordering.
        self.sync("codex-early-message.jsonl")
        row = self.query(
            "SELECT kind, text_excerpt FROM submissions"
            " WHERE native_id='codex:msg-early-sub-01'")[0]
        self.assertEqual(row["kind"], "genuine")
        self.assertEqual(row["text_excerpt"],
                         "Please summarize the early status for the review.")

    def test_stale_version_corrects_child_excerpt_in_place(self):
        leaky = staticmethod(
            lambda text, **kw: text[:300] if isinstance(text, str) else "")
        with mock.patch.object(privacy, "PRIVACY_VERSION", 0), \
                mock.patch.object(privacy, "submission_excerpt", leaky):
            import_codex_file(self.con, fixture("codex-child-message.jsonl"))
        leaked = self.query(
            "SELECT text_excerpt FROM submissions"
            " WHERE native_id='codex:msg-child-sub-01'")[0]["text_excerpt"]
        self.assertTrue(leaked)
        before = self.query("SELECT COUNT(*) n FROM submissions")[0]["n"]
        errors_before = self.query(
            "SELECT COUNT(*) n FROM import_errors")[0]["n"]
        import_codex_file(self.con, fixture("codex-child-message.jsonl"))
        after = self.query(
            "SELECT text_excerpt, kind, text_hash FROM submissions"
            " WHERE native_id='codex:msg-child-sub-01'")[0]
        self.assertEqual(after["text_excerpt"], "")
        self.assertEqual(after["kind"], "genuine")
        self.assertTrue(after["text_hash"])
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM submissions")[0]["n"], before)
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM import_errors")[0]["n"],
            errors_before)

    def test_incremental_import_keeps_empty_until_identity_proves_main(self):
        import os
        from agent_observer.adapters.codex import import_codex_file
        path = os.path.join(self.tmp.name, "rollout-child-live.jsonl")
        with open(fixture("codex-child-message.jsonl")) as fh:
            lines = fh.readlines()
        with open(path, "w") as fh:
            fh.writelines(lines[:3])
        import_codex_file(self.con, path)
        first = self.query(
            "SELECT text_excerpt, session_key FROM submissions"
            " WHERE native_id='codex:msg-child-sub-01'")[0]
        # Session id and thread source alone prove nothing: with no native
        # thread identity the excerpt stays empty instead of provisional.
        self.assertEqual(first["text_excerpt"], "")
        with open(path, "a") as fh:
            fh.writelines(lines[3:])
        import_codex_file(self.con, path)
        row = self.query(
            "SELECT text_excerpt, session_key FROM submissions"
            " WHERE native_id='codex:msg-child-sub-01'")[0]
        # The appended usage proves a worker thread; persisted metadata
        # plus the new portion fail closed, and the row is reconciled to
        # the worker session key without duplication.
        self.assertEqual(row["text_excerpt"], "")
        self.assertEqual(row["session_key"],
                         "codex:thread-fixture-childmsg-worker")
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM submissions")[0]["n"], 1)


class EventPrivacyTest(LedgerCase):
    """Rule 6 through the central writer: per-family native_id, name and
    status validation for Codex and Claude events alike."""

    def _source(self):
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at)"
            " VALUES('codex','p','x',0)")
        return self.con.execute(
            "SELECT id FROM sources WHERE harness='codex'").fetchone()["id"]

    def test_native_id_wrong_type_is_quarantined_never_stringified(self):
        self.assertIsNone(privacy.filter_native_id("tool_call", 123))
        self.assertIsNone(privacy.filter_native_id("tool_call", ["a"]))
        self.assertIsNone(privacy.filter_native_id("tool_call", None))
        self.assertIsNone(privacy.filter_native_id("tool_call", ""))
        self.assertEqual(
            privacy.filter_native_id("tool_call", "call-1"), "call-1")
        source_id = self._source()
        stats: dict = {}
        with self.assertRaises(MissingNativeId):
            insert_event(self.con, stats, source_id=source_id,
                         session_key="codex:s", family="tool_call",
                         native_id=123, name="exec")
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM events")[0]["n"], 0)

    def test_numeric_native_id_through_codex_is_missing_id(self):
        import json as _json
        import os
        path = os.path.join(self.tmp.name, "numeric-id.jsonl")
        with open(path, "w") as fh:
            fh.write(_json.dumps({
                "ordinal": 0,
                "payload": {"cli_version": "0.155.0",
                            "session_id": "sess-num-01",
                            "thread_source": "user"},
                "timestamp": "2026-09-15T14:00:00.000Z",
                "type": "session_meta"}) + "\n")
            fh.write(_json.dumps({
                "ordinal": 1,
                "payload": {"call_id": 123, "id": 456,
                            "output": "ok",
                            "type": "function_call_output"},
                "timestamp": "2026-09-15T14:00:01.000Z",
                "type": "response_item"}) + "\n")
        stats = import_codex_file(self.con, path)
        self.assertEqual(stats["malformed"], 1)
        rows = self.query("SELECT error FROM import_errors")
        self.assertEqual([r["error"] for r in rows], ["missing_id"])
        for row in self.query("SELECT native_id FROM events"):
            self.assertNotIn("123", row["native_id"])

    def test_names_identifier_families_accept_native_identifiers(self):
        # Planner ruling: tool and skill names are native identifiers
        # matching ^[A-Za-z_][A-Za-z0-9_.:/-]{0,79}$ (covers MCP names).
        # Adapters pass only the native name field, never a title.
        legit = (
            "read_file", "mcp__server__tool", "AGENTS.md", "SKILL.md",
            "agentsmd:operations", "Bash", "bash", "exec", "read",
            "edit", "write", "patch", "skill", "my-skill", "wayfinder",
            "ops", "search_files", "collaboration.spawn_agent",
            "function_call_output", "custom_tool_call_output",
            "mcp.unknown", "dynamic.unknown", "unknown", "Read",
            "Edit", "notes.md", "a", "_private", "a" * 80,
            "foo/bar", "foo:baz", "foo-bar", "foo.bar",
        )
        for family in ("tool_call", "tool_result", "read", "skill_read",
                       "skill_invoke", "permission"):
            for name in legit:
                self.assertEqual(
                    privacy.filter_event_name(family, name), name,
                    (family, name))

    def test_names_identifier_families_reject_free_text(self):
        # Titles, sentences, spaces, tag-like text, secret-looking strings
        # with spaces and punctuation outside the allowed set fail closed.
        bad = (
            "", "hello world", "Only Title Here",
            "Read `/redacted/repo/skills/ops/SKILL.md`",
            "done <b>x</b>", "a < b", "sk-fake-secret 12345",
            "SECRET TOKEN abc123", "foo bar", "foo\nbar",
            "foo,bar", "foo;bar", "foo@bar", "foo!bar", '"foo"',
            "'foo'", "(foo)", "foo=bar", "foo+bar", "foo*bar",
            "foo?bar", "foo|bar", "foo\\bar", "foo`bar", "foo~bar",
            "1abc", "123", ".foo", "/foo", "-foo", ":foo",
            "a" * 81, "x" * 200,
        )
        for family in ("tool_call", "tool_result", "read", "skill_read",
                       "skill_invoke", "permission"):
            for name in bad:
                self.assertIsNone(
                    privacy.filter_event_name(family, name), (family, name))
        for bad_value in (None, 42, 3.14, True, False, ["a"], {"a": 1}):
            for family in ("tool_call", "tool_result", "read", "skill_read",
                           "skill_invoke", "permission"):
                self.assertIsNone(
                    privacy.filter_event_name(family, bad_value),
                    (family, repr(bad_value)))
        self.assertIsNone(privacy.filter_event_name("nope", "exec"))

    def test_names_closed_sets_accept_adapter_union(self):
        self.assertEqual(
            privacy.filter_event_name("assistant_message",
                                      "assistant_message"),
            "assistant_message")
        for name in ("context_compaction", "compact_boundary",
                     "time_compacting", "compaction",
                     "auto_compact_started", "auto_compact_completed"):
            self.assertEqual(
                privacy.filter_event_name("compaction", name), name)
        for name in ("task_complete", "turn_aborted", "turn_duration",
                     "subagent_activity", "thread_goal_updated",
                     "api_error", "stop_hook_summary", "informational",
                     "Plan", "HookPrompt", "EnteredReviewMode",
                     "ExitedReviewMode", "collab.unknown",
                     "error", "turn_started", "turn_ended", "tool_started",
                     "retry_state", "subagent_spawned", "subagent_finished",
                     "task_backgrounded", "task_completed",
                     "compaction_checkpoint", "hook_execution",
                     "session_recap", "plan", "background_tasks",
                     "image_compressed", "current_mode_update",
                     "rewind_marker"):
            self.assertEqual(
                privacy.filter_event_name("lifecycle", name), name)
        for name in ("file_change",
                     "Edit", "Write", "MultiEdit", "NotebookEdit",
                     "edit", "write", "patch"):
            self.assertEqual(
                privacy.filter_event_name("file_change", name), name)

    def test_names_closed_sets_reject_identifiers_and_prose(self):
        # Closed families never accept arbitrary identifiers: even
        # identifier-shaped tool names fail there, as does all free text.
        cases = (
            ("lifecycle", "read_file"),
            ("lifecycle", "collab.spawn_agent"),
            ("lifecycle", "custom prose status"),
            ("lifecycle", "Only Title Here"),
            ("lifecycle", "done <b>x</b>"),
            ("compaction", "compact"),
            ("compaction", "read_file"),
            ("compaction", "custom prose"),
            ("file_change", "Read"),
            ("file_change", "AGENTS.md"),
            ("file_change", "custom prose"),
            ("file_change", "done <b>x</b>"),
            ("assistant_message", "chat"),
            ("assistant_message", "read_file"),
            ("assistant_message", "custom prose"),
        )
        for family, name in cases:
            self.assertIsNone(
                privacy.filter_event_name(family, name), (family, name))
        self.assertIsNone(privacy.filter_event_name("tool_call", 42))
        self.assertIsNone(privacy.filter_event_name("tool_call", ""))
        self.assertIsNone(privacy.filter_event_name("tool_call", None))

    def test_statuses_follow_closed_per_family_sets(self):
        # Preserved closed statuses, including Grok permission allow/deny
        # and lifecycle success/failed; error remains valid everywhere it
        # was allowed.
        self.assertEqual(
            privacy.filter_event_status("tool_result", "completed"),
            "completed")
        self.assertEqual(
            privacy.filter_event_status("tool_result", "error"), "error")
        self.assertEqual(
            privacy.filter_event_status("lifecycle", "cancelled"), "cancelled")
        self.assertEqual(
            privacy.filter_event_status("lifecycle", "success"), "success")
        self.assertEqual(
            privacy.filter_event_status("lifecycle", "failed"), "failed")
        self.assertEqual(
            privacy.filter_event_status("lifecycle", "error"), "error")
        self.assertEqual(
            privacy.filter_event_status("permission", "denied"), "denied")
        self.assertEqual(
            privacy.filter_event_status("permission", "allow"), "allow")
        self.assertEqual(
            privacy.filter_event_status("permission", "deny"), "deny")
        self.assertIsNone(
            privacy.filter_event_status("tool_result", "started"))
        self.assertIsNone(
            privacy.filter_event_status("tool_result", "weird prose"))
        self.assertIsNone(
            privacy.filter_event_status("tool_result", 0))
        self.assertIsNone(
            privacy.filter_event_status("compaction", "completed"))
        self.assertIsNone(
            privacy.filter_event_status("tool_result", None))

    def test_statuses_http_codes_for_lifecycle_and_tool_result(self):
        # HTTP 100-599 as int or exact three-digit string, stored as the
        # three-digit string, for lifecycle and tool_result only.
        for code in (100, 200, 403, 429, 500, 599):
            for family in ("lifecycle", "tool_result"):
                self.assertEqual(
                    privacy.filter_event_status(family, code), str(code),
                    (family, code))
                self.assertEqual(
                    privacy.filter_event_status(family, str(code)), str(code),
                    (family, str(code)))
        # Out-of-range, wrong width, wrong type and other families fail.
        for bad in (99, 0, 600, 999, 1000, 42, "99", "00", "000", "099",
                    "600", "999", "5000", "500 ", " 500", "500\n",
                    "error 500", True, False, 500.0, "200 OK",
                    ["500"], {"code": 500}):
            for family in ("lifecycle", "tool_result"):
                self.assertIsNone(
                    privacy.filter_event_status(family, bad), (family, bad))
        for family in ("tool_call", "read", "skill_read", "permission",
                       "file_change", "compaction", "assistant_message",
                       "skill_invoke"):
            self.assertIsNone(privacy.filter_event_status(family, 500))
            self.assertIsNone(privacy.filter_event_status(family, "500"))
        # Free-text status sentences and unknown enums still fail.
        for bad in ("started", "weird prose", "Only Title Here",
                    "EVIL-STATUS-abc123", ""):
            self.assertIsNone(
                privacy.filter_event_status("lifecycle", bad), bad)
            self.assertIsNone(
                privacy.filter_event_status("tool_result", bad), bad)

    def test_central_writer_routes_every_protected_field(self):
        source_id = self._source()
        stats: dict = {}
        insert_event(self.con, stats, source_id=source_id,
                     session_key="codex:s", family="tool_result",
                     native_id="call-9", name="hello world",
                     status="weird prose", target="/p/x.py",
                     detail={"exit_code": 1, "note": "prose"})
        row = self.query("SELECT * FROM events WHERE native_id='call-9'")[0]
        self.assertIsNone(row["name"])
        self.assertIsNone(row["status"])
        self.assertEqual(row["target"], "/p/x.py")
        import json as _json
        self.assertEqual(_json.loads(row["detail_json"]), {"exit_code": 1})

    def test_central_writer_keeps_legitimate_identifiers_and_codes(self):
        # Legitimate native identifiers and HTTP codes are what reach the
        # database; free text never does.
        source_id = self._source()
        stats: dict = {}
        insert_event(self.con, stats, source_id=source_id,
                     session_key="codex:s", family="tool_call",
                     native_id="call-10", name="mcp__server__tool",
                     target="/p/y.py")
        insert_event(self.con, stats, source_id=source_id,
                     session_key="codex:s", family="tool_result",
                     native_id="call-11", name="read_file", status=500,
                     target="/p/z.py")
        insert_event(self.con, stats, source_id=source_id,
                     session_key="codex:s", family="lifecycle",
                     native_id="life-1", name="error", status="429")
        import json as _json
        row = self.query(
            "SELECT * FROM events WHERE native_id='call-10'")[0]
        self.assertEqual(row["name"], "mcp__server__tool")
        row = self.query(
            "SELECT * FROM events WHERE native_id='call-11'")[0]
        self.assertEqual(row["name"], "read_file")
        self.assertEqual(row["status"], "500")
        row = self.query(
            "SELECT * FROM events WHERE native_id='life-1'")[0]
        self.assertEqual(row["name"], "error")
        self.assertEqual(row["status"], "429")
        # Skill identifier detail follows the same native-identifier rule.
        self.assertEqual(
            privacy.filter_detail(
                "skill_read", {"skill": "agentsmd:operations"}),
            {"skill": "agentsmd:operations"})
        self.assertEqual(
            privacy.filter_detail("skill_read", {"skill": "hello world"}),
            {})
        self.assertEqual(
            privacy.filter_detail("skill_read", {"skill": "done <b>x</b>"}),
            {})

    def test_stale_reimport_corrects_every_protected_event_field(self):
        self.sync("codex-mini.jsonl")
        before = {(r["family"], r["native_id"]): dict(r) for r in self.query(
            "SELECT family, native_id, name, target, status, detail_json"
            " FROM events")}
        count_before = self.query(
            "SELECT COUNT(*) n FROM events")[0]["n"]
        self.assertTrue(before)
        victim_family, victim_native = sorted(before)[0]
        self.con.execute(
            "UPDATE events SET name='Hello World prose', status='weird prose',"
            " target='leaked target', detail_json='{\"note\": \"prose\"}'"
            " WHERE family=? AND native_id=?",
            (victim_family, victim_native))
        self.con.execute(
            "UPDATE sources SET privacy_version=0 WHERE harness='codex'")
        self.con.commit()
        stats = import_codex_file(
            self.con, fixture("codex-mini.jsonl"), full=True)
        self.assertGreaterEqual(stats.get("events_updated", 0), 1)
        row = self.query(
            "SELECT family, native_id, name, target, status, detail_json"
            " FROM events WHERE family=? AND native_id=?",
            (victim_family, victim_native))[0]
        # Every protected field is re-derived through privacy.py: prose is
        # gone, valid values are restored, rows are not duplicated.
        self.assertNotIn("prose", (row["name"] or "") + (row["status"] or ""))
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM events")[0]["n"], count_before)
        if before[(victim_family, victim_native)]["name"] is not None:
            self.assertEqual(
                row["name"],
                before[(victim_family, victim_native)]["name"])
        else:
            self.assertIsNone(row["name"])


class NullOrdinalDedupTest(LedgerCase):
    """import_errors deduplicates on the structural source ordinal, so a
    record with ordinal:null never adds a duplicate row."""

    def test_null_native_ordinals_deduplicate_across_resyncs(self):
        from agent_observer.adapters.codex import import_codex_file
        first = import_codex_file(
            self.con, fixture("codex-null-ordinal.jsonl"))
        self.assertEqual(first["malformed"], 2)
        self.assertEqual(first["responses_inserted"], 1)
        errors = self.query(
            "SELECT ordinal_num, error, line_excerpt FROM import_errors"
            " ORDER BY ordinal_num")
        self.assertEqual(
            [(r["ordinal_num"], r["error"]) for r in errors],
            [(1, "unsupported_schema"), (2, "malformed_usage")])
        for row in errors:
            self.assertIsNotNone(row["ordinal_num"])
        # A forced full re-read under the same version adds no rows.
        import_codex_file(
            self.con, fixture("codex-null-ordinal.jsonl"), full=True)
        again = self.query(
            "SELECT ordinal_num, error, line_excerpt FROM import_errors"
            " ORDER BY ordinal_num")
        self.assertEqual(
            [(r["ordinal_num"], r["error"], r["line_excerpt"])
             for r in again],
            [(r["ordinal_num"], r["error"], r["line_excerpt"])
             for r in errors])

    def test_none_ordinal_is_null_safe_directly(self):
        import os
        import tempfile
        edge = os.path.join(tempfile.gettempdir(),
                            "agent-observer-null-ordinal-edge.jsonl")
        with open(edge, "w") as fh:
            fh.write("{}\n")
        try:
            src = JsonlSource(self.con, "codex", edge)
            list(src.records())
            src.error(None, "schema_error", '{"type": "x"}')
            src.error(None, "schema_error", '{"type": "x"}')
            self.con.commit()
            rows = self.query(
                "SELECT ordinal_num, error FROM import_errors"
                " WHERE source_path=?", (edge,))
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["ordinal_num"])
            self.assertEqual(rows[0]["error"], "schema_error")
        finally:
            os.remove(edge)


class PrivacyVersionTest(LedgerCase):
    LEAKY = {
        "submission_excerpt": staticmethod(
            lambda text, **kw: text[:300] if isinstance(text, str) else ""),
        "assistant_excerpt": staticmethod(
            lambda text: text[-400:] if isinstance(text, str) else ""),
        "filter_detail": staticmethod(
            lambda family, detail: dict(detail)
            if isinstance(detail, dict) else {}),
        "error_category": staticmethod(lambda value: "import_error"),
        "line_excerpt": staticmethod(
            lambda line: (line or "")[:200] if isinstance(line, str) else ""),
    }

    def _counts(self):
        return {
            table: self.query(f"SELECT COUNT(*) n FROM {table}")[0]["n"]
            for table in ("submissions", "events", "import_errors",
                          "responses", "sources")}

    def _import_both(self):
        first = import_codex_file(self.con, fixture("codex-privacy.jsonl"))
        second = claude.import_claude_file(
            self.con, fixture("claude-privacy.jsonl"))
        return first, second

    def test_version_bump_corrects_rows_in_place(self):
        with mock.patch.object(privacy, "PRIVACY_VERSION", 0), \
                mock.patch.object(privacy, "submission_excerpt",
                                  self.LEAKY["submission_excerpt"]), \
                mock.patch.object(privacy, "assistant_excerpt",
                                  self.LEAKY["assistant_excerpt"]), \
                mock.patch.object(privacy, "filter_detail",
                                  self.LEAKY["filter_detail"]), \
                mock.patch.object(privacy, "error_category",
                                  self.LEAKY["error_category"]), \
                mock.patch.object(privacy, "line_excerpt",
                                  self.LEAKY["line_excerpt"]):
            self._import_both()
        # The old rules leaked: the sensitive suite would catch it.
        leaked = "".join(r["text_excerpt"] or "" for r in self.query(
            "SELECT text_excerpt FROM submissions"))
        self.assertIn("SECRET-PRIV-CODEX-TAG-aaaa1111", leaked)
        versions = {r["privacy_version"] for r in self.query(
            "SELECT privacy_version FROM sources")}
        self.assertEqual(versions, {0})
        before = self._counts()
        # Poison a native title the way a pre-fix import kept one.
        self.con.execute(
            "UPDATE sessions SET title='old native title'"
            " WHERE session_key='claude:sess-privacy'")
        self.con.commit()

        first, second = self._import_both()

        # Rows are corrected in place, never duplicated.
        self.assertEqual(self._counts(), before)
        scan_secrets(self)
        excerpt = self.query(
            "SELECT text_excerpt FROM submissions"
            " WHERE native_id='codex:msg-priv-sub-01'")[0]["text_excerpt"]
        self.assertEqual(excerpt, GENUINE_EXCERPT)
        categories = sorted(
            r["error"] for r in self.query("SELECT error FROM import_errors"))
        self.assertEqual(categories, ["malformed_json", "missing_id",
                                      "unknown_record", "unsupported_schema",
                                      "unsupported_schema"])
        for row in self.query("SELECT line_excerpt FROM import_errors"):
            self.assertNotIn("SECRET", row["line_excerpt"] or "")
        title = self.query(
            "SELECT title FROM sessions"
            " WHERE session_key='claude:sess-privacy'")[0]["title"]
        self.assertIsNone(title)
        versions = {r["privacy_version"] for r in self.query(
            "SELECT privacy_version FROM sources")}
        self.assertEqual(versions, {privacy.PRIVACY_VERSION})
        self.assertGreaterEqual(
            first.get("submissions_updated", 0)
            + second.get("submissions_updated", 0), 1)
        self.assertGreaterEqual(
            first.get("events_updated", 0)
            + second.get("events_updated", 0), 1)

    def test_same_version_resync_writes_nothing_new(self):
        self._import_both()
        before = self._counts()
        before_errors = sorted(
            (r["error"], r["line_excerpt"], r["ordinal_num"])
            for r in self.query(
                "SELECT error, line_excerpt, ordinal_num FROM import_errors"))
        self._import_both()
        self.assertEqual(self._counts(), before)
        # A forced full re-read under the same version adds no rows either,
        # and never duplicates an import_error.
        import_codex_file(self.con, fixture("codex-privacy.jsonl"), full=True)
        claude.import_claude_file(
            self.con, fixture("claude-privacy.jsonl"), full=True)
        self.assertEqual(self._counts(), before)
        after_errors = sorted(
            (r["error"], r["line_excerpt"], r["ordinal_num"])
            for r in self.query(
                "SELECT error, line_excerpt, ordinal_num FROM import_errors"))
        self.assertEqual(after_errors, before_errors)


class IdentityMigrationTest(LedgerCase):
    """A privacy-version re-import replaces source-owned identity fields,
    clearing omitted or invalid values instead of COALESCE-keeping them.
    Staleness is inferred centrally from the session's source rows, never
    from an adapter flag."""

    IDENTITY_COLS = ("agentsmd_version", "instructions_sha256",
                     "preferences_sha256", "direction_status",
                     "identity_json", "project_dir", "git_branch", "title")

    def _identity_row(self, con, key):
        return con.execute(
            "SELECT agentsmd_version, instructions_sha256,"
            " preferences_sha256, direction_status, identity_json,"
            " project_dir, git_branch, title FROM sessions"
            " WHERE session_key=?", (key,)).fetchone()

    def _counts(self):
        return {
            table: self.query(f"SELECT COUNT(*) n FROM {table}")[0]["n"]
            for table in ("sessions", "submissions", "events",
                          "import_errors", "responses", "sources")}

    def _error_keys(self):
        return sorted(
            (r["harness"], r["source_path"], r["ordinal_num"], r["error"],
             r["line_excerpt"]) for r in self.query(
                "SELECT harness, source_path, ordinal_num, error,"
                " line_excerpt FROM import_errors"))

    def test_stale_reimport_clears_old_identity_content(self):
        import json as _json
        # End-to-end through the real source/version path: import a real
        # rollout, poison its session with pre-sanitization identity and a
        # native title, mark the source stale, then re-sync.
        import_codex_file(self.con, fixture("codex-mini.jsonl"))
        key = self.query(
            "SELECT session_key FROM sessions"
            " WHERE session_key LIKE 'codex:%' LIMIT 1")[0]["session_key"]
        old_inst = "a" * 64
        self.con.execute(
            "UPDATE sessions SET agentsmd_version='99.9.9',"
            " instructions_sha256=?, preferences_sha256=?,"
            " direction_status='ready', project_dir='/old/repo',"
            " title='old native title', identity_json=?"
            " WHERE session_key=?",
            (old_inst, "b" * 64,
             _json.dumps({"direction_status": "ready",
                          "instructions_sha256": old_inst,
                          "instructions_path": "/old/AGENTS.md",
                          "repository_root": "/old/repo",
                          "git_head": "c" * 40}), key))
        self.con.execute(
            "UPDATE sources SET privacy_version=0 WHERE harness='codex'")
        self.con.commit()
        before = self._counts()
        errors_before = self._error_keys()
        import_codex_file(self.con, fixture("codex-mini.jsonl"), full=True)
        # Rows are corrected in place, never duplicated, and import
        # errors are replaced rather than duplicated.
        self.assertEqual(self._counts(), before)
        self.assertEqual(self._error_keys(), errors_before)
        row = self._identity_row(self.con, key)
        # Every old identity value is removed: no old hash, path, status
        # or title survives, whatever the fixture re-derives.
        blob = _json.dumps([row["agentsmd_version"],
                            row["instructions_sha256"],
                            row["preferences_sha256"],
                            row["direction_status"],
                            row["identity_json"], row["project_dir"]])
        for poison in (old_inst, "b" * 64, "/old/AGENTS.md", "/old/repo",
                       "c" * 40, "99.9.9", "old native title"):
            self.assertNotIn(poison, blob)
        self.assertIsNone(row["title"])
        # The re-imported row matches a fresh import of the same fixture:
        # omitted or invalid old values clear instead of persisting.
        control_path = os.path.join(self.tmp.name, "control.db")
        control = db.connect(control_path)
        try:
            db.init_db(control)
            import_codex_file(control, fixture("codex-mini.jsonl"))
            control.commit()
            fresh = self._identity_row(control, key)
        finally:
            control.close()
        for col in self.IDENTITY_COLS:
            self.assertEqual(row[col], fresh[col], col)
        versions = {r["privacy_version"] for r in self.query(
            "SELECT privacy_version FROM sources")}
        self.assertEqual(versions, {privacy.PRIVACY_VERSION})
        # A current-version re-sync is a true no-op through the fast path.
        again = import_codex_file(self.con, fixture("codex-mini.jsonl"))
        self.assertTrue(again.get("unchanged"))
        self.assertEqual(self._counts(), before)
        self.assertEqual(self._error_keys(), errors_before)

    def test_stale_detected_beyond_the_single_selected_source(self):
        # Central inference covers every source row behind the session:
        # staleness in an associated source counts even when the
        # incoming source_id itself is current.
        import_codex_file(self.con, fixture("codex-mini.jsonl"))
        key = self.query(
            "SELECT session_key FROM sessions"
            " WHERE session_key LIKE 'codex:%' LIMIT 1")[0]["session_key"]
        native = key.split(":", 1)[1]
        current = self.query(
            "SELECT id FROM sources WHERE harness='codex' LIMIT 1"
        )[0]["id"]
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at,"
            " session_id, privacy_version)"
            " VALUES('codex','sibling-path','deadbeef',0,?,0)", (native,))
        sibling = self.con.execute(
            "SELECT id FROM sources WHERE harness='codex'"
            " AND path='sibling-path'").fetchone()["id"]
        self.assertFalse(db.session_sources_stale(
            self.con, key, "codex", "unrelated-native", current))
        self.assertTrue(db.session_sources_stale(
            self.con, key, "codex", native, current))
        # The sibling's events associate it with the session even when the
        # native id lookup is bypassed.
        self.con.execute(
            "INSERT INTO events(source_id, session_key, family, native_id)"
            " VALUES(?,?,?,?)", (sibling, key, "lifecycle", "sibling-evt"))
        self.assertTrue(db.session_sources_stale(
            self.con, key, "codex", "unrelated-native", current))
        # Remove the sibling confounders so the rows below prove their own
        # association path: each must fail without its table's coverage.
        self.con.execute(
            "DELETE FROM events WHERE native_id='sibling-evt'")
        self.con.execute(
            "DELETE FROM sources WHERE path='sibling-path'")
        self.assertFalse(db.session_sources_stale(
            self.con, key, "codex", "unrelated-native", current))
        # A stale source linked only through responses or turns counts
        # too, without relying on sources.session_id or the selected
        # source_id: responses and turns are source-owned rows of the
        # session exactly like events and submissions.
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at,"
            " privacy_version)"
            " VALUES('codex','responses-only-path','deadbeef',0,0)")
        resp_only = self.con.execute(
            "SELECT id FROM sources WHERE harness='codex'"
            " AND path='responses-only-path'").fetchone()["id"]
        self.con.execute(
            "INSERT INTO responses(response_id, source_id, harness,"
            " session_key) VALUES(?,?,?,?)",
            ("codex:resp-only-1", resp_only, "codex", key))
        self.assertTrue(db.session_sources_stale(
            self.con, key, "codex", "unrelated-native", current))
        self.con.execute(
            "DELETE FROM responses WHERE response_id='codex:resp-only-1'")
        self.con.execute(
            "INSERT INTO sources(harness, path, sha256, imported_at,"
            " privacy_version)"
            " VALUES('codex','turns-only-path','deadbeef',0,0)")
        turn_only = self.con.execute(
            "SELECT id FROM sources WHERE harness='codex'"
            " AND path='turns-only-path'").fetchone()["id"]
        self.con.execute(
            "INSERT INTO turns(turn_id, source_id, session_key)"
            " VALUES(?,?,?)", ("turn-only-1", turn_only, key))
        self.assertTrue(db.session_sources_stale(
            self.con, key, "codex", "unrelated-native", current))
        self.con.execute("DELETE FROM turns WHERE turn_id='turn-only-1'")
        # The session row's own stored source_id counts on its own: a
        # session whose row points at a stale source is stale even with a
        # current incoming source and no other linked rows.
        db.upsert_session(self.con, "codex:stored1", "codex", "stored1",
                          resp_only)
        self.assertTrue(db.session_sources_stale(
            self.con, "codex:stored1", "codex", "stored1", current))
        db.upsert_session(self.con, "codex:stored2", "codex", "stored2",
                          current)
        self.assertFalse(db.session_sources_stale(
            self.con, "codex:stored2", "codex", "stored2", current))
        # Unknown sessions need no replacement.
        self.assertFalse(db.session_sources_stale(
            self.con, "codex:missing", "codex", native, current))

    def test_normal_import_keeps_fill_unknown(self):
        from agent_observer import db as _db
        _db.upsert_session(self.con, "codex:keep1", "codex", "keep1", None,
                           agentsmd_version="12.1.0",
                           instructions_sha256="d" * 64)
        _db.upsert_session(self.con, "codex:keep1", "codex", "keep1", None,
                           preferences_sha256="e" * 64)
        row = self.query(
            "SELECT agentsmd_version, instructions_sha256,"
            " preferences_sha256 FROM sessions"
            " WHERE session_key='codex:keep1'")[0]
        self.assertEqual(row["agentsmd_version"], "12.1.0")
        self.assertEqual(row["instructions_sha256"], "d" * 64)
        self.assertEqual(row["preferences_sha256"], "e" * 64)


class PrivacyUnitTest(unittest.TestCase):
    def test_submission_excerpt_needs_genuine_main_session(self):
        self.assertEqual(
            privacy.submission_excerpt("hello", is_genuine=False), "")
        self.assertEqual(
            privacy.submission_excerpt("hello", is_genuine=True,
                                       is_main_session=False), "")
        self.assertEqual(privacy.submission_excerpt(
            "Please summarize the project status.", is_genuine=True),
            "Please summarize the project status.")

    def test_submission_excerpt_truncates_without_parsing(self):
        # A quoted '>' inside an attribute still ends the excerpt at '<'.
        self.assertEqual(
            privacy.submission_excerpt(
                'Keep this <a title="quoted > inside"> drop all of this',
                is_genuine=True),
            "Keep this")
        self.assertEqual(
            privacy.submission_excerpt("Keep this <<<drop this",
                                       is_genuine=True),
            "Keep this")
        self.assertEqual(
            privacy.submission_excerpt("Keep this </drop this",
                                       is_genuine=True),
            "Keep this")
        self.assertEqual(
            privacy.submission_excerpt("Keep this <!drop this",
                                       is_genuine=True),
            "Keep this")
        # An unterminated block truncates the same way.
        self.assertEqual(
            privacy.submission_excerpt("Keep this <block never closed",
                                       is_genuine=True),
            "Keep this")
        # Bare '<' before whitespace, digits or end of text is kept,
        # as is a bare '<<' (only '<<<' is a marker).
        self.assertEqual(
            privacy.submission_excerpt("a < b and 3 < 4", is_genuine=True),
            "a < b and 3 < 4")
        self.assertEqual(
            privacy.submission_excerpt("a << b", is_genuine=True), "a << b")
        # Letter detection is Unicode-aware: '<' plus a non-ASCII letter
        # is a tag-like marker, exactly per the spec.
        self.assertEqual(
            privacy.submission_excerpt("Gardez ceci <élan drop",
                                       is_genuine=True),
            "Gardez ceci")

    def test_unicode_numerals_are_not_markers(self):
        # U+2460 is a numeral, not a letter: '<' plus isalpha() is False,
        # so the text is kept whole.
        self.assertFalse("①".isalpha())
        self.assertEqual(
            privacy.submission_excerpt("Total <① item", is_genuine=True),
            "Total <① item")
        self.assertEqual(
            privacy.assistant_excerpt("Total <① item"), "Total <① item")
        self.assertFalse(privacy.contains_marker("Total <① item"))
        # A Unicode letter still marks: '<' plus isalpha() is True.
        self.assertTrue("é".isalpha())
        self.assertEqual(
            privacy.submission_excerpt("Gardez ceci <élan drop",
                                       is_genuine=True),
            "Gardez ceci")
        self.assertEqual(privacy.assistant_excerpt("fini <élan"), "")
        self.assertTrue(privacy.contains_marker("a <é b"))
        self.assertTrue(privacy.contains_marker("a </ b"))
        self.assertTrue(privacy.contains_marker("a <! b"))
        self.assertTrue(privacy.contains_marker("a <<< b"))
        self.assertFalse(privacy.contains_marker("a < b and 3 < 4"))
        self.assertFalse(privacy.contains_marker("a << b"))
        self.assertFalse(privacy.contains_marker(None))

    def test_submission_excerpt_collapses_whitespace_and_caps_length(self):
        self.assertEqual(
            privacy.submission_excerpt("a\n\n  b\tc", is_genuine=True),
            "a b c")
        self.assertEqual(
            len(privacy.submission_excerpt("x" * 500, is_genuine=True)), 300)
        self.assertEqual(privacy.submission_excerpt(
            "", is_genuine=True), "")
        self.assertEqual(privacy.submission_excerpt(
            None, is_genuine=True), "")

    def test_assistant_excerpt_keeps_only_marker_free_tail(self):
        self.assertEqual(privacy.assistant_excerpt("done"), "done")
        long_text = "y" * 500 + "tail"
        self.assertEqual(privacy.assistant_excerpt(long_text), "y" * 396 + "tail")
        self.assertEqual(
            privacy.assistant_excerpt("done <b>bold</b>"), "")
        self.assertEqual(privacy.assistant_excerpt("done <<<hidden"), "")
        self.assertEqual(privacy.assistant_excerpt("open <block never"), "")
        self.assertEqual(privacy.assistant_excerpt(""), "")
        self.assertEqual(privacy.assistant_excerpt(None), "")
        # A marker before the last 400 characters does not taint the span.
        self.assertEqual(
            privacy.assistant_excerpt("<b>old</b> " + "z" * 500),
            "z" * 400)

    def test_error_category_is_closed_with_a_fixed_fallback(self):
        for member in ("malformed_json", "unknown_record", "schema_error",
                       "missing_id", "malformed_usage", "usage_conflict",
                       "source_unreadable", "unsupported_schema"):
            self.assertEqual(privacy.error_category(member), member)
        for other in ("SECRET_SECRET", "secret_secret", "json_error",
                      "schema_error: boom", "", None, 42,
                      "malformed_json "):
            self.assertEqual(privacy.error_category(other),
                             privacy.ERROR_FALLBACK)
        self.assertEqual(privacy.ERROR_FALLBACK, "import_error")

    def test_line_excerpt_holds_only_sorted_key_names(self):
        self.assertEqual(
            privacy.line_excerpt(
                '{"type": "x", "payload": {"secret": 1}, "ordinal": 2}'),
            "ordinal,payload,type")
        self.assertEqual(privacy.line_excerpt('["a"]'), "")
        self.assertEqual(privacy.line_excerpt('"str"'), "")
        self.assertEqual(privacy.line_excerpt("not json"), "")
        self.assertEqual(privacy.line_excerpt(""), "")
        self.assertEqual(privacy.line_excerpt(None), "")
        self.assertLessEqual(
            len(privacy.line_excerpt(
                "{\"" + "\", \"".join(f"k{i:03d}" for i in range(50))
                + "\": 1}")), 200)

    def test_filter_detail_keeps_only_read_keys_with_typed_values(self):
        self.assertEqual(
            privacy.filter_detail(
                "file_change", {"paths": ["/p/b.py", "/p/a.py"],
                                "type": "edit"}),
            {"paths": ["/p/a.py", "/p/b.py"]})
        self.assertEqual(
            privacy.filter_detail(
                "tool_result",
                {"exit_code": 3, "exitCode": "0", "reason": "nope"}),
            {"exit_code": 3})
        self.assertEqual(
            privacy.filter_detail(
                "read",
                {"start_line": 1, "num_lines": 50, "cmd": "cat",
                 "evidence": "prose"}),
            {"start_line": 1, "num_lines": 50, "cmd": "cat"})
        self.assertEqual(
            privacy.filter_detail(
                "skill_read", {"skill": "wayfinder", "start_line": True}),
            {"skill": "wayfinder"})
        self.assertEqual(
            privacy.filter_detail("assistant_message",
                                  {"excerpt": "Done. Tests pass."}),
            {"excerpt": "Done. Tests pass."})
        self.assertEqual(
            privacy.filter_detail("assistant_message",
                                  {"excerpt": "Done <b>x</b>"}), {})
        for family in ("tool_call", "skill_invoke", "compaction",
                       "lifecycle", "permission"):
            self.assertEqual(
                privacy.filter_detail(family, {"anything": 1}), {})
        self.assertEqual(privacy.filter_detail("nope", {"a": 1}), {})
        self.assertEqual(privacy.filter_detail("read", None), {})
        self.assertEqual(privacy.filter_detail("read", "text"), {})

    def test_filter_target_keeps_only_bounded_strings(self):
        self.assertEqual(privacy.filter_target("/p/x.py"), "/p/x.py")
        self.assertEqual(len(privacy.filter_target("c" * 5000)), 500)
        self.assertIsNone(privacy.filter_target({"path": "x"}))
        self.assertIsNone(privacy.filter_target(["x"]))
        self.assertIsNone(privacy.filter_target(42))
        self.assertIsNone(privacy.filter_target(None))

    def test_skill_targets_hold_only_validated_identifiers(self):
        # Finding 1: skill_read and skill_invoke targets accept only a
        # validated native skill identifier (the same complete rule as
        # names). Titles, sentences, paths, whitespace, markers and
        # wrong types fail closed.
        for family in ("skill_read", "skill_invoke"):
            for valid in ("agentsmd:operations", "wayfinder", "my-skill",
                          "mcp__server__tool", "a" * 80,
                          "skills/ops/SKILL.md"):
                self.assertEqual(
                    privacy.filter_target(valid, family=family), valid,
                    (family, valid))
            for bad in ("My Helpful Skill Title", "Only Title Here",
                        "Read `/redacted/repo/skills/ops/SKILL.md`",
                        "/u/.claude/plugins/cache/skills/operations",
                        "/abs/path/SKILL.md", "hello world", "a b",
                        "done <b>x</b>", "a <<< b", "foo\nbar", "",
                        "a" * 81, None, 42, True, ["a"], {"a": 1}):
                self.assertIsNone(
                    privacy.filter_target(bad, family=family), (family, bad))

    def test_non_skill_targets_keep_bounded_string_behavior(self):
        # Compatibility: existing non-skill callers passing no family,
        # or another family, keep the historical behavior.
        self.assertEqual(
            privacy.filter_target("/p/x.py", family="tool_call"), "/p/x.py")
        self.assertEqual(
            privacy.filter_target("cat notes.md", family="tool_result"),
            "cat notes.md")
        self.assertIsNone(privacy.filter_target(42, family="read"))


class SkillTargetTest(LedgerCase):
    """Finding 1 through the Claude adapter: a free-text Skill title
    never persists as target, name, detail or identity; skill_read
    keeps the validated skill identifier, never the directory path."""

    TITLE = "My Helpful Skill Title"

    def _import_skill_use(self, skill_value):
        import json as _json
        import os
        assistant = {
            "sessionId": "sess-skill-target", "cwd": "/redacted/repo",
            "version": "2.1.280", "isSidechain": False, "type": "assistant",
            "uuid": "as-skill", "timestamp": "2026-09-14T10:00:05Z",
            "message": {
                "id": "msg-skill", "model": "claude-fable-5-1",
                "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0, "output_tokens": 5},
                "content": [{"type": "tool_use", "id": "tu-skill-target",
                             "name": "Skill", "input": {"skill": skill_value}}]},
        }
        path = os.path.join(self.tmp.name, "skill-target.jsonl")
        with open(path, "w") as fh:
            fh.write(_json.dumps(assistant) + "\n")
        return claude.import_claude_file(self.con, path)

    def test_free_text_skill_title_persists_nowhere(self):
        self._import_skill_use(self.TITLE)
        rows = self.query(
            "SELECT family, native_id, name, target, detail_json FROM events"
            " WHERE native_id='tu-skill-target'")
        self.assertEqual(len(rows), 2)
        by_family = {r["family"]: r for r in rows}
        self.assertIn("skill_invoke", by_family)
        self.assertIn("tool_call", by_family)
        # The native tool name survives on the tool_call; the free-text
        # skill value survives nowhere: no skill name, no skill target.
        self.assertEqual(by_family["tool_call"]["name"], "Skill")
        self.assertIsNone(by_family["tool_call"]["target"])
        self.assertIsNone(by_family["skill_invoke"]["name"])
        self.assertIsNone(by_family["skill_invoke"]["target"])
        self.assertIsNone(by_family["skill_invoke"]["detail_json"])
        blob = "".join((r["name"] or "") + (r["target"] or "")
                       for r in rows)
        blob += "".join(r["detail_json"] or "" for r in rows)
        blob += "".join(r["identity_json"] or "" for r in self.query(
            "SELECT identity_json FROM sessions"))
        self.assertNotIn(self.TITLE, blob)

    def test_valid_skill_identifier_survives_in_name_and_target(self):
        self._import_skill_use("agentsmd:operations")
        row = self.query(
            "SELECT name, target FROM events WHERE family='skill_invoke'"
            " AND native_id='tu-skill-target'")[0]
        self.assertEqual(row["name"], "agentsmd:operations")
        self.assertEqual(row["target"], "agentsmd:operations")
        call = self.query(
            "SELECT target FROM events WHERE family='tool_call'"
            " AND native_id='tu-skill-target'")[0]
        self.assertEqual(call["target"], "agentsmd:operations")

    def _import_skill_command_use(self, command_value):
        import json as _json
        import os
        assistant = {
            "sessionId": "sess-skill-cmd", "cwd": "/redacted/repo",
            "version": "2.1.280", "isSidechain": False, "type": "assistant",
            "uuid": "as-skill-cmd", "timestamp": "2026-09-14T10:00:05Z",
            "message": {
                "id": "msg-skill-cmd", "model": "claude-fable-5-1",
                "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0, "output_tokens": 5},
                "content": [{"type": "tool_use", "id": "tu-skill-cmd",
                             "name": "Skill",
                             "input": {"command": command_value}}]},
        }
        result = {
            "sessionId": "sess-skill-cmd", "cwd": "/redacted/repo",
            "version": "2.1.280", "isSidechain": False, "type": "user",
            "uuid": "u-skill-cmd", "timestamp": "2026-09-14T10:00:06Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu-skill-cmd",
                 "content": "Launching skill"}]},
        }
        path = os.path.join(self.tmp.name, "skill-cmd.jsonl")
        with open(path, "w") as fh:
            fh.write(_json.dumps(assistant) + "\n")
            fh.write(_json.dumps(result) + "\n")
        return claude.import_claude_file(self.con, path)

    def test_title_in_command_field_reaches_no_event_target(self):
        self._import_skill_command_use(self.TITLE)
        rows = self.query(
            "SELECT family, native_id, name, target, detail_json FROM events"
            " WHERE native_id='tu-skill-cmd'")
        self.assertEqual(len(rows), 3)
        by_family = {r["family"]: r for r in rows}
        self.assertIn("skill_invoke", by_family)
        self.assertIn("tool_call", by_family)
        self.assertIn("tool_result", by_family)
        # The command field is validated as a skill identifier before any
        # path or command handling, so the title fails closed everywhere.
        self.assertIsNone(by_family["skill_invoke"]["name"])
        self.assertIsNone(by_family["skill_invoke"]["target"])
        self.assertIsNone(by_family["tool_call"]["target"])
        self.assertIsNone(by_family["tool_result"]["target"])
        blob = "".join((r["name"] or "") + (r["target"] or "")
                       for r in rows)
        blob += "".join(r["detail_json"] or "" for r in rows)
        self.assertNotIn(self.TITLE, blob)

    def test_valid_identifier_in_command_field_stays_intact(self):
        self._import_skill_command_use("agentsmd:operations")
        rows = {r["family"]: r for r in self.query(
            "SELECT family, name, target FROM events"
            " WHERE native_id='tu-skill-cmd'")}
        self.assertEqual(rows["skill_invoke"]["name"], "agentsmd:operations")
        self.assertEqual(rows["skill_invoke"]["target"], "agentsmd:operations")
        self.assertEqual(rows["tool_call"]["target"], "agentsmd:operations")
        self.assertEqual(rows["tool_result"]["target"], "agentsmd:operations")

    def test_skill_read_keeps_the_identifier_never_the_directory(self):
        claude.sync(self.con,
                    root=os.path.join(FIXTURES, "claude"))
        rows = self.query(
            "SELECT name, target, detail_json FROM events"
            " WHERE family='skill_read'")
        self.assertTrue(rows)
        import json as _json
        for row in rows:
            self.assertEqual(row["name"], "operations")
            self.assertEqual(row["target"], "operations")
            detail = _json.loads(row["detail_json"])
            self.assertEqual(detail.get("skill"), "operations")
            # The installed path lives only in detail skill_path, never
            # in the target.
            self.assertIn("skill_path", detail)
            self.assertIn("/skills/operations/SKILL.md",
                          detail["skill_path"])
            self.assertNotIn("/", row["target"] or "")


class CodexSkillTargetTest(LedgerCase):
    """Finding 1 through the Codex adapter: skill_read target holds only
    the validated skill identifier derived from the Skill directory name;
    the installed path lives only in detail skill_path."""

    def _write_rollout(self, path_value):
        import json as _json
        import os
        lines = [
            {"ordinal": 0,
             "payload": {"session_id": "sess-codex-skill",
                         "thread_source": "user",
                         "cli_version": "0.155.0"},
             "timestamp": "2026-09-15T14:00:00.000Z",
             "type": "session_meta"},
            {"ordinal": 1,
             "payload": {"item": {
                 "command": ["/bin/zsh", "-lc", f"cat {path_value}"],
                 "cwd": "file:///redacted/workspace",
                 "exit_code": 0,
                 "id": "exec-skill-001",
                 "parsed_cmd": [{"cmd": f"cat {path_value}",
                                 "name": "SKILL.md",
                                 "path": path_value,
                                 "type": "read"}],
                 "source": "unified_exec_startup",
                 "status": "completed",
                 "stdout": "skill body",
                 "type": "CommandExecution"},
                 "type": "item_completed"},
             "timestamp": "2026-09-15T14:00:01.000Z",
             "type": "event_msg"},
        ]
        path = os.path.join(self.tmp.name, "codex-skill.jsonl")
        with open(path, "w") as fh:
            for obj in lines:
                fh.write(_json.dumps(obj) + "\n")
        return path

    def test_skill_read_target_is_identifier_path_only_in_detail(self):
        path = self._write_rollout("skills/wayfinder/SKILL.md")
        import_codex_file(self.con, path)
        rows = self.query(
            "SELECT family, name, target, detail_json FROM events"
            " WHERE family='skill_read'")
        self.assertEqual(len(rows), 1)
        import json as _json
        row = rows[0]
        self.assertEqual(row["target"], "wayfinder")
        detail = _json.loads(row["detail_json"] or "{}")
        self.assertEqual(detail.get("skill"), "wayfinder")
        self.assertEqual(detail.get("skill_path"),
                         "skills/wayfinder/SKILL.md")
        self.assertNotIn("/", row["target"] or "")

    def test_invalid_skill_path_drops_identifier_keeps_safe_path(self):
        # A free-text title cannot become a skill identifier; the target
        # stays None while the raw path still survives only in detail
        # when it is marker-safe, and markers fail closed.
        import json as _json
        path = self._write_rollout("/tmp/skills/evil title/SKILL.md")
        import_codex_file(self.con, path)
        rows = self.query(
            "SELECT target, detail_json FROM events"
            " WHERE family='skill_read'")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["target"])
        detail = _json.loads(rows[0]["detail_json"] or "{}")
        # No validated identifier survives.
        self.assertNotIn("skill", detail)
        # The marker-safe path still survives in detail; free-text titles
        # never reach name/target.
        self.assertEqual(detail.get("skill_path"),
                         "/tmp/skills/evil title/SKILL.md")

    def test_stale_reimport_corrects_skill_target_in_place(self):
        path = self._write_rollout("skills/wayfinder/SKILL.md")
        import_codex_file(self.con, path)
        before = self.query("SELECT COUNT(*) n FROM events")[0]["n"]
        self.assertGreater(before, 0)
        # Poison the row the way a pre-fix import kept the installed path
        # as the target with no detail path.
        self.con.execute(
            "UPDATE events SET target='skills/wayfinder/SKILL.md',"
            " detail_json='{\"cmd\": \"cat\"}'"
            " WHERE family='skill_read'")
        self.con.execute(
            "UPDATE sources SET privacy_version=0 WHERE harness='codex'")
        self.con.commit()
        stats = import_codex_file(self.con, path, full=True)
        self.assertGreaterEqual(stats.get("events_updated", 0), 1)
        import json as _json
        row = self.query(
            "SELECT target, detail_json FROM events"
            " WHERE family='skill_read'")[0]
        self.assertEqual(row["target"], "wayfinder")
        detail = _json.loads(row["detail_json"] or "{}")
        self.assertEqual(detail.get("skill_path"),
                         "skills/wayfinder/SKILL.md")
        self.assertEqual(
            self.query("SELECT COUNT(*) n FROM events")[0]["n"], before)


class SkillPathDetailTest(unittest.TestCase):
    """Finding 1 privacy unit: skill file paths survive only in detail
    skill_path, fail closed on markers, types and bounds."""

    def test_skill_path_keeps_marker_free_paths_only(self):
        self.assertEqual(
            privacy.filter_detail(
                "skill_read",
                {"skill": "wayfinder",
                 "skill_path": "/tmp/skills/wayfinder/SKILL.md"}),
            {"skill": "wayfinder",
             "skill_path": "/tmp/skills/wayfinder/SKILL.md"})
        self.assertEqual(
            privacy.filter_detail(
                "skill_invoke",
                {"skill_path": "/tmp/skills/my-skill/SKILL.md"}),
            {"skill_path": "/tmp/skills/my-skill/SKILL.md"})
        for bad in ("", None, 42, True, ["x"], {"x": 1},
                    "/p/<b>SKILL.md", "/p/<<<SKILL.md", "x" * 501):
            self.assertNotIn(
                "skill_path",
                privacy.filter_detail("skill_read", {"skill_path": bad}),
                repr(bad))
            self.assertNotIn(
                "skill_path",
                privacy.filter_detail("skill_invoke", {"skill_path": bad}),
                repr(bad))
