from pathlib import Path
import tempfile
from agentlab.util import lab
from agentplat.session import SessionLog
from tools.audit_agent_run import audit

def main():
    with lab('lab-49-completion-audit','完成声明与真实收尾','finish 工具成功不等于验收链已结束'):
        with tempfile.TemporaryDirectory() as td:
            log=SessionLog(Path(td)/'s.jsonl');start=log.append('run/started')
            log.append('tool/result',tool='finish',ok=True)
            log.append('independent_review/started',agent_id='reviewer',digest='new')
            naive=int(any(e.kind=='tool/result' and e.data.get('tool')=='finish' and e.data.get('ok') for e in log.events))
            actual=int(audit(log.path,start.seq)['completed'])
            assert naive==1 and actual==0
            log.append('session/closed',finished=True);log.append('run/settled',status='done')
            assert audit(log.path,start.seq)['completed']
            print('[BROKEN-REPRODUCED] finish 工具返回成功，独立验收却仍在运行')
            print('[FIX-APPLIED] 评测读取 session/closed 与 run/settled，不采信中间声明')
            print(f'[VERIFY] wrong_completion_reports: {naive} -> {actual}')
            print('[TAKEAWAY] 评价整个执行链，而不只看最后一段自然语言。')
    return 0

if __name__=='__main__':raise SystemExit(main())
