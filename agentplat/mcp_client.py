"""显式配置的 MCP HTTP 客户端（2025-03-26，JSON/SSE 响应子集）。

不执行服务端采样/授权请求，不把 server instructions 提升成系统指令。
浏览器等工具通过管理员配置的服务及本地 allowlist 暴露。
"""
import json
import threading
import urllib.request
from urllib.parse import urlsplit


class MCPClient:
    def __init__(self, url, *, allowed_tools=(), headers=None, timeout_s=15):
        parts = urlsplit(url)
        if parts.scheme not in ('https', 'http') or parts.username or parts.password:
            raise ValueError('MCP URL 无效')
        if parts.scheme == 'http' and parts.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('远端 MCP 必须使用 HTTPS')
        self.url, self.allowed = url, frozenset(allowed_tools)
        self.headers = dict(headers or {})
        self.timeout = timeout_s
        self.session = ''
        self.initialized = False
        self.counter = 0
        self.lock = threading.RLock()

    def request(self, method, params=None, notification=False):
        with self.lock:
            self.counter += 1
            payload = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
            if not notification:
                payload['id'] = self.counter
            headers = {**self.headers, 'Content-Type': 'application/json',
                       'Accept': 'application/json, text/event-stream',
                       'MCP-Protocol-Version': '2025-03-26'}
            if self.session:
                headers['Mcp-Session-Id'] = self.session
            request = urllib.request.Request(self.url, json.dumps(payload).encode(), headers)
            # 禁止把认证头跟随重定向发往另一个端点。
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    raise RuntimeError('MCP 重定向被拒绝')
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
                self.session = response.headers.get('Mcp-Session-Id', self.session)
                if notification:
                    return {}
                if 'text/event-stream' in response.headers.get('Content-Type', ''):
                    raw = bytearray()
                    event = []
                    result = None
                    while len(raw) <= 1_000_000:
                        line = response.readline(65537)
                        if not line:
                            break
                        raw.extend(line)
                        if line.startswith(b'data:'):
                            event.append(line[5:].strip())
                        elif not line.strip() and event:
                            candidate = json.loads(b'\n'.join(event))
                            event = []
                            if candidate.get('id') == payload['id']:
                                result = candidate
                                break
                    if result is None:
                        raise RuntimeError('MCP SSE 未返回配对结果或输出超限')
                else:
                    raw = response.read(1_000_001)
                    if len(raw) > 1_000_000:
                        raise RuntimeError('MCP 响应过大')
                    result = json.loads(raw)
            if result.get('id') != payload.get('id') or result.get('jsonrpc') != '2.0':
                raise RuntimeError('MCP 响应 ID/版本不匹配')
            if 'error' in result:
                raise RuntimeError(str(result['error']))
            return result['result']

    def initialize(self):
        with self.lock:
            if self.initialized:
                return
            result = self.request('initialize', {'protocolVersion': '2025-03-26',
                'capabilities': {}, 'clientInfo': {'name': 'Agent4Learning', 'version': '1.0'}})
            if result.get('protocolVersion') != '2025-03-26':
                raise RuntimeError('服务端未接受已实现的 MCP 版本')
            self.request('notifications/initialized', notification=True)
            self.initialized = True

    def list_tools(self):
        self.initialize()
        tools = []
        cursor = None
        seen = set()
        for _ in range(20):
            result = self.request('tools/list', {'cursor': cursor} if cursor else {})
            tools.extend(t for t in result.get('tools', []) if t['name'] in self.allowed)
            cursor = result.get('nextCursor')
            if not cursor:
                return tools
            if cursor in seen:
                raise RuntimeError('MCP 分页游标循环')
            seen.add(cursor)
        raise RuntimeError('MCP 工具列表分页超限')

    def call(self, name, arguments):
        if name not in self.allowed:
            raise PermissionError(f'宿主未授权 MCP 工具 {name}')
        self.initialize()
        result = self.request('tools/call', {'name': name, 'arguments': arguments})
        if result.get('isError'):
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return json.dumps({'untrusted_tool_result': result}, ensure_ascii=False)
