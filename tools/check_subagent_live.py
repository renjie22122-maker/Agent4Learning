"""Actual parent LLM delegates two hidden random facts to readonly child LLMs."""
from pathlib import Path
import sys, tempfile, json, time, secrets
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.llmconfig import LLMConfig
from agentplat.llm import OpenAIChatClient
from agentplat.loop import CodingAgent
from agentplat.workspace import Workspace
from agentplat.guard import CostGuard
from agentplat.runtime import CapabilityPolicy

cfg=replace(LLMConfig.load(),reasoning_effort='low')
with tempfile.TemporaryDirectory(prefix='subagent-live-') as td:
    root=Path(td);ws=Workspace(root/'workspace');ws.execution_mode='local'
    facts={name:secrets.token_hex(6) for name in ('alpha','beta')}
    for name,value in facts.items():(ws.root/(name+'.txt')).write_text(name+'='+value,encoding='utf-8')
    agent=CodingAgent(OpenAIChatClient(cfg),cfg,workspace=ws,guard=CostGuard(),session_dir=root/'sessions',hard_iterations=16)
    allowed=frozenset({'spawn_agent','wait_agent','get_agent','finish'})
    agent.tools={k:v for k,v in agent.tools.items() if k in allowed};agent.capabilities=CapabilityPolicy(allowed,False,False,False)
    started=time.monotonic()
    result=agent.run('这是子任务编排实测。主任务仅能调用委派、等待、查询和完成工具。请在同一模型步骤创建两个 readonly 子任务，每个 token_budget=30000；分别读取 alpha.txt 和 beta.txt 中的完整值，并尽快 finish 返回原文。等待两个子任务完成后，用 finish 汇总两个文件的完整值。不要凭空推测文件内容，不需要其他检查。')
    children=[item['data'] for item in agent.children.tasks.values()] if agent.children else []
    calls=[e.data for e in agent.session.of_kind('tool/call')]
    checks={'parent_completed':result.ok,'two_children':len(children)==2,
            'children_completed':len(children)==2 and all(c['status']=='completed' for c in children),
            'random_facts_correct':all(v in result.summary for v in facts.values()),
            'parent_did_not_read':not any(c.get('tool') in ('read_file','run_shell') for c in calls),
            'children_readonly':all(c['mode']=='readonly' for c in children)}
    report={'checks':checks,'elapsed_s':round(time.monotonic()-started,2),'model':cfg.model_or('mid'),
            'summary':result.summary,'error':result.error,'stop_reason':result.stopped_by,
            'parent_tokens':result.tokens_in+result.tokens_out,'parent_usd_calculated':result.usd,
            'children':children,'parent_tool_calls':calls,'expected':facts}
    output=Path(__file__).resolve().parents[1]/'.diagnostics'/'subagent-live-report.json'
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('children','parent_tool_calls')},ensure_ascii=False,indent=2))
    if not all(checks.values()):raise AssertionError('live delegation did not pass all checks')
