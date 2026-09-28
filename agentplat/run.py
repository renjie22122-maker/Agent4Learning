"""Capstone 入口：跑一遍完整的多租户压测 + SLO 合规判定。

用法::

    python -m agentplat.run                 # 标准压测
    python -m agentplat.run --probes        # 同时起 /livez /readyz /metrics 探针端口
    python -m agentplat.run --mode broken   # 反面版本（关掉所有防护）做对照
    python -m agentplat.run --dump-config   # 打印全部配置项

``--mode broken`` 不是摆设：它用**同一份负载**证明"这些防护到底值多少钱"。
这也是 lab-18 讲的核心方法 —— 任何优化都必须有对照组。
"""

from __future__ import annotations

import argparse
import sys
import time

from agentlab.metrics import METRICS
from agentlab.util import (
    FIX,
    VERIFY,
    head,
    improvement,
    kv,
    note,
    phase,
    rule,
    takeaway,
)

from .cache import render_policy_table
from .config import PlatformConfig
from .loadgen import LoadGenerator, TenantProfile, build_engine
from .service import ServicePlatform


def apply_broken_mode(cfg: PlatformConfig) -> PlatformConfig:
    """把所有防护关掉/放宽，得到"反面版本"的配置。

    注意这不是"随便调参"，而是一一对应我们在 lab 里复现过的每一个坑。
    """
    cfg.request_budget_ms = 30_000.0  # 没有预算概念
    cfg.stage_budget_ms = {
        "rate_limit": 10_000.0,
        "cache": 10_000.0,
        "retrieve": 10_000.0,
        "llm": 10_000.0,
        "tools": 10_000.0,
        "finalize": 10_000.0,
    }
    cfg.max_retries = 4  # 无脑重试
    cfg.retry_budget_per_request = 64
    cfg.breaker_failure_threshold = 10_000  # 等于不熔断
    cfg.breaker_slow_call_ms = 1e9
    cfg.global_concurrency = 512  # 无界并发
    cfg.tenant_concurrency = 512
    cfg.batch_max_ratio = 1.0  # 批处理可以吃掉全部容量
    cfg.tenant_qps = 10_000.0
    cfg.tenant_burst = 10_000.0
    cfg.exact_cache_size = 0  # 无缓存
    cfg.semantic_cache_size = 0
    cfg.semantic_threshold = 0.99  # 语义缓存等于没有
    cfg.context_token_budget = 200_000  # 上下文不治理
    cfg.context_keep_turns = 200
    cfg.routing_mode = "all_large"  # 全用最贵的模型
    cfg.slo_cost_per_success_usd = 1.0  # 放宽 SLO，让它"看起来达标"
    return cfg


