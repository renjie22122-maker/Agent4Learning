"""Host import/search UI for the real CodingAgent knowledge base."""
import html
import json
from . import ui
from .knowledge import KnowledgeBase, database_root


def render(demo, qs):
    from urllib.parse import urlencode
    from .knowledge_scopes import context, resolve, selected
    sid=qs.get('session','')
    available=context(demo,sid)
    if not qs.get('scope'):
        qs={**qs,'scope':available[0]['id']}
    source=resolve(demo,qs)
    kb = KnowledgeBase(source['root'])
    query=urlencode({'session':sid,'scope':source['id']})
    hidden=f'<input type="hidden" name="session" value="{html.escape(sid,quote=True)}"><input type="hidden" name="scope" value="{source["id"]}">'
    documents = kb.list_documents()
    rows = []
    token = html.escape(demo.permissions_token)
    for doc in documents:
        rows.append('<tr><td><a href="/knowledge/document?'+html.escape(query)+'&amp;id=' + doc['id'] + '">' + html.escape(doc['name']) + '</a></td>'
                    + f'<td>{html.escape(doc["kind"])}</td><td>{doc["chunks"]}</td>'
                    + '<td>' + html.escape('; '.join(json.loads(doc['warnings']))) + '</td>'
                    + f'<td><form method="post" action="/knowledge/remove">{hidden}<input type="hidden" name="token" value="{token}"><input type="hidden" name="id" value="{doc["id"]}"><button>撤销检索</button></form></td></tr>')
    results = ''
    from .vector_knowledge import status
    index_status = status(kb)
    if qs.get('q'):
        search = kb.search(qs['q'])
        hits = search['hits']
        results += '<p>'+html.escape(search.get('method','')+' '+search.get('warning',''))+'</p>'
        for hit in hits:
            results += f'<div class="card"><b>{html.escape(hit["name"])} — {html.escape(hit["location"])}</b><p>{html.escape(hit["citation"])}</p><pre style="white-space:pre-wrap">{html.escape(hit["text"])}</pre></div>'
        if not hits:
            results = '<p>没有匹配内容。没有据此生成答案。</p>'
    job = demo.knowledge_jobs.get(qs.get('job', ''))
    job_html = ''
    if job and job.get('knowledge_root') == source['root']:
        if job['status'] == 'running':
            job_html += '<meta http-equiv="refresh" content="4"><p>正在导入，页面每 4 秒刷新。大文件和 OCR 可能较慢。</p>'
        job_html += '<pre style="white-space:pre-wrap">' + html.escape(json.dumps(job, ensure_ascii=False, indent=2)) + '</pre>'
    navigation=' · '.join(f'<a href="/knowledge?{html.escape(urlencode(dict(session=sid,scope=s["id"])))}">{html.escape(s["label"])}</a>' for s in available)
    enabled={s['id'] for s in selected(demo.ws_mgr,sid,available)}
    selection=''
    if sid:
        boxes=' '.join(f'<label><input type="checkbox" name="use_{s["id"]}" {"checked" if s["id"] in enabled else ""}>{html.escape(s["label"])}</label>' for s in available)
        selection=f'<div class="card"><h2>本会话允许检索</h2><form method="post" action="/knowledge/select">{hidden}<input type="hidden" name="token" value="{token}">{boxes}<button>保存检索范围</button></form><p>取消全部可关闭知识库检索；保存会影响后续调用，已有聊天引用不会删除。任务运行中请先完成或停止任务。</p></div>'
    else:
        selection='<p>当前为资料管理页。请从具体对话的“知识库”入口设置该会话的检索范围；普通对话需先创建会话。</p>'
    sharing={'session':'仅当前会话及其子任务使用。','project':'同一工作区的项目对话共享；各会话可取消启用。','public':'所有会话都可以主动启用；请仅放入适合共享的资料。'}[source['id']]
    body = f'''<h1>文档知识库</h1><nav>{navigation}</nav>{selection}<h2>正在管理：{html.escape(source['label'])}</h2><p>{sharing} 管理此库不会自动为会话启用它。</p>
    <p>导入本机文件或目录。支持文本、Markdown、CSV/TSV、DOCX、XLSX、PPTX、PDF 和图片 OCR。仅处理本机资料，不调用付费模型。旧版 DOC/XLS 尚不支持。</p>
    <form method="post" action="/knowledge/import">{hidden}<input type="hidden" name="token" value="{token}">
    <label>文件或目录的完整路径<input name="path" required style="width:70%" placeholder="D:\\资料\\项目文档"></label><button>导入并建立索引</button></form>
    <h2>向量检索</h2><p>{html.escape(json.dumps(index_status,ensure_ascii=False))}</p>
    <p>本机 embedding，不上传资料。小库精确检索，达到 5 万向量后自动建立经召回校准的 HNSW；关键词与语义结果融合。尚未索引或模型不可用时会明确显示降级。</p>
    <form method="post" action="/knowledge/reindex">{hidden}<input type="hidden" name="token" value="{token}"><button>补建当前知识库向量索引</button></form>
    {job_html}<h2>当前库检索验证</h2><form method="get" action="/knowledge">{hidden}<input name="q" value="{html.escape(qs.get('q',''), quote=True)}" placeholder="输入关键词或问题"><button>检索</button></form>
    {results}<h2>已导入文档（{len(documents)}）</h2><table><thead><tr><th>原件</th><th>格式</th><th>分块</th><th>提取提示</th><th>操作</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
    <p>Agent 仅检索本会话启用的库。资料是参考内容，不能授予权限。图片当前做 OCR，不解释场景与图表；扫描页会明确提示识别结果。撤销检索会隐藏内容，原件保留用于审计。</p><a href="/agent?session={html.escape(sid,quote=True)}">返回对话</a>'''
    return ui.page('本地知识库', 'agent', body)
