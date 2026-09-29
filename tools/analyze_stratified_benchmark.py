"""Read-only statistical analysis: retain failures, separate artifact and workflow success."""
import argparse,json,statistics,math
from pathlib import Path

def wilson(successes,n):
 if not n:return None
 z=1.959963984540054;p=successes/n;d=1+z*z/n
 center=(p+z*z/(2*n))/d;radius=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
 return [max(0,center-radius),min(1,center+radius)]

def analyze(report):
 from collections import defaultdict
 groups=defaultdict(list);categories=defaultdict(list)
 for row in report.get('results',[]):
  groups[row['task']].append(row);categories[row.get('category','unknown')].append(row)
 def stats(rows):
  success=lambda r:bool(r.get('passed') and r.get('all_turns_completed',r.get('declared_ok',False)))
  elapsed=sorted(r['elapsed_s'] for r in rows if isinstance(r.get('elapsed_s'),(int,float)))
  costs=[r['total_usd_estimate'] for r in rows if isinstance(r.get('total_usd_estimate'),(int,float))]
  return dict(n=len(rows),artifact_passed=sum(r.get('passed') is True for r in rows),
              workflow_passed=sum(success(r) for r in rows),
              false_successes=sum(r.get('false_success') is True for r in rows),
              harness_errors=sum(bool(r.get('classification')) for r in rows),
              elapsed_observed=len(elapsed),p50_seconds=statistics.median(elapsed) if elapsed else None,
              p95_seconds=elapsed[max(0,math.ceil(len(elapsed)*.95)-1)] if elapsed else None,
              cost_observed=len(costs),known_cost_usd=sum(costs),
              total_cost_usd=sum(costs) if len(costs)==len(rows) else None)
 tasks={key:stats(rows) for key,rows in groups.items()}
 for value in tasks.values():value['workflow_wilson95']=wilson(value['workflow_passed'],value['n'])
 result=dict(schema_version=1,planned_cases=report.get('evaluation',{}).get('planned_cases'),
             observed_cases=len(report.get('results',[])),overall=stats(report.get('results',[])),
             tasks=tasks,categories={key:stats(rows) for key,rows in categories.items()},
             caveats=['Per-task Wilson intervals assume comparable independent runs; n=3 is weak evidence.',
                      'No pooled confidence interval across heterogeneous tasks.',
                      'Timeout/error rows remain in success denominators; missing cost remains unknown.',
                      'Pricing-derived USD is not the final provider invoice.'])
 return result

def main():
 ap=argparse.ArgumentParser();ap.add_argument('report');ap.add_argument('--output');a=ap.parse_args()
 report=analyze(json.loads(Path(a.report).read_text(encoding='utf-8')))
 if a.output:Path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':main()

