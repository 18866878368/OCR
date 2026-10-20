from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from medocr.core_engine import Context, case_items, overlap_items, special_measures, rule_hash
from medocr.rules import batch_report
from medocr.specs import core_specs
from medocr.store import Store

BATCH = {'id':'b','start':'2025-04-01','end':'2025-04-30'}
CONFIG = {'calculation_engine':'core35','indicator_set':'core','transfer_time_basis':'record_time',
 'icu_departments':['ICU'],'hospital_reference':{
 'surgery_grades':{'术式A':4,'术式B':3},'registered_types':['术式A','术式B'],
 'special_antibiotics':['药A'],'physician_ids':{'医生甲':'doctor1','麻醉甲':'anes1'},
 'calendar_start':'2025-04-01','calendar_end':'2025-04-30',
 'workdays':['2025-04-14','2025-04-15','2025-04-16','2025-04-17','2025-04-18'],
 'dispute_inventory_complete':True,'project_inventory_complete':True,'project_cycle_start':2022,
 'projects':[{'project_id':'A','introduced_year':2023,'carried_years':[2024,2025]},
             {'project_id':'B','introduced_year':2023,'carried_years':[2024]},
             {'project_id':'C','introduced_year':2023,'carried_years':[2025]},
             {'project_id':'D','introduced_year':2023,'carried_years':[]},
             {'project_id':'E','introduced_year':2024,'carried_years':[2025]},
             {'project_id':'F','introduced_year':2024,'carried_years':[]}]}}


def fact(kind,id,time=None,attrs=None,record=None,case='c'):
    return {'id':case+'_'+id,'case_id':case,'kind':kind,'event_time':time,'record_time':record,
            'attributes':attrs or {},'document_id':'d_'+case,'page':1,'quote':'synthetic '+id,
            'verified':True,'active':True,'origin':'synthetic'}


def link(source,target,relation):
    return fact('link',source['id']+target['id']+relation,attrs={
        'source_event_id':source['id'],'target_event_id':target['id'],'relation':relation},case=source['case_id'])


