"""Exercise recovery over real HTTP, including UTF-8 response framing."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from agentplat.demo import make_handler


class RecoveryHTTPTests(unittest.TestCase):
    def request(self, reports, renderer=None):
        demo = SimpleNamespace(permissions_token='fixture', recovery_reports=reports)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(demo))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            conn.request('GET', '/recovery', headers={'Cookie': 'agentlab_access=fixture'})
            response = conn.getresponse()
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(int(response.getheader('Content-Length')), len(body))
            self.assertNotIn(b'HTTP/1.1', body)
            return body.decode('utf-8')
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_empty_recovery_page(self):
        self.assertIn('本次启动没有需要恢复的任务', self.request([]))

    def test_attention_page_escapes_log_data(self):
        body = self.request([dict(session_id='fixture', status='needs_attention',
                                  reason='<script>坏</script>')])
        self.assertIn('需要核对', body)
        self.assertIn('&lt;script&gt;', body)
        self.assertNotIn('<script>坏', body)

    def test_string_renderer_is_encoded_before_headers(self):
        with patch('agentplat.recovery.page', return_value='恢复页面：中文'):
            self.assertEqual(self.request([]), '恢复页面：中文')


if __name__ == '__main__':
    unittest.main()
