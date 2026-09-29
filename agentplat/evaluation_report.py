"""Common, additive envelope for task benchmarks and specialist experiments."""
from datetime import datetime, timezone
import platform
import subprocess

STATUSES = frozenset(('passed', 'failed', 'skipped', 'error', 'measured', 'unknown'))


def envelope(kind, cases, configuration=None, *, planned_cases=None):
    if kind not in ('task_benchmark', 'integration', 'retrieval', 'runtime_contract'):
        raise ValueError('Unknown evaluation kind')
    cases = list(cases)
    ids = [c['id'] for c in cases]
    if len(set(ids)) != len(ids) or any(c.get('status') not in STATUSES for c in cases):
        raise ValueError('Duplicate case or invalid outcome')
    if planned_cases is not None and planned_cases < len(cases):
        raise ValueError('Observed cases exceed plan')
    try:
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], timeout=5,
                                          stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], timeout=5,
                                            stderr=subprocess.DEVNULL, text=True).strip())
    except (OSError, subprocess.SubprocessError):
        revision, dirty = None, None
    counts = {s: sum(c['status'] == s for c in cases) for s in sorted(STATUSES)}
    denominator = counts['passed'] + counts['failed']
    return dict(schema_version=1, kind=kind, created_at=datetime.now(timezone.utc).isoformat(),
                source=dict(commit=revision, dirty=dirty),
                environment=dict(system=platform.system(), python=platform.python_version()),
                configuration=configuration or {}, planned_cases=planned_cases,
                observed_cases=len(cases), complete=None if planned_cases is None else len(cases)==planned_cases,
                counts=counts, graded_cases=denominator,
                pass_rate=counts['passed']/denominator if denominator else None, cases=cases)


def benchmark_cases(rows):
    return [dict(id=f"{r['task']}:{r['repeat']}",
                 status='error' if r.get('classification') else
                 ('passed' if r.get('passed') is True else 'failed' if r.get('passed') is False else 'unknown'))
            for r in rows]


def inspect_report(report):
    """Read old formats without inventing tasks/runs or rewriting evidence."""
    if 'evaluation' in report:
        value = report['evaluation']
        if value.get('schema_version') != 1:
            return {'kind':'unknown', 'observed_cases':None, 'reason':'Unsupported schema version'}
        return value
    if isinstance(report.get('results'), list):
        return dict(kind='task_benchmark', observed_cases=len(report['results']),
                    configuration=report.get('configuration'), legacy=True)
    if isinstance(report.get('checks'), dict):
        return dict(kind='integration', observed_cases=len(report['checks']), legacy=True)
    if isinstance(report.get('passed'), bool):
        return dict(kind='integration', observed_cases=1, legacy=True)
    if isinstance(report.get('memory'), dict) and isinstance(report['memory'].get('cases'), list):
        return dict(kind='retrieval', observed_cases=len(report['memory']['cases'])+len(report.get('ann',[])), legacy=True)
    return dict(kind='unknown', observed_cases=None, reason='Unrecognized report; cannot infer sample count')
