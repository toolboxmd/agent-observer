"""Harness filtering preserves backing evidence without building other harnesses."""

import hashlib
import os
import shutil
from pathlib import Path
from unittest.mock import patch

from agent_observer import db, lookup
from tests.helpers import LedgerCase
from tests.test_source_availability import _place
from tests.test_lookup import _codex_rollout
from agent_observer.adapters.codex import import_codex_file


def _legacy_backing(con, event_rows, *, session_key, session_keys, harness):
    """Pre-#101 query, retained as a differential oracle on small fixtures."""
    selects, args = [], []
    for table in ('sessions', 'events', 'user_messages', 'responses', 'turns',
                  'codex_occurrences'):
        if table == 'codex_occurrences' and not lookup._has_table(con, table):
            continue
        clause, values = lookup._key_filter('session_key', session_key, session_keys)
        selects.append(f'SELECT session_key, source_id FROM {table}'
                       f' WHERE source_id IS NOT NULL{clause}')
        args.extend(values)
    clause, values = lookup._key_filter('sess.session_key', session_key, session_keys)
    selects.append('SELECT sess.session_key, src.id FROM sessions sess'
                   ' JOIN sources src ON src.harness=sess.harness'
                   ' AND src.session_id=sess.native_id'
                   f' WHERE src.id IS NOT NULL{clause}')
    args.extend(values)
    where = ''
    if harness is not None:
        where = ' WHERE harness=?'
        args.append(harness)
    rows = con.execute(
        'SELECT session_key, source_id, 0 n, harness, path, size_bytes,'
        ' read_offset, tail_sha256, ino FROM ('
        'SELECT b.session_key, b.source_id, COALESCE(src.harness, sess.harness,'
        " substr(b.session_key, 1, instr(b.session_key, ':') - 1)) harness,"
        ' src.path, src.size_bytes, src.read_offset, src.tail_sha256, src.ino'
        f" FROM ({' UNION '.join(selects)}) b"
        ' LEFT JOIN sources src ON src.id=b.source_id'
        ' LEFT JOIN sessions sess ON sess.session_key=b.session_key'
        f') backed{where}', args)
    seen = {(r['session_key'], r['source_id']) for r in event_rows}
    return [r for r in rows if (r['session_key'], r['source_id']) not in seen]


