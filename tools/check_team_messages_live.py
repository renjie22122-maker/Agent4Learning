"""Real API sibling-to-sibling message delivery with a hidden random fact."""
from pathlib import Path
import sys,tempfile,time,json,secrets
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.llmconfig import LLMConfig
from agentplat.workspace import Workspace
from agentplat.subagents import AgentManager,TERMINAL

with tempfile.TemporaryDirectory(prefix='team-message-live-') as td:
    root=Path(td);cfg=replace(LLMConfig.load(),reasoning_effort='low')
    manager=AgentManager(cfg,Workspace(root/'ws'),root/'team',max_workers=2)
    marker=secrets.token_hex(8)
    try:
        recipient=manager.spawn('你是消息接收者。另一个团队成员稍后会通过 send_agent_message 给你发送随机代号。'
            '先用 team_state 写 key=receiver_ready,value=yes,expected_revision=0。'
            '收到团队消息前不要 finish，也不要自己创建子任务；可查询 team_state 的 delivery 标记等待。'
            '收到消息后立刻 finish 返回消息中的完整随机代号。')
        sender=manager.spawn(f'你是消息发送者。只需调用 send_agent_message，agent_id={recipient}，message="随机代号：{marker}"。'
            '发送成功后用 team_state 写 key=delivery,value=sent,expected_revision=0，然后 finish。不要创建子任务。')
        deadline=time.monotonic()+150
        for key in (sender,recipient):
            state=manager.get(key)
            while state['status'] not in TERMINAL and time.monotonic()<deadline:
                state=manager.wait(key,1,state['revision'])
        a,b=manager.get(sender),manager.get(recipient)
        calls=manager.tasks[sender]['agent'].session.of_kind('tool/call')
        checks={'sender_completed':a['status']=='completed','recipient_completed':b['status']=='completed',
                'direct_message_tool_used':any(e.data.get('tool')=='send_agent_message' for e in calls),
                'message_delivered':b.get('delivered_messages',0)>0,'random_fact_received':marker in b['summary']}
        report={'checks':checks,'sender':a,'recipient':b}
        output=Path(__file__).resolve().parents[1]/'.diagnostics'/'team-messages-live.json'
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(checks));assert all(checks.values())
    finally:manager.close();manager.pool.shutdown(wait=True)
