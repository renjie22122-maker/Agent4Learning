from pathlib import Path
import sys
import tempfile
import json
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.plugins import PluginRegistry


class PluginTests(unittest.TestCase):
    def test_hot_reload_references_and_revocation(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from agentplat import skill_import, plugins
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root/'example'; source.mkdir()
            (source/'SKILL.md').write_text('---\nname: example\ndescription: Review local code\n---\nRead reference.txt.', encoding='utf-8')
            (source/'reference.txt').write_text('Evidence first', encoding='utf-8')
            with patch.object(skill_import, 'ROOT', root/'installed'), patch.object(plugins, 'CONFIG', root/'plugins.json'):
                agent = SimpleNamespace(tools={}, capabilities=SimpleNamespace(allowed_tools=None))
                plugins.install(agent)
                result = skill_import.import_skill(source)
                plugins.refresh(agent)
                catalog = json.loads(agent.tools['list_skills'].fn())['skills']
                self.assertEqual(catalog[0]['description'], 'Review local code')
                name = catalog[0]['name']
                self.assertIn('Evidence first', agent.tools['read_skill_file'].fn(name, 'reference.txt'))
                with self.assertRaises(ValueError): agent.tools['read_skill_file'].fn(name, '../../outside.txt')
                skill_import.set_enabled(result['imported'][0], False)
                plugins.refresh(agent)
                self.assertEqual(json.loads(agent.tools['list_skills'].fn())['skills'], [])
                with self.assertRaises(KeyError): agent.tools['read_skill'].fn(name)

    def test_skill_import_and_zip_traversal(self):
        from unittest.mock import patch
        import zipfile
        from agentplat import skill_import, plugins
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root/'source'; source.mkdir()
            (source/'SKILL.md').write_text('---\nname: review-code\n---\nRead first.', encoding='utf-8')
            (source/'reference.txt').write_text('fixture')
            with patch.object(skill_import, 'ROOT', root/'installed'), patch.object(plugins, 'CONFIG', root/'plugins.json'):
                result = skill_import.import_skill(source)
                self.assertFalse(result['scripts_executed'])
                self.assertTrue(skill_import.list_skills()[0]['enabled'])
                skill_import.set_enabled(result['imported'][0], False)
                self.assertFalse(skill_import.list_skills()[0]['enabled'])
                bad = root/'bad.zip'
                with zipfile.ZipFile(bad,'w') as archive: archive.writestr('../escape.txt','no')
                with self.assertRaises(ValueError): skill_import.import_skill(bad)
                self.assertFalse((root/'escape.txt').exists())

    def test_dependency_rollback_and_skill_scope(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root/'SKILL.md').write_text('Read files before editing.')
            a = root/'a.json'; a.write_text(json.dumps({'name':'a','api_version':1,'skills':['SKILL.md']}))
            b = root/'b.json'; b.write_text(json.dumps({'name':'b','api_version':1,'requires':['a']}))
            registry = PluginRegistry({}); registry.mount_all([b,a])
            self.assertIn('Read files', registry.read_skill('a/SKILL.md'))
            with self.assertRaises(ValueError): registry.unmount('a')
            registry.unmount('b'); registry.unmount('a')
            b.write_text(json.dumps({'name':'b','api_version':1,'requires':['missing']}))
            with self.assertRaises(ValueError): registry.mount_all([a,b])
            self.assertFalse(registry.loaded)


if __name__ == '__main__': unittest.main(verbosity=2)
