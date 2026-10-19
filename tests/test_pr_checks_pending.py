"""Regression from bestia ledger event 363608 (native exit-8 command).

The fixture retains the command, status, exit code and duration. Session and
item identities are replaced; cwd, process id and all output are omitted.
"""

import json
from pathlib import Path

from agent_observer import analysis, db, pricing, report
from agent_observer.adapters.codex import import_codex_file
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
        phases, _ = report._phase_report(
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
