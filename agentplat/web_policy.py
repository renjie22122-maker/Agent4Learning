"""Host-controlled public-web domain grants, reloadable without restarting agents."""
import ipaddress
import json
import os
from pathlib import Path
import re
import threading

POLICY_PATH = Path(__file__).resolve().parents[1] / '.agent-runtime' / 'web-policy.json'
_lock = threading.RLock()


def normalize_domains(text):
    result = set()
    for value in re.split(r'[\s,]+', text.strip()):
        if not value:
            continue
        value = value.lower().rstrip('.').encode('idna').decode('ascii')
        if len(value) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value):
            raise ValueError('请输入完整域名，不要填写 URL、端口、路径或通配符')
        if '.' not in value or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in value.split('.')):
            raise ValueError('域名格式无效')
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            raise ValueError('网页授权只接受域名，不接受 IP 地址')
        result.add(value)
    return sorted(result)


def domains():
    with _lock:
        if POLICY_PATH.exists():
            value = json.loads(POLICY_PATH.read_text(encoding='utf-8'))
            return normalize_domains(','.join(value['domains']))
        return normalize_domains(os.environ.get('AGENTLAB_WEB_DOMAINS', ''))


def save_domains(text):
    values = normalize_domains(text)
    with _lock:
        POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = POLICY_PATH.with_suffix('.tmp')
        temp.write_text(json.dumps({'domains': values}, ensure_ascii=False), encoding='utf-8')
        temp.replace(POLICY_PATH)
    return values


def policy():
    with _lock:
        saved=json.loads(POLICY_PATH.read_text(encoding='utf-8')) if POLICY_PATH.exists() else {}
        mode=saved.get('mode','allowlist')
        if mode not in ('public','allowlist','off'):raise ValueError('未知网页访问模式')
        return {'mode':mode,'domains':domains()}

def allowed(host):
    value=policy()
    return value['mode']=='public' or value['mode']=='allowlist' and host.lower() in value['domains']

def parse_sites(text):
    from urllib.parse import urlsplit
    hosts=[]
    for value in re.split(r'[\s,，;；]+',text.strip()):
        if not value:continue
        parts=urlsplit(value if '://' in value else 'https://'+value)
        if parts.scheme not in ('http','https') or parts.username or parts.password or not parts.hostname:
            raise ValueError('请填写无用户名密码的 HTTP(S) 网址或域名')
        hosts.extend(normalize_domains(parts.hostname))
    if not hosts:raise ValueError('请先输入网址或域名')
    return sorted(set(hosts))

def change(action,value):
    with _lock:
        current=policy()
        if action=='mode':
            if value not in ('public','allowlist','off'):raise ValueError('请选择有效的访问模式')
            current['mode']=value
        elif action=='add':current['domains']=sorted(set(current['domains'])|set(parse_sites(value)))
        elif action=='remove':current['domains']=sorted(set(current['domains'])-set(normalize_domains(value)))
        elif action=='clear':current['domains']=[]
        else:raise ValueError('未知网页授权操作')
        POLICY_PATH.parent.mkdir(parents=True,exist_ok=True)
        temp=POLICY_PATH.with_suffix('.tmp');temp.write_text(json.dumps(current,ensure_ascii=False),encoding='utf-8');temp.replace(POLICY_PATH)
        return current
