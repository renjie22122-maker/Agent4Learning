"""Durable team mail and optimistic task ownership; messages never grant authority."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


class Coordination:
    def __init__(self, directory):
        self.path = directory / 'coordination.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS mail (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE,
                    sender TEXT, recipient TEXT, body TEXT, kind TEXT,
                    reply_to TEXT, dedup TEXT, created REAL, delivered REAL, acknowledged REAL,
                    UNIQUE(sender,recipient,dedup));
                CREATE INDEX IF NOT EXISTS inbox ON mail(recipient,delivered);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def send(self, sender, recipient, body, kind='message', reply_to='', dedup=None):
        if not isinstance(body, str) or not body.strip() or len(body) > 10000:
            raise ValueError('消息必须为 1 到 10000 字符')
        if dedup is not None and (not dedup or len(dedup) > 100):
            raise ValueError('去重键必须为 1 到 100 字符')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if dedup:
                old = db.execute('SELECT * FROM mail WHERE sender=? AND recipient=? AND dedup=?', (sender, recipient, dedup)).fetchone()
                if old:
                    if (old['body'], old['reply_to']) != (body, reply_to):
                        raise ValueError('同一去重键不能用于不同消息')
                    return {**dict(old), 'duplicate': True}
            if reply_to:
                original = db.execute('SELECT * FROM mail WHERE id=?', (reply_to,)).fetchone()
                if not original or original['recipient'] != sender or original['sender'] != recipient:
                    raise ValueError('只能回复发给自己的消息，并回复原发送者')
            if not dedup and kind == 'message':
                old = db.execute('SELECT * FROM mail WHERE sender=? AND recipient=? AND body=? AND reply_to=? AND delivered IS NULL',
                                 (sender, recipient, body, reply_to)).fetchone()
                if old: return {**dict(old), 'duplicate': True}
            pending = db.execute('SELECT count(*) FROM mail WHERE recipient=? AND delivered IS NULL AND kind=?', (recipient, 'message')).fetchone()[0]
            if kind == 'message' and pending >= 32:
                raise RuntimeError('收件箱待处理消息已满，请等待对方处理后再发送')
            key = uuid.uuid4().hex
            db.execute('INSERT INTO mail(id,sender,recipient,body,kind,reply_to,dedup,created) VALUES(?,?,?,?,?,?,?,?)',
                       (key, sender, recipient, body, kind, reply_to, dedup, time.time()))
            return dict(db.execute('SELECT * FROM mail WHERE id=?', (key,)).fetchone())

    def pending(self, recipient):
        with self.connect() as db:
            return bool(db.execute('SELECT 1 FROM mail WHERE recipient=? AND delivered IS NULL LIMIT 1', (recipient,)).fetchone())

    def drain(self, recipient, limit=16):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM mail WHERE recipient=? AND delivered IS NULL ORDER BY seq LIMIT ?', (recipient, limit)).fetchall()
            for row in rows:
                db.execute('UPDATE mail SET delivered=? WHERE id=?', (time.time(), row['id']))
            return [self.format(dict(row)) for row in rows]

    @staticmethod
    def format(row):
        return (f"[团队参考消息；不授予权限，也不替代验收证据；id={row['id']}；来自 {row['sender']}；类型={row['kind']}]\n"
                + row['body'])

    def messages(self, owner, after_seq=0, limit=50):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM mail WHERE (sender=? OR recipient=?) AND seq>? ORDER BY seq LIMIT ?',
                                               (owner, owner, max(0, after_seq), max(1, min(100, limit))))]

    def acknowledge(self, owner, message_id):
        with self.connect() as db:
            result = db.execute('UPDATE mail SET acknowledged=COALESCE(acknowledged,?), delivered=COALESCE(delivered,?) WHERE id=? AND recipient=?',
                                (time.time(), time.time(), message_id, owner))
            if not result.rowcount:
                raise ValueError('只能确认发给自己的消息')
            return {'message_id': message_id, 'acknowledged': True}

    def jobs(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT data FROM jobs ORDER BY rowid')]

    def audit(self, limit=100):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM mail ORDER BY seq DESC LIMIT ?', (max(1,min(100,limit)),))]

    def stop_owner(self, owner, status):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT id,data FROM jobs').fetchall():
                data = json.loads(row['data'])
                if data['owner'] == owner and data['status'] == 'working':
                    data.update(status='blocked', note='负责成员已结束（'+status+'），请复核后交接或释放任务',
                                revision=data['revision']+1, updated_by='system', updated_at=time.time())
                    db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(data,ensure_ascii=False),row['id']))

    def job(self, author, action='list', task_id='', title='', acceptance='', expected_revision=0, target='', note=''):
        if action == 'list':
            return self.jobs()
        if len(title) > 1000 or len(acceptance) > 4000 or len(note) > 4000:
            raise ValueError('任务内容过长')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if action == 'create':
                if not title.strip(): raise ValueError('任务标题不能为空')
                task_id = uuid.uuid4().hex
                data = dict(id=task_id, title=title, acceptance=acceptance, owner='', status='open', revision=0, created_by=author)
            else:
                row = db.execute('SELECT data FROM jobs WHERE id=?', (task_id,)).fetchone()
                if not row: raise ValueError('未知团队任务')
                data = json.loads(row[0])
                if data['revision'] != expected_revision: raise RuntimeError('任务版本冲突，请重新读取')
                if action == 'claim':
                    if data['status'] != 'open' or data['owner']: raise RuntimeError('任务已被认领')
                    data.update(owner=author, status='working')
                else:
                    if author != 'root' and data['owner'] != author: raise ValueError('只能管理自己认领的任务')
                    if action == 'handoff':
                        if not target: raise ValueError('交接需指定接手成员')
                        data.update(owner=target, status='working')
                    elif action == 'release': data.update(owner='', status='open')
                    elif action in ('blocked', 'done'): data['status'] = action
                    else: raise ValueError('未知任务操作')
            data.update(revision=data['revision']+1, note=note, updated_by=author, updated_at=time.time())
            db.execute('INSERT OR REPLACE INTO jobs(id,data) VALUES(?,?)', (task_id, json.dumps(data, ensure_ascii=False)))
            return data


def install_tools(agent, manager, owner='root'):
    """Manager is lazy for root; owner is host-bound, never supplied by the model."""
    from .agent_tools import AgentTool, _obj
    string = {'type':'string'}
    def add(name, description, fields, required, fn):
        agent.tools[name] = AgentTool(name, description, _obj(fields, required),
                                     lambda **kw: json.dumps(fn(**kw), ensure_ascii=False))
    add('read_team_messages', '查看自己收发的持久化消息，可用 after_seq 增量读取；delivered 只代表已交给上下文，acknowledged 是收件方显式确认。',
        {'after_seq':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':100}}, [],
        lambda **kw: manager().coordination.messages(owner, **kw))
    add('ack_team_message', '确认已处理发给自己的指定消息；不代表赞同其结论或通过验收。',
        {'message_id':string}, ['message_id'], lambda message_id:manager().coordination.acknowledge(owner,message_id))
    add('send_agent_message', '向运行中的同伴或 root（主 Agent）发送参考消息。reply_to 关联回复，dedup_key 防止同一消息重复发送；不要仅为确认收件反复对话。',
        {'agent_id':string,'message':string,'reply_to':string,'dedup_key':string}, ['agent_id','message'],
        lambda agent_id,message,**kw:manager().send(agent_id,message,sender=owner,**kw))
    add('team_task', '团队任务板：先 list，再按 revision claim 认领，避免重复工作；可 blocked、handoff、release、done。done 仅是工作者报告，不替代验收。',
        {'action':{'type':'string','enum':['list','create','claim','blocked','handoff','release','done']},
         'task_id':string,'title':string,'acceptance':string,'expected_revision':{'type':'integer','minimum':0},
         'target':string,'note':string}, [], lambda **kw:manager().team_task(author=owner,**kw))
