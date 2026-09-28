"""Opt-in real API check: child reads an unpredictable fact and reports to root."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dataclasses import replace
import json,secrets,tempfile,threading
from agentplat.llmconfig import LLMConfig
from agentplat.llm import OpenAIChatClient
from agentplat.loop import CodingAgent
from agentplat.workspace import Workspace

with tempfile.TemporaryDirectory(prefix='team-coordination-live-') as td:
    root=Path(td);ws=Workspace(root/'ws')
    marker=secrets.token_hex(12)
    (ws.root/'fact.txt').write_text(marker,encoding='utf-8')
    cfg=replace(LLMConfig.load(),reasoning_effort='low',max_tokens=2048)
    stop=threading.Event();timer=threading.Timer(180,stop.set);timer.start()
    agent=CodingAgent(OpenAIChatClient(cfg),cfg,workspace=ws,session_dir=root/'sessions',stop_flag=stop)
    try:
        result=agent.run('这是团队通信验收。你是主 Agent，不能自己读 fact.txt。只创建一个 readonly 子 Agent，要求它读取 fact.txt，'
          '然后用 send_agent_message 给 agent_id=root 发送文件内完整代号，dedup_key=fact-report，最后 finish。'
          '你收到消息后用 ack_team_message 确认消息 ID，等待子任务结束后 finish，结论包含完整代号。不要写文件或创建其他子任务。')
        manager=agent.children
        mail=manager.coordination.messages('root') if manager else []
        calls=[e.data.get('tool') for e in agent.session.of_kind('tool/call')]
        checks={'root_completed':result.ok,'random_fact_received':marker in result.summary,
                'root_did_not_read_file':'read_file' not in calls,
                'child_sent_root_message':any(m['kind']=='message' and marker in m['body'] for m in mail),
                'explicit_ack':any(m['acknowledged'] for m in mail if m['kind']=='message'),
                'completion_event':any(m['kind']=='task_finished' for m in mail)}
        report={'checks':checks,'summary':result.summary,'error':result.error,
                'root_tokens':result.tokens_in+result.tokens_out,'team_tokens':manager.budget.spent if manager else 0}
        target=Path(__file__).resolve().parents[1]/'.diagnostics/team-coordination-live.json'
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(checks));assert all(checks.values()),report
    finally:
        timer.cancel();stop.set()
        if agent.children:agent.children.close();agent.children.pool.shutdown(wait=True)
