"""Host-selected knowledge scopes. No model-supplied filesystem roots."""
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlencode
from .knowledge import KnowledgeBase, database_root


def sources(root, sid='', general=False):
    result = []
    if sid:
        private = database_root(root) if general else database_root(Path(root)/'.session-knowledge'/sid)
        result.append(dict(id='session', label='本会话资料', root=str(private)))
    if not general:
        result.append(dict(id='project', label='项目知识库', root=str(database_root(root))))
    result.append(dict(id='public', label='公共知识库（需主动启用）', root=str(database_root(root).parent/'public')))
    return result


def settings_path(manager, sid):
    import hashlib
    key = hashlib.sha256(sid.encode()).hexdigest()
    return Path(manager.state_path).parent/'.agent-runtime'/'knowledge-selections'/(key+'.json')


def selected(manager, sid, available):
    path = settings_path(manager, sid)
    ids = json.loads(path.read_text(encoding='utf-8')) if sid and path.exists() else ['session','project']
    return [s for s in available if s['id'] in ids]


def context(demo, sid=''):
    if sid:
        live = demo.live_sessions.get(sid)
        state = live[0] if live else next((s for s in demo.list_agent_sessions(10000) if s['session_id']==sid), None)
        if state is None: raise ValueError('会话不存在；未切换到其他知识库')
        return sources(state['workspace'], sid, state.get('conversation_kind')=='general')
    from .workspaces import DEFAULT_WORKSPACE
    general = not demo.ws_mgr.current_group and demo.ws_mgr.current == DEFAULT_WORKSPACE
    return sources(demo.ws_mgr.current, general=general)


def resolve(demo, params):
    available=context(demo, params.get('session',''))
    scope=params.get('scope') or ('session' if params.get('session') else 'project')
    source=next((s for s in available if s['id']==scope),None)
    if not source: raise ValueError('请先选择会话或项目，再选择知识库范围')
    return source


def configure(workspace, manager, sid):
    workspace.knowledge_sources=selected(manager,sid,sources(workspace.root,sid,workspace.general_chat))
    workspace.knowledge_session=sid


def save_selection(demo, sid, ids):
    if not sid: raise ValueError('请先创建或打开一个会话')
    available=context(demo,sid)
    if not set(ids)<= {s['id'] for s in available}: raise ValueError('无效知识库范围')
    live=demo.live_sessions.get(sid)
    if live and live[0].get('status')=='running': raise ValueError('任务运行中不能修改检索范围；请完成或停止后再设置')
    children=getattr(live[1],'children',None) if live else None
    if children and any(children.get(k).get('status') in ('queued','running','pending') for k in list(children.tasks)):
        raise ValueError('子任务仍在运行；请结束后再修改知识库检索范围')
    path=settings_path(demo.ws_mgr,sid);path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as f:
        json.dump(ids,f);temporary=Path(f.name)
    try: os.replace(temporary,path)
    finally: temporary.unlink(missing_ok=True)
    if live:
        from .independent_review import retire
        retire(live[1], '用户修改了知识库检索范围')
        live[1].ws.knowledge_sources=[s for s in available if s['id'] in ids]
        live[1].session.append('knowledge/selection', scopes=ids)


class ScopedKnowledge:
    def __init__(self, workspace): self.workspace=workspace
    def sources(self):
        if hasattr(self.workspace,'knowledge_sources'): return self.workspace.knowledge_sources
        return [dict(id='project',label='项目知识库',root=str(self.workspace.knowledge_root))]
    def tag(self, item, source):
        result={**item,'scope':source['id'],'scope_label':source['label']}
        result['id']=source['id']+':'+item['id']
        if 'citation' in item: result['citation']='kb:'+result['id']
        if 'neighbors' in item:
            result['neighbors']=[{**n,'citation':'kb:'+source['id']+':'+n['citation'].removeprefix('kb:')} for n in item['neighbors']]
        if 'document_id' in item:
            result['original_url']='/knowledge/document?'+urlencode(dict(session=getattr(self.workspace,'knowledge_session',''),scope=source['id'],id=item['document_id']))
        return result
    def list_documents(self):
        return [self.tag(d,s) for s in self.sources() for d in KnowledgeBase(s['root']).list_documents()]
    def search(self,query,top_k=5):
        if not 1<=top_k<=20 or len(query)>2000: raise ValueError('无效检索参数')
        hits=[];methods=[]
        for source in self.sources():
            result=KnowledgeBase(source['root']).search(query,top_k)
            methods.append(dict(scope=source['id'],method=result.get('method'),warning=result.get('warning')))
            for rank,hit in enumerate(result['hits']): hits.append({**self.tag(hit,source),'scope_rank':rank})
        # Interleave per-library rankings: BM25/vector scores from different indexes are not comparable.
        hits.sort(key=lambda h:h['scope_rank'])
        return dict(hits=hits[:top_k],method='各库混合检索后按库内排名交错合并',sources=methods,untrusted_reference=True)
    def read_chunk(self,chunk_id):
        prefix,sep,raw=chunk_id.partition(':')
        for source in self.sources():
            if sep and source['id']!=prefix: continue
            try: return self.tag(KnowledgeBase(source['root']).read_chunk(raw if sep else chunk_id),source)
            except ValueError: continue
        raise ValueError('分块不存在、已撤销或不在本会话启用的知识库范围')


def snapshot_sources(parent, child, destination):
    from .knowledge import snapshot
    records=[];child.knowledge_sources=[]
    for source in ScopedKnowledge(parent).sources():
        target=Path(destination)/source['id']
        records.append(dict(scope=source['id'],**snapshot(source['root'],target)))
        child.knowledge_sources.append({**source,'root':str(target)})
    child.knowledge_session=getattr(parent,'knowledge_session','')
    return records
