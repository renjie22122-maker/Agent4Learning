from pathlib import Path
import sys
import json
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.streaming import assemble


def event(delta, finish=None):
    return ('data: ' + json.dumps({'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}) + '\n').encode()


class StreamingTests(unittest.TestCase):
    def test_fragmented_tool_and_text(self):
        seen = []
        result = assemble([event({'content':'你好'}), event({'tool_calls':[{'index':0,'id':'call1','function':{'name':'finish','arguments':'{"sum'}}]}),
                           event({'tool_calls':[{'index':0,'function':{'arguments':'mary":"完成"}'}}]}, 'tool_calls'), b'data: [DONE]\n'], seen.append)
        self.assertEqual(seen, ['你好'])
        call = result['choices'][0]['message']['tool_calls'][0]
        self.assertEqual(json.loads(call['function']['arguments']), {'summary':'完成'})

    def test_truncated_stream_never_returns_tools(self):
        with self.assertRaises(ValueError): assemble([event({'tool_calls':[{'index':0,'function':{'name':'write_file','arguments':'{'}}]})])


if __name__ == '__main__': unittest.main(verbosity=2)
