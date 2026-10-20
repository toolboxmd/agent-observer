"""Regression from bestia ledger event 363608 (native exit-8 command).

The fixture retains the command, status, exit code and duration. Session and
item identities are replaced; cwd, process id and all output are omitted.
"""

import json
from pathlib import Path

from agent_observer import analysis, db, pricing, report
from agent_observer.adapters.codex import import_codex_file
from agent_observer.command_status import pending_pr_checks
from tests.helpers import LedgerCase, fixture


class PendingChecksTest(LedgerCase):
    def commands(self, commands):
        rows = [json.loads(line) for line in Path(
            fixture("codex-pr-checks-pending.jsonl")).read_text().splitlines()]
        path = Path(self.tmp.name) / "pending.jsonl"
        events = [rows[0]]
        for index, (command, code) in enumerate(commands):
            event = json.loads(json.dumps(rows[1]))
            item = event["payload"]["item"]
            item.update(id=f"check-{index}", command=["/usr/bin/bash", "-lc", command],
                        exit_code=code)
            events.append(event)
        path.write_text("".join(json.dumps(row) + "\n" for row in events))
        import_codex_file(self.con, str(path))
        return path

    def test_real_exit8_is_pending_and_not_a_failure(self):
        command = "gh pr checks 353 --repo toolboxmd/chromeria"
        self.commands([(command, 8)] * 2)
        events = self.query("SELECT * FROM events WHERE family='tool_result'")
        self.assertEqual(len(events), 2)
        self.assertEqual([e["status"] for e in events], ["pending", "pending"])
        self.assertFalse(any(analysis._failed(e) for e in events))
        self.assertFalse(any(analysis._passed(e) for e in events))
        self.sync("codex-mini.jsonl")
        usage = dict(self.query("SELECT * FROM responses LIMIT 1")[0])
        usage["session_key"] = "codex:pr-checks-pending"
        phases, _, _ = report._phase_report(
            self.con, "unbound-task", [usage], {usage["session_key"]}, [],
            pricing.load_schedule())
        self.assertEqual(phases[0]["activity"]["failed_tool_results"], 0)
        result = analysis.diagnose(self.con, detectors=["repeated_failure"])
        self.assertEqual(result["counts"].get("repeated_failure", 0), 0)

    def test_other_nonzero_exits_still_fail(self):
        commands = [("gh pr checks 353", 1), ("gh pr checks 353", 1),
                    ("pytest", 8),
                    ("echo gh pr checks 353", 8),
                    ("gh pr checks-other", 8),
                    ("gh pr checks 353; pytest", 8),
                    ("gh pr checks 353 && pytest", 8),
                    ("gh pr checks 353 | cat", 8),
                    ("gh pr checks $(echo 353)", 8),
                    ("gh pr checks 353\npytest", 8),
                    ("gh pr checks \'", 8)]
        self.commands(commands)
        events = self.query("SELECT * FROM events WHERE family='tool_result'")
        self.assertEqual(len(events), len(commands))
        self.assertTrue(all(analysis._failed(e) for e in events))
        self.assertTrue(all(e["status"] == "failed" for e in events))
        result = analysis.diagnose(self.con, detectors=["repeated_failure"])
        self.assertEqual(result["counts"].get("repeated_failure"), 1)

    def test_existing_rows_corrected_without_native_replay(self):
        path = self.commands([("gh pr checks 353", 8), ("gh pr checks 353", 1),
                              ("pytest", 8)])
        # Simulate the previous release's rows and absent correction marker.
        self.con.execute("UPDATE events SET status='failed' WHERE family='tool_result'")
        self.con.execute("DELETE FROM schema_meta WHERE key='pr_checks_pending_version'")
        self.con.commit()
        path.unlink()  # Existing ledgers must not need retained native sources.
        db.init_db(self.con)
        events = self.query("SELECT * FROM events WHERE family='tool_result' ORDER BY id")
        self.assertEqual([e["status"] for e in events], ["pending", "failed", "failed"])
        db.init_db(self.con)
        self.assertEqual(self.query("SELECT COUNT(*) FROM events")[0][0], 3)

    def test_quoted_arguments_and_full_replay_stay_pending(self):
        path = self.commands([('/usr/bin/gh pr checks 353 --repo "owner/repo"', 8)])
        import_codex_file(self.con, str(path), full=True)
        event = self.query("SELECT * FROM events WHERE family='tool_result'")[0]
        self.assertEqual(event["status"], "pending")
        self.assertEqual(json.loads(event["detail_json"])["exit_code"], 8)

    def test_real_compound_targets(self):
        # Paths replaced; event 90306's Python body omitted, not interpreted.
        records = json.loads(Path(fixture("pr-checks-stored-targets.json")).read_text())
        for record in records:
            with self.subTest(ledger_id=record["ledger_id"]):
                self.assertTrue(pending_pr_checks(
                    "tool_result", record["target"], {"exit_code": 8}))

    def test_version_one_correction_is_revisited(self):
        self.commands([("tail /tmp/proof.log; gh pr checks 114", 8)])
        self.con.execute("UPDATE events SET status='failed' WHERE family='tool_result'")
        self.con.execute("INSERT OR REPLACE INTO schema_meta(key,value)"
                         " VALUES ('pr_checks_pending_version','1')")
        self.con.commit()
        db.init_db(self.con)
        event = self.query("SELECT * FROM events WHERE family='tool_result'")[0]
        self.assertEqual(event["status"], "pending")
        self.assertEqual(self.query("SELECT value FROM schema_meta WHERE"
                                    " key='pr_checks_pending_version'")[0][0], "2")

    def test_quoting_heredocs_and_ambiguous_status_producers(self):
        cases = [
            ("echo 'not a separator; still echo'; gh pr checks 353", True),
            ("gh pr checks 353 > '/tmp/checks;pytest'", True),
            ("gh pr checks 353 --jq '.[] | .bucket'", True),
            ("echo 'gh pr checks 353;'; pytest", False),
            ("printf '%s' '&& gh pr checks 353'", False),
            ("cat <<'END'\ngh pr checks 353; fake command\nEND\npytest", False),
            ("cat <<'END'\npytest; 'unbalanced data\nEND\ngh pr checks 353", True),
            ("pytest && gh pr checks 353", False),
            ("gh pr checks 353 && pytest", False),
            ("git status --short &&\n gh pr checks 353", True),
            ("gh pr checks 353 && git status --short", True),
            ("gh pr view 353 --jq 'halt_error(8)' && gh pr checks 353", False),
            ("gh pr checks 353 | cat", False),
            ("gh pr checks 353 || pytest", False),
        ]
        for target, expected in cases:
            with self.subTest(target=target):
                self.assertEqual(pending_pr_checks(
                    "tool_result", target, {"exit_code": 8}), expected)
