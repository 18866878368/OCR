from pathlib import Path
import tempfile
import unittest

from medocr.store import Store
from medocr.relations import confirm_relation
from medocr.identity import possible_duplicate_admissions
from medocr.rules import batch_report


class RelationsIdentityTests(unittest.TestCase):
    def setup_store(self, root):
        store=Store(root); bid=store.batch('synthetic','2025-04-01','2025-04-30')
        for cid in ('a','b'):
            with store.conn:
                store.conn.execute('INSERT INTO cases(id) VALUES(?)',(cid,))
                store.conn.execute('INSERT INTO documents VALUES(?,?,?,?,1)',('d'+cid,cid,'synthetic.pdf','hash'+cid))
                store.conn.execute('INSERT INTO batch_cases VALUES(?,?)',(bid,cid))
        return store,bid

    def event(self,store,cid,kind,attrs=None,time=None):
        return store.save_event({'case_id':cid,'kind':kind,'attributes':attrs or {},'event_time':time,
            'document_id':'d'+cid,'page':1,'quote':'关联手术及术前讨论的合成证据','origin':'synthetic'})

    def test_manual_relation_checks_endpoint_type_evidence_and_audits(self):
        with tempfile.TemporaryDirectory() as temp:
            store,bid=self.setup_store(Path(temp)/'data')
            surgery=self.event(store,'a','surgery'); discussion=self.event(store,'a','discussion')
            with self.assertRaises(ValueError):
                confirm_relation(store,'a','discussion_for_surgery',surgery,discussion,'伪造证据','doctor','checked')
            with self.assertRaises(ValueError):
                confirm_relation(store,'a','discussion_for_surgery',discussion,surgery,'合成证据','doctor','checked')
            eid=confirm_relation(store,'a','discussion_for_surgery',surgery,discussion,'合成证据','doctor','checked')
            self.assertTrue(store.one('SELECT verified FROM events WHERE id=?',(eid,))['verified'])
            self.assertTrue(store.one('SELECT id FROM audit WHERE object_id=?',(eid,)))
            store.close()

    def test_duplicate_admission_blocks_reviewed_report_until_manual_merge(self):
        with tempfile.TemporaryDirectory() as temp:
            store,bid=self.setup_store(temp)
            for cid in ('a','b'):
                self.event(store,cid,'admission',{'patient_id':'syntheticID'},'2025-04-01T08:00:00')
            self.assertEqual(len(possible_duplicate_admissions(store,bid)),1)
            cfg={'indicator_set':'core','calculation_engine':'core35','transfer_time_basis':'record_time','icu_departments':['ICU']}
            report=batch_report(store,bid,cfg)
            self.assertEqual(report['summary'][0]['status'],'住院人次待核对')
            self.assertIsNone(report['summary'][0]['value'])
            store.merge_cases('b','a','doctor')
            self.assertFalse(possible_duplicate_admissions(store,bid))
            self.assertEqual(len(store.cases(bid)),1)
            store.close()
