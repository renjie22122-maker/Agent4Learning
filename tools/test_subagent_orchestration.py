"""Product orchestration tests using disposable workspaces and controlled models."""
from pathlib import Path
import sys, tempfile, threading, time, unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.subagents import AgentManager, TERMINAL
from agentplat.experiments import ScriptedModel
from agentplat.llmconfig import LLMConfig
from agentplat.workspace import Workspace


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.ws=Workspace(self.root/'workspace');self.ws.execution_mode='local'
        self.cfg=LLMConfig(max_tokens=128)

    def manager(self,factory):
        manager=AgentManager(self.cfg,self.ws,self.root/'children',factory=factory,max_workers=2)
        self.addCleanup(manager.close);return manager

    def done(self,manager,key):
        until=time.monotonic()+12;state=manager.get(key)
        while state['status'] not in TERMINAL and time.monotonic()<until:
            state=manager.wait(key,1,state['revision'])
        self.assertIn(state['status'],TERMINAL,state)
        return state

    def test_dependency_order_and_live_message_delivery(self):
        entered=threading.Event();release=threading.Event();seen=[];clients=[]
        class Model(ScriptedModel):
            def complete_with_tools(inner,model,messages,tools,timeout):
                seen.append([m.to_api() for m in messages])
                if inner is clients[0] and inner.turn==0:
                    entered.set();release.wait(5)
                return super().complete_with_tools(model,messages,tools,timeout)
        def factory():
            model=Model([[('finish',{'summary':'upstream-marker'})]])
            clients.append(model);return model
        manager=self.manager(factory)
        a=manager.spawn('Inspect A');self.assertTrue(entered.wait(3))
        b=manager.spawn('Inspect B',depends_on=[a])
        self.assertEqual(len(clients),1)
        manager.send(a,'steering-marker-731')
        release.set()
        self.assertEqual(self.done(manager,a)['status'],'completed')
        self.assertEqual(self.done(manager,b)['status'],'completed')
        self.assertTrue(any('steering-marker-731' in str(m) for m in seen[1:]))
        self.assertIn('upstream-marker',manager.get(b)['context'])
        self.assertIn('spawn_agent',manager.tasks[a]['agent'].tools)
        self.assertNotIn('write_file',manager.tasks[a]['agent'].tools)

    def test_failed_dependency_blocks_and_retry_is_new_attempt(self):
        class Failure:
            def complete_with_tools(self,*args):raise RuntimeError('fixture failure')
        clients=iter([Failure(),ScriptedModel()]);manager=self.manager(lambda:next(clients))
        a=manager.spawn('fail once');self.assertEqual(self.done(manager,a)['status'],'failed')
        b=manager.spawn('blocked successor',depends_on=[a]);self.assertEqual(self.done(manager,b)['status'],'blocked')
        retry=manager.retry(a,'try again');self.assertNotEqual(retry,a)
        self.assertEqual(self.done(manager,retry)['status'],'completed')
        self.assertEqual(manager.get(a)['status'],'failed')

    def test_isolated_review_merge_and_parent_conflict(self):
        script=[[('write_file',{'path':'artifact.txt','content':'child-result'})],
                [('run_shell',{'command':"python -c \"from pathlib import Path; assert Path('artifact.txt').read_text()=='child-result'\""})],
                [('finish',{'summary':'artifact.txt written and verified'})]]
        manager=self.manager(lambda:ScriptedModel(script))
        key=manager.spawn('Produce artifact',mode='isolated',token_budget=50000)
        self.assertEqual(self.done(manager,key)['status'],'completed')
        self.assertFalse((self.ws.root/'artifact.txt').exists())
        self.assertIn('child-result',manager.review(key)['diff'])
        self.assertEqual(manager.apply(key)['applied'],['artifact.txt'])
        self.assertEqual((self.ws.root/'artifact.txt').read_text(),'child-result')
        second=manager.spawn('Produce artifact again',mode='isolated',token_budget=50000)
        self.assertEqual(self.done(manager,second)['status'],'completed')
        # Add a child-side change, then make a conflicting parent change.
        branch=manager.tasks[second]['branch'];(branch.root/'artifact.txt').write_text('child-second')
        (self.ws.root/'artifact.txt').write_text('parent-new')
        with self.assertRaises(RuntimeError):manager.apply(second)
        self.assertEqual((self.ws.root/'artifact.txt').read_text(),'parent-new')


if __name__=='__main__':unittest.main(verbosity=2)