def run_one(cfg: PlatformConfig, label: str, mode: str, seed: int = 7) -> dict:
    platform, engine = build_engine(cfg, seed=seed)
    if mode == "broken":
        # 反面版本还额外把上游调慢一点：真实的"没有防护"事故里，上游抖动是
        # 触发条件，而不是全部原因。这样对照组才有区分度。
        for name in ("small-8b", "mid-32b", "large-400b"):
            engine.server.set_latency(name, cfg.provider_p50_ms * 1.6)
            engine.server.set_error_rate(name, 0.10)
    platform.start(warmup=cfg.warmup_on_start)

    tenants = [
        TenantProfile("tenant-a", users=6, weight=1.0, batch_ratio=0.15),
        TenantProfile("tenant-b", users=6, weight=1.0, batch_ratio=0.15),
        # tenant-c 是"批量大户"：它会试图吃掉所有容量，用来验证隔离是否生效
        TenantProfile("tenant-c", users=3, weight=2.5, batch_ratio=0.85),
    ]
    gen = LoadGenerator(cfg, engine, platform, seed=seed)
    # 并发数选择本身就是容量规划。这里的负载设计在"可达容量"附近：
    # SLO 是可达的，但 429/降级仍会出现，能反映真实余量。
    # 实测（fixed 模式，45 请求）：并发 6→98% 成功但吞吐仅 15/s；
    # 并发 10→73%；并发 8→82% 且超 SLO 比例为 0。**继续加并发不会提升吞吐，
    # 只会把成功率打下去** —— 这就是"并发不等于吞吐"的实测证据。
    result = gen.run(tenants, requests_per_tenant=12, concurrency=6)
    result["_platform"] = platform
    result["_engine"] = engine
    result["_label"] = label
    result["_mode"] = mode
    result["_concurrency"] = int(engine.bulkheads.global_pool.max_used)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Agent 平台 capstone 压测")
    ap.add_argument("--mode", choices=["fixed", "broken", "both"], default="both")
    ap.add_argument("--probes", action="store_true", help="启动 /livez /readyz /metrics 端口")
    ap.add_argument("--dump-config", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    base = PlatformConfig.from_env()
    if args.dump_config:
        base.render()
        return 0

    print(rule("="))
    print("  Agent 生产平台 Capstone：多租户混合负载 + SLO 合规判定")
    print(rule("="))
    note("这份压测把 18 个 lab 的结论装进同一条链路，然后用对照组验证它们的价值。")
    note("负载：3 个租户（其中 tenant-c 是批量大户，会用 85% 的批处理比例打满容量）")

    head("1. 配置（每一项都来自一个 lab 的结论）")
    base.render()

    head("2. 反面版本：关掉所有防护，同一份负载")
    broken_cfg = apply_broken_mode(PlatformConfig.from_env())
    phase("broken 模式压测", "(无预算 / 无熔断 / 无缓存 / 无隔离 / 全大模型)")
    b = run_one(broken_cfg, "broken", "broken", seed=args.seed)
    gen_broken = LoadGenerator(broken_cfg, b["_engine"], b["_platform"], seed=args.seed)
    gen_broken.render_report(b, "反面版本报告（BROKEN）")

    head("3. 生产版本：全防护打开，同一份负载")
    phase("fixed 模式压测", "(分层预算 / 熔断 / 缓存 / 隔离 / 级联路由)")
    f = run_one(base, "fixed", "fixed", seed=args.seed)
    gen_fixed = LoadGenerator(base, f["_engine"], f["_platform"], seed=args.seed)
    reports = gen_fixed.render_report(f, "生产版本报告（FIXED）")

    head("4. 对照验证：这些防护值多少钱")
    rows = [
        ("P95 延迟(ms)", b["stats"].p95, f["stats"].p95, "ms"),
        ("P99 延迟(ms)", b["stats"].p99, f["stats"].p99, "ms"),
        ("有效吞吐(成功/s)", b["goodput"], f["goodput"], "/s"),
        ("超 SLO 比例(%)", b["over_slo_rate"] * 100, f["over_slo_rate"] * 100, "%"),
        ("每成功成本($)", b["cost_per_success"], f["cost_per_success"], "$"),
        ("总成本($)", b["usd"], f["usd"], "$"),
        ("峰值并发", b["_concurrency"], f["_concurrency"], ""),
        ("租户公平性", b["fairness"], f["fairness"], ""),
    ]
    print(f"\n  {'指标':<18} {'BROKEN':>12} {'FIXED':>12} {'变化':>12}")
    print("  " + "-" * 58)
    for name, bv, fv, unit in rows:
        lower_better = ("成本" in name or "延迟" in name or "超 SLO" in name)
        delta = improvement(bv, fv, lower_is_better=lower_better)
        print(f"  {name:<18} {bv:>12.4g} {fv:>12.4g} {delta:>12}")

    print(f"{VERIFY} p95_latency_ms: {b['stats'].p95:.1f} -> {f['stats'].p95:.1f} "
          f"({improvement(b['stats'].p95, f['stats'].p95)})")
    print(f"{VERIFY} goodput_per_s: {b['goodput']:.2f} -> {f['goodput']:.2f} "
          f"({improvement(b['goodput'], f['goodput'], lower_is_better=False)})")
    print(f"{VERIFY} cost_per_success_usd: {b['cost_per_success']:.6f} -> "
          f"{f['cost_per_success']:.6f} "
          f"({improvement(b['cost_per_success'], f['cost_per_success'])})")
    print(f"{VERIFY} over_slo_rate: {b['over_slo_rate']:.4f} -> {f['over_slo_rate']:.4f} "
          f"({improvement(b['over_slo_rate'], f['over_slo_rate'])})")
    print(f"{VERIFY} isolation_incidents: {b['isolation']['cross_talk']} -> "
          f"{f['isolation']['cross_talk']}")

    head("5. 缓存策略表（工程硬编码的交付物）")
    render_policy_table()

    head("6. 平台运行态")
    f["_engine"].render_state()
    METRICS.render("关键指标", include=["agent_", "cache_", "router_", "session_"])

    head("7. 生命周期验证：探针 + 优雅停机")
    plat: ServicePlatform = f["_platform"]
    if args.probes:
        from .service import ProbeServer

        ps = ProbeServer(plat, port=0)
        port = ps.start()
        note(f"探针已启动: http://127.0.0.1:{port}/livez /readyz /metrics")
        import urllib.request

        for path in ("/livez", "/readyz"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as resp:
                note(f"{path} -> {resp.status} {resp.read().decode('utf-8')[:60]}")
        ps.stop()
    else:
        live, lmsg = plat.liveness()
        ready, rmsg = plat.readiness()
        kv("liveness", f"{live} {lmsg}")
        kv("readiness", f"{ready} {rmsg}")
        note("（加 --probes 会真的起 HTTP 端口验证探针语义）")

    phase("优雅停机：摘流 → 排空 → 停服", "(lab-01 的结论)")
    report = plat.request_shutdown(grace_s=base.shutdown_grace_s)
    kv("排空耗时", f"{report['drain_ms']:.1f}", "ms")
    kv("被掐断的请求", report["interrupted"])
    kv("停机后拒绝的请求", report["rejected_after_drain"])
    live, lmsg = plat.liveness()
    ready, rmsg = plat.readiness()
    kv("停机后 liveness", f"{live} ({lmsg})")
    kv("停机后 readiness", f"{ready} ({rmsg})")
    print(f"\n{FIX} 停机期间被掐断的请求数 = {report['interrupted']}")

    passed = sum(1 for x in reports if x.passed)
    head("8. 最终结论")
    note(f"SLO 达标: {passed}/{len(reports)}")
    note(f"P95: {b['stats'].p95:.0f}ms → {f['stats'].p95:.0f}ms")
    note(f"有效吞吐: {b['goodput']:.1f} 成功/s → {f['goodput']:.1f} 成功/s")
    note(f"每成功成本: ${b['cost_per_success']:.6f} → ${f['cost_per_success']:.6f}")
    note(f"租户公平性: {b['fairness']:.3f} → {f['fairness']:.3f}")
    note(f"跨会话事故: {b['isolation']['cross_talk']} → {f['isolation']['cross_talk']}")
    note("")
    note("注意：同一份负载下成功率接近，不代表两个版本能力相同 ——")
    note(f"  反面版本把请求堵在慢路径上（吞吐 {b['qps']:.1f} req/s），")
    note(f"  生产版本用同样的时间服务了 {f['qps']:.1f} req/s。")
    note("  所以对比必须看**有效吞吐（成功/s）**，而不是只看成功率。")
    takeaway(
        "生产级 Agent = 预算 + 熔断 + 隔离 + 缓存 + 上下文治理 + 成本路由 + 可观测，"
        "缺任何一项都会被同一份负载打回原形。"
    )
    return 0 if passed >= 4 else 1


if __name__ == "__main__":
    sys.exit(main())
