"""Review infrastructure failures must never masquerade as code defects."""
from pathlib import Path
import sys, tempfile, unittest, json, time
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat import independent_review
from agentplat.llmconfig import LLMConfig
from agentplat.subagents import AgentManager, TERMINAL
from agentplat.workspace import Workspace
from agentplat.experiments import ScriptedModel


class ReviewBudgetTests(unittest.TestCase):
    def test_incomplete_retry_same_artifact_and_configured_budget(self):
        with tempfile.TemporaryDirectory() as td:
            results={'status':'budget_exceeded','error':'budget','used_tokens':90000}
            spawned=[]
            def spawn(task,**kw): spawned.append(kw);return str(len(spawned))
            manager=SimpleNamespace(spawn=spawn,get=lambda key:results)
            agent=SimpleNamespace(cfg=LLMConfig(),ws=Workspace(Path(td)),
                session=SimpleNamespace(append=lambda *a,**kw:None),child_manager=lambda:manager,
                _task_text='verify',_finish_rejects=0)
            verdict=independent_review.check(agent)
            self.assertIn('未完成',verdict.instruction)
            self.assertNotIn('修复后',verdict.instruction)
            self.assertFalse(verdict.allow)
            self.assertEqual(spawned[0]['token_budget'],0)
            agent.cfg.verification_token_budget=234567
            independent_review.retry(agent)
            self.assertEqual(len(spawned),2)
            self.assertEqual(agent._independent_review['agent_id'],'2')
            self.assertEqual(spawned[-1]['token_budget'],234567)
            results.update(status='completed',summary='[]')
            self.assertFalse(independent_review.check(agent).allow)
            results['summary']=json.dumps({'verdict':'fail','findings':['counterexample']})
            self.assertIn('发现缺陷',independent_review.check(agent).instruction)

    def test_unlimited_and_shared_limit(self):
        for total in (None,1):
            with self.subTest(total=total), tempfile.TemporaryDirectory() as td:
                root=Path(td)
                manager=AgentManager(LLMConfig(max_tokens=128),Workspace(root/'ws'),root/'children',
                                     factory=lambda:ScriptedModel([[('finish',{'summary':json.dumps({'verdict':'blocked','findings':[],'tests':[],'reason':'fixture missing dependency'})})]]),total_tokens=total)
                try:
                    key=manager.spawn('finish',token_budget=0,purpose='verification')
                    state=manager.get(key);deadline=time.monotonic()+8
                    while state['status'] not in TERMINAL and time.monotonic()<deadline:
                        state=manager.wait(key,.2,state['revision'])
                    self.assertEqual(state['status'],'completed' if total is None else 'budget_exceeded')
                    self.assertEqual(state['token_budget'],0)
                    self.assertFalse(manager.budget.reservations)
                    if total is not None:
                        retry=manager.retry(key)
                        self.assertEqual(manager.get(retry)['purpose'],'verification')
                finally:
                    manager.close();manager.pool.shutdown(wait=True)


if __name__=='__main__': unittest.main(verbosity=2)
