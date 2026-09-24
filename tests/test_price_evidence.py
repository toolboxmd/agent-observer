"""Retained valuations survive catalogue changes through the public CLI."""
import hashlib
import json
import sqlite3
from pathlib import Path

from agent_observer import db
from tests.test_close_loop import CloseLoopCase, fixture_rates, write_prices, run


class PriceEvidenceTest(CloseLoopCase):
    def test_missing_ledger_requires_sync_without_creating_files(self):
        result = run(self.db, 'task', 'show', '--task', 'T-A', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('sync first', result.stderr)
        self.assertFalse(Path(self.db).exists())

    def test_read_only_connection_keeps_snapshot_and_refuses_writes(self):
        self._two_tasks()
        reader = db.connect_read_only(self.db)
        self.addCleanup(reader.close)
        before = reader.execute("SELECT title FROM tasks WHERE task_id='T-A'").fetchone()[0]
        with sqlite3.connect(self.db) as writer:
            writer.execute("UPDATE tasks SET title='new evidence' WHERE task_id='T-A'")
        self.assertEqual(reader.execute("SELECT title FROM tasks WHERE task_id='T-A'").fetchone()[0], before)
        with self.assertRaises(sqlite3.OperationalError):
            reader.execute("UPDATE tasks SET title='forbidden' WHERE task_id='T-B'")
        self.assertEqual(self._task_json('T-A')['task']['title'], 'new evidence')

    def test_report_consumer_can_read_while_another_connection_holds_write_lock(self):
        self._two_tasks()
        with sqlite3.connect(self.db) as writer:
            writer.execute('BEGIN IMMEDIATE')
            local = self._task_json('T-A', '--prices', self.prices)
            pub = self._publish_json('T-A', '--prices', self.prices)
        self.assertEqual(pub['summary']['snapshot_id'], local['snapshot_id'])

    def test_saved_schedule_reproduces_old_valuation_after_rate_change(self):
        self._two_tasks()
        old = self._task_json('T-A', '--prices', self.prices)
        schedule = old['price_schedule']
        expected_id = hashlib.sha256(json.dumps(
            schedule, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(old['price_schedule_id'], expected_id)
        self.assertAlmostEqual(old['estimated_cost']['estimated_cost_usd_total'], .0073)
        saved = Path(self.tmp.name) / 'saved-schedule.json'
        saved.write_text(json.dumps(schedule))

        rates = fixture_rates()
        rates['gpt-6-fixture']['rates']['input_tokens'] = 4
        write_prices(self.prices, rates)
        new = self._task_json('T-A', '--prices', self.prices)
        self.assertAlmostEqual(new['estimated_cost']['estimated_cost_usd_total'], .0103)
        self.assertNotEqual(new['price_schedule_id'], old['price_schedule_id'])
        self.assertNotEqual(new['snapshot_id'], old['snapshot_id'])
        replay = self._task_json('T-A', '--prices', str(saved))
        self.assertEqual(replay, old)

    def test_publication_uses_same_evidence_and_only_renders_reported_rates(self):
        self._two_tasks()
        rates = fixture_rates()
        rates['gpt-6-fixture']['rates']['cache_write_input_tokens'] = {'5m': 3, '1h': 4}
        rates['gpt-6-fixture']['long_context_threshold'] = 272000
        rates['gpt-6-fixture']['long_context_rates'] = {
            'input_tokens': 4, 'cached_input_tokens': 2,
            'output_tokens': 12, 'reasoning_output_tokens': 12}
        rates['unused-private-model'] = fixture_rates()['gpt-6-fixture']
        write_prices(self.prices, rates, {'private_note': 'DO_NOT_PUBLISH'})
        local = self._task_json('T-A', '--prices', self.prices)
        pub = self._publish_json('T-A', '--prices', self.prices)
        for key in ('price_schedule', 'price_schedule_id', 'estimated_cost',
                    'phases', 'models', 'snapshot_id'):
            self.assertEqual(pub['summary'][key], local[key])
        self.assertIn(local['price_schedule_id'], pub['body'])
        self.assertIn('USD per million tokens', pub['body'])
        self.assertIn('272000', pub['body'])
        self.assertIn('5m: 3', pub['body'])
        self.assertIn('1h: 4', pub['body'])
        self.assertNotIn('unused-private-model', pub['body'])
        self.assertNotIn('DO_NOT_PUBLISH', pub['body'])
        self.assertNotIn(self.tmp.name, pub['body'])

    def test_retained_unknown_price_does_not_become_zero(self):
        self._two_tasks()
        write_prices(self.prices, {})
        old = self._task_json('T-A', '--prices', self.prices)
        self.assertIsNone(old['estimated_cost']['estimated_cost_usd_total'])
        saved = Path(self.tmp.name) / 'unknown-schedule.json'
        saved.write_text(json.dumps(old['price_schedule']))
        write_prices(self.prices, fixture_rates())
        self.assertIsNotNone(self._task_json('T-A', '--prices', self.prices)
                             ['estimated_cost']['estimated_cost_usd_total'])
        replay = self._task_json('T-A', '--prices', str(saved))
        self.assertEqual(replay, old)
