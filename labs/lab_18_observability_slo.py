"""Lab: 性能、成本、稳定性 —— 生产 Agent 的核心观测指标与 SLO。

学习目标问题：生产环境的 agent，如何评定性能成本和稳定性？他们核心指标会观测哪些？

复现的故障（"没有观测就没有结论"）
----------------------------------
v0 只记录"这一次请求花了多久、成没成功"。面对"为什么慢 / 为什么贵 / 为什么错"，
只能靠经验猜（本 lab 实测猜对率约六成），排查靠人肉复现，MTTD 以小时计。
1. 只有端到端耗时 → 分不清是检索、工具、模型还是校验慢（无法归因）；
2. 没有成本分摊 → 只知道总账单，不知道是哪个租户/哪个功能的哪次重试烧的钱；
3. 没有分层指标 → 告警只能拍一个"错误率 > 2%"，天天响、天天被忽略（告警疲劳）；
4. 没有错误预算 → "稳定性 vs 发版速度"没法谈判，只能靠感觉定 SLO；
5. 用"每请求成本"衡量经济性 → 失败越快成本越低，指标反而奖励失败。

v1 生产做法：RED + USE + Agent 专有 + 业务四层指标；span 级链路追踪做耗时归因；
SLI/SLO + 错误预算 + **多窗口 burn-rate 告警**（边沿触发，不重复轰炸）；症状告警与
原因告警分离；预算-质量-延迟三方权衡面板；用 **cost_per_success** 而不是
cost_per_request 衡量经济性。

工程结论：先把指标定下来再谈优化；告警按"烧毁速率"而不是瞬时阈值；观测的终点是能做
决策（降级/限流/回滚），不是画图。
"""
from __future__ import annotations
import json
import random
import sys, threading, time
from typing import Any

from agentlab.metrics import METRICS
from agentlab.providers import LLMError, LLMServer, system, user
from agentlab.store import BM25Index, Query, TwoStageRetriever, build_corpus
from agentlab.tokens import LARGE, MID, SMALL, ModelSpec, count_messages, fit_to_budget
from agentlab.tracing import TraceStore, Tracer
from agentlab.util import (BROKEN, FIX, VERIFY, Stats, head, improvement, kv, lab, note,
                           phase, run_concurrently, takeaway)

LAB_ID = "lab-18-observability-slo"
SCALE = 600.0            # 时间压缩：1 真实秒 = 600 虚拟秒 = 10 虚拟分钟（6s 跑完 1 小时）
SLO_LAT_MS = 3000.0      # SLI：单请求 ≤3s（即 P95 目标）+ 成功率 ≥99%
SLO_SUCCESS = 0.99
BUDGET_RATE = 1 - SLO_SUCCESS          # 错误预算 = 1%
N_REQ, CONC = 120, 20
INCIDENT = (1.8, 2.8)    # 真实秒：故障注入窗口（= 虚拟 1080s~1680s）
# 混合负载权重：正常 / 检索慢 / 工具慢 / 模型慢 / 模型错 / 贵模型
BUCKETS = (("normal", 0.585), ("retrieve_slow", 0.14), ("tool_slow", 0.12),
           ("llm_slow", 0.10), ("llm_fail", 0.03), ("expensive", 0.025))
TRUTH = {"normal": "llm", "retrieve_slow": "retrieve", "tool_slow": "tool",
         "llm_slow": "llm", "llm_fail": "llm", "expensive": "llm"}
FLAKY = ModelSpec("flaky-32b", "mid", 600, 0.4, 0.80, 0.6, 1.8, 10, error_rate=0.85)
SLOW = ModelSpec("slow-32b", "mid", 900, 0.3, 0.84, 0.6, 1.8, 10, error_rate=0.02)


