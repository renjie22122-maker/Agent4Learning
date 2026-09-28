from pathlib import Path
import sys, threading, unittest, http.client, json, tempfile
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.demo import make_handler
from agentplat.pages_agent import LIVE_JS
from agentplat import access_modes, independent_review, billing
from agentplat.workspace import Workspace
from agentplat.runtime import PermissionDenied
from agentplat.session import SessionLog
from agentplat.llmconfig import LLMConfig
from agentlab.providers import Usage


class LiveFeatures(unittest.TestCase):
    def test_partial_text_arrives_before_completion(self):
        state={'status':'running','session_id':'fixture','task':'demo','steps':[],'turns':[],'streamed_text':'第一段'}
        demo=SimpleNamespace(permissions_token='fixture-token',_lock=threading.RLock(),live_sessions={'fixture':(state,None,None)})
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(demo))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        try:
            client.request('GET','/api/agent-events?session=fixture',headers={'Cookie':'agentlab_access=fixture-token'})
            response=client.getresponse();self.assertEqual(response.status,200)
            def event():
                self.assertEqual(response.readline().strip(),b'event: progress')
                payload=json.loads(response.readline().decode().removeprefix('data: '))
                response.readline();return payload
            first=event();self.assertEqual(first['status'],'running');self.assertIn('第一段',first['html'])
            with demo._lock: state['streamed_text']='第一段第二段'
            self.assertIn('第二段',event()['html'])
            with demo._lock:state['status']='done'
            self.assertEqual(event()['status'],'done')
            self.assertIn('new EventSource',LIVE_JS);self.assertNotIn('setInterval',LIVE_JS)
        finally:
            state['status']='done';client.close();server.shutdown();server.server_close();worker.join(2)

    def test_permission_transitions(self):
        with tempfile.TemporaryDirectory() as td:
            agent=SimpleNamespace(ws=Workspace(Path(td)/'ws'),session=SessionLog(Path(td)/'log'))
            agent.ws.execution_mode='native';access_modes.apply(agent,'readonly')
            with self.assertRaises(PermissionDenied): agent.capabilities.check('write_file',writes=True)
            with self.assertRaises(PermissionDenied): agent.capabilities.check('spawn_agent')
            access_modes.apply(agent,'full');self.assertEqual(agent.ws.execution_mode,'local')
            agent.ws.check_command('custom-command --version')
            access_modes.apply(agent,'auto');self.assertEqual(agent.ws.execution_mode,'native')
            with self.assertRaises(Exception): agent.ws.check_command('custom-command --version')

    def test_independent_evidence_and_version_binding(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'code.py').write_text('x=1')
            results={'status':'completed','summary':json.dumps({'verdict':'pass','tests':['independent test'],'findings':[]}),'evidence':[]}
            tasks=[]
            def spawn(task,**kw): tasks.append((task,kw)); return str(len(tasks))
            manager=SimpleNamespace(spawn=spawn,get=lambda key:results)
            agent=SimpleNamespace(ws=Workspace(root),session=SimpleNamespace(append=lambda *a,**k:None),child_manager=lambda:manager,_task_text='Return correct results',_finish_rejects=0)
            self.assertFalse(independent_review.check(agent).allow)
            self.assertEqual(tasks[0][1]['mode'],'isolated')
            results['evidence']=[{'exit_code':0}];self.assertTrue(independent_review.check(agent).allow)
            results['changes']=[{'before':'oldhash','path':'code.py'}]
            self.assertFalse(independent_review.check(agent).allow)
            results['changes']=[];(root/'code.py').write_text('x=2');independent_review.check(agent)
            self.assertEqual(len(tasks),2)

    def test_cache_billing_not_double_counted(self):
        cfg=LLMConfig(base_url='https://api.deepseek.com',model='deepseek-flash')
        # UTC Sunday: unambiguous off-peak, independent of holiday calendar.
        from datetime import datetime, timezone
        timestamp=datetime(2026,9,27,8,tzinfo=timezone.utc).timestamp()
        result=billing.quote(cfg,Usage(1000000,1000000,800000),timestamp=timestamp)
        self.assertAlmostEqual(result['usd'],.6324)
        self.assertEqual(result['usd'],result['usd_min'])
        self.assertFalse(result['invoice'])


if __name__=='__main__':unittest.main(verbosity=2)