def fixture():
    a = fact('admission','a','2025-04-01T08:00:00',{'department':'内科'})
    discharge = fact('discharge','discharge','2025-04-12T10:00:00',{'nursing_level':'一级护理'})
    transfer = fact('transfer','transfer',attrs={'occurred':True,'from_department':'内科','to_department':'外科'},record='2025-04-02T07:00:00')
    def order(id,time,attrs=None):
        return fact('order',id,time,{'order_type':'temporary','order_category':'treatment','is_medication':False,**(attrs or {})})
    o1 = order('o1','2025-04-01T09:00:00',{'order_type':'long','order_category':'examination','stop_time':'2025-04-01T17:00:00'})
    o2 = order('o2','2025-04-02T08:00:00',{'drug_name':'药A','is_medication':True})
    o4 = order('o4','2025-04-01T08:50:00')
    o5 = order('o5','2025-04-02T10:00:00')
    os = order('os','2025-04-02T12:00:00')
    oc = order('oc','2025-04-01T14:06:00')
    c1 = fact('consultation','c1','2025-04-01T08:10:00',{'urgency':'emergency','arrival_time':'2025-04-01T08:20:00','completion_time':'2025-04-01T08:30:00'})
    c2 = fact('consultation','c2','2025-04-01T10:00:00',{'urgency':'ordinary','completion_time':'2025-04-02T10:00:00'})
    cd = fact('consultation','cd','2025-04-02T07:40:00',{'urgency':'ordinary','completion_time':'2025-04-02T07:50:00'})
    s = fact('surgery','s','2025-04-03T09:00:00',{'procedure_name':'术式A','planned_procedure':'术式A','surgeon':'医生甲',
        'planned_surgeon':'医生甲','grade':4,'end_time':'2025-04-03T11:00:00','unplanned':True,'reoperation':True,
        'emergency_lifesaving':False,'intraoperative_transfusion':True,'autotransfusion':True})
    ana = fact('anesthesia','ana','2025-04-03T08:50:00',{'physician':'麻醉甲','end_time':'2025-04-03T11:10:00'})
    pre = fact('discussion','pre','2025-04-02T10:00:00',{'discussion_type':'preoperative','complete':True,
        'participants':['医生甲','医生乙'],'departments':['内科','外科','麻醉科'],'completion_time':'2025-04-02T10:00:00'},record='2025-04-02T10:00:00')
    difficult = fact('discussion','difficult','2025-04-02T16:00:00',{'discussion_type':'difficult','complete':True,'conclusion':'调整治疗'})
    death_disc = fact('discussion','death_disc','2025-04-14T14:00:00',{'discussion_type':'death','organizer':'医务科','host':'医生主任','host_role':'科主任'},record='2025-04-14T14:00:00')
    consent = fact('consent','consent','2025-04-02T13:00:00',{'patient_signed_time':'2025-04-02T13:00:00','physician_signed_time':'2025-04-02T12:30:00'})
    hand = fact('nursing','hand','2025-04-03T20:00:00',{'nursing_type':'bedside_handover'})
    rescue = fact('rescue','rescue','2025-04-10T10:00:00',{'end_time':'2025-04-10T10:30:00','survival_confirmed_until':'2025-04-11T11:00:00'})
    death = fact('death','death','2025-04-12T10:00:00')
    critical = fact('critical_value','critical','2025-04-01T14:00:00',{'received_time':'2025-04-01T14:05:00'})
    ward = fact('ward_round','ward','2025-04-01T15:00:00',{'physician_role':'主治医师','analysis':'病情分析','plan':'治疗意见','norm_compliant':True})
    blood = fact('transfusion','blood','2025-04-03T10:00:00',{'end_time':'2025-04-03T10:30:00','intraoperative':True,'autologous':True})
    assessment = fact('transfusion_assessment','assessment','2025-04-03T12:00:00',{'norm_compliant':True,'assessment':'输血后评估'})
    complication = fact('complication','complication','2025-04-04T12:00:00',{'surgery_related':True,'diagnosis':'术后并发症'})
    events = [a,discharge,transfer,o1,o2,o4,o5,os,oc,c1,c2,cd,s,ana,pre,difficult,death_disc,consent,hand,rescue,death,critical,ward,blood,assessment,complication,
        fact('cost','cost',attrs={'total':250000}),fact('readmission','readmission','2025-04-01T08:00:00',{'unplanned':True}),
        fact('dispute','dispute',attrs={'death_related':True}),fact('upload','upload',attrs={'required':True,'complete':True})]
    for source,target,relation in [(c1,o4,'implements_consultation'),(c2,o5,'implements_consultation'),(cd,o2,'implements_consultation'),
        (s,pre,'discussion_for_surgery'),(s,os,'order_for_surgery'),(s,consent,'consent_for_surgery'),(s,hand,'handover_for_surgery'),
        (s,ana,'anesthesia_for_surgery'),(s,complication,'complication_of_surgery'),(s,blood,'transfusion_for_surgery'),
        (blood,assessment,'assessment_for_transfusion'),(critical,oc,'treatment_for_critical_value'),(critical,ward,'round_for_critical_value'),
        (o2,cd,'consultation_for_antibiotic')]:
        events.append(link(source,target,relation))
    return events


