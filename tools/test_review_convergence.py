import json,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from agentplat.subagents import AgentManager,TERMINAL
from agentplat.llmconfig import LLMConfig
from agentplat.workspace import Workspace
from agentplat.experiments import ScriptedModel
from agentplat.loop import CodingAgent
from agentplat.independent_review import check,wait_pending


class ReviewConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.addCleanup(self.tmp.cleanup)
        self.ws=Workspace(self.root/'ws');self.cfg=LLMConfig(max_tokens=128)

    def manager(self,factory):
        manager=AgentManager(self.cfg,self.ws,self.root/'team',factory=factory)
        self.addCleanup(lambda:(manager.close(),manager.pool.shutdown(wait=True)))
        return manager

    def test_wait_ignores_stale_revision_and_billing_changes(self):
        release=threading.Event();self.addCleanup(release.set)
        class Delayed(ScriptedModel):
            def complete_with_tools(self,*args):release.wait(2);return super().complete_with_tools(*args)
        manager=self.manager(Delayed);key=manager.spawn('wait test')
        result=manager.wait_for_model(key,timeout_s=.15,after_revision=-1)
        self.assertGreaterEqual(result['actual_wait_seconds'],.12)
        self.assertNotIn('context',result);self.assertNotIn('task',result)
        release.set()

    def test_blocked_review_finishes_without_command_evidence(self):
        payload={'verdict':'blocked','tests':[],'findings':[],'reason':'Required environment unavailable'}
        manager=self.manager(lambda:ScriptedModel([[('finish',{'summary':json.dumps(payload)})]]))
        agent=SimpleNamespace(cfg=self.cfg,ws=self.ws,_files_touched=['app.js'],_task_text='verify UI',
            _finish_rejects=0,session=SimpleNamespace(append=lambda *a,**kw:None),child_manager=lambda:manager,
            stop_flag=threading.Event(),steering=None)
        check(agent)
        wait_pending(agent)
        result=manager.get(agent._independent_review['agent_id'])
        self.assertEqual(result['status'],'completed')
        self.assertFalse(result['evidence'])
        verdict=check(agent)
        self.assertFalse(verdict.allow);self.assertTrue(verdict.exhausted)
        self.assertEqual(verdict.by,'独立验收受阻')
        child=manager.tasks[result['agent_id']]['agent']
        self.assertIn('browser_check',child.tools)
        self.assertIn('verification_environment',child.tools)
        self.assertNotIn('send_agent_message',child.tools)

    def test_host_wait_wakes_for_steering(self):
        release=threading.Event();self.addCleanup(release.set)
        class Delayed(ScriptedModel):
            def complete_with_tools(self,*args):release.wait(2);return super().complete_with_tools(*args)
        manager=self.manager(Delayed);key=manager.spawn('test')
        agent=SimpleNamespace(_independent_review={'agent_id':key},child_manager=lambda:manager,
            stop_flag=threading.Event(),steering=lambda:['new user requirement'])
        self.assertEqual(wait_pending(agent),['new user requirement']);release.set()

    def test_parent_makes_no_model_calls_while_review_is_running(self):
        payload=json.dumps({'verdict':'blocked','tests':[],'findings':[],'reason':'fixture'})
        manager=self.manager(lambda:ScriptedModel([[('finish',{'summary':payload})]],delay=.25))
        key=manager.spawn('review',purpose='verification')
        calls=[]
        class Parent(ScriptedModel):
            def complete_with_tools(self,*args):
                calls.append(manager.get(key)['status'])
                return super().complete_with_tools(*args)
        parent=CodingAgent(Parent(),self.cfg,workspace=self.ws,session_dir=self.root/'parent',enable_subagents=False)
        parent._independent_review={'agent_id':key};parent.child_manager=lambda:manager
        result=parent.run('read-only parent')
        self.assertTrue(result.ok)
        self.assertEqual(calls,['completed'])
        self.assertTrue(parent.session.of_kind('independent_review/waited'))

    def test_failed_browser_assertion_invalidates_old_evidence(self):
        from unittest.mock import patch
        from agentplat.browser_tools import install
        parent=CodingAgent(ScriptedModel(),self.cfg,workspace=self.ws,session_dir=self.root/'browser',enable_subagents=False)
        with patch('agentplat.browser_tools.BrowserSession') as browser:
            browser.return_value.call.side_effect=[{'matched':True,'url':'https://agent-preview.invalid/index.html'},RuntimeError('assertion failed')]
            install(parent)
            tool=parent.tools['browser_check']
            tool.fn(selector='#score',expected_text='1')
            self.assertTrue(parent.evidence.valid(self.ws.scope))
            with self.assertRaises(RuntimeError):tool.fn(selector='#score',expected_text='2')
            self.assertFalse(parent.evidence.valid(self.ws.scope))

    def test_cancelled_request_is_not_reported_as_model_error(self):
        class Cancelled:
            def complete_with_tools(self,*args):raise InterruptedError('cancelled request')
        agent=CodingAgent(Cancelled(),self.cfg,workspace=self.ws,session_dir=self.root/'logs',enable_subagents=False)
        result=agent.run('read only check')
        self.assertEqual(result.stopped_by,'user_aborted')
        self.assertNotIn('InterruptedError',result.error)

    def test_readonly_followup_does_not_reverify_historical_edits(self):
        agent=CodingAgent(ScriptedModel(),self.cfg,workspace=self.ws,session_dir=self.root/'followup',enable_subagents=False)
        self.assertTrue(agent.run('read-only initial task').ok)
        (self.ws.root/'historical.txt').write_text('prior delivery')
        agent._files_touched=['historical.txt']
        agent._independent_review={'agent_id':'obsolete'}
        agent.independent_review_required=True
        result=agent.continue_with('just tell me where the file is')
        self.assertTrue(result.ok)
        self.assertIsNone(agent._independent_review)
        self.assertEqual(agent._files_touched,[])

    def test_pass_without_evidence_rejected_but_fail_can_report_counterexample(self):
        for status,findings,expected in [('pass',[],False),('fail',['static counterexample'],True)]:
            payload={'verdict':status,'tests':['inspected implementation'],'findings':findings}
            agent=CodingAgent(ScriptedModel([[('finish',{'summary':json.dumps(payload)})]]),self.cfg,
                workspace=self.ws,session_dir=self.root/status,enable_subagents=False)
            agent.verification_task=True
            result=agent.run('verify')
            self.assertEqual(result.ok,expected)


if __name__=='__main__':unittest.main()
