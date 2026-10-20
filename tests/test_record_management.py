import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from medocr.candidate_scope import current_scope
from medocr.record_management import delete_records, deletion_plan, REPORT_FILES
from medocr.store import Store
from test_rule_extract import page, store_fixture
from test_candidate_scope import CFG, helper


class RecordManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.s,self.bid=store_fixture(self.root/'data',[page('手术开始时间：2025-04-01 09:00','surgery')])
        self.cfg={**CFG,'report_dir':str(self.root/'reports')}

    def tearDown(self):
        self.s.close(); self.temp.cleanup()

    def old(self,id='old_candidate'):
        return self.s.save_event({'id':id,'case_id':'c','document_id':'d','page':1,'kind':'surgery',
            'quote':'合成历史证据','origin':'local_llm','attributes':{'_chunk':'old','_signature':'old'}})

    def test_confirmation_required_and_stale_preview_rejected(self):
        self.old()
        with self.assertRaisesRegex(ValueError,'确认'):
            delete_records(self.s,self.bid,self.cfg,'history')
        plan=deletion_plan(self.s,self.bid,self.cfg,'history')
        self.old('another_history')
        with self.assertRaisesRegex(ValueError,'变化'):
            delete_records(self.s,self.bid,self.cfg,'history',True,False,plan['signature'])
        self.assertEqual(len(self.s.events('c')),2)

    def test_history_removes_only_old_automatic_candidates_and_refreshes_export(self):
        self.old(); helper(self.s,self.cfg)
        edited=self.old('edited'); self.s.review_event(edited,{'verified':False},'doctor')
        verified=self.old('verified'); self.s.review_event(verified,{'verified':True},'doctor')
        self.s.save_event({'id':'manual','case_id':'c','kind':'order','document_id':'d','page':1,
            'origin':'manual','quote':'合成人工事实'})
        result=delete_records(self.s,self.bid,self.cfg,'history',True,False)
        self.assertEqual(result['deleted_events'],1)
        self.assertEqual({e['id'] for e in self.s.events('c')},{'current','edited','verified','manual'})
        self.assertEqual(self.s.one('SELECT COUNT(*) n FROM pages')['n'],1)
        self.assertTrue((Path(self.cfg['report_dir'])/self.bid/'facts.json').exists())
        self.assertIsNone(result['report_error'])

    def test_human_links_and_indicator_reviews_protect_referenced_history(self):
        self.old()
        self.s.save_event({'id':'manual_link','case_id':'c','kind':'link','document_id':'d','page':1,
            'origin':'manual','quote':'合成关联','verified':True,
            'attributes':{'source_event_id':'old_candidate','target_event_id':'old_candidate'}})
        reviewed=self.old('indicator_reviewed_event')
        self.s.save_review(self.bid,'c','core_15',reviewed,1,None,'合成复核','第1页','doctor')
        plan=deletion_plan(self.s,self.bid,self.cfg,'history')
        self.assertEqual(plan['event_ids'],[])
        self.assertEqual(plan['protected_history'],2)

    def test_shared_case_is_preserved_after_deleting_one_batch(self):
        self.old(); other=self.s.batch('other')
        with self.s.conn:
            self.s.conn.execute("INSERT INTO batch_cases VALUES(?,'c')",(other,))
            self.s.conn.execute('INSERT INTO batch_processing VALUES(?,?,?)',(self.bid,'assisted','synthetic'))
        self.s.save_review(other,'c','core_01','admission',1,None,'人工复核','第1页','doctor')
        result=delete_records(self.s,self.bid,self.cfg,'batch',True,False)
        self.assertEqual(result['deleted_cases'],0)
        self.assertEqual(result['preserved_shared_cases'],1)
        self.assertEqual(len(self.s.events('c')),1)
        self.assertEqual(len(self.s.cases(other)),1)
        self.assertTrue(self.s.one('SELECT * FROM reviews WHERE batch_id=?',(other,)))
        self.assertFalse(self.s.one('SELECT * FROM batches WHERE id=?',(self.bid,)))
        self.assertEqual(self.s.all('PRAGMA foreign_key_check'),[])

    def test_exclusive_batch_deletes_all_database_dependencies_and_generated_reports(self):
        self.old()
        self.s.extraction_status('c','old','old','complete',event_ids=['old_candidate'])
        self.s.extraction_plan('c',current_scope(self.s,'c',self.cfg))
        self.s.save_review(self.bid,'c','core_01','admission',1,None,'合成复核','第1页','doctor')
        self.s.set_coverage(self.bid,'c','core_01',True,'完整核查','doctor')
        self.s.save_measure(self.bid,'core_20','numerator',1,'合成台账','doctor')
        self.s.approve_rule(self.bid,'core_01','synthetic','doctor','合成确认')
        with self.s.conn:
            self.s.conn.execute('INSERT INTO jobs(batch_id,source_id,path,case_id,stage,updated) VALUES(?,?,?,?,?,?)',
                (self.bid,'source','synthetic.pdf','c','complete','synthetic'))
        folder=Path(self.cfg['report_dir'])/self.bid; folder.mkdir(parents=True)
        for name in REPORT_FILES: (folder/name).write_text('synthetic',encoding='utf-8')
        (folder/'user-note.txt').write_text('keep',encoding='utf-8')
        original=self.root/'original.pdf'; original.write_bytes(b'synthetic')
        result=delete_records(self.s,self.bid,self.cfg,'batch',True,False)
        self.assertEqual(result['deleted_cases'],1)
        for table in ('batches','batch_cases','cases','documents','pages','events','reviews','coverage','measures',
                      'rule_approvals','jobs','extraction_runs','extraction_outputs','extraction_plans'):
            self.assertEqual(self.s.one(f'SELECT COUNT(*) n FROM {table}')['n'],0,table)
        self.assertEqual(self.s.all('PRAGMA foreign_key_check'),[])
        self.assertTrue(original.exists())
        self.assertTrue((folder/'user-note.txt').exists())
        self.assertFalse(any((folder/name).exists() for name in REPORT_FILES))
        self.assertTrue(self.s.one("SELECT * FROM audit WHERE kind='delete_records'"))

    def test_backup_contains_pre_deletion_database_and_previous_exports(self):
        self.old()
        folder=Path(self.cfg['report_dir'])/self.bid; folder.mkdir(parents=True)
        (folder/'facts.json').write_text('synthetic previous result',encoding='utf-8')
        result=delete_records(self.s,self.bid,self.cfg,'batch',True,True)
        backup=Path(result['backup_dir'])
        with closing(sqlite3.connect(backup/'medocr.sqlite3')) as s:
            self.assertEqual(s.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
            self.assertEqual(s.execute('SELECT COUNT(*) FROM batches').fetchone()[0],1)
        self.assertEqual((backup/'report/facts.json').read_text(encoding='utf-8'),'synthetic previous result')

    def test_running_shared_worker_blocks_deletion_before_backup(self):
        other=self.s.batch('other')
        with self.s.conn:
            self.s.conn.execute("INSERT INTO batch_cases VALUES(?,'c')",(other,))
            self.s.conn.execute('INSERT INTO batch_workers VALUES(?,?,?,?,?,?)',(other,123,'running',0,'','synthetic'))
        with patch('medocr.record_management.process_alive',return_value=True):
            self.assertTrue(deletion_plan(self.s,self.bid,self.cfg,'batch')['blocked'])
            with self.assertRaisesRegex(ValueError,'正在处理'):
                delete_records(self.s,self.bid,self.cfg,'batch',True,True)
        self.assertTrue(self.s.one('SELECT id FROM batches WHERE id=?',(self.bid,)))
        self.assertFalse((self.s.root/'deletion_backups').exists())

    def test_output_membership_is_pruned_without_reactivating_old_candidates(self):
        self.old()
        self.s.extraction_status('c','old','old','complete',event_ids=['old_candidate'])
        delete_records(self.s,self.bid,self.cfg,'history',True,False)
        self.assertEqual(json.loads(self.s.one('SELECT event_ids FROM extraction_outputs')['event_ids']),[])

    def test_invalid_report_target_does_not_change_database(self):
        self.old()
        outside=self.root/'outside'; outside.mkdir()
        report_root=Path(self.cfg['report_dir']); report_root.mkdir()
        # Exercise the guard without requiring Windows symlink privileges.
        with patch.object(Path,'is_symlink',autospec=True,side_effect=lambda p:p==report_root/self.bid):
            with self.assertRaisesRegex(ValueError,'删除范围'):
                delete_records(self.s,self.bid,self.cfg,'batch',True,False)
        self.assertTrue(self.s.one('SELECT id FROM batches WHERE id=?',(self.bid,)))

