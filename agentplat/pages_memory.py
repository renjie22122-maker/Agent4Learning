"""Host-only memory selection and review forms; model tools cannot approve entries."""
import html, time
from . import ui
from .memory import MemoryStore
E=lambda value:html.escape(str(value),quote=True)


def render(demo,qs):
    store=MemoryStore();token=E(demo.permissions_token)
    hidden=f'<input type="hidden" name="token" value="{token}">'
    sessions=[]
    for row in demo.list_agent_sessions(200):
        if not row.get('log_path'):continue
        disabled='disabled' if row.get('status')=='running' else ''
        sessions.append(f'<label style="display:block"><input type="checkbox" name="session_{E(row["session_id"])}" value="1" {disabled}>{E(row["session_id"])} — {E(row.get("task", "")[:120])} ({E(row.get("status", ""))})</label>')
    sources=[]
    for row in store.sources():
        if row['enabled']:
            sources.append(f'<form method="post" action="/memories/revoke">{hidden}<input type="hidden" name="id" value="{E(row["id"])}">{E(row["path"])} <button>撤销来源并清除其记忆</button></form>')
    cards=[]
    for row in store.list():
        if row['status']=='deleted':continue
        options=lambda field,choices:''.join(f'<option value="{v}" {"selected" if row[field]==v else ""}>{label}</option>' for v,label in choices)
        expired=row['expires'] is not None and row['expires']<=time.time()
        expiry=time.strftime('%Y-%m-%dT%H:%M',time.localtime(row['expires'])) if row['expires'] else ''
        cards.append(f'''<div class="card"><form method="post" action="/memories/update">{hidden}
        <input type="hidden" name="id" value="{E(row['id'])}"><input type="hidden" name="revision" value="{row['revision']}">
        <p>来源 {E(row['source'])} · 事件 {row['seq']} · {'已过期' if expired else E(row['status'])}</p>
        <textarea name="content" rows="4" style="width:95%">{E(row['content'])}</textarea>
        <label>类型<select name="kind">{options('kind',[('preference','偏好'),('decision','项目决策'),('experience','验证经验')])}</select></label>
        <label>范围<select name="scope">{options('scope',[('project','来源项目'),('user','当前本机用户所有项目')])}</select></label>
        <label>状态<select name="status">{options('status',[('pending','待确认'),('active','启用并允许召回'),('disabled','停用')])}</select></label>
        <label>过期时间（留空不过期）<input type="datetime-local" name="expires" value="{expiry}"></label><button>保存</button>
        <details><summary>来源证据</summary><pre>{E(row['evidence'])}</pre></details></form>
        <form method="post" action="/memories/delete">{hidden}<input type="hidden" name="id" value="{E(row['id'])}"><button>删除记忆</button></form></div>''')
    body=f'''<h1>长期记忆</h1><p>{E(qs.get('notice',''))}</p>
    <p>只从你选择的会话提取候选。用户原话与成功任务中的验证记录分别保存；模型自评不会自动成为事实。确认启用后，新任务按相关性召回。运行中的会话暂不导入。所选来源的后续对话结束后会增量提取候选。</p>
    <p>候选采用本地规则提取，不调用模型，不上传日志。请将一次性任务整理成可复用的偏好或决策。常见密钥格式会遮盖，但仍请检查正文。</p>
    <form method="post" action="/memories/select">{hidden}{''.join(sessions)}
    <label>初始范围<select name="scope"><option value="project">来源项目</option><option value="user">当前本机用户所有项目</option></select></label><button>提取所选会话的候选记忆</button></form>
    <h2>已选择的来源</h2>{''.join(sources)}<h2>记忆条目</h2>{''.join(cards) or '<p>尚无候选记忆。</p>'}
    <p>停用、删除及过期影响后续召回，已发送给模型的历史上下文不会被远程撤回。</p><a href="/agent">返回 Agent</a>'''
    return ui.page('长期记忆','agent',body)


def mutate(demo,path,form):
    import secrets, datetime
    if not secrets.compare_digest(form.get('token',''),demo.permissions_token):raise PermissionError('无效表单 token')
    store=MemoryStore()
    if path=='/memories/select':
        sessions={r['session_id']:r for r in demo.list_agent_sessions(200)}
        count=0
        for field in form:
            if not field.startswith('session_'):continue
            row=sessions.get(field[len('session_'):])
            if not row or not row.get('log_path') or not row.get('workspace'):raise ValueError('会话不可导入')
            if row.get('status')=='running':raise ValueError('请等待会话结束再提取')
            count+=store.select_source(row['log_path'],row['workspace'],form.get('scope','project'))
        return f'提取了 {count} 条候选，请检查后启用。'
    if path=='/memories/delete':store.delete(form['id'])
    elif path=='/memories/revoke':store.revoke(form['id'])
    elif path=='/memories/update':
        expiry=datetime.datetime.fromisoformat(form['expires']).timestamp() if form.get('expires') else None
        store.update(form['id'],form['content'],form['kind'],form['scope'],form['status'],expiry,int(form['revision']))
    else:raise ValueError('未知记忆操作')
    return '记忆设置已保存。'
