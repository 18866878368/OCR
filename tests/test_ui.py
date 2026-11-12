from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfWriter
from streamlit.testing.v1 import AppTest

from medocr.config import load_config
from medocr.store import Store
from medocr.batch import enqueue
from test_core_engine import fixture


class InterfaceTests(unittest.TestCase):
    def test_synthetic_batch_fact_editor_and_35_indicator_navigation(self):
        root=Path(__file__).resolve().parents[1]
        cfg=load_config(root/'config.json')
        with tempfile.TemporaryDirectory() as temp:
            cfg.update(data_dir=str(Path(temp)/'data'),work_dir=str(Path(temp)/'work'),report_dir=str(Path(temp)/'reports'))
            source_pdf = Path(temp)/'source.pdf'
            writer = PdfWriter()
            writer.add_blank_page(595,842)
            writer.write(source_pdf)
            with Store(cfg['data_dir']) as store:
                bid=store.batch('界面合成测试','2025-04-01','2025-04-30')
                with store.conn:
                    store.conn.execute("INSERT INTO cases(id) VALUES('c')")
                    store.conn.execute("INSERT INTO documents VALUES('d_c','c',?,'synthetic_hash',1)",(str(source_pdf),))
                    store.conn.execute("INSERT INTO batch_cases VALUES(?,'c')",(bid,))
                    store.conn.execute("INSERT INTO pages(document_id,page,text,stage) VALUES('d_c',1,'synthetic evidence','full')")
                for e in fixture():
                    store.save_event(e)
                store.save_measure(bid,'core_31','minutes',[2,8],'合成危急值台账','tester')
                store.save_measure(bid,'core_31','eligible_total',2,'合成危急值台账','tester')
                # The queue panel is rendered without starting a worker or loading a model.
                pending=Path(temp)/'synthetic.pdf'; pending.write_bytes(b'synthetic queue only')
                enqueue(store,bid,[pending])
            with patch('medocr.config.load_config',return_value=cfg):
                app=AppTest.from_file(str(root/'app.py'),default_timeout=20).run()
                self.assertFalse(app.exception)
                self.assertTrue(any(b.label=='开始处理' for b in app.button))
                app.radio(key='navigation').set_value('查看结果').run()
                self.assertFalse(app.exception)
                self.assertEqual(len(app.dataframe[0].value),35)
                app.selectbox(key='result_metric').set_value('core_05').run()
                self.assertFalse(app.exception)
                self.assertTrue(any(row.value=='### 结果展示' for row in app.markdown))
                result = next(row for row in app.expander if row.label=='第 1 条 · 规则')
                self.assertFalse(result.checkbox)
                self.assertTrue(any('证据页：1' in row.value for row in result.caption))
                audit = next(row for row in app.expander if '开发/审计详情' in row.label)
                self.assertTrue(any('PDF 第 1 页' in row.value for row in audit.markdown))
                next(box for box in audit.checkbox if box.label=='显示依据所在 PDF 原页').check().run()
                self.assertFalse(app.exception)
                self.assertFalse(app.error)
                audit = next(row for row in app.expander if '开发/审计详情' in row.label)
                self.assertEqual(audit.image[0].captions, ['原始 PDF 第 1 页'])
                next(box for box in audit.checkbox if box.label=='查看该页 OCR 全文').check().run()
                audit = next(row for row in app.expander if '开发/审计详情' in row.label)
                self.assertEqual(audit.text_area[0].value, 'synthetic evidence')
                app.selectbox(key='result_metric').set_value('core_30').run()
                self.assertTrue(any('四年新技术新项目台账' in row.value for row in app.markdown))
                app.selectbox(key='result_metric').set_value('core_31').run()
                next(r for r in app.radio if r.label=='结果类型').set_value('已复核结果').run()
                self.assertFalse(app.exception)
                self.assertTrue(any('5 分钟' in row.value for row in app.info))
                self.assertTrue(any('医院补充统计' in row.value for row in app.info))
                app.radio(key='navigation').set_value('核对与复核').run()
                app.radio(key='review_section').set_value('指标复核').run()
                self.assertEqual(len(app.selectbox(key='review_metric').options),35)
                app.selectbox(key='review_metric').set_value('core_31').run()
                self.assertFalse(app.exception)
                app.selectbox(key='review_metric').set_value('core_24').run()
                self.assertFalse(app.exception)
                app.radio(key='review_section').set_value('事实复核').run()
                relation=next(s for s in app.selectbox if s.label=='关联类型')
                relation.set_value('discussion_for_surgery').run()
                self.assertFalse(app.exception)
                self.assertTrue(any(b.label=='保存已复核关联' for b in app.button))

    def test_single_start_queues_older_import_and_never_auto_reviews(self):
        root=Path(__file__).resolve().parents[1]
        cfg=load_config(root/'config.local.json')
        with tempfile.TemporaryDirectory() as temp:
            cfg.update(data_dir=str(Path(temp)/'data'),work_dir=str(Path(temp)/'work'),report_dir=str(Path(temp)/'reports'))
            from medocr.pipeline import import_pdf
            pdf=Path(temp)/'synthetic.pdf'; writer=PdfWriter(); writer.add_blank_page(595,842)
            with pdf.open('wb') as file: writer.write(file)
            with Store(cfg['data_dir']) as store:
                bid=store.batch('单病例流程测试'); import_pdf(store,bid,pdf)
            with patch('medocr.config.load_config',return_value=cfg),patch('medocr.batch.launch_worker',return_value={'pid':123}) as worker:
                app=AppTest.from_file(str(root/'app.py'),default_timeout=20).run()
                app.button(key='start_processing').click().run()
                self.assertFalse(app.exception)
                worker.assert_called_once()
                self.assertFalse(worker.call_args.kwargs['use_llm'])
                self.assertEqual(worker.call_args.kwargs['scope'],'all')
                with Store(cfg['data_dir']) as store:
                    self.assertEqual(len(store.all('SELECT * FROM jobs')),1)
                    self.assertEqual(store.one('SELECT count(*) n FROM reviews')['n'],0)
                    self.assertEqual(store.one('SELECT count(*) n FROM coverage')['n'],0)
                assist = next(c for c in app.checkbox if c.label=='使用本机语义模型辅助疑难片段（可选）')
                assist.check().run()
                app.button(key='start_processing').click().run()
                self.assertTrue(worker.call_args.kwargs['use_llm'])

    def test_unknown_and_ratio_formats(self):
        from medocr.ui_workflow import format_value,chinese_csv
        from medocr.review_ui import _format_calculation
        self.assertEqual(format_value(None,'rate'),'待补充 / 待复核')
        self.assertEqual(format_value(0.2,'rate'),'20.00%')
        self.assertEqual(format_value(0.2,'rate_ratio'),'0.2')
        self.assertEqual(_format_calculation({'kind':'median','value':5,'denominator':2},False),'5 分钟（应统计 2 条）')
        self.assertEqual(_format_calculation({'kind':'retention','value':0.5,'numerator':3,'denominator':6},False),'3 / 6 = 50.00%')
        self.assertEqual(_format_calculation({'kind':'ratio','value':1.5,'numerator':3,'denominator':2},False),'3 / 2 = 1.5')
        self.assertIn("'=HYPERLINK",chinese_csv([{'指标':'=HYPERLINK(test)'}]).decode('utf-8-sig'))

    def test_historical_candidates_are_hidden_and_excluded_from_bulk_review(self):
        root=Path(__file__).resolve().parents[1]
        cfg=load_config(root/'config.local.json')
        with tempfile.TemporaryDirectory() as temp:
            cfg.update(data_dir=str(Path(temp)/'data'),work_dir=str(Path(temp)/'work'),report_dir=str(Path(temp)/'reports'))
            with Store(cfg['data_dir']) as store:
                bid=store.batch('历史隔离合成测试')
                with store.conn:
                    store.conn.execute("INSERT INTO cases(id) VALUES('c')")
                    store.conn.execute("INSERT INTO documents VALUES('d_c','c','synthetic.pdf','synthetic_hash',1)")
                    store.conn.execute("INSERT INTO batch_cases VALUES(?,'c')",(bid,))
                    store.conn.execute("INSERT INTO pages(document_id,page,text,stage) VALUES('d_c',1,'synthetic','full')")
                    store.conn.execute('INSERT INTO batch_processing VALUES(?,?,?)',(bid,'assisted','synthetic'))
                for e in fixture(): store.save_event(e)
                store.save_event({'id':'isolated_old','case_id':'c','kind':'surgery','document_id':'d_c','page':1,
                    'quote':'历史合成证据','origin':'local_llm','attributes':{'_chunk':'old','_signature':'old'}})
            with patch('medocr.config.load_config',return_value=cfg):
                app=AppTest.from_file(str(root/'app.py'),default_timeout=20).run()
                app.radio(key='navigation').set_value('核对与复核').run()
                app.radio(key='review_section').set_value('事实复核').run()
                self.assertFalse(app.exception)
                self.assertEqual(len(app.dataframe[0].value),len(fixture()))
                app.checkbox(key='review_history_c').check().run()
                self.assertFalse(app.exception)
                self.assertEqual(len(app.dataframe[0].value),len(fixture())+1)
                self.assertFalse(app.multiselect[0].options)

    def test_record_management_confirmation_history_cleanup_and_last_batch_deletion(self):
        root=Path(__file__).resolve().parents[1]
        cfg=load_config(root/'config.local.json')
        with tempfile.TemporaryDirectory() as temp:
            cfg.update(data_dir=str(Path(temp)/'data'),work_dir=str(Path(temp)/'work'),report_dir=str(Path(temp)/'reports'))
            with Store(cfg['data_dir']) as store:
                bid=store.batch('删除功能合成测试')
                with store.conn:
                    store.conn.execute("INSERT INTO cases(id) VALUES('c')")
                    store.conn.execute("INSERT INTO documents VALUES('d_c','c','synthetic.pdf','synthetic_hash',1)")
                    store.conn.execute("INSERT INTO batch_cases VALUES(?,'c')",(bid,))
                    store.conn.execute("INSERT INTO pages(document_id,page,text,stage) VALUES('d_c',1,'synthetic','full')")
                for e in fixture(): store.save_event(e)
                store.save_event({'id':'old_to_delete','case_id':'c','kind':'surgery','document_id':'d_c','page':1,
                    'quote':'历史合成证据','origin':'local_llm','attributes':{'_chunk':'old','_signature':'old'}})
            with patch('medocr.config.load_config',return_value=cfg):
                app=AppTest.from_file(str(root/'app.py'),default_timeout=20).run()
                app.radio(key='navigation').set_value('设置').run()
                self.assertFalse(app.exception)
                app.checkbox(key='delete_make_backup_'+bid+'_history').uncheck()
                next(b for b in app.button if b.label=='删除历史候选').click().run()
                self.assertTrue(any('确认' in e.value for e in app.error))
                with Store(cfg['data_dir']) as store:
                    self.assertTrue(store.one("SELECT id FROM events WHERE id='old_to_delete'"))
                app.checkbox(key='delete_make_backup_'+bid+'_history').uncheck()
                app.checkbox(key='delete_confirm_'+bid+'_history').check()
                next(b for b in app.button if b.label=='删除历史候选').click().run()
                self.assertFalse(app.exception)
                with Store(cfg['data_dir']) as store:
                    self.assertFalse(store.one("SELECT id FROM events WHERE id='old_to_delete'"))
                    self.assertEqual(len(store.events('c')),len(fixture()))
                app.radio(key='delete_record_kind').set_value('整个批次').run()
                app.checkbox(key='delete_make_backup_'+bid+'_batch').uncheck()
                app.checkbox(key='delete_confirm_'+bid+'_batch').check()
                next(b for b in app.button if b.label=='删除整个批次').click().run()
                self.assertFalse(app.exception)
                self.assertTrue(any('暂无可删除' in e.value for e in app.info))
                with Store(cfg['data_dir']) as store:
                    self.assertEqual(store.all('SELECT * FROM batches'),[])
