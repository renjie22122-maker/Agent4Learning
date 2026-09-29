"""Host import/search UI for the real CodingAgent knowledge base."""
import html
import json
from . import ui
from .knowledge import KnowledgeBase, database_root


def render(demo, qs):
    kb = KnowledgeBase(database_root(demo.ws_mgr.current))
    documents = kb.list_documents()
    rows = []
    token = html.escape(demo.permissions_token)
    for doc in documents:
        rows.append('<tr><td><a href="/knowledge/document?id=' + doc['id'] + '">' + html.escape(doc['name']) + '</a></td>'
                    + f'<td>{html.escape(doc["kind"])}</td><td>{doc["chunks"]}</td>'
                    + '<td>' + html.escape('; '.join(json.loads(doc['warnings']))) + '</td>'
                    + f'<td><form method="post" action="/knowledge/remove"><input type="hidden" name="token" value="{token}"><input type="hidden" name="id" value="{doc["id"]}"><button>撤销检索</button></form></td></tr>')
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
    if job and job['workspace'] == str(demo.ws_mgr.current):
        if job['status'] == 'running':
            job_html += '<meta http-equiv="refresh" content="4"><p>正在导入，页面每 4 秒刷新。大文件和 OCR 可能较慢。</p>'
        job_html += '<pre style="white-space:pre-wrap">' + html.escape(json.dumps(job, ensure_ascii=False, indent=2)) + '</pre>'
    body = f'''<h1>当前工作区知识库</h1><p>{html.escape(str(demo.ws_mgr.current))}</p>
    <p>导入本机文件或目录。支持文本、Markdown、CSV/TSV、DOCX、XLSX、PPTX、PDF 和图片 OCR。仅处理本机资料，不调用付费模型。旧版 DOC/XLS 尚不支持。</p>
    <form method="post" action="/knowledge/import"><input type="hidden" name="token" value="{token}">
    <label>文件或目录的完整路径<input name="path" required style="width:70%" placeholder="D:\\资料\\项目文档"></label><button>导入并建立索引</button></form>
    <h2>向量检索</h2><p>{html.escape(json.dumps(index_status,ensure_ascii=False))}</p>
    <p>本机 embedding，不上传资料。小库精确检索，达到 5 万向量后自动建立经召回校准的 HNSW；关键词与语义结果融合。尚未索引或模型不可用时会明确显示降级。</p>
    <form method="post" action="/knowledge/reindex"><input type="hidden" name="token" value="{token}"><button>补建当前知识库向量索引</button></form>
    {job_html}<h2>检索验证</h2><form method="get" action="/knowledge"><input name="q" value="{html.escape(qs.get('q',''), quote=True)}" placeholder="输入关键词或问题"><button>检索</button></form>
    {results}<h2>已导入文档（{len(documents)}）</h2><table><thead><tr><th>原件</th><th>格式</th><th>分块</th><th>提取提示</th><th>操作</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
    <p>Agent 可调用 search_knowledge、read_knowledge_chunk、list_knowledge。资料是参考内容，不能授予权限。图片当前做 OCR，不解释场景与图表；扫描页会明确提示识别结果。撤销检索会隐藏内容，原件保留用于审计。</p><a href="/agent?panel=1">返回 Agent</a>'''
    return ui.page('本地知识库', 'agent', body)
