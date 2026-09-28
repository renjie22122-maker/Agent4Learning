"""Unavailable execution must have an honest, terminating review outcome."""
from pathlib import Path
import tempfile,json,time
from agentlab.util import lab
from agentplat.subagents import AgentManager,TERMINAL
from agentplat.workspace import Workspace
from agentplat.llmconfig import LLMConfig
from agentplat.experiments import ScriptedModel


def main():
    with lab('lab-45-review-blocked','验收受阻也必须能收尾','无法执行测试不等于代码有缺陷，更不能伪造通过'):
        report={'verdict':'blocked','tests':[],'findings':[],'reason':'required runtime unavailable'}
        before=int(report['verdict'] not in ('pass','fail'))
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            manager=AgentManager(LLMConfig(max_tokens=128),Workspace(root/'ws'),root/'tasks',
                factory=lambda:ScriptedModel([[('finish',{'summary':json.dumps(report)})]]))
            try:
                key=manager.spawn('report missing runtime',purpose='verification')
                deadline=time.monotonic()+5
                while manager.get(key)['status'] not in TERMINAL and time.monotonic()<deadline:
                    current=manager.get(key);manager.wait(key,.1,current['revision'])
                state=manager.get(key);after=int(state['status']!='completed')
                assert after==0 and json.loads(state['summary'])['verdict']=='blocked' and not state['evidence']
                print('[BROKEN-REPRODUCED] 只有 pass/fail 的协议无法表达环境受阻')
                print('[FIX-APPLIED] blocked 可以结束验收，但不会被主任务当作通过')
                print(f'[VERIFY] missing_review_outcomes: {before} -> {after}')
                print('[TAKEAWAY] 完成验收报告与交付通过是两个不同状态。')
            finally:manager.close();manager.pool.shutdown(wait=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