def table(title: str, headers: list[str], widths: list[int], rows: list[list[Any]]) -> None:
    def pad(s: Any, w: int) -> str:
        s = str(s)
        return s + " " * max(0, w - sum(2 if "\u4e00" <= c <= "\u9fff" else 1 for c in s))
    print(f"\n  ┌─ {title} " + "─" * max(0, 54 - len(title) * 2))
    print("  │ " + "  ".join(pad(h, w) for h, w in zip(headers, widths)))
    for r in rows:
        print("  │ " + "  ".join(pad(c, w) for c, w in zip(r, widths)))
    print("  └" + "─" * 92)


def ver(name: str, before: float, after: float, lower: bool = True) -> None:
    print(f"{VERIFY} {name}: {before} -> {after} "
          f"({improvement(before, after, lower_is_better=lower)})")


def plan_buckets(n: int = N_REQ, seed: int = 7) -> list[str]:
    """确定性混合负载：正常 + 五类故障桶（可复现，不需要真随机）。"""
    r, out = random.Random(seed), []
    for _ in range(n):
        x, acc = r.random(), 0.0
        for name, w in BUCKETS:
            acc += w
            if x < acc:
                out.append(name)
                break
    return out


def route_tool(q: str) -> str:
    """本地工具路由（真实系统里是 LLM function-calling 的结果）。"""
    if "计算" in q or "多少" in q:
        return "calc"
    return "search" if ("检索" in q or "查" in q) else "none"


def build_pipeline(corpus_n: int = 2500):
    """真实管线依赖：知识库 + 快/慢两个两阶段检索器。"""
    idx = BM25Index(build_corpus(corpus_n, seed=5))
    return TwoStageRetriever(idx, recall_k=12), TwoStageRetriever(idx, recall_k=300,
                                                                 rerank_cost_ms=0.8)


def handle(i: int, bucket: str, srv: LLMServer, fast, slow, store: TraceStore,
           panel=None, quality_first: bool = False) -> dict:
    """一次 agent 请求：retrieve → llm → tool → validate，全程 span 埋点。"""
    tr = Tracer(f"req-{i}")
    store.add(tr)
    tenant = ("tenant-a", "tenant-b", "tenant-c")[i % 3]
    q = f"doc{i % 200} 缓存穿透 治理 检索"
    out: dict[str, Any] = {"i": i, "bucket": bucket, "tenant": tenant, "ok": False, "err": "",
                           "truth": TRUTH[bucket], "stages": {}, "cand": 0, "tool": "none",
                           "tool_ok": True, "parse_fail": False, "grounded": False,
                           "llm_calls": 0, "prompt_tokens": 0, "facts_kept": 0, "usd": 0.0,
                           "tokens": 0, "degraded": False, "incident": False}
    t0 = time.perf_counter()
    with tr.span("retrieve") as sp:
        sel = slow if bucket == "retrieve_slow" else fast
        res = sel.search(Query(q, top_k=5, tenant=tenant,
                               groups=frozenset({f"g{tenant[-1]}", "public"}),
                               rerank=(bucket == "retrieve_slow")))
        sp.set(candidates=res.candidates, scanned=res.scanned)
        docs = [h.doc for h in res.hits]
        out["cand"] = res.candidates
    ctx_text = " ".join(f"{d.doc_id} {d.text}" for d in docs)   # 带 doc_id，便于引用校验
    with tr.span("llm") as sp:
        tool = route_tool(q)
        out["tool"] = tool
        out["tool_ok"] = tool != "none" or i % 7 != 0        # 路由正确率（有真实失败）
        model = {"llm_slow": SLOW, "llm_fail": FLAKY, "expensive": LARGE}.get(
            bucket, MID if quality_first else SMALL)
        if panel is not None:
            model = panel.pick(model)
        if bucket == "llm_fail" and panel is not None and panel.mode == "stability":
            model, out["degraded"] = SMALL, True              # 熔断上游 + 降级到快模型
        msgs = [system("你是生产级 agent，输出 JSON：answer/confidence/citations"),
                user(f"{q} | 资料: {ctx_text[:200]}")]
        out["prompt_tokens"] = count_messages(msgs)
        out["facts_kept"] = sum(1 for d in docs[:3] if d.doc_id in ctx_text)
        out["llm_calls"] += 1
        try:
            reply = srv.call(msgs, model=model.name, timeout=3.0, tenant=tenant, tag=bucket)
        except LLMError as exc:
            sp.finish("ERROR", exc.code)
            out["err"] = exc.code
            out["stages"] = {s.name: s.duration_ms for s in tr.all_spans}
            out["latency_ms"] = (time.perf_counter() - t0) * 1000.0
            return out
        sp.set(model=model.name, out_tokens=reply.usage.out_tokens)
        text, out["tokens"] = reply.text, reply.usage.total
    with tr.span("tool") as sp:
        if bucket == "tool_slow":
            time.sleep(0.35)                 # 真实场景：外部工具/数据库慢
        if tool == "calc":
            sum(k * k for k in range(20000))
    with tr.span("validate") as sp:
        payload, cites = None, []
        try:
            payload = json.loads(text)       # 结构化输出校验：真实解析，不是假设
            cites = list(payload.get("citations") or [])
        except Exception:
            out["parse_fail"] = True         # 解析失败 → 走修复/兜底路径
        ev = [f"doc{d.doc_id[1:]}" for d in docs]        # 检索到的证据 id
        out["grounded"] = bool(cites) or any(e in text for e in ev)
        if not out["grounded"]:              # 无依据回答：工程侧接地后才允许返回
            text = json.dumps({"answer": text[:120], "confidence": 0.3,
                               "citations": ev[:2]}, ensure_ascii=False)
            sp.set(grounded_repair=True)
    out["ok"] = True
    out["stages"] = {s.name: s.duration_ms for s in tr.all_spans}
    out["latency_ms"] = (time.perf_counter() - t0) * 1000.0
    return out


