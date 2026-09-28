"""Durability, ownership races, authority and budgeted child compaction."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from agentplat.team_coordination import Coordination
from agentplat.subagents import AgentManager, TERMINAL
from agentplat.workspace import Workspace
from agentplat.llmconfig import LLMConfig
from agentplat.experiments import ScriptedModel


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.mail=Coordination(self.root)

    def test_durable_dedup_reply_and_ack_authority(self):
        first=self.mail.send('a','root','finding',dedup='one')
        self.assertEqual(self.mail.send('a','root','finding',dedup='one')['id'],first['id'])
        with self.assertRaises(ValueError):self.mail.send('a','root','changed',dedup='one')
        recovered=Coordination(self.root)
        self.assertTrue(recovered.pending('root'))
        self.assertIn(first['id'],recovered.drain('root')[0])
        self.assertFalse(recovered.pending('root'))
        self.assertIsNotNone(recovered.messages('root')[0]['delivered'])
        self.assertIsNone(recovered.messages('root')[0]['acknowledged'])
        with self.assertRaises(ValueError):recovered.acknowledge('b',first['id'])
        recovered.acknowledge('root',first['id'])
        recovered.send('root','a','checked',reply_to=first['id'])
        with self.assertRaises(ValueError):recovered.send('b','a','pretend reply',reply_to=first['id'])

    def test_backpressure_and_automatic_pending_dedup(self):
        first=self.mail.send('a','b','same')
        self.assertEqual(first['id'],self.mail.send('a','b','same')['id'])
        for i in range(31):self.mail.send('a','b',str(i))
        with self.assertRaises(RuntimeError):self.mail.send('a','b','overflow')
        self.mail.drain('b',1)
        self.mail.send('a','b','available again')

    def test_only_one_concurrent_claim_and_versioned_handoff(self):
        task=self.mail.job('root','create',title='shared work',acceptance='check output')
        def claim(owner):
            try:return self.mail.job(owner,'claim',task_id=task['id'],expected_revision=1)
            except RuntimeError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(claim,['a','b']))
        winner=next(r for r in results if r)
        self.assertEqual(sum(r is not None for r in results),1)
        loser='a' if winner['owner']=='b' else 'b'
        with self.assertRaises(ValueError):self.mail.job(loser,'release',task_id=task['id'],expected_revision=2)
        handed=self.mail.job(winner['owner'],'handoff',task_id=task['id'],expected_revision=2,target=loser,note='API contract fixed')
        self.assertEqual(handed['owner'],loser)
        self.mail.stop_owner(loser,'failed')
        self.assertEqual(self.mail.jobs()[0]['status'],'blocked')

    def manager(self,factory=ScriptedModel):
        manager=AgentManager(LLMConfig(max_tokens=128),Workspace(self.root/'ws'),self.root/'agents',factory=factory)
        self.addCleanup(lambda:(manager.close(),manager.pool.shutdown(wait=True)))
        return manager

    def done(self,manager,key):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            result=manager.get(key)
            if result['status'] in TERMINAL:return result
            manager.wait(key,.1,result['revision'])
        self.fail('task did not terminate')

    def test_child_reports_to_root_and_automatic_completion_event(self):
        model=lambda:ScriptedModel([[('send_agent_message',{'agent_id':'root','message':'found-731','dedup_key':'report'})],
                                   [('finish',{'summary':'finished'})]])
        manager=self.manager(model)
        key=manager.spawn('send root a finding')
        self.assertEqual(self.done(manager,key)['status'],'completed')
        messages=manager.coordination.messages('root')
        self.assertEqual([m['kind'] for m in messages],['message','task_finished'])
        self.assertEqual(messages[0]['sender'],key)
        self.assertIn('found-731','\n'.join(manager.deliver()))
        self.assertFalse(manager.coordination.pending('root'))
        next_task=manager.followup(key,'inspect again')
        self.assertNotEqual(next_task['agent_id'],key)
        self.assertEqual(manager.get(next_task['agent_id'])['continued_from'],key)

    def test_child_compaction_uses_shared_budget(self):
        from agentlab.providers import Usage
        from agentplat.compaction import SUMMARY_SECTIONS
        class SummarizingModel(ScriptedModel):
            def complete_with_tools(self,model,messages,tools,timeout):
                if not tools:
                    return '\n'.join(section+'：保留工作记录' for section in SUMMARY_SECTIONS), [], Usage(120,40,0)
                return super().complete_with_tools(model,messages,tools,timeout)
        manager=self.manager(SummarizingModel)
        key=manager.spawn('inspect')
        self.done(manager,key)
        agent=manager.tasks[key]['agent']
        self.assertTrue(agent.compactor.enabled)
        before=manager.budget.spent
        from agentlab.providers import ChatMessage
        messages=[ChatMessage('system','coding agent'),ChatMessage('user','不能修改 public API')]
        messages += [ChatMessage('assistant','earlier progress '*100) for _ in range(12)]
        result=agent.compactor.summarize(messages,keep_last=4)
        self.assertIsNotNone(result)
        self.assertGreater(result[0],0)
        self.assertIn('不能修改 public API','\n'.join(m.content for m in messages))
        self.assertGreater(manager.budget.spent,before)
        self.assertEqual(manager.budget.spent,manager.get(key)['used_tokens'])

    def test_team_page_shows_escaped_mail_and_board(self):
        from types import SimpleNamespace
        from agentplat.pages_team import render
        manager=self.manager()
        manager.send('root','<script>unsafe</script>')
        manager.team_task(action='create',title='API task')
        demo=SimpleNamespace(live_sessions={'s':({},SimpleNamespace(children=manager))},agent_state={})
        page=render(demo,{'session':'s'}).decode()
        self.assertIn('API task',page)
        self.assertIn('&lt;script&gt;unsafe',page)
        self.assertNotIn('<script>unsafe',page)


if __name__=='__main__':unittest.main()
