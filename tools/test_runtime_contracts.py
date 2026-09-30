"""Adversarial runtime experiments; no model credentials or user history."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
import unittest
import os
import time
from types import SimpleNamespace
from agentplat.tool_guards import ToolGuards, ToolRequest
from agentplat.runtime import PermissionDenied, workspace_digest
from agentplat.review_lifecycle import ReviewLifecycle
from agentplat.loop_types import LoopResult
from agentplat.evaluation_report import envelope, benchmark_cases


class RuntimeContracts(unittest.TestCase):
    def test_deny_cannot_be_overridden(self):
        guards = ToolGuards(); seen = []
        guards.register('deny', lambda r, a: 'read-only')
        guards.register('later_allow', lambda r, a: seen.append(True))
        with self.assertRaises(PermissionDenied): guards.check(ToolRequest('write'), {})
        self.assertEqual(seen, [])

    def test_guard_mutations_do_not_change_arguments(self):
        guards = ToolGuards(); args = {'nested': {'path': 'safe'}}
        guards.register('mutator', lambda r, a: a['nested'].update(path='escape'))
        guards.check(ToolRequest('write'), args)
        self.assertEqual(args['nested']['path'], 'safe')

    def test_guard_errors_fail_closed(self):
        guards = ToolGuards()
        guards.register('broken', lambda r, a: 1/0)
        with self.assertRaises(PermissionDenied): guards.check(ToolRequest('write'), {})

    def test_live_execution_boundary_uses_guard(self):
        from agentplat.loop import CodingAgent
        from agentplat.workspace import Workspace
        from agentplat.llmconfig import LLMConfig
        from agentplat.experiments import ScriptedModel
        with tempfile.TemporaryDirectory() as td:
            agent = CodingAgent(ScriptedModel(), LLMConfig(), workspace=Workspace(Path(td)/'ws'),
                                session_dir=Path(td)/'sessions', enable_subagents=False)
            agent.tool_guards.register('deny-write', lambda r, a: 'fixture' if r.writes else None)
            out, ok, recorded = agent._execute_tool('write_file', {'path':'bad.txt','content':'bad'},
                agent.tools['write_file'], 'guard-test', 1, lambda e: None)
            self.assertFalse(ok); self.assertTrue(recorded)
            self.assertFalse((agent.ws.root/'bad.txt').exists())
            self.assertIn('fixture', out)

    def test_final_report_has_host_verdict_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as td:
            events = []; agent = ReviewLifecycle()
            agent.ws = SimpleNamespace(scope=Path(td)); agent._acceptance_task = 'task'
            agent.session = SimpleNamespace(append=lambda *a, **kw: events.append((a,kw)))
            agent._independent_review = dict(passed=True,agent_id='review-1',task='task',
                                            digest=workspace_digest(Path(td)),tests=['external assertion'])
            result = LoopResult(True); agent._finalize_delivery(result, '验收 not_started')
            self.assertEqual(result.summary, '验收 not_started')
            self.assertEqual(result.acceptance['status'], 'passed')
            self.assertEqual(result.author_summary, '验收 not_started')
            self.assertEqual(events[0][0][0], 'delivery/finalized')
            (Path(td)/'changed').write_text('new')
            with self.assertRaises(RuntimeError): agent._finalize_delivery(LoopResult(True), 'done')

    def test_reviewer_machine_contract_is_preserved(self):
        agent = ReviewLifecycle(); agent.verification_task = True
        result = LoopResult(True); agent._finalize_delivery(result, '{"verdict":"pass"}')
        self.assertEqual(result.summary, '{"verdict":"pass"}')

    def test_missing_outcome_is_not_zero_or_pass(self):
        report = envelope('integration',[{'id':'unknown','status':'unknown'}])
        self.assertIsNone(report['pass_rate']); self.assertIsNone(report['complete'])
        self.assertEqual(report['counts']['unknown'],1)

    def test_partial_report_excludes_skips_and_errors_from_graded_rate(self):
        cases = benchmark_cases([dict(task='x',repeat=0,passed=True),
            dict(task='x',repeat=1,passed=False,classification='timeout')])
        report = envelope('task_benchmark',cases,planned_cases=3)
        self.assertFalse(report['complete']); self.assertEqual(report['counts']['error'],1)
        self.assertEqual(report['graded_cases'],1)

    def test_duplicate_case_is_rejected(self):
        with self.assertRaises(ValueError):
            envelope('integration',[{'id':'x','status':'passed'}]*2)

    def test_old_reports_are_classified_without_fabricating_runs(self):
        from agentplat.evaluation_report import inspect_report
        self.assertIsNone(inspect_report({})['observed_cases'])
        self.assertEqual(inspect_report({'passed':False})['observed_cases'],1)
        self.assertEqual(inspect_report({'checks':{'a':True,'b':False}})['observed_cases'],2)
        self.assertNotIn('runs',inspect_report({'passed':True}))

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object integration')
    def test_timeout_settles_descendant_process(self):
        import ctypes
        from ctypes import wintypes
        from agentplat.processes import ProcessSupervisor
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.WaitForSingleObject.restype=wintypes.DWORD
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        supervisor=ProcessSupervisor();handle=None
        with tempfile.TemporaryDirectory() as td:
            try:
                script="import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); print(p.pid,flush=True); time.sleep(120)"
                task=supervisor.start([sys.executable,'-u','-c',script],Path(td),timeout_s=3)
                deadline=time.monotonic()+2
                state=supervisor.poll(task)
                while not state['output'].strip() and time.monotonic()<deadline:
                    time.sleep(.05);state=supervisor.poll(task)
                pid=int(state['output'].strip())
                handle=kernel.OpenProcess(0x100000,False,pid)
                self.assertTrue(handle,'Cannot observe the owned descendant')
                state=supervisor.wait(task,10)
                self.assertEqual(state['status'],'timeout')
                self.assertEqual(kernel.WaitForSingleObject(handle,5000),0,'Descendant survived timeout')
            finally:
                supervisor.close()
                if handle:kernel.CloseHandle(handle)


if __name__ == '__main__': unittest.main()
