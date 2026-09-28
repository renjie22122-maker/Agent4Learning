"""Local Git review and isolated worktree creation. Never pushes or merges remotely."""
from pathlib import Path
import re
import subprocess
import uuid

MANAGED = Path(__file__).resolve().parents[1] / '.agent-runtime' / 'worktrees'


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            encoding='utf-8', errors='replace', timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    return result.stdout


def review(root):
    # Resolve repository first; callers only pass the host-selected workspace.
    repository = git(root, 'rev-parse', '--show-toplevel').strip()
    return {'repository':repository, 'status':git(root, 'status', '--short')[:20000],
            'unstaged_diff':git(root, 'diff', '--no-ext-diff', '--no-textconv')[:80000],
            'staged_diff':git(root, 'diff', '--cached', '--no-ext-diff', '--no-textconv')[:80000],
            'note':'未跟踪文件仅列出名称；请按需读取。输出可能截断。'}


def create_worktree(root, ref='HEAD'):
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', ref) or ref.startswith('-'):
        raise ValueError('无效 Git ref')
    commit = git(root, 'rev-parse', '--verify', ref + '^{commit}').strip()
    identifier = uuid.uuid4().hex[:12]
    branch = 'codex/agent-' + identifier
    target = MANAGED / identifier
    MANAGED.mkdir(parents=True, exist_ok=True)
    git(root, 'worktree', 'add', '-b', branch, str(target), commit)
    return {'workspace':str(target), 'branch':branch, 'commit':commit,
            'uncommitted_changes_copied':False, 'note':'基于指定提交创建；原工作区未提交修改未复制。可在界面选择此工作区并行运行。'}
