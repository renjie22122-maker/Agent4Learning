"""Host-side tree operations must never follow user-controlled links.

This is a preflight, not a defence against a concurrent privileged filesystem
attacker. Process containment remains the execution backend's responsibility.
"""
import os
from pathlib import Path
import stat


def check_entry(path):
    path = Path(path)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError(f'Refusing symlink/reparse point: {path}')
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise ValueError(f'Refusing hard link: {path}')
    return info


def walk_files(root, ignored=()):
    root = Path(root)
    check_entry(root)
    def fail(error):
        raise error
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        dirs[:] = [name for name in dirs if name not in ignored]
        for name in dirs:
            check_entry(Path(folder) / name)
        for name in files:
            if name in ignored:
                continue
            path = Path(folder) / name
            check_entry(path)
            yield path
