"""Reproducible ANN scale evaluation and real local memory retrieval.

Synthetic vector latency/recall is not an end-to-end QA benchmark.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def ann_benchmark(sizes,dimensions=512,ef_search=512):
    import numpy as np
    from agentplat.ann_index import backend,calibrate
    faiss=backend();reports=[]
    for n in sizes:
        rng=np.random.default_rng(731);vectors=rng.normal(size=(n,dimensions)).astype('float32');faiss.normalize_L2(vectors)
        queries=rng.normal(size=(100,dimensions)).astype('float32');faiss.normalize_L2(queries)
        exact=faiss.IndexFlatIP(dimensions);exact.add(vectors)
        graph=faiss.IndexHNSWFlat(dimensions,32,faiss.METRIC_INNER_PRODUCT);graph.hnsw.efConstruction=160;graph.hnsw.efSearch=ef_search
        start=time.perf_counter();graph.add(vectors);build=time.perf_counter()-start
        calibration=calibrate(graph)
        times={};results={}
        for name,index in [('exact',exact),('hnsw',graph)]:
            index.search(queries[:5],10);latencies=[];found=[]
            for query in queries:
                start=time.perf_counter();_,ids=index.search(query.reshape(1,-1),10)
                latencies.append((time.perf_counter()-start)*1000);found.append(ids[0])
            times[name]={'p50_ms':float(np.percentile(latencies,50)),'p95_ms':float(np.percentile(latencies,95))}
            results[name]=found
        recall=float(np.mean([len(set(a)&set(b))/10 for a,b in zip(results['exact'],results['hnsw'])]))
        reports.append({'vectors':n,'dimensions':dimensions,'queries':100,'recall_at_10':recall,'build_seconds':build,'serialized_bytes':len(faiss.serialize_index(graph)),'latency':times,'ef_search':graph.hnsw.efSearch,'calibration':calibration})
    return reports


def memory_benchmark():
    from agentplat.memory import MemoryStore
    from agentplat.semantic_memory import build
    examples=[('后端代码统一使用 Python，测试框架采用 pytest。','服务端的编程语言与验证工具是什么？'),
              ('所有导出报表必须保留两位小数，金额以人民币计。','财务输出的数值精度和币种如何处理？'),
              ('生产数据库迁移必须先备份，审批后才能执行。','上线时修改数据表之前有哪些必要步骤？')]
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=MemoryStore(root/'memory');expected=[]
        for i,(content,query) in enumerate(examples):
            source=root/f'{i}.jsonl';source.write_text(json.dumps({'seq':1,'kind':'session/created','data':{'task':content}}),encoding='utf-8');store.select_source(source,root)
            row=next(r for r in store.list() if r['content']==content);store.update(row['id'],content,'decision','project','active',None,1);expected.append(row['id'])
        index=build(store);results=[]
        for (content,query),identity in zip(examples,expected):
            hits=store.search(root,query,3)
            results.append({'query':query,'expected':content,'top1_correct':bool(hits and hits[0]['id']==identity),'recall_at_3':any(h['id']==identity for h in hits),'scores':[h['retrieval'] for h in hits]})
        return {'index':index,'cases':results}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--sizes',nargs='+',type=int,default=[10000,100000]);parser.add_argument('--memory-only',action='store_true');parser.add_argument('--dimensions',type=int,default=512);parser.add_argument('--ef-search',type=int,default=512);args=parser.parse_args()
    report={'memory':memory_benchmark()}
    if not args.memory_only:report['ann']=ann_benchmark(args.sizes,args.dimensions,args.ef_search)
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
