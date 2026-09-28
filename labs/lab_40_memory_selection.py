"""Unreviewed conversation facts must not silently become long-term memory."""
from pathlib import Path
import tempfile,json
from agentlab.util import lab
from agentplat.memory import MemoryStore


def main():
    with lab('lab-40-memory-selection','选择会话、确认记忆与撤销','候选资料不是已经确认的长期事实'):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'session.jsonl';store=MemoryStore(root/'memory')
            source.write_text(json.dumps({'seq':1,'kind':'session/created','data':{'task':'以后默认使用 pytest'}}),encoding='utf-8')
            store.select_source(source,root)
            before=len(store.list())  # Broken: return every stored candidate.
            after=len(store.search(root,'pytest'))
            assert before==1 and after==0
            row=store.list()[0]
            store.update(row['id'],row['content'],'preference','project','active',None,1)
            assert len(store.search(root,'pytest'))==1
            assert store.search(root/'other-project','pytest')==[]
            store.delete(row['id']);store.refresh(source)
            assert store.search(root,'pytest')==[]
            print('[BROKEN-REPRODUCED] 将所有候选直接召回，会把未确认内容当作长期事实')
            print('[FIX-APPLIED] 来源选择、确认、范围过滤、删除墓碑共同参与检索')
            print(f'[VERIFY] unapproved_entries_used: {before} -> {after}')
            print('[TAKEAWAY] 保存日志、生成候选、允许召回是三个不同动作。')
    return 0


if __name__=='__main__':raise SystemExit(main())
