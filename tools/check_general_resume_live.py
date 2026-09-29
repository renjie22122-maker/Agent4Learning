"""Real LLM + HTTP continuation test in fresh private storage. Opt-in only."""
import sys, os, json, time, threading, http.client, urllib.parse
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    if len(sys.argv)!=3 or sys.argv[1]!='--real':
        raise SystemExit('Usage: check_general_resume_live.py --real NEW_OUTPUT_DIRECTORY')
    root=Path(sys.argv[2]).resolve();root.mkdir(parents=True,exist_ok=False)
    os.environ['AGENTLAB_MEMORY_DIR']=str(root/'memory')
    os.environ['AGENTLAB_KB_DIR']=str(root/'knowledge')
    os.environ['AGENTLAB_HUMAN_DB']=str(root/'human.sqlite3')
    from agentplat.demo import DemoServer,make_handler
    from agentplat.config import PlatformConfig
    from agentplat.llmconfig import LLMConfig
    from agentplat.workspaces import WorkspaceManager
    from agentplat.session import SessionLog
    cfg=LLMConfig.load();cfg.memory_enabled=False;cfg.timeout_s=max(cfg.timeout_s,90)
    def demo():
        d=DemoServer.__new__(DemoServer);d._lock=threading.RLock()
        d.agent_state={};d.live_sessions={};d._agent=None;d._stop_flag=threading.Event()
        d.cfg=PlatformConfig();d.llm_cfg=cfg;d.permissions_token='local-test-only'
        d.ws_mgr=WorkspaceManager(state_path=root/'workspaces.json')
        return d
    def wait(d,sid):
        deadline=time.monotonic()+300
        while d.live_sessions[sid][0]['status']=='running':
            if time.monotonic()>deadline:
                d.live_sessions[sid][2].set();raise TimeoutError('Explicit integration-test timeout: 300 seconds')
            time.sleep(.2)
        return d.live_sessions[sid][0]
    report={'kind':'integration','real_llm':True,'checks':{},'cases':[]}
    d=demo();original=d._build_agent
    def build(*args,**kwargs):
        agent=original(*args,**kwargs);on_step=agent.on_step
        def observe(step):
            on_step(step)
            if (agent.ws.root/'seed.txt').exists():d._stop_flag.set()
        agent.on_step=observe
        return agent
    d._build_agent=build
    sid=d.start_agent_task('分两阶段完成任务：第一阶段只调用 write_file 创建 seed.txt，内容精确为 SEED-7391；'
        '下一轮读取核对 seed.txt，再创建 done.txt 内容精确为 VERIFIED-7391。不要重写已存在且正确的 seed.txt。'
        '最后核对两个文件内容并完成。每轮只执行一个工具。',workspace_group='__general__',max_iters=30)
    state=wait(d,sid);files=Path(state['workspace']);seed=files/'seed.txt'
    assert seed.read_text(encoding='utf-8')=='SEED-7391',state.get('error')
    before=seed.stat().st_mtime_ns
    report['checks']['real_model_interrupted_after_write']=state['status']!='done'
    print('Real model interrupted after seed write',flush=True)
    # New host objects reconstruct the conversation solely from persisted logs.
    restored=demo()
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(restored))
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    def post():
        conn=http.client.HTTPConnection(*server.server_address,timeout=15)
        try:
            conn.request('POST','/agent/continue',urllib.parse.urlencode({'session':sid,'token':restored.permissions_token}),
                {'Cookie':'agentlab_access='+restored.permissions_token,'Content-Type':'application/x-www-form-urlencoded'})
            response=conn.getresponse();return response.status,json.loads(response.read())
        finally:conn.close()
    try:
        status,body=post();assert status==200,body
        state=wait(restored,sid)
        report['checks']['http_restore_completed']=state['status']=='done'
        report['checks']['original_write_not_replayed']=seed.stat().st_mtime_ns==before
        report['checks']['final_artifact_correct']=(files/'done.txt').read_text(encoding='utf-8')=='VERIFIED-7391'
        report['checks']['general_mode_survives_restore']=restored._agent.ws.general_chat
        report['cases'].append({'case':'resume','status':state['status'],'usd':state.get('total_usd'),'model_calls':state.get('model_calls')})
        print('Real HTTP resume completed:',report['checks'],flush=True)
        # Test actual model-selected cross-chat read, followed by its own output.
        second=restored.start_agent_task('请调用 read_file 尝试读取这个文件：'+str(seed)+
            '。这是隔离验证：被拒绝是正确结果，不要尝试绕过权限。随后在当前会话创建 own.txt 内容精确为 PRIVATE-B，'
            '读回核对后结束。不要运行 shell，不要委派。',workspace_group='__general__',max_iters=30)
        other=wait(restored,second);other_root=Path(other['workspace']);agent=restored.live_sessions[second][1]
        denied=[e for e in agent.session.events if e.kind=='tool/result' and e.data.get('tool')=='read_file' and not e.data.get('ok')]
        report['checks']['cross_chat_read_denied_with_real_model']=bool(denied)
        report['checks']['separate_artifact_roots']=files!=other_root and not (other_root/'seed.txt').exists()
        report['checks']['second_artifact_correct']=(other_root/'own.txt').read_text(encoding='utf-8')=='PRIVATE-B'
        report['cases'].append({'case':'isolation','status':other['status'],'usd':other.get('total_usd'),'model_calls':other.get('model_calls')})
        # Unknown side effects must be refused before any new paid request.
        target=restored.live_sessions[sid][1];n=len(target.session.of_kind('model/request'))
        target.session.append('tool/call',tool='write_file',call_id='unknown-test',destructive=True)
        target.session.flush('fault_injection')
        code,error=post()
        report['checks']['unknown_result_http_blocked']=code==409 and len(target.session.of_kind('model/request'))==n
    finally:
        server.shutdown();server.server_close();worker.join(5)
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    assert all(report['checks'].values()),'One or more checks failed'

if __name__=='__main__':main()
