"""A stale revision must not turn waiting into repeated model requests."""
from pathlib import Path
import tempfile,threading,time
from agentlab.util import lab
from agentplat.subagents import AgentManager
from agentplat.workspace import Workspace
from agentplat.llmconfig import LLMConfig
from agentplat.experiments import ScriptedModel


def main():
    with lab('lab-44-review-wait','等待上限不等于实际耗时','旧版本游标会立即返回；模型不能按 timeout 参数累加时间'):
        with tempfile.TemporaryDirectory() as td:
            release=threading.Event();root=Path(td)
            class Delayed(ScriptedModel):
                def complete_with_tools(self,*args):release.wait(5);return super().complete_with_tools(*args)
            manager=AgentManager(LLMConfig(max_tokens=128),Workspace(root/'ws'),root/'tasks',factory=Delayed)
            try:
                key=manager.spawn('wait fixture')
                start=time.monotonic();manager.wait(key,.15,-1);before=int(time.monotonic()-start<.1)
                result=manager.wait_for_model(key,.15,-1);after=int(result['actual_wait_seconds']<.1)
                assert before==1 and after==0
                print('[BROKEN-REPRODUCED] 旧游标立即返回，最长等待时间不能作为实际等待时长')
                print('[FIX-APPLIED] 模型等待仅由完成、取消或消息唤醒，并返回单调时钟实测耗时')
                print(f'[VERIFY] premature_wait_returns: {before} -> {after}')
                print('[TAKEAWAY] 独立验收期间主模型由宿主挂起，不需要循环发起模型请求。')
            finally:release.set();manager.close();manager.pool.shutdown(wait=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