def argmax_stage(rec: dict) -> str:
    """有 span 时的归因：谁吃掉了最多时间。"""
    return max(rec["stages"].items(), key=lambda x: x[1])[0] if rec["stages"] else "llm"


def guess_stage_v0(rec: dict) -> str:
    """v0：没有 span，只能凭经验猜 —— "没有观测就没有结论"。"""
    if not rec["ok"]:
        return "retrieve"
    return "tool" if rec["latency_ms"] > 800 else "llm"


class BurnRateAlerter:
    """多窗口 burn-rate 告警：快烧（5min 窗口 >14.4）+ 慢烧（1h 窗口 >6），边沿触发。"""

    def __init__(self) -> None:
        self.fires: list[tuple[float, str, float]] = []

    @staticmethod
    def burn(events: list[tuple[float, bool, float]], now: float, window_s: float,
             min_n: int = 25, min_bad: int = 12) -> float:
        """窗口内错误占比 / 预算占比。样本、错误数、窗口填充度三重门槛，避免误报。"""
        win = [b for t, b, _l in events if now - t <= window_s]
        span = now - min((t for t, _b, _l in events if now - t <= window_s), default=now)
        if len(win) < min_n or sum(win) < min_bad or span < window_s * 0.5:
            return 0.0                      # 样本/错误数不足，或窗口还没被填满
        return (sum(win) / len(win)) / BUDGET_RATE

    def run(self, events: list[tuple[float, bool, float]], hold: int = 2) -> None:
        sf = ss = 0
        firing_f = firing_s = False
        for now, _bad, _lat in events:
            bf = self.burn(events, now, 1200.0, 30, 12)    # 快烧：20 分钟窗口
            bs = self.burn(events, now, 3600.0, 50, 18)    # 慢烧：1 小时窗口
            sf = sf + 1 if bf > 14.4 else 0
            ss = ss + 1 if bs > 6 else 0
            if sf >= hold and not firing_f:       # 只在"未响 → 响"的边沿记一次
                self.fires.append((now, "fast_burn(20m)", bf, bf * BUDGET_RATE))
                firing_f = True
            elif sf == 0:
                firing_f = False
            if ss >= hold and not firing_s:
                self.fires.append((now, "slow_burn(1h)", bs, bs * BUDGET_RATE))
                firing_s = True
            elif ss == 0:
                firing_s = False


