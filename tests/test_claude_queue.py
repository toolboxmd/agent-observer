"""Queue evidence is ordered, exact-once and never an SDK authorship guess."""

import json
import os
from unittest.mock import patch

from agent_observer import cli, interventions, lookup, privacy
from agent_observer.adapters import claude, t3
from tests.helpers import LedgerCase


class ClaudeQueueTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp.name, 'queue-session.jsonl')

    def write(self, records, append=False):
        with open(self.path, 'a' if append else 'w') as fh:
            for record in records:
                fh.write(json.dumps(record) + '\n')

    def queue(self, text, **extra):
        # Native shape from the copied corpus: no uuid or origin fields.
        return dict(type='queue-operation', operation='enqueue',
                    sessionId='queue-session', timestamp='2026-09-14T10:00:00Z',
                    content=text, **extra)

    def user(self, uuid, text, **extra):
        return dict(type='user', sessionId='queue-session', uuid=uuid,
                    timestamp='2026-09-14T10:00:01Z',
                    message={'role': 'user', 'content': text}, **extra)

    def meta(self, **extra):
        return dict(type='agent-name', sessionId='queue-session',
                    entrypoint='cli', **extra)

    def imported(self, **kwargs):
        return claude.import_claude_file(self.con, self.path, **kwargs)

    def rows(self):
        return self.query('SELECT * FROM user_messages ORDER BY ordinal_num')

    def test_multiple_enqueues_match_across_turns_in_native_order(self):
        self.write([self.meta(), self.queue('First change'),
                    self.queue('Second change'), self.queue('First change'),
                    self.user('one', 'First change'),
                    self.user('two', 'Second change'),
                    self.user('three', 'First change'),
                    self.user('four', 'First change')])
        stats = self.imported()
        rows = self.rows()
        self.assertEqual(stats['malformed'], 0)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r['is_genuine'] for r in rows))
        self.assertEqual([r['turn_id'] for r in rows],
                         ['claude:one', 'claude:two', 'claude:three', 'claude:four'])
        self.assertEqual({r['native_id'] for r in rows} | {r['alias_id'] for r in rows},
                         {r['native_id'] for r in rows} |
                         {'claude:one', 'claude:two', 'claude:three', None})
        ids = [r['native_id'] for r in rows]
        self.imported(full=True)
        self.assertEqual([r['native_id'] for r in self.rows()], ids)
        self.assertTrue(self.imported()['unchanged'])

    def test_unmatched_main_queue_uses_privacy_and_can_be_read_by_judge(self):
        text = 'Use the other file <task-notification>PRIVATE-QUEUE-TAIL</task-notification>'
        self.write([self.meta(), self.queue(text)])
        with patch.object(privacy, 'submission_excerpt', wraps=privacy.submission_excerpt) as gate:
            stats = self.imported()
        self.assertEqual(stats['malformed'], 0)
        self.assertEqual(len(self.rows()), 1)
        row = self.rows()[0]
        self.assertEqual(row['kind'], 'genuine')
        self.assertEqual(row['text_excerpt'], 'Use the other file')
        self.assertTrue(gate.called)
        index = lookup._index_claude(self.path)
        self.assertEqual(interventions._record_text('claude', self.path, index,
                                                   row['ordinal_num'], row['native_id']), text)
        self.assertNotIn('PRIVATE-QUEUE-TAIL', '\n'.join(self.con.iterdump()))

    def test_later_match_keeps_primary_key_and_foreign_keys(self):
        self.write([self.meta(), self.queue('First change'), self.queue('Second change')])
        self.imported()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        ids = [r['native_id'] for r in rows]
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES('queue-task',0)")
        for native_id in ids:
            self.con.execute('INSERT INTO assignments(user_message_id,task_id,created_at) VALUES(?,?,0)',
                             (native_id, 'queue-task'))
            self.con.execute('INSERT INTO delegations(owning_user_message,child_thread,created_at) VALUES(?,?,0)',
                             (native_id, 'child:' + native_id))
        self.con.commit()
        self.write([self.user('one', 'First change'), self.user('two', 'Second change')], append=True)
        self.assertTrue(self.imported()['incremental'])
        self.assertEqual([r['native_id'] for r in self.rows()], ids)
        self.assertEqual([r['alias_id'] for r in self.rows()], ['claude:one', 'claude:two'])
        self.assertEqual(self.query('PRAGMA foreign_key_check'), [])
        self.assertEqual(cli._resolve_submission(self.con, 'claude:one'), ids[0])
        self.imported(full=True)
        self.assertEqual([r['native_id'] for r in self.rows()], ids)
        self.assertEqual(self.query('PRAGMA foreign_key_check'), [])

    def test_late_child_identity_reclassifies_an_already_imported_queue(self):
        self.write([self.meta(), self.queue('Child instruction')])
        self.imported()
        self.assertEqual(len(self.rows()), 1)
        native_id = self.rows()[0]['native_id']
        self.write([self.user('child', 'Child instruction', isSidechain=True, agentId='child-id')], append=True)
        self.imported()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['native_id'], native_id)
        self.assertEqual(rows[0]['session_key'], 'claude:queue-session:agent:child-id')
        self.assertEqual(rows[0]['kind'], 'synthetic')
        self.assertEqual(rows[0]['is_genuine'], 0)
        self.assertEqual(rows[0]['text_excerpt'], '')
        self.assertFalse(claude.prompt_origin_allowed(self.con, native_id))
        self.imported(full=True)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['native_id'], native_id)

    def test_sidechain_and_child_file_queues_are_never_genuine(self):
        self.write([self.meta(), self.queue('SIDECHAIN-PRIVATE', isSidechain=True)])
        self.imported()
        self.assertEqual([r['kind'] for r in self.rows()], ['synthetic'])
        child_dir = os.path.join(self.tmp.name, 'another-session', 'subagents')
        os.makedirs(child_dir)
        self.path = os.path.join(child_dir, 'agent-child.jsonl')
        self.write([self.meta(), self.queue('CHILD-FILE-PRIVATE')])
        self.imported()
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(r['kind'] == 'synthetic' and not r['text_excerpt'] for r in self.rows()))
        blob = '\n'.join(self.con.iterdump())
        self.assertNotIn('SIDECHAIN-PRIVATE', blob)
        self.assertNotIn('CHILD-FILE-PRIVATE', blob)

    def test_sdk_and_scaffolding_queues_do_not_invent_authorship(self):
        self.write([dict(self.meta(), entrypoint='sdk-ts'),
                    self.queue('Unknown SDK input'),
                    self.queue('<system-reminder>PRIVATE-SYSTEM</system-reminder>'),
                    self.user('sdk', 'Unknown SDK input', promptSource='sdk'),
                    self.queue('Unmatched SDK input')])
        self.imported()
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(r['kind'] for r in rows), ['scaffolding', 'unknown', 'unknown'])
        self.assertTrue(all(not r['text_excerpt'] and not r['is_genuine'] for r in rows))

    def test_alias_retains_exact_native_origin_backfill(self):
        self.write([dict(self.meta(), entrypoint='sdk-ts'), self.queue('SDK input'),
                    self.user('sdk', 'SDK input', promptSource='sdk')])
        self.imported()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        stable = rows[0]['native_id']
        t3._ensure_tables(self.con)
        self.con.execute('INSERT INTO t3_native_prompt_origins VALUES(?,?,?)',
                         ('claude:queue-session', 'claude:sdk', 'client'))
        t3._backfill_prompt_origins(self.con)
        self.assertEqual(self.rows()[0]['kind'], 'genuine')
        self.assertEqual(self.rows()[0]['text_excerpt'], 'SDK input')
        self.assertEqual(self.rows()[0]['native_id'], stable)
        selected = interventions.select(self.con, created_by=('user',))
        self.assertEqual(len(selected['interventions']), 1)
        self.assertEqual(selected['interventions'][0]['created_by'], 'user')
        self.con.execute('DELETE FROM t3_native_prompt_origins')
        t3._clear_retired_actors(self.con, [self.rows()[0]])
        self.assertEqual(self.rows()[0]['kind'], 'unknown')
        self.assertEqual(self.rows()[0]['text_excerpt'], '')

    def test_old_ledger_replay_keeps_existing_user_ids_and_clears_queue_errors(self):
        self.write([self.meta(), self.queue('Existing input'), self.user('old', 'Existing input')])
        self.imported()
        # Emulate the released adapter: only canonical user rows and queue quarantine.
        self.con.execute('DELETE FROM claude_native_kinds')
        self.con.execute('DELETE FROM user_messages')
        self.con.execute("INSERT INTO user_messages(native_id,source_id,session_key,turn_id,ordinal_num,kind,text_hash,text_excerpt,is_genuine) VALUES('claude:old',1,'claude:queue-session','claude:old',2,'genuine',?,'Existing input',1)",
                         (claude.text_hash('Existing input'),))
        self.con.execute("INSERT INTO tasks(task_id,created_at) VALUES('legacy-task',0)")
        self.con.execute("INSERT INTO assignments(user_message_id,task_id,created_at) VALUES('claude:old','legacy-task',0)")
        self.con.execute('UPDATE sources SET claude_usage_version=1')
        self.con.execute("INSERT INTO import_errors(harness,source_path,ordinal_num,error,line_excerpt,created_at) VALUES('claude',?,1,'unsupported_schema','content,operation,sessionId,timestamp,type',0)", (self.path,))
        self.con.commit()
        self.assertFalse(self.imported()['unchanged'])
        self.assertEqual([r['native_id'] for r in self.rows()], ['claude:old'])
        self.assertEqual(self.query('PRAGMA foreign_key_check'), [])
        self.assertEqual(self.query('SELECT * FROM import_errors'), [])

    def test_only_supported_queue_shapes_are_accepted(self):
        self.write([self.meta(), dict(self.queue('ignored'), operation='dequeue'),
                    dict(self.queue('ignored'), operation='remove'),
                    dict(self.queue('PRIVATE-FUTURE'), operation='future'),
                    self.queue(42),
                    {key: value for key, value in self.queue('no text').items() if key != 'content'},
                    self.queue('Valid prompt')])
        stats = self.imported()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['text_excerpt'], 'Valid prompt')
        self.assertEqual(stats['malformed'], 2)
        self.assertNotIn('PRIVATE-FUTURE', '\n'.join(self.con.iterdump()))

    def test_user_before_same_text_enqueue_is_a_distinct_input(self):
        self.write([self.meta(), self.user('direct', 'Repeat this'),
                    self.queue('Repeat this'), self.user('later', 'Repeat this')])
        self.imported()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual([r['turn_id'] for r in self.rows()], ['claude:direct', 'claude:later'])

    def test_late_main_entrypoint_revisits_unknown_without_changing_id(self):
        self.write([self.queue('Previously unknown input')])
        self.imported()
        row = self.rows()[0]
        self.assertEqual(row['kind'], 'unknown')
        self.write([self.meta()], append=True)
        self.imported()
        self.assertEqual(self.rows()[0]['native_id'], row['native_id'])
        self.assertEqual(self.rows()[0]['kind'], 'genuine')
        self.assertEqual(self.rows()[0]['text_excerpt'], 'Previously unknown input')

    def test_late_sidechain_marker_only_reclassifies_its_matching_queue(self):
        self.write([self.meta(), self.queue('Private sidechain input')])
        self.imported()
        native_id = self.rows()[0]['native_id']
        self.write([self.user('side', 'Private sidechain input', isSidechain=True),
                    self.user('ordinary', 'Ordinary main input')], append=True)
        self.imported()
        self.assertEqual(self.rows()[0]['native_id'], native_id)
        self.assertEqual(self.rows()[0]['kind'], 'synthetic')
        self.assertEqual(self.rows()[0]['text_excerpt'], '')
        self.assertFalse(claude.prompt_origin_allowed(self.con, native_id))
        self.assertEqual(self.rows()[1]['kind'], 'genuine')

    def test_identical_text_in_another_session_does_not_consume_queue(self):
        self.write([self.meta(), self.queue('Repeated input')])
        self.imported()
        self.path = os.path.join(self.tmp.name, 'other-session.jsonl')
        self.write([dict(self.meta(), sessionId='other-session'),
                    dict(self.user('other', 'Repeated input'), sessionId='other-session')])
        self.imported()
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(r['alias_id'] is None for r in self.rows()))

    def test_split_import_matches_fifo_over_several_turn_boundaries(self):
        self.write([self.meta(), self.queue('Repeat this'), self.queue('Other input'),
                    self.queue('Repeat this')])
        self.imported()
        ids = [r['native_id'] for r in self.rows()]
        self.write([self.user('one', 'Repeat this')], append=True)
        self.imported()
        self.write([self.user('two', 'Other input'), self.user('three', 'Repeat this')], append=True)
        self.imported()
        self.assertEqual([r['native_id'] for r in self.rows()], ids)
        self.assertEqual([r['alias_id'] for r in self.rows()], ['claude:one', 'claude:two', 'claude:three'])
        self.imported(full=True)
        self.assertEqual([r['native_id'] for r in self.rows()], ids)

    def test_sidechain_enqueue_cannot_become_a_genuine_main_input(self):
        self.write([self.meta(), self.queue('Same text', isSidechain=True),
                    self.user('main', 'Same text')])
        self.imported()
        self.assertEqual([r['kind'] for r in self.rows()], ['synthetic', 'genuine'])
        self.assertTrue(all(r['alias_id'] is None for r in self.rows()))
        self.imported(full=True)
        self.assertEqual([r['kind'] for r in self.rows()], ['synthetic', 'genuine'])

    def test_upgrade_does_not_pair_queue_with_a_non_user_attachment(self):
        self.write([self.meta(), self.queue('Same text'),
                    {'type': 'attachment', 'sessionId': 'queue-session', 'uuid': 'attachment',
                     'attachment': {'type': 'queued_command', 'prompt': 'Same text'}}])
        self.imported()
        self.con.execute("DELETE FROM user_messages WHERE native_id LIKE 'claude:queue:%'")
        self.con.execute('UPDATE sources SET claude_usage_version=1')
        self.con.commit()
        self.imported()
        self.assertEqual([r['kind'] for r in self.rows()], ['genuine', 'synthetic'])
        self.assertTrue(all(r['alias_id'] is None for r in self.rows()))
