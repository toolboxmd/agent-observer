"""Resync performance and Grok MCP lifecycle regressions.

Codex unchanged re-sync must be cheap while accounting stays exact; the
reconciliation index must exist for new and migrated ledgers; every benign
Grok MCP lifecycle shape must be known and store nothing.
"""

import json
import os
import shutil

from agent_observer import db, report
from agent_observer.adapters import grok
from agent_observer.adapters.codex import import_codex_file
from tests.helpers import FIXTURES, LedgerCase, fixture

GROK_ROOT = os.path.join(FIXTURES, "grok")
GROK_SID = "01fixture1-aaaa-4b5c-8d6e-000000000001"
GROK_KEY = f"grok:{GROK_SID}"


class CodexUnchangedSkipsReconciliationTest(LedgerCase):
    """An unchanged re-sync performs no reconciliation queries."""

    def test_unchanged_second_import_runs_no_reconciliation_sql(self):
        first = self.sync("codex-legacy-a.jsonl")
        self.assertEqual(first["responses_inserted"], 2)
        self.assertFalse(first.get("unchanged"))
        before_rows = self.query(
            "SELECT response_id, total_tokens, thread_total_tokens,"
            " is_overlap FROM responses ORDER BY thread_total_tokens")
        before_totals = report.scope_totals(self.con)

        sql_log: list = []
        self.con.set_trace_callback(sql_log.append)
        try:
            second = import_codex_file(
                self.con, fixture("codex-legacy-a.jsonl"))
        finally:
            self.con.set_trace_callback(None)

        self.assertTrue(second.get("unchanged"))
        self.assertEqual(second["responses_inserted"], 0)
        self.assertEqual(second["malformed"], 0)
        self.assertEqual(first["sha256"], second["sha256"])
        # The full reconciliation UPDATE (correlated EXISTS over
        # responses) must not run on the unchanged path. A plausible
        # slow implementation that still calls _reconcile_fallback fails
        # here because its UPDATE mentions is_overlap.
        recon = [s for s in sql_log
                 if "is_overlap" in s and "UPDATE" in s.upper()]
        self.assertEqual(recon, [])
        # Accounting is unchanged: same rows, same totals.
        after_rows = self.query(
            "SELECT response_id, total_tokens, thread_total_tokens,"
            " is_overlap FROM responses ORDER BY thread_total_tokens")
        self.assertEqual([dict(r) for r in after_rows],
                         [dict(r) for r in before_rows])
        self.assertEqual(report.scope_totals(self.con), before_totals)
        self.assertEqual(before_totals["total_tokens"], 450 + 670)
        self.assertEqual(before_totals["responses"], 2)


class ReconcileIndexMigrationTest(LedgerCase):
    """The reconciliation index exists for new and migrated ledgers."""

    def test_index_exists_and_covers_reconciliation_predicates(self):
        row = self.query(
            "SELECT sql FROM sqlite_master WHERE type='index'"
            " AND name='idx_responses_reconcile'")
        self.assertEqual(len(row), 1)
        sql = row[0]["sql"] or ""
        for col in ("session_key", "semantics", "is_overlap",
                    "thread_total_tokens", "total_tokens"):
            self.assertIn(col, sql)

    def test_existing_ledger_without_index_gains_it_on_init(self):
        self.con.execute("DROP INDEX IF EXISTS idx_responses_reconcile")
        self.con.commit()
        missing = self.query(
            "SELECT 1 FROM sqlite_master WHERE type='index'"
            " AND name='idx_responses_reconcile'")
        self.assertEqual(len(missing), 0)
        db.init_db(self.con)
        present = self.query(
            "SELECT 1 FROM sqlite_master WHERE type='index'"
            " AND name='idx_responses_reconcile'")
        self.assertEqual(len(present), 1)


