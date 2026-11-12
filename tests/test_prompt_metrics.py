from copy import deepcopy
import json
import tempfile
import unittest
from unittest.mock import patch

from medocr.config import load_config
from medocr.prompt_metrics import PROMPT_METRIC_IDS, RULE_FIRST_IDS, _validate_result, run_prompt_metrics, prompt_summary, prompt_items
from medocr.rules import batch_report
from medocr.report import persist_batch_report
from medocr.specs import core_specs
from medocr.store import Store


class PromptMetricTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.batch = self.store.batch("prompt metrics")
        self.case = "case-prompt"
        self.document = "document-prompt"
        self.store.conn.execute(
            "INSERT INTO cases(id,episode_key,admission_complete) VALUES(?,?,?)",
            (self.case, None, 1),
        )
        self.store.conn.execute(
            "INSERT INTO batch_cases(batch_id,case_id) VALUES(?,?)",
            (self.batch, self.case),
        )
        self.store.conn.execute(
            "INSERT INTO documents(id,case_id,path,file_hash,page_count) VALUES(?,?,?,?,?)",
            (self.document, self.case, "case.pdf", "hash", 1),
        )
        self.store.conn.execute(
            "INSERT INTO pages(document_id,page,text,category,stage,signature,image_path,error,payload,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.document, 1, "主任医师查房记录\n术前讨论", "progress", "full", "page", "", "", "{}", "now"),
        )
        self.store.conn.commit()
        self.config = load_config("config.local.json")
        self.config["prompt_metrics"]["metric_ids"] = ["core_03"]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_rule_first_and_prompt_sets_are_disjoint(self):
        self.assertEqual(
            RULE_FIRST_IDS,
            {"core_01", "core_02", "core_05", "core_07", "core_09", "core_13", "core_15", "core_17"},
        )
        self.assertTrue(RULE_FIRST_IDS.isdisjoint(PROMPT_METRIC_IDS))

    def test_unlabelled_order_citation_recovers_short_dates_only_when_row_agrees(self):
        quote = '04.01 08:00 模拟医嘱 04.02 17:00'
        pages = {1: [(self.document, '长期医嘱单\n2025年\n' + quote)]}
        values = {'order_type': 'long', 'start_date': '2025-04-01', 'stop_date': '2025-04-02'}
        response = {'id': 'core_23', 'status': 'complete', 'objects': [{
            'item_key': 'one', 'reason': '模拟理由', 'input_values': values,
            'evidence': [{'page': 1, 'quote': quote, 'purpose': '医嘱原文'}],
        }]}
        spec = next(item for item in core_specs() if item['id'] == 'core_23')
        row = _validate_result(response, spec, pages, {})['objects'][0]
        self.assertEqual(row['input_values'], values)
        self.assertEqual((row['numerator'], row['denominator']), (0, 1))
        self.assertEqual(row['input_evidence']['order_type'][0]['quote'], '长期医嘱单')
        self.assertEqual(row['input_evidence']['stop_date'][0]['document_id'], self.document)

        values['stop_date'] = values['start_date']
        row = _validate_result(response, spec, pages, {})['objects'][0]
        self.assertIsNone(row['input_values']['stop_date'])
        self.assertIsNone(row['numerator'])

    def test_order_fields_and_model_history_survive_recalculation(self):
        self.config['prompt_metrics']['metric_ids'] = ['core_23']
        order_text = '开立 2025-04-01 08:00 停止 2025-04-01 17:00'
        with self.store.conn:
            self.store.conn.execute('UPDATE pages SET text=? WHERE document_id=?',
                                    ('长期医嘱\n' + order_text, self.document))
        obj = {
            'item_key': 'order-1', 'numerator': 9, 'denominator': 142, 'reason': '模型候选',
            'input_values': {'order_type': 'long', 'start_date': '2025-04-01', 'stop_date': '2025-04-01'},
            'evidence': [{'page': 1, 'quote': '长期医嘱', 'fields': ['order_type']},
                         {'page': 1, 'quote': order_text, 'fields': ['start_date', 'stop_date']}],
        }
        response = {'id': 'core_23', 'status': 'complete', 'objects': [obj]}
        with patch('medocr.prompt_metrics._prompt_request', return_value={'results': [response]}):
            self.assertEqual(run_prompt_metrics(self.store, self.batch, self.case, self.config)['status'], 'complete')
        first = persist_batch_report(self.store, self.batch, self.config)
        detail = self.store.one("SELECT payload FROM calculation_details WHERE run_id=? AND metric_id='core_23' AND mode='prompt'",
                                (first['run_id'],))
        saved = json.loads(detail['payload'])
        self.assertEqual((saved['numerator'], saved['denominator']), (1, 1))
        self.assertEqual(saved['input_values'], obj['input_values'])
        self.assertEqual(saved['input_evidence']['stop_date'][0]['document_id'], self.document)
        history = self.store.one('SELECT raw_response, payload FROM prompt_result_history WHERE id=?', (saved['prompt_result_id'],))
        self.assertEqual(json.loads(history['raw_response']), response)
        self.assertEqual(json.loads(history['payload'])['objects'][0]['numerator'], 1)

        changed = deepcopy(response)
        changed['objects'][0]['input_values']['stop_date'] = '2025-04-02'  # Unsupported by cited OCR.
        with patch('medocr.prompt_metrics._prompt_request', return_value={'results': [changed]}):
            run_prompt_metrics(self.store, self.batch, self.case, self.config, force=True)
        second = persist_batch_report(self.store, self.batch, self.config)
        latest = next(row for row in second['details'] if row['metric_id']=='core_23' and row['calculation_mode']=='prompt')
        self.assertIsNone(latest['input_values']['stop_date'])
        self.assertIsNone(latest['numerator'])
        self.assertEqual(latest['missing_fields'], ['stop_date'])
        self.assertIsNone(next(row for row in second['summary'] if row['metric_id']=='core_23')['draft_value'])
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertNotEqual(saved['prompt_result_id'], latest['prompt_result_id'])
        self.assertEqual(self.store.one('SELECT COUNT(*) n FROM prompt_result_history')['n'], 2)

    def test_prompt_result_uses_existing_page_and_normalizes_percent(self):
        response = {
            "results": [{
                "id": "core_03", "status": "complete",
                "objects": [{"item_key": "case", "numerator": 1, "denominator": 1,
                             "reason": "有主任医师查房",
                             "evidence": [{"page": 1, "quote": "主任医师查房记录"}]}],
                "conflicts": [], "components": {},
            }]
        }
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "synthetic-key"}), patch("medocr.prompt_metrics._prompt_request", return_value=response) as request:
            result = run_prompt_metrics(self.store, self.batch, self.case, self.config)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(request.call_count, 1)
        saved = self.store.prompt_results(self.batch, self.case, "core_03")[0]
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["payload"]["status"], "complete")
        self.assertFalse(saved["payload"].get("validation_issues"))
        self.assertTrue(saved["payload"]["objects"][0]["evidence"][0]["literal_match"])
        detail = prompt_items(self.store, self.batch, "core_03")[0]
        self.assertIn("有主任医师查房", detail["reason"])

    def test_legacy_placeholder_does_not_hide_object_reason_or_rewrite_payload(self):
        payload = {
            "id": "core_03", "status": "insufficient",
            "numerator_reason": "模型未提供说明",
            "denominator_reason": "模型未提供说明", "analysis": "模型未提供说明",
            "validation_issues": ["numerator_reason缺失", "denominator_reason缺失", "analysis缺失", "原文证据无法定位"],
            "objects": [{"item_key": "case", "numerator": 1, "denominator": 1,
                         "reason": "旧结果已保存对象判定依据", "evidence": []}],
        }
        original = deepcopy(payload)
        self.store.save_prompt_result(self.batch, self.case, "core_03", "legacy", "complete", payload)
        detail = prompt_items(self.store, self.batch, "core_03")[0]
        self.assertIn("旧结果已保存对象判定依据", detail["reason"])
        self.assertEqual(detail["numerator_reason"], "")
        self.assertEqual(detail["denominator_reason"], "")
        self.assertEqual(detail["analysis"], "")
        self.assertEqual(detail["prompt_status"], "insufficient")
        self.assertEqual(detail["prompt_payload"], original)
        report = batch_report(self.store, self.batch, self.config)
        row = next(row for row in report['details'] if row['metric_id']=='core_03' and row['calculation_mode']=='prompt')
        self.assertEqual(row['calculation_trace']['unknowns'], ['原文证据无法定位'])
        saved = self.store.prompt_results(self.batch, self.case, "core_03")[0]
        self.assertEqual(saved["payload"], original)

    def test_multiple_objects_keep_their_own_reasons(self):
        payload = {
            "id": "core_06", "status": "complete",
            "numerator_reason": "整个指标的分子说明",
            "denominator_reason": "整个指标的分母说明", "analysis": "整个指标的总体分析",
            "objects": [
                {"item_key": "first", "numerator": 1, "denominator": 1,
                 "reason": "第一对象符合条件", "numerator_reason": "第一对象有对应处置",
                 "denominator_reason": "第一对象是应统计会诊", "analysis": "第一对象分析", "evidence": []},
                {"item_key": "second", "numerator": 0, "denominator": 1,
                 "reason": "第二对象未符合条件", "evidence": []},
            ],
        }
        self.store.save_prompt_result(self.batch, self.case, "core_06", "objects", "complete", payload)
        first, second = prompt_items(self.store, self.batch, "core_06")
        self.assertEqual(first["numerator_reason"], "第一对象有对应处置")
        self.assertEqual(first["denominator_reason"], "第一对象是应统计会诊")
        self.assertEqual(first["analysis"], "第一对象分析")
        self.assertIn("第二对象未符合条件", second["reason"])
        self.assertEqual(second["numerator_reason"], "")
        self.assertEqual(second["denominator_reason"], "")
        self.assertEqual(second["analysis"], "")

    def test_object_without_any_reason_is_marked_insufficient(self):
        response = {"results": [{
            "id": "core_03", "status": "complete",
            "objects": [{"item_key": "case", "numerator": None, "denominator": None,
                         "evidence": [{"page": 1, "quote": "主任医师查房记录"}]}],
            "conflicts": [], "components": {},
        }]}
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "synthetic-key"}), patch(
                "medocr.prompt_metrics._prompt_request", return_value=response):
            run_prompt_metrics(self.store, self.batch, self.case, self.config)
        payload = self.store.prompt_results(self.batch, self.case, "core_03")[0]["payload"]
        self.assertEqual(payload["status"], "insufficient")
        self.assertTrue(any(issue.startswith("objects[0]") and "判定理由缺失" in issue
                            for issue in payload["validation_issues"]))
        detail = prompt_items(self.store, self.batch, "core_03")[0]
        self.assertIsNone(detail["numerator"])
        self.assertIsNone(detail["denominator"])

    def test_special_metric_without_objects_retains_top_level_explanation(self):
        payload = {
            "id": "core_31", "status": "complete", "objects": [],
            "analysis": "按两条报告时长计算中位数",
            "numerator_reason": "报告时长分别为 2 和 8 分钟",
            "denominator_reason": "共统计两条危急值报告",
            "components": {"minutes": [2, 8], "eligible_total": 2}, "evidence": [],
        }
        self.store.save_prompt_result(self.batch, self.case, "core_31", "special", "complete", payload)
        detail = prompt_items(self.store, self.batch, "core_31")[0]
        self.assertIn(payload["analysis"], detail["reason"])
        self.assertEqual(detail["analysis"], payload["analysis"])
        self.assertEqual(detail["numerator_reason"], payload["numerator_reason"])
        self.assertEqual(detail["denominator_reason"], payload["denominator_reason"])

    def test_invalid_evidence_is_partial_and_not_zero(self):
        response = {
            "results": [{
                "id": "core_03", "status": "insufficient",
                "objects": [{"item_key": "case", "numerator": 0, "denominator": 1,
                             "reason": "无法核验",
                             "evidence": [{"page": 1, "quote": "不存在的原文"}]}],
                "conflicts": [], "components": {},
            }]
        }
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "synthetic-key"}), patch("medocr.prompt_metrics._prompt_request", return_value=response):
            result = run_prompt_metrics(self.store, self.batch, self.case, self.config)
        self.assertEqual(result["status"], "complete")
        saved = self.store.prompt_results(self.batch, self.case, "core_03")[0]
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["payload"]["status"], "insufficient")

    def test_valid_insufficient_result_is_cached_without_second_paid_call(self):
        response = {"results": [{"id": "core_03", "status": "insufficient", "objects": [],
                                  "conflicts": [], "components": {}}]}
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "synthetic-key"}), patch("medocr.prompt_metrics._prompt_request", return_value=response) as request:
            first = run_prompt_metrics(self.store, self.batch, self.case, self.config)
            second = run_prompt_metrics(self.store, self.batch, self.case, self.config)
        self.assertEqual(first["status"], "complete")
        self.assertEqual(second["status"], "cached")
        self.assertEqual(request.call_count, 1)

    def test_objects_are_aggregated_by_rule_engine_units(self):
        response = {"results": [{"id": "core_03", "status": "complete",
                                  "objects": [{"item_key": "case", "numerator": 1, "denominator": 1,
                                                "reason": "符合", "evidence": [{"page": 1, "quote": "主任医师查房记录"}]}],
                                  "conflicts": [], "components": {}}]}
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "synthetic-key"}), patch("medocr.prompt_metrics._prompt_request", return_value=response):
            run_prompt_metrics(self.store, self.batch, self.case, self.config)
        spec = next(item for item in core_specs() if item["id"] == "core_03")
        result, details = prompt_summary(self.store, self.batch, spec)
        self.assertEqual((result["numerator"], result["denominator"], result["value"]), (1, 1, 1))
        self.assertEqual(details[0]["origin"], "prompt")
        report = batch_report(self.store, self.batch, self.config)
        row = next(row for row in report['details'] if row['metric_id']=='core_03' and row['calculation_mode']=='prompt')
        trace = row['calculation_trace']
        self.assertEqual(trace['objects'][0]['item_key'], 'case')
        self.assertEqual(trace['steps'][0]['evidence'][0]['page'], 1)


if __name__ == "__main__":
    unittest.main()
