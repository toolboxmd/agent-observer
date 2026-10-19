"""Sanitized shapes from #133's real Claude steers and Codex notices.

Only transport boilerplate survives; ids, commands and message bodies are fake.
"""
import json
import io
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout, closing

from agent_observer import db, interventions, intervention_evidence, cli, verdicts
from agent_observer.adapters import claude, codex, grok, t3
from tests.test_interventions import _line, _tool_use, _tool_result, _write
from tests.test_t3_v2 import write_v2, ROOT, CL2, P1


NOTE = ("Note from T3 Code: the {sender} interrupted your previous turn, if it was still "
        "running, to deliver the message below. Tool calls stopped for this message were "
        "interrupted, not refused, even if their results say the user declined them or "
        "does not want them. Follow the message below before deciding whether to retry "
        "a stopped action. Stopped actions may have partly run, other running work may "
        "have stopped too, and actions that finished keep their effects.\n\nSanitized message.")


class ActorRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = db.connect(os.path.join(self.tmp.name, 'ledger.db'))
        self.addCleanup(self.con.close)
        db.init_db(self.con)
        t3._ensure_tables(self.con)

    def test_real_steer_sequences_count_once_and_keep_raw_evidence(self):
        for sender, actor in [('user', 'user'), ('agent in T3 thread sender-1', 'agent')]:
            with self.subTest(sender=sender):
                session = actor
                path = os.path.join(self.tmp.name, session + '.jsonl')
                records = [
                    _line(session, 'assistant', 'a-'+actor,
                          _tool_use('call-'+actor, 'Bash', {'command': 'true'}), 0,
                          entrypoint='sdk-ts'),
                    _line(session, 'user', 'r-'+actor,
                          _tool_result('call-'+actor, 'Stopped', True), 1,
                          entrypoint='sdk-ts', toolDenialKind='user-rejected'),
                    _line(session, 'user', 'echo-'+actor,
                          '[Request interrupted by user for tool use]', 1,
                          entrypoint='sdk-ts'),
                    _line(session, 'user', 'steer-'+actor, NOTE.format(sender=sender), 5,
                          entrypoint='sdk-ts', promptSource='sdk'),
                ]
                _write(path, records)
                for full in (False, True, False):
                    claude.import_claude_file(self.con, path, full=full)
                    rows = interventions.select(self.con, created_by=None)['interventions']
                    rows = [r for r in rows if r['session'] == 'claude:'+session]
                    self.assertEqual(len(rows), 1)
                    [row] = rows
                    self.assertEqual((row['id'], row['kind'], row['created_by']),
                                     ('sub:claude:steer-'+actor, 'interrupt', actor))
                    self.assertIn('ev:claude:'+session+'/permission/call-'+actor,
                                  row['related_ids'])
                    self.assertEqual(interventions.resolve(self.con, row['id']), row)
                    for identity in row['related_ids']:
                        self.assertEqual(interventions.resolve(self.con, identity)['id'], row['id'])
                        [alias] = interventions.show(self.con, [identity])
                        self.assertEqual((alias['id'], alias['status']), (row['id'], 'ok'))
                    [shown] = interventions.show(self.con, [row['id']])
                    self.assertEqual(shown['status'], 'ok')
                    self.assertIn('Sanitized message.', shown['text'])
                self.assertEqual(self.con.execute(
                    'SELECT count(*) FROM events WHERE session_key=? AND family="permission"',
                    ('claude:'+session,)).fetchone()[0], 1)

    def test_completion_notice_is_agent_even_with_human_native_marker(self):
        path = os.path.join(self.tmp.name, 'codex.jsonl')
        text = ('Delegated task node:delegated-task:fixture reached a terminal state. '
                'Use task_status with taskId node:delegated-task:fixture to read the result.')
        _write(path, [
            {'type': 'session_meta', 'payload': {'id': 'cx', 'originator': 'codex-tui'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
             'id': 'notice', 'content': [{'type': 'input_text', 'text': text}],
             'internal_chat_message_metadata_passthrough': {'content_item_kinds': ['user.text']}}},
        ])
        for full in (False, True, False):
            codex.import_codex_file(self.con, path, full=full)
            row = interventions.resolve(self.con, 'sub:codex:notice')
            self.assertEqual(row['created_by'], 'agent')

        # Native notices can inherit a preceding human conversational turn.
        self.con.execute("UPDATE user_messages SET turn_id='codex:previous-human'"
                         " WHERE native_id='codex:notice'")
        self.con.execute("INSERT INTO t3_native_prompt_origins"
                         "(session_key,native_id,actor_kind) VALUES(?,?,?)",
                         ('codex:cx', 'codex:previous-human', 'client'))
        self.assertEqual(interventions.resolve(self.con, 'sub:codex:notice')
                         ['created_by'], 'agent')

    def test_pasted_notice_with_new_instruction_stays_user(self):
        notice = ('Delegated task node:fixture reached a terminal state. '
                  'Use task_status with taskId node:fixture to read the result.')
        for suffix in ('\nNow implement my request.',
                       '<runtime_info>Context</runtime_info>\nNow implement my request.',
                       '<runtime_info>Context</runtime_info>\nMy words</runtime_info>'):
            with self.subTest(suffix=suffix):
                path = os.path.join(self.tmp.name, 'pasted.jsonl')
                _write(path, [
                    {'type': 'session_meta', 'payload': {'id': 'pasted', 'originator': 'codex-tui'}},
                    {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                     'id': 'pasted', 'content': [{'type': 'input_text', 'text': notice+suffix}],
                     'internal_chat_message_metadata_passthrough': {
                         'content_item_kinds': ['user.text']}}}])
                codex.import_codex_file(self.con, path, full=True)
                self.assertEqual(interventions.resolve(self.con, 'sub:codex:pasted')
                                 ['created_by'], 'user')

    def test_partial_or_human_authored_agent_steer_does_not_claim_agent(self):
        path = os.path.join(self.tmp.name, 'pasted-steer.jsonl')
        header = NOTE.format(sender='agent in T3 thread sender-1')
        partial = header.partition(' Follow the message below')[0]+'\nMy own instruction.'
        _write(path, [
            _line('pasted-steer', 'user', 'partial', partial, 1,
                  entrypoint='sdk-ts', promptSource='sdk'),
            _line('pasted-steer', 'user', 'human', header, 2,
                  entrypoint='sdk-ts', promptSource='sdk')])
        for identity in ('partial', 'human'):
            self.con.execute('INSERT INTO t3_turn_origins VALUES(?,?,?,?)',
                             (identity, 'root', 'client', None))
        claude.import_claude_file(self.con, path)
        for identity in ('partial', 'human'):
            self.assertEqual(interventions.resolve(self.con, 'sub:claude:'+identity)
                             ['created_by'], 'user')
        self.assertIsNone(self.con.execute('SELECT transport_kind FROM intervention_prompt_evidence '
                                          'WHERE native_id=?', ('claude:partial',)).fetchone()[0])

    def test_unknown_sdk_root_keeps_stored_reason_without_guessing_from_root(self):
        path = os.path.join(self.tmp.name, 'unknown.jsonl')
        _write(path, [_line('root-session', 'user', 'unmatched', 'Sanitized input', 0,
                           entrypoint='sdk-ts', promptSource='sdk')])
        self.con.execute('INSERT INTO t3_threads VALUES(?,?,?,?,?,?)',
                         ('root', 'root', 'claude', None, 'root-session', 'client'))
        claude.import_claude_file(self.con, path)
        row = interventions.resolve(self.con, 'sub:claude:unmatched')
        self.assertEqual((row['created_by'], row['thread']), ('unknown', 'root'))
        self.assertEqual(row['actor_reason'], 'native_sdk_prompt_without_matching_origin')
        # Reporting must not rely on rereading a transcript or writing the ledger.
        os.remove(path)
        self.assertEqual(interventions.resolve(self.con, row['id'])['actor_reason'],
                         row['actor_reason'])
        stored = self.con.execute('SELECT actor_reason FROM intervention_prompt_evidence '
                                  'WHERE native_id=?', ('claude:unmatched',)).fetchone()
        self.assertEqual(stored[0], row['actor_reason'])
        self.con.commit()
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.main(['--db', os.path.join(self.tmp.name, 'ledger.db'),
                               'interventions', '--created-by', 'unknown'])
        self.assertEqual(result, 0)
        self.assertIn('actor_reason='+row['actor_reason'], output.getvalue())

    def test_weak_assistant_reference_cannot_credit_a_root_prompt(self):
        # Real root-thread shape: Chromeria points to its last assistant UUID,
        # not the native user UUID. Matching text or the opener cannot repair it.
        state = os.path.join(self.tmp.name, 't3.sqlite')
        write_v2(state)
        with closing(sqlite3.connect(state)) as native:
            native.execute('DELETE FROM orchestration_v2_projection_provider_threads '
                           'WHERE thread_id=? AND provider=?', (ROOT, 'codex'))
            native.execute(
                'UPDATE orchestration_v2_projection_provider_turns SET payload_json=? '
                'WHERE run_attempt_id=?',
                (json.dumps({'nativeTurnRef': {'driver': 'claudeAgent',
                    'nativeId': 'assistant-uuid', 'strength': 'weak'}}), 'attempt:vm1'))
            native.commit()
        path = os.path.join(self.tmp.name, 'weak.jsonl')
        _write(path, [
            _line(CL2, 'user', P1, 'Sanitized input', 0,
                  entrypoint='sdk-ts', promptSource='sdk'),
            _line(CL2, 'assistant', 'assistant-uuid', 'Sanitized response', 1,
                  entrypoint='sdk-ts')])
        claude.import_claude_file(self.con, path)
        t3.sync(self.con, source=state)
        row = interventions.resolve(self.con, 'sub:claude:'+P1)
        self.assertEqual((row['created_by'], row['thread']), ('unknown', 'root'))
        self.assertEqual(row['actor_reason'], 'native_sdk_prompt_without_matching_origin')

    def test_unrelated_refusal_before_an_ordinary_prompt_stays_a_rejection(self):
        path = os.path.join(self.tmp.name, 'refusal.jsonl')
        _write(path, [
            _line('plain', 'user', 'rejected', _tool_result('call', 'No', True), 1,
                  toolDenialKind='user-rejected'),
            _line('plain', 'user', 'echo', '[Request interrupted by user for tool use]', 1),
            _line('plain', 'user', 'next', 'Ordinary input', 5)])
        claude.import_claude_file(self.con, path)
        rows = interventions.select(self.con, created_by=('user',))['interventions']
        self.assertEqual({r['kind'] for r in rows}, {'rejection', 'prompt'})

    def test_quoted_or_typed_transport_text_does_not_claim_a_steer(self):
        path = os.path.join(self.tmp.name, 'typed.jsonl')
        _write(path, [
            _line('typed', 'user', 'typed', NOTE.format(sender='user'), 0),
            _line('typed', 'user', 'quoted', 'Quote:\n'+NOTE.format(sender='user'), 1,
                  entrypoint='sdk-ts', promptSource='sdk')])
        claude.import_claude_file(self.con, path)
        for identity in ('typed', 'quoted'):
            self.assertEqual(interventions.resolve(self.con, 'sub:claude:'+identity)['kind'],
                             'prompt')

    def test_root_mirror_backfills_a_reason_for_an_unparsed_harness(self):
        self.test_unknown_sdk_root_keeps_stored_reason_without_guessing_from_root()
        self.con.execute('DELETE FROM intervention_prompt_evidence')
        intervention_evidence.backfill_root_reasons(self.con)
        row = interventions.resolve(self.con, 'sub:claude:unmatched')
        self.assertEqual(row['actor_reason'], 'native_prompt_without_matching_origin')
        self.assertEqual(self.con.execute('SELECT actor_reason FROM intervention_prompt_evidence')
                         .fetchone()[0], row['actor_reason'])

    def test_stored_grouped_verdicts_resolve_show_and_recur_once(self):
        path = os.path.join(self.tmp.name, 'stored.jsonl')
        records = [
            _line('stored', 'user', 'echo', '[Request interrupted by user]', 1,
                  entrypoint='sdk-ts'),
        ]
        _write(path, records)
        self.con.execute('INSERT INTO t3_threads VALUES(?,?,?,?,?,?)',
                         ('root', 'root', 'claude', None, 'stored', 'client'))
        claude.import_claude_file(self.con, path)
        verdicts.add(self.con, [{'intervention': 'sub:claude:echo', 'behavior': 'correction',
            'judge': 'spectrum-review', 'verdict': 'yes', 'problem': 'Sanitized problem',
            'group': 'fixture'}])
        records.append(_line('stored', 'user', 'steer', NOTE.format(sender='user'), 5,
                             entrypoint='sdk-ts', promptSource='sdk'))
        _write(path, records)
        claude.import_claude_file(self.con, path)
        canonical = 'sub:claude:steer'
        self.assertEqual(interventions.resolve(self.con, 'sub:claude:echo')['id'], canonical)
        [shown] = interventions.show(self.con, ['sub:claude:echo'])
        self.assertEqual((shown['id'], shown['status']), (canonical, 'ok'))
        self.assertIn('Sanitized message.', shown['text'])
        [listed] = verdicts.listing(self.con, 'correction')
        self.assertEqual(listed['intervention'], 'sub:claude:echo')
        self.assertEqual(listed['canonical_intervention'], canonical)
        self.assertEqual(listed['session'], 'claude:stored')
        result = verdicts.recurrence(self.con, 'correction')
        self.assertEqual((result['occurrences'], result['coverage']['judged']), (1, 1))
        self.assertEqual(result['groups'][0]['interventions'], [canonical])
        self.assertEqual(interventions.select(self.con, unjudged='correction')['interventions'], [])
        # The real spectrum-review batch also judged the follow-up itself.
        # Equal-time duplicate yes verdicts still describe one intervention.
        verdicts.add(self.con, [{'intervention': canonical, 'behavior': 'correction',
            'judge': 'spectrum-review', 'verdict': 'yes', 'problem': 'Sanitized problem',
            'group': 'fixture'}])
        self.con.execute('UPDATE verdicts SET recorded_at=1')
        result = verdicts.recurrence(self.con, 'correction')
        self.assertEqual((result['occurrences'], result['coverage']['judged']), (1, 1))
        self.assertEqual(result['superseded_verdicts'][0]['intervention'], 'sub:claude:echo')
        self.assertEqual(result['superseded_verdicts'][0]['problem'], 'Sanitized problem')
        self.assertEqual(result['superseded_verdicts'][0]['group'], 'fixture')
        # A canonical no cannot hide the alias yes, whatever their timestamps.
        verdicts.add(self.con, [{'intervention': canonical, 'behavior': 'correction',
            'judge': 'spectrum-review', 'verdict': 'no'}])
        self.assertEqual(len(verdicts.listing(self.con, 'correction')), 2)
        for alias_time, canonical_time in ((1, 1), (1, 2), (2, 1)):
            with self.subTest(alias_time=alias_time, canonical_time=canonical_time):
                self.con.execute('UPDATE verdicts SET recorded_at=? WHERE intervention_id=?',
                                 (alias_time, 'sub:claude:echo'))
                self.con.execute('UPDATE verdicts SET recorded_at=? WHERE intervention_id=?',
                                 (canonical_time, canonical))
                result = verdicts.recurrence(self.con, 'correction')
                self.assertEqual(result['verdicts'], {'yes': 1, 'no': 0, 'unclear': 0})
                self.assertEqual(verdicts.groups(self.con, 'correction')[0]['occurrences'], 1)
                for row in verdicts.listing(self.con, 'correction'):
                    self.assertEqual(row['canonical_verdict'], 'yes')
                    self.assertEqual(row['canonical_problem'], 'Sanitized problem')
                    self.assertEqual(row['canonical_group'], 'fixture')
                [superseded] = result['superseded_verdicts']
                self.assertEqual(superseded['intervention'], canonical)
                self.assertIn('problem', superseded)
                self.assertIn('group', superseded)

    def test_grok_records_agent_steer_and_completion_metadata(self):
        path = os.path.join(self.tmp.name, 'grok-session')
        texts = [NOTE.format(sender='agent in T3 thread sender-1'),
                 'Delegated task node:fixture reached a terminal state. '
                 'Use task_status with taskId node:fixture to read the result.'
                 '<runtime_info>Sanitized runtime</runtime_info>']
        texts += ['<t3_code_instructions>Sanitized instructions</t3_code_instructions>\n'
                  '<user_request>'+text+'</user_request>' for text in texts[:1]]
        texts += ['<t3_code_instructions>Sanitized instructions</t3_code_instructions>\n'
                  '<user_request>Delegated task node:wrapped reached a terminal state. '
                  'Use task_status with taskId node:wrapped to read the result.</user_request>'
                  '<runtime_info>Sanitized runtime</runtime_info>']
        texts += ['Quoted notice: '+texts[1]]
        native_context = '<pull_request_linking>Sanitized linking</pull_request_linking>'
        texts += [texts[1]+native_context, texts[1]+native_context+'\nMy own instruction.']
        tool_context = '<t3_code_tool_use>Sanitized tools</t3_code_tool_use>'
        texts += [texts[1]+native_context+tool_context,
                  texts[1]+native_context+tool_context+'\nMy own instruction.']
        _write(os.path.join(path, 'updates.jsonl'), [
            {'timestamp': 1791367200 + i, 'method': 'session/update', 'params': {
                'sessionId': 'grok-session', 'update': {'sessionUpdate': 'user_message_chunk',
                'content': {'type': 'text', 'text': text}, '_meta': {'promptIndex': i}}}}
            for i, text in enumerate(texts)])
        for full in (False, True, False):
            grok.import_grok_session(self.con, path, full=full)
            for i, kind in enumerate(('interrupt', 'prompt', 'interrupt', 'prompt')):
                row = interventions.resolve(self.con, f'sub:grok:grok-session:prompt:{i}')
                self.assertEqual((row['created_by'], row['kind']), ('agent', kind))
            quoted = interventions.resolve(self.con, 'sub:grok:grok-session:prompt:4')
            self.assertEqual((quoted['created_by'], quoted['kind']), ('unknown', 'prompt'))
            self.assertEqual(quoted['actor_reason'], 'native_prompt_without_matching_origin')
            self.assertEqual(interventions.resolve(self.con, 'sub:grok:grok-session:prompt:5')
                             ['created_by'], 'agent')
            self.assertEqual(interventions.resolve(self.con, 'sub:grok:grok-session:prompt:6')
                             ['created_by'], 'unknown')
            self.assertEqual(interventions.resolve(self.con, 'sub:grok:grok-session:prompt:7')
                             ['created_by'], 'agent')
            self.assertEqual(interventions.resolve(self.con, 'sub:grok:grok-session:prompt:8')
                             ['created_by'], 'unknown')
        # A ledger from the old importer must replay unchanged native files once.
        self.con.execute('DELETE FROM intervention_prompt_evidence')
        self.con.execute('UPDATE grok_session_meta SET summary_fp=?',
                         ('44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',))
        with mock.patch.object(grok, '_load_jsonl_records', wraps=grok._load_jsonl_records) as load:
            grok.import_grok_session(self.con, path)
            self.assertTrue(load.called)
        self.assertEqual(interventions.resolve(self.con, 'sub:grok:grok-session:prompt:1')
                         ['created_by'], 'agent')
        with mock.patch.object(grok, '_load_jsonl_records', wraps=grok._load_jsonl_records) as load:
            grok.import_grok_session(self.con, path)
            load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
