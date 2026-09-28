"""Provider usage accounting, cache-aware published prices, never invoice claims."""
from datetime import datetime, timezone
from html.parser import HTMLParser
import re, time, threading, urllib.request
from urllib.parse import urlparse

SOURCE = 'https://api-docs.deepseek.com/quick_start/pricing/'
# Verified 2026-09-28, peak USD per million: cache hit / miss / output.
_prices = {'deepseek-flash':(.006,.3,1.2),'deepseek-v4-pro':(.044,1.32,3.96)}
_checked = '2026-09-28 官方价格快照'
_last_attempt = 0
_lock = threading.Lock()


def parse_prices(html):
    class Text(HTMLParser):
        def __init__(self): super().__init__(); self.parts=[]
        def handle_data(self,data): self.parts.append(data)
    parser=Text();parser.feed(html);text=' '.join(parser.parts)
    section=text.split('PRICING',1)[1].split('Concurrency',1)[0]
    values=[float(x) for x in re.findall(r'\$\s*(\d+(?:\.\d+)?)',section)]
    if len(values)!=12 or 'OFF-PEAK' not in section or 'PEAK' not in section:
        raise ValueError('unrecognized pricing table')
    for i in (0,1,4,5,8,9):
        if abs(values[i]*2-values[i+2])>1e-9: raise ValueError('unrecognized discount rule')
    return {'deepseek-flash':(values[2],values[6],values[10]),'deepseek-v4-pro':(values[3],values[7],values[11])}


def refresh(cfg):
    global _last_attempt, _checked, _prices
    if not cfg.is_real or urlparse(cfg.base_url).hostname!='api.deepseek.com': return
    with _lock:
        if time.monotonic()-_last_attempt<3600: return
        _last_attempt=time.monotonic()
    try:
        with urllib.request.urlopen(SOURCE,timeout=4) as response: raw=response.read(2_000_001)
        if len(raw)>2_000_000: return
        prices=parse_prices(raw.decode('utf-8'))
        with _lock:
            _prices=prices;_checked=datetime.now(timezone.utc).isoformat()
    except Exception: pass # retain dated snapshot and label it; never pretend a refresh succeeded


def quote(cfg, usage, model=None, timestamp=None):
    model=model or cfg.model_or('mid')
    if model in ('deepseek-v4-flash','deepseek-v4-flash-vision-exp'):model='deepseek-flash'
    with _lock: rates=_prices.get(model);checked=_checked
    official=urlparse(cfg.base_url).hostname=='api.deepseek.com' and rates is not None
    if official:
        hit,miss,out=rates
        now=datetime.fromtimestamp(timestamp if timestamp is not None else time.time(),timezone.utc)
        peak=now.weekday()<5 and (1<=now.hour<4 or 6<=now.hour<10)
        if not peak:hit,miss,out=hit/2,miss/2,out/2
        # Holiday exclusion cannot be inferred from weekdays; show a range at peak.
        note='工作日峰时范围，节假日按低端' if peak else '非峰时'
        low_multiplier=.5 if peak else 1
    else:
        hit=miss=cfg.price_in_per_m;out=cfg.price_out_per_m
        checked='用户配置';note='未自动获取该供应商价格';low_multiplier=1
    cached=max(0,min(usage.cached_tokens,usage.in_tokens))
    cost=((usage.in_tokens-cached)*miss+cached*hit+usage.out_tokens*out)/1_000_000
    return {'usd':cost,'usd_min':cost*low_multiplier,'price_hit_per_m':hit,'price_miss_per_m':miss,'price_out_per_m':out,
            'price_source':SOURCE if official else '用户配置','price_checked_at':checked,'price_note':note,
            'in_tokens':usage.in_tokens,'out_tokens':usage.out_tokens,'cached_tokens':cached,'invoice':False}


def record(cfg,usage,guard=None,model=None,client=None,tag='coding-agent'):
    result=quote(cfg,usage,model,getattr(client,'last_request_started_at',None))
    result['usage_estimated']=getattr(client,'last_usage_estimated',True)
    if guard is not None:
        average=((usage.in_tokens-result['cached_tokens'])*result['price_miss_per_m']+result['cached_tokens']*result['price_hit_per_m'])/max(1,usage.in_tokens)
        guard.record(usage.in_tokens,usage.out_tokens,average,result['price_out_per_m'],tag=tag)
    return result


def include_children(state, agent):
    manager = getattr(agent, 'children', None)
    if not manager: return
    with manager.lock:
        values = [item['data'] for item in manager.tasks.values()]
        state['child_tokens'] = sum(item.get('used_tokens',0) for item in values)
        state['child_usd'] = sum(item.get('usd',0) for item in values)
        state['child_usd_min'] = sum(item.get('usd_min',item.get('usd',0)) for item in values)
        state['child_usage_estimated'] = any(item.get('usage_estimated',True) for item in values)
