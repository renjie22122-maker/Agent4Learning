"""打一枪确认服务可用，并把单次请求的 span 树摊开给人看。

用法: python tools/smoke_ask.py "你的问题" [租户]
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8800"


def ask(q: str, tenant: str = "alpha", **extra) -> dict:
    params = {"q": q, "tenant": tenant, "format": "json", **extra}
    url = f"{BASE}/ask?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    q = sys.argv[1] if len(sys.argv) > 1 else "生产环境怎么定位内存泄漏"
    tenant = sys.argv[2] if len(sys.argv) > 2 else "alpha"
    r = ask(q, tenant)
    print(f"问题   : {q}")
    print(f"回答   : {r['answer'][:80]}")
    print(f"模型   : {r['model']}   延迟: {r['latency_ms']}ms   成本: ${r['usd']}"
          f"   缓存: {r['cached']} {r['cache_layer']}")
    print(f"tokens : in={r['tokens']['in']} out={r['tokens']['out']}"
          f"   上下文={r['context_tokens']}   工具={r['tool_calls']}次   重试={r['retries']}")
    print(f"trace  : {r['trace_id']}")
    print("span 树 :")
    for s in r["spans"]:
        attrs = "  " + " ".join(f"{k}={v}" for k, v in list(s["attrs"].items())[:3]) if s["attrs"] else ""
        print(f"   {s['span']:<12}{s['ms']:>8}ms  {s['status']}{attrs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
