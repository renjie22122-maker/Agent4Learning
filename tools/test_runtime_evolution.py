import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import tempfile,unittest,time,json,os
from unittest.mock import patch
from agentplat.session import SessionLog
from agentplat.review_policy import decide
from agentplat.delegation import decide as delegate
from agentplat.benchmark import TASKS,grade

REFERENCES={
'json_merge':'''import copy
def merge(a,b):
 out=copy.deepcopy(a)
 for k,v in b.items():
  if v is None:out.pop(k,None)
  elif isinstance(v,dict):out[k]=merge(out.get(k,{}) if isinstance(out.get(k),dict) else {},v)
  else:out[k]=copy.deepcopy(v)
 return out
''',
'topological_order':'''def order(edges):
 graph={k:set(v) for k,v in edges.items()}
 for v in list(graph.values()):
  for x in v:graph.setdefault(x,set())
 out=[]
 while graph:
  ready=sorted(k for k,v in graph.items() if not v)
  if not ready:raise ValueError()
  k=ready[0];out.append(k);del graph[k]
  for v in graph.values():v.discard(k)
 return out
''',
'safe_paths':'''def safe_relative(p):
 if not p or any(c in p for c in ('\\\\',':','\\x00')) or any(x in ('','.','..') for x in p.split('/')):raise ValueError()
 return p
''',
'unicode_counts':'''import unicodedata,collections
def counts(text):return dict(collections.Counter(w.casefold() for w in unicodedata.normalize('NFC',text).split()))
''',
'date_ranges':'''from datetime import date,timedelta
def days(start,end):
 a,b=date.fromisoformat(start),date.fromisoformat(end)
 return [(a+timedelta(days=i)).isoformat() for i in range((b-a).days+1)]
''',
'followup_pagination':'''def page(items,number,size):
 if number<1 or size<1:raise ValueError()
 return items[(number-1)*size:number*size]
def pages(items,size):
 if size<1:raise ValueError()
 return [items[i:i+size] for i in range(0,len(items),size)]
'''}


