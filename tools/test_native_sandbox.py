"""Live Windows OS-boundary tests. Run explicitly outside an outer restrictive sandbox.

All attack targets are disposable fixtures, no real user files or external network.
"""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.workspace import Workspace

def elevated_windows():
    if os.name != 'nt': return False
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


@unittest.skipUnless(os.name == 'nt', 'requires Windows AppContainer')
@unittest.skipUnless(elevated_windows(), 'requires elevated Windows integration-test host; skipped, not passed')
class NativeSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = Workspace(self.root / 'workspace')
        self.ws.execution_mode = 'native'
        self.ws.native_network = 'host'  # explicit diagnostic: characterize OS behavior
        self.addCleanup(self.ws.processes.close)
        self.outside = self.root / 'private.txt'
        self.outside.write_text('private-fixture')

    def run_code(self, code):
        (self.ws.root / 'probe.py').write_text(code, encoding='utf-8')
        output = self.ws.run('python probe.py', timeout_s=8)
        self.assertEqual(self.ws.last_execution['status'], 'exited', output)
        self.assertEqual(self.ws.last_execution['exit_code'], 0, output)
        return json.loads(self.ws.last_execution['output'])

    def test_filesystem_real_denials(self):
        code = f'''import pathlib,json
results={{}}
pathlib.Path('inside.txt').write_text('ok')
for action in ('read','write'):
 try:
  p=pathlib.Path({str(self.outside)!r})
  p.read_text() if action=='read' else p.write_text('changed')
  results[action]='ALLOWED'
 except PermissionError: results[action]='DENIED'
print(json.dumps(results))
'''
        self.assertEqual(self.run_code(code), {'read': 'DENIED', 'write': 'DENIED'})
        self.assertEqual((self.ws.root / 'inside.txt').read_text(), 'ok')
        self.assertEqual(self.outside.read_text(), 'private-fixture')

    def test_secondary_folder_scope(self):
        secondary=self.root/'secondary';secondary.mkdir()
        multi=Workspace({'main':self.ws.root,'docs':secondary})
        multi.execution_mode='native';multi.native_network='host'
        self.addCleanup(multi.processes.close)
        (secondary/'probe.py').write_text(
            'from pathlib import Path\nPath("inside.txt").write_text("ok")\n'
            f'target=Path({str(self.outside)!r})\n'
            'try:\n target.write_text("escape")\nexcept PermissionError:\n print("DENIED")\n',encoding='utf-8')
        multi.run('python probe.py',timeout_s=10,cwd='@docs')
        self.assertEqual(multi.last_execution['exit_code'],0,multi.last_execution)
        self.assertIn('DENIED',multi.last_execution['output'])
        self.assertEqual((secondary/'inside.txt').read_text(),'ok')
        self.assertEqual(self.outside.read_text(),'private-fixture')

    def test_descendant_inherits_boundary(self):
        child = f"from pathlib import Path; Path({str(self.outside)!r}).write_text('escaped')"
        result = self.run_code(f'import subprocess,sys,json\nr=subprocess.run([sys.executable,"-c",{child!r}],capture_output=True)\nprint(json.dumps({{"code":r.returncode,"denied":b"PermissionError" in r.stderr}}))')
        self.assertNotEqual(result['code'], 0)
        self.assertTrue(result['denied'])
        self.assertEqual(self.outside.read_text(), 'private-fixture')

    def test_token_is_appcontainer(self):
        result = self.run_code("import ctypes as c,ctypes.wintypes as w,json\nk=c.WinDLL('kernel32');a=c.WinDLL('advapi32')\nk.GetCurrentProcess.restype=w.HANDLE\na.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)]\na.GetTokenInformation.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.POINTER(w.DWORD)]\nt=w.HANDLE();assert a.OpenProcessToken(k.GetCurrentProcess(),8,c.byref(t))\nv=w.DWORD();n=w.DWORD();assert a.GetTokenInformation(t,29,c.byref(v),4,c.byref(n))\nprint(json.dumps(v.value))\n")
        self.assertEqual(result, 1)

    def test_strict_network_gate(self):
        self.ws.native_network = 'deny'
        (self.ws.root / "target.py").write_text("open('executed.txt','w').write('yes')")
        output = self.ws.run("python target.py", timeout_s=8)
        if 'SANDBOX_PREFLIGHT_FAILED' in output:
            self.assertFalse((self.ws.root / 'executed.txt').exists())
            self.assertNotEqual(self.ws.last_execution['exit_code'], 0)
            print('OBSERVED: OS loopback isolation unavailable; strict mode correctly refused execution')
        else:
            self.assertEqual(self.ws.last_execution['exit_code'], 0, output)
            self.assertTrue((self.ws.root / 'executed.txt').exists())

    def test_environment_secrets_not_inherited(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {'AGENTLAB_FIXTURE_SECRET': 'fixture-only'}):
            self.assertFalse(self.run_code('import os,json; print(json.dumps("AGENTLAB_FIXTURE_SECRET" in os.environ))'))

    def test_native_timeout(self):
        (self.ws.root / 'slow.py').write_text('import time;print("started",flush=True);time.sleep(30)')
        self.ws.run('python slow.py', timeout_s=1)
        self.assertEqual(self.ws.last_execution['status'], 'timeout')
        self.assertIn('started', self.ws.last_execution['output'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