class GrokMcpLifecycleTest(LedgerCase):
    """Every benign MCP shape is known; unknown shapes stay unknown_record."""

    def _isolated_con(self, name):
        path = os.path.join(self.tmp.name, f"{name}.db")
        con = db.connect(path)
        db.init_db(con)
        return con

    def _all_text_values(self, con):
        found = []
        tables = [r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            cols = con.execute(f"PRAGMA table_info({table})").fetchall()
            text_cols = [c["name"] for c in cols if c["type"] == "TEXT"]
            for col in text_cols:
                for row in con.execute(
                        f'SELECT "{col}" v FROM "{table}" WHERE "{col}"'
                        " IS NOT NULL"):
                    found.append((table, col, row["v"] or ""))
        return found

    def test_every_benign_mcp_shape_is_ignored(self):
        tmp = os.path.join(self.tmp.name, "mcpbenign")
        shutil.copytree(GROK_ROOT, tmp)
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", GROK_SID)
        benign = [
            {"ts": "2026-09-01T10:01:01Z", "type": "mcp_server_starting",
             "server_name": "MCP-SENTINEL-server-aaa",
             "target": "MCP-SENTINEL-target-bbb",
             "timeout_sec": 5, "transport": "MCP-SENTINEL-transport-ccc"},
            {"ts": "2026-09-01T10:01:02Z", "type": "mcp_config_resolved",
             "disabled": False, "servers": ["MCP-SENTINEL-servers-ddd"]},
            {"ts": "2026-09-01T10:01:03Z", "type": "mcp_server_connected",
             "duration_ms": 12,
             "server_name": "MCP-SENTINEL-server-eee",
             "tool_count": 3, "tools": ["MCP-SENTINEL-tool-fff"],
             "transport": "MCP-SENTINEL-transport-ggg"},
            {"ts": "2026-09-01T10:01:04Z", "type": "mcp_init_completed",
             "auth_required": False, "duration_ms": 20, "failed": 0,
             "is_reinit": False, "succeeded": 2, "total_servers": 2,
             "total_tools": 5},
            {"ts": "2026-09-01T10:01:05Z", "type": "mcp_init_completed",
             "auth_required": True, "duration_ms": 21, "failed": 1,
             "failed_servers": ["MCP-SENTINEL-failed-hhh"],
             "is_reinit": True, "succeeded": 1, "total_servers": 2,
             "total_tools": 4},
            {"ts": "2026-09-01T10:01:06Z", "type": "mcp_server_failed",
             "duration_ms": 8,
             "error_message": "MCP-SENTINEL-err-msg-iii",
             "error_type": "MCP-SENTINEL-err-type-jjj",
             "server_name": "MCP-SENTINEL-server-kkk",
             "target": "MCP-SENTINEL-target-lll",
             "timeout_sec": 5, "transport": "MCP-SENTINEL-transport-mmm"},
            # Existing benign noise stays benign.
            {"ts": "2026-09-01T10:01:07Z", "type": "phase_changed"},
            {"ts": "2026-09-01T10:01:08Z", "type": "loop_started"},
            {"ts": "2026-09-01T10:01:09Z", "type": "first_token"},
        ]
        with open(os.path.join(sdir, "events.jsonl"), "a") as fh:
            for obj in benign:
                fh.write(json.dumps(obj) + "\n")
        con = self._isolated_con("mcpbenign")
        stats = grok.sync(con, root=tmp)
        # No new import error for any benign shape.
        errors = list(con.execute("SELECT error FROM import_errors"))
        mcp_errors = [r for r in errors
                      if (r["error"] or "") == "unknown_record"]
        # The fixture itself carries one malformed updates line; no MCP
        # shape may add to it.
        self.assertEqual(len(mcp_errors), 0)
        # No MCP value reaches any ledger text column.
        sentinels = ("MCP-SENTINEL-server-aaa", "MCP-SENTINEL-target-bbb",
                     "MCP-SENTINEL-transport-ccc", "MCP-SENTINEL-servers-ddd",
                     "MCP-SENTINEL-server-eee", "MCP-SENTINEL-tool-fff",
                     "MCP-SENTINEL-transport-ggg", "MCP-SENTINEL-failed-hhh",
                     "MCP-SENTINEL-err-msg-iii", "MCP-SENTINEL-err-type-jjj",
                     "MCP-SENTINEL-server-kkk", "MCP-SENTINEL-target-lll",
                     "MCP-SENTINEL-transport-mmm")
        for table, col, val in self._all_text_values(con):
            for sentinel in sentinels:
                self.assertNotIn(sentinel, val,
                                 f"{table}.{col} stores MCP value")
        # No MCP type name becomes an event either.
        names = {r["name"] for r in con.execute(
            "SELECT name FROM events WHERE session_key=?", (GROK_KEY,))}
        for mcp_type in ("mcp_server_starting", "mcp_config_resolved",
                         "mcp_server_connected", "mcp_init_completed",
                         "mcp_server_failed"):
            self.assertNotIn(mcp_type, names)
        con.close()
        self.assertGreaterEqual(stats["sources"], 1)

    def test_unknown_mcp_shape_stays_unknown_record_without_values(self):
        tmp = os.path.join(self.tmp.name, "mcpunknown")
        shutil.copytree(GROK_ROOT, tmp)
        sdir = os.path.join(tmp, "%2Fredacted%2Frepo", GROK_SID)
        evil_server = "MCP-EVIL-server-xyz sk-fake-secret-mcp-111"
        evil_extra = "MCP-EVIL-extra-zzz should never persist"
        # Known type with an unrecognized extra key.
        unknown = {"ts": "2026-09-01T10:02:01Z",
                   "type": "mcp_server_starting",
                   "server_name": evil_server,
                   "target": "/redacted/repo/mcp",
                   "timeout_sec": 5, "transport": "stdio",
                   "extra_evil": evil_extra}
        with open(os.path.join(sdir, "events.jsonl"), "a") as fh:
            fh.write(json.dumps(unknown) + "\n")
        con = self._isolated_con("mcpunknown")
        grok.sync(con, root=tmp)
        unknowns = list(con.execute(
            "SELECT error, line_excerpt FROM import_errors"
            " WHERE error='unknown_record'"))
        self.assertTrue(unknowns)
        # The unknown MCP shape keeps only the fixed category and the
        # sorted top-level key names, never values.
        match = [r for r in unknowns
                 if "extra_evil" in (r["line_excerpt"] or "")]
        self.assertTrue(match)
        row = match[0]
        self.assertEqual(row["error"], "unknown_record")
        self.assertEqual(
            row["line_excerpt"],
            "extra_evil,server_name,target,timeout_sec,transport,ts,type")
        self.assertNotIn(evil_server, row["line_excerpt"] or "")
        self.assertNotIn(evil_extra, row["line_excerpt"] or "")
        self.assertNotIn(evil_server, row["error"] or "")
        for table, col, val in self._all_text_values(con):
            self.assertNotIn(evil_server, val,
                             f"{table}.{col} leaks MCP value")
            self.assertNotIn("sk-fake-secret-mcp-111", val,
                             f"{table}.{col} leaks MCP secret")
            self.assertNotIn(evil_extra, val,
                             f"{table}.{col} leaks MCP value")
            self.assertNotIn("mcp_server_starting", val,
                             f"{table}.{col} leaks MCP type value")
        con.close()
