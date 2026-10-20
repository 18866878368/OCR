import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from medocr.rule_extract import rule_facts, semantic_kinds, rule_links, eligible_event, rule_first_events
from medocr.facts import extract_facts_case
from medocr.store import Store
from medocr.rules import batch_report


def page(text, category='unknown', number=1):
    return {'case_id':'c','document_id':'d','page':number,'text':text,'category':category,'stage':'full'}


def store_fixture(folder, pages):
    s = Store(folder); bid = s.batch('合成规则测试')
    with s.conn:
        s.conn.execute("INSERT INTO cases(id) VALUES('c')")
        s.conn.execute("INSERT INTO documents VALUES('d','c','synthetic.pdf','hash',?)",(len(pages),))
        s.conn.execute("INSERT INTO batch_cases VALUES(?,'c')",(bid,))
        for p in pages:
            s.conn.execute("INSERT INTO pages(document_id,page,text,stage,category) VALUES('d',?,?,'full',?)",(p['page'],p['text'],p['category']))
    return s,bid


class RuleFirstTests(unittest.TestCase):
    def test_explicit_transfer_uses_record_time_and_icu_mapping(self):
        rows=rule_facts(page('转科记录\n转科时间：2025-04-02 09:00\n记录时间：2025-04-02 10:00\n转出科室：外科\n转入科室：ICU'))
        row=next(e for e in rows if e['kind']=='transfer')
        self.assertEqual(row['event_time'],'2025-04-02T09:00:00')
        self.assertEqual(row['record_time'],'2025-04-02T10:00:00')
        self.assertTrue(row['attributes']['occurred'])
        self.assertTrue(row['attributes']['to_icu'])
        self.assertFalse(row['verified'])

    def test_plans_history_and_absent_keywords_are_not_actual_events(self):
        for text in ['拟转科至外科','未转科','既往史：\n入院时间：2024-01-01 09:00','计划手术开始时间：2025-04-02 09:00']:
            self.assertEqual(rule_facts(page(text)),[],text)

    def test_dates_without_clocks_and_conflicting_times_remain_unknown(self):
        row=rule_facts(page('入院日期：2025年4月1日'))[0]
        self.assertIsNone(row['event_time'])

    def test_adjacent_date_clock_cells_require_a_label_and_no_intervening_field(self):
        row=rule_facts(page('入院时间\n2025-04-01\n08:30'))[0]
        self.assertEqual(row['event_time'],'2025-04-01T08:30:00')
        row=rule_facts(page('入院日期：2025-04-01\n打印时间：08:30'))[0]
        self.assertIsNone(row['event_time'])
        self.assertEqual(row['attributes']['event_date'],'2025-04-01')
        row=rule_facts(page('手术开始时间：2025-04-01 09:00 / 2025-04-01 10:00'))[0]
        self.assertIsNone(row['event_time'])

    def test_order_table_selects_opening_column_not_execution_or_stop(self):
        text='医嘱编号|开立时间|执行时间|停止时间|医嘱内容\nA1|2025-04-01 09:00|2025-04-01 12:00|2025-04-02 08:00|吸氧治疗\nA2|2025-04-01 10:00|2025-04-01 13:00||CT检查'
        rows=rule_facts(page(text,'orders'))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['event_time'],'2025-04-01T09:00:00')
        self.assertEqual(rows[0]['attributes']['stop_time'],'2025-04-02T08:00:00')
        self.assertEqual(rows[1]['attributes']['order_category'],'examination')
        self.assertEqual(len(rows[0]['attributes']['_evidence']),2)
        self.assertNotEqual(rows[0]['id'],rows[1]['id'])

    def test_execution_only_table_does_not_invent_opening_time(self):
        row=rule_facts(page('执行时间|医嘱内容\n2025-04-01 10:00|CT检查','orders'))[0]
        self.assertIsNone(row['event_time'])
        self.assertIn('event_time',row['attributes']['_needs_semantic'])

    def test_rules_dont_claim_clinical_norms_from_document_title(self):
        row=rule_facts(page('输血后评估记录\n评估时间：2025-04-01 12:00\n评估内容：生命体征稳定'))[0]
        self.assertNotIn('norm_compliant',row['attributes'])
        self.assertIn('norm_compliant',row['attributes']['_needs_semantic'])

    def test_links_require_shared_relation_specific_id_not_time(self):
        surgery=page('手术开始时间：2025-04-01 09:00\n手术编号：S1',number=1)
        discussion=page('术前讨论记录\n讨论时间：2025-04-01 09:00',number=2)
        with tempfile.TemporaryDirectory() as temp:
            with Store(temp) as s:
                with s.conn:
                    s.conn.execute("INSERT INTO cases(id) VALUES('c')")
                    s.conn.execute("INSERT INTO documents VALUES('d','c','synthetic','hash',2)")
                for p in [surgery,discussion]:
                    for e in rule_facts(p): s.save_event(e)
                self.assertEqual(rule_links(s,'c'),0)
                for e in rule_facts({**discussion,'text':discussion['text']+'\n手术编号：S1'}): s.save_event(e)
                self.assertEqual(rule_links(s,'c'),1)

    def test_rules_only_never_calls_model_and_cache_preserves_review(self):
        p=page('入院时间：2025-04-01 08:00','admission')
        with tempfile.TemporaryDirectory() as temp:
            s,bid=store_fixture(temp,[p])
            with patch('medocr.facts.request_json',side_effect=AssertionError('must not call')):
                result=extract_facts_case(s,'c',{'llm':{'enabled':False}},False)
                row=s.events('c')[0]; s.review_event(row['id'],{'verified':True},'医生')
                again=extract_facts_case(s,'c',{'llm':{'enabled':False}},False)
                extract_facts_case(s,'c',{'llm':{'enabled':False}},False,force=True)
            self.assertEqual(result['failed_pages'],0)
            self.assertEqual(again['cached_rule_pages'],1)
            self.assertTrue(s.events('c')[0]['verified'])
            self.assertFalse(s.events('c')[0]['attributes'].get('_stale',False))
            s.close()

    def test_assistance_skips_complete_admission_and_unrelated_page(self):
        p=page('入院时间：2025-04-01 08:00','admission')
        self.assertEqual(semantic_kinds(p,rule_facts(p)),[])
        cfg={'llm':{'enabled':True,'model':'synthetic','base_url':'http://127.0.0.1:1234/v1','max_chars_per_page':4000}}
        with tempfile.TemporaryDirectory() as temp:
            s,_=store_fixture(temp,[p,page('只是文档页眉','unknown',2)])
            with patch('medocr.facts.request_json',side_effect=AssertionError('no unresolved kinds')):
                result=extract_facts_case(s,'c',cfg,True)
            self.assertEqual(result['semantic_pages'],0)
            self.assertEqual(result['failed_pages'],0)
            s.close()

    def test_full_processing_is_not_proof_that_transfer_did_not_happen(self):
        cfg={'calculation_engine':'core35','indicator_set':'core','transfer_time_basis':'record_time','icu_departments':['ICU'],'llm':{'enabled':False}}
        with tempfile.TemporaryDirectory() as temp:
            s,bid=store_fixture(temp,[page('入院时间：2025-04-01 08:00','admission')])
            extract_facts_case(s,'c',cfg,False)
            r=next(x for x in batch_report(s,bid,cfg)['summary'] if x['metric_id']=='core_01')
            self.assertIsNone(r['draft_value'])
            self.assertIsNone(r['value'])
            s.close()

    def test_old_model_candidates_excluded_until_human_review(self):
        e={'origin':'local_llm','verified':False,'attributes':{}}
        self.assertFalse(eligible_event(e,'rules'))
        self.assertTrue(eligible_event(e,'assisted'))
        self.assertTrue(eligible_event({**e,'verified':True},'rules'))

    def test_assistance_fills_unknowns_without_replacing_rule_time_or_false(self):
        rule=rule_facts(page('手术开始时间：2025-04-01 09:00\n手术编号：S1\n是否非计划手术：否'))[0]
        helper={**rule,'id':'helper','origin':'local_llm','event_time':'2025-04-01T10:00:00',
                'attributes':{'surgery_id':'S1','unplanned':True,'surgeon':'合成医生'}}
        rows=rule_first_events([rule,helper])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['event_time'],rule['event_time'])
        self.assertFalse(rows[0]['attributes']['unplanned'])
        self.assertEqual(rows[0]['attributes']['surgeon'],'合成医生')
        self.assertIn('event_time',rows[0]['attributes']['_assistance_conflicts'])
        self.assertNotIn('surgeon',rule['attributes'])
        self.assertIn('helper',rows[0]['_ids'])

    def test_ambiguous_assistance_and_human_reviews_are_not_auto_composed(self):
        rule=rule_facts(page('手术开始时间：2025-04-01 09:00\n手术编号：S1'))[0]
        helper={**rule,'id':'helper','origin':'local_llm'}
        duplicate={**rule,'id':'other_rule'}
        self.assertEqual(len(rule_first_events([rule,duplicate,helper])),3)
        self.assertEqual(len(rule_first_events([rule,{**helper,'verified':True}])),2)

    def test_optional_model_outage_still_completes_rule_processing(self):
        from medocr.batch import enqueue,run_batch
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); s=Store(root/'data'); bid=s.batch('无模型测试')
            pdf=root/'synthetic.pdf'; w=PdfWriter(); w.add_blank_page(595,842)
            with pdf.open('wb') as f: w.write(f)
            enqueue(s,bid,[pdf])
            cfg={'report_dir':str(root/'reports'),'calculation_engine':'core35','indicator_set':'core','transfer_time_basis':'record_time','ocr':{},'llm':{'enabled':True}}
            with patch('medocr.batch.preflight',side_effect=URLError('offline')),patch('medocr.batch.process_pages',return_value={'failed':0}),patch('medocr.batch.extract_case',return_value={'failed_pages':0}) as extract:
                r=run_batch(s,bid,cfg,True)
            self.assertFalse(extract.call_args.args[3])
            self.assertEqual(r['failed'],0)
            self.assertEqual(s.all('SELECT stage FROM jobs')[0]['stage'],'partial')
            self.assertTrue((root/'reports'/bid/'summary.csv').exists())
            s.close()
