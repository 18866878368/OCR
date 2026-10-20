import json
from pathlib import Path
import tempfile
import unittest
from medocr.store import Store
from medocr.rules import batch_report
from medocr.report import export_report

CFG = {'transfer_time_basis':'record_time','icu_departments':['ICU']}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.bid = self.store.batch('test','2025-04-01','2025-04-30')
        with self.store.conn:
            self.store.conn.execute("INSERT INTO cases(id) VALUES('c')")
            self.store.conn.execute("INSERT INTO documents VALUES('d','c','synthetic.pdf','hash',1)")
            self.store.conn.execute("INSERT INTO batch_cases VALUES(?,'c')",(self.bid,))
            self.store.conn.execute("INSERT INTO pages(document_id,page) VALUES('d',1)")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_rerun_does_not_overwrite_review(self):
        event = {'case_id':'c','kind':'admission','event_time':'2025-04-12T14:50','document_id':'d','page':1,'quote':'synthetic evidence','origin':'regex'}
        eid = self.store.save_event(event)
        self.store.review_event(eid,{'verified':1,'event_time':'2025-04-12T15:00'},'tester')
        self.store.save_event(event)
        self.assertEqual(self.store.events('c')[0]['event_time'],'2025-04-12T15:00')
        self.assertEqual(len(self.store.all('SELECT * FROM audit')),1)

    def test_manual_review_overrides_and_export(self):
        self.store.save_review(self.bid,'c','core_01','admission',1,0,'人工完整核查','原PDF第1、5、8-10页及完整核查说明','tester')
        report = batch_report(self.store,self.bid,CFG)
        row = next(x for x in report['summary'] if x['metric_id']=='core_01')
        self.assertEqual(row['value'],0)
        self.assertEqual(len(report['summary']),51)
        config = {**CFG,'report_dir':str(Path(self.tmp.name)/'reports')}
        folder = export_report(self.store,self.bid,config)
        self.assertTrue((folder/'summary.csv').exists())
        self.assertEqual(json.loads((folder/'report.json').read_text(encoding='utf-8'))['case_count'],1)

    def test_partial_event_enumeration_blocks_result(self):
        self.store.save_review(self.bid,'c','core_05','consult1',1,1,'及时到位','第5页，申请和到位时间','tester')
        result = next(x for x in batch_report(self.store,self.bid,CFG)['summary'] if x['metric_id']=='core_05')
        self.assertIsNone(result['value'])
        self.store.set_coverage(self.bid,'c','core_05',True,'全部会诊对象已核查','tester')
        result = next(x for x in batch_report(self.store,self.bid,CFG)['summary'] if x['metric_id']=='core_05')
        self.assertEqual(result['value'],1)

    def test_review_audit_and_supplement_source(self):
        with self.assertRaises(ValueError):
            self.store.save_review(self.bid,'c','core_01','admission',1,0,'','','tester')
        self.store.save_measure(self.bid,'core_20','numerator',3,'组织讨论台账','tester')
        self.store.save_measure(self.bid,'core_20','denominator',2,'纠纷死亡病例台账','tester')
        result = next(x for x in batch_report(self.store,self.bid,CFG)['summary'] if x['metric_id']=='core_20')
        self.assertEqual(result['value'],1.5)

    def test_episode_merge_and_batch_dedup(self):
        with self.store.conn:
            self.store.conn.execute("INSERT INTO cases(id) VALUES('c2')")
            self.store.conn.execute("INSERT INTO documents VALUES('d2','c2','synthetic2.pdf','hash2',1)")
            self.store.conn.execute("INSERT INTO batch_cases VALUES(?,'c2')",(self.bid,))
        self.store.merge_cases('c2','c','tester')
        self.assertEqual(len(self.store.cases(self.bid)),1)
        self.assertEqual(len(self.store.all("SELECT * FROM documents WHERE case_id='c'")),2)


if __name__=='__main__':
    unittest.main()
