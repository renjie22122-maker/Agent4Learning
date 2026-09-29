"""Paired forced strategies, with identical task and external grader.
This measures a prescribed strategy, not autonomous task decomposition quality.
"""
import argparse,json,sys,time,random
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def run_worker(path,mode):
 from tools.benchmark_agent import worker
 from agentplat.benchmark import TASKS
 spec=json.loads(Path(path).read_text())
 task=TASKS[spec['task']]
 prefix=('本次对照必须由主 Agent 直接实现，不调用普通子 Agent 或团队计划；独立验收仍由宿主管理。\n'
         if mode=='local' else
         '本次对照必须调用 spawn_agent 创建一个 isolated 实现子任务，传明确验收条件；等待完成、检查并合并产物，然后主 Agent 验证并 finish。不要创建团队计划或第二个实现子任务；独立验收仍由宿主管理。\n')
 task['prompt']=prefix+task['prompt']
 worker(path)

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--worker');ap.add_argument('--mode',choices=['local','delegate'])
 ap.add_argument('--real',action='store_true');ap.add_argument('--output',required=True)
 args=ap.parse_args()
 if args.worker:return run_worker(args.worker,args.mode)
 if not args.real:raise SystemExit('--real required')
 from agentplat.processes import ProcessSupervisor
 from agentplat.evaluation_report import envelope
 from agentplat.session import SessionLog
 root=Path(args.output).resolve();root.mkdir(parents=True,exist_ok=False)
 schedule=[(mode,i) for i in range(3) for mode in ('local','delegate')]
 random.Random(9137).shuffle(schedule);rows=[]
 for mode,i in schedule:
  folder=root/f'{mode}-{i}';folder.mkdir()
  spec=dict(task='median_repair',repeat=i,folder=str(folder),max_steps=100,review=True,timeout=600,review_profile='balanced')
  path=folder/'spec.json';path.write_text(json.dumps(spec),encoding='utf-8')
  supervisor=ProcessSupervisor()
  try:
   key=supervisor.start([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--worker',str(path),'--mode',mode,'--output',str(root)],ROOT,timeout_s=600)
   state=supervisor.wait(key,1)
   while state['status']=='running':state=supervisor.wait(key,1)
   if (folder/'result.json').exists():
    row=json.loads((folder/'result.json').read_text(encoding='utf-8'))
    logs=[SessionLog.load(p)[0] for p in (folder/'sessions').glob('*.jsonl')]
    spawn=sum(e.kind=='tool/call' and e.data.get('tool')=='spawn_agent' for log in logs for e in log.events)
    plans=sum(e.kind=='tool/call' and e.data.get('tool')=='plan_team' for log in logs for e in log.events)
    conforming=(spawn==0 if mode=='local' else spawn==1) and plans==0
    row.update(mode=mode,conforming=conforming,spawn_calls=spawn)
   else:row=dict(mode=mode,repeat=i,passed=False,conforming=False,classification=state['status'],diagnostic=state['output'][-1000:])
   rows.append(row)
   observations=[dict(category='coding:median_repair',model=r['model'],mode=r['mode'],
                      passed=bool(r['passed'] and r.get('all_turns_completed')),elapsed_s=r['elapsed_s'],
                      total_usd=r['total_usd_estimate'],independently_graded=True)
                 for r in rows if r.get('conforming') and r.get('grader_details') and r.get('elapsed_s') is not None]
   report=dict(schema_version=1,observations=observations,results=rows,
               interpretation='Randomized prescribed strategy comparison; not autonomous planning performance',
               evaluation=envelope('task_benchmark',[dict(id=f"{r['mode']}:{r['repeat']}",status='error' if not r.get('conforming') else 'passed' if r.get('passed') and r.get('all_turns_completed') else 'failed') for r in rows],{'runs':3,'task':'median_repair','timeout':600,'max_steps':100,'order_seed':9137},planned_cases=6))
   (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
   print(json.dumps({k:row.get(k) for k in ('mode','repeat','passed','conforming','elapsed_s','total_usd_estimate','classification')},ensure_ascii=False),flush=True)
  finally:supervisor.close()

if __name__=='__main__':main()

