"""Lab: 长链路 Agent 的 P95 优化 —— 检索 + 多轮推理怎么设计。

对应生产问题
    * 「生产环境 agent 链路很长，包含检索工具和多轮推理，如何优化设计？如何把 P95 的 RT 降下来？」
    * 「大量检索的 RT 越来越高，百万级别的知识库如何性能优化？」

复现的故障
    v0（拍脑袋版）把链路写成一条串行的长直线：
        query_rewrite → retrieve → rerank → llm_reason×2 → tools×3（串行）→ synthesize
    每一步都不慢，但**尾部会叠加**：6 个阶段各自的 P95 串起来，端到端 P95 直接爆掉。
    检索侧更糟：全量扫描（NaiveRetriever）让 RT 随文档量线性增长，而且权限过滤放在
    排序之后 —— 又慢又漏（本 lab 用 10 万篇语料实测这两点）。

观测与修复的顺序（本 lab 的核心方法论）
    1. 先归因：用 Tracer 打 span，按阶段看 P50/P95/max，用 critical_path() 找出谁贡献了尾部；
    2. 再优化：并行化 → 检索换倒排/两阶段 → 过滤下推 → 两级缓存 → 短路 → 对冲 → 流式。
    每一步都重新测量（一步一测），让读者看到边际收益递减。

工程结论
    * P95 是"最慢的那几个阶段之和"，所以优化顺序永远是"先砍尾巴最长的那个"。
    * 并行化把"串行之和"变成"最慢的那个"，是 ROI 最高的一步（零成本）；
    * 检索的 RT 靠"别让昂贵计算见到太多候选"（倒排召回 + 小批量精排），不是靠换更快的库；
    * 对冲/流式是"用钱/用体验换尾部和感知延迟"，必须显式记账：TTFT 和端到端 RT 是两个指标。
"""

from __future__ import annotations

import gc
import sys
import time

from agentlab.metrics import METRICS
from agentlab.orchestration import hedged_call
from agentlab.providers import LLMServer, system, user
from agentlab.store import BM25Index, NaiveRetriever, Query, TwoStageRetriever, build_corpus
from agentlab.tokens import SMALL
from agentlab.tracing import TraceStore, Tracer
from agentlab.util import (BROKEN, FIX, VERIFY, Stats, head, improvement, kv, lab,
                           note, percentile, phase, rng, run_concurrently, takeaway)

LAB_ID = "lab-10-p95-optimization"
MODEL = SMALL.name
N_REQ, WORKERS, LLM_MS = 36, 8, 60.0
TOOL_MS, RERANK_MS, CONF_TH = 35.0, 12.0, 0.35
SYS = "你是企业知识助手"
QUERIES = ["缓存穿透怎么解决", "熔断阈值怎么配", "限流算法有哪些", "内存泄漏怎么排查",
           "协程调度怎么做", "连接池怎么调优", "索引重建方案", "灰度发布流程"]
TENANTS = ("tenant-a", "tenant-b")
STAGES = ("query_rewrite", "retrieve", "rerank", "llm_reason", "tools", "synthesize")

C_CACHE_HIT = METRICS.counter("pipeline_cache_hits_total", "两级缓存命中")
C_SHORT = METRICS.counter("pipeline_short_circuit_total", "短路提前返回")
H_STAGE = METRICS.histogram("pipeline_stage_ms", "链路阶段耗时")
H_E2E = METRICS.histogram("pipeline_e2e_ms", "端到端耗时")


def _llm(srv: LLMServer, prompt: str, tag: str, box: list[int], tenant: str) -> str:
    box[0] += 1
    return srv.call([system(SYS), user(prompt)], model=MODEL, timeout=2.0,
                    tenant=tenant, tag=tag).text


