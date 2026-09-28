"""A pending question must not be treated as an answered question."""
import os,tempfile,threading,time
from pathlib import Path
from unittest.mock import patch
from agentlab.util import lab
from agentplat.experiments import ScriptedModel
from agentplat.loop import CodingAgent
from agentplat.llmconfig import LLMConfig
from agentplat.workspace import Workspace
from agentplat.human_input import list_questions,answer

def main():
    with lab('lab-50-human-wait','聊天内等待回答','提交问题不等于得到用户答复'):
        with tempfile.TemporaryDirectory() as td,patch.dict(os.environ,{'AGENTLAB_HUMAN_DB':str(Path(td)/'q.db')}):
            root=Path(td);stop=threading.Event()
            model=ScriptedModel([[('request_user_input',{'question':'选择颜色','options':['蓝','绿']})],[('finish',{'summary':'已收到选择'})]])
            agent=CodingAgent(model,LLMConfig(),workspace=Workspace(root/'ws'),session_dir=root/'logs',stop_flag=stop,enable_subagents=False)
            results=[];worker=threading.Thread(target=lambda:results.append(agent.run('先确认再继续')));worker.start()
            try:
                deadline=time.monotonic()+5;rows=[]
                while time.monotonic()<deadline:
                    rows=list_questions(agent.session.session_id)
                    if rows:break
                    time.sleep(.02)
                assert rows
                before=int(bool(rows)) # Broken policy: existence of request interpreted as permission to continue.
                time.sleep(.2);after=int(bool(results))
                assert before==1 and after==0 and model.turn==1
                answer(agent.session.session_id,rows[0]['id'],'绿');worker.join(5)
                assert results[0].ok and model.turn==2
                print('[BROKEN-REPRODUCED] 只检查问题已提交，会把尚未回答误当成可以继续')
                print('[FIX-APPLIED] 宿主挂起工具，答复通过会话绑定后恢复；等待期间不调用模型')
                print(f'[VERIFY] premature_completion: {before} -> {after}')
                print('[TAKEAWAY] 申请、回答与授权是不同状态，不能用默认选项代替用户答复。')
            finally:stop.set();worker.join(5)
    return 0

if __name__=='__main__':raise SystemExit(main())
