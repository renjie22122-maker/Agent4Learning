"""端到端驱动 demo 服务：缓存分层、多租户隔离、熔断降级、SSE 流式、优雅停机。

两种用法：

1. **自己拉起服务再驱动**（推荐，可重复）：

       python tools/demo_driver.py

2. **驱动一个已经在跑的服务**：

       python -m agentplat.demo --port 8791 --corpus 20000     # 另一个终端
       AGENTLAB_DEMO_BASE=http://127.0.0.1:8791 python tools/demo_driver.py

**为什么不直接用 PowerShell 的 Invoke-RestMethod**：`&` 在 PowerShell 里是语句
分隔符，`?q=x&tenant=y` 会被吃掉一半参数 —— 第一次演示就踩了这个坑：服务只收到
q，tenant 恒为默认值，看起来像"缓存串租户"。用 urlencode 构造请求可以彻底避免。
**排障第一原则：先怀疑自己的客户端。**
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

EXTERNAL_BASE = os.environ.get("AGENTLAB_DEMO_BASE", "").strip()
BASE = EXTERNAL_BASE  # 由 main() 决定：外部服务 or 自建服务


def _url(path: str, **params) -> str:
    return BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")


def get(path: str, **params) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(_url(path, **params), timeout=90) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def ask(q: str, tenant: str, user: str = "u1", session: str = "s1") -> dict:
    _code, body = get("/ask", q=q, tenant=tenant, user=user, session=session, format="json")
    return json.loads(body)


def head(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def strip_html(text: str) -> str:
    """从 demo 的极简 HTML 页面里抽出可读文本（管理端点给人看的是网页）。"""
    import html as _html
    import re

    body = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
    body = re.sub(r"(?is)<br\s*/?>", "\n", body)
    body = re.sub(r"(?is)</(div|p|h1|h2|tr|li|span|b)>", "\n", body)
    body = re.sub(r"(?s)<[^>]+>", "", body)
    lines = [ln.strip() for ln in _html.unescape(body).splitlines()]
    return "\n".join(ln for ln in lines if ln)


# --------------------------------------------------------------------------
# 自建服务生命周期
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_service(port: int) -> subprocess.Popen:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentplat.demo", "--port", str(port), "--corpus", "20000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError(f"服务启动失败：\n{proc.stdout.read() if proc.stdout else ''}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2):
                return proc
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("服务在 60s 内没有就绪")


def main() -> int:
    global BASE

    own_proc: subprocess.Popen | None = None
    if EXTERNAL_BASE:
        BASE = EXTERNAL_BASE
        print(f"驱动已运行的服务: {BASE}")
    else:
        port = _free_port()
        BASE = f"http://127.0.0.1:{port}"
        print(f"正在启动独立服务实例（端口 {port}）…")
        own_proc = start_service(port)
        print(f"服务就绪: {BASE}\n（演示结束会自动优雅停机并回收该进程）")

    try:
        return run_demo()
    finally:
        if own_proc is not None and own_proc.poll() is None:
            try:
                get("/admin/drain")
                time.sleep(1.0)
            except Exception:  # noqa: BLE001
                pass
            if own_proc.poll() is None:
                own_proc.kill()
            print("\n[driver] 演示服务已回收")


def run_demo() -> int:
    code, body = get("/readyz")
    if code == 0:
        print(f"服务没起来：{body}")
        return 2
    print(f"/readyz -> HTTP {code} {body}")

    head("1. 五层缓存与多租户隔离")
    A = "会话隔离怎么工程化落地"
    B = "会话隔离怎么工程化落地的"
    C = "会话隔离怎么工程化实施"
    rows = [
        ("冷请求（落 LLM）", A, "alpha"),
        ("同租户同句（L1）", A, "alpha"),
        ("换租户同句（应重算）", A, "beta"),
        ("纯措辞增量（多半不命中）", B, "alpha"),
        ("危险近似（护栏拦下）", C, "alpha"),
    ]
    print(f"{'步骤':<26}{'租户':<8}{'model':<12}{'layer':<10}{'延迟':>11}")
    for note, q, t in rows:
        r = ask(q, t)
        print(f"{note:<26}{t:<8}{r['model']:<12}{r['cache_layer'] or '-':<10}"
              f"{r['latency_ms']:>10.1f}ms")
    print("\n结论：L1 精确缓存按 (tenant, query, model) 隔离 → 换租户绝不复用；")
    print("      字符级 L2 语义缓存刻意保守（只接受实质 bigram 完全覆盖），")
    print("      真实改写会漏召回并回落到 LLM —— 漏召回只是少省钱，错命中是事故。")

    head("2. /metrics（Prometheus 风格）")
    _c, m = get("/metrics")
    wanted = ("agent_", "llm_calls_total", "llm_input_tokens_total",
              "llm_output_tokens_total", "llm_cost_usd_total",
              "cache_exact_hits_total", "cache_semantic_hits_total",
              "router_escalated_total", "session_active")
    shown = [ln for ln in m.splitlines()
             if not ln.startswith("#") and any(ln.startswith(w) for w in wanted)]
    print(f"  /metrics 共 {len(m.splitlines())} 行，其中本 demo 相关的 {len(shown)} 条：")
    for line in sorted(shown):
        print("    " + line)
    print("\n  注：这里只列出**已经产生过数据**的指标（未触发的计数器不会出现），")
    print("      这正是 Prometheus 的语义 —— 没数据就是没发生，而不是 0 缺失。")

    head("3. 注入上游劣化 → 观察重试 / 熔断 / 降级")
    # 故意打狠一点（error_rate 0.85 + p50 3s），并把熔断阈值降到 3。
    # 为什么降阈值：演示是**串行**的，每个失败请求要 ~3s，10 次才 30 秒；
    # 而默认要连续失败 6 次才打开。生产上并发几十上百，几秒就累计够了 ——
    # 调阈值只是为了在串行演示里也能看到真实行为，不是把机制改简单。
    _c, _msg = get("/admin/degrade", error_rate=0.85, p50_ms=3000, threshold=3, cooldown=2)
    print("  已注入：error_rate=0.85, p50=3000ms, 熔断阈值=3, 冷却=2s")
    print("  （阈值调到 3 是因为演示是串行的：每失败一次要 ~3s，默认阈值 6 在 30s 内凑不够）\n")

    print(f"  {'#':<4}{'ok':<7}{'model':<12}{'degraded':<10}{'retries':<9}{'延迟':>11}  错误")
    fails = 0
    tag = str(int(time.time()))[-6:]
    instant = 0
    for i in range(8):
        r = ask(f"上游劣化期间的提问 {tag}-{i}", "alpha")
        err = (r.get("error") or "")[:30]
        if not r["ok"]:
            fails += 1
        # 熔断打开后请求会被**瞬间拒绝**（不占线程、不花钱）——这是它的核心价值
        if r["latency_ms"] < 5 and not r["ok"]:
            instant += 1
        print(f"  {i:<4}{str(r['ok']):<7}{r['model']:<12}{str(r['degraded']):<10}"
              f"{r['retries']:<9}{r['latency_ms']:>10.1f}ms  {err}")

    _c, snap = get("/api/snapshot")
    s = json.loads(snap)
    print(f"\n  → 失败 {fails}/8，其中 {instant} 个是**瞬间失败**（<5ms）")
    print(f"     熔断器状态: {s['breakers']}")
    if instant:
        print("     瞬间失败 = 熔断已打开，不再浪费线程和钱去试一个死掉的上游。")
    else:
        print("     熔断尚未打开（连续失败还没到阈值）—— 上游只是慢，还没坏透。")
    print(f"  劣化期间累计成本 ${s['usd']}")
    for line in s["provider"]:
        print("    " + line)

    head("4. 恢复上游 → 熔断器自动回来（不重启进程）")
    _c, msg_html = get("/admin/recover")
    print("  " + strip_html(msg_html).splitlines()[0])
    print("\n  立刻连打（前几个可能仍被熔断拒绝，因为冷却期未到）：")
    print(f"  {'#':<4}{'ok':<7}{'model':<12}{'延迟':>11}  时间线")
    deadline = time.time() + 10
    i = 0
    while time.time() < deadline and i < 30:
        r = ask(f"熔断恢复验证 {tag}-{i}", "alpha")
        quick_reject = r["latency_ms"] < 5 and not r["ok"]
        mark = "瞬间拒绝（熔断仍开）" if quick_reject else "已放行"
        print(f"  {i:<4}{str(r['ok']):<7}{r['model']:<12}{r['latency_ms']:>10.1f}ms  {mark}")
        if not quick_reject and r["ok"]:
            _c, snap2 = get("/api/snapshot")
            print(f"\n  → 熔断器放行探针并成功，随后闭合，服务自己回来了。")
            print(f"     结束状态: {json.loads(snap2)['breakers']}")
            print("     全程没有重启进程、没有改配置 —— 这就是 closed→open→half_open→closed 的闭环。")
            break
        i += 1
        time.sleep(0.4)
    else:
        print("\n  → 超时仍未恢复。检查 cooldown 是否过长，或阈值是否过严。")

    head("5. SSE 流式：TTFT 与端到端 RT 是两个指标")
    url = _url("/ask", q="流式输出怎么优化", tenant="alpha", stream=1)
    print(f"  GET {url}\n")
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            event = None
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event:
                    data = json.loads(line[6:])
                    if event == "span":
                        print(f"    [span] {data['name']:<14}{data['ms']:>8}ms {data['status']}")
                    elif event == "ttft":
                        print(f"    [TTFT] {data['ttft_ms']}ms  ← 用户看到第一个字的时间")
                    elif event == "chunk":
                        print(f"    [chunk] {data['text']!r}")
                    elif event == "done":
                        print(f"    [done] 端到端={data['e2e_ms']}ms  TTFT={data['ttft_ms']}ms"
                              f"  model={data['model']}  cached={data['cached']}")
                        print(f"           {data['note']}")
                    event = None
    except Exception as exc:  # noqa: BLE001
        print(f"  SSE 失败: {type(exc).__name__}: {exc}")

    head("6. 优雅停机：摘流 → 排空 → 停服（liveness 仍为 true）")
    print("  停机前探针：")
    for p in ("/livez", "/readyz"):
        c, b = get(p)
        print(f"    {p:<9} HTTP {c}  {b}")
    c, b = get("/admin/drain")
    print(f"\n  /admin/drain -> HTTP {c}")
    for line in strip_html(b).splitlines():
        if any(k in line for k in ("排空", "掐断", "readiness", "liveness", "拒绝", "ms")):
            print("    " + line)
    print("\n  停机后探针（readiness 应转 false，liveness 仍 true）：")
    for p in ("/livez", "/readyz"):
        c, b = get(p)
        print(f"    {p:<9} HTTP {c}  {b}")
    c, b = get("/ask", q="停机后还能接流量吗", tenant="alpha", format="json")
    print(f"\n  停机后再提问 -> HTTP {c}")
    if c == 503:
        print("    503 + 可重试信号：负载均衡会把它摘掉，用户看到的是透明重试而不是报错。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
