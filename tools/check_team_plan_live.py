"""Explicit real-API DAG integration test; isolated task data, no chat history."""
import json
import os
from pathlib import Path
import sys
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    if len(sys.argv)!=3 or sys.argv[1]!='--real':raise SystemExit('--real NEW_OUTPUT_DIRECTORY')
    root=Path(sys.argv[2]).resolve();root.mkdir(parents=True,exist_ok=False)
    os.environ['AGENTLAB_MEMORY_DIR']=str(root/'memory');os.environ['AGENTLAB_KB_DIR']=str(root/'knowledge')
    from agentplat.llmconfig import LLMConfig
    from agentplat.subagents import AgentManager
    from agentplat.workspace import Workspace
    from agentplat.team_planner import TeamPlanner,FINAL
    from agentplat.runtime import CapabilityPolicy
    cfg=LLMConfig.load();cfg.memory_enabled=False;cfg.max_tokens=2048
    ws=Workspace(root/'workspace');ws.execution_mode='local';cancel=threading.Event()
    manager=AgentManager(cfg,ws,root/'children',parent_cancel=cancel)
    manager.authority_provider=lambda:CapabilityPolicy();manager.run_deadline=time.monotonic()+240
    planner=TeamPlanner(manager);manager.planner=planner
    timer=threading.Timer(245,cancel.set);timer.start()
    try:
        plan=planner.create('两个并行文件与一个依赖集成任务',[{'id':'a','task':'只创建 a.txt，内容严格为 alpha，无换行。用一次 Python 断言验证，不要添加测试文件。','acceptance':'a.txt 内容严格等于 alpha'},
          {'id':'b','task':'只创建 b.txt，内容严格为 beta，无换行。用一次 Python 断言验证，不要添加测试文件。','acceptance':'b.txt 内容严格等于 beta'},
          {'id':'combine','depends_on':['a','b'],'task':'读取 a.txt 与 b.txt，创建 combined.txt，内容为前者加冒号再加后者，无换行；不要修改 a.txt 或 b.txt。用一次 Python 断言验证，不增加其他文件。','acceptance':'combined.txt 严格等于 alpha:beta，a.txt 与 b.txt 保持不变'}])
        while True:
            result=planner.wait(plan['id'],5)
            if result['status'] in FINAL or cancel.is_set():break
        verified=result['status']=='ready_for_final_review' and (ws.root/'combined.txt').read_text()=='alpha:beta'
        report={'passed':verified,'plan':result,'tokens':manager.budget.spent}
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'passed':verified,'status':result['status'],'nodes':{k:v['state'] for k,v in result['nodes'].items()},'tokens':manager.budget.spent},ensure_ascii=False))
        assert verified,result
    finally:timer.cancel();cancel.set();planner.closed=True;manager.close();manager.pool.shutdown(wait=True)


if __name__=='__main__':main()
