"""Endpoint-scoped capacity metadata; no guessing from arbitrary model names."""
import hashlib
import json
import threading
import time
import urllib.request
from urllib.parse import urlparse

_cache = {}
_lock = threading.Lock()


def _key(cfg):
    return (cfg.chat_url(), hashlib.sha256(cfg.api_key.encode()).hexdigest())


def parse(payload):
    result = {}
    for item in payload.get('data', []):
        if not isinstance(item, dict) or not isinstance(item.get('id'), str): continue
        value = next((item[k] for k in ('context_window','context_length','max_context_length') if k in item), None)
        if isinstance(value, int) and not isinstance(value, bool) and 1024 <= value <= 100_000_000:
            result[item['id']] = value
    return result


def discover(cfg):
    if not cfg.is_real or not cfg.api_key: return
    key = _key(cfg)
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 3600: return
    # Never forward authorization to a redirected metadata endpoint.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    try:
        url = cfg.chat_url().rsplit('/chat/completions', 1)[0] + '/models'
        request = urllib.request.Request(url, headers={'Authorization':'Bearer '+cfg.api_key})
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=4) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000: raise ValueError('metadata too large')
        values = parse(json.loads(raw))
    except Exception:
        values = {}
    with _lock: _cache[key] = (time.monotonic(), values)


def resolve(cfg, model=None):
    if cfg.context_window > 0: return cfg.context_window, '手动覆盖'
    model = model or cfg.model_or('mid')
    with _lock:
        entry = _cache.get(_key(cfg))
        if entry and model in entry[1]: return entry[1][model], '接口 /models 元数据'
    if urlparse(cfg.base_url).hostname == 'api.deepseek.com' and model in {'deepseek-flash','deepseek-v4-flash','deepseek-v4-pro'}:
        return 1_000_000, '官方模型配置（接口未提供容量）'
    return 64_000, '容量未知：保守回退，非模型上限'
