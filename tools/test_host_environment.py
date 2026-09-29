"""Environment identity, approved execution outcomes, EOF and cleanup regressions."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.execution_environment import task_environment, describe
from agentplat.processes import ProcessSupervisor
from agentplat import approvals


class HostEnvironmentTests(unittest.TestCase):
    def test_profile_preserved_without_credentials_or_hooks(self):
        source={'PATH':'toolchain','USERPROFILE':r'C:\Users\someone','HOME':'/home/someone',
                'APPDATA':'config','LOCALAPPDATA':'cache','VIRTUAL_ENV':'venv','JAVA_HOME':'jdk',
                'DEEPSEEK_API_KEY':'secret','CUSTOM_TOKEN':'secret','PYTHONPATH':'inject',
                'PYTHONHOME':'inject','NODE_OPTIONS':'inject','OTHER_VARIABLE':'unknown'}
        result=task_environment(source)
        for k in ('PATH','USERPROFILE','HOME','APPDATA','LOCALAPPDATA','VIRTUAL_ENV','JAVA_HOME'):self.assertEqual(result[k],source[k])
        for k in ('DEEPSEEK_API_KEY','CUSTOM_TOKEN','PYTHONPATH','PYTHONHOME','NODE_OPTIONS','OTHER_VARIABLE'):self.assertNotIn(k,result)
        self.assertEqual(source['PYTHONHOME'],'inject')
    def test_real_python_resolves_home(self):
        env=task_environment()
        result=subprocess.run([sys.executable,'-c','from pathlib import Path; print(Path.home()); print(Path("~").expanduser())'],env=env,capture_output=True,text=True,timeout=10,check=True)
        self.assertEqual(result.stdout.splitlines(),[str(Path.home())]*2)
    def test_noninteractive_command_receives_eof(self):
        with tempfile.TemporaryDirectory() as td:
            supervisor=ProcessSupervisor()
            try:
                key=supervisor.start([sys.executable,'-c','import sys; print("EOF", repr(sys.stdin.read()))'],td,interactive=False,timeout_s=5)
                result=supervisor.wait(key,8)
                self.assertEqual(result['status'],'exited');self.assertEqual(result['exit_code'],0)
                self.assertIn("EOF ''",result['output'])
            finally:supervisor.close()
    def test_timeout_is_not_success_even_if_exit_zero(self):
        from agentplat.tool_runtime import ToolRuntime
        from agentplat.agent_tools import AgentTool, _obj
        from agentplat.runtime import CapabilityPolicy
        agent=ToolRuntime();agent.session=NS(append=lambda *a,**k:None,flush=lambda *a:None);agent.capabilities=CapabilityPolicy()
        tool=AgentTool('request_execution','fixture',_obj({},[]),lambda:json.dumps({'executed':True,'result':{'status':'timeout','exit_code':0,'success':False}}),True)
        _,ok,_=agent._execute_tool('request_execution',{},tool,'fixture',1,lambda s:None)
        self.assertFalse(ok)
    def test_cleanup_error_preserves_observed_result(self):
        with tempfile.TemporaryDirectory() as td,patch.object(approvals,'DATABASE',Path(td)/'approvals.db'):
            from agentplat.runtime import CapabilityPolicy
            agent=NS(ws=NS(root=Path(td),allow_shell=True,execution_mode='native'),capabilities=CapabilityPolicy(),stop_flag=threading.Event(),session=NS(session_id='s',append=lambda *a,**k:None,flush=lambda *a:None))
            row=approvals.request('s',td,'echo fixture','test',timeout_s=300);approvals.decide(row['request_id'],True)
            class Executor:
                def start(self,*a,**kw):
                    assert kw['timeout_s']==300 and kw['interactive'] is False
                    return 'id'
                def wait(self,*a):return dict(status='exited',exit_code=0,output='valuable result')
                def close(self):raise NotADirectoryError('cleanup fixture')
            with patch('agentplat.processes.ProcessSupervisor',Executor):result=approvals.execute(agent,row['request_id'])
            self.assertEqual(result['output'],'valuable result');self.assertFalse(result['success'])
            self.assertEqual(result['next_execution_backend'],'native');self.assertIn('NotADirectoryError',result['cleanup_error'])
    def test_timeout_validation_and_legacy_migration(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td,patch.object(approvals,'DATABASE',Path(td)/'legacy.db'):
            db=sqlite3.connect(approvals.DATABASE)
            db.execute('CREATE TABLE approvals (id TEXT PRIMARY KEY, session TEXT, workspace TEXT, command TEXT, reason TEXT, status TEXT, expires REAL)')
            db.execute("INSERT INTO approvals VALUES ('old','s',?,'echo old','test','pending',9999999999)",(str(Path(td).resolve()),));db.commit();db.close()
            rows=approvals.list_requests();self.assertEqual(rows[0]['timeout_s'],60)
            row=approvals.request('s',td,'echo new','test',120);approvals.decide(row['request_id'],True)
            self.assertEqual(approvals.claim(row['request_id'],'s',td)['timeout_s'],120)
            for value in (0,-1,3601,float('nan'),True):
                with self.assertRaises(ValueError):approvals.request('s',td,'echo invalid','test',value)
    def test_native_identity_does_not_claim_host_packages(self):
        info=describe(NS(execution_mode='native'))
        self.assertEqual(info['host_python'],sys.executable)
        self.assertIn('后续 run_shell 不会自动切换',info['note'])

if __name__=='__main__':unittest.main(verbosity=2)
