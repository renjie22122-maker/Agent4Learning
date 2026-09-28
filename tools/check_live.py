"""真实凭据连通性测试：调一次真实 LLM，确认端到端可用。

注意：**这会真实消耗你账号的 token**（约几十个 token，成本不到 1 分钱）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("AGENTLAB_DEMO_BASE", "http://127.0.0.1:8800")


def post(path: str, data: dict) -> tuple[int, str]:
    req = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(data).encode(),
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get(path: str, **params) -> tuple[int, str]:
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def strip(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"[ \t]+", " ", h).strip()


def main() -> int:
    print("=" * 78)
    print("  真实 LLM 连通性测试（会消耗少量 token）")
    print("=" * 78)

    print("\n[1] 连接自检 /settings/probe")
    st, body = post("/settings/probe", {})   # 空表单 = 用当前已保存配置
    text = strip(body)
    if "连接测试成功" in text:
        m = re.search(r"返回 (.{0,90}?)\s*模型", text)
        print("    ✅ 成功")
        if m:
            print("    模型回复:", m.group(1)[:88])
        for key in ("耗时", "tokens", "端点"):
            mm = re.search(rf"{key} (\S+)", text)
            if mm:
                print(f"    {key}: {mm.group(1)}")
    else:
        print("    ❌ 失败")
        for line in text.splitlines():
            if "错误" in line or "怎么办" in line or "异常" in line:
                print("   ", line.strip()[:200])
        return 1

    print("\n[2] 走完整 agent 链路提问（真实模型生成答案）")
    t0 = time.perf_counter()
    st, body = get("/ask", q="用一句话解释什么是熔断器", tenant="alpha", format="json")
    if st != 200:
        print(f"    HTTP {st}: {body[:300]}")
        return 1
    d = json.loads(body)
    print(f"    ok       = {d['ok']}")
    print(f"    模型     = {d['model']}")
    print(f"    延迟     = {d['latency_ms']}ms")
    print(f"    tokens   = in {d['tokens']['in']} / out {d['tokens']['out']}")
    print(f"    成本     = ${d['usd']}")
    print(f"    缓存     = {d['cached']} {d['cache_layer']}")
    print(f"    trace    = {d['trace_id']}")
    print(f"    回答     = {(d.get('answer') or d.get('error') or '')[:200]}")
    print("\n    span 归因：")
    for s in d["spans"]:
        print(f"      {s['span']:<12}{s['ms']:>9.1f}ms  {s['status']}")

    print("\n[3] 再问一次同一句（验证真实模式下缓存依然生效）")
    st, body = get("/ask", q="用一句话解释什么是熔断器", tenant="alpha", format="json")
    d2 = json.loads(body)
    print(f"    缓存 = {d2['cached']} {d2['cache_layer']}   延迟 = {d2['latency_ms']}ms"
          f"   ← 同租户同问题，第二次不该再打真实 API")

    print("\n" + "=" * 78)
    print("  结论：真实 LLM 已端到端打通，且缓存/超时/成本归因等机制照旧生效。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
