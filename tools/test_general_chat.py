import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import test_live_chat
from agentplat.experiments import ScriptedModel
from agentplat.general_chat import storage
from agentplat.runtime import PermissionDenied
from agentplat.tool_guards import ToolRequest
from agentplat.pages_agent import agent_page


class GeneralChatTests(unittest.TestCase):
    def test_text_assertion_negative_control_and_stale_evidence(self):
        from agentplat.workspace import Workspace
        from agentplat.session import SessionLog
        from agentplat.runtime import Evidence
        from agentplat.document_assertions import install
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as td:
            agent=SimpleNamespace(ws=Workspace(Path(td)/'files'),session=SessionLog(Path(td)/'log.jsonl'),tools={},evidence=Evidence())
            install(agent);file=agent.ws.root/'result.txt';file.write_text('correct',encoding='utf-8')
            check=agent.tools['check_file_text'].fn
            with self.assertRaises(AssertionError):check('result.txt','wrong')
            self.assertFalse(agent.evidence.valid(agent.ws.scope))
            check('result.txt','correct');self.assertTrue(agent.evidence.valid(agent.ws.scope))
            file.write_text('changed',encoding='utf-8');self.assertFalse(agent.evidence.valid(agent.ws.scope))

    def test_separate_roots_and_no_project_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            demo=test_live_chat.LiveChatTests().make_demo(Path(td))
            with patch('agentplat.llm.OpenAIChatClient',return_value=ScriptedModel()),patch.object(demo,'_run_agent_thread'):
                a=demo.start_agent_task('first general conversation',workspace_group='__general__')
                b=demo.start_agent_task('second general conversation',workspace_group='__general__')
            one=demo.live_sessions[a][1];two=demo.live_sessions[b][1]
            self.assertNotEqual(one.ws.root,two.ws.root)
            self.assertEqual(one.ws.root,storage(demo.ws_mgr,a))
            (one.ws.root/'private.txt').write_text('secret',encoding='utf-8')
            with self.assertRaises(Exception):two.ws.resolve(str(one.ws.root/'private.txt'))
            for name in ('run_shell','spawn_agent','plan_team','start_process','request_host_command','run_approved_command'):
                with self.assertRaises(PermissionDenied):two.tool_guards.check(ToolRequest(name,shell=name in ('run_shell','start_process')), {})
            view=dict(demo.live_sessions[a][0],status='stopped')
            html=agent_page(demo.ws_mgr,[],view,panel=True).decode()
            self.assertIn('普通对话 · 未绑定项目',html)
            self.assertNotIn('<h2>工作区</h2>',html)
            self.assertNotIn(str(one.ws.root),html)

    def test_project_selection_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            demo=test_live_chat.LiveChatTests().make_demo(Path(td))
            with patch('agentplat.llm.OpenAIChatClient',return_value=ScriptedModel()),patch.object(demo,'_run_agent_thread'):
                sid=demo.start_agent_task('existing explicit workspace')
            agent=demo.live_sessions[sid][1]
            self.assertEqual(agent.ws.root,Path(td)/'workspace')
            self.assertFalse(agent.ws.general_chat)

if __name__=='__main__':unittest.main()
