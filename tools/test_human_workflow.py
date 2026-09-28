import json, os, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
from agentplat import human_input, approvals
from agentplat.experiments import ScriptedModel
from agentplat.llmconfig import LLMConfig
from agentplat.loop import CodingAgent
from agentplat.workspace import Workspace


class HumanWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'AGENTLAB_HUMAN_DB':str(self.root/'human.db')});self.env.start();self.addCleanup(self.env.stop)
        self.approvals=patch.object(approvals,'DATABASE',self.root/'approvals.db');self.approvals.start();self.addCleanup(self.approvals.stop)
        self.ws=Workspace(self.root/'ws');self.ws.execution_mode='local'
        self.stop=threading.Event()

    def pending(self, sid):
        until=time.monotonic()+5
        while time.monotonic()<until:
            rows=human_input.list_questions(sid)
            if rows:return rows[-1]
            time.sleep(.02)
        self.fail('no question')

    def test_loop_waits_without_model_calls_and_continues_once(self):
        model=ScriptedModel([[('request_user_input',{'question':'选择颜色','options':['蓝色','绿色']})],
                             [('finish',{'summary':'选择已收到'})]])
        agent=CodingAgent(model,LLMConfig(),workspace=self.ws,session_dir=self.root/'logs',stop_flag=self.stop,enable_subagents=False)
        result=[];thread=threading.Thread(target=lambda:result.append(agent.run('交互测试。必须先调用 request_user_input 问颜色，然后 finish 概述选择。')))
        thread.start()
        try:
            row=self.pending(agent.session.session_id);time.sleep(.3);self.assertEqual(model.turn,1)
            with self.assertRaises(ValueError):human_input.answer('wrong-session',row['id'],'绿色')
            human_input.answer(agent.session.session_id,row['id'],'绿色')
            with self.assertRaises(ValueError):human_input.answer(agent.session.session_id,row['id'],'绿色')
            thread.join(5);self.assertFalse(thread.is_alive());self.assertTrue(result[0].ok);self.assertEqual(model.turn,2)
            self.assertTrue(agent.session.of_kind('human/answered'))
        finally:self.stop.set();thread.join(5)

    def test_denial_and_ordinary_answer_never_grant_command(self):
        request=approvals.request('s',self.ws.root,'python -V','fixture')
        question=human_input.create('s','s','question',{'question':'可以吗'})
        human_input.answer('s',question,'可以')
        with self.assertRaises(PermissionError):approvals.claim(request['request_id'],'s',self.ws.root)
        key=human_input.create('s','s','approval',request)
        human_input.answer('s',key,'deny')
        with self.assertRaises(PermissionError):approvals.claim(request['request_id'],'s',self.ws.root)

    def test_persistent_answer_and_single_use_approval(self):
        request=approvals.request('s',self.ws.root,'python -V','fixture')
        key=human_input.create('s','s','approval',request)
        # Connections are closed between creation, reload and reply (restart storage contract).
        self.assertEqual(human_input.list_questions('s')[0]['status'],'pending')
        human_input.answer('s',key,'allow')
        self.assertEqual(approvals.claim(request['request_id'],'s',self.ws.root)['command'],'python -V')
        with self.assertRaises(PermissionError):approvals.claim(request['request_id'],'s',self.ws.root)

    def test_cancel_wakes_waiter(self):
        agent=CodingAgent(ScriptedModel(),LLMConfig(),workspace=self.ws,session_dir=self.root/'logs',stop_flag=self.stop,enable_subagents=False)
        results=[];thread=threading.Thread(target=lambda:results.append(human_input.ask(agent,'确认吗')));thread.start()
        self.pending(agent.session.session_id);self.stop.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertEqual(results[0]['status'],'cancelled')

    def test_expired_approval_cannot_be_answered(self):
        request=approvals.request('s',self.ws.root,'python -V','fixture')
        key=human_input.create('s','s','approval',request)
        with approvals.connection() as db:db.execute('UPDATE approvals SET expires=0')
        with self.assertRaises(ValueError):human_input.answer('s',key,'allow')
        with self.assertRaises(PermissionError):approvals.claim(request['request_id'],'s',self.ws.root)

    def test_reply_endpoint_restores_root_once_after_restart(self):
        import http.client,urllib.parse
        from types import SimpleNamespace
        from http.server import ThreadingHTTPServer
        from agentplat.demo import make_handler
        calls=[]
        demo=SimpleNamespace(permissions_token='fixture',live_sessions={},
                             continue_agent_task=lambda *a,**k:calls.append((a,k)))
        key=human_input.create('root','root','question',{'question':'颜色？'})
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(demo))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            def post(token):
                client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                try:
                    client.request('POST','/agent/human-input',body=urllib.parse.urlencode({'token':token,'id':key,'session':'root','answer':'绿'}),headers={'Cookie':'agentlab_access=fixture','Content-Type':'application/x-www-form-urlencoded'})
                    response=client.getresponse();code=response.status;response.read();return code
                finally:client.close()
            self.assertEqual(post('wrong'),403);self.assertFalse(calls)
            self.assertEqual(post('fixture'),200);self.assertEqual(len(calls),1)
            self.assertEqual(calls[0][1]['session_id'],'root')
            self.assertEqual(post('fixture'),400);self.assertEqual(len(calls),1)
        finally:server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
