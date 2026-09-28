"""Portable tests for sandbox selection, broker grants, and iteration semantics."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class SandboxContractTests(unittest.TestCase):
    def test_domain_policy_reload_and_revoke(self):
        from agentplat import web_policy
        from agentplat.sources import SourceStore
        with tempfile.TemporaryDirectory() as td, patch.object(web_policy, 'POLICY_PATH', Path(td) / 'policy.json'):
            web_policy.save_domains('www.example.org, EXAMPLE.org')
            self.assertEqual(web_policy.domains(), ['example.org', 'www.example.org'])
            store = SourceStore(td)
            store.domain_provider = web_policy.domains
            with self.assertRaises(PermissionError):
                store.fetch('https://unauthorized.example.org')
            web_policy.save_domains('')
            with self.assertRaises(PermissionError):
                store.fetch('https://www.example.org')
            for bad in ('https://example.org', '*.example.org', '127.0.0.1', 'example.org:443', 'a..org'):
                with self.assertRaises(ValueError):
                    web_policy.save_domains(bad)

    def test_permission_form_checks_token_and_origin(self):
        import threading
        import urllib.request
        import urllib.error
        from http.server import ThreadingHTTPServer
        from types import SimpleNamespace
        from agentplat.demo import make_handler
        from agentplat import web_policy
        with tempfile.TemporaryDirectory() as td, patch.object(web_policy, 'POLICY_PATH', Path(td) / 'policy.json'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(SimpleNamespace(permissions_token='fixture-token')))
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                for route in ('/api/runtime', '/permissions', '/knowledge'):
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        urllib.request.urlopen(base + route, timeout=3)
                    self.assertEqual(denied.exception.code, 401)
                for data, origin in [(b'domains=example.org', base),
                                     (b'token=fixture-token&domains=example.org', 'https://untrusted.example')]:
                    request = urllib.request.Request(base + '/permissions/web', data=data, headers={'Origin': origin, 'Cookie': 'agentlab_access=fixture-token'})
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(request, timeout=3)
                    self.assertEqual(error.exception.code, 403)
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *args): return None
                request = urllib.request.Request(base + '/permissions/web', data=b'token=fixture-token&domains=example.org', headers={'Origin': base, 'Cookie': 'agentlab_access=fixture-token'})
                with self.assertRaises(urllib.error.HTTPError) as response:
                    urllib.request.build_opener(NoRedirect).open(request, timeout=3)
                self.assertIn(response.exception.code, (302, 303))
                self.assertEqual(web_policy.domains(), ['example.org'])
            finally:
                server.shutdown(); server.server_close(); worker.join(3)

    def test_clean_environment_drops_credentials(self):
        from agentplat.windows_sandbox import clean_environment
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'UNEXPECTED_SECRET': 'fixture'}):
            env = clean_environment(Path(td), Path(td))
            self.assertNotIn('UNEXPECTED_SECRET', env)
            self.assertIn('SystemRoot', env)

    def test_progress_can_exceed_40_and_80_without_shell(self):
        from agentplat.experiments import ScriptedModel
        from agentplat.llmconfig import LLMConfig
        from agentplat.loop import CodingAgent
        from agentplat.workspace import Workspace
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = Workspace(root / 'workspace')
            for i in range(85):
                (ws.root / f'part{i}.txt').write_text(f'fact {i}')
            script = [[('read_file', {'path': f'part{i}.txt'})] for i in range(85)] + [[('finish', {'summary': '只读检查完成'})]]
            agent = CodingAgent(ScriptedModel(script), LLMConfig(model='fake', provider='mock'),
                                workspace=ws, session_dir=root / 'sessions', enable_subagents=False,
                                compaction_enabled=False)
            result = agent.run('只读检查目录，不运行命令')
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.model_calls, 86)
            self.assertEqual(ws.commands_run, 0)

    def test_explicit_limit_is_exact(self):
        from agentplat.experiments import ScriptedModel
        from agentplat.llmconfig import LLMConfig
        from agentplat.loop import CodingAgent
        from agentplat.workspace import Workspace
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent = CodingAgent(ScriptedModel([[('list_dir', {})]] * 10), LLMConfig(model='fake', provider='mock'),
                                workspace=Workspace(root / 'workspace'), session_dir=root / 'sessions',
                                hard_iterations=3, enable_subagents=False)
            result = agent.run('检查目录')
            self.assertEqual(result.model_calls, 3)
            self.assertEqual(result.stopped_by, 'hard_limit')


if __name__ == '__main__':
    unittest.main(verbosity=2)
