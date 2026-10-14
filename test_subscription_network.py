"""Read retry after transient queue outages; never retry ambiguous mutations."""
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch
import subscription_bridge as bridge


class Network(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {'INSIGHT_QUEUE_TOKEN':'offline'}):
            self.queue=bridge.Queue({'queue_repository':'jinhoo-choi/insight-bot-data',
                'queue_branch':'main','queue_prefix':'subscription/insight'})
    def error(self, code):
        return urllib.error.HTTPError('https://example.invalid',code,'offline',{},None)
    def test_get_recovers_after_500_and_503(self):
        with patch.object(bridge.urllib.request,'urlopen',side_effect=[self.error(500),
                self.error(503),io.BytesIO(json.dumps({'private':True}).encode())]) as request, \
             patch.object(bridge.time,'sleep') as sleep:
            self.assertEqual(self.queue.request(''),{'private':True})
            self.assertEqual(request.call_count,3)
            self.assertEqual([x.args[0] for x in sleep.call_args_list],[1,2])
    def test_get_network_failure_is_bounded(self):
        with patch.object(bridge.urllib.request,'urlopen',side_effect=urllib.error.URLError('offline')) as request, \
             patch.object(bridge.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'GET 네트워크 실패'):
                self.queue.request('')
            self.assertEqual(request.call_count,3)
    def test_mutation_failure_is_never_retried(self):
        for method in ['PUT','POST']:
            with self.subTest(method=method), patch.object(bridge.urllib.request,'urlopen',
                    side_effect=self.error(500)) as request, patch.object(bridge.time,'sleep') as sleep:
                with self.assertRaisesRegex(RuntimeError,method+' 실패 HTTP 500'):
                    self.queue.request('',method,{})
                self.assertEqual(request.call_count,1); sleep.assert_not_called()
    def test_missing_file_and_auth_failure_fail_without_other_identity(self):
        with patch.object(bridge.urllib.request,'urlopen',side_effect=self.error(404)) as request:
            self.assertIsNone(self.queue.request('')); self.assertEqual(request.call_count,1)
        with patch.object(bridge.urllib.request,'urlopen',side_effect=self.error(403)) as request:
            with self.assertRaisesRegex(RuntimeError,'HTTP 403'):self.queue.request('')
            self.assertEqual(request.call_count,1)

if __name__=='__main__':unittest.main()
