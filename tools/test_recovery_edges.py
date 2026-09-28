from pathlib import Path
import sys, tempfile, unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.loop import CodingAgent
from agentplat.llmconfig import LLMConfig
from agentplat.workspace import Workspace
from agentplat.experiments import ScriptedModel
from agentlab.providers import ChatMessage
from agentplat.compaction import Compactor, SUMMARY_SECTIONS
from agentlab.providers import Usage


class RecoveryEdges(unittest.TestCase):
    def test_exception_then_followup_never_reuses_step_id(self):
        with tempfile.TemporaryDirectory() as td:
            agent=CodingAgent(ScriptedModel([[('finish',{'summary':'done'})]]), LLMConfig(), workspace=Workspace(Path(td)/'ws'),session_dir=Path(td)/'logs')
            def fail(*args, **kwargs):
                agent.session.append('step/start',iteration=74)
                raise ValueError('failure after model response')
            with patch.object(agent,'_turn_impl',side_effect=fail):
                with self.assertRaises(ValueError): agent._turn('fixture',[],None,fresh=False)
            self.assertEqual(agent._iter_offset,75)
            self.assertEqual(agent.session.of_kind('step/end')[-1].data['iteration'],74)
            agent._iter_offset=68  # stale pre-exception memory from the production failure
            agent._conversation=[ChatMessage('user','Read only task')]
            agent.continue_with('Report only')
            self.assertEqual(agent.session.of_kind('step/start')[-1].data['iteration'],75)
            agent.restore_conversation(agent.session.path)
            self.assertGreaterEqual(agent._iter_offset,76)
            agent.continue_with('Report again')
            ids=[e.data['iteration'] for e in agent.session.of_kind('step/start')]
            self.assertEqual(ids,sorted(set(ids)))

    def test_summary_keeps_entire_multi_tool_batch(self):
        class Summarizer:
            def complete(self,*args): return '\n'.join(s+'：fixture' for s in SUMMARY_SECTIONS),Usage(10,10,0)
        messages=[ChatMessage('system','rules'),ChatMessage('user','task')]
        messages += [ChatMessage('assistant','old')]*4
        messages += [ChatMessage('assistant','',tool_calls=[{'id':'a'},{'id':'b'}]),ChatMessage('tool','a',tool_call_id='a'),ChatMessage('tool','b',tool_call_id='b'),ChatMessage('assistant','recent')]
        compactor=Compactor(Summarizer(),LLMConfig())
        result=compactor.summarize(messages,3)
        self.assertTrue(result)
        self.assertEqual(messages[-4].tool_calls,[{'id':'a'},{'id':'b'}])


if __name__=='__main__': unittest.main(verbosity=2)