def run_request(srv: LLMServer, idx: BM25Index, cache: dict, i: int, *, parallel: bool = False,
                use_cache: bool = False, short: bool = False) -> dict:
    """一次完整链路。四个开关 = 四个优化步骤，可以逐个打开、逐个测量。"""
    q, tenant = QUERIES[i % len(QUERIES)], TENANTS[i % 2]
    calls = [0]
    tr = Tracer(f"req-{i:03d}")
    t0 = time.perf_counter()

    with tr.span("query_rewrite") as sp:
        rw = cache.get(("rw", q)) if use_cache else None
        if rw is None:
            rw = _llm(srv, f"改写并扩写这个查询：{q}", "rewrite", calls, tenant)
            if use_cache:
                cache[("rw", q)] = rw
        else:
            C_CACHE_HIT.inc()
        sp.set(cached=rw is not None)

    with tr.span("retrieve") as sp:
        rkey = ("rt", tenant, q)
        res = cache.get(rkey) if use_cache else None
        if res is None:
            res = idx.search(Query(rw, top_k=5, tenant=tenant,
                                   groups=frozenset({f"g{tenant[-1]}", "public"})))
            if use_cache:
                cache[rkey] = res
        else:
            C_CACHE_HIT.inc()
        sp.set(scanned=res.scanned, candidates=res.candidates, hits=len(res.hits))

    conf = res.hits[0].score if res.hits else 0.0
    skip_rerank = short and conf >= CONF_TH
    with tr.span("rerank") as sp:
        if not skip_rerank:
            time.sleep(RERANK_MS / 1000.0)
        sp.set(skipped=skip_rerank, conf=round(conf, 3))

    rounds = 1 if skip_rerank else 2  # 高置信度：跳过一轮推理
    with tr.span("llm_reason") as sp:
        prompts = [f"第{k + 1}轮推理：基于检索结果回答「{q}」" for k in range(rounds)]
        if parallel and rounds > 1:
            run_concurrently(lambda k: _llm(srv, prompts[k], "reason", calls, tenant), rounds, rounds)
        else:
            for p in prompts:
                _llm(srv, p, "reason", calls, tenant)
        sp.set(rounds=rounds, parallel=parallel and rounds > 1)

    with tr.span("tools") as sp:
        tools = [f"tool-{k}" for k in range(3)]
        if parallel:
            run_concurrently(lambda k: time.sleep(TOOL_MS / 1000.0), len(tools), len(tools))
        else:
            for _ in tools:
                time.sleep(TOOL_MS / 1000.0)
        sp.set(n=len(tools), parallel=parallel)

    with tr.span("synthesize") as sp:
        _llm(srv, f"汇总成最终答案：{q}", "synth", calls, tenant)
        sp.set(short=skip_rerank)

    ms = (time.perf_counter() - t0) * 1000.0
    for s in tr.roots:
        H_STAGE.observe(s.duration_ms)
    H_E2E.observe(ms)
    if skip_rerank:
        C_SHORT.inc()
    return {"tr": tr, "ms": ms, "calls": calls[0], "short": skip_rerank}


def run_workload(idx: BM25Index, **flags) -> dict:
    srv = LLMServer(max_queue=64, seed=11)
    srv.set_latency(MODEL, LLM_MS, 0.4)  # 压缩墙钟；延迟分布形状与计费规则不变
    cache: dict = {}
    store = TraceStore()

    def one(i: int) -> dict:
        r = run_request(srv, idx, cache, i, **flags)
        store.add(r["tr"])
        return r

    rows = [r for r in run_concurrently(one, N_REQ, WORKERS) if isinstance(r, dict)]
    lat = [r["ms"] for r in rows]
    return {"stats": Stats(lat), "store": store, "rows": rows,
            "calls_per_req": sum(r["calls"] for r in rows) / max(1, len(rows)),
            "short": sum(1 for r in rows if r["short"]),
            "usd": srv.ledger.usd, "cache_hits": METRICS.value("pipeline_cache_hits_total"),
            "tokens": srv.ledger.total_tokens}


