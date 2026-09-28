import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from agentplat import web_policy
from agentplat.sources import SourceStore
from agentplat.workspaces import WorkspaceManager
from agentplat.pages_workspaces import mutate, browse


class ProjectWebSettings(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy_patch = patch.object(web_policy, 'POLICY_PATH', self.root / 'policy.json')
        self.policy_patch.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.policy_patch.stop)

    def test_modes_preserve_list_and_normalize_urls(self):
        web_policy.change('add', 'https://Example.com/news?a=1，www.example.org')
        self.assertEqual(web_policy.policy()['domains'], ['example.com', 'www.example.org'])
        web_policy.change('mode', 'public')
        self.assertTrue(web_policy.allowed('other.example'))
        web_policy.change('remove', 'example.com')
        self.assertEqual(web_policy.policy()['mode'], 'public')
        web_policy.change('mode', 'off')
        self.assertFalse(web_policy.allowed('www.example.org'))
        web_policy.change('mode', 'allowlist')
        self.assertTrue(web_policy.allowed('www.example.org'))
        self.assertFalse(web_policy.allowed('example.org'))
        for value in ['https://user:pass@example.com', 'http://127.0.0.1', 'file:///tmp/file']:
            with self.assertRaises(ValueError): web_policy.parse_sites(value)

    def test_public_mode_still_blocks_private_addresses(self):
        store = SourceStore(self.root)
        store.policy_provider = web_policy.policy
        web_policy.change('mode', 'public')
        for address in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1']:
            with patch('agentplat.sources.socket.getaddrinfo', return_value=[(2, 1, 6, '', (address, 80))]), patch('agentplat.sources.socket.create_connection') as connect:
                with self.assertRaises(PermissionError): store.fetch('http://example.com')
                connect.assert_not_called()
        web_policy.change('mode', 'off')
        with patch('agentplat.sources.socket.getaddrinfo') as dns:
            with self.assertRaises(PermissionError): store.fetch('http://example.com')
            dns.assert_not_called()

    def test_redirect_checks_current_policy(self):
        store = SourceStore(self.root)
        states = iter([{'mode':'public','domains':[]}, {'mode':'off','domains':[]}])
        store.policy_provider = lambda: next(states)
        with patch('agentplat.sources.socket.getaddrinfo', return_value=[(2,1,6,'',('93.184.216.34',80))]), patch('agentplat.sources.socket.create_connection'), patch('agentplat.sources.http.client.HTTPConnection') as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 302
            response.getheader.return_value = 'http://other.example/page'
            with self.assertRaises(PermissionError): store.fetch('http://example.com')
            self.assertEqual(connection.call_count, 1)

    def test_multiple_paths_and_preserved_aliases(self):
        folders = [self.root / 'app', self.root / '文档']
        for folder in folders: folder.mkdir()
        demo = SimpleNamespace(ws_mgr=WorkspaceManager(state_path=self.root/'groups.json'), permissions_token='test')
        form = {'token':'test', 'folders_json':json.dumps([str(p) for p in folders])}
        key = mutate(demo, '/workspaces/save', form)
        saved = demo.ws_mgr.groups[key]
        self.assertEqual(saved['name'], 'app')
        self.assertEqual(len(saved['folders']), 2)
        mutate(demo, '/workspaces/save', {**form, 'id':key, 'name':'Renamed'})
        self.assertEqual(demo.ws_mgr.groups[key]['folders'], saved['folders'])
        self.assertTrue(browse(demo, {'token':'test','path':str(folders[0])})['selectable'])
        with self.assertRaises(PermissionError): browse(demo, {'token':'wrong'})
        with self.assertRaises(ValueError): mutate(demo, '/workspaces/save', {**form,'folders_json':'[]'})


if __name__ == '__main__': unittest.main()
