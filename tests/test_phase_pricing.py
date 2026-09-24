"""Native cache evidence and disjoint work-phase consumption."""
import json
from pathlib import Path

from agent_observer import db, pricing, report
from agent_observer.adapters import claude
from tests.test_close_loop import CloseLoopCase, run


class PhasePricingTest(CloseLoopCase):
    def test_ttl_upgrade_uses_final_record_not_provisional_split(self):
        con=db.connect(':memory:'); self.addCleanup(con.close); db.init_db(con)
        usage=dict(input_tokens=2,output_tokens=100,cache_read_input_tokens=0,
                   cache_creation_input_tokens=100,output_tokens_details={'thinking_tokens':10},
                   cache_creation={'ephemeral_5m_input_tokens':100,'ephemeral_1h_input_tokens':0})
        first=dict(type='assistant',sessionId='stream',uuid='a',message=dict(
            id='response',model='claude-fable-5-1',content=[],usage=usage,stop_reason=None))
        final=json.loads(json.dumps(first)); final['uuid']='b'
        final['message']['stop_reason']='end_turn'
        final['message']['usage']['cache_creation']={'ephemeral_5m_input_tokens':0,'ephemeral_1h_input_tokens':100}
        path=Path(self.tmp.name)/'stream.jsonl'
        path.write_text(json.dumps(first)+'\n'+json.dumps(final)+'\n')
        claude.import_claude_file(con,str(path))
        fresh=dict(con.execute('SELECT * FROM responses').fetchone())
        con.execute('UPDATE responses SET cache_write_5m_tokens=NULL,cache_write_1h_tokens=NULL')
        con.execute('UPDATE sources SET claude_usage_version=NULL');con.commit()
        result=claude.import_claude_file(con,str(path))
        upgraded=dict(con.execute('SELECT * FROM responses').fetchone())
        self.assertEqual(result['malformed'],0)
        self.assertEqual(upgraded['cache_write_1h_tokens'],100)
        self.assertEqual(pricing.price_response(upgraded,pricing.load_schedule()),
                         pricing.price_response(fresh,pricing.load_schedule()))

    def test_claude_native_ttl_backfill_prices_without_changing_usage(self):
        con = db.connect(':memory:')
        self.addCleanup(con.close)
        db.init_db(con)
        usage = dict(input_tokens=2, output_tokens=2623, cache_read_input_tokens=0,
                     cache_creation_input_tokens=150702,
                     output_tokens_details={'thinking_tokens':1860},
                     cache_creation={'ephemeral_5m_input_tokens':0,
                                     'ephemeral_1h_input_tokens':150702})
        obj = dict(type='assistant', sessionId='ttl-session', uuid='record',
                   timestamp='2026-09-24T10:00:00Z', message=dict(
                       id='ttl-response', model='claude-fable-5-1', content=[],
                       usage=usage, stop_reason='end_turn'))
        path=Path(self.tmp.name)/'ttl.jsonl'; path.write_text(json.dumps(obj)+'\n')
        claude.import_claude_file(con,str(path))
        row=dict(con.execute('SELECT * FROM responses').fetchone())
        self.assertAlmostEqual(pricing.price_response(row,pricing.load_schedule())[0],3.14521)
        con.execute('UPDATE responses SET cache_write_5m_tokens=NULL,cache_write_1h_tokens=NULL')
        con.execute('UPDATE sources SET claude_usage_version=NULL')
        con.commit()
        claude.import_claude_file(con,str(path))
        repaired=dict(con.execute('SELECT * FROM responses').fetchone())
        self.assertEqual(repaired['total_tokens'],153327)
        self.assertEqual(repaired['cache_write_1h_tokens'],150702)
        self.assertTrue(claude.import_claude_file(con,str(path))['unchanged'])
        # Contradictory TTL on a finalized response cannot rewrite evidence.
        usage['cache_creation']={'ephemeral_5m_input_tokens':150702,'ephemeral_1h_input_tokens':0}
        path.write_text(json.dumps(obj)+'\n')
        result=claude.import_claude_file(con,str(path))
        self.assertEqual(result['malformed'],1)
        self.assertEqual(con.execute('SELECT cache_write_1h_tokens FROM responses').fetchone()[0],150702)

    def test_free_muse_is_priced_without_paid_model_substitution(self):
        row=dict(model='muse-spark-1.3-contributor-free',
                 semantics='opencode:input_excludes_cache,reasoning_separate',
                 input_tokens=100,cached_input_tokens=1000,cache_write_input_tokens=0,
                 output_tokens=20,reasoning_output_tokens=10,total_tokens=1130)
        self.assertEqual(pricing.price_response(row,pricing.load_schedule()),(0.0,'priced'))

    def test_phase_partition_and_thinking_are_separate_dimensions(self):
        run(self.db,'sync','--source',self.mini)
        run(self.db,'capture','create-task','--task','T')
        for sub,phase in [('msg-mini-sub-01','implementation'),('msg-mini-sub-02','review')]:
            run(self.db,'capture','assign','--task','T','--submission',sub,'--phase',phase)
        r=self._task_json('T')
        phases={p['phase']:p for p in r['phases']}
        self.assertEqual(phases['implementation']['usage']['total_tokens'],3350)
        self.assertEqual(phases['review']['usage']['total_tokens'],2150)
        model=phases['implementation']['models'][0]
        self.assertEqual(model['generation']['reasoning_tokens'],40)
        self.assertEqual(model['generation']['other_output_tokens'],310)
        self.assertAlmostEqual(model['generation']['reasoning_share'],40/350)
        self.assertEqual(sum(p['usage']['responses'] for p in r['phases']),r['attributed']['responses'])
        pub=self._publish_json('T')
        self.assertEqual(pub['summary']['phases'],r['phases'])
        self.assertIn('Usage by work phase',pub['body'])
        self.assertIn('Reasoning',pub['body'])
        # The fixture's MCP result lacks native turn identity. Keep it in
        # session context rather than guessing which phase owns it.
        self.assertEqual(r['activity_session_context']['mcp_results'],1)
        self.assertEqual(phases['implementation']['activity']['mcp_results'],0)
        con=db.connect(self.db); self.addCleanup(con.close)
        con.execute("UPDATE events SET turn_id='codex:turn-mini-bbb' WHERE name='mcp.unknown'")
        con.commit()
        phases={p['phase']:p for p in self._task_json('T')['phases']}
        self.assertEqual(phases['review']['activity']['mcp_results'],1)

    def test_phase_capture_changes_snapshot_and_survives_state_update(self):
        self._two_tasks()
        before=self._task_json('T-A')
        self.assertEqual(before['phases'][0]['phase'],'unclassified')
        p=run(self.db,'capture','attempt','--task','T-A','--turn','codex:turn-mini-aaa',
              '--role','parent','--phase','mixed')
        self.assertEqual(p.returncode,0,p.stderr)
        run(self.db,'capture','attempt','--task','T-A','--turn','codex:turn-mini-aaa',
            '--role','parent','--state','complete')
        after=self._task_json('T-A')
        self.assertEqual(after['phases'][0]['phase'],'mixed')
        self.assertEqual(after['phases'][0]['usage']['total_tokens'],3350)
        self.assertNotEqual(after['snapshot_id'],before['snapshot_id'])

    def test_router_stage_labels_only_dedicated_owned_session(self):
        run(self.db,'sync','--source',self.mini)
        run(self.db,'capture','create-task','--task','T')
        run(self.db,'capture','assign-session','--task','T','--session','codex:sess-fixture-mini-01',
            '--exclusive','--evidence','dedicated fixture')
        con=db.connect(self.db); self.addCleanup(con.close)
        con.execute("INSERT INTO attempts(task_id,turn_id,role,harness,session_key,stage,state)"
                    " VALUES('T','router:inv','worker','router','codex:sess-fixture-mini-01',"
                    "'implementation_hard','complete')")
        con.commit()
        r=self._task_json('T')
        self.assertEqual(r['phases'][0]['phase'],'implementation')
        self.assertEqual(r['phases'][0]['usage']['total_tokens'],5500)
        run(self.db,'capture','assign','--task','T','--submission','msg-mini-sub-01')
        r=self._task_json('T')
        self.assertEqual([(p['phase'],p['usage']['total_tokens']) for p in r['phases']],
                         [('implementation',5500)])
        con.execute("INSERT INTO attempts(task_id,turn_id,role,harness,session_key,stage,state)"
                    " VALUES('T','router:review','worker','router','codex:sess-fixture-mini-01',"
                    "'review_final','complete')")
        con.commit()
        self.assertEqual(self._task_json('T')['phases'][0]['phase'],'mixed')

    def test_unknown_reasoning_is_not_zero_or_needed_for_same_output_rate(self):
        row=dict(model='claude-fable-5-1',harness='claude',effort=None,is_overlap=0,
                 semantics='claude:input_excludes_cache,output_includes_thinking',
                 input_tokens=2,cached_input_tokens=0,cache_write_input_tokens=0,
                 output_tokens=100,reasoning_output_tokens=None,total_tokens=102)
        self.assertAlmostEqual(pricing.price_response(row,pricing.load_schedule())[0],.00502)
        m=report.model_usage([row])[0]
        self.assertIsNone(m['generation']['reasoning_tokens'])
        self.assertIsNone(m['generation']['other_output_tokens'])
        self.assertIsNone(m['generation']['reasoning_share'])