def report_attribution(res: dict, title: str) -> dict[str, Stats]:
    """耗时归因：先看谁贡献了尾部，再决定优化谁。"""
    agg = res["store"].aggregate()
    slowest = res["store"].slowest(1)[0]
    print(f"\n  ── {title}：最慢请求的 span 树 ──")
    slowest.render_stage_table()
    path = slowest.critical_path()
    note("critical_path: " + " → ".join(f"{s.name}({s.duration_ms:.0f}ms)" for s in path))
    print(f"  {'stage':<16} {'n':>4} {'p50':>9} {'p95':>9} {'max':>9}  {'占端到端P95':>10}")
    e2e_p95 = res["stats"].p95
    for name in STAGES:
        s = agg.get(name)
        if s:
            print(f"  {name:<16} {s.n:>4} {s.p50:>8.1f}ms {s.p95:>8.1f}ms {s.mx:>8.1f}ms"
                  f"  {s.p95 / e2e_p95:>9.0%}")
    tail = max(((s, n) for n, s in agg.items()), key=lambda x: x[0].p95)
    note(f"尾部最大贡献者：{tail[1]}（P95={tail[0].p95:.0f}ms，占端到端 P95 的 "
         f"{tail[0].p95 / e2e_p95:.0%}）")
    return agg


def bench_retrieval() -> dict:
    """百万级知识库：全量扫描 vs 倒排 vs 两阶段（真实墙钟实测）。"""
    phase("2. 观测 / 归因", "(百万级检索：Naive 全量扫描 vs BM25 倒排 vs 两阶段)")
    t0 = time.perf_counter()
    docs = build_corpus(100_000, seed=7)
    note(f"语料：100_000 篇（构建 {time.perf_counter() - t0:.2f}s；百万级只是线性外推，"
         f"实测受 25s 墙钟与内存约束）")
    qs = [Query(q, top_k=5, tenant="tenant-a", groups=frozenset({"ga", "public"}))
          for q in QUERIES[:3]]
    nav = NaiveRetriever(docs)
    naive = [nav.search(q) for q in qs]
    del nav
    gc.collect()
    t0 = time.perf_counter()
    idx = BM25Index(docs)
    build_s = time.perf_counter() - t0
    bm = [idx.search(Query(q, top_k=5, tenant="tenant-a", groups=frozenset({"ga", "public"})))
          for q in QUERIES]
    two = TwoStageRetriever(idx, recall_k=50, rerank_cost_ms=0.45)
    ts = [two.search(Query(f"{q}+变体{j}", top_k=5, tenant="tenant-a",
                           groups=frozenset({"ga", "public"}), rerank=True))
          for j, q in enumerate(QUERIES)]
    naive_st = Stats([r.latency_ms for r in naive])
    bm_st, two_st = Stats([r.latency_ms for r in bm]), Stats([r.latency_ms for r in ts])
    kv("倒排索引构建", f"{build_s:.2f}", " s")
    kv("全量扫描（100k 篇）scanned", f"{naive[0].scanned}", f"  候选 {naive[0].candidates}")
    kv("倒排召回 scanned(posting)", f"{bm[0].scanned}", f"  候选 {bm[0].candidates}")
    kv("两阶段 精排候选", f"{ts[0].candidates}", f"  实际精排 50")
    print(f"  {'检索方式':<22} {'n':>3} {'p50':>9} {'p95':>9} {'max':>9}")
    for name, st in (("naive 全量扫描", naive_st), ("bm25 倒排 top5", bm_st),
                     ("两阶段 召回50+精排", two_st)):
        print(f"  {name:<22} {st.n:>3} {st.p50:>8.1f}ms {st.p95:>8.1f}ms {st.mx:>8.1f}ms")
    per_cand = 0.45
    note(f"对照：如果把 {ts[0].candidates} 个候选全部精排 = {per_cand}ms × "
         f"{ts[0].candidates} ≈ {per_cand * ts[0].candidates / 1000:.1f}s（按实测单候选成本推算）")
    return {"docs": docs, "idx": idx, "two": two, "naive": naive_st, "bm25": bm_st,
            "two_stage": two_st, "scanned_naive": naive[0].scanned, "scanned_bm25": bm[0].scanned,
            "rerank_candidates": 50, "candidates": ts[0].candidates}


