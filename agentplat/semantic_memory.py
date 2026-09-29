"""Local revision-bound memory vectors; eligibility always comes from MemoryStore."""
from .vector_knowledge import LocalEmbedder,config
import threading
_jobs={}
_jobs_lock=threading.RLock()


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS memory_vectors (id TEXT, revision INTEGER, model TEXT, window INTEGER, vector BLOB, PRIMARY KEY(id,model,window))')
    db.execute('CREATE TABLE IF NOT EXISTS memory_index_options (key TEXT PRIMARY KEY,value TEXT)')


def enable_auto(store):
    with store.db() as db:
        schema(db);db.execute("INSERT OR REPLACE INTO memory_index_options VALUES ('auto','1')")


def schedule(store):
    """After the first explicit index build, updates trigger a single local job."""
    with store.db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_index_options'").fetchone():return
        if not db.execute("SELECT 1 FROM memory_index_options WHERE key='auto' AND value='1'").fetchone():return
    key=str(store.root.resolve())
    with _jobs_lock:
        if key in _jobs:_jobs[key]=True;return
        _jobs[key]=False
    def worker():
        try:
            while True:
                build(store)
                with _jobs_lock:
                    if not _jobs.get(key):_jobs.pop(key,None);return
                    _jobs[key]=False
        except Exception as exc:
            with store.db() as db:
                schema(db);db.execute("INSERT OR REPLACE INTO memory_index_options VALUES ('last_error',?)",(type(exc).__name__,))
            with _jobs_lock:_jobs.pop(key,None)
    threading.Thread(target=worker,daemon=True,name='memory-index').start()


def build(store,embedder=None):
    settings=config()
    if not settings and embedder is None:return {'enabled':False,'indexed':0}
    model=embedder or LocalEmbedder(settings);count=0
    while True:
        with store.db() as db:
            schema(db)
            rows=db.execute("SELECT m.* FROM memories m JOIN sources s ON s.id=m.source WHERE m.status='active' AND s.enabled=1 AND (m.expires IS NULL OR m.expires>strftime('%s','now')) AND NOT EXISTS (SELECT 1 FROM memory_vectors v WHERE v.id=m.id AND v.revision=m.revision AND v.model=?) LIMIT 8",(model.key,)).fetchall()
        if not rows:break
        vectors=model.encode([r['content'] for r in rows])
        with store.db() as db:
            for window,(owner,vector) in enumerate(vectors):
                row=rows[owner]
                current=db.execute('SELECT revision,status FROM memories WHERE id=?',(row['id'],)).fetchone()
                if current and current['revision']==row['revision'] and current['status']=='active':
                    db.execute('INSERT OR REPLACE INTO memory_vectors VALUES (?,?,?,?,?)',(row['id'],row['revision'],model.key,window,vector.tobytes()))
        count+=len(rows)
    with store.db() as db:db.execute("DELETE FROM memory_index_options WHERE key='last_error'")
    return {'enabled':True,'indexed':count,'model':model.key}


def search(store,rows,query,embedder=None):
    if not rows or (not config() and embedder is None):return {}
    import numpy as np
    model=embedder or LocalEmbedder(config())
    eligible={r['id']:r['revision'] for r in rows}
    with store.db() as db:
        schema(db)
        if not db.execute('SELECT 1 FROM memory_vectors WHERE model=? LIMIT 1',(model.key,)).fetchone():return {}
        queries=np.stack([v for _,v in model.encode([query],query=True)])
        cursor=db.execute('SELECT id,revision,vector FROM memory_vectors WHERE model=?',(model.key,))
        scores={}
        while True:
            batch=[r for r in cursor.fetchmany(256)]
            if not batch:break
            valid=[r for r in batch if eligible.get(r['id'])==r['revision']]
            if not valid:continue
            similarity=(np.stack([np.frombuffer(r['vector'],dtype='<f4') for r in valid])@queries.T).max(axis=1)
            for row,score in zip(valid,similarity):scores[row['id']]=max(scores.get(row['id'],-1),float(score))
        return scores


def status(store):
    with store.db() as db:
        schema(db)
        total=db.execute("SELECT count(*) FROM memories m JOIN sources s ON s.id=m.source WHERE m.status='active' AND s.enabled=1 AND (m.expires IS NULL OR m.expires>strftime('%s','now'))").fetchone()[0]
        try:key=LocalEmbedder(config()).key
        except (OSError,KeyError):return {'enabled':False,'active':total,'indexed':0}
        indexed=db.execute("SELECT count(DISTINCT m.id) FROM memories m JOIN sources s ON s.id=m.source JOIN memory_vectors v ON v.id=m.id AND v.revision=m.revision WHERE m.status='active' AND s.enabled=1 AND (m.expires IS NULL OR m.expires>strftime('%s','now')) AND v.model=?",(key,)).fetchone()[0]
        error=db.execute("SELECT value FROM memory_index_options WHERE key='last_error'").fetchone()
        return {'enabled':True,'active':total,'indexed':indexed,'method':'local BGE + lexical + recency','error':error[0] if error else ''}
