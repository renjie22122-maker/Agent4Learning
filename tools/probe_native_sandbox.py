"""Small live native sandbox probe; no model calls or external network traffic."""
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.processes import ProcessSupervisor

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='sandbox-probe-') as td:
        root = Path(td)
        workspace = root / 'workspace'
        workspace.mkdir()
        supervisor = ProcessSupervisor()
        try:
            task = supervisor.start('python -c "print(42)"', workspace,
                                    native_workspace=workspace, native_network='host', timeout_s=10)
            result = supervisor.wait(task, 30)
            print(result)
            assert result['status'] == 'exited' and result['exit_code'] == 0 and '42' in result['output']
        finally:
            supervisor.close()
