"""公开网页取证：域名授权、固定解析地址、大小限制与可回取来源。"""
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import socket
import ssl
from urllib.parse import urlsplit, urljoin


class SourceStore:
    def __init__(self, root, domains=()):
        self.root = Path(root) / '.sources'
        self.domains = frozenset(d.lower() for d in domains)

    def fetch(self, url: str, max_bytes=200000, timeout_s=15):
        self.domains = frozenset(self.domain_provider()) if hasattr(self, 'domain_provider') else self.domains
        original = url
        for _ in range(4):
            parts = urlsplit(url)
            host = (parts.hostname or '').lower()
            if parts.scheme not in ('http', 'https') or parts.username or parts.password:
                raise ValueError('只允许无凭据的 HTTP(S) 网页')
            if hasattr(self,'policy_provider'):
                policy=self.policy_provider()
                permitted=policy['mode']=='public' or policy['mode']=='allowlist' and host in policy['domains']
            else:permitted=host in self.domains
            if not permitted:
                raise PermissionError(f'当前网页访问设置不允许 {host}；请在 /permissions 选择“允许公开网页”或添加此网站')
            port = parts.port or (443 if parts.scheme == 'https' else 80)
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise PermissionError('禁止访问私网、回环或保留地址')
            # 使用已校验 IP 建连，避免检查后第二次 DNS 解析指向私网。
            connection = http.client.HTTPConnection(host, port, timeout=timeout_s)
            connection.sock = socket.create_connection(addresses[0][4], timeout_s)
            if parts.scheme == 'https':
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=host)
            try:
                connection.request('GET', (parts.path or '/') + ('?' + parts.query if parts.query else ''),
                                   headers={'User-Agent': 'Agent4Learning/1.0', 'Accept-Encoding': 'identity'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.getheader('Location', ''))
                    continue
                if response.status != 200:
                    raise RuntimeError(f'HTTP {response.status}')
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise RuntimeError('响应超出大小上限，未将截断内容记为完整来源')
                content_type = response.getheader('Content-Type', '')
            finally:
                connection.close()
            digest = hashlib.sha256(raw).hexdigest()
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / (digest + '.txt')).write_bytes(raw)
            metadata = dict(source_id=digest, requested_url=original, url=url,
                            fetched_at=datetime.now(timezone.utc).isoformat(), sha256=digest,
                            content_type=content_type, bytes=len(raw), complete=True,
                            path=f'.sources/{digest}.txt')
            (self.root / (digest + '.json')).write_text(json.dumps(metadata), encoding='utf-8')
            return {**metadata, 'untrusted_content': raw.decode('utf-8', 'replace')}
        raise RuntimeError('重定向次数超限')

    def validate_claims(self, claims: list[dict], expected_count: int):
        """检查来源存在、引文确实出现、条目覆盖；不冒充语义真实性判断。"""
        unsupported = []
        seen = set()
        for i, claim in enumerate(claims):
            source_id = claim.get('source_id', '')
            if len(source_id) != 64 or any(c not in '0123456789abcdef' for c in source_id):
                unsupported.append(i)
                continue
            path = self.root / (source_id + '.txt')
            quote = claim.get('quote', '')
            key = (source_id, quote)
            raw = path.read_bytes() if path.exists() else b''
            if key in seen or hashlib.sha256(raw).hexdigest() != source_id or not quote or quote not in raw.decode('utf-8', 'replace'):
                unsupported.append(i)
            seen.add(key)
        return dict(expected_count=expected_count, actual_count=len(claims),
                    unsupported=unsupported, complete=len(claims) == expected_count and not unsupported,
                    semantic_truth_verified=False)
