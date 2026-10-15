"""Rotated Claude sessions retain task and cost ownership through queue aliases."""

import json
import os
import sqlite3
from unittest.mock import patch

from agent_observer import dispatch, report
from agent_observer.adapters import claude, t3
from tests.helpers import LedgerCase
from tests.test_t3 import T3_TABLES

ROOT = 'queue-binding-root'
WORKER = 'sub.queue-binding-root.worker-abcd'
TASK = 'example/queue#15'
OLD = 'claude:sess-old'
NEW = 'claude:sess-new'
PLANNER = 'claude:sess-planner'
SCHEDULE = {'models': {'m': {'semantics': [claude.SEMANTICS], 'rates': {
    key: 1.0 for key in ('input_tokens', 'cached_input_tokens',
                         'cache_write_input_tokens', 'output_tokens',
                         'reasoning_output_tokens')}}}}


class ClaudeQueueBindingTest(LedgerCase):
    def transcript(self, session, uuid, tokens, queued=False):
        path = os.path.join(self.tmp.name, session + '.jsonl')
        records = []
        if queued:
            records.append({'type': 'queue-operation', 'operation': 'enqueue',
                            'sessionId': session, 'content': 'Synthetic queued work',
                            'timestamp': '2026-10-01T10:00:31Z'})
        records += [
            {'type': 'user', 'sessionId': session, 'uuid': uuid,
             'entrypoint': 'sdk-ts', 'promptSource': 'sdk',
             'timestamp': '2026-10-01T10:00:32Z',
             'message': {'role': 'user', 'content': 'Synthetic queued work'}},
            {'type': 'assistant', 'sessionId': session, 'uuid': 'answer-' + session,
             'timestamp': '2026-10-01T10:00:33Z',
             'message': {'id': 'response-' + session, 'model': 'm',
                         'content': [], 'stop_reason': 'end_turn',
                         'usage': {'input_tokens': tokens, 'output_tokens': 0,
                                   'cache_read_input_tokens': 0,
                                   'cache_creation_input_tokens': 0}}},
        ]
        with open(path, 'w') as fh:
            for record in records:
                fh.write(json.dumps(record) + '\n')
        return path

    def prepare(self, *, upgraded=False, current=False, origin=True):
        self.old_path = self.transcript('sess-old', 'user-uuid-1', 120, queued=True)
        if upgraded:
            # Seed the released adapter's unsupported-queue state. All user,
            # usage and source rows still come from the real native parser.
            with patch.object(claude, '_queue_operation', side_effect=claude._UnsupportedSchema):
                claude.import_claude_file(self.con, self.old_path)
            self.con.execute('UPDATE sources SET claude_usage_version=1')
            self.con.execute("INSERT INTO tasks(task_id,origin,created_at) VALUES(?,'t3',0)", (TASK,))
            self.con.execute('INSERT INTO assignments(user_message_id,task_id,created_at) VALUES(?,?,0)',
                             ('claude:user-uuid-1', TASK))
            self.con.commit()
        claude.import_claude_file(self.con, self.old_path)
        row = self.query('SELECT * FROM user_messages WHERE session_key=?', (OLD,))[0]
        self.stable_id = row['native_id']
        if upgraded:
            self.assertEqual(row['native_id'], 'claude:user-uuid-1')
            self.assertTrue(row['alias_id'].startswith('claude:queue:'))
        else:
            self.assertTrue(row['native_id'].startswith('claude:queue:'))
            self.assertEqual(row['alias_id'], 'claude:user-uuid-1')
        self.assertEqual(row['kind'], 'unknown')
        planner = self.transcript('sess-planner', 'planner-uuid', 12)
        claude.import_claude_file(self.con, planner)
        if current:
            path = self.transcript('sess-new', 'current-uuid', 60)
            claude.import_claude_file(self.con, path)
        self.state = os.path.join(self.tmp.name, 't3-state.sqlite')
        native = sqlite3.connect(self.state)
        native.executescript(T3_TABLES + '\nCREATE TABLE projection_threads(thread_id TEXT, created_at TEXT);')
        for thread, session, created, uuid, actor in (
            (ROOT, 'sess-planner', '2026-10-01T10:00:00Z', 'planner-uuid', 'client'),
            (WORKER, 'sess-new', '2026-10-01T10:00:30Z', 'user-uuid-1', 'server'),
        ):
            native.execute('INSERT INTO projection_threads VALUES(?,?)', (thread, created))
            native.execute('INSERT INTO provider_session_runtime(thread_id,provider_name,adapter_key,resume_cursor_json) VALUES(?,?,?,?)',
                           (thread, 'claude', 'claudeAgent', json.dumps({'resume': session})))
            if origin or thread == ROOT:
                message = 't3-' + uuid
                native.execute('INSERT INTO projection_turns(thread_id,turn_id,pending_message_id) VALUES(?,?,?)',
                               (thread, uuid, message))
                native.execute('INSERT INTO orchestration_events(sequence,stream_id,event_type,occurred_at,actor_kind,payload_json) VALUES(?,?,?,?,?,?)',
                               (1 if thread == ROOT else 2, thread, t3.TURN_START_EVENT, created,
                                actor, json.dumps({'threadId': thread, 'messageId': message})))
        native.execute('INSERT INTO projection_thread_pull_requests(thread_id,host,repository,number,url,source,snapshot_json) VALUES(?,?,?,?,?,?,?)',
                       (WORKER, 'github.com', 'example/queue', 15,
                        'https://github.com/example/queue/pull/15', 'agent', json.dumps({'state': 'open'})))
        native.commit()
        native.close()
        result = t3.sync(self.con, source=self.state)
        self.assertFalse(result['failed'])
        self.con.commit()

    def assert_discovery(self):
        expected = {OLD}
        with self.subTest(consumer='dispatch.thread_sessions'):
            self.assertEqual(dispatch.thread_sessions(self.con, WORKER), expected)
        with self.subTest(consumer='t3._tree_sessions'):
            self.assertEqual(t3._tree_sessions(self.con, {WORKER: ['sess-new']},
                                               {WORKER: [WORKER]}), {WORKER: expected})

    def assert_task_cost(self, current):
        keys = {OLD, PLANNER} | ({NEW} if current else set())
        tokens = 132 + (60 if current else 0)
        with self.subTest(consumer='report.task_report'):
            actual = report.task_report(self.con, TASK, schedule=SCHEDULE)
            self.assertEqual(set(actual['scope_sessions']), keys)
            self.assertEqual(actual['attributed']['responses'], 3 if current else 2)
            self.assertEqual(actual['attributed']['total_tokens'], tokens)
            self.assertAlmostEqual(actual['estimated_cost']['estimated_cost_usd_total'], tokens / 1e6)
        with self.subTest(consumer='dispatch.compare'):
            actual = dispatch.compare(self.con, self.state, schedule=SCHEDULE,
                                      approval=lambda _: {'approved_at': None, 'error': None})
            self.assertEqual(len(actual['jobs']), 1)
            job = actual['jobs'][0]
            self.assertEqual(job['job'], TASK)
            self.assertEqual(job['unmapped_threads'], 0)
            self.assertEqual(job['tokens'], tokens)
            self.assertAlmostEqual(job['cost_usd'], tokens / 1e6)
        self.assertEqual(self.query('PRAGMA foreign_key_check'), [])
        self.assertEqual(self.query('SELECT native_id FROM user_messages WHERE session_key=?', (OLD,))[0][0],
                         self.stable_id)
        if current:
            self.assertEqual(self.query('SELECT kind,text_excerpt FROM user_messages WHERE session_key=?', (NEW,))[0][:],
                             ('unknown', ''))

    def test_fresh_queue_first_rotated_session_is_discovered(self):
        self.prepare()
        self.assert_discovery()

    def test_upgraded_rotated_session_is_discovered(self):
        self.prepare(upgraded=True)
        self.assert_discovery()

    def test_fresh_rotated_session_contributes_task_and_dispatch_cost(self):
        self.prepare()
        self.assert_task_cost(current=False)

    def test_upgraded_rotated_session_contributes_task_and_dispatch_cost(self):
        self.prepare(upgraded=True)
        self.assert_task_cost(current=False)

    def test_fresh_old_and_current_sessions_both_contribute_cost(self):
        self.prepare(current=True)
        self.assert_task_cost(current=True)

    def test_upgraded_old_and_current_sessions_both_contribute_cost(self):
        self.prepare(upgraded=True, current=True)
        self.assert_task_cost(current=True)

    def test_unmatched_sdk_alias_does_not_invent_a_thread_or_actor(self):
        self.prepare(origin=False)
        self.assertEqual(dispatch.thread_sessions(self.con, WORKER), set())
        self.assertEqual(t3._tree_sessions(self.con, {WORKER: ['sess-new']},
                                          {WORKER: [WORKER]}), {WORKER: set()})
        row = self.query('SELECT kind,text_excerpt FROM user_messages WHERE session_key=?', (OLD,))[0]
        self.assertEqual(tuple(row), ('unknown', ''))
        self.assertEqual(report.task_sessions(self.con, TASK), {PLANNER})

    def test_canonical_user_enqueue_alias_is_not_a_native_turn_uuid(self):
        self.prepare(upgraded=True)
        row = self.query('SELECT alias_id FROM user_messages WHERE session_key=?', (OLD,))[0]
        self.con.execute('INSERT INTO t3_turn_origins VALUES(?,?,?,NULL)',
                         (row['alias_id'].partition(':')[2], 'unrelated', 'server'))
        self.assertEqual(dispatch.thread_sessions(self.con, 'unrelated'), set())
        self.assertEqual(t3._tree_sessions(self.con, {}, {'unrelated': ['unrelated']}),
                         {'unrelated': set()})
