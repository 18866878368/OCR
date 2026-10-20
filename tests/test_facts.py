import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from medocr.facts import validate_facts, chunks_for, extract_facts_case
from medocr.store import Store
from medocr.import_facts import import_hospital_facts
from medocr.references import load_reference

UNIT = {'document_id':'d','page':1,'text':'手术开始时间：2025-04-03 09:00；手术结束时间：2025-04-03 11:00；四级手术；术式A；术者医生甲','category':'surgery'}


def result():
    return {'events':[{'kind':'surgery','event_time':'2025-04-03T09:00','record_time':None,
        'attributes':{'grade':4,'procedure_name':'术式A','surgeon':'医生甲','end_time':'2025-04-03T11:00'},
        'evidence':[{'document_id':'d','page':1,'quote':UNIT['text']}]}]}


def mock_model(config,system,content):
    return result() if '"surgery":' in system else {'events':[]}


class FactsTests(unittest.TestCase):
    def test_multiple_evidence_pages_and_typed_values(self):
        rows = validate_facts(result(),[UNIT],'c')
        self.assertFalse(rows[0]['verified'])
        self.assertEqual(rows[0]['attributes']['grade'],4)
        self.assertEqual(rows[0]['attributes']['end_time'],'2025-04-03T11:00:00')

    def test_fabricated_quote_time_grade_and_person_are_rejected(self):
        for key,value in [('surgeon','不存在医生'),('grade',3),('end_time','2025-04-03T12:00')]:
            data = result(); data['events'][0]['attributes'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                validate_facts(data,[UNIT],'c')
        data = result(); data['events'][0]['evidence'][0]['page']=2
        with self.assertRaises(ValueError):
            validate_facts(data,[UNIT],'c')

    def test_split_same_page_evidence_does_not_overwrite_previous_fragment(self):
        units = [{**UNIT,'text':UNIT['text'][:50]},{**UNIT,'text':UNIT['text'][50:]}]
        data = result()
        data['events'][0]['evidence']=[{'document_id':'d','page':1,'quote':u['text']} for u in units]
        validate_facts(data,units,'c')

    def test_date_and_clock_columns_can_be_composed_without_inventing_midnight(self):
        unit = {**UNIT,'text':'手术日期：2025年4月3日；开始时间：09:00；结束时间：11:00；四级手术；术式A；术者医生甲'}
        data=result(); data['events'][0]['evidence'][0]['quote']=unit['text']
        rows=validate_facts(data,[unit],'c')
        self.assertEqual(rows[0]['event_time'],'2025-04-03T09:00:00')
        unit={**UNIT,'text':'手术日期：2025年4月3日；四级手术；术式A；术者医生甲'}
        data=result(); data['events'][0]['event_time']=None
        data['events'][0]['attributes'].pop('end_time')
        data['events'][0]['attributes']['event_date']='2025-04-03'
        data['events'][0]['evidence'][0]['quote']=unit['text']
        rows=validate_facts(data,[unit],'c')
        self.assertIsNone(rows[0]['event_time'])
        self.assertEqual(rows[0]['attributes']['event_date'],'2025-04-03')

    def test_chunks_keep_adjacent_pages_and_cover_long_page_without_truncation(self):
        page = {**UNIT,'stage':'full','text':'a'*2600}
        chunks = list(chunks_for([page],1000))
        self.assertEqual(sum(len(u['text']) for chunk in chunks for u in chunk),2600)
        self.assertTrue(all(sum(len(u['text']) for u in chunk)<=1000 for chunk in chunks))

    def make_store(self, temp):
        store = Store(temp); bid = store.batch('test')
        with store.conn:
            store.conn.execute("INSERT INTO cases(id) VALUES('c')")
            store.conn.execute("INSERT INTO documents VALUES('d','c','synthetic.pdf','hash',1)")
            store.conn.execute("INSERT INTO batch_cases VALUES(?,'c')",(bid,))
            store.conn.execute("INSERT INTO pages(document_id,page,text,stage,category) VALUES('d',1,?,'full','surgery')",(UNIT['text'],))
        return store,bid

    def test_extraction_cache_and_source_change_preserve_manual_review(self):
        cfg = {'llm':{'enabled':True,'model':'synthetic','base_url':'http://127.0.0.1:1234/v1','max_chars_per_page':12000}}
        with tempfile.TemporaryDirectory() as temp:
            store,bid = self.make_store(temp)
            with patch('medocr.facts.request_json',side_effect=mock_model):
                extracted = extract_facts_case(store,'c',cfg,True)
                self.assertEqual(extracted['failed_pages'],0)
                semantic = [e for e in store.events('c') if e['origin']=='local_llm'][0]
                store.review_event(semantic['id'],{'verified':True},'doctor')
                again = extract_facts_case(store,'c',cfg,True)
                self.assertEqual(again['cached_chunks'],1)
                with store.conn:
                    store.conn.execute("UPDATE pages SET text=text||' 文书更新' WHERE document_id='d'")
                extract_facts_case(store,'c',cfg,True)
                old = next(e for e in store.events('c') if e['id']==semantic['id'])
                self.assertTrue(old['verified'])
                self.assertTrue(old['attributes']['_stale'])
            store.close()

    def test_model_failure_is_persisted_and_retryable(self):
        cfg = {'llm':{'enabled':True,'model':'synthetic','base_url':'http://127.0.0.1:1234/v1','max_chars_per_page':12000}}
        with tempfile.TemporaryDirectory() as temp:
            store,_ = self.make_store(temp)
            with patch('medocr.facts.request_json',side_effect=ValueError('bad JSON')):
                first = extract_facts_case(store,'c',cfg,True)
            self.assertEqual(first['failed_pages'],1)
            self.assertTrue(store.one("SELECT status FROM extraction_runs WHERE chunk_id NOT LIKE 'rules:%' AND status='error'"))
            with patch('medocr.facts.request_json',side_effect=mock_model):
                retry = extract_facts_case(store,'c',cfg,True)
            self.assertEqual(retry['failed_pages'],0)
            store.close()

    def test_hospital_import_has_immutable_source_and_requires_explicit_review(self):
        with tempfile.TemporaryDirectory() as temp:
            store,bid = self.make_store(temp)
            path = Path(temp)/'source.json'
            path.write_text(json.dumps([{'case_id':'c','kind':'upload','attributes':{'required':True,'complete':False}}]),encoding='utf-8')
            self.assertEqual(import_hospital_facts(store,bid,path,'doctor'),1)
            event = store.events('c')[0]
            self.assertFalse(event['verified'])
            self.assertTrue(Path(event['attributes']['_external_source']).is_file())
            import_hospital_facts(store,bid,path,'doctor',True)
            self.assertTrue(store.events('c')[0]['verified'])
            store.close()

    def test_reference_template_does_not_claim_empty_lists_are_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'reference.json'
            path.write_text(json.dumps({'registered_types':[],'special_antibiotics':[],'complete_sections':[]}),encoding='utf-8')
            data = load_reference(path)
            self.assertNotIn('registered_types',data)
            self.assertNotIn('special_antibiotics',data)

    def test_validation_retry_retains_strict_evidence_and_unverified_state(self):
        from medocr.facts import request_valid_facts
        invalid=result(); invalid['events'][0]['attributes']['surgeon']='不存在医生'
        with patch('medocr.facts.request_json',side_effect=[invalid,result()]) as model:
            rows=request_valid_facts({'llm':{}},['surgery'],{'pages':[UNIT]},[UNIT],'c')
        self.assertEqual(len(rows),1)
        self.assertFalse(rows[0]['verified'])
        self.assertIn('validation_feedback',model.call_args.args[2])
        with patch('medocr.facts.request_json',return_value=invalid) as model:
            rows=request_valid_facts({'llm':{}},['surgery'],{'pages':[UNIT]},[UNIT],'c')
            self.assertIsNone(rows[0]['attributes']['surgeon'])
            self.assertTrue(rows[0]['attributes']['_validation_issues'])
        self.assertEqual(model.call_count,3)

    def test_numbered_line_evidence_is_reconstructed_from_original_source(self):
        from medocr.facts import request_valid_facts
        data=result(); data['events'][0]['evidence']=[{'document_id':'d','page':1,'start_line':1,'end_line':1}]
        with patch('medocr.facts.request_json',return_value=data):
            rows=request_valid_facts({'llm':{'protocol':'ollama'}},['surgery'],{'pages':[UNIT]},[UNIT],'c')
        self.assertEqual(rows[0]['quote'],UNIT['text'])
        data=result(); data['events'][0]['evidence']=[{'document_id':'d','page':1,'start_line':2,'end_line':2}]
        with patch('medocr.facts.request_json',return_value=data):
            with self.assertRaisesRegex(ValueError,'证据'):
                request_valid_facts({'llm':{'protocol':'ollama'}},['surgery'],{'pages':[UNIT]},[UNIT],'c')
