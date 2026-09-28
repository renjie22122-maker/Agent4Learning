"""Reproducible local agent evaluation with external grading and hard worker timeout."""
import argparse,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agentplat.benchmark import TASKS,grade,reliability,trace_metrics


def wait_until_exit(supervisor, identifier):
    state=supervisor.wait(identifier,1)
    while state['status']=='running':
        state=supervisor.wait(identifier,1)
    return state


def worker(spec_path):
    import os
    spec=json.loads(Path(spec_path).read_text());folder=Path(spec['folder']);task=TASKS[spec['task']]
    os.environ['AGENTLAB_MEMORY_DIR']=str(folder/'private-memory')
    os.environ['AGENTLAB_KB_DIR']=str(folder/'private-knowledge')
    os.environ['AGENTLAB_HUMAN_DB']=str(folder/'private-human.sqlite3')
    from agentplat import approvals
    approvals.DATABASE=folder/'private-approvals.sqlite3'
    from agentplat import plugins,attachments
    plugins.CONFIG=folder/'empty-plugins.json'
    attachments.ROOT=folder/'private-attachments'
    from agentplat.workspace import Workspace
    from agentplat.loop import CodingAgent
    from agentplat.llm import OpenAIChatClient
    from agentplat.llmconfig import LLMConfig
    from agentplat.guard import CostGuard
    ws=Workspace(folder/'workspace')
    for name,content in task['files'].items(): (ws.root/name).write_text(content,encoding='utf-8')
    cfg=LLMConfig.load()
    from agentplat.knowledge import KnowledgeBase
    from agentplat.memory import MemoryStore
    assert not MemoryStore().list()
    kb=KnowledgeBase(ws.knowledge_root)
    assert not kb.list_documents()
    for name,content in task.get('knowledge',{}).items():
        doc=folder/name;doc.write_text(content,encoding='utf-8');kb.import_file(doc)
    from agentplat.vector_knowledge import build as build_vectors
    vector_index=build_vectors(kb)
    if not cfg.is_real:raise RuntimeError('Real API not configured')
    # Fail environment checks before spending on the model.
    ws.run('python -V',timeout_s=15)
    if not ws.last_execution or ws.last_execution.get('exit_code')!=0:raise RuntimeError('Sandbox preflight failed')
    agent=CodingAgent(OpenAIChatClient(cfg),cfg,workspace=ws,guard=CostGuard(max_usd=None,max_calls=None),
                      session_dir=folder/'sessions',hard_iterations=spec['max_steps'],enable_subagents=spec['review'])
    agent.independent_review_required=spec['review']
    initial_history=sum(e.kind.startswith(('conversation/','followup/','memory/recalled')) for e in agent.session.events)
    assert initial_history==0
    if spec['task']=='browser_counter':
        from agentplat.browser_tools import install
        install(agent)
    start=time.monotonic()
    agent.run_deadline=start+spec.get('timeout',300)
    result=agent.run(task['prompt'])
    results=[result]
    if task.get('followup'):
        result=agent.continue_with(task['followup']);results.append(result)
    passed,details=grade(spec['task'],ws.root,folder/'grader')
    children=[t['data'] for t in agent.children.tasks.values()] if agent.children else []
    import hashlib
    row=dict(task=spec['task'],repeat=spec['repeat'],passed=passed,declared_ok=result.ok,
             all_turns_completed=all(r.ok for r in results),turn_stop_reasons=[r.stopped_by for r in results],
             clean_start=True,history_events_before_run=initial_history,memory_root=str(folder/'private-memory'),knowledge_root=str(ws.knowledge_root),
             vector_index=vector_index,
             task_hash=hashlib.sha256(json.dumps(task,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
             false_success=bool(result.ok and not passed),stop_reason=result.stopped_by,
             elapsed_s=round(time.monotonic()-start,3),model=cfg.model_or('mid') or cfg.model,
             execution_mode=ws.execution_mode,parent_usd_estimate=sum(r.usd for r in results),
             child_usd_estimate=sum(x.get('usd',0) for x in children),
             child_tokens=sum(x.get('used_tokens',0) for x in children),
             grader_details=details,metrics=trace_metrics(agent.session.events))
    (folder/'result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--real',action='store_true');ap.add_argument('--worker')
    ap.add_argument('--runs',type=int,default=2);ap.add_argument('--tasks',nargs='+',choices=list(TASKS),default=list(TASKS))
    ap.add_argument('--review',action='store_true');ap.add_argument('--timeout',type=int,default=240)
    ap.add_argument('--jobs',type=int,default=1);ap.add_argument('--max-steps',type=int,default=24);ap.add_argument('--output',default='.diagnostics/benchmark')
    args=ap.parse_args()
    if args.worker:return worker(args.worker)
    if args.runs<1 or not 1<=args.timeout<=3600 or args.max_steps<1 or not 1<=args.jobs<=3:ap.error('invalid bounds')
    if not args.real:
        print(json.dumps({'tasks':args.tasks,'runs':args.runs,'review':args.review,'model_calls':0},indent=2));return
    from agentplat.processes import ProcessSupervisor
    out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=False)
    from concurrent.futures import ThreadPoolExecutor,as_completed
    rows=[]
    def trial(task,repeat):
        supervisor=ProcessSupervisor()
        folder=out/f'{task}-{repeat}';folder.mkdir()
        spec=dict(task=task,repeat=repeat,folder=str(folder),max_steps=args.max_steps,review=args.review,timeout=args.timeout)
        path=folder/'spec.json';path.write_text(json.dumps(spec))
        try:
            identifier=supervisor.start([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--worker',str(path)],ROOT,timeout_s=args.timeout)
            state=wait_until_exit(supervisor,identifier)
            result=folder/'result.json'
            if result.exists():return json.loads(result.read_text(encoding='utf-8'))
            return dict(task=task,repeat=repeat,passed=False,declared_ok=False,classification='timeout' if state['status']=='timeout' else 'harness_or_environment_error',diagnostic=str(state)[-2000:])
        finally:supervisor.close()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        pending=[pool.submit(trial,task,repeat) for task in args.tasks for repeat in range(args.runs)]
        for future in as_completed(pending):
            row=future.result();rows.append(row)
            report=dict(suite='agent4learning-clean-v3',official_benchmark=False,configuration=vars(args),
                        results=sorted(rows,key=lambda r:(r['task'],r['repeat'])),reliability=reliability(rows),
                        artifact_reliability=reliability(rows),
                        workflow_reliability=reliability([{**r,'passed':bool(r.get('passed') and r.get('all_turns_completed',r.get('declared_ok')))} for r in rows]),
                        false_successes=sum(r.get('false_success',False) for r in rows))
            temp=out/'report.tmp';temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(out/'report.json')
            print(json.dumps({k:row.get(k) for k in ['task','repeat','passed','declared_ok','elapsed_s','classification']},ensure_ascii=False),flush=True)



if __name__=='__main__':main()
