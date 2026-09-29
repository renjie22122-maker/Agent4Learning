"""Reviewer approval routing, evidence ownership and authority boundaries."""
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import test_permission_recovery as recovery
from agentplat import approvals, human_input
from agentplat.runtime import CapabilityPolicy, PermissionDenied
from agentplat.verification_tools import install


class ReviewerExecutionTests(recovery.RecoveryTests):
    def setUp(self):
        super().setUp()
        self.events=[]
        self.agent.verification_task=True
        self.agent.tools={}
        self.agent.ws.scope=self.agent.ws.root
        self.agent.ws.human_session='parent'
        self.agent.ws.execution_mode='native'
        self.agent.session.append=lambda kind,**data:self.events.append((kind,data))
        install(self.agent,NS(get=lambda owner:{}),'test')
        self.agent.capabilities=CapabilityPolicy(frozenset(self.agent.tools))

    def run_request(self, decision=None, revoke=False):
        # The inherited cases still address the owner; this test suite also
        # checks the actual parent-facing routing separately below.
        self.agent.ws.human_session='test'
        return super().run_request(decision,revoke)

    def test_parent_card_child_evidence(self):
        result=[]
        def work():
            result.append(json.loads(self.agent.tools['request_execution'].fn(
                command=self.command,reason='Independent interpreter check')))
        thread=threading.Thread(target=work);thread.start()
        try:
            deadline=time.monotonic()+5
            rows=[]
            while time.monotonic()<deadline:
                rows=human_input.list_questions('parent')
                if rows:break
                time.sleep(.02)
            self.assertTrue(rows)
            self.assertEqual(result,[])
            self.assertFalse(any(k=='approval/consumed' for k,d in self.events))
            human_input.answer('parent',rows[0]['id'],'allow')
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertTrue(result[0]['result']['success'])
            evidence=[d for k,d in self.events if k=='verification/evidence']
            self.assertEqual(len(evidence),1)
            self.assertEqual(evidence[0]['execution_backend'],'host')
            self.assertTrue(self.agent._verified)
            self.assertEqual(approvals.list_requests()[0]['session'],'test')
        finally:
            self.agent.stop_flag.set();thread.join(10)

    def test_author_grant_not_reusable(self):
        grant=approvals.request('parent',self.agent.ws.root,self.command,'author')
        approvals.decide(grant['request_id'],True)
        with self.assertRaises(PermissionError):approvals.execute(self.agent,grant['request_id'])
        self.assertFalse(self.events)

    def test_failed_host_command_has_no_evidence(self):
        grant=approvals.request('test',self.agent.ws.root,'unused','fixture')
        approvals.decide(grant['request_id'],True)
        class Executor:
            def start(self,*a,**kw):return 'p'
            def wait(self,*a):return dict(status='timeout',exit_code=0,output='partial')
            def close(self):pass
        with patch('agentplat.processes.ProcessSupervisor',Executor):
            result=approvals.execute(self.agent,grant['request_id'])
        self.assertFalse(result['success']);self.assertFalse(self.agent._verified)
        self.assertFalse(any(k=='verification/evidence' for k,d in self.events))

    def test_shell_disabled_has_no_approval_tools(self):
        self.agent.ws.allow_shell=False;self.agent.tools={}
        install(self.agent,NS(get=lambda owner:{}),'test')
        self.assertNotIn('request_execution',self.agent.tools)

    def test_live_parent_revocation(self):
        self.agent.authority_provider=lambda:CapabilityPolicy(None,False,False,False)
        with self.assertRaises(PermissionDenied):
            self.agent.tools['request_execution'].fn(command=self.command,reason='check')
        self.assertEqual(human_input.list_questions('parent'),[])


if __name__=='__main__':
    import unittest
    unittest.main()
