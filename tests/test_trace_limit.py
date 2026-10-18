"""Trace paging stays visible across selectors, using only synthetic events."""

import json

from tests.helpers import LedgerCase
from tests.test_cli import run


class TraceLimitTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.con.execute(
            "INSERT INTO tasks(task_id, created_at) VALUES('trace-task', 0)")
        self.con.execute(
            "INSERT INTO session_assignments VALUES"
            "('test:trace', 'trace-task', 'synthetic', 0)")
        self.con.executemany(
            "INSERT INTO events(session_key, ordinal_num, family, native_id, "
            "turn_id) VALUES('test:trace', ?, 'tool_call', ?, 'trace-turn')",
            [(n, f'call-{n}') for n in range(201)])
        self.con.execute(
            "INSERT INTO events(session_key, family, native_id) "
            "VALUES('test:other', 'read', 'other')")
        self.con.commit()

    def trace(self, *args, as_json=False):
        result = run(self.db_path, 'trace', *args,
                     *(['--json'] if as_json else []))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if as_json else result.stdout

    def test_default_limit_reports_cut_for_all_event_selectors(self):
        for selector, total in (
                (('--session', 'test:trace'), 201),
                (('--task', 'trace-task'), 201),
                (('--turn', 'trace-turn'), 201),
                (('--family', 'tool_call'), 201),
                ((), 202)):
            with self.subTest(selector=selector):
                text = self.trace(*selector)
                self.assertIn(f'200 of {total}', text)
                self.assertIn('truncated', text)
                self.assertIn('--limit', text)
                payload = self.trace(*selector, as_json=True)
                self.assertEqual(payload['returned'], 200)
                self.assertEqual(payload['total'], total)
                self.assertTrue(payload['truncated'])
                self.assertEqual(len(payload['events']), 200)
                self.assertIn('--limit', payload['truncation_hint'])

    def test_explicit_limit_and_larger_limit(self):
        small = self.trace('--session', 'test:trace', '--limit', '1',
                           as_json=True)
        self.assertEqual((small['returned'], small['total'],
                          small['truncated']), (1, 201, True))
        self.assertIn('1 of 201', self.trace(
            '--session', 'test:trace', '--limit', '1'))
        for limit in ('201', '300'):
            with self.subTest(limit=limit):
                payload = self.trace('--session', 'test:trace', '--limit',
                                     limit, as_json=True)
                self.assertEqual((payload['returned'], payload['total'],
                                  payload['truncated']), (201, 201, False))
                text = self.trace('--session', 'test:trace', '--limit', limit)
                self.assertTrue(text.startswith('events: 201\n'))
                self.assertNotIn('truncated', text)
                self.assertNotIn('--limit', text)
                self.assertIn('... (151 more)', text)

    def test_filtered_and_empty_results_are_not_truncated(self):
        for selector, count in (
                (('--family', 'read'), 1),
                (('--session', 'test:trace', '--family', 'read'), 0),
                (('--task', 'unbound'), 0)):
            with self.subTest(selector=selector):
                payload = self.trace(*selector, as_json=True)
                self.assertEqual((payload['returned'], payload['total'],
                                  payload['truncated']), (count, count, False))
                self.assertEqual(self.trace(*selector),
                                 f'events: {count}\njoined call_ids: 0\n'
                                 'unmatched results: 0\nunanswered calls: 0\n'
                                 + ('  #None read  [unknown]\n' if count else ''))
