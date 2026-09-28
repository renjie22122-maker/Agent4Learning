"""隔离副本与乐观并发合并：拒绝覆盖父工作区在分叉后的修改。"""
from pathlib import Path
import hashlib
import os
import shutil
import tempfile

IGNORED = {'.git', '.sessions', '.spill', '.browser', '.agent-runtime', '__pycache__', '.pytest_cache'}


def inventory(root):
    if isinstance(root, dict):
        return {f'@{alias}/{name}': value for alias, folder in root.items()
                for name, value in inventory(folder).items()}
    out = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in IGNORED and not (Path(folder) / d).is_symlink()]
        for name in files:
            path = Path(folder) / name
            if path.is_symlink():
                raise ValueError('隔离合并不支持符号链接')
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def scoped_path(scope, relative):
    if isinstance(scope, dict):
        alias, _, name = relative[1:].partition('/')
        if not relative.startswith('@') or alias not in scope:
            raise ValueError('未知的隔离文件夹')
        root = Path(scope[alias]).resolve()
    else:
        root, name = Path(scope).resolve(), relative
    target = (root / name).resolve()
    target.relative_to(root)
    return target


class IsolatedChanges:
    def __init__(self, parent, directory=None):
        self.parent = {k: Path(v).resolve() for k,v in parent.items()} if isinstance(parent, dict) else Path(parent).resolve()
        self.temporary = tempfile.TemporaryDirectory(prefix='agent-isolated-') if directory is None else None
        self.root = Path(self.temporary.name) / 'workspace' if self.temporary else Path(directory)
        self.base = inventory(self.parent)
        ignore = lambda folder, names: [n for n in names if n in IGNORED or (Path(folder) / n).is_symlink()]
        if isinstance(self.parent, dict):
            directory = self.root
            self.root = {alias: directory / alias for alias in self.parent}
            for alias, folder in self.parent.items():
                shutil.copytree(folder, self.root[alias], ignore=ignore)
        else:
            shutil.copytree(self.parent, self.root, ignore=ignore)
        self.applied = False

    def manifest(self):
        after = inventory(self.root)
        return [{'path': name, 'before': self.base.get(name), 'after': after.get(name)}
                for name in sorted(self.base.keys() | after.keys())
                if self.base.get(name) != after.get(name)]

    def apply(self):
        if self.applied:
            raise RuntimeError('该产物已合并，禁止重复应用')
        changes = self.manifest()
        current = inventory(self.parent)
        conflicts = [c['path'] for c in changes if current.get(c['path']) != c['before']]
        if conflicts:
            raise RuntimeError(f'父工作区已修改，拒绝覆盖：{conflicts}')
        backup = {}
        try:
            for change in changes:
                target = scoped_path(self.parent, change['path'])
                backup[target] = target.read_bytes() if target.exists() else None
                if change['after'] is None:
                    target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                        temp.write(scoped_path(self.root, change['path']).read_bytes())
                        staged = Path(temp.name)
                    staged.replace(target)
        except BaseException:
            for target, content in backup.items():
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(content)
            raise
        self.applied = True
        return [c['path'] for c in changes]

    def close(self):
        if self.temporary:
            self.temporary.cleanup()
