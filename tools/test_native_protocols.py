import unittest
from dataclasses import replace
from agentlab.providers import ChatMessage
from agentplat.llmconfig import LLMConfig
from agentplat.native_protocols import encode,decode
from agentplat.model_client import create_client
from agentplat.llm_errors import LLMCallError


class NativeProtocolTests(unittest.TestCase):
    def test_roundtrip_opaque_blocks_and_usage(self):
        cases=[('openai_responses',dict(status='completed',output=[
            dict(type='reasoning',id='r',encrypted_content='opaque'),
            dict(type='function_call',call_id='a',name='read_file',arguments='{"path":"x"}')],
            usage=dict(input_tokens=12,output_tokens=3,input_tokens_details=dict(cached_tokens=4)))),
            ('anthropic',dict(stop_reason='tool_use',content=[dict(type='thinking',thinking='t',signature='opaque'),
                dict(type='tool_use',id='a',name='read_file',input=dict(path='x'))],
                usage=dict(input_tokens=8,cache_read_input_tokens=4,output_tokens=3))),
            ('gemini',dict(candidates=[dict(finishReason='STOP',content=dict(parts=[
                dict(functionCall=dict(id='a',name='read_file',args=dict(path='x')),thoughtSignature='opaque')]))],
                usageMetadata=dict(promptTokenCount=12,candidatesTokenCount=2,thoughtsTokenCount=1,cachedContentTokenCount=4)))]
        for mode,raw in cases:
            with self.subTest(mode=mode):
                text,calls,usage,measured=decode(mode,raw)
                self.assertEqual((usage.in_tokens,usage.out_tokens,usage.cached_tokens),(12,3,4))
                self.assertTrue(measured)
                messages=[ChatMessage('user','Read x'),ChatMessage('assistant',text,tool_calls=calls),
                          ChatMessage('tool','contents',tool_call_id='a')]
                body=encode(LLMConfig(transport=mode,json_mode=False,reasoning_effort='',stream_tools=False),'model',messages,[])
                import json
                self.assertIn('opaque',json.dumps(body));self.assertIn('contents',json.dumps(body))

    def test_incomplete_response_never_releases_calls(self):
        for mode,raw in [('openai_responses',dict(status='incomplete')),('anthropic',dict(stop_reason='max_tokens')),
                         ('gemini',dict(candidates=[dict(finishReason='MAX_TOKENS')]))]:
            with self.subTest(mode=mode),self.assertRaises(ValueError):decode(mode,raw)

    def test_unsupported_options_fail_before_network(self):
        for mode in ('anthropic','gemini','openai_responses'):
            cfg=LLMConfig(transport=mode,stream_tools=True)
            with self.assertRaises(LLMCallError):create_client(cfg).complete('m',[ChatMessage('user','hi')],1)


if __name__=='__main__':unittest.main()
