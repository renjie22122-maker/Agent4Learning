"""Uniform post-run decimal precision checks prompted by independent review."""
import argparse,json,shutil,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.workspace import Workspace

CHECK='''from totals import totals
big='9'*30
assert totals('name,amount\\nx,'+big+'.99\\n')=={'x':big+'.99'}
assert totals('name,amount\\nx,'+big+'.00\\nx,0.01\\nx,-'+big+'.00\\n')=={'x':'0.01'}
print('DECIMAL_HOLDOUT_PASS')
'''

def run(root):
    root=Path(root);rows=[]
    for folder in sorted(root.glob('csv_totals-*')):
        source=folder/'workspace'/'totals.py'
        if not source.exists():rows.append({'trial':folder.name,'passed':False,'reason':'missing artifact'});continue
        ws=Workspace(folder/'supplemental-grader');shutil.copyfile(source,ws.root/'totals.py')
        (ws.root/'check.py').write_text(CHECK,encoding='utf-8')
        try:
            output=ws.run('python check.py',timeout_s=15)
            rows.append({'trial':folder.name,'passed':ws.last_execution.get('exit_code')==0 and 'DECIMAL_HOLDOUT_PASS' in output,'details':output})
        finally:ws.processes.close()
    (root/'supplemental-decimal.json').write_text(json.dumps({'post_hoc':True,'model_feedback':False,'original_scores_modified':False,'results':rows},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(rows,ensure_ascii=False))

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('root');a=ap.parse_args();run(a.root)
