"""Local BGE embeddings + persistent exact cosine index, fused with lexical ranks.

No document is sent to an external API. Index building is an explicit host job.
The flat index scans bounded batches; it is not an ANN/distributed database.
"""
import hashlib
import heapq
import json
import os
from pathlib import Path
import threading

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / '.agent-runtime/rag.json'
_lock = threading.RLock()
_models = {}


def config():
    if os.environ.get('AGENTLAB_VECTOR_RAG') == '0': return {}
    try:
        value = json.loads(CONFIG.read_text(encoding='utf-8'))
        return value if value.get('enabled') else {}
    except (OSError, ValueError): return {}


class LocalEmbedder:
    def __init__(self, settings):
        self.path = Path(settings['model_path']).resolve()
        revision = (self.path / 'revision.txt').read_text().strip()
        self.key = hashlib.sha256((str(self.path)+revision+'bge-cls-overflow-v1').encode()).hexdigest()

    def encode(self, texts, query=False):
        import numpy as np
        import torch
        from transformers import AutoModel, AutoTokenizer
        with _lock:
            if self.key not in _models:
                torch.set_num_threads(2)
                tokenizer = AutoTokenizer.from_pretrained(str(self.path),local_files_only=True,trust_remote_code=False)
                model = AutoModel.from_pretrained(str(self.path),local_files_only=True,trust_remote_code=False,use_safetensors=True).eval()
                _models[self.key] = tokenizer, model
            tokenizer, model = _models[self.key]
            if query: texts = ['为这个句子生成表示以用于检索相关文章：'+x for x in texts]
            values = tokenizer(texts, padding=True, truncation=True, max_length=512,
                               stride=64, return_overflowing_tokens=True, return_tensors='pt')
            owners = values.pop('overflow_to_sample_mapping').tolist()
            vectors=[]
            with torch.inference_mode():
                for start in range(0,len(owners),16):
                    output = model(**{k:v[start:start+16] for k,v in values.items()}).last_hidden_state[:,0]
                    vectors.extend(torch.nn.functional.normalize(output,p=2,dim=1).cpu().numpy())
            return [(owner,np.asarray(vector,dtype='<f4')) for owner,vector in zip(owners,vectors)]


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS vectors (chunk_id TEXT, model TEXT, window INTEGER, dim INTEGER, vector BLOB, PRIMARY KEY(chunk_id,model,window))')
    db.execute('CREATE INDEX IF NOT EXISTS vectors_model ON vectors(model,chunk_id)')


def status(kb):
    settings=config()
    with kb.connect() as db:
        schema(db)
        total=db.execute('SELECT count(*) FROM chunks c JOIN documents d ON c.document_id=d.id WHERE d.active=1').fetchone()[0]
        if not settings:return {'enabled':False,'chunks':total,'indexed':0,'method':'关键词'}
        try: key=LocalEmbedder(settings).key
        except (OSError, KeyError):return {'enabled':True,'chunks':total,'indexed':0,'error':'本地模型未准备好'}
        count=db.execute('SELECT count(DISTINCT v.chunk_id) FROM vectors v JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.id=c.document_id WHERE v.model=? AND d.active=1',(key,)).fetchone()[0]
        return {'enabled':True,'model':'BGE local / CLS','chunks':total,'indexed':count,'method':'BM25 + cosine + RRF','index':'exact flat / bounded batches'}


def build(kb, progress=None, embedder=None):
    settings=config()
    if embedder is None and not settings:return {'enabled':False,'indexed_now':0}
    model=embedder or LocalEmbedder(settings); done=0
    while True:
        with kb.connect() as db:
            schema(db)
            rows=db.execute('SELECT c.id,c.text FROM chunks c JOIN documents d ON c.document_id=d.id WHERE d.active=1 AND NOT EXISTS(SELECT 1 FROM vectors v WHERE v.chunk_id=c.id AND v.model=?) ORDER BY c.id LIMIT 8',(model.key,)).fetchall()
        if not rows:break
        vectors=model.encode([r['text'] for r in rows])
        with kb.connect() as db:
            for window,(owner,vector) in enumerate(vectors):
                db.execute('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?,?)',(rows[owner]['id'],model.key,window,len(vector),vector.tobytes()))
        done+=len(rows)
        if progress:progress(done)
    return {'enabled':True,'indexed_now':done,'model_key':model.key}


def search(kb, query, limit=50, embedder=None):
    import numpy as np
    model=embedder or LocalEmbedder(config())
    queries=np.stack([v for _,v in model.encode([query],query=True)])
    best=[]
    with kb.connect() as db:
        schema(db)
        cursor=db.execute('SELECT v.chunk_id,v.dim,v.vector FROM vectors v JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.id=c.document_id WHERE v.model=? AND d.active=1',(model.key,))
        while True:
            rows=cursor.fetchmany(512)
            if not rows:break
            matrix=np.stack([np.frombuffer(row['vector'],dtype='<f4') for row in rows])
            scores=(matrix @ queries.T).max(axis=1)
            for row,score in zip(rows,scores):
                candidate=(float(score),row['chunk_id'])
                # Keep extra windows, then collapse duplicate source chunks.
                if len(best)<limit*4:heapq.heappush(best,candidate)
                elif candidate>best[0]:heapq.heapreplace(best,candidate)
    unique={}
    for score,key in best:unique[key]=max(score,unique.get(key,-1))
    return sorted(unique.items(),key=lambda x:(-x[1],x[0]))[:limit]


def hybrid(kb, query, lexical, top_k):
    settings=config()
    if not settings:return {'hits':lexical[:top_k],'method':'FTS5 BM25 + lexical coverage','untrusted_reference':True}
    try:
        info=status(kb)
        if not info['indexed']:
            return {'hits':lexical[:top_k],'method':'lexical fallback','warning':'向量索引尚未建立，请在知识库页面建立索引','index':info,'untrusted_reference':True}
        semantic=search(kb,query)
    except Exception as exc:
        return {'hits':lexical[:top_k],'method':'lexical fallback','warning':'向量检索不可用：'+type(exc).__name__,'untrusted_reference':True}
    scores={};records={x['id']:x for x in lexical}
    for rank,hit in enumerate(lexical):scores[hit['id']]=1/(60+rank+1)
    for rank,(key,cosine) in enumerate(semantic):
        scores[key]=scores.get(key,0)+1/(60+rank+1)
        if key not in records:
            records[key]=kb.read_chunk(key)
        records[key]['cosine']=round(cosine,5)
    hits=[]
    for key in sorted(scores,key=lambda k:(-scores[k],k))[:top_k]:
        hit={**records[key],'fusion_score':scores[key]}
        hit['neighbors']=kb.neighbors(key)
        hits.append(hit)
    return {'hits':hits,'method':'BM25 + BGE cosine / RRF','index':info,
            'warning':'向量索引不完整，未索引内容仍参与关键词检索' if info['indexed']<info['chunks'] else '',
            'untrusted_reference':True}
