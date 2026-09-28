"""Read-only trajectory evaluation; finish tool output is not a completed run."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.session import SessionLog
from agentplat.benchmark import trace_metrics


def audit(path,start_seq):
    log,skipped=SessionLog.load(Path(path))
    events=[e for e in log.events if e.seq>=start_seq]
    next_start=next((i for i,e in enumerate(events) if e.kind=='run/started' and e.seq>start_seq),len(events))
    events=events[:next_start]
    closed=[e for e in events if e.kind=='session/closed']
    settled=[e for e in events if e.kind=='run/settled']
    report={'source':str(path),'start_seq':start_seq,'skipped_lines':skipped,'metrics':trace_metrics(events),
            'completed':bool(closed and closed[-1].data.get('finished') and settled and settled[-1].data.get('status')=='done'),
            'elapsed_s':round(events[-1].ts-events[0].ts,3) if events else 0,'review_transitions':[]}
    previous=None
    for e in events:
        if e.kind!='independent_review/started':continue
        edits=[x.data.get('path') for x in events if previous and previous.seq<x.seq<e.seq and x.kind=='tool/call' and x.data.get('tool') in ('write_file','edit_file','append_file','delete_file')]
        report['review_transitions'].append({'seq':e.seq,'agent_id':e.data.get('agent_id'),'edits_since_previous_review':edits,
                                            'note':'diagnostic only: path alone cannot prove semantic irrelevance'})
        previous=e
    return report


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('session');ap.add_argument('--start-seq',type=int,required=True);ap.add_argument('--output',required=True)
    a=ap.parse_args();result=audit(a.session,a.start_seq);Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False,indent=2))
