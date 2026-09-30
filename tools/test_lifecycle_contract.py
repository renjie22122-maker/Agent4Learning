import unittest
from types import SimpleNamespace
from agentplat.task_lifecycle import project


class LifecycleTests(unittest.TestCase):
    def events(self,*items):
        return [SimpleNamespace(seq=i+1,kind=k,data=d) for i,(k,d) in enumerate(items)]

    def test_approval_and_steering_cannot_fake_completion(self):
        state=project(self.events(('run/started',{}),('human/requested',dict(question_id='a',request_type='approval')),
                                 ('followup/user',{}),('session/closed',dict(finished=True))))
        self.assertEqual(state['state'],'interrupted')
        self.assertEqual(state['violations'][-1]['reason'],'completion with unresolved work')

    def test_unknown_effect_survives_resume_and_can_settle(self):
        items=[('run/started',{}),('tool/call',dict(call_id='a',tool='write_file',destructive=True)),
               ('run/started',dict(recovered=True))]
        self.assertIn('a',project(self.events(*items))['unresolved_effects'])
        items += [('tool/result',dict(call_id='a')),('model/request',{}),('session/closed',dict(finished=True))]
        state=project(self.events(*items));self.assertEqual(state['state'],'completed');self.assertEqual(state['violations'],[])

    def test_late_result_does_not_resurrect_cancelled_run(self):
        state=project(self.events(('run/started',{}),('tool/call',dict(call_id='a',tool='read_file')),
                                 ('run/cancel_requested',{}),('tool/result',dict(call_id='a'))))
        self.assertEqual(state['state'],'cancelled');self.assertFalse(state['tools'])

    def test_finish_control_can_close_before_its_tool_result(self):
        state=project(self.events(('run/started',{}),('tool/call',dict(call_id='f',tool='finish')),
                                 ('session/closed',dict(finished=True)),('tool/result',dict(call_id='f'))))
        self.assertEqual(state['state'],'completed');self.assertEqual(state['violations'],[])


if __name__=='__main__':unittest.main()
