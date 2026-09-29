"""在独立临时工作区执行离线测试，避免旧测试清空用户 workspace。"""
from pathlib import Path
import subprocess
import sys
import re

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentplat.processes import ProcessSupervisor
BOOTSTRAP = '''
import pathlib, runpy, sys, tempfile, os
os.environ["AGENTLAB_EXECUTION_MODE"] = "local"
os.environ["AGENTLAB_OFFLINE_FIXTURES_ONLY"] = "1"
sys.path.insert(0, sys.argv[1])
import agentplat.workspace as workspace
with tempfile.TemporaryDirectory() as td:
    workspace.DEFAULT_WORKSPACE = pathlib.Path(td) / "workspace"
    test_path = sys.argv[2]
    sys.argv = [test_path]
    runpy.run_path(test_path, run_name="__main__")
'''

if __name__ == '__main__':
    failed = []
    for path in sorted((ROOT / 'tools').glob('test_*.py')):
        if path.name in ('test_offline.py', 'test_hang_regression.py', 'test_support.py', 'test_native_sandbox.py'):
            continue
        supervisor = ProcessSupervisor()
        try:
            identifier = supervisor.start(
                [sys.executable, '-X', 'utf8', '-c', BOOTSTRAP, str(ROOT), str(path)], ROOT, timeout_s=120)
            state = supervisor.wait(identifier, 1)
            while state['status'] == 'running':
                state = supervisor.wait(identifier, 1)
            failed_run = state['status'] != 'exited' or state['exit_code'] != 0
            if failed_run:
                failed.append(path.name)
                print(state['output'][-5000:])
            skipped = bool(re.search(r'OK \(skipped=|\bSKIP\b', state['output']))
            label = 'FAIL' if failed_run else 'PASS_WITH_SKIPS' if skipped else 'PASS'
            if skipped:
                print('\n'.join(line for line in state['output'].splitlines()
                                if 'SKIP' in line or 'skipped=' in line))
            print(f'{path.name}: {label} ({state["status"]})', flush=True)
        except Exception as exc:
            failed.append(path.name)
            print(f'{path.name}: HARNESS_ERROR {type(exc).__name__}: {exc}', flush=True)
        finally:
            supervisor.close()
    sys.exit(bool(failed))
