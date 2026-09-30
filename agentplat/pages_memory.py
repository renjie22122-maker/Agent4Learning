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
        valid_from=time.strftime('%Y-%m-%dT%H:%M',time.localtime(row['valid_from'])) if row.get('valid_from') else ''
        cards.append(f'''<div class="card"><form method="post" action="/memories/update">{hidden}
        <input type="hidden" name="id" value="{E(row['id'])}"><input type="hidden" name="revision" value="{row['revision']}">
        <p>来源 {E(row['source'])} · 事件 {row['seq']} · {'已过期' if expired else E(row['status'])}</p>
        <textarea name="content" rows="4" style="width:95%">{E(row['content'])}</textarea>
        <label>类型<select name="kind">{options('kind',[('preference','偏好'),('decision','项目决策'),('experience','验证经验')])}</select></label>
        <label>范围<select name="scope">{options('scope',[('project','来源项目'),('user','当前本机用户所有项目')])}</select></label>
        <label>状态<select name="status">{options('status',[('pending','待确认'),('active','启用并允许召回'),('disabled','停用')])}</select></label>
        <label>生效时间（留空立即生效）<input type="datetime-local" name="valid_from" value="{valid_from}"></label>
        <label>过期时间（留空不过期）<input type="datetime-local" name="expires" value="{expiry}"></label><button>保存</button>
        <details><summary>来源证据</summary><pre>{E(row['evidence'])}</pre></details><a href="/memories?related={E(row['id'])}">检查相似或可能冲突的记忆</a></form>
        <form method="post" action="/memories/delete">{hidden}<input type="hidden" name="id" value="{E(row['id'])}"><button>删除记忆</button></form></div>''')
    from .semantic_memory import status
    index_status=status(store)
    index_status["job"]="running" if getattr(demo,"memory_index_running",False) else getattr(demo,"memory_index_result",{})
    related=''
    if qs.get('related'):
        suggestions=store.related(qs['related'])
        related='<h2>相似记忆核对</h2><p>相似度不证明两条记忆矛盾，请核对适用条件后编辑或停用。</p>'+''.join('<div class="card">'+E(r['content'])+'</div>' for r in suggestions)
    active=[r for r in store.list() if r['status']=='active']
    choices=''.join(f'<option value="{E(r["id"])}:{r["revision"]}">{E(r["content"][:100])}</option>' for r in active)
    relations=f'''<h2>确认记忆关系</h2><p>只按你确认的关系处理。同义标记不会自动合并正文；“左侧替代右侧”会停用右侧，保留历史，且不会在新记忆过期后自动恢复旧记忆。</p>
    <form method="post" action="/memories/relate">{hidden}<select name=left>{choices}</select>
    <select name=kind><option value=conflicts>存在冲突，召回时提示核实</option><option value=duplicates>含义重复</option><option value=supersedes>左侧替代右侧（停用右侧）</option></select>
    <select name=right>{choices}</select><button>确认关系</button></form>
    <details><summary>关系历史</summary><pre>{E(store.relations())}</pre></details>'''
    body=f'''<h1>长期记忆</h1><p>{E(qs.get('notice',''))}</p>
    <p>只从你选择的会话提取候选。用户原话与成功任务中的验证记录分别保存；模型自评不会自动成为事实。确认启用后，新任务按相关性召回。运行中的会话暂不导入。所选来源的后续对话结束后会增量提取候选。</p>
    <p>候选采用本地规则提取，不调用模型，不上传日志。请将一次性任务整理成可复用的偏好或决策。常见密钥格式会遮盖，但仍请检查正文。</p>
    {related}{relations}<form method="post" action="/memories/select">{hidden}{''.join(sessions)}
    <label>初始范围<select name="scope"><option value="project">来源项目</option><option value="user">当前本机用户所有项目</option></select></label><button>提取所选会话的候选记忆</button></form>
    <h2>语义记忆索引</h2><p>{E(index_status)}</p><form method="post" action="/memories/reindex">{hidden}<button>建立或更新本地语义索引</button></form><p>首次建立后，启用或修改记忆会自动增量索引。旧版本向量立即失效；未索引条目仍可关键词召回。相似不等于一致，不会自动覆盖偏好。</p><h2>已选择的来源</h2>{''.join(sources)}<h2>记忆条目</h2>{''.join(cards) or '<p>尚无候选记忆。</p>'}
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
    if path=='/memories/reindex':
        import threading
        from .semantic_memory import enable_auto
        enable_auto(store)
        if getattr(demo,'memory_index_running',False):return '语义记忆索引正在建立，请稍后刷新。'
        demo.memory_index_running=True
        def work():
            try:
                from .semantic_memory import build
                demo.memory_index_result=build(store)
            except Exception as exc:demo.memory_index_result={'error':type(exc).__name__}
            finally:demo.memory_index_running=False
        threading.Thread(target=work,daemon=True).start()
        return '已开始建立本地语义记忆索引，完成后刷新查看覆盖率。'
    if path=='/memories/delete':store.delete(form['id'])
    elif path=='/memories/revoke':store.revoke(form['id'])
    elif path=='/memories/update':
        expiry=datetime.datetime.fromisoformat(form['expires']).timestamp() if form.get('expires') else None
        valid_from=datetime.datetime.fromisoformat(form['valid_from']).timestamp() if form.get('valid_from') else None
        store.update(form['id'],form['content'],form['kind'],form['scope'],form['status'],expiry,int(form['revision']),valid_from)
    elif path=='/memories/relate':
        left,lr=form['left'].rsplit(':',1);right,rr=form['right'].rsplit(':',1)
        store.relate(left,right,form['kind'],int(lr),int(rr))
    else:raise ValueError('未知记忆操作')
    return '记忆设置已保存。'
