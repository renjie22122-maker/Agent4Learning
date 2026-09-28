"""查看正在运行的 demo 服务状态（命令行速查）。"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.environ.get("AGENTLAB_DEMO_BASE", "http://127.0.0.1:8800")


def main() -> int:
    try:
        raw = urllib.request.urlopen(f"{BASE}/api/snapshot", timeout=15).read().decode()
    except Exception as exc:  # noqa: BLE001
        print(f"服务不可达（{BASE}）：{type(exc).__name__}: {exc}")
        return 2
    s = json.loads(raw)
    print(f"  服务地址   {BASE}")
    print(f"  后端       {s['backend']}  模型 {s['model']}")
    print(f"  已运行     {s['uptime_s']}s")
    print(f"  请求数     {s['requests']}   成功率 {s['ok_rate']:.0%}   缓存命中 {s['cache_hits']}")
    print(f"  P95        {s['p95_ms']}ms")
    print(f"  累计成本   ${s['usd']}")
    print(f"  熔断器     {s['breakers']}")
    print(f"  缓存       {s['cache']}")
    print(f"  会话       {s['sessions']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
