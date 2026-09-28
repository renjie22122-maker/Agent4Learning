import json,tempfile,threading,unittest,http.client
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from agentplat.desktop_login import existing_token,login_page
from agentplat.reliability import FailureCircuit,call_with_recovery


class ReliabilityLoginTests(unittest.TestCase):
    def test_credential_reused_only_for_same_service(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'access.json';token='a'*32
            path.write_text(json.dumps({'token':token,'port':8800}))
            self.assertEqual(existing_token(path,8800,'new'),token)
            self.assertEqual(existing_token(path,8801,'new'),'new')
            path.write_text('{bad json')
            self.assertEqual(existing_token(path,8800,'new'),'new')

    def test_anonymous_page_has_no_secret_and_auth_cookie_persists(self):
        from agentplat.demo import make_handler
        token='b'*32
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(SimpleNamespace(permissions_token=token)))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            client=http.client.HTTPConnection('127.0.0.1',server.server_port)
            client.request('GET','/agent');response=client.getresponse();body=response.read().decode()
            self.assertEqual(response.status,401);self.assertNotIn(token,body)
            self.assertIn('桌面',body)
            client.request('GET','/auth?token='+token);response=client.getresponse();response.read()
            self.assertEqual(response.status,303)
            self.assertIn('Max-Age=2592000',response.getheader('Set-Cookie'))
            self.assertIn('HttpOnly',response.getheader('Set-Cookie'));client.close()
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_transient_retries_are_bounded_and_counted(self):
        calls=[];attempts=[];retries=[]
        class Busy(Exception):code='429';retryable=True
        def request():
            calls.append(1)
            if len(calls)<3:raise Busy()
            return 'ok'
        result=call_with_recovery(request,on_attempt=lambda:attempts.append(1),on_retry=lambda *args:retries.append(args),delays=(0,0))
        self.assertEqual(result,'ok');self.assertEqual(len(attempts),3);self.assertEqual(len(retries),2)
        calls.clear()
        def never():calls.append(1);raise Busy()
        with self.assertRaises(Busy):call_with_recovery(never,delays=(0,0))
        self.assertEqual(len(calls),3)

    def test_auth_timeout_and_cancel_not_retried(self):
        for code in ('401','TIMEOUT','400'):
            calls=[]
            class Failure(Exception):retryable=True
            error=Failure();error.code=code
            def request():calls.append(1);raise error
            with self.assertRaises(Failure):call_with_recovery(request,delays=(0,0))
            self.assertEqual(len(calls),1)
        stop=threading.Event();stop.set()
        with self.assertRaises(InterruptedError):call_with_recovery(lambda:self.fail('must not call'),cancel=stop)

    def test_interleaved_failures_trip_but_changed_files_allow_retry(self):
        circuit=FailureCircuit()
        for i in range(5):
            self.assertFalse(circuit.observe('run_shell',{'command':'x'},'missing runtime',False,'same'))
            circuit.observe('read_file',{'path':'a'},'data',True,'')
        self.assertTrue(circuit.observe('run_shell',{'command':'x'},'missing runtime',False,'same'))
        self.assertFalse(circuit.observe('run_shell',{'command':'x'},'missing runtime',False,'changed'))
        circuit.observe('run_shell',{'command':'x'},'ok',True,'')
        self.assertFalse(circuit.observe('run_shell',{'command':'x'},'missing runtime',False,'same'))

    def test_loop_retries_model_without_replaying_tools(self):
        from agentplat.loop import CodingAgent
        from agentplat.experiments import ScriptedModel
        from agentplat.llmconfig import LLMConfig
        from agentplat.workspace import Workspace
        from agentplat.llm import LLMCallError
        class Transient(ScriptedModel):
            attempted=False
            def complete_with_tools(self,*args):
                if not self.attempted:
                    self.attempted=True
                    raise LLMCallError('429','fixture busy',retryable=True)
                return super().complete_with_tools(*args)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            agent=CodingAgent(Transient(),LLMConfig(),workspace=Workspace(root/'ws'),session_dir=root/'log',enable_subagents=False)
            result=agent.run('read only task')
            self.assertTrue(result.ok)
            self.assertEqual(result.model_calls,2)
            self.assertEqual(len(agent.session.of_kind('model/retry')),1)
            self.assertEqual(len(agent.session.of_kind('tool/call')),1)


if __name__=='__main__':unittest.main()
