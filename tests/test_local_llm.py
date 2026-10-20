import json
import unittest
from unittest.mock import patch
from medocr.extract import llm_events, extract_case

PAGE = {'case_id':'c','document_id':'d','page':1,'category':'admission','text':'入院时间：2025-04-12 14:50'}
CONFIG = {'llm':{'enabled':True,'base_url':'http://127.0.0.1:11434/v1','model':'synthetic','max_chars_per_page':12000,'timeout_seconds':1}}


class Response:
    def __init__(self,data): self.data = data
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self,*args):
        return json.dumps({'choices':[{'message':{'content':json.dumps(self.data,ensure_ascii=False)}}]}).encode('utf-8')


def output(event):
    class Opener:
        def open(self,*args,**kwargs): return Response({'events':[event]})
    return Opener()


class LocalLLMTests(unittest.TestCase):
    def test_evidence_supported_event_stays_candidate(self):
        event = {'kind':'admission','event_time':'2025-04-12T14:50','record_time':None,'quote':PAGE['text'],'attributes':{}}
        with patch('medocr.extract.urllib.request.build_opener',return_value=output(event)):
            result = llm_events(PAGE,CONFIG)
        self.assertFalse(result[0]['verified'])
        self.assertEqual(result[0]['page'],1)

    def test_fabricated_quote_is_rejected(self):
        event = {'kind':'admission','event_time':None,'quote':'不存在的原文','attributes':{}}
        with patch('medocr.extract.urllib.request.build_opener',return_value=output(event)):
            with self.assertRaises(ValueError): llm_events(PAGE,CONFIG)

    def test_fabricated_time_is_rejected(self):
        event = {'kind':'admission','event_time':'2025-04-12T16:00','quote':PAGE['text'],'attributes':{}}
        with patch('medocr.extract.urllib.request.build_opener',return_value=output(event)):
            with self.assertRaises(ValueError): llm_events(PAGE,CONFIG)

    def test_missing_environment_not_silently_successful(self):
        with self.assertRaises(ValueError):
            extract_case(None,'c',{'llm':{'enabled':False}},True)


if __name__=='__main__':
    unittest.main()
