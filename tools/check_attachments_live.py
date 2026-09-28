"""Real API reads a random uploaded fact through scoped attachment tools."""
from pathlib import Path
import sys,json,tempfile,base64,secrets
from dataclasses import replace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat import attachments
from agentplat.llmconfig import LLMConfig
from agentplat.llm import OpenAIChatClient
from agentplat.loop import CodingAgent
from agentplat.workspace import Workspace

with tempfile.TemporaryDirectory(prefix='attachments-live-') as td,patch.object(attachments,'ROOT',Path(td)/'attachments'):
    root=Path(td);fact=secrets.token_hex(12)
    item=attachments.upload('facts.csv',base64.b64encode(('key,value\nsecret,'+fact+'\n').encode()).decode())
    cfg=replace(LLMConfig.load(),reasoning_effort='low')
    agent=CodingAgent(OpenAIChatClient(cfg),cfg,workspace=Workspace(root/'workspace'),session_dir=root/'sessions',hard_iterations=8,enable_subagents=False)
    attachments.bind(agent,[item])
    result=agent.run('请读取附件 facts.csv，调用 finish 返回 secret 对应的完整值。不修改任何文件。'+attachments.describe([item]))
    calls=[e.data.get('tool') for e in agent.session.of_kind('tool/call')]
    report={'ok':result.ok,'fact_matched':fact in result.summary,'attachment_read':any(c in ('read_attachment','search_attachment') for c in calls),'calls':calls,'error':result.error}
    (Path(__file__).resolve().parents[1]/'.diagnostics'/'attachments-live.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report));assert report['ok'] and report['fact_matched'] and report['attachment_read']
