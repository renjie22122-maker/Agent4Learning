"""Paid negative-control test: independent reviewer must reject a flawed median."""
from pathlib import Path
import sys, tempfile, time, json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.llmconfig import LLMConfig
from agentplat.workspace import Workspace
from agentplat.subagents import AgentManager, TERMINAL
from agentplat.independent_review import check
from types import SimpleNamespace

with tempfile.TemporaryDirectory() as td:
    root=Path(td); ws=Workspace(root/'workspace'); ws.execution_mode='local'
    positive = '--positive' in sys.argv
    code = ('from statistics import median as _median\ndef median(values):\n    if not values: raise ValueError("empty")\n    return _median(values)\n' if positive else
            'def median(values):\n    values.sort()\n    return values[len(values)//2]\n')
    (ws.root/'median.py').write_text(code,encoding='utf-8')
    cfg=LLMConfig.load();manager=AgentManager(cfg,ws,root/'review',max_workers=1)
    agent=SimpleNamespace(cfg=cfg,ws=ws,_files_touched=['median.py'],session=SimpleNamespace(append=lambda *a,**k:None),child_manager=lambda:manager,
                          _task_text='median.py 中 median(values)：奇数返回中位数，偶数返回中间两数的均值；空列表抛 ValueError；不能修改输入列表。',_finish_rejects=0)
    try:
        check(agent);identifier=agent._independent_review['agent_id'];deadline=time.monotonic()+240
        result=manager.get(identifier)
        while result['status'] not in TERMINAL and time.monotonic()<deadline:
            result=manager.wait(identifier,timeout_s=5,after_revision=result.get('revision',-1))
        verdict=check(agent) if result['status'] in TERMINAL else None
        child=manager.tasks[identifier].get('agent')
        events=[{'kind':e.kind,'data':e.data} for e in child.session.events if e.kind in ('reflection/rejected','assistant/message')] if child else []
        report={'expected':'pass' if positive else 'reject','allowed':verdict.allow if verdict else None,'review':result,'diagnostics':events[-8:]}
        output=Path(__file__).resolve().parents[1]/'.diagnostics'/('independent-review-live-positive.json' if positive else 'independent-review-live.json')
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'allowed':report['allowed'],'status':result['status'],'summary':result.get('summary'),'evidence':result.get('evidence'),'error':result.get('error')},ensure_ascii=False))
        if verdict is None or verdict.allow != positive or result['status']!='completed' or json.loads(result['summary']).get('verdict')!=('pass' if positive else 'fail'):
            raise AssertionError('did not complete an independently evidenced rejection')
    finally:manager.close()
