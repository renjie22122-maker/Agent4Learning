"""Read-only retention audit. Never deletes evidence based on file age alone."""
import hashlib
import time
from pathlib import Path
from .filesystem_contract import walk_files,check_entry


def audit_spills(workspace,reference_roots,*,active=True,min_age_days=30):
    """Caller must enumerate all reference stores; candidates still require review.

    Without a complete reference inventory this cannot prove a file is unused.
    Deliberately no automatic deletion API: sessions, checkpoints and branches
    can retain locators long after the command that generated them has ended.
    """
    root=Path(workspace).absolute();check_entry(root)
    spill=root/'.spill';problems=[];references=[];rows=[]
    if not reference_roots:problems.append('No reference stores supplied')
    for location in reference_roots:
        try:
            for path in walk_files(Path(location)):
                if path.suffix.lower() not in ('.json','.jsonl','.md','.txt'):continue
                if path.stat().st_size>20_000_000:
                    problems.append('Reference exceeds audit size bound: '+str(path));continue
                references.append(path.read_text(encoding='utf-8'))
        except (OSError,ValueError,UnicodeError) as exc:problems.append(type(exc).__name__+': '+str(location))
    if spill.exists():
        for path in walk_files(spill):
            data=path.read_bytes();age=(time.time()-path.stat().st_mtime)/86400
            referenced=any(path.name in content for content in references)
            reason=('active workspace' if active else 'incomplete reference audit' if problems else
                    'referenced by retained evidence' if referenced else 'within retention period' if age<min_age_days else
                    'no supplied reference found; manual review required')
            rows.append(dict(path=str(path.relative_to(root)),bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),
                             age_days=round(age,3),action='review_candidate' if reason.startswith('no supplied') else 'retain',reason=reason))
    return dict(schema_version=1,operation='read_only_audit',automatic_deletion=False,
                workspace=str(root),reference_inventory_complete=False,
                problems=problems,files=rows,
                note='Candidate is not authorization to delete. Preserve logs, checkpoint references, memory sources, branch bases and active child snapshots.')
