import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentplat.session import SessionLog, CheckpointError
from agentplat.recovery import inspect_run
from agentplat.runtime import workspace_digest
from tools.service_supervisor import supervise, restart_delay


class CrashRecoveryTests(unittest.TestCase):
    def fixture(self, root, **overrides):
        workspace = root/'workspace'; workspace.mkdir()
        (workspace/'example.txt').write_text('original')
        log = SessionLog(root/'session.jsonl', session_id='session')
        log.append('session/created', session_id='session', task='read files')
        values = dict(text='read files', permission_mode='readonly',
                      workspace_roots={'main':str(workspace)}, max_iters=0, max_usd=None)
        values.update(overrides)
        log.append('run/started', **values)
        log.append('conversation/message', message={'role':'user','content':'read files'})
        log.append('recovery/checkpoint', digest=workspace_digest({'main':workspace}))
        log.append('model/request', iteration=1)
        return log, workspace

    def test_safe_boundary_and_changed_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            log, workspace = self.fixture(Path(td))
            self.assertEqual(inspect_run(log.path)['status'], 'safe')
            (workspace/'example.txt').write_text('changed')
            self.assertEqual(inspect_run(log.path)['status'], 'needs_attention')

    def test_uncertain_tool_never_replayed(self):
        with tempfile.TemporaryDirectory() as td:
            log, workspace = self.fixture(Path(td))
            log.append('tool/call', call_id='unknown', tool='append_file', destructive=True)
            row = inspect_run(log.path)
            self.assertIn('未知', row['reason'])
            self.assertEqual((workspace/'example.txt').read_text(), 'original')

    def test_cancelled_or_settled_not_restarted(self):
        for kind in ['run/settled','run/cancel_requested','ui/turn','turn/stopping','session/closed']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                log, _ = self.fixture(Path(td)); log.append(kind)
                self.assertIsNone(inspect_run(log.path))

    def test_budget_permissions_recovery_storm_are_blocked(self):
        for options in [dict(permission_mode='auto'), dict(max_iters=10),
                        dict(max_usd=1), dict(recovered=True)]:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as td:
                log, _ = self.fixture(Path(td), **options)
                self.assertEqual(inspect_run(log.path)['status'], 'needs_attention')

    def test_late_progress_and_corrupt_log_blocked(self):
        for kind in ['assistant/message','tool/result','steering/queued']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                log, _ = self.fixture(Path(td)); log.append(kind)
                self.assertEqual(inspect_run(log.path)['status'], 'needs_attention')
        with tempfile.TemporaryDirectory() as td:
            log, _ = self.fixture(Path(td))
            with log.path.open('a') as f: f.write('{broken')
            self.assertEqual(inspect_run(log.path)['status'], 'needs_attention')

    def test_write_failure_does_not_publish_event(self):
        with tempfile.TemporaryDirectory() as td:
            log = SessionLog(Path(td)/'log.jsonl')
            with patch.object(log, '_write', side_effect=CheckpointError('disk full')):
                with self.assertRaises(CheckpointError): log.append('tool/call')
            self.assertEqual(log.events, []); self.assertEqual(log.last_seq, 0)

    def test_real_process_crash_then_clean_exit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); count = root/'count'
            script = root/'child.py'
            script.write_text('import os\nfrom pathlib import Path\np=Path('+repr(str(count))+')\n'
                              'n=int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\n'
                              'os._exit(7 if n==0 else 0)\n')
            with patch('tools.service_supervisor.restart_delay', return_value=0):
                self.assertEqual(supervise([sys.executable,str(script)],root,0),0)
            self.assertEqual(count.read_text(),'2')
            self.assertEqual(json.loads((root/'supervisor-status.json').read_text())['reason'],'clean_exit')
            self.assertEqual(len(list(root.glob('service-*.log'))),2)

    def test_crash_storm_backoff(self):
        self.assertEqual(restart_delay([],1000),2)
        self.assertIsNone(restart_delay([990]*5,1000))
        self.assertEqual(restart_delay([1]*5,1000),2)

    def test_server_restores_context_and_readonly_permissions(self):
        import time
        from agentplat.recovery import recover_server
        from tools.test_live_chat import LiveChatTests
        from agentplat.experiments import ScriptedModel
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); log, workspace = self.fixture(root)
            # Place fixture where the actual web session restoration searches.
            directory = root/'.sessions'; directory.mkdir()
            log.path.replace(directory/'session.jsonl')
            saved, _ = SessionLog.open(directory/'session.jsonl')
            created = saved.events[0]
            created.data.update(workspace=str(workspace),workspace_roots={'main':str(workspace)})
            saved.path.write_text('\n'.join(e.to_json() for e in saved.events)+'\n',encoding='utf-8')
            demo = LiveChatTests().make_demo(root)
            with patch('agentplat.llm.OpenAIChatClient',return_value=ScriptedModel([[('finish',{'summary':'Inspection complete'})]])):
                recover_server(demo)
                deadline = time.monotonic()+8
                while time.monotonic()<deadline and demo.agent_state.get('status')=='running':
                    time.sleep(.02)
            self.assertEqual(demo.recovery_reports[0]['status'],'resumed',demo.recovery_reports)
            self.assertEqual(demo._agent.permission_mode,'readonly')
            self.assertEqual(demo.agent_state['status'],'done',demo.agent_state)
            events,_ = SessionLog.load(saved.path)
            seqs=[e.seq for e in events.events]
            self.assertEqual(seqs,sorted(set(seqs)))


if __name__ == '__main__': unittest.main()
