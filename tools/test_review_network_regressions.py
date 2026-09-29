"""Regressions from the September 29 review/network incident; no external API."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agentplat.sources import SourceStore
from agentplat.independent_review import status_question, wait_pending, retire
from agentplat.review_evidence import snapshot, intact
from agentplat.windows_command import prepare


class NetworkRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = SourceStore(self.temp.name, ['example.com'])
        self.addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2606:4700:4700::1111', 80, 0, 0)),
                          (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 80))]

    def fetch(self, sockets, raw=b'hello', **kw):
        with patch('agentplat.sources.socket.getaddrinfo', return_value=self.addresses) as dns, \
             patch('agentplat.sources.socket.socket', side_effect=sockets), \
             patch('agentplat.sources.http.client.HTTPConnection') as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200; response.read.return_value = raw
            response.getheader.return_value = 'text/plain'
            result = self.store.fetch('http://example.com', **kw)
            self.assertEqual(dns.call_count, 1)
            return result

    def test_ipv6_uses_full_sockaddr(self):
        sock = MagicMock(); result = self.fetch([sock])
        sock.connect.assert_called_once_with(self.addresses[0][4])
        self.assertTrue(result['complete'])

    def test_fallback_to_validated_ipv4(self):
        first, second = MagicMock(), MagicMock()
        first.connect.side_effect = OSError('IPv6 unavailable')
        self.fetch([first, second]); first.close.assert_called_once()
        second.connect.assert_called_once_with(self.addresses[1][4])

    def test_mixed_private_dns_never_connects(self):
        self.addresses[1] = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1',80))
        sock=MagicMock()
        with self.assertRaises(PermissionError): self.fetch([sock])
        sock.connect.assert_not_called()

    def test_truncated_preview_is_not_complete_evidence(self):
        result = self.fetch([MagicMock()], raw=b'abcdef', max_bytes=5, allow_truncated=True)
        self.assertFalse(result['complete']); self.assertEqual(result['bytes'],5)
        verdict=self.store.validate_claims([{'source_id':result['source_id'],'quote':'abc'}],1)
        self.assertFalse(verdict['complete'])

    def test_default_still_rejects_oversized_source(self):
        with self.assertRaisesRegex(RuntimeError,'SIZE_LIMIT'):
            self.fetch([MagicMock()],raw=b'abcdef',max_bytes=5)

    def test_dns_errors_are_classified(self):
        with patch('agentplat.sources.socket.getaddrinfo',side_effect=socket.gaierror()):
            with self.assertRaisesRegex(RuntimeError,'DNS_ERROR'):self.store.fetch('http://example.com')


class ReviewRegressions(unittest.TestCase):
    def test_requirement_update_cancels_review_and_finishes_new_snapshot(self):
        from agentplat.loop import CodingAgent
        from agentplat.experiments import ScriptedModel
        from agentplat.llmconfig import LLMConfig
        from agentplat.workspace import Workspace
        from agentplat.subagents import AgentManager
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); ws=Workspace(root/'ws');ws.execution_mode='local';cfg=LLMConfig(max_tokens=512)
            summary=json.dumps({'verdict':'pass','tests':['checked file'],'findings':[]})
            manager=AgentManager(cfg,ws,root/'children',factory=lambda:ScriptedModel([
                [('run_shell',{'command':'python -c "print(1)"'})], [('finish',{'summary':summary})]],delay=.2))
            script=[]
            for value in ('old','new'):
                script += [[('write_file',{'path':'value.txt','content':value})],
                    [('run_shell',{'command':'python -c "print(1)"'})], [('finish',{'summary':'完成 value.txt 并测试'})]]
            agent=CodingAgent(ScriptedModel(script),cfg,workspace=ws,session_dir=root/'logs',enable_subagents=False,hard_iterations=8)
            agent.children=manager; agent.child_manager=lambda:manager; agent.independent_review_required=True
            sent=[False]
            def steering():
                if getattr(agent,'_independent_review',None) and not sent[0]:
                    sent[0]=True;return ['将 value.txt 改为 new，然后验证']
                return []
            agent.steering=steering
            try:
                result=agent.run('创建 value.txt 并验证')
                self.assertTrue(result.ok,result.error)
                self.assertEqual((ws.root/'value.txt').read_text(),'new')
                self.assertTrue(agent.session.of_kind('independent_review/superseded'))
                self.assertFalse(any(e.data['by']=='子任务未收尾' for e in agent.session.of_kind('reflection/rejected')))
                self.assertEqual(len(manager.tasks),2)
            finally:manager.close();manager.pool.shutdown(wait=True)

    def test_status_question_does_not_eat_requirement(self):
        self.assertTrue(status_question('谁在验收？'))
        self.assertTrue(status_question('!\n等待独立验收\n宿主正在等待验收结果\n你在等谁验收？'))
        self.assertFalse(status_question('谁在验收？另外把输出改成 CSV'))

    def test_status_query_keeps_wait_and_task(self):
        events=[]; count=[0]
        state={'agent_id':'v','status':'running','revision':1,'created_at':0}
        def waiting(*a,**kw):state['status']='completed'
        def steering():
            count[0]+=1
            return ['谁在验收？'] if count[0]==1 else []
        manager=SimpleNamespace(get=lambda _:dict(state),wait=waiting)
        agent=SimpleNamespace(_independent_review={'agent_id':'v'},_task_text='original',
            child_manager=lambda:manager,stop_flag=threading.Event(),steering=steering,
            session=SimpleNamespace(append=lambda kind,**kw:events.append((kind,kw))))
        self.assertEqual(wait_pending(agent),[])
        self.assertEqual(agent._task_text,'original')
        self.assertTrue(any(k=='independent_review/status' for k,_ in events))
        self.assertEqual(agent._independent_review['agent_id'],'v')

    def test_retire_cancels_and_preserves_result(self):
        manager=SimpleNamespace(get=lambda _: {'status':'running','summary':'partial'},cancel=MagicMock())
        agent=SimpleNamespace(_independent_review={'agent_id':'v'},child_manager=lambda:manager,
                              session=SimpleNamespace(append=MagicMock()))
        retire(agent,'new requirement');manager.cancel.assert_called_once_with('v')
        self.assertIsNone(agent._independent_review)
        self.assertEqual(agent._previous_review['summary'],'partial')

    def test_hidden_review_cannot_trigger_generic_child_gate(self):
        from agentplat.loop import CodingAgent
        from agentplat.experiments import ScriptedModel
        from agentplat.llmconfig import LLMConfig
        from agentplat.workspace import Workspace
        with tempfile.TemporaryDirectory() as td:
            agent=CodingAgent(ScriptedModel(),LLMConfig(),workspace=Workspace(Path(td)/'ws'),
                session_dir=Path(td)/'logs',enable_subagents=False,reflection=False)
            agent.children=SimpleNamespace(tasks={'v':{'data':{'purpose':'verification','status':'running'}}})
            self.assertTrue(agent._review_finish({}).allow)
            agent.children.tasks['w']={'data':{'purpose':'work','status':'running'}}
            self.assertEqual(agent._review_finish({}).by,'子任务未收尾')

    def test_only_referenced_evidence_copied_and_tamper_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/'author'; target=root/'review'
            (source/'.spill').mkdir(parents=True);target.mkdir()
            (source/'.spill/web-a.txt').write_text('official source')
            (source/'.spill/private.txt').write_text('unrelated history')
            (target/'report.md').write_text('Evidence .spill/web-a.txt and .spill/missing.txt')
            manifest=snapshot(source,target)
            self.assertTrue((target/'.spill/web-a.txt').exists())
            self.assertFalse((target/'.spill/private.txt').exists())
            self.assertTrue(any(i.get('missing') for i in manifest))
            self.assertTrue(intact(target,manifest))
            (target/'.spill/web-a.txt').write_text('tampered')
            self.assertFalse(intact(target,manifest))

    def test_unrelated_historical_reference_not_required(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'author';target=root/'review'
            source.mkdir();target.mkdir()
            (target/'old.md').write_text('Old source .spill/no-longer-present.txt')
            (target/'new.md').write_text('No source needed')
            self.assertEqual(snapshot(source,target,['new.md']),[])


class WindowsCommandRegressions(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows cmd regression')
    def test_multiline_python_really_executes_all_lines(self):
        cmd=prepare('python -u -c "print(\'A\')\nprint(\'B\')\nraise SystemExit(7)"')
        result=subprocess.run(cmd,shell=True,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.stdout.splitlines(),['A','B']);self.assertEqual(result.returncode,7)

    def test_compound_multiline_rejected_before_execution(self):
        with self.assertRaisesRegex(ValueError,'WINDOWS_MULTILINE'):
            prepare('python -c "print(1)\nprint(2)" ; echo done')


if __name__=='__main__': unittest.main()
