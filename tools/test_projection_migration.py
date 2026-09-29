import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import tempfile,unittest,json
from agentplat.session import SessionLog,replay
from agentplat.session_projection import project_session


class ProjectionMigration(unittest.TestCase):
 def test_current_recovery_fields_match_on_valid_histories(self):
  with tempfile.TemporaryDirectory() as td:
   log=SessionLog(Path(td)/'s.jsonl')
   log.append('session/created',session_id=log.session_id,task='write')
   log.append('step/start',iteration=74)
   log.append('tool/call',call_id='a',tool='write_file',path='a.txt',destructive=True)
   log.append('tool/result',call_id='a',ok=True)
   log.append('assistant/message',in_tokens=100,out_tokens=20,usd=.01)
   log.append('conversation/message',message={'role':'user','content':'task'})
   log.append('session/closed',finished=True)
   log.append('followup/user',text='new task')
   log.append('tool/call',call_id='b',tool='run_shell',destructive=True,command='python job.py')
   before=replay(log);after=project_session(log.events,log.session_id)
   for name in vars(before):self.assertEqual(getattr(before,name),after[name],name)

 def test_duplicate_intents_are_not_silently_lost(self):
  with tempfile.TemporaryDirectory() as td:
   log=SessionLog(Path(td)/'s.jsonl')
   log.append('tool/call',call_id='dup',tool='write_file',path='a',destructive=True)
   log.append('tool/call',call_id='dup',tool='write_file',path='b',destructive=True)
   log.append('tool/result',call_id='dup',ok=True)
   state=project_session(log.events)
   self.assertEqual(len(state['unknown_calls']),1)
   self.assertEqual(state['unknown_calls'][0]['path'],'b')
   self.assertTrue(state['anomalies'])

 def test_corrupt_tail_never_grants_completion(self):
  with tempfile.TemporaryDirectory() as td:
   log=SessionLog(Path(td)/'s.jsonl');log.append('tool/call',call_id='a',destructive=True)
   with log.path.open('a') as f:f.write('{"kind":"session/closed"')
   loaded,skipped=SessionLog.load(log.path);state=project_session(loaded.events,skipped=skipped)
   self.assertFalse(state['finished']);self.assertEqual(state['skipped_lines'],1)
   self.assertEqual(len(state['unknown_calls']),1)

 def test_new_run_invalidates_old_completion_without_followup_event(self):
  with tempfile.TemporaryDirectory() as td:
   log=SessionLog(Path(td)/'s.jsonl');log.append('session/closed',finished=True)
   log.append('run/started',text='new')
   self.assertFalse(project_session(log.events)['finished'])

if __name__=='__main__':unittest.main()
