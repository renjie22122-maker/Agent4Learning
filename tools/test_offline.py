"""在独立临时工作区执行离线测试，避免旧测试清空用户 workspace。"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
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
        try:
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', '-c', BOOTSTRAP, str(ROOT), str(path)],
                cwd=ROOT, capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=120)
            if result.returncode:
                failed.append(path.name)
                print((result.stdout + result.stderr)[-5000:])
            print(f'{path.name}: {"FAIL" if result.returncode else "PASS"}', flush=True)
        except subprocess.TimeoutExpired:
            failed.append(path.name)
            print(f'{path.name}: TIMEOUT', flush=True)
    sys.exit(bool(failed))
