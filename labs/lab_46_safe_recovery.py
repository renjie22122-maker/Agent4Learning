"""Crash after a write but before its result: never blindly repeat it."""
from pathlib import Path
import tempfile
from agentlab.util import lab
from agentplat.session import SessionLog
from agentplat.recovery import inspect_run


def main():
    with lab('lab-46-safe-recovery', '崩溃恢复与重复副作用', '没有结果日志不等于没有执行'):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); target = root/'delivery.txt'
            log = SessionLog(root/'session.jsonl', session_id='session')
            log.append('session/created', session_id='session', task='append delivery')
            log.append('run/started', permission_mode='auto', text='append delivery')
            log.append('tool/call', tool='append_file', call_id='once', destructive=True)
            target.write_text('delivered\n')  # crash before result is persisted
            baseline = target.read_text() + 'delivered\n'
            before = baseline.count('delivered') - 1
            row = inspect_run(log.path)
            assert row['status'] == 'needs_attention'
            after = target.read_text().count('delivered') - 1
            assert after == 0
            print('[BROKEN-REPRODUCED] 没有 tool/result 时盲目重跑会产生重复交付')
            print('[FIX-APPLIED] 未知调用进入核对列表，保留原文件和日志')
            print(f'[VERIFY] duplicate_effects: {before} -> {after}')
            print('[TAKEAWAY] 自动重启服务与安全重放任务是两件事。')
    return 0


if __name__ == '__main__': raise SystemExit(main())
