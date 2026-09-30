"""Real-model plan-only environment smoke test; never approves host execution."""
import argparse,json,os,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main():
    p=argparse.ArgumentParser();p.add_argument('--real',action='store_true');p.add_argument('--output',required=True)
    args=p.parse_args()
    if not args.real:raise SystemExit('Explicit --real required')
    root=Path(args.output).resolve();root.mkdir(parents=True,exist_ok=False)
    for key,suffix in [('AGENTLAB_MEMORY_DIR','memory'),('AGENTLAB_KB_DIR','knowledge'),('AGENTLAB_HUMAN_DB','human.sqlite3')]:os.environ[key]=str(root/suffix)
    from agentplat import approvals,plugins,attachments,dev_environments
    approvals.DATABASE=root/'approvals.sqlite3';plugins.CONFIG=root/'plugins.json';attachments.ROOT=root/'attachments'
    dev_environments.BASE=root/'environments'
    from agentplat.workspace import Workspace
    from agentplat.loop import CodingAgent
    from agentplat.llmconfig import LLMConfig
    from agentplat.model_client import create_client
    from agentplat.guard import CostGuard
    cfg=LLMConfig.load()
    if not cfg.is_real:raise SystemExit('Real model configuration required')
    agent=CodingAgent(create_client(cfg),cfg,workspace=Workspace(root/'workspace'),session_dir=root/'sessions',
                      hard_iterations=8,enable_subagents=False,guard=CostGuard(max_usd=None,max_calls=None))
    allowed={'inspect_development_environment','list_development_environments','plan_development_environment','finish'}
    agent.tools={k:v for k,v in agent.tools.items() if k in allowed}
    agent.run_deadline=time.monotonic()+180
    spec=dict(name='existing-python',version='.'.join(map(str,sys.version_info[:3])),kind='existing',runtime=sys.executable,
              version_args=['--version'],smoke=[['{runtime}','-c','print(2+2)']])
    result=agent.run('Inspect available Python environment hints and register this existing-runtime plan using the environment tools. '
                     'Do not prepare, verify, retire, install or execute anything. This request is only to save the plan for later user approval. '
                     'Explain its state and the approval needed next. Exact plan: '+json.dumps(spec),model=cfg.model_or('mid') or cfg.model)
    rows=dev_environments.Environments().list(agent.ws.root)
    passed=result.ok and len(rows)==1 and rows[0]['state']=='planned' and rows[0]['plan']['spec']==spec and rows[0]['receipt']=={}
    report=dict(passed=passed,completed=result.ok,summary=result.summary,model_calls=result.model_calls,
                tools=result.tool_calls,states=[r['state'] for r in rows],host_commands_executed=False,usd=result.usd,
                scope='real-model plan tool selection only; execution/approval tested separately')
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='summary'}))
    if not passed:raise SystemExit(1)


if __name__=='__main__':main()
