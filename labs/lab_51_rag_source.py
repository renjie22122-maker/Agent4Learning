from pathlib import Path
import tempfile
from agentlab.util import lab
from agentplat.knowledge import KnowledgeBase,snapshot

def main():
    with lab('lab-51-rag-source','验收原始知识来源','作者转录不是独立来源'):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'policy.txt';source.write_text('hotel_limit=680',encoding='utf-8')
            kb=KnowledgeBase(root/'kb');kb.import_file(source)
            source_id=kb.search('hotel_limit')['hits'][0]['id']
            snapshot(kb.root,root/'verifier-kb')
            transcript='hotel_limit=999'
            before=int('999' in transcript)
            original=KnowledgeBase(root/'verifier-kb').read_chunk(source_id)
            after=int('999' in original['text'])
            assert before==1 and after==0 and original['sha256']
            print('[BROKEN-REPRODUCED] 作者转录把 680 改成 999，验收只读转录会接受错误数字')
            print('[FIX-APPLIED] 宿主 SQLite 一致性快照保留原始分块与版本摘要，独立工具回取原文')
            print(f'[VERIFY] false_source_acceptance: {before} -> {after}')
            print('[TAKEAWAY] 来源真实性和检索相关性是两项不同检查；增加向量不能替代来源核验。')
    return 0

if __name__=='__main__':raise SystemExit(main())
