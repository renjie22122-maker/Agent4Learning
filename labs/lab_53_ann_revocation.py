"""Actual HNSW backend, synthetic vectors: retrieval must obey revocation."""
import tempfile
from pathlib import Path
import numpy as np
from agentlab.util import lab
from agentplat.knowledge import KnowledgeBase
from agentplat import vector_knowledge as vectors,ann_index

def main():
    with lab('lab-53-ann-revocation','ANN 撤销一致性','图索引不能替代权威数据状态'):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);kb=KnowledgeBase(root/'kb');doc=root/'source.txt';doc.write_text('original')
            kb.import_file(doc)
            class Model:
                key='lab53'
                def encode(self,texts,query=False):return [(i,np.array([1,0],dtype='<f4')) for i,_ in enumerate(texts)]
            vectors.build(kb,embedder=Model());ann_index.build(kb,'lab53')
            index,_,_=ann_index.load(kb,'lab53');kb.remove(kb.list_documents()[0]['id'])
            query=np.array([[1,0]],dtype='float32')
            before=len(index.search(query,1)[1][0]);after=len(ann_index.search(kb,'lab53',query,1))
            assert before==1 and after==0
            print('[BROKEN-REPRODUCED] 撤销资料后，原始 HNSW 图仍返回旧节点')
            print('[FIX-APPLIED] 返回前按 SQLite 当前版本和启用状态过滤；重建发布新索引代')
            print(f'[VERIFY] revoked_hits: {before} -> {after}')
            print('[TAKEAWAY] ANN 管近邻，权威数据库管权限和版本；性能评测另测 recall@k 与 p95。')
    return 0
if __name__=='__main__':raise SystemExit(main())
