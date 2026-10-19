"""Cue's real SQLite boundary and aggregate report contract."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from helpers import LedgerCase
from agent_observer import cli, cue, fleet


class CueTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.source = Path(self.tmp.name) / 'contextmd.sqlite'
        self.library = Path(self.tmp.name) / 'library'
        self.library.mkdir()
        self.target = str(Path(self.tmp.name) / 'operations/procedures/research/index.md')
        self.raw = ('---\nid: research\nname: Research\napplies: research work\n'
                    f'target: {self.target}\n---\nPRIVATE SERVED TEXT\n').encode()
        (self.library / 'research.md').write_bytes(self.raw)
        self.version = hashlib.sha256(self.raw).hexdigest()[:12]
        self.native = sqlite3.connect(self.source)
        self.native.executescript('''
          CREATE TABLE moments(id INTEGER PRIMARY KEY, received_at REAL, harness TEXT,
            session TEXT, epoch INTEGER, holdout INTEGER, kind TEXT, tool_use_id TEXT,
            owners_json TEXT, cue_status TEXT, delivery TEXT, window TEXT);
          CREATE TABLE candidates(id INTEGER PRIMARY KEY, moment_id INTEGER, matcher TEXT,
            entry TEXT, version TEXT, score REAL, outcome TEXT, stage TEXT, served_at REAL);
          CREATE TABLE reads(harness TEXT,session TEXT,epoch INTEGER,path TEXT,kind TEXT,
            moment_id INTEGER,read_at REAL,pending_tool_use_id TEXT);
          CREATE TABLE deliveries(delivery TEXT,stage TEXT,body TEXT);
        ''')
        self.moment(1, 100)
        self.candidate(1, 1)
        self.moment(2, 110, kind='tool_result', call='read-call')
        self.read(2, 110)
        self.moment(3, 2000, kind='before_tool')
        self.native.commit()

    def tearDown(self):
        self.native.close()
        super().tearDown()

    def moment(self, id, at, session='s', epoch=0, holdout=0, kind='before_tool', call=None):
        self.native.execute('INSERT INTO moments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (id,at,'codex',session,epoch,holdout,kind,call,
             json.dumps({str(self.library): 'abc'}),'ok',None,'PRIVATE WINDOW TEXT'))

    def candidate(self, id, moment, outcome='injected', version=None, entry='research'):
        self.native.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?)',
            (id,moment,'intent',entry,version or self.version,0.9,outcome,'settled',100))

    def read(self, moment, at, session='s', epoch=0, kind='shell_full', pending=None):
        self.native.execute('INSERT INTO reads VALUES(?,?,?,?,?,?,?,?)',
            ('codex',session,epoch,self.target,kind,moment,at,pending))

    def refresh(self):
        self.native.commit()
        result = cue.import_source(self.con, self.source)
        self.assertEqual(result['status'], 'ok')
        return cue.report(self.con, check_source=False, machine='test')

    def arm(self, result, arm='served'):
        return result['groups'][0]['arms'][arm]

    def test_full_partial_and_no_text(self):
        result = self.refresh()
        arm = self.arm(result)
        self.assertEqual((arm['read'],arm['full'],arm['partial'],arm['sessions']), (1,1,0,1))
        self.assertEqual(arm['pending_window'],0)
        text = '\n'.join(self.con.iterdump())
        self.assertNotIn('PRIVATE',text)
        self.assertEqual(result['read_sources'], {'contextmd_only':1})
        self.native.execute("UPDATE reads SET kind='shell_partial'")
        self.assertEqual(self.arm(self.refresh())['partial'],1)

    def test_holdout_is_session_wide_and_counts_sessions(self):
        self.moment(4,100,session='h',holdout=1)
        self.candidate(4,4,'holdout')
        self.moment(5,110,session='h',holdout=1)
        self.read(5,110,session='h',kind='read_partial')
        self.candidate(5,4,'holdout')
        result = self.refresh()
        self.assertEqual(result['holdout_unit'],'session')
        arm = self.arm(result,'holdout')
        self.assertEqual((arm['opportunities'],arm['sessions'],arm['partial']),(1,1,1))
        self.assertEqual(result['groups'][0]['repeats']['holdout']['opportunities'],1)
        self.assertIn('causal', result['interpretation'])

    def test_pending_failed_and_duplicate_reads_not_counted(self):
        self.native.execute("UPDATE reads SET pending_tool_use_id='read-call'")
        self.assertEqual(self.arm(self.refresh())['read'],0)
        self.native.execute('UPDATE reads SET pending_tool_use_id=NULL')
        self.read(2,110)
        result = self.refresh()
        self.assertEqual(self.arm(result)['read'],1)
        self.assertEqual(self.con.execute('SELECT count(*) FROM cue_reads').fetchone()[0],1)
        self.native.execute('DELETE FROM reads')  # failed pending reads are deleted by Chromeria
        self.assertEqual(self.arm(self.refresh())['read'],0)

    def test_native_identity_is_linked_without_duplicate_events(self):
        self.con.execute("INSERT INTO events(session_key,family,native_id,target,detail_json) VALUES(?,?,?,?,?)",
                         ('codex:s','skill_read','read-call:'+self.target,'operations',json.dumps({'skill_path':self.target})))
        result = self.refresh()
        self.assertEqual(result['read_sources'],{'native_and_contextmd':1})
        self.assertEqual(self.arm(result)['native_matched_reads'],1)
        self.assertEqual(self.con.execute('SELECT count(*) FROM events').fetchone()[0],1)
        self.con.execute("UPDATE events SET native_id='different-call:read'")
        self.assertEqual(self.refresh()['read_sources'],{'contextmd_only':1})

    def test_earlier_read_is_separate_and_epoch_scoped(self):
        self.native.execute('DELETE FROM reads')
        self.moment(4,90)
        self.read(4,90,kind='read_partial')
        arm = self.arm(self.refresh())
        self.assertEqual((arm['read'],arm['already_read'],arm['already_partial']),(0,1,1))
        self.native.execute('UPDATE reads SET epoch=1')
        self.assertEqual(self.arm(self.refresh())['already_read'],0)

    def test_window_stops_at_user_message_epoch_or_time_cap(self):
        for kind,epoch,at in [('user_message',0,105),('compaction',1,105),('before_tool',0,1900)]:
            with self.subTest(kind=kind):
                self.native.execute('DELETE FROM moments WHERE id=4')
                self.moment(4,at,epoch=epoch,kind=kind)
                self.native.execute('UPDATE reads SET read_at=?',(at,))
                self.assertEqual(self.arm(self.refresh())['read'],0)

    def test_pending_window_and_watermark_read(self):
        self.native.execute('DELETE FROM moments WHERE id=3')
        arm = self.arm(self.refresh())
        self.assertEqual((arm['read'],arm['pending_window']),(1,1))

    def test_outcomes_and_unavailable_versions_are_not_misclassified(self):
        self.candidate(4,1,'below_threshold')
        self.candidate(5,1,'delivery_unverified')
        self.candidate(6,1,'holdout')  # inconsistent source arm, never added to holdout denominator
        self.candidate(7,1,version='old-version')
        result = self.refresh()
        self.assertEqual(self.arm(result)['opportunities'],1)
        self.assertNotIn('holdout',result['groups'][0]['arms'])
        self.assertEqual(result['gaps'],{'version_unavailable':1})
        self.assertEqual(result['groups'][0]['outcomes']['below_threshold'],1)

    def test_source_is_read_only_and_refresh_idempotent(self):
        before = self.source.read_bytes()
        self.refresh()
        self.refresh()
        self.assertEqual(self.source.read_bytes(),before)
        with contextlib.closing(cue._open(self.source)) as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute('DELETE FROM moments')
        self.assertEqual(self.con.execute('SELECT count(*) FROM cue_candidates').fetchone()[0],1)

    def test_missing_schema_drift_no_cue_and_not_imported_are_gaps(self):
        self.assertEqual(cue.health(self.con,self.source)['status'],'not_imported')
        self.native.execute("UPDATE moments SET cue_status='unavailable'")
        self.native.commit()
        self.assertEqual(cue.import_source(self.con,self.source)['status'],'no_cue')
        self.native.execute('DROP TABLE reads')
        self.native.commit()
        self.assertEqual(cue.import_source(self.con,self.source)['status'],'schema_drift')
        self.assertEqual(cue.import_source(self.con, str(self.source)+'missing')['status'],'missing_database')
        self.assertEqual(cue.report(self.con,check_source=False)['groups'],[])

    def test_version_matching_target_relative_to_library(self):
        self.native.execute('UPDATE candidates SET version=?',('stale',))
        result = self.refresh()
        self.assertEqual(result['groups'],[])
        self.assertEqual(result['gaps'],{'version_unavailable':1})

    def test_cli_and_export_aggregates_only(self):
        self.con.commit()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['--db',self.db_path,'cue','import','--source',str(self.source),'--json']),0)
            self.assertEqual(cli.main(['--db',self.db_path,'cue','report','--source',str(self.source),'--json']),0)
        self.assertIn('opportunities',output.getvalue())
        with patch.object(cue,'source_path',return_value=self.source):
            doc = fleet.export(self.con,machine='test')
        encoded = json.dumps(doc['cue'])
        for secret in ('PRIVATE',self.target,'codex:s','moment_id','native_event_id'):
            self.assertNotIn(secret, encoded)
        self.assertIn('research',encoded)
        self.assertEqual(cue.render(cue.report(self.con,check_source=False)).splitlines()[0],
                         'Cue procedure reads: ok')
        self.assertEqual(fleet._schema_only(fleet.validate(doc))['cue'],doc['cue'])
        doc['cue']['groups'][0]['prompt'] = 'PRIVATE'
        with self.assertRaisesRegex(ValueError,'Cue aggregate'):
            fleet.validate(doc)

    def test_targets_use_each_moments_own_library_roots(self):
        other = Path(self.tmp.name) / 'other/library'
        other.mkdir(parents=True)
        relative = self.raw.replace(self.target.encode(), b'../procedure.md')
        (self.library / 'research.md').write_bytes(relative)
        (other / 'research.md').write_bytes(relative)
        self.native.execute('UPDATE candidates SET version=?',
                            (hashlib.sha256(relative).hexdigest()[:12],))
        self.moment(4,2001,session='other')
        self.native.execute('UPDATE moments SET owners_json=? WHERE id=4',
                            (json.dumps({str(other): 'abc'}),))
        result = self.refresh()
        self.assertEqual(result['gaps'],{})
        target = self.con.execute('SELECT target FROM cue_candidates').fetchone()[0]
        self.assertEqual(target,str((self.library / '../procedure.md').resolve()))

    def test_candidate_delivery_tracks_carrys_own_delivery(self):
        self.native.execute('ALTER TABLE candidates ADD COLUMN delivery TEXT')
        self.native.execute("INSERT INTO deliveries VALUES('original','released','PRIVATE')")
        self.native.execute("INSERT INTO deliveries VALUES('carry','settled','PRIVATE')")
        self.native.execute("UPDATE moments SET delivery='original'")
        self.native.execute("UPDATE candidates SET delivery='carry'")
        self.refresh()
        self.assertEqual(self.con.execute('SELECT delivery_stage FROM cue_candidates').fetchone()[0],'settled')

    def test_entry_ids_cannot_export_paths(self):
        self.candidate(2,1,entry='/private/path')
        self.refresh()
        self.assertEqual(self.con.execute('SELECT count(*) FROM cue_candidates').fetchone()[0],1)

    def test_export_keeps_custom_source_snapshot_without_default_database(self):
        self.refresh()
        missing = Path(self.tmp.name) / 'absent-default.sqlite'
        with patch.object(cue, 'source_path', return_value=missing):
            self.assertEqual(cue.report(self.con)['status'], 'missing_database')
            exported = fleet.export(self.con, machine='test')['cue']
        self.assertEqual(exported['status'], 'ok')
        self.assertEqual(exported['groups'][0]['arms']['served']['read'], 1)

    def test_oversized_json_rate_is_a_validation_error(self):
        self.refresh()
        with patch.object(cue, 'source_path', return_value=self.source):
            exported = fleet.export(self.con, machine='test')
        exported['cue']['groups'][0]['arms']['served']['read_rate'] = 10**400
        # Exercise JSON's real integer decoding before the fleet trust boundary.
        with self.assertRaisesRegex(ValueError, 'Cue aggregate'):
            fleet.validate(json.loads(json.dumps(exported)))

    def test_headline_compares_first_judgments_not_repeated_deliveries(self):
        for id,at in ((4,120),(5,130)):
            self.moment(id,at)
            self.candidate(id,id,'deduped' if id==4 else 'injected')
        for id,at in ((6,100),(7,120),(8,130)):
            self.moment(id,at,session='h',holdout=1)
            self.candidate(id,id,'holdout')
        self.moment(9,110,session='h',holdout=1)
        self.read(9,110,session='h')
        result=self.refresh()
        for arm in ('served','holdout'):
            self.assertEqual(self.arm(result,arm)['opportunities'],1)
            self.assertEqual(self.arm(result,arm)['read'],1)
            self.assertEqual(result['groups'][0]['repeats'][arm]['opportunities'],2)
        self.assertEqual(result['summary']['arms']['holdout']['sessions'],1)

    def test_hidden_first_judgment_cannot_be_promoted_by_later_injection(self):
        self.native.execute("UPDATE candidates SET outcome='hidden' WHERE id=1")
        self.moment(4,120)
        self.candidate(4,4,'injected')
        result=self.refresh()
        self.assertNotIn('served',result['groups'][0]['arms'])
        self.assertEqual(result['groups'][0]['excluded']['hidden']['opportunities'],1)
        self.assertEqual(result['groups'][0]['repeats']['served']['opportunities'],1)

    def test_first_judgment_resets_per_epoch_and_ignores_below_threshold(self):
        self.moment(4,90)
        self.candidate(4,4,'below_threshold')
        self.moment(5,120,epoch=1)
        self.candidate(5,5,'injected')
        self.moment(6,130,epoch=1)
        self.candidate(6,6,'deduped')
        result=self.refresh()
        self.assertEqual(self.arm(result)['opportunities'],2)
        self.assertEqual(result['groups'][0]['repeats']['served']['opportunities'],1)

    def test_user_message_boundary_survives_missing_cue_metadata(self):
        for arm,assignment,outcome in (('served',0,'injected'),('holdout',1,'holdout')):
            self.native.execute('UPDATE moments SET holdout=? WHERE id=1',(assignment,))
            self.native.execute('UPDATE candidates SET outcome=? WHERE id=1',(outcome,))
            for epoch,holdout in ((None,0),(0,None),(None,None),('unknown',None)):
                with self.subTest(arm=arm,epoch=epoch,holdout=holdout):
                    self.native.execute('DELETE FROM moments WHERE id=4')
                    self.moment(4,105,epoch=epoch,holdout=holdout,kind='user_message')
                    result=self.refresh()
                    self.assertEqual(self.arm(result,arm)['read'],0)
                    self.assertEqual(self.arm(result,arm)['boundary_without_cue_metadata'],1)

    def test_summary_sessions_are_distinct_across_entries(self):
        # Same session reads two different entry targets; the headline has one session.
        other=self.raw.replace(b'id: research',b'id: other')
        (self.library/'other.md').write_bytes(other)
        self.candidate(4,1,entry='other',version=hashlib.sha256(other).hexdigest()[:12])
        result=self.refresh()
        self.assertEqual(result['summary']['arms']['served']['opportunities'],2)
        self.assertEqual(result['summary']['arms']['served']['sessions'],1)
        self.assertEqual(result['sessions_by_arm']['served'],1)

    def test_cluster_interval_uses_sessions_and_requires_two(self):
        self.assertIsNone(self.arm(self.refresh())['interval95'])
        self.moment(4,100,session='no-read')
        self.candidate(4,4)
        result=self.refresh()
        self.assertEqual(self.arm(result)['sessions'],2)
        # Resampling two one-opportunity sessions can yield all failure or all success.
        self.assertEqual(self.arm(result)['interval95'],[0.0,1.0])
        self.assertIn('session_cluster',result['interval_method'])

    def test_first_unavailable_target_is_not_replaced_by_later_delivery(self):
        self.native.execute("UPDATE candidates SET version='unavailable' WHERE id=1")
        self.moment(4,120)
        self.candidate(4,4)
        result=self.refresh()
        self.assertEqual(result['groups'][0]['arms'],{})
        self.assertEqual(result['groups'][0]['repeats']['served']['opportunities'],1)
        self.assertEqual(result['gaps'],{'version_unavailable':1})

    def test_first_undelivered_judgment_is_not_replaced_by_later_delivery(self):
        self.native.execute("UPDATE candidates SET outcome='cut_budget' WHERE id=1")
        self.moment(4,120)
        self.candidate(4,4)
        result=self.refresh()
        self.assertEqual(result['groups'][0]['arms'],{})
        self.assertEqual(result['groups'][0]['excluded']['not_served']['opportunities'],1)
        self.assertEqual(result['groups'][0]['repeats']['served']['opportunities'],1)

    def test_later_missing_metadata_boundary_does_not_count_when_not_needed(self):
        self.moment(4,105,kind='user_message')
        self.moment(5,106,epoch=None,holdout=None,kind='user_message')
        arm=self.arm(self.refresh())
        self.assertEqual(arm['read'],0)
        self.assertEqual(arm['boundary_without_cue_metadata'],0)

    def test_holdout_gets_a_new_first_judgment_after_compaction(self):
        self.native.execute("UPDATE moments SET holdout=1 WHERE id=1")
        self.native.execute("UPDATE candidates SET outcome='holdout' WHERE id=1")
        self.moment(4,120,epoch=1,holdout=1,kind='compaction')
        self.candidate(4,4,'holdout')
        self.moment(5,130,epoch=1,holdout=1)
        self.candidate(5,5,'holdout')
        result=self.refresh()
        self.assertEqual(self.arm(result,'holdout')['opportunities'],2)
        self.assertEqual(result['groups'][0]['repeats']['holdout']['opportunities'],1)

    def test_assignment_session_count_includes_excluded_first_deliveries(self):
        self.moment(4,100,session='hidden')
        self.candidate(4,4,'hidden')
        result=self.refresh()
        self.assertEqual(result['sessions_by_arm']['served'],2)
        self.assertEqual(self.arm(result)['sessions'],1)
        self.assertEqual(result['summary']['excluded']['hidden']['sessions'],1)
