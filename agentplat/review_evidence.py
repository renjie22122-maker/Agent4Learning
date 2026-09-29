"""Copy only referenced source artifacts into a verifier's isolated workspace."""
import hashlib
import re
from pathlib import Path

REFERENCE = re.compile(r'\.(?:spill|sources)/[A-Za-z0-9_-]+\.(?:txt|json)')


def snapshot(parent, target, paths=None):
    parents = parent if isinstance(parent, dict) else {'main': Path(parent)}
    targets = target if isinstance(target, dict) else {'main': Path(target)}
    manifest = []
    for alias, root in parents.items():
        root, destination = Path(root).resolve(), Path(targets[alias]).resolve()
        references = set()
        # Scan the delivered snapshot, not caches/history or unrelated host data.
        for path in destination.rglob('*'):
            relative = path.relative_to(destination).as_posix()
            if paths is not None and not any(p.startswith('[') for p in paths):
                if relative not in paths and f'@{alias}/{relative}' not in paths:
                    continue
            if path.is_file() and path.suffix in ('.md', '.py', '.txt', '.json', '.html') and path.stat().st_size <= 2_000_000:
                references.update(REFERENCE.findall(path.read_text(encoding='utf-8', errors='replace')))
        references.update(p[:-4]+'.json' for p in list(references)
                          if p.startswith('.sources/') and p.endswith('.txt') and (root/(p[:-4]+'.json')).is_file())
        for relative in sorted(references):
            source = root / relative
            if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(root):
                manifest.append({'alias':alias, 'path':relative, 'missing':True})
                continue
            if source.stat().st_size > 10_000_000:
                manifest.append({'alias':alias, 'path':relative, 'missing':True, 'reason':'size limit'})
                continue
            raw = source.read_bytes()
            out = destination / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(raw)
            manifest.append({'alias':alias,'path':relative,'sha256':hashlib.sha256(raw).hexdigest()})
    return manifest


def intact(target, manifest):
    roots = target if isinstance(target, dict) else {'main':Path(target)}
    for item in manifest:
        if item.get('missing'): continue
        path = Path(roots[item['alias']]) / item['path']
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            return False
    return True