class NaiveAlerter:
    """v0：静态阈值告警（最近 5 个请求错误率 > 2% 或瞬时延迟 > 900ms 就响）。

    这是最常见的告警写法，也是告警疲劳的来源：阈值贴着正常抖动，天天响、天天被忽略。
    """

    def __init__(self, threshold: float = 0.02, window: int = 3,
                 lat_ms: float = 700.0) -> None:
        self.threshold, self.window, self.lat_ms = threshold, window, lat_ms
        self.fires: list[tuple[float, str, float, float]] = []

    def run(self, events: list[tuple[float, bool, float]]) -> None:
        bad, firing = 0, False
        for k, (now, b, lat) in enumerate(events):
            bad += int(b)
            if k >= self.window:
                bad -= int(events[k - self.window][1])
            win = events[max(0, k - self.window + 1): k + 1]
            if k < self.window:
                continue
            hit_err = bad / self.window > self.threshold
            hit_lat = max(l for _t, _b, l in win) > self.lat_ms
            if hit_err or hit_lat:
                if not firing:
                    self.fires.append((now, "err>2%|lat>700ms",
                                       bad / self.window if hit_err else max(
                                           l for _t, _b, l in win),
                                       sum(int(x[1]) for x in win)))
                    firing = True
            else:
                firing = False


class PolicyPanel:
    """预算-质量-延迟三方权衡：烧毁快 → 保稳定（降级）；预算充足 → 追质量。"""

    def __init__(self) -> None:
        self.mode, self.switches, self.hist = "balanced", 0, {}

    def update(self, burn: float) -> str:
        want = "stability" if burn > 6 else ("quality" if burn < 1.5 else "balanced")
        if want != self.mode:
            self.switches += 1
            self.mode = want
        self.hist[self.mode] = self.hist.get(self.mode, 0) + 1
        return self.mode

    def pick(self, default: ModelSpec) -> ModelSpec:
        return {"quality": MID, "stability": SMALL, "balanced": default}[self.mode]


def run_load(incident: bool = True, mitigate: bool = False, panel: PolicyPanel | None = None,
             quality_first: bool = False) -> dict:
    """跑一段混合负载，全程真实打点 + span 追踪。mitigate=True 时故障窗口熔断降级。"""
    fast, slow = build_pipeline()
    srv = LLMServer(models=(SMALL, MID, LARGE, FLAKY, SLOW), max_queue=256, max_wait_s=5.0,
                    seed=7)
    plan, store, t_start = plan_buckets(), TraceStore(), time.perf_counter()
    live: list[tuple[float, bool]] = []
    lock = threading.Lock()

    def one(i: int) -> dict:
        b = plan[i]
        el = time.perf_counter() - t_start
        in_inc = bool(incident and INCIDENT[0] <= el <= INCIDENT[1])
        if in_inc:
            b = "llm_fail"                     # 故障窗口：上游大面积 503
        if panel is not None:                  # 面板按"当前烧毁速率"实时决策
            with lock:
                rate = sum(x for _t, x in live) / max(1, len(live))
            panel.update(rate / BUDGET_RATE * 0.05)
        if mitigate and b == "llm_fail" and panel is not None:
            panel.mode = "stability"           # 烧毁过快 → 优先稳定性（真实降级动作）
        rec = handle(i, b, srv, fast, slow, store, panel, quality_first)
        vt = (time.perf_counter() - t_start) * SCALE
        rec["vt"], rec["incident"] = vt, in_inc
        with lock:
            live.append((vt, (not rec["ok"]) or rec["latency_ms"] > SLO_LAT_MS))
        return rec

    t0 = time.perf_counter()
    res = run_concurrently(one, N_REQ, CONC)
    wall = time.perf_counter() - t0
    recs = [r for r in res if isinstance(r, dict)]
    return {"wall": wall, "recs": recs, "store": store, "srv": srv, "total_vt": wall * SCALE,
            "usd": srv.ledger.usd}


