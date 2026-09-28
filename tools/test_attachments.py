from pathlib import Path
import sys,tempfile,unittest,base64,json,zipfile,io,threading,http.client
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat import attachments
from agentplat.session import SessionLog
from agentplat.workspace import Workspace
from agentplat.demo import make_handler
from agentplat.pages_agent import _turn_user

class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.patch=patch.object(attachments,'ROOT',self.root/'attachments');self.patch.start();self.addCleanup(self.patch.stop)
    def upload(self,name,raw):return attachments.upload(name,base64.b64encode(raw).decode())
    def agent(self,name):
        agent=SimpleNamespace(session=SessionLog(self.root/(name+'.jsonl')),tools={},ws=Workspace(self.root/name))
        attachments.install(agent);return agent
    def test_text_binding_pagination_and_isolation(self):
        item=self.upload('notes.txt',('unique fact\n'*300).encode());agent=self.agent('a');other=self.agent('b')
        attachments.bind(agent,[item]);attachments.install(agent)
        other_item=self.upload('other.txt',b'Other conversation');attachments.bind(other,[other_item]);attachments.install(other)
        result=json.loads(agent.tools['read_attachment'].fn(attachment_id=item['id'],limit=1))
        self.assertIn('unique fact',result['chunks'][0]['text']);self.assertIsNotNone(result['next_offset'])
        with self.assertRaises(PermissionError):other.tools['read_attachment'].fn(attachment_id=item['id'])
        restored=self.agent('restore');restored.session=SessionLog.load(agent.session.path)[0]
        self.assertIn(item['id'],restored.ws.attachment_source())
        html=_turn_user('Read this'+attachments.describe([item]),0)
        self.assertIn('notes.txt',html);self.assertNotIn('使用 read_attachment',html)
    def test_csv_and_docx_extraction(self):
        csv=self.upload('data.csv',b'name,value\napple,42\n');agent=self.agent('a');attachments.bind(agent,[csv]);attachments.install(agent)
        self.assertIn('42',agent.tools['read_attachment'].fn(attachment_id=csv['id']))
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as z:
            z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Document fact 73</w:t></w:r></w:p></w:body></w:document>')
        doc=self.upload('report.docx',raw.getvalue());attachments.bind(agent,[doc])
        self.assertIn('Document fact 73',agent.tools['read_attachment'].fn(attachment_id=doc['id']))
    def test_reject_invalid_uploads(self):
        for name,raw in [('evil.exe',b'x'),('empty.txt',b''),('broken.docx',b'not a zip')]:
            with self.assertRaises((ValueError,RuntimeError)):self.upload(name,raw)
        with self.assertRaises(ValueError):attachments.upload('a.txt','%%%')
        with self.assertRaises(ValueError):attachments.directory('../escape')
        with self.assertRaises(ValueError):attachments.validate_ids(['a']*11)
        item=self.upload('../../safe.txt',b'safe');self.assertEqual(item['name'],'safe.txt');self.assertFalse((self.root/'safe.txt').exists())
    def test_http_upload_auth_and_size(self):
        demo=SimpleNamespace(permissions_token='token')
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(demo));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for token,status in [('bad',403),('token',200)]:
                client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=15)
                client.request('POST','/agent/attachments',json.dumps({'name':'http.txt','data':base64.b64encode(b'HTTP fact').decode()}),{'Cookie':'agentlab_access=token','X-Form-Token':token,'Content-Type':'application/json'})
                response=client.getresponse();self.assertEqual(response.status,status);payload=json.loads(response.read());client.close()
                if status==200:self.assertEqual(payload['name'],'http.txt')
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_attachment_arriving_during_run_and_restore(self):
        import time
        from tools.test_live_chat import LiveChatTests,BlockingModel
        demo=LiveChatTests().make_demo(self.root);model=BlockingModel(False)
        item=self.upload('steering.txt',b'new reference')
        with patch('agentplat.llm.OpenAIChatClient',return_value=model):
            sid=demo.start_agent_task('Read only initial task')
            self.assertTrue(model.entered.wait(3))
            demo.continue_agent_task('Read attached file',session_id=sid,attachment_ids=[item['id']])
            model.release.set();deadline=time.monotonic()+6
            while demo.agent_state['status']=='running' and time.monotonic()<deadline:time.sleep(.02)
            agent=demo.live_sessions[sid][1]
            self.assertIn('read_attachment',agent.tools)
            self.assertIn('new reference',agent.tools['read_attachment'].fn(attachment_id=item['id']))
            demo.agent_state={};demo.live_sessions.clear();demo.restore_agent_session(sid)
            restored=demo.live_sessions[sid][1];attachments.install(restored)
            self.assertIn('new reference',restored.tools['read_attachment'].fn(attachment_id=item['id']))

if __name__=='__main__':unittest.main(verbosity=2)
