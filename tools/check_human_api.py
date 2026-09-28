"""Real-model clarification through the authenticated production reply endpoint."""
import json,os,sys,threading,time,urllib.request,urllib.parse
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.loop import CodingAgent
from agentplat.llm import OpenAIChatClient
from agentplat.llmconfig import LLMConfig
from agentplat.workspace import Workspace
from agentplat.human_input import list_questions
from agentplat.demo import make_handler

def main():
    root=Path(sys.argv[1]).resolve();root.mkdir(parents=True,exist_ok=False)
    os.environ['AGENTLAB_HUMAN_DB']=str(root/'human.db')
    os.environ['AGENTLAB_MEMORY_DIR']=str(root/'memory')
    os.environ['AGENTLAB_KB_DIR']=str(root/'knowledge')
    cfg=LLMConfig.load();stop=threading.Event();ws=Workspace(root/'workspace')
    agent=CodingAgent(OpenAIChatClient(cfg),cfg,workspace=ws,session_dir=root/'sessions',enable_subagents=False,stop_flag=stop,hard_iterations=6)
    result=[]
    demo=SimpleNamespace(permissions_token='isolated-fixture-token',live_sessions={agent.session.session_id:({'status':'running'},agent,stop)})
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(demo))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    worker=threading.Thread(target=lambda:result.append(agent.run('这是交互功能验收。必须先调用 request_user_input 问我喜欢蓝色还是绿色，等待真实回答后调用 finish 用中文概述选择。不要读写文件，不要自行选择，也不要只在文字里提问。')))
    worker.start()
    try:
        deadline=time.monotonic()+90;rows=[]
        while time.monotonic()<deadline and worker.is_alive():
            rows=list_questions(agent.session.session_id)
            if rows:break
            time.sleep(.2)
        assert rows,'real model did not ask'
        before=len(agent.session.of_kind('model/request'));time.sleep(1)
        assert len(agent.session.of_kind('model/request'))==before
        data=urllib.parse.urlencode({'token':demo.permissions_token,'session':agent.session.session_id,'id':rows[0]['id'],'answer':'绿色'}).encode()
        request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/agent/human-input',data=data,headers={'Cookie':'agentlab_access='+demo.permissions_token})
        with urllib.request.urlopen(request,timeout=5) as response:assert response.status==200
        worker.join(90);assert result and result[0].ok,str(result)
        assert '绿' in result[0].summary,result[0].summary
        report={'passed':True,'waiting_model_calls':before,'after_model_calls':len(agent.session.of_kind('model/request')),'summary':result[0].summary,'model':cfg.model_or('mid')}
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
    finally:
        stop.set();worker.join(5);server.shutdown();server.server_close()

if __name__=='__main__':main()
