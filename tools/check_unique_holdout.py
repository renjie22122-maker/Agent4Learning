"""Post-run supplemental mixed-type checks, never fed back to evaluated agents."""
import argparse,json,shutil,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.workspace import Workspace

CHECK='''from unique import select_unique
import copy
cases=[[{1},frozenset({1})],[frozenset({1}),{1}],[1,True,1.0],[{},{}],[[1],[1],[2]]]
for xs in cases:
 before=copy.deepcopy(xs);expected=[]
 for x in xs:
  if not any(x==y for y in expected):expected.append(x)
 assert select_unique(xs)==expected,repr(xs)
 for limit in [0,1,2,5]:assert select_unique(xs,limit=limit)==expected[:limit],repr((xs,limit))
 assert xs==before
print('MIXED_TYPE_HOLDOUT_PASS')
'''

def run(root):
    root=Path(root);results=[]
    for folder in sorted(root.glob('followup_constraints-*')):
        source=folder/'workspace'/'unique.py'
        if not source.exists():results.append({'trial':folder.name,'passed':False,'reason':'missing artifact'});continue
        ws=Workspace(folder/'supplemental-grader')
        shutil.copyfile(source,ws.root/'unique.py');(ws.root/'check.py').write_text(CHECK,encoding='utf-8')
        try:
            output=ws.run('python check.py',timeout_s=15)
            passed=ws.last_execution.get('exit_code')==0 and 'MIXED_TYPE_HOLDOUT_PASS' in output
            results.append({'trial':folder.name,'passed':passed,'details':output})
        finally:ws.processes.close()
    report={'post_hoc':True,'model_feedback':False,'original_scores_modified':False,'results':results}
    (root/'supplemental-mixed-types.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=False))

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('root');a=ap.parse_args();run(a.root)
