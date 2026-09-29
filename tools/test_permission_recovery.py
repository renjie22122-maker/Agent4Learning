"""Permission recovery: real durable waiting and harmless host execution."""
import os, sys, tempfile, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat import approvals, human_input
from agentplat.permission_recovery import request_execution, guidance
from agentplat.runtime import CapabilityPolicy, PermissionDenied

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        for context in (patch.object(approvals,'DATABASE',root/'approvals.db'),
                        patch.dict(os.environ,{'AGENTLAB_HUMAN_DB':str(root/'human.db')})):
            context.start(); self.addCleanup(context.stop)
        self.agent=SimpleNamespace(ws=SimpleNamespace(root=root,allow_shell=True),
            session=SimpleNamespace(session_id='test',append=lambda *a,**k:None,flush=lambda *a:None),
            capabilities=CapabilityPolicy(),stop_flag=threading.Event())
        self.command='"'+sys.executable+'" --version'

    def run_request(self, decision=None, revoke=False):
        result=[]; errors=[]
        def worker():
            try: result.append(request_execution(self.agent,self.command,'Verify interpreter version'))
            except Exception as exc: errors.append(exc)
        thread=threading.Thread(target=worker);thread.start()
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                rows=human_input.list_questions('test')
                if rows: break
                time.sleep(.02)
            self.assertTrue(rows); self.assertTrue(thread.is_alive());self.assertEqual(result,[])
            self.assertIn('risk',rows[-1]['payload'])
            if revoke: self.agent.ws.allow_shell=False
            if decision: human_input.answer('test',rows[-1]['id'],decision)
            else: self.agent.stop_flag.set()
            thread.join(10); self.assertFalse(thread.is_alive())
            return result,errors
        finally:
            self.agent.stop_flag.set();thread.join(10)

    def test_allow_executes_once(self):
        result,errors=self.run_request('allow')
        self.assertEqual(errors,[]);self.assertTrue(result[0]['executed'])
        self.assertEqual(result[0]['result']['exit_code'],0)
        self.assertIn('Python',result[0]['result']['output'])
        with self.assertRaises(PermissionError):
            approvals.claim(result[0]['request_id'],'test',self.agent.ws.root)

    def test_deny_never_executes_or_reasks(self):
        result,errors=self.run_request('deny')
        self.assertEqual(errors,[]);self.assertFalse(result[0]['executed'])
        for n in range(105): approvals.request('other',self.agent.ws.root,'echo newer','test')
        with self.assertRaises(PermissionError):request_execution(self.agent,self.command,'again')
        self.assertEqual(len(human_input.list_questions('test')),1)

    def test_cancel_does_not_execute(self):
        result,errors=self.run_request()
        self.assertEqual(errors,[]);self.assertFalse(result[0]['executed'])

    def test_revocation_while_waiting_blocks_execution(self):
        result,errors=self.run_request('allow',revoke=True)
        self.assertEqual(result,[]);self.assertIsInstance(errors[0],PermissionDenied)

    def test_general_and_readonly_cannot_escalate(self):
        self.agent.ws.general_chat=True
        with self.assertRaises(PermissionDenied):request_execution(self.agent,self.command,'test')
        self.agent.ws.general_chat=False
        self.agent.capabilities=CapabilityPolicy(None,False,False,False)
        with self.assertRaises(PermissionDenied):request_execution(self.agent,self.command,'test')
        self.assertEqual(human_input.list_questions('test'),[])

    def test_unknown_side_effects_have_no_retry_suggestion(self):
        text=guidance(self.agent,'run_shell','TimeoutError: result unknown')
        self.assertNotIn('request_execution',text)
        self.assertIn('不得自动重放',text)
        self.assertIn('request_execution',guidance(self.agent,'run_shell','ModuleNotFoundError'))
        self.assertEqual(guidance(self.agent,'run_shell','SyntaxError: invalid syntax'),'')

if __name__=='__main__':unittest.main(verbosity=2)
