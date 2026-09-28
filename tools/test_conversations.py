from pathlib import Path
import sys,tempfile,unittest,threading,json,http.client
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.conversations import ConversationStore,store_for,mutate
from agentplat.workspaces import WorkspaceManager,DEFAULT_WORKSPACE
from agentplat.pages_agent import _sidebar
from agentplat.demo import make_handler

class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.mgr=WorkspaceManager(state_path=self.root/'state.json')
        self.row={'session_id':'fixture','workspace':str(DEFAULT_WORKSPACE),'task':'original','status':'done'}
        self.demo=SimpleNamespace(ws_mgr=self.mgr,permissions_token='fixture-token',live_sessions={},_lock=threading.RLock())
        self.demo.list_agent_sessions=lambda limit=25:[{**self.row,**store_for(self.demo).load().get('fixture',{})}]
    def change(self,action,value):return mutate(self.demo,dict(token='fixture-token',session='fixture',action=action,value=value))
    def test_persistent_title_and_flags(self):
        self.change('rename','New title');self.change('pinned','true');self.change('unread','true')
        saved=ConversationStore(store_for(self.demo).path).load()['fixture']
        self.assertEqual(saved['title'],'New title');self.assertTrue(saved['pinned']);self.assertTrue(saved['unread'])
    def test_archive_and_trash_are_recoverable(self):
        self.change('archived','true')
        self.assertNotIn('data-session="fixture"',_sidebar(self.mgr,self.demo.list_agent_sessions(),None,self.mgr.summary()))
        self.mgr.conversation_view='archived'
        self.assertIn('data-session="fixture"',_sidebar(self.mgr,self.demo.list_agent_sessions(),None,self.mgr.summary()))
        self.change('deleted','true')
        self.assertNotIn('data-session="fixture"',_sidebar(self.mgr,self.demo.list_agent_sessions(),None,self.mgr.summary()))
        self.mgr.conversation_view='trash'
        self.assertIn('data-session="fixture"',_sidebar(self.mgr,self.demo.list_agent_sessions(),None,self.mgr.summary()))
        self.change('deleted','false');self.assertTrue(store_for(self.demo).load()['fixture']['archived'])
    def test_running_task_not_deleted(self):
        self.demo.live_sessions={'fixture':({'status':'running'},None,None)}
        with self.assertRaises(ValueError):self.change('deleted','true')
        self.assertFalse(store_for(self.demo).path.exists())
    def test_move_preserves_scope_and_escapes_title(self):
        folder=self.root/'project';folder.mkdir();key=self.mgr.save_group('Project',{'main':folder})
        self.change('move',key);self.change('rename','<script>alert(1)</script>')
        row=self.demo.list_agent_sessions()[0]
        self.assertEqual(row['workspace'],str(DEFAULT_WORKSPACE));self.assertEqual(row['display_group'],key)
        html=_sidebar(self.mgr,[row],None,self.mgr.summary())
        self.assertNotIn('<script>alert(1)</script>',html);self.assertIn('&lt;script&gt;',html)
        self.change('move','__general__')
    def test_reject_bad_inputs(self):
        for action,value in [('rename',''),('rename','x'*121),('pinned','maybe'),('move','missing'),('unknown','x')]:
            with self.assertRaises(ValueError):self.change(action,value)
        with self.assertRaises(PermissionError):mutate(self.demo,{'token':'bad'})
        with self.assertRaises(ValueError):mutate(self.demo,{'token':'fixture-token','session':'missing','action':'rename','value':'name'})
    def test_http_authorization_and_mutation(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.demo));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        try:
            headers={'Cookie':'agentlab_access=fixture-token','Content-Type':'application/x-www-form-urlencoded'}
            for token,expected in [('bad',403),('fixture-token',200)]:
                client.request('POST','/agent/conversation',f'token={token}&session=fixture&action=rename&value=HTTP+title',headers)
                response=client.getresponse();self.assertEqual(response.status,expected);response.read()
            self.assertEqual(store_for(self.demo).load()['fixture']['title'],'HTTP title')
        finally:client.close();server.shutdown();server.server_close();thread.join(2)

if __name__=='__main__':unittest.main(verbosity=2)
