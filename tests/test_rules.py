import unittest
from medocr.rules import automatic_case, aggregate, calculate_measures, canonical_time, catalog, get_metric, parse_time
from medocr.extract import local_endpoint, candidate_events
from medocr.pipeline import grouped_lines
from medocr.report import safe_cell

CFG = {'transfer_time_basis':'record_time','icu_departments':['ICU','CCU','重症医学科']}
BATCH = {'start':'2025-04-01','end':'2025-04-30'}


def event(kind,time,verified=True,attrs=None,record=None):
    return {'id':kind+str(time),'case_id':'c','kind':kind,'event_time':time,'record_time':record,
            'verified':verified,'attributes':attrs or {},'document_id':'d','page':1,'quote':'synthetic'}


def admission():
    return event('admission','2025-04-12T14:50:00')


def transfer(time,verified=True,**attrs):
    return event('transfer',None,verified,{'occurred':True,'from_department':'心外科','to_department':'心内科',**attrs},time)


class RulesTests(unittest.TestCase):
    def test_catalog_units_and_special_types(self):
        self.assertEqual(len(catalog()),51)
        self.assertEqual(get_metric('core_20')['kind'],'ratio')
        self.assertEqual(get_metric('core_31')['kind'],'median')

    def test_date_parse_chinese_and_invalid(self):
        self.assertEqual(canonical_time('2025年4月12日14时50分'),'2025-04-12T14:50:00')
        self.assertIsNone(parse_time('2025-02-30 12:00'))
        self.assertIsNone(parse_time('2025-04-12'))

    def test_seconds_are_not_rounded(self):
        self.assertEqual(canonical_time('2025-04-14T14:50:01'),'2025-04-14T14:50:01')

    def test_transfer_boundary(self):
        for value,n in [('2025-04-14T14:50:00',1),('2025-04-14T14:50:01',None),('2025-04-12T14:49:59',None)]:
            with self.subTest(value=value):
                self.assertEqual(automatic_case('core_01','c',[admission(),transfer(value)],BATCH,CFG)['numerator'],n)

    def test_missing_transfer_is_unknown(self):
        result = automatic_case('core_01','c',[admission()],BATCH,CFG)
        self.assertEqual(result['denominator'],1)
        self.assertIsNone(result['numerator'])

    def test_unreviewed_transfer_is_only_suggestion(self):
        result = automatic_case('core_01','c',[admission(),transfer('2025-04-13T12:00',False)],BATCH,CFG)
        self.assertIsNone(result['numerator'])
        self.assertEqual(result['suggested_numerator'],1)

    def test_icu_excluded_and_multiple_transfers_once(self):
        items = [admission(),transfer('2025-04-13T12:00',to_department='ICU')]
        self.assertIsNone(automatic_case('core_01','c',items,BATCH,CFG)['numerator'])
        items.extend([transfer('2025-04-13T13:00'),transfer('2025-04-13T14:00')])
        self.assertEqual(automatic_case('core_01','c',items,BATCH,CFG)['numerator'],1)

    def test_unknown_department_not_assumed_non_icu(self):
        result = automatic_case('core_01','c',[admission(),transfer('2025-04-13T12:00',to_department=None)],BATCH,CFG)
        self.assertIsNone(result['numerator'])

    def test_plan_is_not_actual_transfer(self):
        result = automatic_case('core_01','c',[admission(),transfer('2025-04-13T12:00',occurred=False)],BATCH,CFG)
        self.assertIsNone(result['numerator'])

    def test_conflicting_admission_or_unverified_admission(self):
        for events in [[event('admission','2025-04-12T14:50',False)], [admission(),event('admission','2025-04-12T15:50')]]:
            self.assertIsNone(automatic_case('core_01','c',events,BATCH,CFG)['denominator'])

    def test_out_of_period(self):
        result = automatic_case('core_01','c',[event('admission','2025-05-01T00:00')],BATCH,CFG)
        self.assertEqual((result['numerator'],result['denominator']),(0,0))

    def test_time_basis_config(self):
        x = transfer('2025-04-15T00:00')
        x['event_time'] = '2025-04-13T00:00'
        self.assertIsNone(automatic_case('core_01','c',[admission(),x],BATCH,CFG)['numerator'])
        self.assertEqual(automatic_case('core_01','c',[admission(),x],BATCH,{**CFG,'transfer_time_basis':'event_time'})['numerator'],1)

    def test_eight_hour_order_boundary_and_category(self):
        for time,category,n in [('2025-04-12T22:50:00','examination',1),('2025-04-12T22:50:01','treatment',None),('2025-04-12T16:00','other',None)]:
            x = event('order',time,attrs={'order_category':category})
            self.assertEqual(automatic_case('core_02','c',[admission(),x],BATCH,CFG)['numerator'],n)

    def test_unknown_not_removed_from_denominator(self):
        result = aggregate(get_metric('core_01'),[{'numerator':1,'denominator':1},{'numerator':None,'denominator':1}])
        self.assertEqual(result['denominator'],2)
        self.assertEqual((result['lower'],result['upper']),(0.5,1))
        self.assertIsNone(result['value'])

    def test_unknown_denominator_blocks_range(self):
        result = aggregate(get_metric('core_01'),[{'numerator':1,'denominator':1},{'numerator':None,'denominator':None}])
        self.assertIsNone(result['lower'])
        self.assertIsNone(result['value'])

    def test_ratio_allows_distinct_populations(self):
        value = calculate_measures(get_metric('core_20'),{'numerator':3,'denominator':2})
        self.assertEqual(value['value'],1.5)
        partial = aggregate(get_metric('core_20'),[{'numerator':None,'denominator':0},{'numerator':1,'denominator':2}])
        self.assertIsNone(partial['value'])

    def test_rate_ratio_zero_and_normal(self):
        m = get_metric('core_26')
        values = {'four_success':2,'four_total':10,'three_success':1,'three_total':20}
        self.assertEqual(calculate_measures(m,values)['value'],4)
        self.assertIsNone(calculate_measures(m,{**values,'three_success':0})['value'])

    def test_median_and_missing_values(self):
        m = get_metric('core_31')
        self.assertEqual(calculate_measures(m,{'minutes':[9,1,3,5],'eligible_total':4})['value'],4)
        self.assertIsNone(calculate_measures(m,{'minutes':[9,1],'eligible_total':3})['value'])

    def test_catalog_and_retention(self):
        m = get_metric('core_29')
        self.assertEqual(calculate_measures(m,{'performed_types':['A','A'],'registered_types':['A','B']})['value'],0.5)
        with self.assertRaises(ValueError):
            calculate_measures(m,{'performed_types':['C'],'registered_types':['A','B']})
        self.assertEqual(calculate_measures(get_metric('core_30'),dict(zip('ABCDEF',[1,1,1,1,1,1])))['value'],0.5)

    def test_bad_counts_rejected(self):
        for values in [{'numerator':2,'denominator':1},{'numerator':True,'denominator':1},{'numerator':-1,'denominator':2}]:
            with self.assertRaises(ValueError):
                calculate_measures(get_metric('core_01'),values)

    def test_cloud_and_redirect_addresses_rejected(self):
        self.assertEqual(local_endpoint('http://127.0.0.1:11434/v1'),'http://127.0.0.1:11434/v1/chat/completions')
        for value in ['https://api.example.com/v1','http://10.0.0.1/v1','http://localhost.evil/v1','http://user:pass@localhost/v1']:
            with self.assertRaises(ValueError):
                local_endpoint(value)

    def test_candidate_is_not_automatically_reviewed(self):
        page = {'case_id':'c','document_id':'d','page':5,'category':'admission','text':'入院时间：2025-04-12 14:50\n记录时间：2025-04-12 18:22'}
        events = candidate_events(page)
        self.assertEqual(len(events),1)
        self.assertFalse(events[0]['verified'])
        self.assertEqual(events[0]['event_time'],'2025-04-12T14:50:00')

    def test_table_row_grouping(self):
        polys = [[[0,0],[60,0],[60,10],[0,10]],[[70,1],[100,1],[100,11],[70,11]]]
        lines,_ = grouped_lines(['04.12','14:50'],polys,[0.9,0.8])
        self.assertEqual(lines,['04.12 14:50'])

    def test_csv_formula_injection(self):
        self.assertEqual(safe_cell('=HYPERLINK("x")'),'\'=HYPERLINK("x")')
        self.assertEqual(safe_cell(5),5)


if __name__=='__main__':
    unittest.main()
