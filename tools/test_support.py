"""测试夹具拥有自己的工作区；绝不 reset 用户默认目录。"""
import atexit
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.workspace import Workspace


def temporary_workspace():
    directory = tempfile.TemporaryDirectory(prefix='agentlab-test-')
    atexit.register(directory.cleanup)
    workspace = Workspace(Path(directory.name) / 'workspace')
    workspace.execution_mode = 'local'  # 仅执行受版本控制的确定性夹具
    return workspace
