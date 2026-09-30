import json,tempfile,time,unittest
from pathlib import Path
from agentplat.memory import MemoryStore


class MemoryRelationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=MemoryStore(self.root/'db')
        source=self.root/'s.jsonl'
        source.write_text('\n'.join(json.dumps(dict(seq=i,kind='followup/user',data=dict(text=text)))
            for i,text in enumerate(['默认中文回复','默认英文回复'],1)),encoding='utf-8')
        self.store.select_source(source,self.root)
        self.ids=[]
        for row in self.store.list():
            self.store.update(row['id'],row['content'],row['kind'],'project','active',None,1)
            self.ids.append(row['id'])

    def test_conflict_included_even_when_other_not_in_top_k(self):
        a,b=self.ids;self.store.relate(a,b,'conflicts',2,2)
        hits=self.store.search(self.root,'中文回复',1)
        self.assertEqual(len(hits),1);self.assertEqual(len(hits[0]['conflicts']),1)
        self.assertEqual(hits[0]['confidence_basis'],'user_confirmed_statement')
        self.assertFalse(self.store.search(self.root/'other','回复'))

    def test_superseded_fact_does_not_resurrect(self):
        a,b=self.ids;self.store.relate(a,b,'supersedes',2,2)
        row=next(r for r in self.store.list() if r['id']==a)
        self.store.update(a,row['content'],row['kind'],'project','active',time.time()-1,2)
        self.assertFalse(self.store.search(self.root,'回复'))

    def test_future_and_stale_revision(self):
        a,b=self.ids
        self.store.relate(a,b,'conflicts',2,2)
        row=next(r for r in self.store.list() if r['id']==a)
        self.store.update(a,row['content'],row['kind'],'project','active',None,2,time.time()+3600)
        self.assertNotIn(a,[r['id'] for r in self.store.search(self.root,'回复')])
        with self.assertRaises(ValueError):self.store.relate(a,b,'supersedes',2,2)


if __name__=='__main__':unittest.main()
