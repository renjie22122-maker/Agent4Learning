"""Opt-in real-model smoke checks in isolated, projectless conversations."""
import argparse,json,os,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--real',action='store_true');ap.add_argument('--output',required=True)
    args=ap.parse_args()
    if not args.real:raise SystemExit('Explicit --real is required')
    folder=Path(args.output).resolve();folder.mkdir(parents=True,exist_ok=False)
    os.environ['AGENTLAB_MEMORY_DIR']=str(folder/'memory')
    os.environ['AGENTLAB_KB_DIR']=str(folder/'knowledge')
    os.environ['AGENTLAB_HUMAN_DB']=str(folder/'human.sqlite3')
    from agentplat import approvals,plugins,attachments
    approvals.DATABASE=folder/'approvals.sqlite3';plugins.CONFIG=folder/'plugins.json';attachments.ROOT=folder/'attachments'
    from agentplat.workspace import Workspace
    from agentplat.loop import CodingAgent
    from agentplat.model_client import create_client
    from agentplat.llmconfig import LLMConfig
    from agentplat.guard import CostGuard
    from agentplat.general_chat import install
    cfg=LLMConfig.load()
    if not cfg.is_real:raise SystemExit('Real model configuration required')
    rows=[]
    for number,prompt in enumerate([
        '用两句话解释沙箱和工作区有什么区别。',
        'Explain the difference between a process and a thread in two sentences.',
        '给我一个判断整数是否为偶数的 Python 函数，并简单解释。',
    ]):
        ws=Workspace(folder/str(number)/'files');ws.general_chat=True;ws.allow_shell=False
        agent=CodingAgent(create_client(cfg),cfg,workspace=ws,session_dir=folder/str(number)/'sessions',
                          guard=CostGuard(max_usd=None,max_calls=None),hard_iterations=8,enable_subagents=False)
        install(agent);agent.run_deadline=time.monotonic()+150
        start=time.monotonic();result=agent.run(prompt,model=cfg.model_or('mid') or cfg.model)
        text=result.summary;first=text.strip().split('\n')[0] if text else ''
        fixed=any(s in first.lower() for s in ['本轮总结','本轮结论','本次任务','round summary','this round','宿主最终验收'])
        files=[str(p.relative_to(ws.root)) for p in ws.root.rglob('*') if p.is_file()]
        rows.append(dict(prompt=prompt,reply=text,completed=result.ok,fixed_opener=fixed,
                         model_calls=result.model_calls,tool_calls=result.tool_calls,files=files,
                         elapsed_s=round(time.monotonic()-start,2),usd=result.usd))
        (folder/'report.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in rows[-1].items() if k not in ('reply','prompt')},ensure_ascii=False),flush=True)
    if not all(r['completed'] and not r['fixed_opener'] and not r['files'] for r in rows):raise SystemExit(1)


if __name__=='__main__':main()
