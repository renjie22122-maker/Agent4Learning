import hashlib,io,json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from agentplat.dev_environments import Environments,validate,alive


class EnvironmentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.workspace=self.root/'project';self.workspace.mkdir()
        self.store=Environments(self.root/'registry')
        self.spec=dict(name='python-existing',version='.'.join(map(str,sys.version_info[:3])),kind='existing',
                       runtime=sys.executable,version_args=['--version'],
                       smoke=[['{runtime}','-c','assert sum([1,2,3])==6; print("SMOKE_OK")']])

    def test_existing_prepare_reuse_verify_retire_never_deletes_installation(self):
        row=self.store.create(self.workspace,self.spec);key=row['id']
        self.assertEqual(self.store.create(self.workspace,self.spec)['id'],key)
        row=self.store.operate(key,'prepare',key);self.assertEqual(row['state'],'ready')
        self.assertIn('SMOKE_OK',row['receipt']['checks'][-1]['output'])
        with self.assertRaises(RuntimeError):self.store.operate(key,'prepare',key)
        self.assertEqual(self.store.operate(key,'verify',key)['state'],'ready')
        self.assertEqual(self.store.operate(key,'retire',key)['state'],'retired')
        self.assertTrue(Path(sys.executable).is_file())
        self.assertNotEqual(self.store.create(self.workspace,self.spec)['id'],key)

    def test_bad_version_and_hash_do_not_count_as_success(self):
        row=self.store.create(self.workspace,{**self.spec,'version':'98765.43210'})
        with self.assertRaises(ValueError):self.store.operate(row['id'],'prepare','0'*64)
        self.assertEqual(self.store.get(row['id'])['state'],'planned')
        with self.assertRaises(RuntimeError):self.store.operate(row['id'],'prepare',row['id'])
        self.assertEqual(self.store.get(row['id'])['state'],'needs_inspection')
        with self.assertRaises(RuntimeError):self.store.operate(row['id'],'prepare',row['id'])

    def test_live_pid_is_not_killed_or_taken_over(self):
        self.assertTrue(alive(os.getpid()))
        row=self.store.create(self.workspace,self.spec)
        with self.store.db() as db:db.execute('UPDATE environments SET pid=?,state=? WHERE id=?',(os.getpid(),'preparing',row['id']))
        with self.assertRaises(RuntimeError):self.store.operate(row['id'],'verify',row['id'])
        self.assertTrue(alive(os.getpid()))

    def test_scope_and_tampered_plan(self):
        row=self.store.create(self.workspace,self.spec)
        self.assertFalse(self.store.list(self.root/'other'))
        with self.store.db() as db:db.execute('UPDATE environments SET plan=? WHERE id=?',('{}',row['id']))
        with self.assertRaises(ValueError):self.store.get(row['id'])

    def test_portable_archive_path_and_hash_controls(self):
        def bundle(name):
            raw=io.BytesIO()
            with zipfile.ZipFile(raw,'w') as z:z.writestr(name,b'fixture binary')
            return raw.getvalue()
        for name,allowed in [('bin/tool.exe',True),('../escape',False),('folder/.. /escape',False),('NUL.txt',False)]:
            data=bundle(name)
            spec={**self.spec,'kind':'portable','runtime':'bin/tool.exe','archive':'zip','url':'https://example.invalid/tool.zip','sha256':hashlib.sha256(data).hexdigest()}
            class Response(io.BytesIO):url=spec['url']
            folder=self.root/hashlib.sha256(name.encode()).hexdigest();folder.mkdir()
            with patch('urllib.request.urlopen',return_value=Response(data)):
                if allowed:self.store._archive(spec,folder);self.assertTrue((folder/'bin/tool.exe').exists())
                else:
                    with self.assertRaises(ValueError):self.store._archive(spec,folder)
        self.assertFalse((self.root/'escape').exists())

    def test_validation_requires_pins_and_minimal_probes(self):
        for change in [dict(smoke=[]),dict(kind='portable',url='http://unsafe/',sha256='x',archive='zip'),
                       dict(kind='venv',interpreter=sys.executable,runtime='Scripts/python.exe',packages=['numpy'])]:
            with self.assertRaises(ValueError):validate({**self.spec,**change})

    def test_real_empty_venv_and_managed_retirement(self):
        spec={**self.spec,'name':'isolated-python','kind':'venv','interpreter':sys.executable,
              'runtime':'Scripts/python.exe' if os.name=='nt' else 'bin/python','packages':[]}
        row=self.store.create(self.workspace,spec);key=row['id']
        result=self.store.operate(key,'prepare',key)
        self.assertEqual(result['state'],'ready');self.assertTrue(result['receipt']['files'])
        self.assertEqual(self.store.operate(key,'verify',key)['state'],'ready')
        # A change invalidates reuse, and a second verify cannot erase that baseline.
        (Path(result['directory'])/'unapproved.txt').write_text('changed')
        for _ in range(2):
            with self.assertRaises(RuntimeError):self.store.operate(key,'verify',key)
        retired=self.store.operate(key,'retire',key)
        self.assertFalse(Path(result['directory']).exists())
        self.assertTrue(Path(retired['receipt']['quarantine']).is_dir())

    def test_structured_approval_arguments_are_literal(self):
        from agentplat import approvals
        import subprocess,threading
        from types import SimpleNamespace
        from agentplat.runtime import CapabilityPolicy
        with patch.object(approvals,'DATABASE',self.root/'approval.sqlite3'):
            argv=[sys.executable,'-c','import sys; print(sys.argv[1])','a & echo PWNED %USERPROFILE%']
            command=subprocess.list2cmdline(argv)
            with self.assertRaises(ValueError):approvals.request('s',self.workspace,'different','test',argv=argv)
            key=approvals.request('s',self.workspace,command,'test',argv=argv)['request_id']
            approvals.decide(key,True)
            agent=SimpleNamespace(ws=SimpleNamespace(root=self.workspace,allow_shell=True),capabilities=CapabilityPolicy(),
                stop_flag=threading.Event(),session=SimpleNamespace(session_id='s',append=lambda *a,**k:None,flush=lambda *a:None))
            result=approvals.execute(agent,key)
            self.assertTrue(result['success']);self.assertEqual(result['output'].strip(),'a & echo PWNED %USERPROFILE%')
            with self.assertRaises(PermissionError):approvals.execute(agent,key)

    def test_registration_does_not_require_subagents(self):
        from agentplat.loop import CodingAgent
        from agentplat.llmconfig import LLMConfig
        from agentplat.workspace import Workspace
        from agentplat.experiments import ScriptedModel
        agent=CodingAgent(ScriptedModel(),LLMConfig(),workspace=Workspace(self.workspace),
                          session_dir=self.root/'logs',enable_subagents=False)
        self.assertIn('plan_development_environment',agent.tools)
        self.assertIn('request_execution',agent.tools)
        self.assertNotIn('spawn_agent',agent.tools)


if __name__=='__main__':unittest.main()
