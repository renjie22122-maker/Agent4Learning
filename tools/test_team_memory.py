from pathlib import Path
import sys, tempfile, threading, time, json, unittest
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.subagents import AgentManager,TERMINAL
from agentplat.llmconfig import LLMConfig
from agentplat.workspace import Workspace
from agentplat.experiments import ScriptedModel
from agentplat.memory import MemoryStore


def done(manager,key):
    state=manager.get(key);deadline=time.monotonic()+12
    while state['status'] not in TERMINAL and time.monotonic()<deadline:
        state=manager.wait(key,.2,state['revision'])
    return state


class TeamMemoryTests(unittest.TestCase):
    def test_nested_copy_merge_and_recursive_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);ws=Workspace(root/'ws');ws.execution_mode='local'
            (ws.root/'value.txt').write_text('root')
            entered=threading.Event();release=threading.Event()
            class Parent(ScriptedModel):
                def complete_with_tools(inner,*args):
                    entered.set();release.wait(8);return super().complete_with_tools(*args)
            clients=iter([Parent(),ScriptedModel([[('write_file',{'path':'value.txt','content':'leaf'})],
                [('run_shell',{'command':'python -c "print(123)"'})],[('finish',{'summary':'done'})]]),Parent()])
            manager=AgentManager(LLMConfig(max_tokens=128),ws,root/'team',factory=lambda:next(clients),max_workers=2)
            try:
                parent=manager.spawn('parent',mode='isolated');self.assertTrue(entered.wait(3))
                child=manager.spawn('child',parent_id=parent,mode='isolated')
                self.assertEqual(done(manager,child)['status'],'completed')
                manager.apply(child)
                self.assertEqual((manager.tasks[parent]['agent'].ws.root/'value.txt').read_text(),'leaf')
                self.assertEqual((ws.root/'value.txt').read_text(),'root')
                active=manager.spawn('active leaf',parent_id=parent)
                with self.assertRaises(RuntimeError):manager.spawn('too deep',parent_id=active)
                manager.cancel(parent)
                self.assertTrue(manager.tasks[active]['cancel'].is_set())
                release.set();done(manager,parent);done(manager,active)
            finally:release.set();manager.close();manager.pool.shutdown(wait=True)

    def test_shared_budget_serial_reservations_and_restart_accounting(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            manager=AgentManager(LLMConfig(max_tokens=128),Workspace(root/'ws'),root/'team',factory=ScriptedModel,total_tokens=1)
            try:
                keys=[manager.spawn(str(i)) for i in range(3)]
                self.assertTrue(all(done(manager,k)['status']=='budget_exceeded' for k in keys))
                self.assertEqual(manager.budget.spent,0)
                self.assertFalse(manager.budget.reservations)
            finally:manager.close();manager.pool.shutdown(wait=True)

    def test_grandchild_with_one_model_slot(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);cfg=LLMConfig(max_tokens=128)
            class Model(ScriptedModel):
                def complete_with_tools(inner,model,messages,tools,timeout):
                    task=next(m.content for m in messages if m.role=='user' and m.content.startswith('任务：'))
                    if 'branch-task' in task:
                        leaves=[v['data'] for v in manager.tasks.values() if v['data'].get('parent_id')]
                        if not leaves: batch=[('spawn_agent',{'task':'leaf-task'})]
                        elif leaves[0]['status'] not in TERMINAL:
                            batch=[('wait_agent',{'agent_id':leaves[0]['agent_id'],'timeout_s':1,'after_revision':leaves[0]['revision']})]
                        else:batch=[('finish',{'summary':'branch complete'})]
                        inner.script=[batch]
                    return super().complete_with_tools(model,messages,tools,timeout)
            manager=AgentManager(cfg,Workspace(root/'ws'),root/'tasks',factory=Model,max_workers=1)
            try:
                key=manager.spawn('branch-task')
                self.assertEqual(done(manager,key)['status'],'completed')
                leaf=next(v for v in manager.tasks.values() if v['data'].get('parent_id'))
                self.assertEqual(leaf['data']['status'],'completed')
                self.assertEqual(leaf['data']['depth'],2)
                self.assertNotIn('spawn_agent',leaf['agent'].tools)
                self.assertEqual(manager.budget.spent,sum(v['data']['used_tokens'] for v in manager.tasks.values()))
                self.assertFalse(manager.budget.reservations)
            finally:manager.close();manager.pool.shutdown(wait=True)

    def test_peer_messages_permissions_and_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);release=threading.Event();entered=threading.Event()
            class Model(ScriptedModel):
                def complete_with_tools(inner,*args):
                    entered.set();release.wait(5);return super().complete_with_tools(*args)
            manager=AgentManager(LLMConfig(max_tokens=128),Workspace(root/'ws'),root/'tasks',factory=Model)
            try:
                a=manager.spawn('A');b=manager.spawn('B');self.assertTrue(entered.wait(3))
                while manager.tasks[a]['agent'] is None or manager.tasks[b]['agent'] is None:time.sleep(.01)
                agent=manager.tasks[a]['agent']
                response=json.loads(agent.tools['send_agent_message'].fn(agent_id=b,message='peer marker'))
                self.assertTrue(response['queued'])
                self.assertIn(a,manager.get(b)['messages'][0])
                with self.assertRaises(RuntimeError):manager.spawn('bad',parent_id=a,mode='isolated')
                with self.assertRaises(ValueError):agent.tools['cancel_agent'].fn(agent_id=b)
                manager.team_state('plan','first',0,a)
                with self.assertRaises(RuntimeError):manager.team_state('plan','lost update',0,b)
                self.assertEqual(manager.team_state('plan')['value'],'first')
                release.set();done(manager,a);done(manager,b)
            finally:release.set();manager.close();manager.pool.shutdown(wait=True)

    def test_memory_selection_scope_expiry_delete_and_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);store=MemoryStore(root/'db');source=root/'s.jsonl'
            events=[{'seq':1,'kind':'session/created','data':{'task':'以后默认 pytest，api_key=sk-abcdefghijklmnop'}},
                    {'seq':2,'kind':'assistant/message','data':{'text':'所有代码完美无缺'}},
                    {'seq':3,'kind':'verification/evidence','data':{'command':'python check.py','exit_code':0,'digest':'abc'}},
                    {'seq':4,'kind':'session/closed','data':{'finished':True}}]
            source.write_text('\n'.join(map(json.dumps,events)),encoding='utf-8')
            self.assertFalse(store.search(root,'pytest'))
            self.assertEqual(store.select_source(source,root),2)
            self.assertFalse(store.search(root,'pytest'))
            row=next(r for r in store.list() if r['kind']=='preference')
            self.assertNotIn('abcdefghijklmnop',row['content'])
            store.update(row['id'],row['content'],'preference','project','active',None,1)
            self.assertTrue(store.search(root,'pytest'))
            self.assertFalse(store.search(root/'other','pytest'))
            store.update(row['id'],'偏好 pytest','preference','user','active',None,2)
            self.assertTrue(store.search(root/'other','pytest'))
            store.update(row['id'],'偏好 pytest','preference','user','active',time.time()-1,3)
            self.assertFalse(store.search(root,'pytest'))
            store.delete(row['id']);store.refresh(source)
            self.assertEqual(next(r for r in store.list() if r['id']==row['id'])['content'],'')
            store.revoke(store.sources()[0]['id'])
            self.assertFalse(store.search(root,'pytest'))


if __name__=='__main__':unittest.main(verbosity=2)
