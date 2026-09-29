"""Deterministic semantic fixture; actual BGE quality is separately evaluated."""
import json,tempfile
from pathlib import Path
from unittest.mock import patch
import numpy as np
from agentlab.util import lab
from agentplat.memory import MemoryStore
from agentplat import semantic_memory

def main():
    with lab('lab-54-semantic-memory','语义记忆与版本','相关不等于仍然有效'):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=MemoryStore(root/'memory');source=root/'session.jsonl'
            source.write_text(json.dumps({'seq':1,'kind':'session/created','data':{'task':'猫'}}))
            store.select_source(source,root);row=store.list()[0];store.update(row['id'],'猫','decision','project','active',None,1)
            class Model:
                key='lab54'
                def encode(self,texts,query=False):return [(i,np.array([1,0],dtype='<f4')) for i,_ in enumerate(texts)]
            with patch.object(semantic_memory,'config',return_value={}):before=int(not store.search(root,'喵'))
            semantic_memory.build(store,Model())
            with patch.object(semantic_memory,'config',return_value={'enabled':True}),patch.object(semantic_memory,'LocalEmbedder',return_value=Model()):
                after=int(not store.search(root,'喵'));store.delete(row['id']);assert not store.search(root,'喵')
            assert before==1 and after==0
            print('[BROKEN-REPRODUCED] 无词重叠的同义查询漏掉记忆（确定性语义向量夹具）')
            print('[FIX-APPLIED] 融合语义相似度，过滤来源、项目、状态、版本和过期时间')
            print(f'[VERIFY] missed_memories: {before} -> {after}')
            print('[TAKEAWAY] 召回率和有效性须分别验证；此夹具不代表真实 embedding 准确率。')
    return 0
if __name__=='__main__':raise SystemExit(main())
