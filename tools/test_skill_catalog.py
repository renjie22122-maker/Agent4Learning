import sys,json,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.skill_metadata import metadata
from agentplat.plugins import PluginRegistry
class CatalogTests(unittest.TestCase):
 def test_folded_literal_quotes_and_body_boundaries(self):
  self.assertEqual(metadata('---\nname: "demo"\ndescription: >\n  first line\n  second line\n---\ndescription: WRONG')['description'],'first line second line')
  self.assertEqual(metadata('---\ndescription: |-\n  first\n  second\n---')['description'],'first\nsecond')
  self.assertEqual(metadata("---\nname: 'it''s'\n---")['name'],"it's")
 def test_pages_and_ambiguous_short_name(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);paths=[]
   for n in range(22):
    folder=root/str(n);folder.mkdir()
    (folder/'SKILL.md').write_text('---\nname: duplicate\ndescription: >\n  search trigger\n  details\n---',encoding='utf-8')
    p=folder/'manifest.json';p.write_text(json.dumps({'name':'skill-'+str(n),'api_version':1,'skills':['SKILL.md'],'source':'bundle-'+str(n)}));paths.append(p)
   r=PluginRegistry({});r.mount_all(paths)
   first=json.loads(r.list_page());self.assertEqual(len(first['skills']),8);self.assertEqual(first['next_offset'],8)
   self.assertLess(len(r.list_page().encode()),8000)
   second=json.loads(r.list_page(offset=8));self.assertFalse({x['name'] for x in first['skills']} & {x['name'] for x in second['skills']})
   self.assertEqual(json.loads(r.list_page(query='absent'))['matched'],0)
   with self.assertRaisesRegex(ValueError,'多个版本'):r.read_skill('duplicate')
   self.assertIn('search trigger',r.read_skill('skill-1'))
if __name__=='__main__':unittest.main()