def bench_pushdown(idx: BM25Index) -> dict:
    """权限过滤：下推到召回阶段 vs 排序之后再过滤（打分完才知道谁能看）。"""
    phase("2. 观测 / 归因", "(权限过滤：下推 vs 后置过滤 —— 又慢又漏)")
    over = TwoStageRetriever(idx, recall_k=50, rerank_cost_ms=0.45)   # 后置：多召回再筛
    push = TwoStageRetriever(idx, recall_k=10, rerank_cost_ms=0.45)   # 下推：候选本来就干净
    lat_bad, lat_good, leaked, short = [], [], 0, 0
    for rep in range(2):
        for j, qtext in enumerate(QUERIES):
            tenant = TENANTS[j % 2]
            groups = frozenset({f"g{tenant[-1]}", "public"})
            qv = f"{qtext} 变体{rep}"
            t0 = time.perf_counter()
            wide = over.search(Query(qv, 50, tenant="", groups=frozenset(), rerank=True))
            lat_bad.append((time.perf_counter() - t0) * 1000.0)
            visible = [h for h in wide.hits if h.doc.tenant == tenant or "public" in h.doc.acl]
            leaked += len(wide.hits) - len(visible)  # 越权文档已经进了候选/精排/提示词
            short += 1 if len(visible) < 5 else 0    # 过滤完不够 5 条 = 召回塌陷
            t0 = time.perf_counter()
            push.search(Query(qv, 5, tenant=tenant, groups=groups, rerank=True))
            lat_good.append((time.perf_counter() - t0) * 1000.0)
    bad, good = Stats(lat_bad), Stats(lat_good)
    kv("后置过滤 p50 / p95（召回50+精排50）", f"{bad.p50:.1f} / {bad.p95:.1f}", " ms")
    kv("下推到召回 p50 / p95（召回10+精排10）", f"{good.p50:.1f} / {good.p95:.1f}", " ms")
    kv("后置过滤：越权候选 / 召回塌陷请求", f"{leaked} / {short}", " 次")
    print(f"\n{BROKEN} 排序后再过滤：{leaked} 篇他人私有文档进入精排与提示词，{short} 个请求过滤后"
          f"不足 5 条；而且精排成本按候选数计费，更慢（{bad.p95:.0f}ms > {good.p95:.0f}ms）")
    return {"bad": bad, "good": good, "leaked": leaked, "short": short}


def bench_hedge() -> dict:
    """对冲请求：用小钱买尾部（用钱买 P95）。"""
    phase("3. 修复", "(对冲请求：明确记账'成本换尾部')")

    def run(hedge: bool, n: int = 32) -> dict:
        srv = LLMServer(max_queue=64, seed=5)
        srv.set_latency(MODEL, 200, 0.75)  # 上游抖动：尾部很重

        def one(i: int) -> float:
            msgs = [system(SYS), user(f"推理问题{i}")]
            t0 = time.perf_counter()
            if hedge:
                hedged_call(lambda: srv.call(msgs, model=MODEL, timeout=2.0, tag="hedge"),
                            hedge_after_ms=260.0, max_hedges=1)
            else:
                srv.call(msgs, model=MODEL, timeout=2.0, tag="baseline")
            return (time.perf_counter() - t0) * 1000.0

        lat = [x for x in run_concurrently(one, n, 16) if isinstance(x, float)]
        time.sleep(0.8)  # 等落败的那次对冲调用返回并计费（失败/落败也计费）
        return {"stats": Stats(lat), "usd": srv.ledger.usd, "calls": srv.ledger.calls, "n": n}

    base, hedge = run(False), run(True)
    kv("无对冲 p95 / 成本", f"{base['stats'].p95:.0f}ms / ${base['usd']:.5f}",
       f"  ({base['calls']} 次调用)")
    kv("有对冲 p95 / 成本", f"{hedge['stats'].p95:.0f}ms / ${hedge['usd']:.5f}",
       f"  ({hedge['calls']} 次调用)")
    kv("调用量放大", f"{hedge['calls'] / max(1, base['calls']) - 1:+.0%}",
       "  这就是「用钱买尾部」的账单")
    return {"base": base, "hedge": hedge}


