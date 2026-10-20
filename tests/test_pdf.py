from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from medocr.pipeline import import_pdf, normalize_result, process_pages
from medocr.store import Store


class PDFTests(unittest.TestCase):
    def setUp(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest('需基础依赖 pypdf')
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'data')
        self.bid = self.store.batch('synthetic')
        self.path = self.root/'synthetic.pdf'
        writer = PdfWriter()
        writer.add_blank_page(595,842)
        writer.add_blank_page(595,842)
        with self.path.open('wb') as stream:
            writer.write(stream)
        self.config = {'root':str(self.root),'work_dir':str(self.root/'work'),'ocr':{'dpi':250,'index_dpi':180}}

    def tearDown(self):
        if hasattr(self,'store'):
            self.store.close()
            self.tmp.cleanup()

    def test_import_dedup_and_episode(self):
        a = import_pdf(self.store,self.bid,self.path,'admission-A')
        b = import_pdf(self.store,self.bid,self.path,'admission-A')
        self.assertEqual(a['case_id'],b['case_id'])
        self.assertTrue(b['duplicate'])
        self.assertEqual(len(self.store.pages(a['case_id'])),2)
        self.assertEqual(len(self.store.cases(self.bid)),1)

    def test_ocr_cache_failure_and_recovery(self):
        imported = import_pdf(self.store,self.bid,self.path)
        class FakeOCR:
            def __init__(self,*args): pass
            def predict(self,path): return '入院时间：2025-04-12 14:50',{}
        with patch('medocr.pipeline.OCREngine',FakeOCR),patch('medocr.pipeline.render_page',return_value=b'synthetic image'):
            result = process_pages(self.store,imported['case_id'],self.config)
            self.assertEqual(result['processed'],2)
            result = process_pages(self.store,imported['case_id'],self.config)
            self.assertEqual(result['cached'],2)
            with self.assertRaises(ValueError):
                process_pages(self.store,imported['case_id'],self.config,selected={99})
        class BrokenOCR(FakeOCR):
            def predict(self,path): raise ValueError('Synthetic failure')
        with patch('medocr.pipeline.OCREngine',BrokenOCR),patch('medocr.pipeline.render_page',return_value=b'synthetic image'):
            result = process_pages(self.store,imported['case_id'],self.config,force=True)
            self.assertEqual(result['failed'],2)
        with patch('medocr.pipeline.OCREngine',FakeOCR),patch('medocr.pipeline.render_page',return_value=b'synthetic image'):
            self.assertEqual(process_pages(self.store,imported['case_id'],self.config)['processed'],2)

    def test_paddle_result_adapter(self):
        class General:
            json = {'res':{'rec_texts':['入院记录','2025-04-12 14:50'],'rec_scores':[0.98,0.99]}}
        text,payload = normalize_result(General())
        self.assertIn('2025-04-12',text)
        self.assertEqual(len(payload['cells']),2)
        class Structured:
            json = {'res':{'parsing_res_list':[{'block_content':'<table><tr><td>医嘱</td></tr></table>'}]}}
        self.assertIn('医嘱',normalize_result(Structured())[0])


if __name__=='__main__':
    unittest.main()
