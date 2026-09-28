"""Regression check: cancellation must reach an in-flight LLM client.

No paid calls. Exits 1 if cancellation is not delivered or the worker stays alive.
"""
from pathlib import Path
import sys, tempfile, threading, time, json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.subagents import AgentManager,TERMINAL
from agentplat.workspace import Workspace
from agentplat.llmconfig import LLMConfig
from agentplat.experiments import ScriptedModel


def main():
    entered=threading.Event();release=threading.Event();observed=threading.Event()
    class Blocking(ScriptedModel):
        def complete_with_tools(self,*args):
            entered.set()
            while not release.wait(.01):
                flag=getattr(self,'cancel_event',None)
                if flag is not None and flag.is_set():
                    observed.set();raise InterruptedError('cancelled during model call')
            return super().complete_with_tools(*args)
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);model=Blocking();ws=Workspace(root/'workspace')
        manager=AgentManager(LLMConfig(max_tokens=128),ws,root/'children',factory=lambda:model)
        try:
            key=manager.spawn('Inspect fixture');assert entered.wait(3)
            manager.cancel(key);reached=observed.wait(.5);release.set()
            state=manager.get(key);deadline=time.monotonic()+5
            while state['status'] not in TERMINAL and time.monotonic()<deadline:
                state=manager.wait(key,1,state['revision'])
            report={'cancel_reached_inflight_client':reached,'final_status':state['status']}
            output=Path(__file__).resolve().parents[1]/'.diagnostics'/'subagent-cancellation-report.json'
            output.write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(report));return 0 if reached and state['status']=='cancelled' else 1
        finally:release.set();manager.close()


if __name__=='__main__':raise SystemExit(main())
