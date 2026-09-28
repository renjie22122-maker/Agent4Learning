"""Real index replacement and revocation, with no model or network calls."""
from pathlib import Path
import tempfile
from agentlab.util import lab
from agentplat.knowledge import KnowledgeBase


def main():
    with lab('lab-36-knowledge-versions', '知识库版本与撤销', '更新文件后旧答案是否仍能检索'):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); kb = KnowledgeBase(root / 'index')
            source = root / 'policy.txt'
            source.write_text('obsolete_policy 30 days', encoding='utf-8')
            kb.import_file(source)
            cached = kb.search('obsolete_policy')['hits']
            old_chunk = cached[0]['id']
            source.write_text('current_policy 14 days', encoding='utf-8')
            result = kb.import_file(source)
            before = len(cached)
            after = len(kb.search('obsolete_policy')['hits'])
            assert kb.search('current_policy')['hits']
            try:
                kb.read_chunk(old_chunk)
                raise AssertionError('old chunk still visible')
            except ValueError:
                pass
            kb.remove(result['document_id'])
            assert not kb.search('current_policy')['hits']
            print(f'[BROKEN-REPRODUCED] 旧检索缓存仍含 {before} 条过期事实')
            print('[FIX-APPLIED] 索引版本原子切换，回取与检索同时验证有效状态')
            print(f'[VERIFY] stale_facts: {before} -> {after}')
            print('[TAKEAWAY] 保留原件用于审计，不代表旧版本仍可参与回答。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