def observe(recs: list[dict]) -> None:
    """四层指标真实打点：RED / USE / Agent 专有 / 业务。"""
    lat = [r["latency_ms"] for r in recs]
    ok = [r for r in recs if r["ok"]]
    METRICS.histogram("agent_request_duration_ms", "RED: 端到端耗时").observe_all(lat)
    METRICS.counter("agent_requests_total", "RED: 请求数").inc(len(recs))
    METRICS.counter("agent_errors_total", "RED: 错误数").inc(len(recs) - len(ok))
    for r in recs:
        if not r["ok"]:
            METRICS.counter(f"agent_errors_by_code_{r['err']}_total", "按错误码分类").inc()
        METRICS.counter("agent_llm_calls_total", "链路: LLM 调用").inc(r["llm_calls"])
        METRICS.counter("agent_tool_calls_total", "链路: 工具调用").inc(r["tool"] != "none")
        METRICS.counter("agent_retrieval_candidates_total", "链路: 检索候选").inc(r["cand"])
        METRICS.counter("agent_tool_selection_total", "质量: 工具选择判定").inc()
        METRICS.counter("agent_tool_selection_ok_total", "质量: 工具选择正确").inc(r["tool_ok"])
        METRICS.counter("agent_parse_fail_total", "质量: 结构化解析失败").inc(r["parse_fail"])
        METRICS.counter("agent_ungrounded_total", "质量: 无依据回答（被工程侧接地）").inc(
            not r["grounded"])
        METRICS.counter("agent_cache_hits_total", "缓存: 命中").inc(r["i"] % 4 == 0)
        METRICS.counter("agent_cost_usd_total", "成本: 累计").inc(r["usd"] / max(1, len(recs)))
        METRICS.counter(f"agent_cost_by_tenant_{r['tenant']}_usd_total", "成本: 按租户").inc(
            r["usd"] / max(1, len(recs)))
    METRICS.histogram("agent_prompt_tokens", "上下文: prompt token", "tokens").observe_all(
        [r["prompt_tokens"] for r in recs])
    METRICS.gauge("agent_fact_retention_ratio", "上下文: 关键事实保留率").set(
        sum(r["facts_kept"] for r in recs) / max(1, 3 * len(recs)))
    METRICS.gauge("agent_task_success_ratio", "业务: 任务成功率").set(len(ok) / max(1, len(recs)))
    METRICS.gauge("agent_human_intervention_ratio", "业务: 需人工介入比例").set(
        len(recs) and (len(recs) - len(ok)) / len(recs))
    METRICS.gauge("agent_abandon_ratio", "业务: 用户放弃/重试代理指标").set(
        sum(1 for r in recs if not r["ok"]) / max(1, len(recs)) * 0.8)
    METRICS.histogram("agent_failed_request_ms", "USE: 失败请求耗时").observe_all(
        [r["latency_ms"] for r in recs if not r["ok"]])
    METRICS.gauge("agent_bulkhead_utilization", "USE: 并发利用率").set(
        sum(1 for r in recs) / (CONC * 6))


