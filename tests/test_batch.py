from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfWriter

from medocr.batch import enqueue,run_batch
from medocr.store import Store


class BatchTests(unittest.TestCase):
    def test_queue_dedup_resume_and_one_failure_does_not_stop_other_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); store = Store(root/'data'); bid = store.batch('test')
            good = root/'a_good.pdf'; writer = PdfWriter(); writer.add_blank_page(595,842)
            with good.open('wb') as f:
                writer.write(f)
            bad = root/'b_bad.pdf'; bad.write_bytes(b'bad pdf')
            self.assertEqual(enqueue(store,bid,[good,good,bad]),2)
            self.assertEqual(enqueue(store,bid,[good,bad]),0)
            config = {'report_dir':str(root/'reports'),'indicator_set':'core','calculation_engine':'core35',
                      'transfer_time_basis':'record_time','icu_departments':['ICU'],'ocr':{},'llm':{'enabled':False}}
            with patch('medocr.batch.process_pages',return_value={'failed':0}),patch('medocr.batch.extract_case',return_value={'failed_pages':0}):
                stats = run_batch(store,bid,config)
                self.assertEqual((stats['complete'],stats['failed']),(1,1))
                again = run_batch(store,bid,config)
                self.assertEqual((again['cached'],again['failed']),(1,1))
            self.assertEqual(len(store.cases(bid)),1)
            self.assertTrue((root/'reports'/bid/'facts.json').is_file())
            self.assertTrue((root/'reports'/bid/'processing.json').is_file())
            store.close()

    def test_cancel_does_not_turn_into_zero_or_failure_and_can_resume(self):
        from medocr.batch import stop_worker
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); store=Store(root/'data'); bid=store.batch('test')
            pdf=root/'case.pdf'; writer=PdfWriter(); writer.add_blank_page(595,842)
            with pdf.open('wb') as f: writer.write(f)
            enqueue(store,bid,[pdf])
            cfg={'report_dir':str(root/'reports'),'indicator_set':'core','calculation_engine':'core35',
                 'transfer_time_basis':'record_time','icu_departments':['ICU'],'ocr':{},'llm':{'enabled':False}}
            def ocr(*args,**kwargs):
                stop_worker(store,bid)
                kwargs['progress'](1,1,{'failed':0})
                return {'failed':0}
            with patch('medocr.batch.process_pages',side_effect=ocr):
                result=run_batch(store,bid,cfg)
            self.assertTrue(result['stopped'])
            self.assertEqual(result['failed'],0)
            self.assertEqual(store.all('SELECT * FROM jobs')[0]['stage'],'pending')
            with patch('medocr.batch.process_pages',return_value={'failed':0}),patch('medocr.batch.extract_case',return_value={'failed_pages':0}):
                self.assertEqual(run_batch(store,bid,cfg)['complete'],1)
            store.close()
