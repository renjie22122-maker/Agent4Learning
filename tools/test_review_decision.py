import sys,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat import independent_review
from agentplat.review_decision import assess
from agentplat.runtime import workspace_digest
from agentplat.workspace import Workspace
from agentplat.llmconfig import LLMConfig

class DecisionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.ws=Workspace(Path(self.tmp.name)/'ws');(self.ws.root/'code.py').write_text('x=1')
  self.result={'status':'completed','summary':json.dumps({'verdict':'pass','tests':['independent'],'findings':[]}), 'evidence':[{'exit_code':0}],'changes':[],'token_budget':0,'used_tokens':10000}
  self.spawns=[]
  self.manager=SimpleNamespace(get=lambda key:self.result,spawn=lambda *a,**k:self.spawns.append(k) or str(len(self.spawns)))
  self.agent=SimpleNamespace(ws=self.ws,cfg=LLMConfig(),session=SimpleNamespace(events=[],append=lambda *a,**k:None),child_manager=lambda:self.manager,_task_text='task',_finish_rejects=0)
  self.record={'agent_id':'old','digest':workspace_digest(self.ws.scope),'task':'task'};self.agent._independent_review=self.record
 def test_status_and_check_agree_about_modified_fixture(self):
  self.result['changes']=[{'path':'scratch/sample.docx','before':'old','after':'new'}]
  state=independent_review.status(self.agent);v=independent_review.check(self.agent)
  self.assertFalse(state['passed']);self.assertFalse(v.allow)
  self.assertEqual(state['host_decision']['code'],'existing_files_modified')
  self.assertIn('scratch/sample.docx',v.instruction);self.assertNotIn('预算',v.instruction)
  self.assertTrue(state['budget']['unlimited'])
 def test_pass_retry_reuses_without_spawning(self):
  self.assertTrue(independent_review.retry(self.agent)['reused']);self.assertEqual(self.spawns,[])
 def test_mutation_retry_does_not_blindly_repeat(self):
  self.result['changes']=[{'path':'code.py','before':'old'}]
  self.assertFalse(independent_review.retry(self.agent)['passed']);self.assertEqual(self.spawns,[])
 def test_timeout_not_budget(self):
  self.result.update(status='failed',error='LLMCallError: [TIMEOUT]')
  d=assess(self.agent,self.record,self.result);self.assertEqual(d['code'],'timeout');self.assertNotIn('预算',d['reason'])
 def test_real_budget_and_invalid_report_are_distinct(self):
  self.result['status']='budget_exceeded';self.assertEqual(assess(self.agent,self.record,self.result)['code'],'budget_exceeded')
  self.result.update(status='completed',summary='broken');self.assertEqual(assess(self.agent,self.record,self.result)['code'],'invalid_report')
 def test_new_test_files_allowed_but_deleted_inputs_rejected(self):
  self.result['changes']=[{'path':'verification-123/new.json','before':None,'after':'new'}]
  self.assertTrue(assess(self.agent,self.record,self.result)['accepted'])
  self.result['changes'].append({'path':'code.py','before':'old','after':None})
  self.assertFalse(assess(self.agent,self.record,self.result)['accepted'])
 def test_version_or_requirement_changes_invalidate(self):
  self.agent._acceptance_task='new';self.assertEqual(assess(self.agent,self.record,self.result)['code'],'stale_requirements')
  self.agent._acceptance_task='task';(self.ws.root/'code.py').write_text('x=2')
  self.assertEqual(assess(self.agent,self.record,self.result)['code'],'stale_artifact')

 def test_restored_review_identity_is_rechecked(self):
  self.agent.session.events=[SimpleNamespace(kind='independent_review/started',data=self.record)]
  self.agent._independent_review=None
  independent_review.restore_record(self.agent)
  self.assertTrue(independent_review.status(self.agent)['passed'])
  (self.ws.root/'code.py').write_text('x=3')
  self.assertFalse(independent_review.status(self.agent)['passed'])
 def test_superseded_review_is_not_restored(self):
  self.agent.session.events=[SimpleNamespace(kind='independent_review/started',data=self.record),SimpleNamespace(kind='independent_review/superseded',data={})]
  self.agent._independent_review=None;independent_review.restore_record(self.agent)
  self.assertIsNone(self.agent._independent_review)

if __name__=='__main__':unittest.main(verbosity=2)
