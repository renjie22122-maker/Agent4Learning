"""Host-owned exact-command approvals: task-bound, expiring and single-use."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

DATABASE = Path(__file__).resolve().parents[1] / '.agent-runtime' / 'approvals.sqlite3'


@contextmanager
def connection():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, session TEXT, workspace TEXT, command TEXT, reason TEXT, status TEXT, expires REAL)')
    if 'timeout_s' not in {row[1] for row in db.execute('PRAGMA table_info(approvals)')}:
        try: db.execute('ALTER TABLE approvals ADD COLUMN timeout_s REAL NOT NULL DEFAULT 60')
        except sqlite3.OperationalError:
            if 'timeout_s' not in {row[1] for row in db.execute('PRAGMA table_info(approvals)')}: raise
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback(); raise
    finally: db.close()


def request(session, workspace, command, reason, timeout_s=60):
    if not isinstance(timeout_s,(int,float)) or isinstance(timeout_s,bool) or not 1<=timeout_s<=3600:
        raise ValueError('宿主命令 timeout_s 必须为 1–3600 秒，并随授权确认')
    if not command.strip() or len(command) > 4000 or len(reason) > 4000:
        raise ValueError('命令与理由长度超限或命令为空')
    identifier = uuid.uuid4().hex
    with connection() as db:
        db.execute('INSERT INTO approvals (id,session,workspace,command,reason,status,expires,timeout_s) VALUES (?,?,?,?,?,?,?,?)',
                   (identifier, session, str(Path(workspace).resolve()), command, reason, 'pending', time.time()+1800,timeout_s))
    return {'request_id':identifier, 'status':'pending', 'approval_url':'/approvals','timeout_s':timeout_s}


def list_requests():
    with connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM approvals ORDER BY expires DESC LIMIT 100')]


def denied(session, workspace, command):
    with connection() as db:
        return db.execute('SELECT 1 FROM approvals WHERE session=? AND workspace=? AND command=? AND status=? LIMIT 1',
                          (session, str(Path(workspace).resolve()), command, 'denied')).fetchone() is not None


def check_authority(agent, command=''):
    from .runtime import effective_policy, PermissionDenied
    if getattr(agent.ws, 'general_chat', False) or not agent.ws.allow_shell:
        raise PermissionDenied('当前会话没有项目命令权限；请先在界面配置项目与执行权限')
    effective_policy(agent).check('run_approved_command', writes=True, shell=True)
    from .tool_guards import ToolRequest
    guards = getattr(agent, 'tool_guards', None)
    if guards is not None:
        guards.check(ToolRequest('run_approved_command', writes=True, shell=True), {'command': command})


def decide(identifier, allow):
    with connection() as db:
        count = db.execute('UPDATE approvals SET status=? WHERE id=? AND status=? AND expires>?',
                           ('approved' if allow else 'denied', identifier, 'pending', time.time())).rowcount
        if count != 1: raise ValueError('审批不存在、过期或已处理')


def claim(identifier, session, workspace):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM approvals WHERE id=? AND session=? AND workspace=?',
                         (identifier, session, str(Path(workspace).resolve()))).fetchone()
        if not row or row['status'] != 'approved' or row['expires'] < time.time():
            raise PermissionError('该命令尚未获批、已经使用、已过期或不属于当前任务')
        db.execute('UPDATE approvals SET status=? WHERE id=?', ('consumed', identifier))
        return dict(row)


def execute(agent, request_id):
    from .processes import ProcessSupervisor
    check_authority(agent)
    if agent.stop_flag is not None and agent.stop_flag.is_set():
        raise PermissionError('任务已取消，未执行已批准命令')
    row = claim(request_id, agent.session.session_id, agent.ws.root)
    check_authority(agent, row['command'])
    agent.session.append('approval/consumed', request_id=request_id, command=row['command'])
    agent.session.flush('before_approved_host_command')
    supervisor = ProcessSupervisor()
    result = None
    try:
        from .execution_environment import task_environment
        key = supervisor.start(row['command'], agent.ws.root, shell=True, timeout_s=row['timeout_s'], env=task_environment(), interactive=False)
        while True:
            result = supervisor.wait(key, .2)
            if result['status'] != 'running': break
            if agent.stop_flag is not None and agent.stop_flag.is_set():
                result = supervisor.cancel(key)
                break
    finally:
        try: supervisor.close()
        except Exception as exc:
            if result is None: raise
            # A cleanup error must not erase the command's already observed
            # output/outcome and invite an unsafe replay of side effects.
            result['cleanup_error'] = type(exc).__name__+': '+str(exc)
    result.update(execution_backend='host', timeout_s=row['timeout_s'],
                  success=result['status']=='exited' and result['exit_code']==0 and not result.get('cleanup_error'),
                  next_execution_backend=getattr(agent.ws,'execution_mode','unknown'),
                  note='本次为一次性宿主执行；后续普通命令仍使用原后端。timeout/cancelled/cleanup_error 不是成功，不能仅凭 exit_code=0 判断；副作用需核实，不能盲目重跑。')
    from .permission_recovery import host_failure_guidance
    result['diagnostic'] = host_failure_guidance(result)
    agent.session.append('approval/result', request_id=request_id, status=result['status'], exit_code=result['exit_code'],
                         success=result['success'], execution_backend='host', cleanup_error=result.get('cleanup_error'))
    return result
