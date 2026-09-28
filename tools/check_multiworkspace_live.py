"""Paid model integration in temporary folders; never edits user artifacts."""
from pathlib import Path
import sys, tempfile, json, secrets
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.workspace import Workspace
from agentplat.loop import CodingAgent
from agentplat.llm import OpenAIChatClient
from agentplat.llmconfig import LLMConfig

with tempfile.TemporaryDirectory(prefix='multiworkspace-live-') as td:
    root=Path(td); folders={name:root/name for name in ('app','docs')}
    for folder in folders.values():folder.mkdir()
    marker=secrets.token_hex(8)
    (folders['docs']/'input.txt').write_text(marker)
    cfg=replace(LLMConfig.load(),reasoning_effort='low')
    ws=Workspace(folders)
    # Trusted temporary fixture: native isolation is checked separately; no global policy change.
    ws.execution_mode='local'
    agent=CodingAgent(OpenAIChatClient(cfg),cfg,workspace=ws,session_dir=root/'sessions',hard_iterations=15)
    result=agent.run('多文件夹集成检查：读取 @docs/input.txt，将其原样写入 @app/result.txt 和 @docs/result.txt。'
                    '分别用 run_shell 的 cwd=@app 和 cwd=@docs 执行 python 检查各目录 result.txt 存在并打印内容。'
                    '完成后调用 finish 返回读到的原始字符串。不要读取其他目录、联网或额外创建子任务。')
    calls=[e.data for e in agent.session.of_kind('tool/call')]
    report={'ok':result.ok,'error':result.error,'summary':result.summary,'model_calls':result.model_calls,
            'both_written':all((p/'result.txt').exists() and (p/'result.txt').read_text()==marker for p in folders.values()),
            'calls':calls}
    target=Path(__file__).resolve().parents[1]/'.diagnostics'/'multiworkspace-live.json'
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='calls'},ensure_ascii=False))
    assert report['ok'] and report['both_written'],report
