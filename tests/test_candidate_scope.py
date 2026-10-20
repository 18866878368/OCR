"""Regression cases for routing/config changes and legacy candidate isolation."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from medocr.candidate_scope import current_scope, scoped_events, link_signature
from medocr.facts import extract_facts_case, link_case
from medocr.report import export_report
from medocr.rules import batch_report
from medocr.store import Store
from test_rule_extract import page, store_fixture

CFG = {'extraction_mode':'assisted','llm':{'enabled':True,'model':'synthetic',
    'base_url':'http://127.0.0.1:1234/v1','max_chars_per_page':4000},
    'calculation_engine':'core35','indicator_set':'core','transfer_time_basis':'record_time'}


def helper(s, cfg, id='current', kind='surgery', attrs=None):
    scope = current_scope(s,'c',cfg)
    key,sig = next(iter(scope['semantic'].items()))
    eid = s.save_event({'id':id,'case_id':'c','document_id':'d','page':1,'quote':'合成证据',
        'kind':kind,'origin':'local_llm','attributes':{'_chunk':key,'_signature':sig,**(attrs or {})}})
    s.extraction_status('c',key,sig,'complete')  # Legacy checkpoint: no output-ID snapshot.
    return eid,key,sig


class CandidateScopeTests(unittest.TestCase):
    def make(self, folder):
        return store_fixture(folder,[page('手术开始时间：2025-04-01 09:00','surgery')])

    def test_legacy_same_block_and_unselected_block_are_isolated_without_deletion(self):
        with tempfile.TemporaryDirectory() as temp, self.make(temp)[0] as s:
            helper(s,CFG)
            s.save_event({'id':'old_block','case_id':'c','document_id':'d','page':1,
                'quote':'旧合成证据','kind':'surgery','origin':'local_llm',
                'attributes':{'_chunk':'previously-routed-block','_signature':'old'}})
            s.save_event({'id':'old_same','case_id':'c','document_id':'d','page':1,
                'quote':'旧配置证据','kind':'surgery','origin':'local_llm',
                'attributes':{'_chunk':next(iter(current_scope(s,'c',CFG)['semantic'])),'_signature':'old'}})
            before = s.all('SELECT * FROM events')
            self.assertEqual({e['id'] for e in scoped_events(s,'c',CFG)}, {'current'})
            all_rows = scoped_events(s,'c',CFG,include_history=True)
            self.assertEqual(sum(e['candidate_scope']=='history' for e in all_rows),2)
            self.assertEqual(s.all('SELECT * FROM events'),before)

    def test_model_chunking_reference_mode_and_ocr_changes_invalidate_automatic_helpers(self):
        with tempfile.TemporaryDirectory() as temp, self.make(temp)[0] as s:
            helper(s,CFG)
            changes = []
            model=deepcopy(CFG); model['llm']['model']='different'; changes.append(model)
            limit=deepcopy(CFG); limit['llm']['max_chars_per_page']=1000; changes.append(limit)
            refs=deepcopy(CFG); refs['hospital_reference']={'ward_round_standard':'changed'}; changes.append(refs)
            changes.append({**CFG,'extraction_mode':'rules'})
            for changed in changes:
                self.assertEqual(scoped_events(s,'c',changed),[])
            with s.conn:
                s.conn.execute("UPDATE pages SET text='普通无关页面',category='progress'")
            self.assertEqual(current_scope(s,'c',CFG)['semantic'],{})
            self.assertEqual(scoped_events(s,'c',CFG),[])

    def test_failed_or_running_checkpoint_and_latest_empty_output_block_candidates(self):
        with tempfile.TemporaryDirectory() as temp, self.make(temp)[0] as s:
            _,key,sig=helper(s,CFG)
            for state in ['running','error']:
                s.extraction_status('c',key,sig,state)
                self.assertEqual(scoped_events(s,'c',CFG),[])
            s.extraction_status('c',key,sig,'complete',event_ids=[])
            self.assertEqual(scoped_events(s,'c',CFG),[])
            self.assertEqual(len(s.events('c')),1)

    def test_explicit_human_review_and_manual_facts_survive_but_stale_needs_reconfirmation(self):
        with tempfile.TemporaryDirectory() as temp, self.make(temp)[0] as s:
            helper(s,CFG)
            s.review_event('current',{'verified':True},'doctor')
            cfg=deepcopy(CFG); cfg['llm']['model']='new'
            self.assertEqual(scoped_events(s,'c',cfg)[0]['candidate_scope'],'reviewed')
            s.save_event({'id':'manual','case_id':'c','document_id':'d','page':1,'quote':'补充合成事实',
                'kind':'order','origin':'manual'})
            self.assertEqual({e['id'] for e in scoped_events(s,'c',cfg)}, {'current','manual'})
            s.review_event('current',{'attributes':{'_stale':True}},'doctor')
            self.assertEqual({e['id'] for e in scoped_events(s,'c',cfg)}, {'manual'})

    def test_model_association_sees_only_current_endpoints_and_rejects_old_links(self):
        with tempfile.TemporaryDirectory() as temp, self.make(temp)[0] as s:
            helper(s,CFG,id='surgery')
            s.save_event({'id':'consent','case_id':'c','document_id':'d','page':1,
                'quote':'合成知情同意证据','kind':'consent','origin':'manual'})
            s.save_event({'id':'old','case_id':'c','document_id':'d','page':1,
                'quote':'历史手术证据','kind':'surgery','origin':'local_llm','attributes':{'_chunk':'old','_signature':'old'}})
            s.save_event({'id':'old_link','case_id':'c','document_id':'d','page':1,
                'quote':'旧关联证据','kind':'link','origin':'local_llm','attributes':{
                    'source_event_id':'consent','target_event_id':'old','relation':'consent_for_surgery','_signature':'old'}})
            with patch('medocr.facts.request_json',return_value={'links':[]}) as model:
                link_case(s,'c',CFG)
            self.assertEqual(model.call_count,1)
            request=model.call_args.args[2]
            self.assertEqual({request['source']['id']} | {e['id'] for e in request['possible_targets']}, {'surgery','consent'})
            self.assertNotIn('old_link',{e['id'] for e in scoped_events(s,'c',CFG)})

    def test_old_link_is_rejected_even_when_both_endpoints_remain_current(self):
        with tempfile.TemporaryDirectory() as temp, self.make(temp)[0] as s:
            helper(s,CFG,id='surgery')
            s.save_event({'id':'consent','case_id':'c','document_id':'d','page':1,
                'quote':'合成证据','kind':'consent','origin':'manual'})
            sig,_=link_signature(scoped_events(s,'c',CFG),CFG)
            s.save_event({'id':'link','case_id':'c','document_id':'d','page':1,
                'quote':'合成证据','kind':'link','origin':'local_llm','attributes':{
                    'source_event_id':'consent','target_event_id':'surgery','relation':'consent_for_surgery','_signature':sig}})
            s.extraction_status('c','links',sig,'complete')
            self.assertIn('link',{e['id'] for e in scoped_events(s,'c',CFG)})
            s.extraction_status('c','links',sig,'error')
            self.assertNotIn('link',{e['id'] for e in scoped_events(s,'c',CFG)})

    def test_retry_cache_and_force_empty_result_preserve_history_and_audit(self):
        with tempfile.TemporaryDirectory() as temp, self.make(temp)[0] as s:
            def respond(config,system,content):
                if 'possible_targets' in content:
                    return {'links':[]}
                if '"surgery":' not in system:
                    return {'events':[]}
                return {'events':[{'kind':'surgery','event_time':None,'record_time':None,'attributes':{},
                    'evidence':[{'document_id':'d','page':1,'start_line':1,'end_line':1}]}]}
            with patch('medocr.facts.request_json',side_effect=respond):
                extract_facts_case(s,'c',CFG,True)
            count=len(s.events('c')); automatic=[e for e in s.events('c') if e['origin']=='local_llm'][0]
            with patch('medocr.facts.request_json',side_effect=AssertionError('cache must not call model')):
                self.assertEqual(extract_facts_case(s,'c',CFG,True)['cached_chunks'],1)
            with patch('medocr.facts.request_json',return_value={'events':[],'links':[]}):
                extract_facts_case(s,'c',CFG,True,force=True)
            self.assertNotIn(automatic['id'],{e['id'] for e in scoped_events(s,'c',CFG)})
            self.assertEqual(len(s.events('c')),count)
            self.assertTrue(s.all('SELECT * FROM extraction_plans'))

    def test_new_configuration_outputs_do_not_overwrite_old_evidence_or_values(self):
        with tempfile.TemporaryDirectory() as temp, self.make(temp)[0] as s:
            def respond(config,system,content):
                if 'possible_targets' in content:
                    return {'links':[]}
                if '"surgery":' not in system:
                    return {'events':[]}
                return {'events':[{'kind':'surgery','event_time':None,'record_time':None,
                    'attributes':{'unplanned':config['llm']['model']=='synthetic'},
                    'evidence':[{'document_id':'d','page':1,'start_line':1,'end_line':1}]}]}
            with patch('medocr.facts.request_json',side_effect=respond):
                extract_facts_case(s,'c',CFG,True)
                original=next(e for e in s.events('c') if e['origin']=='local_llm')
                changed=deepcopy(CFG); changed['llm']['model']='new-model'
                extract_facts_case(s,'c',changed,True)
            stored=next(e for e in s.events('c') if e['id']==original['id'])
            self.assertTrue(stored['attributes']['unplanned'])
            self.assertEqual(stored['quote'],original['quote'])
            self.assertFalse(any(e['id']==original['id'] for e in scoped_events(s,'c',changed)))
            self.assertFalse(next(e for e in scoped_events(s,'c',changed) if e['origin']=='local_llm')['attributes']['unplanned'])

    def test_rule_candidates_are_also_bound_to_current_ocr_and_configuration(self):
        with tempfile.TemporaryDirectory() as temp, self.make(temp)[0] as s:
            extract_facts_case(s,'c',CFG,False)
            self.assertEqual(len(scoped_events(s,'c',CFG,'rules')),1)
            changed={**CFG,'icu_departments':['changed-department']}
            self.assertEqual(scoped_events(s,'c',changed,'rules'),[])
            with s.conn:
                s.conn.execute("UPDATE pages SET text='无手术资料'")
            self.assertEqual(scoped_events(s,'c',CFG,'rules'),[])

    def test_draft_calculation_and_export_use_same_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            s,bid=self.make(temp)
            with s:
                helper(s,CFG,id='surgery',attrs={'event_date':'2025-04-01'})
                s.save_event({'id':'old','case_id':'c','document_id':'d','page':1,
                    'quote':'旧手术','kind':'surgery','origin':'local_llm','attributes':{
                        '_chunk':'old','_signature':'old','event_date':'2025-04-01'}})
                with s.conn:
                    s.conn.execute('INSERT INTO batch_processing VALUES(?,?,?)',(bid,'assisted','synthetic'))
                report=batch_report(s,bid,CFG)
                rows=[r for r in report['details'] if r['metric_id']=='core_15' and r['calculation_mode']=='draft']
                self.assertTrue(any('surgery' in r.get('fact_ids',[]) for r in rows))
                self.assertFalse(any('old' in r.get('fact_ids',[]) for r in rows))
                path=export_report(s,bid,{**CFG,'report_dir':str(Path(temp)/'reports')})
                facts=json.loads((path/'facts.json').read_text(encoding='utf-8'))
                self.assertFalse(next(e for e in facts if e['id']=='old')['eligible_in_mode'])
                self.assertTrue(next(e for e in facts if e['id']=='surgery')['eligible_in_mode'])
                self.assertIn('current_scopes',json.loads((path/'processing.json').read_text(encoding='utf-8')))