class EvolutionTests(unittest.TestCase):
 def test_rag_graders_reject_obsolete_citations(self):
  from agentplat.knowledge import KnowledgeBase,database_root
  for task in ('rag_conflict','rag_large'):
   with self.subTest(task=task),tempfile.TemporaryDirectory() as td,patch.dict(os.environ,{'AGENTLAB_KB_DIR':td}):
    root=Path(td);ws=root/'ws';ws.mkdir();kb=KnowledgeBase(database_root(ws))
    for name,text in [('current.txt',TASKS[task]['knowledge']['current.txt']),('old.txt','已废止住宿限额400元')]:
     path=root/name;path.write_text(text,encoding='utf-8');kb.import_file(path)
    current=next(h['citation'] for h in kb.search('住宿',10)['hits'] if h['name']=='current.txt')
    old=next(h['citation'] for h in kb.search('住宿',10)['hits'] if h['name']=='old.txt')
    for citation,expected in ((current,True),(old,False),('kb:invented',False)):
     (ws/'result.json').write_text(json.dumps(dict(hotel_limit=TASKS[task]['expected_limit'],currency='CNY',source=citation)),encoding='utf-8')
     self.assertEqual(grade(task,ws,root/'grader')[0],expected)

 def test_statistical_report_keeps_timeouts_and_unknown_cost(self):
  from tools.analyze_stratified_benchmark import analyze
  report=analyze({'results':[dict(task='a',category='coding',passed=True,all_turns_completed=True,elapsed_s=1,total_usd_estimate=.1),
                             dict(task='a',category='coding',passed=False,classification='timeout')]})
  self.assertEqual(report['overall']['n'],2)
  self.assertEqual(report['overall']['workflow_passed'],1)
  self.assertIsNone(report['overall']['total_cost_usd'])
  self.assertGreater(report['tasks']['a']['workflow_wilson95'][1],.5)

 def test_new_graders_positive_and_negative(self):
  for name,source in REFERENCES.items():
   for correct in (True,False):
    with self.subTest(task=name,correct=correct),tempfile.TemporaryDirectory() as td:
     root=Path(td);ws=root/'ws';ws.mkdir()
     (ws/TASKS[name]['artifact']).write_text(source if correct else 'raise RuntimeError("negative control")',encoding='utf-8')
     result,details=grade(name,ws,root/'grader')
     self.assertEqual(result,correct,details)

 def test_review_policy_never_relaxes_unknown_code_or_sources(self):
  self.assertEqual(decide('balanced',['a.md']).level,'evidence')
  for kw in ({'changed':['a.py']},{'changed':['a.md'],'unknown_changes':True},{'changed':['a.md'],'source_used':True}):
   self.assertEqual(decide('balanced',**kw).level,'independent')
  self.assertEqual(decide('strict',['a.md']).level,'independent')

 def test_projection_survives_reload_and_keeps_unknown_effects(self):
  with tempfile.TemporaryDirectory() as td:
   log=SessionLog(Path(td)/'s.jsonl');log.append('run/started');log.append('tool/call',call_id='write',destructive=True)
   log.append('run/started');log.append('human/requested',question_id='q')
   projected=log.project_run();reloaded,_=SessionLog.load(log.path)
   self.assertEqual(projected,reloaded.project_run());self.assertIn('write',projected['unresolved_effects'])
   self.assertEqual(projected['phase'],'waiting_user');self.assertEqual(projected['tool_calls'],0)
   log.append('delivery/finalized',acceptance={'status':'passed'},author_summary='old')
   log.append('followup/user',text='new constraints')
   self.assertEqual(log.project_run()['acceptance'],{})

 def test_delegation_requires_measured_benefit(self):
  self.assertEqual(delegate('task','output','')['action'],'local')
  observations=[dict(category='general',model='m',mode=mode,passed=True,elapsed_s=seconds,total_usd=.1,independently_graded=True)
                for mode,seconds in [('local',20),('delegate',10)] for _ in range(3)]
  self.assertEqual(delegate('task','output','',model='m',evidence={'observations':observations})['action'],'delegate')
  self.assertEqual(delegate('task','output','',parent_task='task',model='m',evidence={'observations':observations})['action'],'local')
  observations[-1]['passed']=False
  self.assertEqual(delegate('task','output','',model='m',evidence={'observations':observations})['action'],'local')

 def test_provider_contract_rejects_missing_enforcement(self):
  from agentplat.subagent_providers import Provider,ProviderRegistry
  with self.assertRaises(ValueError):ProviderRegistry().register(Provider('unsafe',lambda:None,frozenset()))

 def test_registered_provider_is_used_by_real_manager(self):
  from agentplat.subagent_providers import Provider,default_registry,REQUIRED
  from agentplat.loop import CodingAgent
  from agentplat.subagents import AgentManager
  from agentplat.llmconfig import LLMConfig
  from agentplat.workspace import Workspace
  from agentplat.experiments import ScriptedModel
  calls=[]
  def create(*a,**kw):calls.append(True);return CodingAgent(*a,**kw)
  registry=default_registry();registry.register(Provider('instrumented',create,REQUIRED))
  with tempfile.TemporaryDirectory() as td:
   manager=AgentManager(LLMConfig(),Workspace(Path(td)/'ws'),Path(td)/'team',factory=ScriptedModel,providers=registry)
   try:
    key=manager.spawn('简短只读回答',provider='instrumented')
    result=manager.wait(key,10)
    deadline=time.monotonic()+10
    while result['status'] not in ('completed','failed','cancelled') and time.monotonic()<deadline:
     result=manager.wait(key,1,after_revision=result.get('revision',-1))
    self.assertEqual(result['status'],'completed',result);self.assertEqual(calls,[True])
   finally:manager.close();manager.pool.shutdown(wait=True)

if __name__=='__main__':unittest.main()
