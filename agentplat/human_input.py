"""Durable human replies. Waiting is host work, never a model polling loop."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


@contextmanager
def connection():
    path = Path(os.environ.get('AGENTLAB_HUMAN_DB', Path(__file__).resolve().parents[1] / '.agent-runtime/human-input.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS questions (id TEXT PRIMARY KEY, session TEXT, owner TEXT, kind TEXT, payload TEXT, status TEXT, answer TEXT, created REAL)')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def list_questions(session):
    with connection() as db:
        return [{**dict(r), 'payload': json.loads(r['payload'])} for r in db.execute(
            'SELECT * FROM questions WHERE session=? ORDER BY created', (session,))]


def create(session, owner, kind, payload):
    key = uuid.uuid4().hex
    with connection() as db:
        db.execute('INSERT INTO questions VALUES (?,?,?,?,?,?,?,?)',
                   (key, session, owner, kind, json.dumps(payload, ensure_ascii=False), 'pending', '', time.time()))
    return key


def answer(session, key, text):
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise ValueError('请填写答复（最多 16000 字符）')
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM questions WHERE id=? AND session=?', (key, session)).fetchone()
        if not row or row['status'] != 'pending':
            raise ValueError('问题不存在、不属于此会话或已处理')
        if row['kind'] == 'approval':
            if text not in ('allow', 'deny'):
                raise ValueError('审批必须明确选择允许一次或拒绝')
            from .approvals import decide
            decide(json.loads(row['payload'])['request_id'], text == 'allow')
        db.execute('UPDATE questions SET status=?, answer=? WHERE id=?',
                   ('denied' if row['kind'] == 'approval' and text == 'deny' else 'answered', text, key))
        return dict(row)


def wait(agent, kind, payload):
    owner = agent.session.session_id
    session = getattr(agent.ws, 'human_session', '') or owner
    key = create(session, owner, kind, payload)
    agent.session.append('human/requested', question_id=key, request_type=kind, payload=payload)
    agent.session.flush('waiting_user')
    try:
        while True:
            with connection() as db:
                row = dict(db.execute('SELECT * FROM questions WHERE id=?', (key,)).fetchone())
            if row['status'] != 'pending':
                result = {'question_id':key, 'status':row['status'], 'answer':row['answer']}
                if kind == 'approval':
                    result['request_id'] = payload['request_id']
                    if row['status'] == 'answered': result['status'] = 'approved'
                agent.session.append('human/answered', **result)
                return result
            if kind == 'approval':
                from .approvals import list_requests
                approval = next((r for r in list_requests() if r['id'] == payload['request_id']), None)
                if not approval or approval['expires'] < time.time() or approval['status'] != 'pending':
                    status = approval['status'] if approval and approval['expires'] >= time.time() else 'expired'
                    with connection() as db:
                        db.execute("UPDATE questions SET status=? WHERE id=? AND status='pending'", (status, key))
                    continue
            if agent.stop_flag is not None and agent.stop_flag.wait(.2):
                with connection() as db:
                    db.execute("UPDATE questions SET status='cancelled' WHERE id=? AND status='pending'", (key,))
                if kind == 'approval':
                    from .approvals import connection as approval_connection
                    with approval_connection() as db:
                        db.execute("UPDATE approvals SET status='cancelled' WHERE id=? AND status='pending'", (payload['request_id'],))
                return {'question_id':key, 'status':'cancelled'}
            if agent.stop_flag is None: time.sleep(.2)
    finally:
        agent.session.flush('human_wait_exit')


def ask(agent, question, options=None):
    if not isinstance(question, str) or not question.strip() or len(question) > 4000:
        raise ValueError('问题不能为空，最多 4000 字符')
    options = options or []
    if not isinstance(options, list) or len(options) > 6 or any(not isinstance(x,str) or len(x)>500 for x in options):
        raise ValueError('最多 6 个简短选项')
    return wait(agent, 'question', {'question':question, 'options':options})


def request_command(agent, command, reason):
    from . import approvals
    existing = [r for r in approvals.list_requests() if r['session'] == agent.session.session_id and r['command'] == command]
    if any(r['status'] == 'denied' for r in existing):
        raise PermissionError('用户已拒绝同一命令，不重复追问；请调整方案或等待用户主动变更授权')
    request = approvals.request(agent.session.session_id, agent.ws.root, command, reason)
    return wait(agent, 'approval', {**request, 'command':command, 'reason':reason, 'workspace':str(agent.ws.root)})


def install(agent):
    from .agent_tools import AgentTool, _obj
    agent.tools['request_user_input'] = AgentTool('request_user_input',
        '缺少影响方案的信息时向用户提问并等待回答，宿主自动续跑，无需轮询。每次一个关键问题，可用于逐轮澄清需求；答案不授予操作权限。',
        _obj({'question':{'type':'string'},'options':{'type':'array','items':{'type':'string'},'maxItems':6}}, ['question']),
        lambda **args: json.dumps(ask(agent, **args), ensure_ascii=False))
