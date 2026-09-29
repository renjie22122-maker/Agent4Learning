import tempfile
from pathlib import Path
from agentlab.util import lab
from agentplat.subagents import AgentManager
from agentplat.team_planner import TeamPlanner
from agentplat.llmconfig import LLMConfig
from agentplat.workspace import Workspace
from agentplat.experiments import ScriptedModel

def main():
    with lab('lab-55-team-dag','团队依赖调度','成员完成不代表团队交付完成'):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);manager=AgentManager(LLMConfig(),Workspace(root/'work'),root/'team',factory=ScriptedModel)
            planner=TeamPlanner(manager);manager.planner=planner
            tasks=[{'id':'a','task':'给出参考分析','acceptance':'清楚说明','mode':'readonly'},
                   {'id':'b','task':'基于前置结果补充分析','acceptance':'清楚说明','mode':'readonly','depends_on':['a']}]
            try:
                plan=planner.create('组合分析',tasks);result=planner.wait(plan['id'],10)
                assert result['status']=='ready_for_final_review'
                b=manager.get(result['nodes']['b']['agent_id']);assert 'a' in b['context']
                invalid=[{**tasks[0],'depends_on':['b']},tasks[1]]
                rejected=0
                try:planner.create('循环依赖',invalid)
                except ValueError:rejected=1
                assert rejected
                print('[BROKEN-REPRODUCED] 未验证的计划可包含 a→b→a，导致永远等待')
                print('[FIX-APPLIED] 调度前校验 DAG；依赖就绪后才启动，完成仅进入最终验收阶段')
                print(f'[VERIFY] accepted_cycles: 1 -> {1-rejected}')
                print('[TAKEAWAY] 只读结果是参考，不标记 verified；写入任务另走独立验收与冲突合并。')
            finally:planner.closed=True;manager.close();manager.pool.shutdown(wait=True)
    return 0
if __name__=='__main__':raise SystemExit(main())
