"""Deterministic web-session/worker concurrency tests without paid model calls."""
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.demo import DemoServer
from agentplat.experiments import ScriptedModel
from agentplat.config import PlatformConfig
from agentplat.llmconfig import LLMConfig
from agentplat.pages_agent import _thread, _composer


class BlockingModel(ScriptedModel):
    def __init__(self, finish_first=False):
        super().__init__([[('finish', {'summary': 'First answer'})]] if finish_first else
                         [[('list_dir', {})], [('finish', {'summary': 'Final answer'})]])
        self.entered = threading.Event(); self.release = threading.Event(); self.messages = []

    def complete_with_tools(self, model, messages, tools, timeout):
        self.messages.append([m.to_api() for m in messages])
        if self.turn == 0:
            self.entered.set()
            if not self.release.wait(5): raise TimeoutError('test did not release model')
        return super().complete_with_tools(model, messages, tools, timeout)


class LiveChatTests(unittest.TestCase):
    def make_demo(self, root):
        demo = DemoServer.__new__(DemoServer)
        demo._lock = threading.RLock(); demo.agent_state = {}; demo.live_sessions = {}
        demo._agent = None; demo._stop_flag = threading.Event()
        demo.cfg = PlatformConfig(); demo.llm_cfg = LLMConfig(provider='real', model='fixture', base_url='https://invalid.example')
        from agentplat.workspaces import WorkspaceManager
        demo.ws_mgr = WorkspaceManager(state_path=root/'workspaces.json')
        demo.ws_mgr.current = root/'workspace'
        demo.ws_mgr.session_directories = lambda: {root/'.sessions',root/'workspace'/'.sessions'}
        return demo

    def wait_done(self, demo):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with demo._lock:
                if demo.agent_state.get('status') != 'running': break
            time.sleep(.02)
        self.assertEqual(demo.agent_state['status'], 'done', demo.agent_state)

    def exercise(self, finish_first):
        with tempfile.TemporaryDirectory() as td:
            demo = self.make_demo(Path(td)); model = BlockingModel(finish_first)
            with patch('agentplat.llm.OpenAIChatClient', return_value=model):
                sid = demo.start_agent_task('Read only inspection')
                self.assertTrue(model.entered.wait(3))
                demo.continue_agent_task('Focus on the blue interface', session_id=sid)
                state, _ = demo.view_agent_session(sid)
                self.assertEqual(state['pending'][0]['status'], 'queued')
                self.assertNotIn('textarea disabled', _composer(state, True))
                model.release.set(); self.wait_done(demo)
                self.assertTrue(any('blue interface' in str(messages) for messages in model.messages[1:]))
                self.assertFalse(demo.agent_state['pending'])
                self.assertEqual(len(demo.agent_state['turns']), 2 if finish_first else 1)
                # Starting another task must retain the earlier full conversation.
                old_agent = demo._agent
                newer = demo.start_agent_task('Another read only inspection')
                self.wait_done(demo)
                old_view, _ = demo.view_agent_session(sid)
                self.assertEqual(demo.agent_state['session_id'], newer)
                self.assertIs(demo.live_sessions[sid][1], old_agent)
                self.assertIn('Read only inspection', _thread(old_view))
                self.assertEqual(len({s['session_id'] for s in demo.list_agent_sessions()}), 2)
                demo.live_sessions.clear()
                historical, _ = demo.view_agent_session(sid)
                self.assertTrue(historical['historical'])
                self.assertIn('Read only inspection', _thread(historical))
                demo.continue_agent_task('Continue with prior findings', session_id=sid)
                self.wait_done(demo)
                self.assertEqual(demo.agent_state['session_id'], sid)
                self.assertTrue(any('Continue with prior findings' in str(m) for m in model.messages))

    def test_steering_at_next_step(self): self.exercise(False)
    def test_message_arriving_during_finish_is_not_lost(self): self.exercise(True)


if __name__ == '__main__': unittest.main(verbosity=2)
