"""Sandbox lessons use real disposable processes; OS tests are explicitly opt-in."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from .workspace import Workspace


def native_boundary(descendant=False):
    if os.name != 'nt':
        raise RuntimeError('This experiment requires Windows; do not report a skipped OS test as passing')
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        outside = root / 'outside.txt'
        outside.write_text('baseline')
        ws = Workspace(root / 'workspace')
        payload = f"from pathlib import Path; Path({str(outside)!r}).write_text('changed')"
        if descendant:
            payload = f'import subprocess,sys;sys.exit(subprocess.call([sys.executable,"-c",{payload!r}]))'
        (ws.root / 'probe.py').write_text(payload)
        ws.execution_mode = 'local'
        ws.run('python probe.py')
        before = int(outside.read_text() == 'changed')
        outside.write_text('baseline')
        ws.execution_mode = 'native'; ws.native_network = 'host'
        ws.run('python probe.py')
        assert 'PermissionError' in ws.last_execution['output'], ws.last_execution
        after = int(outside.read_text() == 'changed')
        assert ws.last_execution['exit_code'] != 0
        return ('descendant_outside_writes' if descendant else 'outside_writes', before, after)


def native_network_gate():
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td) / 'workspace'); ws.execution_mode = 'native'
        ws.native_network = 'deny'
        (ws.root / 'probe.py').write_text("open('ran.txt','w').write('ran')")
        ws.run('python probe.py')
        rejected = 'SANDBOX_PREFLIGHT_FAILED' in ws.last_execution['output']
        if rejected:
            assert not (ws.root / 'ran.txt').exists()
            print('OS_NETWORK_DENIAL=unavailable; STRICT_EXECUTION=refused. This is not a passing network-isolation claim.')
        else:
            assert ws.last_execution['exit_code'] == 0
            print('OS_NETWORK_DENIAL=observed; capabilities remain empty. Loopback probe is a necessary check, not a complete firewall audit.')
        return 'unverified_isolation_launches', 1, 0


def environment_boundary():
    from .windows_sandbox import clean_environment
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        command = [sys.executable, '-c', 'import os;print(int("TEACHING_FAKE_SECRET" in os.environ))']
        before = int(subprocess.check_output(command, env=dict(os.environ, TEACHING_FAKE_SECRET='fixture'), text=True))
        after = int(subprocess.check_output(command, env=clean_environment(root, root), text=True))
        return 'inherited_secrets', before, after


def domain_revocation():
    from unittest.mock import patch
    from . import web_policy
    from .sources import SourceStore
    with tempfile.TemporaryDirectory() as td, patch.object(web_policy, 'POLICY_PATH', Path(td) / 'policy.json'):
        web_policy.save_domains('example.org')
        cached = web_policy.domains()
        store = SourceStore(td); store.domain_provider = web_policy.domains
        web_policy.save_domains('')
        before = int('example.org' in cached)
        try:
            store.fetch('https://example.org')
            after = 1
        except PermissionError:
            after = 0
        return 'stale_domain_grants', before, after


def turn_accounting():
    from .loop import MaxIterationsPolicy, LoopContext
    context = LoopContext(85, 85, 10, 0, 0, 0, False)
    before = int(context.iteration >= 40 and not context.verified)
    after = int(MaxIterationsPolicy()(context) is not None)
    assert MaxIterationsPolicy(hard_limit=80)(context) is not None
    return 'false_loop_stops', before, after
