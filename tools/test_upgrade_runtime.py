import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np

from agentplat.knowledge import KnowledgeBase
from agentplat import vector_knowledge as vectors,ann_index,semantic_memory
from agentplat.memory import MemoryStore
from tools.test_vector_knowledge import FakeEmbedding


class UpgradeTests(unittest.TestCase):
    def test_unresolved_plan_cannot_be_reported_finished(self):
        from types import SimpleNamespace
        from agentplat.review_lifecycle import ReviewLifecycle
        for status in ('running','blocked','interrupted'):
            agent=SimpleNamespace(children=SimpleNamespace(planner=SimpleNamespace(plans={'id':{'status':status}})))
            verdict=ReviewLifecycle._review_finish(agent,{'summary':'done'})
            self.assertFalse(verdict.allow)

    def test_memory_auto_index_runs_only_after_enabled(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=MemoryStore(root/'memory');source=root/'session.jsonl'
            source.write_text(json.dumps({'seq':1,'kind':'session/created','data':{'task':'approved memory'}}))
            store.select_source(source,root);row=store.list()[0]
            called=threading.Event()
            with patch.object(semantic_memory,'build',side_effect=lambda _:called.set()):
                store.update(row['id'],'approved memory','decision','project','active',None,1)
                self.assertFalse(called.is_set())
                semantic_memory.enable_auto(store)
                store.update(row['id'],'revised memory','decision','project','active',None,2)
                self.assertTrue(called.wait(2))
                deadline=time.monotonic()+2
                while str(store.root.resolve()) in semantic_memory._jobs and time.monotonic()<deadline:time.sleep(.01)

    def test_planner_restart_never_redispatches_and_revision_conflicts(self):
        from types import SimpleNamespace
        from agentplat.team_planner import TeamPlanner
        with tempfile.TemporaryDirectory() as tmp:
            manager=SimpleNamespace(directory=Path(tmp),closed=True,max_queue=12,parent_cancel=None)
            planner=TeamPlanner(manager)
            plan=planner.create('goal',[{'id':'one','task':'task','acceptance':'check'}])
            restored=TeamPlanner(manager)
            self.assertEqual(restored.get(plan['id'])['status'],'interrupted')
            with self.assertRaises(ValueError):restored.revise(plan['id'],'one','new','check','reason',-1)
            with self.assertRaises(ValueError):planner.create('goal',[{'id':'one','task':'task','acceptance':'check','depends_on':['missing']}])

    def test_planner_conflicting_merge_keeps_parent(self):
        from types import SimpleNamespace
        from agentplat.team_planner import TeamPlanner
        from agentplat.isolation import IsolatedChanges
        from agentplat.runtime import workspace_digest
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/'work';work.mkdir();(work/'file.txt').write_text('old')
            branch=IsolatedChanges(work,root/'branch');(branch.root/'file.txt').write_text('agent')
            (work/'file.txt').write_text('user concurrent edit')
            manager=SimpleNamespace(directory=root,closed=True,max_queue=12,parent_cancel=None,
                tasks={'author':{'branch':branch}},get=lambda _: {'status':'completed','summary':json.dumps({'verdict':'pass','findings':[],'tests':['check']})})
            planner=TeamPlanner(manager)
            plan=planner.create('goal',[{'id':'one','task':'task','acceptance':'check'}])
            internal=planner.plans[plan['id']];internal['nodes']['one'].update(state='reviewing',agent_id='author',review_id='review',digest=workspace_digest(branch.root))
            planner.tick(internal)
            self.assertEqual(internal['status'],'blocked');self.assertEqual((work/'file.txt').read_text(),'user concurrent edit')

    def test_ann_delta_revoke_replacement_and_checksum(self):
        ann_index.backend() # Required integration dependency; do not fake ANN.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);kb=KnowledgeBase(root/'kb');model=FakeEmbedding()
            doc=root/'a.txt';doc.write_text('猫',encoding='utf-8');kb.import_file(doc)
            vectors.build(kb,embedder=model);meta=ann_index.build(kb,model.key)
            query=np.array([[1,0]],dtype='float32')
            self.assertEqual(len(ann_index.search(kb,model.key,query,5)),1)
            doc2=root/'b.txt';doc2.write_text('喵',encoding='utf-8');kb.import_file(doc2);vectors.build(kb,embedder=model)
            self.assertEqual(len(ann_index.search(kb,model.key,query,5)),2)
            kb.remove(next(d['id'] for d in kb.list_documents() if d['name']=='a.txt'))
            self.assertEqual(len(ann_index.search(kb,model.key,query,5)),1)
            with kb.connect() as db:db.execute('UPDATE vectors SET vector=vector')
            with self.assertRaises(ValueError):ann_index.search(kb,model.key,query,5)
            fresh=ann_index.build(kb,model.key,rebuild=True)
            self.assertNotEqual(meta['generation'],fresh['generation'])
            self.assertEqual(len(ann_index.search(kb,model.key,query,5)),1)
            ann_index._cache.clear()
            (ann_index.directory(kb,model.key)/fresh['generation']/'ids.json').write_text('[]')
            with self.assertRaises(ValueError):ann_index.search(kb,model.key,query,5)
            diagnostics={}
            with patch.object(vectors,'config',return_value={'enabled':True}):
                hits=vectors.search(kb,'喵',embedder=model,diagnostics=diagnostics)
            self.assertTrue(hits);self.assertEqual(diagnostics['backend'],'exact cosine');self.assertTrue(diagnostics['warning'])

    def test_semantic_memory_revision_scope_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=MemoryStore(root/'mem');source=root/'chat.jsonl'
            source.write_text(json.dumps({'seq':1,'kind':'session/created','data':{'task':'猫喜欢睡觉'}}),encoding='utf-8')
            store.select_source(source,root);row=store.list()[0]
            store.update(row['id'],row['content'],'decision','project','active',None,1)
            model=FakeEmbedding();semantic_memory.build(store,model)
            with patch.object(semantic_memory,'config',return_value={'enabled':True}),patch.object(semantic_memory,'LocalEmbedder',return_value=model):
                self.assertTrue(store.search(root,'喵'))
                self.assertFalse(store.search(root/'other','喵'))
                store.update(row['id'],'狗喜欢运动','decision','project','active',None,2)
                self.assertFalse(store.search(root,'喵'))
                semantic_memory.build(store,model)
                self.assertFalse(store.search(root,'喵'))
                store.delete(row['id']);self.assertFalse(store.search(root,'狗'))

    def test_planner_cycle_and_independent_review_merge(self):
        from agentplat.team_planner import TeamPlanner
        from agentplat.subagents import AgentManager
        from agentplat.workspace import Workspace
        from agentplat.llmconfig import LLMConfig
        from agentplat.experiments import ScriptedModel
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);ws=Workspace(root/'ws');ws.execution_mode='local'
            class Client(ScriptedModel):
                def complete_with_tools(self,model,messages,tools,timeout):
                    reviewer=any(t['function']['name']=='verification_environment' for t in tools)
                    if reviewer:
                        self.script=[[('run_shell',{'command':'python -c "from pathlib import Path; assert Path(\'result.txt\').read_text()==\'42\'"'})],
                                     [('finish',{'summary':json.dumps({'verdict':'pass','findings':[],'tests':['read and assert'],'reason':''})})]]
                    else:self.script=[[('write_file',{'path':'result.txt','content':'42'})],
                                      [('run_shell',{'command':'python -c "from pathlib import Path; assert Path(\'result.txt\').read_text()==\'42\'"'})],
                                      [('finish',{'summary':'result.txt contains 42, checked'})]]
                    return super().complete_with_tools(model,messages,tools,timeout)
            manager=AgentManager(LLMConfig(max_tokens=128),ws,root/'children',factory=Client)
            planner=TeamPlanner(manager);manager.planner=planner
            try:
                with self.assertRaises(ValueError):planner.create('cycle',[{'id':'a','task':'a','acceptance':'a','depends_on':['b']},{'id':'b','task':'b','acceptance':'b','depends_on':['a']}])
                plan=planner.create('写入数字',[{'id':'a','task':'创建 result.txt，内容 42','acceptance':'文件内容严格等于 42'}])
                state=planner.wait(plan['id'],25)
                self.assertEqual(state['status'],'ready_for_final_review',state)
                self.assertEqual(state['nodes']['a']['state'],'merged')
                self.assertEqual((ws.root/'result.txt').read_text(),'42')
                self.assertEqual(state['nodes']['a']['review']['verdict'],'pass')
            finally:planner.closed=True;manager.close();manager.pool.shutdown(wait=True)


if __name__=='__main__':unittest.main()
