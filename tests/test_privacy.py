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

from agent_observer import db, privacy
from agent_observer.adapters import claude
from agent_observer.adapters.codex import import_codex_file
from agent_observer.ingest import JsonlSource, insert_event
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
