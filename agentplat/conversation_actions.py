"""Local reply feedback and explicit, non-executing conversation branches."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid
from .session import SessionLog, replay


def source(demo, sid):
    row = next((s for s in demo.list_agent_sessions(10000) if s['session_id'] == sid), None)
    if not row or row.get('deleted'):
        raise ValueError('会话不存在或已移入回收站')
    live = demo.live_sessions.get(sid)
    path = live[1].session.path if live else row.get('log_path')
    if not path:
        raise ValueError('会话没有可读取的日志')
    log, skipped = SessionLog.load(Path(path))
    if skipped:
        raise ValueError('会话日志损坏，请先检查记录')
    return row, log


def selected_turn(log, number):
    turns = log.of_kind('ui/turn')
    if number < 1 or number > len(turns):
        raise ValueError('请选择已经结束并保存的回复')
    return turns[number-1]


def feedback(demo, form):
    sid = form.get('session','');number = int(form.get('turn','0'))
    row, log = source(demo, sid)
    turn = selected_turn(log, number)
    vote, reason = form.get('vote',''),form.get('reason','').strip()
    if vote not in ('up','down','') or len(reason) > 2000:
        raise ValueError('评分无效或原因超过 2000 字')
    path = Path(demo.ws_mgr.state_path).parent/'.agent-runtime'/'reply-feedback.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    key = sid+':'+str(number)
    item = dict(session=sid,turn=number,vote=vote,reason=reason,
                reply_hash=hashlib.sha256(turn.data.get('summary','').encode()).hexdigest(),at=time.time())
    if vote:data[key]=item
    else:data.pop(key,None)
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,delete=False) as f:
        json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno());tmp=Path(f.name)
    try:os.replace(tmp,path)
    finally:tmp.unlink(missing_ok=True)
    return item


def ratings(demo, sid):
    path = Path(demo.ws_mgr.state_path).parent/'.agent-runtime'/'reply-feedback.json'
    rows = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    return {str(v['turn']):v for v in rows.values() if v['session']==sid}


def branch(demo, form):
    sid=form.get('session','');number=int(form.get('turn','0'))
    row, log=source(demo,sid);boundary=selected_turn(log,number)
    live=demo.live_sessions.get(sid)
    if live and live[0].get('status')=='running':
        raise ValueError('请先停止源会话，再建立一致的分支快照')
    prefix=SessionLog(log.path,fsync=False);prefix.events=[e for e in log.events if e.seq<=boundary.seq]
    state=replay(prefix)
    if not state.messages or state.unknown_calls:
        raise ValueError('该轮缺少完整上下文或存在结果未知的操作，不能创建可靠分支')
    created=log.of_kind('session/created')
    if not created:raise ValueError('缺少会话来源信息')
    info=dict(created[0].data)
    general=info.get('conversation_kind')=='general'
    mode = form.get('files', '')
    if mode not in ('snapshot', 'current', 'shared') or (general and mode == 'shared'):
        raise ValueError('请选择历史文件版本、当前文件独立副本或显式共享项目文件')
    new_id=time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    note='项目目录共享当前磁盘文件；创建分支不会回滚文件。'
    if mode != 'shared':
        from .conversation_versions import copy_checked, version_roots
        roots = info.get('workspace_roots') or {'main': info['workspace']}
        if mode == 'snapshot':
            roots = version_roots(demo.ws_mgr, boundary.data.get('file_version', {}))
        if general:
            from .general_chat import storage
            destinations = {'main': storage(demo.ws_mgr,new_id)}
        else:
            base = Path(demo.ws_mgr.state_path).parent/'.agent-runtime'/'branches'/new_id
            destinations = {a:base/a for a in roots}
        copy_checked(roots, destinations)
        info.update(workspace=str(next(iter(destinations.values()))),
                    workspace_roots={a:str(p) for a,p in destinations.items()})
        note = ('文件来自所选轮次的已校验快照。' if mode == 'snapshot' else
                '所选轮次没有使用历史文件快照；文件来自创建分支时的当前独立副本。')
        note += ' 各分支文件互不影响；不包含 Git 元数据、依赖目录和运行缓存。新文件范围：'+str(info['workspace_roots'])
    origin=dict(session=sid,turn=number,files=mode,note=note)
    info['branch_source']=origin
    target=SessionLog(Path(demo.ws_mgr.state_path).parent/'.sessions'/(new_id+'.jsonl'),session_id=new_id)
    target.append('session/created',**info)
    messages=list(state.messages)+[{'role':'system','content':'[宿主分支说明] 本对话从原会话第 '+str(number)+' 轮分支。'+note+' 旧工具调用仅作为历史证据，不能自动重放；原会话后续消息未继承。'}]
    # Preserve the context boundary of each inherited turn, including when a
    # branch is itself forked from an earlier turn. Never copy executable intents.
    for event in prefix.events:
        if event.kind in ('conversation/message', 'conversation/snapshot', 'ui/turn'):
            target.append(event.kind, **event.data)
    target.append('conversation/snapshot',messages=messages)
    target.append('branch/created',**origin)
    target.flush('branch_created')
    from .conversations import store_for
    store_for(demo).update(new_id,'rename',(row.get('title') or row.get('task','对话'))[:90]+' · 分支')
    return {'session':new_id,'source':origin}
