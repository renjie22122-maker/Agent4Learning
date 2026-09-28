from pathlib import Path
import sys,tempfile,json,threading,unittest,urllib.request,urllib.parse,urllib.error
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.demo import make_handler
from agentplat.memory import MemoryStore


class MemoryHTTP(unittest.TestCase):
    def test_selection_approval_delete_and_csrf(self):
        with tempfile.TemporaryDirectory() as td,patch.dict('os.environ',{'AGENTLAB_MEMORY_DIR':td}):
            root=Path(td);log=root/'old.jsonl'
            log.write_text(json.dumps({'kind':'session/created','seq':1,'data':{'task':'以后默认 pytest'}}),encoding='utf-8')
            session={'session_id':'old','log_path':str(log),'workspace':str(root/'ws'),'status':'finished','task':'pytest'}
            demo=SimpleNamespace(permissions_token='fixture',list_agent_sessions=lambda limit:[session])
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(demo));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));opener.addheaders=[('Cookie','agentlab_access=fixture')]
            base=f'http://127.0.0.1:{server.server_port}'
            def post(route,**kw):return opener.open(base+route,urllib.parse.urlencode(kw).encode()).read().decode()
            try:
                with self.assertRaises(urllib.error.HTTPError) as error:post('/memories/select',token='wrong',session_old='1')
                self.assertEqual(error.exception.code,403)
                page=post('/memories/select',token='fixture',session_old='1',scope='project')
                self.assertIn('pytest',page);store=MemoryStore();row=store.list()[0]
                self.assertFalse(store.search(root/'ws','pytest'))
                post('/memories/update',token='fixture',id=row['id'],revision='1',content='使用 pytest',kind='preference',scope='project',status='active')
                self.assertTrue(store.search(root/'ws','pytest'))
                post('/memories/delete',token='fixture',id=row['id'])
                self.assertFalse(store.search(root/'ws','pytest'))
                session['status']='running'
                with self.assertRaises(urllib.error.HTTPError) as error:post('/memories/select',token='fixture',session_old='1')
                self.assertEqual(error.exception.code,400)
            finally:server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main(verbosity=2)
