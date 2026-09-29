"""Explicit user continuation; never replay an uncertain tool operation."""
from .session import SessionLog, replay
from pathlib import Path


def resume(demo, sid):
    if not sid:
        raise ValueError('请选择要继续的会话')
    with demo._lock:
        entry = demo.live_sessions.get(sid)
        if entry and entry[0].get('status') == 'running':
            return sid  # A double click must not enqueue a second instruction.
        row = next((r for r in demo.list_agent_sessions(200)
                    if r['session_id'] == sid), None)
        path = entry[1].session.path if entry and hasattr(entry[1], 'session') else (row or {}).get('log_path')
        if not path:
            raise ValueError('找不到会话日志，无法安全恢复')
        log, skipped = SessionLog.load(Path(path))
        state = replay(log, skipped)
        if skipped or state.unknown_calls:
            raise ValueError('日志损坏或工具结果未知：请先核对会话记录和实际文件；未启动任务，也未重放操作。')
        if not state.messages:
            raise ValueError('没有可恢复的对话上下文')
        starts = log.of_kind('run/started')
        budget = starts[-1].data if starts else {}
        if not entry:
            demo.restore_agent_session(sid)
            entry = demo.live_sessions[sid]
        agent = entry[1]
        return demo.continue_agent_task(
            '继续当前未完成任务。先核对已有成果和最新要求，仅完成剩余工作；'
            '不得盲目重复已经完成或结果未知的写入、命令及外部操作。若任务已完成，请说明结果。',
            max_iters=budget.get('max_iters', agent.hard_iterations) or 0,
            max_usd=budget.get('max_usd', agent.guard.max_usd), session_id=sid)
