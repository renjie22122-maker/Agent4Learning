"""公开网页取证：域名授权、固定解析地址、大小限制与可回取来源。"""
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import time
from urllib.parse import urlsplit, urljoin


class SourceStore:
    def __init__(self, root, domains=()):
        self.root = Path(root) / '.sources'
        self.domains = frozenset(d.lower() for d in domains)

    def fetch(self, url: str, max_bytes=200000, timeout_s=15, allow_truncated=False):
        if not 1 <= int(max_bytes) <= 10_000_000:
            raise ValueError('max_bytes 必须在 1 到 10000000 之间')
        max_bytes = int(max_bytes)
        deadline = time.monotonic() + max(.1, float(timeout_s))
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
            try:
                addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise RuntimeError('DNS_ERROR: 无法解析网站域名') from exc
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise PermissionError('禁止访问私网、回环或保留地址')
            # 使用已校验 IP 建连，避免检查后第二次 DNS 解析指向私网。
            connection = http.client.HTTPConnection(host, port, timeout=timeout_s)
            try:
                errors = []
                for family, socktype, proto, _, sockaddr in addresses:
                    sock = None
                    try:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0: raise TimeoutError('连接时间已耗尽')
                        sock = socket.socket(family, socktype, proto)
                        sock.settimeout(min(remaining, max(.1, timeout_s / len(addresses))))
                        sock.connect(sockaddr)  # Exact validated address; no second DNS lookup.
                        if parts.scheme == 'https':
                            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
                        sock.settimeout(max(.1, deadline - time.monotonic()))
                        connection.sock = sock
                        break
                    except ssl.SSLCertVerificationError as exc:
                        if sock is not None: sock.close()
                        raise RuntimeError('TLS_CERTIFICATE_ERROR: 证书链验证失败；需检查网站证书、代理与信任库，未关闭验证') from exc
                    except OSError as exc:
                        if sock is not None: sock.close()
                        errors.append(type(exc).__name__)
                else:
                    raise RuntimeError('CONNECT_ERROR: 已校验地址均连接失败：' + ', '.join(errors))
                connection.request('GET', (parts.path or '/') + ('?' + parts.query if parts.query else ''),
                                   headers={'User-Agent': 'Agent4Learning/1.0', 'Accept-Encoding': 'identity'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.getheader('Location', ''))
                    continue
                if response.status != 200:
                    raise RuntimeError(f'HTTP_ERROR: HTTP {response.status}')
                raw = response.read(max_bytes + 1)
                complete = len(raw) <= max_bytes
                if not complete and not allow_truncated:
                    raise RuntimeError('SIZE_LIMIT: 响应超出大小上限；可使用 allow_truncated=true 获取明确标注不完整的预览')
                raw = raw[:max_bytes]
                content_type = response.getheader('Content-Type', '')
            finally:
                connection.close()
            digest = hashlib.sha256(raw).hexdigest()
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / (digest + '.txt')).write_bytes(raw)
            metadata = dict(source_id=digest, requested_url=original, url=url,
                            fetched_at=datetime.now(timezone.utc).isoformat(), sha256=digest,
                            content_type=content_type, bytes=len(raw), complete=complete,
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
            try:
                meta = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                meta = {}
            if not meta.get('complete') or key in seen or hashlib.sha256(raw).hexdigest() != source_id or not quote or quote not in raw.decode('utf-8', 'replace'):
                unsupported.append(i)
            seen.add(key)
        return dict(expected_count=expected_count, actual_count=len(claims),
                    unsupported=unsupported, complete=len(claims) == expected_count and not unsupported,
                    semantic_truth_verified=False)
