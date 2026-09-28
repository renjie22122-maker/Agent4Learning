"""可信工具子进程入口；请求参数仍由 Workspace 校验。"""
import json
import sys
from .workspace import Workspace

if __name__ == '__main__':
    request = json.loads(sys.argv[1])
    workspace = Workspace(request.pop('root'), allow_shell=False)
    try:
        result = {'ok': True, 'text': workspace._grep_inline(**request)}
    except Exception as exc:
        result = {'ok': False, 'error': str(exc)}
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
