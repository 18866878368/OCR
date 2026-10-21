import unittest

from medocr.retrieval import RETRIEVAL_VERSION, retrieval_plan
from medocr.rule_extract import rule_facts
from medocr.specs import core_specs


class RetrievalPlanTests(unittest.TestCase):
    def test_expert_plan_has_sources_and_evidence_policy(self):
        specs = {item['id']: item for item in core_specs()}
        for number in range(1, 19):
            with self.subTest(number=number):
                plan = specs[f'core_{number:02}']['retrieval']
                self.assertTrue(plan['sources'])
                self.assertTrue(plan['require_evidence'])
                self.assertEqual(plan['missing_data_policy'], '待核验')
        self.assertEqual(specs['core_01']['retrieval_version'], RETRIEVAL_VERSION)

    def test_indicator_two_plan_keeps_order_window_and_categories(self):
        plan = retrieval_plan('core_02')
        self.assertEqual(plan['calculation_rules']['time_window_hours'], 8)
        self.assertEqual(plan['calculation_rules']['allowed_order_categories'], ['examination', 'treatment'])

    def test_rule_extractor_keeps_titled_round_role_and_order_nursing_level(self):
        page = {
            'case_id': 'case', 'document_id': 'doc', 'page': 1,
            'text': '\n'.join([
                '主任医师查房记录',
                '查房时间：2025-04-12 10:00',
                '病情分析：病情稳定',
                '诊疗计划：继续观察',
                '长期医嘱',
                '开立时间：2025-04-12 11:00',
                '医嘱类型：长期',
                '医嘱内容：护理级别',
                '护理级别：一级护理',
            ]),
        }
        rows = rule_facts(page, {'icu_departments': ['ICU']})
        rounds = [row for row in rows if row['kind'] == 'ward_round']
        orders = [row for row in rows if row['kind'] == 'order']
        self.assertEqual(rounds[0]['attributes']['physician_role'], '主任医师')
        self.assertEqual(orders[0]['attributes']['nursing_level'], '一级护理')


if __name__ == '__main__':
    unittest.main()
