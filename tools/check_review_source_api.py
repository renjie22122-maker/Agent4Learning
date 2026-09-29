"""Opt-in real reviewer test using an isolated, referenced source fixture."""
import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    if len(sys.argv)!=3 or sys.argv[1]!='--real':
        raise SystemExit('Usage: python tools/check_review_source_api.py --real NEW_OUTPUT_DIRECTORY')
    root=Path(sys.argv[2]).resolve();root.mkdir(parents=True,exist_ok=False)
    for name,folder in [('AGENTLAB_MEMORY_DIR','memory'),('AGENTLAB_KB_DIR','knowledge'),('AGENTLAB_HUMAN_DB','human.sqlite3')]:
        os.environ[name]=str(root/folder)
    from agentplat.experiments import ScriptedModel
    from agentplat.llmconfig import LLMConfig
    from agentplat.loop import CodingAgent
    from agentplat.workspace import Workspace
    from agentplat.subagents import AgentManager
    cfg=LLMConfig.load(); cfg.memory_enabled=False;cfg.max_tokens=min(cfg.max_tokens,2048)
    ws=Workspace(root/'workspace');ws.execution_mode='local'
    (ws.root/'.spill').mkdir();(ws.root/'.spill/source-fixture.txt').write_text('numbers: 1, 2, 3\ntotal: 6\n',encoding='utf-8')
    script=[[('write_file',{'path':'result.txt','content':'6'})],
            [('write_file',{'path':'report.md','content':'result.txt is the sum of numbers in .spill/source-fixture.txt.'})],
            [('run_shell',{'command':'python -c "from pathlib import Path; assert Path(\'result.txt\').read_text()==\'6\'"'})],
            [('finish',{'summary':'已生成 result.txt 和来源说明并验证'})]]
    stop=threading.Event();manager=AgentManager(cfg,ws,root/'children',parent_cancel=stop)
    manager.run_deadline=time.monotonic()+120
    agent=CodingAgent(ScriptedModel(script),cfg,workspace=ws,session_dir=root/'logs',stop_flag=stop,enable_subagents=False,hard_iterations=4)
    agent.child_manager=lambda:manager;agent.children=manager;agent.independent_review_required=True
    watchdog=threading.Timer(130,stop.set);watchdog.start()
    try:
        result=agent.run('创建 result.txt，内容为来源 .spill/source-fixture.txt 中三个数的总和；report.md 指明来源。验收只需直接读取该来源、读取 result.txt 并运行一次求和断言；这是有限来源快照测试，不需要搜索或检查其他项目。')
        child=next(iter(manager.tasks.values()))['data']
        report={'passed':result.ok,'stopped_by':result.stopped_by,'error':result.error,
                'review_status':child['status'],'source_snapshot':child.get('source_snapshot'),
                'source_snapshot_intact':child.get('source_snapshot_intact'),
                'parent_model_steps':agent.client.turn if hasattr(agent,'client') else 4,
                'review_tokens':child.get('used_tokens'),'review_summary':child.get('summary','')}
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False))
        assert result.ok,result.error
        assert child.get('source_snapshot_intact') and child.get('source_snapshot')
    finally:
        watchdog.cancel();stop.set();manager.close();manager.pool.shutdown(wait=True)


if __name__=='__main__':main()
