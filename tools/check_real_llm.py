"""验证真实 LLM 全链路：通过界面端点配置 → 测连接 → 提问 → 检查错误如实暴露。

用一个无效 key，所以预期拿到干净的 401，并且**不能被兜底伪装成"正常回答"**。
这正是这个集成最重要的一条验收标准。
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


def req(path: str, data: dict | None = None, **params) -> tuple[int, str, str]:
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), resp.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), url
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}", url


def strip_tags(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", h).strip()


def main() -> int:
    print("=" * 80)
    print("  真实 LLM 全链路验收")
    print("=" * 80)

    # ---------- 1. 通过保存接口切到真实后端（无效 key）----------
    print("\n1) 提交设置表单：切到真实 LLM（故意用无效 key）")
    st, body, url = req("/settings/save", data={
        "provider": "real",
        "preset": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_key": "sk-invalid-key-for-acceptance-test",
        "m_small": "deepseek-chat",
        "m_mid": "deepseek-chat",
        "m_large": "deepseek-reasoner",
        "temperature": "0.3",
        "max_tokens": "64",
        "timeout_s": "20",
        "max_parallel": "2",
        "price_in_per_m": "0.27",
        "price_out_per_m": "1.1",
        "offline_mock_fallback": "1",
    })
    print(f"   HTTP {st}  ->  {url[:100]}")
    m = re.search(r"notice=([^&]+)", url)
    if m:
        print("   提示：", urllib.parse.unquote(m.group(1)))
    time.sleep(2)

    # ---------- 2. 顶栏环境标识 ----------
    st, body, _ = req("/")
    real_badge = "真实 LLM" in body
    print(f"\n2) 顶栏环境标识显示『真实 LLM』: {real_badge}")
    if not real_badge:
        print("   ❌ 没切换成功")
        return 1

    # ---------- 3. 连接自检 ----------
    print("\n3) 连接自检 /settings/probe")
    st, body, _ = req("/settings/probe", data={
        "base_url": "https://api.deepseek.com",
        "api_key": "sk-invalid-key-for-acceptance-test",
        "m_small": "deepseek-chat",
        "m_mid": "deepseek-chat",
    })
    text = strip_tags(body)
    ok_fail = "连接测试失败" in text and "401" in text
    print(f"   HTTP {st}  显示『连接测试失败 + 401』: {ok_fail}")
    hint = re.search(r"(检查 API Key[^<]{0,60})", text)
    if hint:
        print("   给出的排查提示：", hint.group(1)[:78])

    # ---------- 4. 提问：错误必须如实暴露，不能假装成功 ----------
    print("\n4) 提问（关键验收：错误不能被兜底伪装成正常回答）")
    st, body, _ = req("/ask", q="用一句话解释熔断", tenant="alpha", format="json")
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        print("   ❌ /ask 没返回 JSON：", body[:200])
        return 1
    print(f"   ok        = {data['ok']}")
    print(f"   error     = {(data.get('error') or '')[:110]}")
    print(f"   model     = {data.get('model')}")
    print(f"   latency   = {data.get('latency_ms')}ms")
    print(f"   retries   = {data.get('retries')}")

    masked = "invalid" not in (data.get("error") or "").lower() and "401" in (data.get("error") or "")
    if data["ok"]:
        print("   ❌ 竟然成功了 —— 无效应报错")
        return 1
    if "401" in (data.get("error") or ""):
        print("   ✅ 401 如实抛出，没有被模拟器兜底掩盖")
    else:
        print("   ⚠️  错误码不是 401，请人工确认：", (data.get("error") or "")[:120])

    # ---------- 5. 真实 key 泄漏检查 ----------
    print("\n5) 安全检查：界面任何地方都不能回显完整 key")
    leaked = []
    for path in ("/", "/settings", "/models", "/metrics-view", "/api/snapshot", "/metrics"):
        st, page, _ = req(path)
        if "sk-invalid-key-for-acceptance-test" in page:
            leaked.append(path)
    if leaked:
        print("   ❌ 泄漏页面：", leaked)
        return 1
    print("   ✅ 未泄漏（只显示掩码形式）")

    print("\n" + "=" * 80)
    print("  结论：真实 LLM 已接入，鉴权类错误如实暴露，key 不回显。")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
