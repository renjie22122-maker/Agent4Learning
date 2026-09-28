"""Host CLI: python -m agentplat.knowledge_cli --workspace PATH import FILE_OR_DIR."""
import argparse
import json
from pathlib import Path
import webbrowser
from .knowledge import KnowledgeBase, database_root
from .workspace import DEFAULT_WORKSPACE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=DEFAULT_WORKSPACE)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('import').add_argument('path')
    sub.add_parser('search').add_argument('query')
    sub.add_parser('list')
    sub.add_parser('reindex', help='增量建立本地 embedding 向量索引')
    sub.add_parser('index-status', help='显示向量索引覆盖率')
    sub.add_parser('remove').add_argument('document_id')
    sub.add_parser('open', help='在本机浏览器打开已认证的工作台入口')
    args = parser.parse_args()
    kb = KnowledgeBase(database_root(args.workspace))
    if args.command == 'open':
        record = Path(__file__).resolve().parents[1] / '.agent-runtime' / 'desktop-access.json'
        webbrowser.open(json.loads(record.read_text(encoding='utf-8'))['url'])
        return 0
    if args.command == 'import':
        result = kb.import_path(args.path)
    elif args.command == 'search':
        result = kb.search(args.query)
    elif args.command == 'list':
        result = kb.list_documents()
    elif args.command in ('reindex','index-status'):
        from .vector_knowledge import build, status
        result = build(kb) if args.command == 'reindex' else status(kb)
    else:
        kb.remove(args.document_id); result = {'removed_from_search': args.document_id}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(args.command == 'import' and any(r['status'] == 'error' for r in result))


if __name__ == '__main__':
    raise SystemExit(main())
