"""Multiple authorized roots: routing, evidence, isolation, restore and UI."""
from pathlib import Path
import sys, tempfile, unittest, threading, time
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.workspace import Workspace, WorkspaceError
from agentplat.workspaces import WorkspaceManager, WorkspaceAccessError
from agentplat.runtime import Evidence, workspace_digest
from agentplat.isolation import IsolatedChanges
from agentplat.subagents import AgentManager, TERMINAL
from agentplat.experiments import ScriptedModel
from agentplat.llmconfig import LLMConfig
from agentplat.demo import DemoServer
from agentplat.pages_agent import _thread, _sidebar
from agentplat.pages_workspaces import render


class MultipleFolders(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.folders={k:self.root/k for k in ('app','docs')}
        for p in self.folders.values():p.mkdir()
        self.ws=Workspace(self.folders)
        self.mgr=WorkspaceManager(state_path=self.root/'groups.json')
    def tearDown(self):self.temp.cleanup()

    def test_routing_and_boundary(self):
        self.ws.write_file('@docs/info.txt','secondary fact')
        self.assertEqual((self.folders['docs']/'info.txt').read_text(),'secondary fact')
        self.assertIn('secondary fact',self.ws.read_file('@docs/info.txt'))
        self.assertIn('secondary fact',self.ws.grep('secondary','@docs'))
        for path in ('@missing/a','@docs/../escape.txt','@docs/.agent-runtime/x','../escape.txt'):
            with self.assertRaises(WorkspaceError):self.ws.write_file(path,'forbidden')
        with self.assertRaises(WorkspaceError):Workspace({'a':self.root,'b':self.folders['docs']})

    def test_command_routes_without_expanding_sandbox(self):
        self.ws.execution_mode='native'
        calls=[]
        class Processes:
            def start(inner,command,cwd,**kwargs):calls.append((cwd,kwargs));return 'id'
            def wait(inner,*args):return {'status':'exited','exit_code':0,'output':'ok'}
            def release(inner,*args):pass
        self.ws.processes=Processes()
        self.ws.run('python --version',cwd='@docs')
        self.assertEqual(calls[0][0],self.folders['docs'])
        self.assertEqual(calls[0][1]['native_workspace'],self.folders['docs'])
        self.assertEqual(self.ws.last_execution['exit_code'],0)

    def test_secondary_change_invalidates_evidence(self):
        evidence=Evidence('test',0,workspace_digest(self.ws.scope))
        self.assertTrue(evidence.valid(self.ws.scope))
        self.ws.write_file('@docs/new.txt','changed')
        self.assertFalse(evidence.valid(self.ws.scope))

    def test_isolated_all_roots_and_atomic_conflict(self):
        for p in self.folders.values():(p/'file.txt').write_text('old')
        branch=IsolatedChanges(self.ws.scope,self.root/'copy')
        isolated=Workspace(branch.root)
        isolated.write_file('@app/file.txt','new app')
        isolated.write_file('@docs/file.txt','new docs')
        (self.folders['docs']/'file.txt').write_text('concurrent')
        with self.assertRaises(RuntimeError):branch.apply()
        self.assertEqual((self.folders['app']/'file.txt').read_text(),'old')
        (self.folders['docs']/'file.txt').write_text('old')
        self.assertEqual(set(branch.apply()),{'@app/file.txt','@docs/file.txt'})
        self.assertEqual((self.folders['docs']/'file.txt').read_text(),'new docs')

    def test_group_persistence_and_conflicting_tasks(self):
        key=self.mgr.save_group('Project',self.folders)
        restored=WorkspaceManager(state_path=self.mgr.state_path)
        self.assertEqual(restored.current_group,key)
        self.assertEqual(restored.current_folders(),self.folders)
        with self.assertRaises(WorkspaceAccessError):self.mgr.save_group('bad',{'a':self.root,'b':self.folders['app']})
        demo=DemoServer.__new__(DemoServer)
        demo.live_sessions={'s':({'status':'running','workspace':str(self.folders['app']),'workspace_roots':self.folders},None,None)}
        with self.assertRaises(RuntimeError):demo._check_workspace_available(self.folders['docs'])

    def test_subagent_scope_and_restore_merge(self):
        self.ws.execution_mode='local'
        factory=lambda:ScriptedModel([[('write_file',{'path':'@docs/child.txt','content':'child'})],[('run_shell',{'command':'python --version','cwd':'@docs'})],[('finish',{'summary':'done'})]])
        manager=AgentManager(LLMConfig(max_tokens=128),self.ws,self.root/'children',factory=factory)
        try:
            key=manager.spawn('write secondary file',mode='isolated')
            deadline=time.monotonic()+10
            state=manager.get(key)
            while state['status'] not in TERMINAL and time.monotonic()<deadline:
                state=manager.wait(key,.1,state['revision'])
            self.assertTrue((manager.tasks[key]['agent'].ws.roots['docs']/'child.txt').exists(),state)
            self.assertFalse((self.folders['docs']/'child.txt').exists())
            self.assertEqual(state['status'],'completed',state)
            # Exercise persisted multi-root branch reconstruction.
            manager.tasks[key]['branch']=None
            self.assertIn('@docs/child.txt',manager.apply(key)['applied'])
        finally:manager.close();manager.pool.shutdown(wait=True)

    def test_progress_and_sidebar_and_management(self):
        key=self.mgr.save_group('Combined project',self.folders)
        state={'session_id':'s','workspace':str(self.ws.root),'workspace_group':key,'status':'running','progress_messages':['Earlier **bold**'],'streamed_text':'Next output','turns':[]}
        html=_thread(state)
        self.assertIn('Earlier <strong>bold</strong>',html);self.assertIn('Next output',html)
        state.update(status='done',turns=[{'text':'task','progress_messages':['Earlier **bold**','Next output'],'summary':'Final result'}])
        html=_thread(state)
        self.assertIn('Earlier <strong>bold</strong>',html);self.assertLess(html.index('Next output'),html.index('Final result'))
        self.assertNotIn('本轮结论',html)
        self.assertIn('Combined project',_sidebar(self.mgr,[state],state,self.mgr.summary()))
        from agentplat.workspaces import DEFAULT_WORKSPACE
        ordinary={'session_id':'chat','task':'Ordinary chat','workspace':str(DEFAULT_WORKSPACE)}
        sidebar=_sidebar(self.mgr,[state,ordinary],state,self.mgr.summary())
        projects,chats=sidebar.split('<section id="sidebar-chats">')
        self.assertNotIn('Ordinary chat',projects)
        self.assertIn('Ordinary chat',chats)
        self.assertNotIn('Combined project',chats)
        self.assertIn('project=__general__',chats)
        self.assertIn(b'/workspaces/save',render(SimpleNamespace(ws_mgr=self.mgr,permissions_token='test'),{'new':'1'}))

    def test_session_restores_original_scope_after_group_edit(self):
        from tools.test_live_chat import LiveChatTests
        from unittest.mock import patch
        demo=LiveChatTests().make_demo(self.root)
        demo.ws_mgr=self.mgr
        key=self.mgr.save_group('Original',self.folders)
        model=ScriptedModel([[('finish',{'summary':'read only completed'})]])
        with patch('agentplat.llm.OpenAIChatClient',return_value=model):
            sid=demo.start_agent_task('Only inspect the provided instructions')
            deadline=time.monotonic()+5
            while demo.agent_state['status']=='running' and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(demo.agent_state['status'],'done')
            self.mgr.save_group('Changed',{'app':self.folders['app']},key)
            demo.agent_state={};demo.live_sessions.clear()
            demo.restore_agent_session(sid)
            self.assertEqual(demo.live_sessions[sid][1].ws.roots,self.folders)

    def test_explicit_project_submit_ignores_other_tab_selection(self):
        from tools.test_live_chat import LiveChatTests
        from unittest.mock import patch
        demo=LiveChatTests().make_demo(self.root);demo.ws_mgr=self.mgr
        selected=self.mgr.save_group('Selected',self.folders)
        other=self.root/'elsewhere';other.mkdir()
        self.mgr.save_group('Other tab',{'main':other})
        model=ScriptedModel([[('finish',{'summary':'finished'})]])
        with patch('agentplat.llm.OpenAIChatClient',return_value=model):
            sid=demo.start_agent_task('Read only project fixture',workspace_group=selected)
            deadline=time.monotonic()+5
            while demo.agent_state['status']=='running' and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(demo.live_sessions[sid][1].ws.roots,self.folders)
            self.assertEqual(demo.agent_state['workspace_group'],selected)

if __name__=='__main__':unittest.main(verbosity=2)