def bench_stream() -> dict:
    """流式 + 分块：TTFT（用户感知）和端到端 RT 是两个指标。"""
    phase("3. 修复", "(流式输出：TTFT vs 端到端)")
    chunks, chunk_ms = 8, 14.0

    def one(i: int) -> dict:
        streaming = i % 2 == 0  # 偶数：分块流式；奇数：等全部生成完再一次性返回
        t0 = time.perf_counter()
        ttft = 0.0
        for k in range(chunks):
            time.sleep(chunk_ms / 1000.0)
            if k == 0:
                ttft = (time.perf_counter() - t0) * 1000.0
        total = (time.perf_counter() - t0) * 1000.0
        return {"total": total, "ttft": ttft if streaming else total, "streaming": streaming}

    rows = [r for r in run_concurrently(one, 32, 16) if isinstance(r, dict)]
    total = Stats([r["total"] for r in rows])
    s_ttft = Stats([r["ttft"] for r in rows if r["streaming"]])
    b_ttft = Stats([r["ttft"] for r in rows if not r["streaming"]])
    kv("流式 TTFT p50 / p95", f"{s_ttft.p50:.0f}ms / {s_ttft.p95:.0f}ms")
    kv("非流式 TTFT(=完整) p50 / p95", f"{b_ttft.p50:.0f}ms / {b_ttft.p95:.0f}ms")
    kv("端到端完整时间 p50 / p95（两者相同）", f"{total.p50:.0f}ms / {total.p95:.0f}ms")
    print(f"\n{FIX} 分块流式不改变端到端 RT（{total.p95:.0f}ms 两边一样），但把用户感知的 "
          f"TTFT 从 {b_ttft.p95:.0f}ms 降到 {s_ttft.p95:.0f}ms")
    return {"total": total, "stream_ttft": s_ttft, "blocked_ttft": b_ttft}