class AvailabilityScopeTest(LedgerCase):
    def _source(self, harness, native):
        return self.con.execute(
            'INSERT INTO sources(harness,path,sha256,session_id,imported_at)'
            ' VALUES(?,?,?,?,0)',
            (harness, os.path.join(self.tmp.name, 'missing', native),
             'synthetic', native)).lastrowid

    def _session(self, harness, native, source=None):
        key = f'{harness}:{native}'
        self.con.execute(
            'INSERT INTO sessions(session_key,harness,native_id,source_id,updated_at)'
            ' VALUES(?,?,?,?,0)', (key, harness, native, source))
        return key

    def test_mixed_results_equal_legacy_on_read_only_ledger(self):
        live = _place(self.tmp.name, 'sessions')
        _codex_rollout(live)
        import_codex_file(self.con, live)
        archived = _place(self.tmp.name, 'archived_sessions')
        shutil.move(live, archived)
        relocated = self.con.execute('SELECT id FROM sources WHERE path=?',
                                     (live,)).fetchone()['id']
        quiet_relocated = self._session('codex', 'quiet-relocated', relocated)
        changed = self._source('codex', 'changed')
        changed_path = self.con.execute('SELECT path FROM sources WHERE id=?',
                                        (changed,)).fetchone()['path']
        Path(changed_path).parent.mkdir(parents=True, exist_ok=True)
        Path(changed_path).write_bytes(b'x')
        self.con.execute('UPDATE sources SET size_bytes=2 WHERE id=?', (changed,))
        changed_key = self._session('codex', 'changed', changed)
        # Independent backing paths, including a source found only by native id.
        keys = {quiet_relocated, changed_key}
        for i, table in enumerate(('sessions', 'responses', 'user_messages',
                                   'turns', 'codex_occurrences', 'native')):
            native = f'quiet-{i}'
            sid = self._source('opencode', native)
            key = self._session('opencode', native, sid if table == 'sessions' else None)
            keys.add(key)
            if table == 'responses':
                self.con.execute('INSERT INTO responses(response_id,source_id,session_key)'
                                 ' VALUES(?,?,?)', (native, sid, key))
            elif table == 'user_messages':
                self.con.execute('INSERT INTO user_messages(native_id,source_id,session_key,'
                                 'text_hash,text_excerpt,is_genuine) VALUES(?,?,?,? ,?,1)',
                                 (native, sid, key, 'synthetic', ''))
            elif table == 'turns':
                self.con.execute('INSERT INTO turns(turn_id,source_id,session_key) VALUES(?,?,?)',
                                 (native, sid, key))
            elif table == 'codex_occurrences':
                self.con.execute('INSERT INTO codex_occurrences(source_id,session_key,'
                                 'row_kind,row_key,record_position) VALUES(?,?,?,?,0)',
                                 (sid, key, 'response', native))
            # Disable native matching except for the dedicated native-id case.
            if table != 'native':
                self.con.execute('UPDATE sources SET session_id=NULL WHERE id=?', (sid,))
        # Source harness wins over session harness and key prefix.
        cross = self._source('opencode', 'cross')
        self._session('grok', 'cross', cross)
        # Dangling source ids preserve session and key-prefix fallback behavior.
        self.con.commit()
        self.con.execute('PRAGMA foreign_keys=OFF')
        self._session('opencode', 'dangling', 999999)
        self.con.execute('INSERT INTO turns(turn_id,source_id,session_key) VALUES(?,?,?)',
                         ('orphan', 999998, 'opencode:orphan'))
        self._session('grok', 'other', self._source('grok', 'other'))
        self.con.commit()
        self.con.close()
        before = hashlib.sha256(Path(self.db_path).read_bytes()).digest()
        self.con = db.connect_read_only(self.db_path)
        scopes = [{}, {'harness': 'opencode'}, {'harness': 'codex'},
                  {'harness': 'grok'}, {'harness': 'absent'},
                  {'session_keys': keys, 'harness': 'opencode'},
                  {'session_keys': set(), 'harness': 'opencode'}]
        scopes += [{'session_key': k, 'harness': h}
                   for k in sorted(keys) for h in (None, 'opencode', 'grok')]
        for scope in scopes:
            with self.subTest(scope=scope):
                with patch.object(lookup, '_backing_without_events', _legacy_backing):
                    expected = lookup.availability(self.con, **scope)
                self.assertEqual(lookup.availability(self.con, **scope), expected)
        scoped = lookup.availability(self.con, harness='opencode')
        self.assertEqual(scoped['sessions']['ledger_only'], 9)
        self.assertEqual(scoped['events']['ledger_only'], 0)
        self.assertGreater(lookup.availability(self.con, harness='codex')
                           ['events']['relocated'], 0)
        self.assertEqual(lookup.availability(self.con, session_key=quiet_relocated)
                         ['status'], 'available')
        self.assertEqual(lookup.availability(self.con, session_key=changed_key)
                         ['status'], 'ledger_only')
        self.assertEqual(lookup.availability(self.con, session_key=changed_key)
                         ['sources'][0]['reason'], 'replaced')
        self.assertEqual(hashlib.sha256(Path(self.db_path).read_bytes()).digest(), before)
        # Old schema-3 compatibility remains query-only.
        self.con.close()
        self.con = db.connect(self.db_path)
        self.con.execute('DROP TABLE codex_occurrences')
        self.con.commit()
        self.con.close()
        self.con = db.connect_read_only(self.db_path)
        with patch.object(lookup, '_backing_without_events', _legacy_backing):
            expected = lookup.availability(self.con, harness='opencode')
        self.assertEqual(lookup.availability(self.con, harness='opencode'), expected)

    def test_small_harness_does_not_build_large_harness_backing(self):
        for i in range(2000):
            self._session('grok', str(i), self._source('grok', str(i)))
        self._session('opencode', 'small', self._source('opencode', 'small'))
        self.con.commit()
        calls = 0

        def budget():
            nonlocal calls
            calls += 1
            return calls > 500

        self.con.set_progress_handler(budget, 1000)
        try:
            result = lookup.availability(self.con, harness='opencode')
        finally:
            self.con.set_progress_handler(None, 0)
        self.assertEqual(result['sessions']['ledger_only'], 1)
        self.assertEqual(result['events']['ledger_only'], 0)
