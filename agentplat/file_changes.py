"""Bounded before/after text observations. Never an undo or replay mechanism."""
import difflib
from pathlib import Path


def capture(ws, name):
    if not name or any(p.startswith('.') for p in Path(name).parts):
        return None
    try:
        p = ws.resolve(name)
        if not p.exists():
            return ''
        if not p.is_file() or p.stat().st_size > 256000 or p.stat().st_nlink > 1:
            return None
        value = p.read_text(encoding='utf-8')
        return value if '\0' not in value else None
    except Exception:
        return None


def difference(name, before, after):
    if before is None or after is None:
        return 'Diff unavailable: binary, large, protected or inaccessible file.'
    if before == after:
        return ''
    # SequenceMatcher is quadratic on adversarial input; bound line count too.
    if len(before.splitlines()) + len(after.splitlines()) > 6000:
        return 'Diff unavailable: text exceeds 6000 lines.'
    return ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                      fromfile='before/' + name, tofile='after/' + name))


def capture_tree(ws):
    """Only observe bounded regular text within authorized roots; skip links."""
    import os
    files, omitted = {}, set()
    count = size = 0
    complete = True
    for root in ws.roots.values():
        for base, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                       ('node_modules', '__pycache__', 'dist', 'build', 'vendor')
                       and not (Path(base)/d).is_symlink()
                       and not (getattr((Path(base)/d).lstat(), 'st_file_attributes', 0) & 0x400)]
            if len(Path(base).relative_to(root).parts) > 8:
                dirs[:] = []; complete = False; continue
            for name in names:
                count += 1
                if count > 400 or size > 2000000:
                    return files, omitted, False
                if name.startswith('.') or Path(name).suffix.lower() in ('.pem','.key','.pfx','.p12'):
                    continue
                path = Path(base)/name
                rel = ws.rel(path)
                if path.is_symlink():
                    omitted.add(rel); continue
                value = capture(ws, rel)
                if value is None:
                    omitted.add(rel); continue
                size += len(value.encode('utf-8'))
                if size > 2000000:
                    return files, omitted, False
                files[rel] = value
    return files, omitted, complete


def tree_diff(before, after):
    a, skipped_a, complete_a = before
    b, skipped_b, complete_b = after
    for name in sorted(a.keys() | b.keys()):
        if name in skipped_a or name in skipped_b or (name not in a and not complete_a) or (name not in b and not complete_b):
            continue
        value = difference(name, a.get(name, ''), b.get(name, ''))
        if value:
            yield name, value