def main() -> int:
    with lab(LAB_ID, "长链路 Agent 的 P95 优化：检索 + 多轮推理怎么设计",
             "agent 链路很长（检索+多轮推理），如何把 P95 的 RT 降下来？百万级知识库怎么优化？"):
        head("1. 复现故障：串行长链路，尾部逐级叠加")
        phase("1. 复现故障", "(v0 串行链路，36 请求 / 8 并发)")
        small_docs = build_corpus(4_000, seed=3)
        idx = BM25Index(small_docs)
        v0 = run_workload(idx)
        kv("端到端 P50 / P95 / max", f"{v0['stats'].p50:.0f} / {v0['stats'].p95:.0f} / "
                                     f"{v0['stats'].mx:.0f}", " ms")
        kv("每请求 LLM 调用次数", f"{v0['calls_per_req']:.2f}", " 次")
        kv("成本", f"${v0['usd']:.5f}", f"  ({v0['tokens']} tokens)")
        print(f"\n{BROKEN} 串行链路：P95={v0['stats'].p95:.0f}ms，每请求 "
              f"{v0['calls_per_req']:.1f} 次 LLM 调用，成本 ${v0['usd']:.5f}")
        report_attribution(v0, "v0 串行")

        head("2. 观测 / 归因：先定位尾巴，再决定优化顺序")
        phase("2. 观测 / 归因", "(参考：单阶段耗时与理论下限)")
        one_req = v0["rows"][0]["tr"]
        note("单请求阶段耗时：" + " ".join(f"{s.name}={s.duration_ms:.0f}ms" for s in one_req.roots))
        note("串行链路的理论下限 = 各阶段之和；并行化后下限 = 最慢阶段 × 轮数")
        ret = bench_retrieval()
        push = bench_pushdown(ret["idx"])
        del ret["docs"], ret["idx"], ret["two"]
        gc.collect()

        head("3. 修复：一步一测（并行 → 检索 → 缓存 → 短路 → 对冲 → 流式）")
        phase("3. 修复", "(① 并行化：3 个工具 + 2 轮独立推理并发)")
        v1 = run_workload(idx, parallel=True)
        kv("端到端 P50 / P95", f"{v1['stats'].p50:.0f}ms / {v1['stats'].p95:.0f}ms",
           f"  ({improvement(v0['stats'].p95, v1['stats'].p95)} vs v0)")
        report_attribution(v1, "v1 并行")

        phase("3. 修复", "(② 两级缓存：查询改写缓存 + 检索结果缓存)")
        v2 = run_workload(idx, parallel=True, use_cache=True)
        kv("端到端 P50 / P95", f"{v2['stats'].p50:.0f}ms / {v2['stats'].p95:.0f}ms",
           f"  ({improvement(v1['stats'].p95, v2['stats'].p95)} vs v1)")
        kv("缓存命中次数", f"{v2['cache_hits']}", " 次（改写 + 检索）")
        kv("每请求 LLM 调用次数", f"{v1['calls_per_req']:.2f} -> {v2['calls_per_req']:.2f}", " 次")

        phase("3. 修复", "(③ 提前返回/短路：高置信度跳过精排 + 一轮推理)")
        v3 = run_workload(idx, parallel=True, use_cache=True, short=True)
        kv("端到端 P50 / P95", f"{v3['stats'].p50:.0f}ms / {v3['stats'].p95:.0f}ms",
           f"  ({improvement(v2['stats'].p95, v3['stats'].p95)} vs v2)")
        kv("短路请求数", f"{v3['short']} / {N_REQ}", f"  置信度阈值 {CONF_TH}")
        kv("每请求 LLM 调用次数", f"{v3['calls_per_req']:.2f}", " 次")
        hedge = bench_hedge()
        stream = bench_stream()

        head("4. 验证：逐项收益与优化排序")
        phase("4. 验证", "(P95 逐步累积 + 检索/成本对照)")
        kv("v0 串行 → v1 并行 → v2 缓存 → v3 短路",
           f"{v0['stats'].p95:.0f} → {v1['stats'].p95:.0f} → {v2['stats'].p95:.0f} → "
           f"{v3['stats'].p95:.0f}", " ms")
        kv("检索 P95：naive → bm25 → 两阶段",
           f"{ret['naive'].p95:.0f} → {ret['bm25'].p95:.0f} → {ret['two_stage'].p95:.0f}", " ms")
        print(f"\n  {'优化步骤':<30} {'ΔP95':>10} {'实现成本':<12} {'风险':<28} 优先级")
        steps = [
            ("① 并行化工具/推理", v0["stats"].p95, v1["stats"].p95, "低（改并发）", "无状态依赖才可并行", "P0"),
            ("② 两级缓存", v1["stats"].p95, v2["stats"].p95, "中（key/失效）", "脏数据、租户串味", "P0"),
            ("③ 检索换倒排+两阶段", ret["naive"].p95, ret["two_stage"].p95, "中（建索引）", "召回质量需评估", "P0"),
            ("④ 过滤下推到召回", push["bad"].p95, push["good"].p95, "低（改 Query）", "无（顺带修越权）", "P0"),
            ("⑤ 短路提前返回", v2["stats"].p95, v3["stats"].p95, "中（置信度校准）", "阈值错→答错", "P1"),
            ("⑥ 对冲请求", hedge["base"]["stats"].p95, hedge["hedge"]["stats"].p95,
             "低（包一层）", "成本翻倍，必须有开关", "P2"),
        ]
        for name, b, a, cost, risk, pri in steps:
            print(f"  {name:<30} {b - a:>8.0f}ms {cost:<12} {risk:<28} {pri}")
        print()
        checks = [
            ("p95_latency_ms", v0["stats"].p95, v3["stats"].p95, "0.1f", True),
            ("retrieval_p95_ms", ret["naive"].p95, ret["two_stage"].p95, "0.1f", True),
            ("docs_scanned", float(ret["scanned_naive"]), float(ret["rerank_candidates"]), "0.0f", True),
            ("llm_calls_per_request", v0["calls_per_req"], v3["calls_per_req"], "0.2f", True),
            ("cost_usd", v0["usd"], v3["usd"], "0.5f", True),
            ("ttft_p95_ms", stream["blocked_ttft"].p95, stream["stream_ttft"].p95, "0.1f", True),
        ]
        for name, b_, a_, fs, lower in checks:
            print(f"{VERIFY} {name}: {b_:{fs}} -> {a_:{fs}} "
                  f"({improvement(b_, a_, lower_is_better=lower)})")
        hb, ha = hedge["base"]["usd"], hedge["hedge"]["usd"]
        print(f"{VERIFY} hedge_cost_usd: {hb:.5f} -> {ha:.5f} (+{(ha / hb - 1) * 100:.1f}%)"
              f"  # direction: increase-expected （对冲是用钱买尾部延迟，成本上升就是结论）")
        note("注：hedge_cost_usd 是唯一一条「变贵」的验证行 —— 对冲是用钱买尾部，不是免费优化。")

        head("4. 工程结论")
        note("1) 先归因再优化：Tracer 的 stage P95 + critical_path 告诉你该动谁，猜一定会猜错。")
        note("2) 并行化是 ROI 最高的一步：把「串行之和」变成「最慢的那个」，且几乎零成本。")
        note("3) 检索慢的根因是「候选太多」，不是「库太慢」：倒排召回 + 小批量精排。")
        note("4) 权限过滤必须在召回阶段做：后置过滤更慢（精排按候选数计费），还会把越权文档带进提示词。")
        note("5) 短路/对冲/流式都是「换」不是「省」：短路拿准确率换延迟，对冲拿钱换尾部，")
        note("   流式不改变端到端 RT，只改用户感知的 TTFT —— 两个指标要分开定 SLO。")
        takeaway("P95 优化 = 先按 span 归因找到尾巴，再按「并行化 → 减少候选 → 缓存 → 短路 → "
                 "对冲/流式」的顺序逐步逼近，每一步都要重新测量并记账。")
        METRICS.reset()
    return 0


QUESTIONS = [
    "长链路 agent（检索 + 多轮推理）如何把 P95 的 RT 降下来？ -> 先 span 归因定位尾部，"
    "再并行化 / 减少候选 / 缓存 / 短路 / 对冲 / 流式，逐步测量（attribute first, then "
    "parallelize, prune candidates, cache, short-circuit, hedge, stream）",
    "大量检索的 RT 越来越高，百万级知识库如何优化？ -> 全量扫描换倒排索引 + 两阶段召回精排，"
    "并用权限过滤下推减少候选（inverted index + two-stage recall/rerank + filter pushdown）",
    "权限/元数据过滤放在排序前还是排序后？ -> 必须下推到召回，后置过滤更慢且会泄露/塌陷召回",
    "优化顺序怎么定？ -> 按「对 P95 的贡献 / 实现成本 / 风险」排序：并行化与过滤下推 P0，"
    "对冲最后（用钱买尾部，必须记账）",
    "TTFT 和端到端 RT 是一回事吗？ -> 不是；分块流式不改端到端，但显著降低用户感知的 TTFT",
]


if __name__ == "__main__":
    sys.exit(main())
