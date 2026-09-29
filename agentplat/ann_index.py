"""Immutable, checksummed FAISS HNSW generations; SQLite remains authoritative."""
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import uuid

_lock=threading.RLock()
_cache={}


def calibrate(index,target=.95,seed=91739):
    """Held-out random query audit, not a guarantee for real user queries."""
    import numpy as np
    import time
    faiss=backend();rng=np.random.default_rng(seed)
    queries=rng.normal(size=(32,index.d)).astype('float32');faiss.normalize_L2(queries)
    exact=faiss.downcast_index(index.storage);k=min(10,index.ntotal)
    def timed(engine):
        found=[];latencies=[]
        for query in queries:
            start=time.perf_counter();_,labels=engine.search(query.reshape(1,-1),k)
            latencies.append((time.perf_counter()-start)*1000);found.append(labels[0])
        return found,float(np.percentile(latencies,95))
    expected,exact_ms=timed(exact)
    trials=[]
    for depth in (128,256,512,1024,2048,4096):
        index.hnsw.efSearch=depth
        found,ms=timed(index)
        recall=float(np.mean([len(set(a)&set(b))/k for a,b in zip(expected,found)]))
        trials.append({'ef_search':depth,'recall_at_10':recall,'p95_ms':ms})
        if recall>=target:break
    return {'target_recall':target,'audit_queries':32,'trials':trials,'exact_p95_ms':exact_ms,
            'quality_passed':recall>=target,'ef_search':depth,
            'search':'hnsw' if recall>=target and ms<exact_ms else 'exact_storage',
            'reason':'质量未达标' if recall<target else ('精确检索更快' if ms>=exact_ms else '质量与速度均通过抽样校准')}


def backend():
    private=Path(__file__).resolve().parents[1]/'.agent-runtime/ann-deps'
    if private.is_dir() and str(private) not in sys.path:sys.path.insert(0,str(private))
    import faiss
    faiss.omp_set_num_threads(2)
    return faiss


def directory(kb,model):
    return kb.root/'ann'/hashlib.sha256(model.encode()).hexdigest()


def manifest(kb,model):
    try:return json.loads((directory(kb,model)/'current.json').read_text())
    except (OSError,ValueError):return None


def load(kb,model):
    meta=manifest(kb,model)
    if not meta:raise ValueError('ANN index not built')
    generation=meta['generation']
    if len(generation)!=32 or any(c not in '0123456789abcdef' for c in generation):raise ValueError('invalid generation')
    root=directory(kb,model)/generation
    key=(str(root),meta['sha256'])
    with _lock:
        if key not in _cache:
            raw=(root/'index.faiss').read_bytes()
            ids_raw=(root/'ids.json').read_bytes()
            if hashlib.sha256(raw+ids_raw).hexdigest()!=meta['sha256']:raise ValueError('ANN checksum mismatch')
            index=backend().read_index(str(root/'index.faiss'))
            ids=json.loads(ids_raw)
            if index.ntotal!=len(ids):raise ValueError('ANN mapping mismatch')
            # Bound retained generations; readers keep their own references.
            if len(_cache)>=4:_cache.pop(next(iter(_cache)))
            _cache[key]=(index,ids)
        return (*_cache[key],meta)


def build(kb,model,rebuild=False):
    import numpy as np
    faiss=backend()
    with _lock:
        index=None;ids=[];watermark=0
        if not rebuild and manifest(kb,model):
            try:
                old,old_ids,meta=load(kb,model)
                index=faiss.clone_index(old);ids=list(old_ids);watermark=meta['watermark']
            except (OSError,ValueError,RuntimeError):pass
        with kb.connect() as db:
            epoch=db.execute('SELECT value FROM vector_epoch WHERE id=1').fetchone()[0]
            if index is not None and meta.get('epoch')!=epoch:index=None;ids=[];watermark=0
            cursor=db.execute('SELECT v.rowid AS vid,v.* FROM vectors v JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.id=c.document_id WHERE v.model=? AND d.active=1 AND v.rowid>? ORDER BY v.rowid',(model,watermark))
            while True:
                rows=cursor.fetchmany(512)
                if not rows:break
                matrix=np.stack([np.frombuffer(r['vector'],dtype='<f4') for r in rows])
                if index is None:
                    index=faiss.IndexHNSWFlat(matrix.shape[1],32,faiss.METRIC_INNER_PRODUCT)
                    index.hnsw.efConstruction=160
                index.add(matrix)
                ids.extend([[r['vid'],r['chunk_id']] for r in rows]);watermark=max(r['vid'] for r in rows)
        if index is None:return {'backend':'hnsw','vectors':0}
        calibration=calibrate(index)
        generation=uuid.uuid4().hex;root=directory(kb,model);dest=root/generation
        dest.mkdir(parents=True)
        faiss.write_index(index,str(dest/'index.faiss'))
        ids_raw=json.dumps(ids,separators=(',',':')).encode();(dest/'ids.json').write_bytes(ids_raw)
        meta={'generation':generation,'model':model,'watermark':watermark,'vectors':len(ids),'backend':'hnsw','epoch':epoch,'calibration':calibration,
              'sha256':hashlib.sha256((dest/'index.faiss').read_bytes()+ids_raw).hexdigest()}
        temporary=root/(generation+'.json');temporary.write_text(json.dumps(meta),encoding='utf-8')
        os.replace(temporary,root/'current.json')
        return meta


def search(kb,model,queries,limit):
    import numpy as np
    index,ids,meta=load(kb,model)
    best={};count=min(index.ntotal,max(64,limit*8))
    with kb.connect() as db:
        if db.execute('SELECT value FROM vector_epoch WHERE id=1').fetchone()[0]!=meta.get('epoch'):
            raise ValueError('ANN generation stale after vector replacement; rebuild required')
        while count:
            engine=backend().downcast_index(index.storage) if meta.get('calibration',{}).get('search')=='exact_storage' else index
            scores,labels=engine.search(np.ascontiguousarray(queries,dtype='float32'),count)
            candidates={}
            for row_s,row_l in zip(scores,labels):
                for score,label in zip(row_s,row_l):
                    if label>=0:
                        vid,chunk=ids[int(label)]
                        candidates[vid]=(chunk,max(float(score),candidates.get(vid,('',-1))[1]))
            # DB filtering handles tombstones, changed versions and revoked docs.
            keys=list(candidates)
            for start in range(0,len(keys),400):
                part=keys[start:start+400]
                rows=db.execute('SELECT v.rowid AS vid FROM vectors v JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.id=c.document_id WHERE d.active=1 AND v.model=? AND v.rowid IN ('+','.join('?'*len(part))+')',[model,*part])
                for row in rows:
                    key,score=candidates[row['vid']];best[key]=max(best.get(key,-1),score)
            if len(best)>=limit or count==index.ntotal:break
            count=min(index.ntotal,count*2)
        # New committed vectors are immediately searchable before next publication.
        cursor=db.execute('SELECT v.chunk_id,v.vector FROM vectors v JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.id=c.document_id WHERE v.model=? AND d.active=1 AND v.rowid>?',(model,meta['watermark']))
        while True:
            rows=cursor.fetchmany(512)
            if not rows:break
            scores=(np.stack([np.frombuffer(r['vector'],dtype='<f4') for r in rows])@queries.T).max(axis=1)
            for row,score in zip(rows,scores):best[row['chunk_id']]=max(best.get(row['chunk_id'],-1),float(score))
    return sorted(best.items(),key=lambda v:(-v[1],v[0]))[:limit]
