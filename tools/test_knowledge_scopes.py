"""Scope isolation, explicit opt-in, immutable verifier snapshots and real HTTP forms."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.knowledge import KnowledgeBase
from agentplat.knowledge_scopes import sources, selected, ScopedKnowledge, context, resolve, save_selection, snapshot_sources


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        env=patch.dict(os.environ,AGENTLAB_KB_DIR=str(self.root/'kb'));env.start();self.addCleanup(env.stop)
        self.manager=NS(state_path=self.root/'state.json',current=self.root/'other',current_group='other')
        self.states=[dict(session_id='a',workspace=str(self.root/'project'),conversation_kind='project'),dict(session_id='b',workspace=str(self.root/'project'),conversation_kind='project'),dict(session_id='g',workspace=str(self.root/'chat'),conversation_kind='general')]
        self.demo=NS(ws_mgr=self.manager,live_sessions={},list_agent_sessions=lambda n:self.states,permissions_token='test',knowledge_jobs={},_lock=threading.RLock())
    def workspace(self,sid):
        return NS(knowledge_sources=selected(self.manager,sid,context(self.demo,sid)),knowledge_session=sid)
    def seed(self,source,text):
        kb=KnowledgeBase(source['root'])
        # Seed exact records; document extraction is tested separately.
        from agentplat.knowledge import tokens
        with kb.connect() as db:
            db.execute('INSERT INTO documents(id,name,sha256,kind,warnings,active) VALUES(?,?,?,?,?,1)',('d','manual.txt','hash','txt','[]'))
            db.execute('INSERT INTO chunks VALUES(?,?,?,?,?)',('c','d','line 1',0,text))
            db.execute('INSERT INTO chunk_search VALUES(?,?)',('c',' '.join(tokens(text))))
        return kb
    def test_private_project_public_boundaries(self):
        a=context(self.demo,'a');b=context(self.demo,'b');g=context(self.demo,'g')
        self.assertNotEqual(a[0]['root'],b[0]['root']);self.assertEqual(a[1]['root'],b[1]['root'])
        self.assertEqual([s['id'] for s in selected(self.manager,'g',g)],['session'])
        self.seed(a[0],'secret alpha');self.seed(a[1],'shared alpha');self.seed(a[2],'public alpha')
        ak=ScopedKnowledge(self.workspace('a'));bk=ScopedKnowledge(self.workspace('b'))
        self.assertEqual({h['scope'] for h in ak.search('alpha')['hits']},{'session','project'})
        self.assertEqual({h['scope'] for h in bk.search('alpha')['hits']},{'project'})
        with self.assertRaises(ValueError): bk.read_chunk('session:c')
        with self.assertRaises(ValueError): ak.read_chunk('public:c')
        self.assertEqual(ScopedKnowledge(self.workspace('g')).list_documents(),[])
    def test_selection_persists_and_can_disable_all(self):
        save_selection(self.demo,'a',['public'])
        self.assertEqual([s['id'] for s in self.workspace('a').knowledge_sources],['public'])
        save_selection(self.demo,'a',[])
        self.assertEqual(ScopedKnowledge(self.workspace('a')).search('alpha')['hits'],[])
        with self.assertRaises(ValueError):save_selection(self.demo,'g',['project'])
        with self.assertRaises(ValueError):context(self.demo,'unknown')
    def test_wrong_global_project_does_not_change_session_target(self):
        self.assertEqual(resolve(self.demo,dict(session='a',scope='project'))['root'],context(self.demo,'a')[1]['root'])
        self.assertNotEqual(resolve(self.demo,dict(session='a',scope='project'))['root'],resolve(self.demo,{})['root'])
    def test_snapshot_only_selected_and_remains_stable(self):
        parent=self.workspace('a');original=self.seed(parent.knowledge_sources[0],'original alpha')
        child=NS();snap=snapshot_sources(parent,child,self.root/'snapshot')
        self.assertEqual([s['scope'] for s in snap],['session','project'])
        original.remove('d')
        result=ScopedKnowledge(child).read_chunk('session:c')
        self.assertIn('original',result['text']);self.assertEqual(result['citation'],'kb:session:c')
        with self.assertRaises(ValueError): ScopedKnowledge(parent).read_chunk('session:c')
    def test_running_selection_rejected(self):
        self.demo.live_sessions['a']=(dict(self.states[0],status='running'),None,None)
        with self.assertRaisesRegex(ValueError,'运行中'):save_selection(self.demo,'a',[])
    def test_http_scope_forms_and_csrf(self):
        from agentplat.demo import make_handler
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.demo));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));opener.addheaders=[('Cookie','agentlab_access=test')]
        base=f'http://127.0.0.1:{server.server_port}'
        def post(**fields):return opener.open(base+'/knowledge/select',urllib.parse.urlencode(fields).encode()).read().decode()
        try:
            page=opener.open(base+'/knowledge?session=a').read().decode()
            self.assertIn('本会话允许检索',page);self.assertIn('name="session" value="a"',page)
            self.assertIn('name="scope" value="session"',page)
            with self.assertRaises(urllib.error.HTTPError) as error:post(session='a',token='bad')
            self.assertEqual(error.exception.code,403)
            page=post(session='a',scope='session',token='test',use_public='on')
            self.assertEqual([s['id'] for s in self.workspace('a').knowledge_sources],['public'])
            self.assertIn('name="use_public" checked',page)
        finally:server.shutdown();server.server_close();thread.join(2)

if __name__=='__main__':unittest.main()
