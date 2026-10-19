"""Native fixture regressions for the five capture gaps in #128."""
import json
import os

from agent_observer import analysis, identity, privacy
from agent_observer.adapters import codex, grok
from tests.helpers import LedgerCase


class ParserGapsTest(LedgerCase):
    def write(self, name, records):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'w') as f:
            for record in records:
                f.write(json.dumps(record) + '\n')
        return path

    def codex(self, items):
        records = [{'type': 'session_meta', 'payload': {'id': 'gaps', 'source': 'cli'}}]
        records += [{'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': x}}
                    for x in items]
        return self.write('rollout-gaps.jsonl', records)

    def command(self, id='cmd', exit_code=0, **extra):
        return dict(type='CommandExecution', id=id, command=['cat', '/repo/agentsmd/skills/operations/SKILL.md'],
                    parsed_cmd=[{'type': 'read', 'path': '/repo/agentsmd/skills/operations/SKILL.md'}],
                    status='completed', exit_code=exit_code, **extra)

    def test_sectioned_direction_and_legacy_fail_closed(self):
        header = {'status': 'ready', 'instructions': {'sha256': 'a' * 64},
                  'sections': '0123456789abcdef'}
        body = ('<<<AGENTSMD_PROJECT_DIRECTION_V1>>>\n' + json.dumps(header) +
                '\n<<<AGENTSMD_SECTION 0123456789abcdef VISION.md>>>\n'
                'private text {"instructions": {"sha256": "bad"}}\n'
                '<<<END_AGENTSMD_PROJECT_DIRECTION_V1 0123456789abcdef>>>')
        self.assertEqual(identity.parse_direction_block(body)['instructions_sha256'], 'a' * 64)
        self.assertNotIn('private text', json.dumps(identity.parse_direction_block(body)))
        self.assertEqual(identity.parse_direction_block(body.replace('abcdef>>>', 'abcdee>>>')),
                         {'status': 'unparsed'})
        legacy = '<<<AGENTSMD_PROJECT_DIRECTION_V1>>>\n' + json.dumps({'status': 'ready'}) + '\n<<<END_AGENTSMD_PROJECT_DIRECTION_V1>>>'
        self.assertEqual(identity.parse_direction_block(legacy), {'status': 'ready'})
        self.assertEqual(identity.parse_direction_block(body.replace(json.dumps(header), 'free text')),
                         {'status': 'unparsed'})

    def test_codex_native_duration_and_replay(self):
        path = self.codex([self.command(duration={'secs': 2, 'nanos': 345000000})])
        codex.sync(self.con, source=path)
        self.assertEqual(self.query("SELECT duration_ms FROM events WHERE family='tool_result'")[0][0], 2345)
        self.con.execute('UPDATE events SET duration_ms=NULL')
        self.con.execute('UPDATE sources SET privacy_version=9')
        self.con.commit()
        for _ in range(2):
            codex.sync(self.con, source=path)
            self.assertEqual(self.query("SELECT duration_ms FROM events WHERE family='tool_result'")[0][0], 2345)
            self.assertEqual(self.query('SELECT COUNT(*) FROM events')[0][0], 2)
        self.assertEqual(codex._duration({'duration': 1.5}), 1500)
        self.assertEqual(codex._duration({'duration_ms': 0}), 0)
        for value in ({'secs': True, 'nanos': 0}, {'secs': -1, 'nanos': 0},
                      {'secs': 1, 'nanos': 1000000000}):
            self.assertIsNone(codex._duration({'duration': value}))

    def test_codex_read_states_and_success_report(self):
        path = self.codex([self.command('ok'), self.command('bad', 1),
                           dict(self.command('pending'), status='in_progress', exit_code=None)])
        codex.sync(self.con, source=path)
        states = {r['native_id'].split(':')[0]: r['status'] for r in
                  self.query("SELECT * FROM events WHERE family='skill_read'")}
        self.assertEqual(states, {'ok': 'ok', 'bad': 'failed', 'pending': 'pending'})
        session = self.query('SELECT * FROM sessions')[0]
        self.assertEqual(analysis._session_metrics(self.con, session)['agentsmd_reads'], 1)
        # Repeated failed/pending procedure reads must not look like repeated loads.
        self.assertEqual(analysis._repeated_skills(session, self.query('SELECT * FROM events')), [])

    def test_codex_read_function_result_and_pending(self):
        records = [{'type': 'session_meta', 'payload': {'id': 'reads', 'source': 'cli'}}]
        for call, path, output in [('skill', '/repo/agentsmd/skills/operations/SKILL.md', {'content': 'fixture'}),
                                   ('file', '/repo/a.py', 'fixture text'),
                                   ('bad', '/repo/b.py', {'is_error': True}),
                                   ('pending', '/repo/c.py', None)]:
            records.append({'type': 'response_item', 'payload': {'type': 'function_call',
                'name': 'Read', 'call_id': call, 'arguments': json.dumps({'file_path': path})}})
            if output is not None:
                records.append({'type': 'response_item', 'payload': {'type': 'function_call_output',
                    'call_id': call, 'output': output}})
        path = self.write('rollout-read.jsonl', records)
        codex.sync(self.con, source=path)
        rows = {r['native_id']: (r['family'], r['status']) for r in self.query(
            "SELECT * FROM events WHERE family IN ('read','skill_read')")}
        self.assertEqual(rows, {'skill': ('skill_read', 'ok'), 'file': ('read', 'ok'),
                                'bad': ('read', 'failed'), 'pending': ('read', 'attempted')})
        # Incremental result joins the persisted call without needing an in-memory pending map.
        with open(path, 'a') as f:
            f.write(json.dumps({'type': 'response_item', 'payload': {'type': 'function_call_output',
                'call_id': 'pending', 'output': '{"content":"fixture"}'}}) + '\n')
        codex.sync(self.con, source=path)
        self.assertEqual(self.query("SELECT status FROM events WHERE family='read' AND native_id='pending'")[0][0], 'ok')

    def grok_fixture(self, updates, events):
        path = os.path.join(self.tmp.name, 'grok-gaps'); os.mkdir(path)
        with open(os.path.join(path, 'summary.json'), 'w') as f:
            json.dump({'info': {'id': 'grok-gaps', 'cwd': '/repo'}}, f)
        self.write('grok-gaps/updates.jsonl', [
            {'method': 'session/update', 'params': {'sessionId': 'grok-gaps',
             '_meta': {'eventId': str(i), 'promptId': 'p1'}, 'update': x}}
            for i, x in enumerate(updates)])
        self.write('grok-gaps/events.jsonl', events)
        return path

    def test_grok_mcp_joins_and_outcomes(self):
        path = self.grok_fixture([
            {'sessionUpdate': 'tool_call', 'toolCallId': 'm1', '_meta': {'x.ai/tool': {'name': 'lookup'}}},
            {'sessionUpdate': 'tool_call_update', 'toolCallId': 'm1', 'status': 'completed'}], [
            {'type': 'mcp_tool_call_started', 'call_id': 'm1', 'tool_name': 'lookup', 'timeout_sec': 30},
            {'type': 'mcp_tool_call_completed', 'call_id': 'm1', 'tool_name': 'lookup',
             'duration_ms': 234, 'success': True, 'is_timeout': False,
             'auth_retry_attempted': False, 'reconnect_attempted': True}])
        for full in (False, True, False):
            grok.sync(self.con, source=path, full=full)
            self.assertEqual(self.query('SELECT COUNT(*) FROM import_errors')[0][0], 0)
            self.assertEqual(self.query("SELECT COUNT(*) FROM events WHERE family='tool_call'")[0][0], 1)
            row = self.query("SELECT * FROM events WHERE family='tool_result'")[0]
            self.assertEqual((row['duration_ms'], row['status']), (234, 'ok'))
            self.assertEqual(json.loads(row['detail_json'])['reconnect_attempted'], True)

    def test_grok_read_state(self):
        path = self.grok_fixture([
            {'sessionUpdate': 'tool_call_update', 'toolCallId': call, 'kind': 'read',
             'status': status, 'locations': [{'path': '/repo/agentsmd/skills/operations/SKILL.md'}]}
            for call, status in [('ok', 'completed'), ('bad', 'failed'), ('pending', 'in_progress')]], [])
        grok.sync(self.con, source=path)
        self.assertEqual({r['native_id']: r['status'] for r in self.query(
            "SELECT * FROM events WHERE family='skill_read'")},
            {'ok': 'ok', 'bad': 'failed', 'pending': 'pending'})

    def test_grok_completion_without_locations_updates_prior_read(self):
        path = self.grok_fixture([
            {'sessionUpdate': 'tool_call_update', 'toolCallId': 'r1', 'kind': 'read',
             'status': 'in_progress', 'locations': [{'path': '/repo/a.py'}]}], [])
        grok.sync(self.con, source=path)
        with open(os.path.join(path, 'updates.jsonl'), 'a') as f:
            f.write(json.dumps({'method': 'session/update', 'params': {'sessionId': 'grok-gaps',
                '_meta': {'eventId': 'late', 'promptId': 'p1'}, 'update': {
                'sessionUpdate': 'tool_call_update', 'toolCallId': 'r1', 'status': 'failed'}}}) + '\n')
        grok.sync(self.con, source=path)
        self.assertEqual(self.query("SELECT status FROM events WHERE family='read'")[0][0], 'failed')

    def test_grok_mcp_timeout_and_privacy_repair(self):
        path = self.grok_fixture([], [
            {'type': 'mcp_tool_call_started', 'call_id': 'm2', 'tool_name': 'lookup', 'timeout_sec': 30},
            {'type': 'mcp_tool_call_completed', 'call_id': 'm2', 'tool_name': 'lookup',
             'duration_ms': 30000, 'success': False, 'is_timeout': True,
             'auth_retry_attempted': True, 'reconnect_attempted': False, 'error': 'private error'}])
        grok.sync(self.con, source=path)
        self.con.execute('UPDATE sources SET privacy_version=9')
        self.con.commit()
        for _ in range(2):
            grok.sync(self.con, source=path)
            rows = self.query("SELECT * FROM events WHERE family='tool_result'")
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['status'], rows[0]['duration_ms']), ('error', 30000))
            self.assertTrue(json.loads(rows[0]['detail_json'])['is_timeout'])
            self.assertNotIn('private error', str(dict(rows[0])))

    def test_codex_read_json_file_contents_are_not_result_metadata(self):
        for text in ('{"version":1}', '[]', '123', '{"error":"example"}', 'Error: example from a file'):
            self.assertEqual(codex._read_output_state(text), 'ok')

    def test_grok_locationless_success_retains_version_evidence(self):
        path = self.grok_fixture([
            {'sessionUpdate': 'tool_call_update', 'toolCallId': 'r2', 'kind': 'read',
             'status': 'in_progress', 'locations': [
                 {'path': '/plugins/agentsmd/17.3.1/skills/operations/SKILL.md'}]},
            {'sessionUpdate': 'tool_call_update', 'toolCallId': 'r2', 'status': 'completed'}], [])
        grok.sync(self.con, source=path)
        self.assertEqual(self.query("SELECT agentsmd_version FROM sessions")[0][0], '17.3.1')
        self.assertEqual(self.query("SELECT status FROM events WHERE family='skill_read'")[0][0], 'ok')

    def test_grok_each_successful_read_contributes_version_once(self):
        updates = []
        for call, version, with_locations in [('a', '17.1.0', True),
                                               ('b1', '17.3.1', False),
                                               ('b2', '17.3.1', False)]:
            location = {'path': f'/plugins/agentsmd/{version}/skills/operations/SKILL.md'}
            updates.append({'sessionUpdate': 'tool_call_update', 'toolCallId': call,
                'kind': 'read', 'status': 'completed' if with_locations else 'in_progress',
                'locations': [location]})
            # Repeated witnesses of the same successful read must count once.
            updates.append({'sessionUpdate': 'tool_call_update', 'toolCallId': call,
                            'status': 'completed'})
        path = self.grok_fixture(updates, [])
        grok.sync(self.con, source=path)
        session = self.query('SELECT agentsmd_version,identity_json FROM sessions')[0]
        self.assertEqual(session['agentsmd_version'], '17.3.1')
        self.assertEqual(json.loads(session['identity_json'])['plugin_paths'],
                         {'17.1.0': 1, '17.3.1': 2})
