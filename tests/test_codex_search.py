"""Native Extension/web.search evidence remains indexed and hydratable (#94)."""

import json
import os

from agent_observer import db, lookup
from agent_observer.adapters.codex import import_codex_file
from tests.helpers import LedgerCase
from tests.test_codex_fork import _import_at_revision


class CodexSearchTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp.name, "search.jsonl")
        self.item = {
            "type": "Extension", "kind": "web.search", "id": "exec-search",
            "query": "example reference", "action": {"type": "search"},
            "results": [{"title": "Example", "url": "https://example.org/",
                         "snippet": "Native search evidence"}],
        }

    def write(self, *records):
        rows = [{"type": "session_meta", "payload": {
            "id": "search-session", "thread_source": "user"}}, *records]
        with open(self.path, "w") as fh:
            for ordinal, row in enumerate(rows):
                fh.write(json.dumps({**row, "ordinal": ordinal * 10}) + "\n")

    def completed(self, item=None):
        return {"type": "event_msg", "payload": {
            "type": "item_completed", "item": self.item if item is None else item}}

    def assert_search(self):
        rows = self.query("SELECT * FROM events")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["session_key"], row["family"], row["native_id"]),
                         ("codex:search-session", "tool_result", "exec-search"))
        shown = lookup.show(self.con, [row["id"]])[0]
        self.assertEqual(shown["status"], "ok")
        self.assertEqual(shown["tool"], "web_search")
        self.assertEqual(shown["input"]["query"], self.item["query"])
        self.assertEqual(shown["input"]["action"], self.item["action"])
        result = shown["result"]
        if isinstance(result, str):
            result = json.loads(result)
        self.assertEqual(result, self.item["results"])
        return row["id"]

    def test_native_search_survives_ordinary_full_and_repeat_imports(self):
        self.write(self.completed())
        import_codex_file(self.con, self.path)
        event_id = self.assert_search()
        row = self.query("SELECT * FROM events")[0]
        self.assertEqual(row["name"], "web_search")
        self.assertEqual(row["ordinal_num"], 1)
        # Search content stays in the native file, never the ledger.
        self.assertNotIn(self.item["query"], "\n".join(self.con.iterdump()))
        self.assertNotIn(self.item["results"][0]["snippet"],
                         "\n".join(self.con.iterdump()))
        for full in (False, True, False, True):
            import_codex_file(self.con, self.path, full=full)
            self.assertEqual(self.assert_search(), event_id)

    def test_distinct_native_ids_survive_incremental_append(self):
        self.write(self.completed())
        import_codex_file(self.con, self.path)
        first_id = self.assert_search()
        with open(self.path, "a") as fh:
            fh.write(json.dumps(self.completed({**self.item, "id": "exec-second"})) + "\n")
        stats = import_codex_file(self.con, self.path)
        self.assertTrue(stats["incremental"])
        before = [tuple(r) for r in self.query(
            "SELECT id,native_id FROM events ORDER BY id")]
        self.assertEqual(before[0], (first_id, "exec-search"))
        self.assertEqual([r[1] for r in before], ["exec-search", "exec-second"])
        for full in (False, True):
            import_codex_file(self.con, self.path, full=full)
            self.assertEqual([tuple(r) for r in self.query(
                "SELECT id,native_id FROM events ORDER BY id")], before)

    def test_equivalent_response_output_does_not_duplicate_search(self):
        output = {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "exec-search",
            "output": json.dumps(self.item["results"])}}
        for records in ((self.completed(), output), (output, self.completed())):
            with self.subTest(wrapper_first=records[0]["type"] == "event_msg"):
                self.con.execute("DELETE FROM events")
                self.write(*records)
                import_codex_file(self.con, self.path, full=True)
                event_id = self.assert_search()
                import_codex_file(self.con, self.path, full=True)
                self.assertEqual(self.assert_search(), event_id)

    def test_unknown_extension_is_quarantined_without_losing_search(self):
        unknown = {"type": "Extension", "id": "unknown", "kind": "future.kind"}
        self.write(self.completed(unknown), self.completed())
        stats = import_codex_file(self.con, self.path)
        self.assertEqual(stats["malformed"], 1)
        self.assertEqual(self.query("SELECT error FROM import_errors")[0][0],
                         "unsupported_schema")
        self.assert_search()

    def test_search_without_native_id_is_quarantined(self):
        item = {k: v for k, v in self.item.items() if k != "id"}
        self.write(self.completed(item))
        stats = import_codex_file(self.con, self.path)
        self.assertEqual(stats["malformed"], 1)
        self.assertEqual(self.query("SELECT error FROM import_errors")[0][0],
                         "missing_id")
        self.assertEqual(self.query("SELECT count(*) FROM events")[0][0], 0)

    def test_ordinary_sync_repairs_v122_source_once(self):
        self.write(self.completed())
        self.con.close()
        _import_at_revision("a42c729c531e0d6580a59a77d7d824a9197fa7f7",
                            self.db_path, self.path)
        self.con = db.connect(self.db_path)
        db.init_db(self.con)
        self.assertEqual(self.query("SELECT count(*) FROM events")[0][0], 0)
        stats = import_codex_file(self.con, self.path)
        self.assertFalse(stats.get("unchanged", False))
        event_id = self.assert_search()
        stats = import_codex_file(self.con, self.path)
        self.assertTrue(stats["unchanged"])
        self.assertEqual(self.assert_search(), event_id)
