"""Restart only an empty live agent service; never interrupt user tasks implicitly."""
import json,time,urllib.request,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    auth=json.loads((ROOT/'.agent-runtime/desktop-access.json').read_text(encoding='utf-8'))
    def get(path):
        req=urllib.request.Request('http://127.0.0.1:8800'+path,headers={'Cookie':'agentlab_access='+auth['token']})
        return urllib.request.urlopen(req,timeout=8).read()
    context=json.loads(get('/api/agent-context'))
    if context.get('available'):
        raise RuntimeError('当前服务有已加载会话，未执行自动重启；先确认全部任务状态')
    print('No loaded current agent; restarting idle service. Logs retained.',flush=True)
    get('/admin/drain')
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        try:get('/api/runtime')
        except Exception:break
        time.sleep(.3)
    else:raise RuntimeError('服务没有退出，未启动第二个进程')
    subprocess.run([sys.executable,'-X','utf8',str(ROOT/'tools/start_desktop.py'),'--no-open'],cwd=ROOT,check=True)
    for _ in range(30):
        try:
            version=json.loads(get('/api/runtime'))['version']
            print('Running',version);assert version=='runtime-20';return
        except (OSError,ValueError):time.sleep(.5)
    raise RuntimeError('启动后未确认健康状态')
if __name__=='__main__':main()
