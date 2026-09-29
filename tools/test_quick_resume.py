import sys, tempfile, threading, unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.session import SessionLog
from agentplat.quick_resume import resume


class QuickResumeTests(unittest.TestCase):
    def test_steps_default_collapsed_but_full_details_remain_available(self):
        from agentplat.pages_agent import _trace
        steps=[dict(kind='tool',title=f'step-{i}',detail='x'*200+'END') for i in range(12)]
        html=_trace(steps)
        self.assertEqual(html.count(' open>'),0)
        self.assertEqual(html.count('<details'),13)
        self.assertEqual(html.count('class="step-body"'),12)
        self.assertEqual(html.count('class="step-preview-line"'),3)
        from html.parser import HTMLParser
        class Preview(HTMLParser):
            def __init__(self):super().__init__();self.depth=0;self.parts=[]
            def handle_starttag(self,tag,attrs):
                if ('class','step-preview') in attrs:self.depth=1
                elif self.depth:self.depth+=1
            def handle_endtag(self,tag):
                if self.depth:self.depth-=1
            def handle_data(self,data):
                if self.depth:self.parts.append(data)
        preview=Preview();preview.feed(html);text=''.join(preview.parts)
        self.assertIn('step-11',text)
        self.assertNotIn('step-8',text)
        self.assertIn('step-0',html)
        self.assertNotIn('展开全部',html)

    def fixture(self, directory):
        log = SessionLog(Path(directory)/'fixture.jsonl')
        log.append('conversation/message', message={'role':'user','content':'完成任务'})
        log.append('run/started', max_iters=31, max_usd=0.4)
        log.flush('test')
        calls=[]
        state={'status':'stopped'}
        def continuation(*args, **kwargs):
            calls.append(kwargs);state['status']='running';return 'fixture'
        agent=SimpleNamespace(hard_iterations=0,guard=SimpleNamespace(max_usd=None))
        demo=SimpleNamespace(_lock=threading.RLock(),live_sessions={'fixture':(state,agent,None)},
            list_agent_sessions=lambda n:[{'session_id':'fixture','log_path':str(log.path)}],
            continue_agent_task=continuation)
        return demo,log,calls

    def test_continue_preserves_budget_and_double_click_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            demo,log,calls=self.fixture(td)
            self.assertEqual(resume(demo,'fixture'),'fixture')
            self.assertEqual(resume(demo,'fixture'),'fixture')
            self.assertEqual(len(calls),1)
            self.assertEqual(calls[0]['max_iters'],31)
            self.assertEqual(calls[0]['max_usd'],0.4)

    def test_unknown_operation_blocks_without_starting(self):
        with tempfile.TemporaryDirectory() as td:
            demo,log,calls=self.fixture(td)
            log.append('tool/call',call_id='unknown',tool='write_file',destructive=True)
            log.flush('test')
            with self.assertRaisesRegex(ValueError,'结果未知'):resume(demo,'fixture')
            self.assertEqual(calls,[])

    def test_corrupt_log_blocks_without_starting(self):
        with tempfile.TemporaryDirectory() as td:
            demo,log,calls=self.fixture(td)
            with log.path.open('a',encoding='utf-8') as f:f.write('broken\n')
            with self.assertRaisesRegex(ValueError,'日志损坏'):resume(demo,'fixture')
            self.assertEqual(calls,[])

if __name__=='__main__':unittest.main()
