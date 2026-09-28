import os,tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
from agentplat.benchmark import grade


class CleanBenchmarkTests(unittest.TestCase):
    def test_rag_grader_requires_current_real_citation(self):
        from agentplat.knowledge import KnowledgeBase,database_root
        with tempfile.TemporaryDirectory() as td,patch.dict(os.environ,{'AGENTLAB_KB_DIR':td}):
            root=Path(td)/'ws';root.mkdir();doc=Path(td)/'current.txt';doc.write_text('住宿每日680元。',encoding='utf-8')
            kb=KnowledgeBase(database_root(root));kb.import_file(doc)
            citation=kb.search('680')['hits'][0]['citation']
            for ref,expected in [('kb:invented',False),(citation,True)]:
                (root/'result.json').write_text(json.dumps({'hotel_limit':680,'currency':'CNY','source':ref}))
                self.assertEqual(grade('rag_policy',root,root/'grader')[0],expected)

    def test_browser_grader_positive_and_negative(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);ws=root/'ws';ws.mkdir()
            for delta,expected in [(2,False),(1,True)]:
                (ws/'index.html').write_text('<span id="count">0</span><button id="inc" onclick="change('+str(delta)+')">加</button><button id="dec" onclick="change(-1)">减</button><button id="reset" onclick="n=0;change(0)">重置</button><script>let n=0;function change(v){n=Math.max(0,n+v);document.querySelector("#count").textContent=n;}</script>',encoding='utf-8')
                passed,details=grade('browser_counter',ws,root/f'grade{delta}')
                self.assertEqual(passed,expected,details)

    def test_memory_directories_are_independent(self):
        from agentplat.memory import MemoryStore
        with tempfile.TemporaryDirectory() as td:
            for name in ['one','two']:
                with patch.dict(os.environ,{'AGENTLAB_MEMORY_DIR':str(Path(td)/name)}):
                    self.assertEqual(MemoryStore().list(),[])
                    self.assertEqual(MemoryStore().root,Path(td)/name)


if __name__=='__main__':unittest.main()
