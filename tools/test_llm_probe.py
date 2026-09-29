import sys,json,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.llm import OpenAIChatClient
from agentplat.llmconfig import LLMConfig
from types import SimpleNamespace


class ProbeTests(unittest.TestCase):
    def test_probe_prompt_satisfies_json_response_contract(self):
        client=OpenAIChatClient(LLMConfig())
        def complete(model,messages,timeout):
            self.assertIn('json',messages[0].content.lower())
            return '{"reply":"好"}',SimpleNamespace(in_tokens=10,out_tokens=5)
        client.complete=complete
        result=client.probe('fixture')
        self.assertTrue(result['ok'])
        self.assertEqual(json.loads(result['reply']),{'reply':'好'})

if __name__=='__main__':unittest.main()
