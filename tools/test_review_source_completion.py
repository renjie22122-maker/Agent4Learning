import json,tempfile,unittest,time
from pathlib import Path
from agentplat.experiments import ScriptedModel
from agentplat.llmconfig import LLMConfig
from agentplat.loop import CodingAgent
from agentplat.workspace import Workspace
from agentplat.subagents import AgentManager
from agentplat.knowledge import KnowledgeBase,snapshot


class ReviewSourceCompletionTests(unittest.TestCase):
    def test_author_transcript_is_not_independent_source(self):
        from types import SimpleNamespace
        from agentplat.independent_review import check
        with tempfile.TemporaryDirectory() as td:
            result={'status':'completed','summary':json.dumps({'verdict':'pass','tests':['read transcript'],'findings':[]}), 'evidence':[{'exit_code':0}]}
            manager=SimpleNamespace(spawn=lambda *a,**k:'child',get=lambda *a:result)
            session=SimpleNamespace(append=lambda *a,**k:None,events=[SimpleNamespace(kind='tool/call',data={'tool':'search_knowledge'})])
            agent=SimpleNamespace(ws=Workspace(td),cfg=LLMConfig(),_task_text='查知识库',_files_touched=['result.json'],_finish_rejects=0,session=session,child_manager=lambda:manager)
            self.assertEqual(check(agent).by,'独立验收受阻')
            result['knowledge_reads']=[{'citation':'kb:source','sha256':'host-observed'}]
            self.assertTrue(check(agent).allow)

    def test_snapshot_keeps_original_after_source_reimport(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'policy.txt';source.write_text('hotel 680')
            kb=KnowledgeBase(root/'kb');kb.import_file(source)
            citation=kb.search('hotel')['hits'][0]['id']
            snapshot(kb.root,root/'snapshot')
            source.write_text('hotel 999');kb.import_file(source)
            self.assertEqual(KnowledgeBase(root/'snapshot').read_chunk(citation)['text'],'hotel 680')
            with self.assertRaises(ValueError):kb.read_chunk(citation)

    def test_pass_closes_on_last_allowed_model_step(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);ws=Workspace(root/'ws');ws.execution_mode='local';cfg=LLMConfig(max_tokens=512)
            model=ScriptedModel([[('write_file',{'path':'value.txt','content':'680'})],
                [('run_shell',{'command':'python -c "print(680)"'})],
                [('finish',{'summary':'已写入并验证 value.txt'})]])
            verdict=json.dumps({'verdict':'pass','tests':['read and assert value'],'findings':[]})
            manager=AgentManager(cfg,ws,root/'children',factory=lambda:ScriptedModel([
                [('run_shell',{'command':'python -c "from pathlib import Path; assert Path(\'value.txt\').read_text()==\'680\'"'})],
                [('finish',{'summary':verdict})]],delay=.1))
            agent=CodingAgent(model,cfg,workspace=ws,session_dir=root/'logs',enable_subagents=False,hard_iterations=3)
            agent.independent_review_required=True;agent.child_manager=lambda:manager
            manager.run_deadline=time.monotonic()+30
            try:
                result=agent.run('创建 value.txt 内容为 680，并验证')
                self.assertTrue(result.ok,result.error);self.assertEqual(model.turn,3)
                self.assertEqual(result.stopped_by,'finish')
                self.assertEqual(len(agent.session.of_kind('session/closed')),1)
                self.assertEqual(len(agent.session.of_kind('step/start')),len(agent.session.of_kind('step/end')))
                child=next(iter(manager.tasks.values()))['agent']
                self.assertIn('read_knowledge_chunk',child.tools)
                self.assertTrue(child.session.of_kind('budget/status'))
                self.assertLessEqual(child.session.of_kind('budget/status')[0].data['remaining_run_seconds'],30)
            finally:manager.close();manager.pool.shutdown(wait=True)


if __name__=='__main__':unittest.main()
