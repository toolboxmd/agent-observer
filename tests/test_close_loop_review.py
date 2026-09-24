"""Regressions from independent review of the task outcome account."""
import json
import os
import sqlite3
from pathlib import Path

from tests.test_close_loop import CloseLoopCase, CODEX_SEM, fixture_rates, run
from agent_observer import db, pricing, report
from agent_observer.adapters import codex, router


class ReviewRegressions(CloseLoopCase):
    def _context_records(self, fallback):
        records = [json.loads(x) for x in Path(self.mini).read_text().splitlines()]
        usage = records[9]
        if fallback:
            usage = dict(type='event_msg', ordinal=9, timestamp=usage['timestamp'],
                         payload=dict(type='token_count', info=dict(
                             last_token_usage=usage['payload']['usage'],
                             total_token_usage=usage['payload']['thread_token_usage'])))
        return records[:3], usage

    def test_repeated_usage_preserves_original_model_on_append_and_replay(self):
        con = self.connect()
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                prefix, usage = self._context_records(fallback)
                path = Path(self.tmp.name) / f'repeated-{fallback}.jsonl'
                records = prefix + [usage]
                path.write_text(''.join(json.dumps(r)+'\n' for r in records))
                codex.import_codex_file(con, str(path))
                context = json.loads(json.dumps(prefix[2]))
                context['payload'].update(model='later-model', effort='low')
                with path.open('a') as stream:
                    stream.write(json.dumps(context)+'\n'+json.dumps(usage)+'\n')
                for full in (False, True):
                    codex.import_codex_file(con, str(path), full=full)
                    self.assertEqual({tuple(r) for r in con.execute(
                        'SELECT model,effort FROM responses')}, {('gpt-6-fixture','high')})

    def test_replay_repairs_false_late_model_to_unknown(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                con = db.connect(':memory:')
                self.addCleanup(con.close)
                db.init_db(con)
                prefix, usage = self._context_records(fallback)
                path = Path(self.tmp.name) / f'unknown-{fallback}.jsonl'
                records = prefix[:2] + [usage, prefix[2]]
                path.write_text(''.join(json.dumps(r)+'\n' for r in records))
                codex.import_codex_file(con, str(path))
                con.execute("UPDATE responses SET model='false-late-model',effort='high'")
                con.execute('UPDATE sources SET codex_context_version=NULL')
                con.commit()
                codex.import_codex_file(con, str(path))
                self.assertEqual([tuple(r) for r in con.execute(
                    'SELECT model,effort FROM responses')], [(None,None)])

    def test_copied_checkpoint_cannot_repair_original_context(self):
        con = self.connect()
        for fallback in (False, True):
            prefix, usage = self._context_records(fallback)
            original = Path(self.tmp.name) / f'original-{fallback}.jsonl'
            copy = Path(self.tmp.name) / f'copy-{fallback}.jsonl'
            original.write_text(''.join(json.dumps(r)+'\n' for r in prefix + [usage]))
            codex.import_codex_file(con, str(original))
            copy.write_text(''.join(json.dumps(r)+'\n' for r in prefix[:2] + [usage]))
            codex.import_codex_file(con, str(copy))
            self.assertEqual({tuple(r) for r in con.execute(
                'SELECT model,effort FROM responses')}, {('gpt-6-fixture','high')})

    def test_dispatch_without_worker_ownership_is_incomplete(self):
        self._two_tasks()
        run(self.db, 'capture', 'dispatch', '--submission', 'msg-mini-sub-01',
            '--worker', 'codex:missing-worker')
        r = self._task_json('T-A')
        self.assertFalse(r['complete'])
        self.assertIsNone(r['estimated_cost']['estimated_cost_usd_total'])
        self.assertIsNone(r['native_cost']['total_usd'])
        self.assertEqual(r['coverage']['unbound_dispatches'], ['codex:missing-worker'])

    def test_dispatch_with_owned_native_session_has_no_ownership_gap(self):
        self._two_tasks()
        worker = 'codex:sess-fixture-mini-01'
        run(self.db, 'capture', 'dispatch', '--submission', 'msg-mini-sub-01', '--worker', worker)
        r = self._task_json('T-A')
        self.assertEqual(r['coverage']['unbound_dispatches'], [])
        self.assertTrue(r['complete'])

    def test_dispatch_resolves_unique_bare_native_id_but_not_ambiguous_id(self):
        self._two_tasks()
        worker = 'sess-fixture-mini-01'
        run(self.db, 'capture', 'dispatch', '--submission', 'msg-mini-sub-01', '--worker', worker)
        r = self._task_json('T-A')
        self.assertEqual(r['coverage']['unbound_dispatches'], [])
        self.assertTrue(r['complete'])
        con = self.connect()
        con.execute("INSERT INTO sessions(session_key,harness,native_id,updated_at)"
                    " VALUES(?,?,?,1)", ('claude:'+worker,'claude',worker))
        con.commit()
        r = self._task_json('T-A')
        self.assertEqual(r['coverage']['unbound_dispatches'], [worker])
        self.assertFalse(r['complete'])

    def connect(self):
        con = db.connect(self.db)
        db.init_db(con)
        self.addCleanup(con.close)
        return con

    def test_router_does_not_override_explicit_submission_ownership(self):
        self._two_tasks()
        con = self.connect()
        con.execute("INSERT INTO tasks(task_id,created_at) VALUES('T-C',1)")
        session = con.execute('SELECT session_key FROM sessions').fetchone()[0]
        inv = dict(request_id='job', invocation_id='worker', session_kind='codex_task_id',
                   session_id=session.split(':', 1)[1])
        self.assertIsNone(router._binding_key(con, inv, task_id='T-C'))
        self.assertEqual(report.task_report(con, 'T-A')['attributed']['total_tokens'], 3350)

    def test_missing_worker_transcript_is_not_complete_zero_cost(self):
        con = self.connect()
        con.execute("INSERT INTO tasks(task_id,created_at) VALUES('missing',1)")
        con.execute("INSERT INTO session_assignments VALUES('codex:missing','missing','router:job:inv',1)")
        r = report.task_report(con, 'missing')
        self.assertFalse(r['complete'])
        self.assertEqual(r['coverage']['missing_sessions'], ['codex:missing'])
        self.assertIsNone(r['estimated_cost']['estimated_cost_usd_total'])
        self.assertIsNone(r['native_cost']['total_usd'])

    def test_other_task_binding_changes_snapshot_when_coverage_changes(self):
        run(self.db, 'sync', '--source', self.mini)
        for task in ('A', 'B'):
            run(self.db, 'capture', 'create-task', '--task', task)
        run(self.db, 'capture', 'assign', '--task', 'A', '--submission', 'msg-mini-sub-01')
        before = json.loads(run(self.db, 'task', 'show', '--task', 'A', '--json').stdout)
        run(self.db, 'capture', 'assign', '--task', 'B', '--submission', 'msg-mini-sub-02')
        after = self._task_json('A')
        self.assertNotEqual(before['snapshot_id'], after['snapshot_id'])

    def test_long_context_needs_explicit_nonzero_component_rates(self):
        entry = fixture_rates()['gpt-6-fixture']
        entry.update(long_context_threshold=10, long_context_rates={})
        schedule = pricing.validate_schedule(dict(source_url='https://example.com/prices',
            as_of='2026-09-24', models={'gpt-6-fixture': entry}))
        response = dict(model='gpt-6-fixture', semantics=CODEX_SEM,
                        input_tokens=100, cached_input_tokens=50, cache_write_input_tokens=0,
                        output_tokens=10, reasoning_output_tokens=2)
        self.assertIsNone(pricing.price_response(response, schedule)[0])
        entry['long_context_rates'] = {'input_tokens': 4}
        self.assertIsNone(pricing.price_response(response, schedule)[0])

    def test_incremental_codex_keeps_native_model_context(self):
        con = self.connect()
        lines = Path(self.mini).read_text().splitlines(keepends=True)
        path = Path(self.tmp.name) / 'growing.jsonl'
        path.write_text(''.join(lines[:10]))
        codex.import_codex_file(con, str(path))
        path.write_text(''.join(lines[:11]))
        codex.import_codex_file(con, str(path))
        models = [r[0] for r in con.execute('SELECT model FROM responses')]
        self.assertEqual(models, ['gpt-6-fixture', 'gpt-6-fixture'])

    def test_old_model_metadata_is_repaired_in_place_once(self):
        con = self.connect()
        codex.import_codex_file(con, self.mini)
        count = con.execute('SELECT count(*) FROM responses').fetchone()[0]
        con.execute('UPDATE responses SET model=NULL,effort=NULL')
        con.execute('UPDATE sources SET codex_context_version=NULL')
        con.commit()
        codex.import_codex_file(con, self.mini)
        self.assertEqual(con.execute('SELECT count(*) FROM responses').fetchone()[0], count)
        self.assertEqual(con.execute('SELECT count(*) FROM responses WHERE model IS NULL').fetchone()[0], 0)
        self.assertTrue(codex.import_codex_file(con, self.mini)['unchanged'])

    def test_fallback_checkpoints_keep_model_at_each_native_record(self):
        con = self.connect()
        prefix = [json.loads(x) for x in Path(self.mini).read_text().splitlines()[:3]]
        def checkpoint(total, ordinal):
            bucket = dict(input_tokens=100, cached_input_tokens=0,
                          cache_write_input_tokens=0, output_tokens=10,
                          reasoning_output_tokens=0,total_tokens=110)
            cumulative = dict(bucket, input_tokens=total-10, total_tokens=total)
            return dict(type='event_msg', ordinal=ordinal, timestamp='2026-09-24T10:00:00Z',
                        payload=dict(type='token_count', info=dict(
                            last_token_usage=bucket,total_token_usage=cumulative)))
        second_context = json.loads(json.dumps(prefix[2]))
        second_context['payload']['model'] = 'second-model'
        records = prefix + [checkpoint(110,3),second_context,checkpoint(220,5)]
        path = Path(self.tmp.name)/'fallback.jsonl'
        path.write_text(''.join(json.dumps(r)+'\n' for r in records))
        codex.import_codex_file(con,str(path))
        self.assertEqual([r[0] for r in con.execute('SELECT model FROM responses ORDER BY thread_total_tokens')],
                         ['gpt-6-fixture','second-model'])

    def test_stale_whole_session_conflict_cannot_double_attribute(self):
        self._two_tasks()
        con = self.connect()
        con.execute("INSERT INTO tasks(task_id,created_at) VALUES('T-C',1)")
        session = con.execute('SELECT session_key FROM sessions').fetchone()[0]
        con.execute('INSERT INTO session_assignments VALUES(?,?,?,1)',(session,'T-C','old-binding'))
        for task in ('T-A','T-C'):
            r=report.task_report(con,task)
            self.assertEqual(r['attributed']['total_tokens'],0)
            self.assertFalse(r['complete'])
            self.assertEqual(r['coverage']['conflicting_sessions'],[session])