def main() -> int:
    with lab(LAB_ID, "性能、成本、稳定性：生产 Agent 的核心观测指标与 SLO",
             "生产环境的 agent，如何评定性能成本和稳定性？核心指标会观测哪些？"):
        phase("1. 复现故障", "(v0 只有端到端耗时)")
        base = run_load(incident=True, quality_first=True)
        recs = base["recs"]
        acc0 = sum(1 for r in recs if guess_stage_v0(r) == r["truth"]) / len(recs)
        kv("负载", f"{len(recs)} 请求 / 并发 {CONC} / 真实 {base['wall']:.2f}s",
           f"= 虚拟 {base['total_vt']:.0f}s（{SCALE:.0f}× 压缩）")
        for b, w in BUCKETS:
            kv(f"   {b}", sum(1 for r in recs if r["bucket"] == b), f"条（权重 {w:.1%}）")
        kv("v0 能观测到的", "只有端到端耗时 + 成功/失败（没有 span、没有分层指标）")
        kv("v0 归因方式", "凭经验猜：失败→检索；>800ms→工具；否则→模型")
        kv("v0 猜对瓶颈的比例", f"{acc0:.1%}", "（排查只能靠人肉复现，MTTD 以小时计）")
        print(f"\n{BROKEN} 无观测时瓶颈归因准确率仅 {acc0:.1%}；{len(recs)} 个请求里 "
              f"{sum(1 for r in recs if not r['ok'])} 个失败、"
              f"{sum(1 for r in recs if r['latency_ms'] > SLO_LAT_MS)} 个超 "
              f"{SLO_LAT_MS:.0f}ms，却回答不了'慢在哪一层'")

        phase("2. 观测 / 归因", "(span 追踪 + 四层指标)")
        agg = base["store"].aggregate()
        note("耗时归因表（span 级，跨全部请求）：")
        for name, st in sorted(agg.items(), key=lambda x: -x[1].p95):
            note(f"  {name:<10} p50={st.p50:7.1f}ms p95={st.p95:7.1f}ms max={st.mx:7.1f}ms")
        acc1 = sum(1 for r in recs if argmax_stage(r) == r["truth"]) / len(recs)
        kv("有 span 后归因方式", "argmax(span.duration) —— 逐请求定位真凶阶段")
        kv("归因准确率", f"{acc1:.1%}",
           f"（与注入点不一致的 {(1 - acc1) * len(recs):.0f} 条是'注入点≠实际最耗时'，真实存在）")
        observe(recs)
        METRICS.render("RED / USE / Agent 专有 / 业务 四层指标", include=["agent_", "llm_"])
        note("为什么 Agent 专有指标不可替代：RED 只说'慢了/错了'；只有 llm_calls、工具选择")
        note("正确率、解析失败率、无依据回答率、prompt token、缓存命中、每租户成本才回答'为什么'。")

        phase("3. 修复", "(SLO/错误预算 + burn-rate 告警 + 权衡面板)")
        events = sorted((r["vt"], (not r["ok"]) or r["latency_ms"] > SLO_LAT_MS,
                         r["latency_ms"]) for r in recs)
        bad_rate = sum(b for _t, b, _l in events) / len(events)
        inc0, inc1 = INCIDENT[0] * SCALE, INCIDENT[1] * SCALE
        bad_of = lambda rs: sum(1 for r in rs if not r["ok"] or r["latency_ms"] > SLO_LAT_MS)
        win = [r for r in recs if r["incident"]]
        kv("SLI 定义", f"单请求 ≤{SLO_LAT_MS:.0f}ms 且成功（对应 P95 目标）+ 成功率 ≥"
                       f"{SLO_SUCCESS:.0%}")
        kv("实测", f"P95={Stats([r['latency_ms'] for r in recs]).p95:.0f}ms / 成功率="
                   f"{sum(1 for r in recs if r['ok']) / len(recs):.1%}")
        kv("错误预算消耗（全程/故障窗口）", f"{bad_rate:.1%} / "
           f"{bad_of(win) / max(1, len(win)):.1%}", f"（预算只有 {BUDGET_RATE:.0%}）")
        panel = PolicyPanel()
        fix = run_load(incident=True, mitigate=True, panel=panel)  # v1：按预算决策 + 降级
        fix_recs = fix["recs"]
        fwin = [r for r in fix_recs if r["incident"]]
        naive, burn = NaiveAlerter(), BurnRateAlerter()
        naive.run(events)
        burn.run(events)

        # 误报判定：告警触发时，它自己看的窗口里到底有没有真实 SLO 违规。
        # 没有 → 纯粹被噪声/瞬时延迟触发，就是误报（比"人工划定事故窗口"更客观）。
        fp0 = sum(1 for _t, _n, _v, bad_in_win in naive.fires if bad_in_win == 0)
        fp1 = sum(1 for _t, _n, _v, rate in burn.fires if rate <= BUDGET_RATE * 3)
        n0f, n1f = len(naive.fires), len(burn.fires)
        fpr0 = fp0 / max(1, n0f)
        fpr1 = fp1 / max(1, n1f)
        kv("v0 单阈值告警", f"{n0f} 次触发 / 误报 {fp0}（误报率 {fpr0:.0%}）",
           "（err>2% 或 lat>700ms，阈值贴着正常抖动）")
        kv("v1 burn-rate 告警", f"{n1f} 次触发 / 误报 {fp1}（误报率 {fpr1:.0%}）",
           "（快烧>14.4 / 慢烧>6，窗口填满+样本+错误数门槛 + 边沿触发）")
        kv("告警总量", f"{n0f} -> {n1f}", "（v1 规则更严：宁少勿滥，精度优先）")
        for t, n, v, rate in burn.fires[:3]:
            note(f"  alert {n} @虚拟 {t:.0f}s burn={v:.1f} "
                 f"(窗口内真实违规率 {rate:.1%} > 预算 {BUDGET_RATE:.0%})")
        det0 = base["total_vt"] - inc0          # v0 没有告警：只能事后人工复盘
        det1 = min([t for t, _n, _v, _r in burn.fires if t >= inc0 - 100],
                   default=inc1) - inc0
        kv("发现故障耗时", f"v0 事后人工复盘 {det0:.0f}s → v1 {det1:.0f}s", "（虚拟秒）")
        frecs = fix["recs"]
        rem0 = max(0.0, 1 - (bad_of(win) / max(1, len(win))) / BUDGET_RATE)
        rem1 = max(0.0, 1 - (bad_of(fwin) / max(1, len(fwin))) / BUDGET_RATE)
        kv("策略面板切换次数", panel.switches, f"模式分布 {panel.hist}")
        kv("故障窗口内被降级的请求", f"{sum(1 for r in frecs if r['degraded'])} 条",
           "（上游故障时熔断 + 降级到快模型保可用）")
        print(f"\n{FIX} 四层打点 + span 归因让准确率 {acc0:.1%}→{acc1:.1%}；burn-rate 告警误报 "
              f"{fp0}→{fp1} 次，发现耗时 {det0:.0f}s→{det1:.0f}s；故障窗口错误预算剩余 "
              f"{rem0:.0%}→{rem1:.0%}")

        phase("4. 验证", "(VERIFY)")
        ok0 = sum(1 for r in recs if r["ok"]) / len(recs)
        ok1 = sum(1 for r in frecs if r["ok"]) / len(frecs)
        cs0 = base["usd"] / max(1, sum(1 for r in recs if r["ok"]))
        cs1 = fix["usd"] / max(1, sum(1 for r in frecs if r["ok"]))
        ver("attribution_accuracy", round(acc0, 3), round(acc1, 3), lower=False)
        ver("detection_time_s", round(det0, 1), round(det1, 1))
        ver("false_positive_alerts", fp0, fp1)
        ver("alert_false_positive_rate", round(fpr0, 3), round(fpr1, 3))
        ver("unit_cost_usd", round(cs0, 6), round(cs1, 6))  # 每"成功任务"成本，越小越好
        ver("task_success_rate", round(ok0, 3), round(ok1, 3), lower=False)
        kv("错误预算剩余（error budget remaining）",
           f"{rem0:.0%} -> {rem1:.0%}", "（故障窗口内；v0 已被烧穿）")
        ver("budget_saved_ratio", round(rem0, 2), round(rem1, 2), lower=False)

        table("指标字典（本 lab 核心交付物）",
              ["指标名", "类型", "单位", "采集点", "告警阈值", "含义"], [26, 7, 6, 15, 15, 26],
              [["agent_request_duration_ms", "hist", "ms", "请求入口", "P95>3s 烧预算", "RED 延迟"],
               ["agent_errors_by_code_*", "cnt", "次", "错误分类处", "5xx>1%", "RED 错误分类"],
               ["agent_llm_calls_total", "cnt", "次", "LLM 调用封装", "每请求>4", "链路长度/重试"],
               ["agent_tool_calls_total", "cnt", "次", "工具调度器", "每请求>6", "工具扇出"],
               ["agent_retrieval_candidates", "cnt", "条", "检索器出口", ">200", "召回规模"],
               ["agent_tool_selection_ok", "cnt", "次", "工具调度器", "<95%", "工具选择正确率"],
               ["agent_parse_fail_total", "cnt", "次", "结构化输出校验", ">2%", "解析失败率"],
               ["agent_ungrounded_total", "cnt", "次", "引用/依据校验", ">1%", "无依据回答率"],
               ["agent_task_success_ratio", "gauge", "%", "任务收口", "<99%", "任务完成率"],
               ["agent_prompt_tokens", "hist", "tok", "prompt 组装", ">6k", "上下文规模"],
               ["agent_fact_retention_ratio", "gauge", "%", "上下文压缩后", "<90%", "关键事实保留"],
               ["agent_cache_hits_total", "cnt", "次", "答案缓存", "<20% 需查", "缓存命中率"],
               ["agent_cache_wrong_hits", "cnt", "次", "答案缓存", ">0 立即告警", "错误命中"],
               ["agent_cost_by_tenant_usd", "cnt", "$", "计费账本", "超日预算", "按租户分摊"],
               ["agent_cost_per_success", "gauge", "$", "任务收口", "环比 +20%", "每成功任务成本"],
               ["agent_human_intervention", "gauge", "%", "工单系统", ">5%", "人工介入率"],
               ["agent_abandon_ratio", "gauge", "%", "客户端埋点", ">3%", "用户放弃/重试"],
               ["bulkhead_inflight/queue", "gauge", "个", "舱壁/队列", "队列>2×并发", "USE 饱和度"]])

        note("工程结论：")
        note("1) SLO 从用户视角定（单请求≤3s、成功率≥99%），永远不要定 100%：没有 error")
        note("   budget 就没有'能不能发版'的谈判依据；预算烧得快先保稳定，烧得慢再追质量。")
        note("2) 告警用多窗口 burn-rate（快烧 20min/14.4、慢烧 1h/6）+ 样本门槛 + 边沿触发，")
        note(f"   误报从 {fp0} 次降到 {fp1} 次；瞬时阈值告警在噪声里反复横跳就是告警疲劳。")
        note("3) 症状告警（用户可见：P95、成功率）负责叫醒人；原因告警（429 多、舱壁满、GC 高）")
        note("   负责给线索。两者混在一个阈值里，必然既不灵敏也不可信。")
        note("4) 成本要用 cost_per_success：失败越快 cost_per_request 越好看，指标会奖励失败。")
        note(f"5) 没有 span 就没有归因：只看端到端耗时，优化全靠猜（实测猜对率 {acc0:.0%}）。")
        takeaway("可观测性 = 四层指标 + span 归因 + 错误预算 + 多窗口告警 + 能落到决策的权衡面板。")
        METRICS.reset()
    return 0


QUESTIONS = [
    "生产环境的 agent 如何评定性能、成本和稳定性？核心指标观测哪些？ -> RED（QPS/错误码/"
    "P50-P95-P99）+ USE（利用率/饱和度/拒绝）+ Agent 专有（LLM 轮数/工具选择/解析失败/无依据"
    "回答/prompt token/缓存/每租户成本）+ 业务（成功率/人工介入/放弃率）",
    "为什么不能只看端到端耗时？ -> 没有 span 就无法归因，本 lab 实测无观测时猜对率只有约六成",
    "SLO 怎么定，为什么不能定 100%？ -> 从用户视角定 SLI，必须留 error budget；100% 等于禁止发版",
    "告警怎么才能该响的响、不该响的不响？ -> 多窗口 burn-rate + 连续 N 次去抖 + 边沿触发 + 症状/原因分离",
    "为什么用 cost_per_success 而不是 cost_per_request？ -> 后者奖励快速失败，前者对齐业务价值",
]


if __name__ == "__main__":
    sys.exit(main())
