"""Diagnose keeps its evidence while occurrence lookups stay session-scoped."""

import hashlib
from pathlib import Path

from agent_observer import analysis, db
from tests.test_analysis import AnalysisCase


class DiagnoseScaleTest(AnalysisCase):
    def test_initialized_ledger_does_not_scan_other_sessions_occurrences(self):
        source = self.con.execute(
            "INSERT INTO sources(harness,path,sha256,imported_at)"
            " VALUES('codex','synthetic','synthetic',0)").lastrowid
        self.con.executemany(
            "INSERT INTO codex_occurrences(source_id,session_key,row_kind,"
            " row_key,record_position) VALUES(?,'codex:unrelated','event',?,?)",
            ((source, str(i), i) for i in range(4000)))
        for i in range(100):
            self.session(f"grok:{i}")
        # Simulate an existing schema-3 ledger receiving the additive index
        # through the ordinary explicit writer initialization, twice.
        self.con.execute("DROP INDEX IF EXISTS idx_codex_occurrences_session")
        self.con.commit()
        db.init_db(self.con)
        db.init_db(self.con)
        self.con.close()
        before = Path(self.db_path).read_bytes()
        self.con = db.connect_read_only(self.db_path)
        calls = 0

        def budget():
            nonlocal calls
            calls += 1
            return calls > 200

        self.con.set_progress_handler(budget, 1000)
        try:
            result = analysis.diagnose(self.con)
        finally:
            self.con.set_progress_handler(None, 0)
        self.assertEqual(result['sessions'], 100)
        self.assertEqual(result['counts'], {})
        self.assertEqual(Path(self.db_path).read_bytes(), before)

    def test_mixed_results_equal_before_and_after_explicit_index_upgrade(self):
        for key in ('grok:plain', 'claude:plain', 'codex:known',
                    'codex:conflict', 'codex:partial', 'codex:tied', 'codex:child'):
            self.session(key)
            self.event(key, 'read', target='/p/a.py', ts=1001)
            self.event(key, 'read', target='/p/a.py', ts=1002)
            for ts in (1003, 1004):
                self.event(key, 'skill_read', name='diagnosis', ts=ts)
            for ts in (1005, 1006):
                self.event(key, 'tool_result', name='Bash', target='pytest',
                           status='error', ts=ts)
            self.event(key, 'file_change', target='/p/tests/test_a.py', ts=1007)
            self.event(key, 'tool_result', name='Bash', target='pytest',
                       status='ok', ts=1008)
            self.event(key, 'assistant_message', ts=1009,
                       detail={'excerpt': 'Should I continue?'})
            self.event(key, 'tool_result', name='Bash', ts=1010)
            self.con.execute("UPDATE events SET size_bytes=60000 WHERE native_id=?",
                             (f'e{self.seq}',))
            self.prompt(key, 'no, wrong')
            self.prompt(key, 'huh?')
            self.con.execute("UPDATE user_messages SET ts=1010.5 WHERE native_id=?",
                             (f's{self.seq}',))
            if not key.startswith('codex:'):
                continue
            events = self.con.execute(
                'SELECT id FROM events WHERE session_key=? ORDER BY ts', (key,)
            ).fetchall()
            for copy in range(2):
                source = self.con.execute(
                    'INSERT INTO sources(harness,path,sha256,imported_at)'
                    " VALUES('codex',?,'synthetic',0)", (f'{key}-{copy}',)
                ).lastrowid
                ordered = list(events)
                if key == 'codex:conflict' and copy:
                    ordered.reverse()
                if key == 'codex:partial':
                    ordered = ordered[copy::2]
                for pos, row in enumerate(ordered):
                    self.con.execute(
                        'INSERT INTO codex_occurrences(source_id,session_key,'
                        'row_kind,row_key,record_position) VALUES(?,?,?,?,?)',
                        (source, key, 'event', str(row['id']),
                         pos // 2 if key == 'codex:tied' else pos))
        self.con.execute("UPDATE sessions SET role='subagent'"
                         " WHERE session_key='codex:child'")
        self.con.execute("DROP INDEX IF EXISTS idx_codex_occurrences_session")
        self.con.commit()
        self.con.close()
        scopes = ({}, {'harness': 'grok'}, {'harness': 'codex'},
                  {'session': 'codex:conflict'}, {'since': 1004, 'until': 1011},
                  {'detectors': ['repeated_read'], 'limit': 2}, {'limit': 0})

        def results():
            digest = hashlib.sha256(Path(self.db_path).read_bytes()).digest()
            self.con = db.connect_read_only(self.db_path)
            try:
                return [analysis.diagnose(self.con, **scope) for scope in scopes]
            finally:
                self.con.close()
                self.assertEqual(hashlib.sha256(Path(self.db_path).read_bytes()).digest(),
                                 digest)

        expected = results()
        self.assertEqual(expected[0]['sessions'], 6)
        self.assertEqual(set(expected[0]['counts']), set(analysis.DETECTORS))
        self.assertTrue(expected[0]['incidents'])
        self.con = db.connect(self.db_path)
        db.init_db(self.con)
        self.con.close()
        self.assertEqual(results(), expected)
        # LedgerCase owns closing this handle at teardown.
        self.con = db.connect_read_only(self.db_path)
