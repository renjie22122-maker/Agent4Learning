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
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback(); raise
    finally: db.close()


def request(session, workspace, command, reason):
    if not command.strip() or len(command) > 4000 or len(reason) > 4000:
        raise ValueError('命令与理由长度超限或命令为空')
    identifier = uuid.uuid4().hex
    with connection() as db:
        db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?)',
                   (identifier, session, str(Path(workspace).resolve()), command, reason, 'pending', time.time()+1800))
    return {'request_id':identifier, 'status':'pending', 'approval_url':'/approvals'}


def list_requests():
    with connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM approvals ORDER BY expires DESC LIMIT 100')]


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
    row = claim(request_id, agent.session.session_id, agent.ws.root)
    agent.session.append('approval/consumed', request_id=request_id, command=row['command'])
    agent.session.flush('before_approved_host_command')
    supervisor = ProcessSupervisor()
    try:
        from .execution_environment import task_environment
        key = supervisor.start(row['command'], agent.ws.root, shell=True, timeout_s=60, env=task_environment())
        result = supervisor.wait(key, 60)
        if result['status'] == 'running': result = supervisor.wait(key, 5)
        agent.session.append('approval/result', request_id=request_id, status=result['status'], exit_code=result['exit_code'])
        return result
    finally: supervisor.close()
