import sys, unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.pages_agent import _thread
from agentplat.chat_timeline import enrich_turns

class TimelineTests(unittest.TestCase):
 def state(self):
  return dict(session_id='fixture',status='running',started_at=10,current_text='TASK',
    progress_messages=['BEFORE','AFTER'],progress_times=[11,16],steps=[dict(at=12,kind='tool',title='BEFORE_TOOL'),dict(at=17,kind='tool',title='AFTER_TOOL')],
    steering_messages=[dict(id='s',at=14,text='USER_STEERING',status='delivered')],
    human_questions=[dict(id='q',created=13)])
 def test_live_order_and_no_duplicates(self):
  h=_thread(self.state())
  self.assertLess(h.index('BEFORE'),h.index('data-human-slot="q"'))
  self.assertLess(h.index('data-human-slot="q"'),h.index('USER_STEERING'))
  self.assertLess(h.index('USER_STEERING'),h.index('AFTER'))
  self.assertEqual(h.count('USER_STEERING'),1)
 def test_new_output_does_not_move_interaction(self):
  a=self.state();a['progress_messages'].append('LATEST');a['progress_times'].append(20)
  h=_thread(a);self.assertLess(h.index('data-human-slot="q"'),h.index('LATEST'))
 def test_completed_and_later_turn_keep_original_position(self):
  a=self.state();turn={**a,'at':10,'text':'TASK','summary':'FINISHED'}
  a.update(turns=[turn],started_at=30,current_text='SECOND',progress_messages=[],progress_times=[],steps=[],steering_messages=[])
  h=_thread(a)
  self.assertLess(h.index('data-human-slot="q"'),h.index('FINISHED'))
  self.assertLess(h.index('USER_STEERING'),h.index('SECOND'))
  self.assertEqual(h.count('data-human-slot="q"'),1)
 def test_legacy_timing_from_log_without_mutation(self):
  events=[SimpleNamespace(kind=k,ts=t,data=d) for k,t,d in [('run/started',10,{}),('assistant/message',11,{'text':'BEFORE'}),('steering/queued',12,{'id':'s','text':'EXTRA'}),('assistant/message',13,{'text':'AFTER'}),('ui/turn',15,{})]]
  original={'progress_messages':['BEFORE','AFTER']}
  t=enrich_turns([original],events)[0]
  self.assertEqual(t['progress_times'],[11,13]);self.assertEqual(t['steering_messages'][0]['at'],12)
  self.assertNotIn('at',original)

if __name__=='__main__':unittest.main(verbosity=2)
