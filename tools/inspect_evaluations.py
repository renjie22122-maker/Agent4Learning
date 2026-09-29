"""Read-only inventory of evaluation formats; unknown is never reported as 0/0."""
import sys, json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.evaluation_report import inspect_report

if __name__ == '__main__':
    root=Path(sys.argv[1])
    paths=sorted(root.rglob('*.json')) if root.is_dir() else [root]
    for path in paths:
        try:
            raw=json.loads(path.read_text(encoding='utf-8'))
            info=inspect_report(raw) if isinstance(raw,dict) else {'kind':'unknown','observed_cases':None}
            print(json.dumps({'path':str(path),**{k:info.get(k) for k in ('kind','observed_cases','complete','counts','reason')}},ensure_ascii=False))
        except (OSError,ValueError,TypeError) as exc:
            print(json.dumps({'path':str(path),'kind':'unreadable','error':type(exc).__name__}))
