"""Real API reviewer validates a UI with a real browser, without inventing a runtime."""
from pathlib import Path
import sys,json,tempfile,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dataclasses import replace
from types import SimpleNamespace
from agentplat.llmconfig import LLMConfig
from agentplat.workspace import Workspace
from agentplat.subagents import AgentManager,TERMINAL
from agentplat.independent_review import check

with tempfile.TemporaryDirectory(prefix='review-ui-') as td:
    root=Path(td);ws=Workspace(root/'ws')
    (ws.root/'index.html').write_text('<meta charset="utf-8"><button id="add" onclick="document.getElementById(\'score\').textContent=Number(document.getElementById(\'score\').textContent)+1">加一</button><p id="score">0</p>',encoding='utf-8')
    cfg=replace(LLMConfig.load(),reasoning_effort='low',max_tokens=2048)
    manager=AgentManager(cfg,ws,root/'team')
    agent=SimpleNamespace(cfg=cfg,ws=ws,_files_touched=['index.html'],_task_text='index.html 初始 #score 显示 0；点击 #add 一次后显示 1，再点一次显示 2。使用真实浏览器交互验证，不需要测试其他功能。',
        session=SimpleNamespace(append=lambda *a,**k:None),child_manager=lambda:manager,_finish_rejects=0)
    try:
        check(agent);key=agent._independent_review['agent_id'];deadline=time.monotonic()+180
        while manager.get(key)['status'] not in TERMINAL and time.monotonic()<deadline:
            state=manager.get(key);manager.wait(key,1,state['revision'])
        result=manager.get(key);verdict=check(agent)
        child=manager.tasks[key]['agent']
        calls=[e.data.get('tool') for e in child.session.of_kind('tool/call')]
        checks={'completed':result['status']=='completed','passed':verdict.allow,
                'browser_assertion':'browser_check' in calls,'browser_interaction':'browser_click' in calls,
                'no_interpreter_development':'write_file' not in calls}
        report={'checks':checks,'result':result,'tools':calls,'evidence_valid':child.evidence.valid(child.ws.scope),'finish_messages':[e.data for e in child.session.of_kind('conversation/message') if 'finish' in str(e.data)]}
        (Path(__file__).resolve().parents[1]/'.diagnostics/review-ui-live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(checks));assert all(checks.values()),result.get('error')
    finally:manager.close();manager.pool.shutdown(wait=True)