class CoreEngineTests(unittest.TestCase):
    def test_professional_title_does_not_prove_department_head(self):
        events=fixture()
        discussion=next(e for e in events if e['id']=='c_death_disc')
        discussion['attributes']['host_role']='主任医师'
        self.assertIsNone(case_items(21,self.context(events))[0]['numerator'])
        ctx=self.context(events)
        discussion['attributes']['department']='外科'
        ctx=self.context(events)
        ctx.refs['department_heads']={'外科':'医生主任'}
        self.assertEqual(case_items(21,ctx)[0]['numerator'],1)

    def test_reused_order_identifier_with_different_times_is_ambiguous(self):
        events=[fact('order','one','2025-04-01T08:00:00',{'order_id':'same'}),
                fact('order','two','2025-04-02T08:00:00',{'order_id':'same'})]
        ctx=self.context(events)
        self.assertEqual(len(ctx.get('order')),2)
        self.assertIn('order',ctx.duplicates)

    def context(self, events=None, complete=True):
        return Context('c',fixture() if events is None else events,BATCH,deepcopy(CONFIG),complete)

    def test_case_and_event_rules_cover_all_regular_core_indicators(self):
        ctx = self.context()
        regular = set(range(1,36))-{24,25,26,27,29,30,31}
        for n in sorted(regular):
            with self.subTest(indicator=n):
                rows = case_items(n,ctx)
                self.assertTrue(rows)
                self.assertTrue(all(r['denominator']==1 and r['numerator']==1 for r in rows),rows)

    def test_consultation_time_boundary_and_unrelated_order(self):
        events = fixture()
        e = next(e for e in events if e['id']=='c_c1')
        e['attributes']['arrival_time']='2025-04-01T08:20:01'
        ctx = self.context(events)
        self.assertEqual(case_items(5,ctx)[0]['numerator'],0)
        events = [e for e in events if not (e['kind']=='link' and e['attributes'].get('relation')=='implements_consultation')]
        self.assertEqual(case_items(6,self.context(events))[0]['numerator'],0)
        self.assertIsNone(case_items(6,self.context(events,False))[0]['numerator'])

    def test_nonplanned_first_surgery_is_not_unplanned_reoperation(self):
        events = [e for e in fixture() if e['kind']!='readmission']
        next(e for e in events if e['kind']=='surgery')['attributes']['reoperation']=False
        self.assertEqual(case_items(11,self.context(events))[0]['denominator'],0)

    def test_missing_transfer_attributes_never_become_negative(self):
        events = fixture()
        next(e for e in events if e['kind']=='transfer')['attributes']['occurred']=None
        self.assertIsNone(case_items(1,self.context(events))[0]['numerator'])

    def test_ratio_populations_are_independent(self):
        events = fixture()
        next(e for e in events if e['kind']=='dispute')['attributes']['death_related']=False
        row = case_items(20,self.context(events))[0]
        self.assertEqual((row['numerator'],row['denominator']),(1,0))

    def test_unknown_external_disputes_and_uploads(self):
        ctx = self.context([e for e in fixture() if e['kind'] not in {'dispute','upload'}])
        ctx.refs['dispute_inventory_complete']=False
        self.assertIsNone(case_items(20,ctx)[0]['denominator'])
        self.assertIsNone(case_items(22,ctx)[0]['denominator'])

    def test_workday_calendar_is_required(self):
        ctx = self.context(); ctx.refs.pop('workdays')
        self.assertIsNone(case_items(19,ctx)[0]['numerator'])

    def test_surgery_names_need_alias_or_semantic_confirmation(self):
        ctx = self.context()
        ctx.get('surgery')[0]['attributes']['planned_procedure']='术式A别名'
        self.assertIsNone(case_items(17,ctx)[0]['numerator'])
        ctx.refs['procedure_aliases']={'术式A别名':'术式A'}
        self.assertEqual(case_items(17,ctx)[0]['numerator'],1)

    def test_overlap_counts_surgeries_once_and_excludes_touching_endpoints(self):
        first = self.context()
        s = fact('surgery','s','2025-04-03T10:00:00',{'surgeon':'医生甲','end_time':'2025-04-03T12:00:00'},case='other')
        a = fact('anesthesia','a','2025-04-03T10:00:00',{'physician':'麻醉甲','end_time':'2025-04-03T12:00:00'},case='other')
        second = Context('other',[s,a,link(s,a,'anesthesia_for_surgery')],BATCH,CONFIG,True)
        for n in (24,25):
            rows = overlap_items([first,second],n,True)
            self.assertEqual(sum(r['numerator'] for r in rows),2)
        s['event_time']='2025-04-03T11:00:00'
        second = Context('other',[s],BATCH,CONFIG,True)
        self.assertEqual(sum(r['numerator'] for r in overlap_items([first,second],24,True)),0)
        self.assertIsNone(overlap_items([first],24,False)[0]['numerator'])

    def test_special_formulas_use_all_four_counts_unique_types_and_project_ledger(self):
        first = self.context()
        a = fact('admission','a','2025-04-01T08:00:00',case='other')
        s = fact('surgery','s','2025-04-05T08:00:00',{'procedure_name':'术式B','grade':3},case='other')
        c = fact('complication','c','2025-04-06T08:00:00',{'surgery_related':True},case='other')
        d = fact('death','d','2025-04-15T08:00:00',case='other')
        cv = fact('critical_value','cv','2025-04-01T09:00:00',{'received_time':'2025-04-01T09:15:00'},case='other')
        second = Context('other',[a,s,c,d,cv,link(s,c,'complication_of_surgery')],BATCH,CONFIG,True)
        contexts = [first,second]
        specs = {int(s['id'][-2:]):s for s in core_specs()}
        from medocr.rules import calculate_measures
        for n,expected in [(26,1),(27,1),(29,1),(30,0.5),(31,10)]:
            with self.subTest(indicator=n):
                values,_,missing = special_measures(n,contexts,CONFIG)
                self.assertFalse(missing)
                self.assertEqual(calculate_measures(specs[n],values)['value'],expected)

    def test_long_order_count_and_antibiotic_dictionary(self):
        ctx = self.context()
        rows = case_items(23,ctx)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['numerator'],1)
        ctx.refs.pop('special_antibiotics')
        self.assertIsNone(case_items(33,ctx)[0]['denominator'])

    def test_date_only_order_is_counted_without_fabricating_a_clock(self):
        e=fact('order','dateonly',attrs={'order_type':'long','event_date':'2025-04-03','stop_date':'2025-04-03'})
        ctx=self.context([e])
        row=case_items(23,ctx)[0]
        self.assertEqual((row['denominator'],row['numerator']),(1,1))
        e['attributes']['event_date']='2025-05-03'
        self.assertEqual(case_items(23,self.context([e]))[0]['denominator'],0)

    def test_transfusion_is_per_event_and_requires_later_assessment(self):
        ctx = self.context()
        ctx.get('transfusion_assessment')[0]['event_time']='2025-04-03T10:00:00'
        self.assertEqual(case_items(34,ctx)[0]['numerator'],0)

    def test_full_report_has_35_indicators_and_separates_draft_from_reviewed(self):
        with tempfile.TemporaryDirectory() as temp, Store(temp) as store:
            bid = store.batch('synthetic','2025-04-01','2025-04-30')
            with store.conn:
                store.conn.execute("INSERT INTO cases(id) VALUES('c')")
                store.conn.execute("INSERT INTO documents VALUES('d_c','c','synthetic.pdf','hash',1)")
                store.conn.execute("INSERT INTO batch_cases VALUES(?,'c')",(bid,))
                store.conn.execute("INSERT INTO pages(document_id,page,stage,text) VALUES('d_c',1,'full','synthetic')")
            for e in fixture():
                e['verified']=False
                store.save_event(e)
            store.extraction_status('c','chunk','signature','complete')
            store.extraction_status('c','links','signature','complete')
            report = batch_report(store,bid,CONFIG)
            self.assertEqual(len(report['summary']),35)
            first = report['summary'][0]
            self.assertEqual(first['draft_value'],1)
            self.assertIsNone(first['value'])
            for e in store.events('c'):
                store.review_event(e['id'],{'verified':True},'doctor')
            for spec in core_specs():
                store.set_coverage(bid,'c',spec['id'],True,'All synthetic source objects checked','doctor')
                if spec['rule_conflict']:
                    store.approve_rule(bid,spec['id'],rule_hash(spec),'doctor','Synthetic rule review')
            report = batch_report(store,bid,CONFIG)
            indexed = {r['metric_id']:r for r in report['summary']}
            for n in set(range(1,36))-{24,25,26,27,29,30,31}:
                self.assertEqual(indexed[f'core_{n:02}']['value'],1,indexed[f'core_{n:02}'])
            self.assertEqual(indexed['core_24']['value'],0)
            self.assertEqual(indexed['core_25']['value'],0)
            self.assertEqual(indexed['core_30']['value'],0.5)
            self.assertEqual(indexed['core_31']['value'],5)
            store.close()

    def test_unverified_validation_issues_cannot_enter_numeric_draft(self):
        with tempfile.TemporaryDirectory() as temp,Store(temp) as store:
            bid=store.batch('uncertain','2025-04-01','2025-04-30')
            with store.conn:
                store.conn.execute("INSERT INTO cases(id) VALUES('c')")
                store.conn.execute("INSERT INTO documents VALUES('d_c','c','synthetic.pdf','hash',1)")
                store.conn.execute("INSERT INTO batch_cases VALUES(?,'c')",(bid,))
                store.conn.execute("INSERT INTO pages(document_id,page,stage,text) VALUES('d_c',1,'full','synthetic')")
            for event in fixture():
                if event['kind']=='transfer':
                    event['verified']=False
                    event['attributes']['_validation_issues']=['time not supported']
                store.save_event(event)
            store.extraction_status('c','chunk','signature','complete')
            store.set_coverage(bid,'c','core_01',True,'Synthetic coverage','doctor')
            report=batch_report(store,bid,CONFIG)
            self.assertIsNone(report['summary'][0]['draft_value'])
            self.assertIsNone(report['summary'][0]['value'])
            store.review_event('c_transfer',{'verified':True},'doctor')
            self.assertEqual(batch_report(store,bid,CONFIG)['summary'][0]['value'],1)
