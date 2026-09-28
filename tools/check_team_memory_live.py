"""Real API: grandchild delegation, peer messaging and approved cross-chat memory."""
from pathlib import Path
import sys, os, tempfile, secrets, json, time
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.llmconfig import LLMConfig
from agentplat.subagents import AgentManager, TERMINAL
from agentplat.workspace import Workspace
from agentplat.llm import OpenAIChatClient
from agentplat.loop import CodingAgent
from agentplat.memory import MemoryStore


def wait(manager,key):
    deadline=time.monotonic()+180;state=manager.get(key)
    while state['status'] not in TERMINAL and time.monotonic()<deadline:
        state=manager.wait(key,1,state['revision'])
    return state


with tempfile.TemporaryDirectory(prefix='team-memory-live-') as td:
    root=Path(td);os.environ['AGENTLAB_MEMORY_DIR']=str(root/'memory')
    cfg=replace(LLMConfig.load(),reasoning_effort='low',subagent_max_depth=2)
    ws=Workspace(root/'workspace');secret=secrets.token_hex(6)
    (ws.root/'fact.txt').write_text(secret,encoding='utf-8')
    manager=AgentManager(cfg,ws,root/'team',max_workers=1,max_queue=8)
    try:
        key=manager.spawn('递归委派测试：必须调用 spawn_agent 创建一个 readonly 子任务，让它读取 fact.txt 并 finish 返回完整内容。'
                          '你不要自己读取文件；等待它完成后 finish 返回它读取的内容。只需要这一个子任务，不做其他检查。')
        state=wait(manager,key)
        descendants=[i for i in manager.tasks.values() if i['data'].get('parent_id')==key]
        parent_calls=[e.data for e in manager.tasks[key]['agent'].session.of_kind('tool/call')]
        tree_ok=(state['status']=='completed' and len(descendants)==1 and descendants[0]['data']['status']=='completed'
                 and secret in state['summary'] and not any(c['tool'] in ('read_file','read_chunk') for c in parent_calls))
        source=root/'previous-session.jsonl';remembered=secrets.token_hex(8)
        source.write_text(json.dumps({'seq':1,'kind':'session/created','data':{'task':'以后项目发布代号默认使用 '+remembered}},ensure_ascii=False),encoding='utf-8')
        store=MemoryStore();store.select_source(source,ws.root)
        row=store.list()[0];store.update(row['id'],row['content'],'preference','project','active',None,row['revision'])
        agent=CodingAgent(OpenAIChatClient(cfg),cfg,workspace=Workspace(ws.root),session_dir=root/'sessions',enable_subagents=False)
        agent.tools={k:v for k,v in agent.tools.items() if k=='finish'}
        result=agent.run('根据已确认的历史偏好，回答这个项目的默认发布代号。无需读文件，直接 finish 给出完整代号。')
        memory_ok=result.ok and remembered in result.summary
        report={'recursive_delegation':tree_ok,'memory_recalled_in_new_chat':memory_ok,
                'children':[v['data'] for v in manager.tasks.values()],
                'memory_summary':result.summary,'memory_recall_events':len(agent.session.of_kind('memory/recalled'))}
        target=Path(__file__).resolve().parents[1]/'.diagnostics'/'team-memory-live.json'
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k!='children'},ensure_ascii=False))
        assert tree_ok and memory_ok, 'live task failed; inspect saved report'
    finally:manager.close();manager.pool.shutdown(wait=True)
