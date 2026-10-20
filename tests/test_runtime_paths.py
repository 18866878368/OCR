import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from medocr.pipeline import OCREngine
from medocr.runtime_paths import native_model_dir


class RuntimePathTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows native path compatibility')
    def test_unicode_model_cache_reuse_and_weight_update(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)/'中文模型'
            source.mkdir()
            for name in ('inference.yml', 'inference.json', 'inference.pdiparams'):
                (source/name).write_bytes(b'model')
            with patch('medocr.runtime_paths.tempfile.gettempdir', return_value=temp):
                cached = Path(native_model_dir(source))
                self.assertTrue(str(cached).isascii())
                self.assertEqual((cached/'inference.pdiparams').read_bytes(), b'model')
                before = (cached/'inference.pdiparams').stat().st_mtime_ns
                self.assertEqual(native_model_dir(source), str(cached))
                self.assertEqual((cached/'inference.pdiparams').stat().st_mtime_ns, before)
                (source/'inference.pdiparams').write_bytes(b'new model weights')
                updated = Path(native_model_dir(source))
                self.assertNotEqual(updated, cached)
                self.assertEqual((updated/'inference.pdiparams').read_bytes(), b'new model weights')

    @unittest.skipUnless(os.name == 'nt', 'Windows native path compatibility')
    def test_incomplete_unicode_model_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)/'不完整模型'
            source.mkdir()
            (source/'inference.yml').write_bytes(b'model')
            with self.assertRaises(RuntimeError):
                native_model_dir(source)

    def test_unicode_image_is_decoded_as_bgr(self):
        try:
            from PIL import Image
            import numpy as np
        except ImportError:
            self.skipTest('OCR image dependencies are required')
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp)/'中文页图.png'
            Image.new('RGB', (2, 2), (255, 0, 0)).save(image)
            class Runner:
                def predict(self, data):
                    self_test.assertIsInstance(data, np.ndarray)
                    self_test.assertEqual(data[0, 0].tolist(), [0, 0, 255])
                    return []
            self_test = self
            engine = OCREngine.__new__(OCREngine)
            engine.pipeline = Runner()
            self.assertEqual(engine.predict(image), ('', {'results': []}))
