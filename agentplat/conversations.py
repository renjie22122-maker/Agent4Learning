"""Host-owned conversation organization; never changes execution scope or logs."""
import json, os, tempfile
from pathlib import Path

class ConversationStore:
    def __init__(self,path): self.path=Path(path)
    def load(self):
        return json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
    def update(self,sid,action,value=None):
        rows=self.load();item=rows.setdefault(sid,{})
        if action=='rename':
            title=str(value or '').strip()
            if not title or len(title)>120:raise ValueError('名称需要 1 到 120 个字符')
            item['title']=title
        elif action in ('pinned','archived','deleted','unread'):
            if not isinstance(value,bool):raise ValueError('状态必须是布尔值')
            item[action]=value
        elif action=='move':item['display_group']=value
        else:raise ValueError('未知会话操作')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=self.path.parent,delete=False) as f:
            json.dump(rows,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno());temporary=Path(f.name)
        try:os.replace(temporary,self.path)
        finally:temporary.unlink(missing_ok=True)
        return item

def store_for(demo):
    return ConversationStore(demo.ws_mgr.state_path.parent/'.agent-runtime'/'conversations.json')

def mutate(demo,form):
    import secrets
    if not secrets.compare_digest(form.get('token',''),demo.permissions_token):raise PermissionError('invalid form token')
    sid=form.get('session','');action=form.get('action','');value=form.get('value','')
    if not any(row['session_id']==sid for row in demo.list_agent_sessions(10000)):raise ValueError('会话不存在')
    if action=='deleted' and value=='true':
        entry=demo.live_sessions.get(sid)
        if entry and entry[0].get('status')=='running':raise ValueError('请先停止运行中的任务，再移入回收站')
    if action in ('pinned','archived','deleted','unread'):
        if value not in ('true','false'):raise ValueError('无效状态')
        value=value=='true'
    if action=='move' and value not in demo.ws_mgr.groups and value!='__general__':raise ValueError('项目不存在')
    return store_for(demo).update(sid,action,value)
