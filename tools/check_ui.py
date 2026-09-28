"""检查所有标签页是否正常渲染（状态码 + 关键内容 + 大小）。

界面类改动的回归测试：光看"服务起来了"不够，必须每一页都真的渲染出内容。
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("AGENTLAB_DEMO_BASE", "http://127.0.0.1:8800")

PAGES: list[tuple[str, str, list[str]]] = [
    # 注意：空对话页**不应该**有 span 区块（还没提问），所以这里只检查表单存在。
    # 断言要跟着 UI 文案走 —— 改文案时这里必须同步，否则会误报失败。
    ("对话工作台", "/", ["对话工作台", "提问", "后端：", "人格", "注入内部知识库检索"]),
    ("模型与路由", "/models", ["档位", "降级链", "真实模型名"]),
    ("缓存", "/cache", ["L1 精确", "判别性护栏", "策略表"]),
    ("熔断与限流", "/resilience", ["熔断器", "限流桶", "舱壁", "故障注入"]),
    ("请求历史", "/history", ["trace", "成功率"]),
    ("会话与隔离", "/sessions", ["三元组", "劫持拦截"]),
    ("指标与成本", "/metrics-view", ["每成功任务成本", "按租户分摊", "Prometheus"]),
    ("LLM 设置", "/settings", ["OpenAI 兼容", "API Key", "档位映射"]),
]


def get(path: str, **params) -> tuple[int, str]:
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    code, _ = get("/readyz")
    if code == 0:
        print("服务没起来")
        return 2

    print(f"{'页面':<14}{'路径':<18}{'HTTP':<6}{'大小':>9}  检查")
    print("-" * 78)
    bad = 0
    for label, path, needles in PAGES:
        st, body = get(path)
        missing = [n for n in needles if n not in body]
        ok = st == 200 and not missing
        if not ok:
            bad += 1
        note = "OK" if ok else f"缺少 {missing} (HTTP {st})"
        print(f"{label:<14}{path:<18}{st:<6}{len(body):>8}B  {note}")

    # 提问一次，确认动态渲染（回答 + span）也在工作
    print()
    st, body = get("/", q="什么是熔断", tenant="alpha")
    ok_q = st == 200 and ("回答" in body and "span 归因" in body)
    print(f"{'带提问的首页':<14}{'/?q=...':<18}{st:<6}{len(body):>8}B  "
          f"{'OK（回答+span 已渲染）' if ok_q else '失败：回答区没渲染'}")
    if not ok_q:
        bad += 1

    # trace 页
    st, body = get("/history")
    import re

    m = re.search(r"/trace/([0-9a-f]{6,})", body)
    if m:
        st2, body2 = get(f"/trace/{m.group(1)}")
        ok_t = st2 == 200 and "span 归因" in body2
        print(f"{'单次 trace':<14}{'/trace/…':<18}{st2:<6}{len(body2):>8}B  "
              f"{'OK' if ok_t else '失败'}")
        if not ok_t:
            bad += 1
    else:
        print("单次 trace      （历史里还没有 trace，跳过）")

    # 运维端点
    print()
    for p in ("/livez", "/readyz", "/metrics", "/api/snapshot"):
        st, body = get(p)
        print(f"  {p:<16} HTTP {st:<5} {len(body):>8}B")

    print()
    if bad:
        print(f"❌ 有 {bad} 项失败")
        return 1
    print("✅ 全部标签页渲染正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
