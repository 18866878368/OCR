import json
import unittest
from unittest.mock import MagicMock, patch

from medocr.facts import request_json


class TransportTests(unittest.TestCase):
    def config(self, url='http://127.0.0.1:11434/v1'):
        return {'llm':{'enabled':True,'protocol':'ollama','base_url':url,'model':'synthetic'}}

    def opener(self, payload):
        opener=MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value=json.dumps(payload).encode()
        return opener

    def test_native_ollama_json_and_nonthinking_request(self):
        opener=self.opener({'message':{'content':'{"events":[]}'},'done_reason':'stop'})
        with patch('medocr.facts.urllib.request.build_opener',return_value=opener):
            self.assertEqual(request_json(self.config(),'system',{'text':'synthetic'}),{'events':[]})
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'http://127.0.0.1:11434/api/chat')
        body=json.loads(request.data)
        self.assertEqual(body['format'],'json')
        self.assertFalse(body['think'])
        self.assertFalse(body['truncate'])
        self.assertFalse(body['stream'])

    def test_external_endpoint_blocked_before_any_request(self):
        with patch('medocr.facts.urllib.request.build_opener') as opener:
            with self.assertRaises(ValueError):
                request_json(self.config('https://example.com'),'system',{})
            opener.assert_not_called()

    def test_local_endpoint_cannot_select_a_cloud_model(self):
        cfg=self.config(); cfg['llm']['model']='synthetic:cloud'
        with patch('medocr.facts.urllib.request.build_opener') as opener:
            with self.assertRaises(ValueError):
                request_json(cfg,'system',{})
            opener.assert_not_called()

    def test_truncated_json_is_not_accepted_as_complete(self):
        opener=self.opener({'message':{'content':'{"events":[]}'},'done_reason':'length'})
        with patch('medocr.facts.urllib.request.build_opener',return_value=opener),self.assertRaises(ValueError):
            request_json(self.config(),'system',{})
