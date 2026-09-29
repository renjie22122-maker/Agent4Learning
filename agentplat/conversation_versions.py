"""Bounded, host-owned file snapshots. Never execute historical tool calls."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import uuid

EXCLUDED = {'.git', '.sessions', '.spill', '.sources', '.browser', '.agent-runtime',
            '.chat-storage', '.agents', '__pycache__', '.pytest_cache', '.venv', 'venv', 'node_modules'}
MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 10000


def inventory(roots):
    result = {}; size = 0
    def walk_error(error):
        raise error
    for alias, raw in roots.items():
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,31}', alias):
            raise ValueError('文件夹别名无效')
        root = Path(raw)
        if not root.is_dir(): raise ValueError('版本源目录不存在')
        if root.is_symlink() or getattr(root.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('版本源目录不能是链接')
        for base, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED)
            for name in dirs + files:
                p = Path(base)/name
                if p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & 0x400:
                    raise ValueError('文件范围包含链接，无法安全保存版本')
            for name in sorted(files):
                p = Path(base)/name
                size += p.stat().st_size
                if size > MAX_BYTES or len(result) >= MAX_FILES:
                    raise ValueError('文件快照超过 128 MiB 或 10000 个文件；本轮仍正常保存对话')
                result[alias+'/'+p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def copy_checked(roots, destinations):
    before = inventory(roots)
    for alias, dest in destinations.items():
        Path(dest).mkdir(parents=True, exist_ok=False)
    for key in before:
        alias, name = key.split('/', 1)
        target = Path(destinations[alias])/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(roots[alias])/name, target)
    if inventory(roots) != before or inventory(destinations) != before:
        raise ValueError('复制期间文件发生变化，请重试')
    return before


def capture(manager, roots):
    version = uuid.uuid4().hex
    base = Path(manager.state_path).parent/'.agent-runtime'/'versions'/version
    try:
        files = copy_checked(roots, {a: base/a for a in roots})
        return dict(status='ready', id=version, aliases=list(roots), files=len(files),
                    digest=hashlib.sha256(repr(sorted(files.items())).encode()).hexdigest())
    except (OSError, ValueError) as exc:
        # A failed snapshot is not advertised as a usable version.
        expected = (Path(manager.state_path).parent/'.agent-runtime'/'versions').resolve()
        if base.resolve().parent == expected and base.exists():
            shutil.rmtree(base, ignore_errors=True)
        return dict(status='unavailable', reason=str(exc))


def version_roots(manager, version):
    ident = version.get('id', '')
    if version.get('status') != 'ready' or not re.fullmatch('[0-9a-f]{32}', ident):
        raise ValueError('所选轮次没有文件快照，请选择复制当前文件')
    base = Path(manager.state_path).parent/'.agent-runtime'/'versions'/ident
    roots = {a: base/a for a in version['aliases']}
    files = inventory(roots)
    digest = hashlib.sha256(repr(sorted(files.items())).encode()).hexdigest()
    if digest != version['digest']: raise ValueError('文件快照校验失败，不能用作历史版本')
    return roots
