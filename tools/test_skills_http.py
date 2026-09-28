"""Exercise the real host routes with isolated installation storage."""
from pathlib import Path
import sys, tempfile, threading, unittest, urllib.request, urllib.parse, urllib.error
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.demo import make_handler
from agentplat import plugins, skill_import


class SkillHTTPTests(unittest.TestCase):
    def test_import_disable_and_csrf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root/'example'; source.mkdir()
            (source/'SKILL.md').write_text('---\nname: http-fixture\n---\nRead first.', encoding='utf-8')
            demo = SimpleNamespace(permissions_token='test-only-token')
            with patch.object(plugins, 'CONFIG', root/'plugins.json'), patch.object(skill_import, 'ROOT', root/'installed'):
                server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(demo))
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                base = f'http://127.0.0.1:{server.server_port}'
                opener = urllib.request.build_opener()
                opener.addheaders = [('Cookie', 'agentlab_access=test-only-token')]
                def post(route, **fields):
                    return opener.open(base+route, urllib.parse.urlencode(fields).encode()).read().decode()
                try:
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        post('/skills/import', path=str(source), token='wrong')
                    self.assertEqual(denied.exception.code, 403)
                    html = post('/skills/import', path=str(source), token=demo.permissions_token)
                    self.assertIn('http-fixture', html)
                    name = skill_import.list_skills()[0]['name']
                    post('/skills/toggle', name=name, action='disable', token=demo.permissions_token)
                    self.assertFalse(skill_import.list_skills()[0]['enabled'])
                    self.assertIn('技能管理', opener.open(base+'/skills').read().decode())
                finally:
                    server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__': unittest.main(verbosity=2)
