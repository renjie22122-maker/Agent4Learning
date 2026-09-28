"""Conservative crash recovery: uncertain operations are never replayed."""
from pathlib import Path
from .session import SessionLog, replay
from .runtime import workspace_digest


def inspect_run(path):
    log, skipped = SessionLog.load(Path(path))
    starts = log.of_kind('run/started')
    if not starts:
        return None
    start = starts[-1]
    events = [e for e in log.events if e.seq > start.seq]
    if any(e.kind in ('run/settled', 'run/cancel_requested', 'ui/turn', 'session/closed', 'turn/stopping') for e in events):
        return None
    row = dict(start.data, session_id=Path(path).stem, path=str(path), status='needs_attention')
    def blocked(reason):
        return dict(row, reason=reason)
    if skipped:
        return blocked('日志存在损坏行，需要核对现场')
    state = replay(log)
    if state.unknown_calls:
        return blocked('工具执行结果未知，禁止自动重放')
    if start.data.get('recovered'):
        return blocked('自动恢复后再次中断，已停止重复恢复')
    if start.data.get('permission_mode') != 'readonly':
        return blocked('任务允许副作用，需核对文件、命令和子任务后继续')
    from .access_modes import READ_TOOLS
    if any((e.kind == 'permission/applied' and e.data.get('mode') != 'readonly') or
           (e.kind == 'tool/call' and (e.data.get('destructive') or e.data.get('tool') not in READ_TOOLS))
           for e in events):
        return blocked('运行期间出现权限升级或非只读操作')
    if start.data.get('max_iters') or start.data.get('max_usd'):
        return blocked('设置了任务预算，需确认剩余额度后继续')
    checkpoints = [e for e in events if e.kind == 'recovery/checkpoint']
    if not checkpoints or not state.messages:
        return blocked('未到达完整的模型请求检查点')
    checkpoint = checkpoints[-1]
    if any(e.kind not in ('model/request', 'model/retry', 'checkpoint/barrier')
           for e in events if e.seq > checkpoint.seq):
        return blocked('检查点后仍有进展，需核对最新状态')
    roots = start.data.get('workspace_roots', {})
    if not roots or any(not Path(p).is_dir() for p in roots.values()):
        return blocked('工作区文件夹不可用')
    if workspace_digest({k:Path(v) for k,v in roots.items()}) != checkpoint.data.get('digest'):
        return blocked('检查点后工作区已变化')
    return dict(row, status='safe', reason='只读任务在模型请求边界中断，可以恢复完整上下文')


def recover_server(demo):
    """Called once after startup. Only newly journaled read-only runs qualify."""
    demo.recovery_reports = []
    from .conversations import store_for
    metadata = store_for(demo).load()
    for directory in demo.ws_mgr.session_directories():
        for path in Path(directory).glob('*.jsonl'):
            try:
                row = inspect_run(path)
                if not row:
                    continue
                if row['session_id'] in demo.live_sessions:
                    continue
                demo.recovery_reports.append(row)
                if metadata.get(row['session_id'], {}).get('deleted') or metadata.get(row['session_id'], {}).get('archived'):
                    row.update(status='needs_attention', reason='会话已归档或删除，不自动恢复')
                    continue
                if row['status'] != 'safe':
                    continue
                with demo._lock:
                    demo.restore_agent_session(row['session_id'])
                    state, agent, stop = demo.live_sessions[row['session_id']]
                    from .access_modes import apply
                    apply(agent, 'readonly')
                    agent._crash_recovery = True
                    demo.continue_agent_task('服务异常重启后继续尚未完成的只读任务：' + row['text'],
                                             session_id=row['session_id'])
                    row['status'] = 'resumed'
            except Exception as exc:
                demo.recovery_reports.append({'session_id':path.stem, 'status':'needs_attention',
                                              'reason':f'恢复失败：{type(exc).__name__}: {exc}'})


def page(demo):
    import html
    import urllib.parse
    rows = []
    for item in getattr(demo, 'recovery_reports', []):
        sid = item['session_id']
        label = {'safe':'可安全恢复','resumed':'已自动恢复','needs_attention':'需要核对'}.get(item['status'], item['status'])
        rows.append('<li><a href="/agent?session=' + urllib.parse.quote(sid) + '">' +
                    html.escape(sid) + '</a>：' + html.escape(label) + ' — ' +
                    html.escape(item.get('reason','')) + '</li>')
    import json
    try:
        supervisor = json.loads((Path(__file__).resolve().parents[1] / '.agent-runtime' / 'supervisor-status.json').read_text())
        raw_status = supervisor.get('state', 'unknown')
        status = html.escape({'running':'运行中','stopped':'已停止','backoff':'等待重启',
                              'blocked':'已停止自动重试'}.get(raw_status,str(raw_status)))
    except (OSError, ValueError):
        status = '未启用桌面守护启动器'
    return ('<!doctype html><meta charset="utf-8"><title>运行恢复</title>'
            '<main style="max-width:900px;margin:40px auto;font:16px/1.7 sans-serif">'
            '<a href="/agent">返回对话</a><h1>运行恢复</h1><p>守护进程最近状态：' + status + '</p>'
            '<p>只读任务在完整请求检查点中断时自动恢复一次。写入、命令、子任务或未知结果不会自动重放。'
            '需要核对的任务可进入原会话，检查日志与文件后发送继续指令。</p><ul>' +
            ''.join(rows) + '</ul>' + ('<p>本次启动没有需要恢复的任务。</p>' if not rows else '') + '</main>')
