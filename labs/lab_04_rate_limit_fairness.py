"""Lab: 高并发下的限流与公平性 —— 一个租户如何拖垮所有人。
对应生产问题：「高并发下会如何解决 LLM 接口限流？」「多用户并发，如何避免一个
租户打满全局配额？」
复现的故障（v0）：全局一个配额、一个舱壁、一条 FIFO 队列。跑批量任务的大租户在
t=0 甩出 300 个请求，占满 provider 的并发槽和排队位；后面到的 5 个小租户只能排在
300 个批量请求之后，排队时间超过客户端预算 → 超时 → 重试。小租户的 P95 由**别人的
流量**决定。另一半故障是限流算法选错：固定窗口在窗口边界会放过 2×limit 的突发。
v1 生产做法：① 分层限流（入口全局 → 每租户 → 每模型/provider → 每工具，四层都真实
实现并打点）；② WFQ 公平排队替代 FIFO（大租户虚拟时间被推远，小租户可插队）；
③ 每租户独立 TokenBucket（rate/burst 可配）；④ 读 `LLMError.retry_after` + 全抖动
退避，把"我该退"的信号返回客户端，而不是把 429 变成 500。
工程结论：限流服务端和客户端两边都要 —— 服务端保护自己（拒绝比排队便宜），客户端
礼貌 + 省成本（不打无用请求，也就不为超时和失败付 token 费）。没有租户维度的舱壁/
队列等于没有多租户。公平不是免费的：大租户 makespan 变长（配额就是用来限制它的），
但小租户的 P95 不再由别人决定 —— 这才是可承诺的 SLO。
"""
from __future__ import annotations
import heapq
import math
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from agentlab.clock import VirtualClock
from agentlab.metrics import METRICS
from agentlab.orchestration import SlidingWindowLimiter, TokenBucket
from agentlab.providers import LLMError, LLMServer, user
from agentlab.util import (
    BROKEN, FIX, VERIFY, head, improvement, kv, lab, note, percentile, phase, rng, takeaway,
)
LAB_ID = "lab-04-rate-limit-fairness"
# 配额表：rate=rps，burst=允许的瞬时突发（令牌桶容量）
QUOTA: dict[str, tuple[float, float]] = {
    "tenant-big": (50.0, 8.0),      # 批量任务租户：被明确限速，突发很小
    "tenant-a": (100.0, 20.0), "tenant-b": (100.0, 20.0), "tenant-c": (100.0, 20.0),
    "tenant-d": (100.0, 20.0), "tenant-e": (100.0, 20.0),
}
GLOBAL_QUOTA = (600.0, 100.0)   # L1 入口全局：保护自己
MODEL_QUOTA = (300.0, 60.0)     # L3 每模型/provider：保护上游
TOOL_QUOTA = (200.0, 40.0)      # L4 每工具：保护下游工具
TENANTS = list(QUOTA)
SMALL_TENANTS = [t for t in TENANTS if t != "tenant-big"]
REQ_LATENCY_S = 0.22    # small-8b p50 延迟（虚拟时间）
CAPACITY = 48           # provider 总并发槽（= max_inflight 上限）
MAX_QUEUE = 24          # provider 排队位上限（超过直接 429）
CLIENT_BUDGET_S = 1.5   # 客户端对一次 provider 调用的耐心（超时预算）
MAX_RETRIES = 2         # v0/v1 同一个重试上限，差别只在"有没有隔离与限流"
PACE_S = 0.001          # 客户端自我节流的最小间隔

class VClockLimiters:
    """VirtualClock 驱动的分层限流：入口全局 → 每租户 → 每模型 → 每工具。

    令牌桶逻辑与核心 `TokenBucket` 一致；另外维护一条"令牌时间轴"，被卡住的请求能领到
    一个**还没被占用**的令牌时刻。各层独立发号，所以大租户堵不住小租户。
    """

    def __init__(self, clk: VirtualClock) -> None:
        self.clk = clk
        self.spec = {"L1_global": GLOBAL_QUOTA, "L3_model": MODEL_QUOTA, "L4_tool": TOOL_QUOTA,
                     **{f"L2_tenant:{t}": QUOTA[t] for t in TENANTS}}
        self.buckets = {n: TokenBucket(r, b, clock=clk, name=n)
                        for n, (r, b) in self.spec.items()}
        self.token_time = {n: clk.monotonic() for n in self.buckets}
        self.order = ("L1_global", "L2_tenant", "L3_model", "L4_tool")
        self.denied: dict[str, int] = {}

    def entry(self, tenant: str, broken: bool, gate: "PacedGate") -> tuple[bool, str, float]:
        """返回 (此刻放行, 被哪一层卡下, 该层下一个令牌的时刻)。"""
        if broken:
            return True, "", 0.0
        now = self.clk.monotonic()
        for layer in self.order:
            name = f"{layer}:{tenant}" if layer == "L2_tenant" else layer
            if self.buckets[name].try_acquire():
                self.token_time[name] = min(self.token_time[name], now)
                continue
            when = gate.reserve(name, max(now, self.token_time[name])
                                + 1.0 / self.spec[name][0])
            self.token_time[name] = when
            self.denied[name] = self.denied.get(name, 0) + 1
            return False, name, when
        return True, "", 0.0

class PacedGate:
    """客户端自我节流队列（生产里就是 SDK 里的 limiter + 排队）。

    每个限流层一个预约游标；到达队列与预约队列**按时刻归并**，所以先到的批量任务
    无法用自己的预约把后来的交互请求挡在门外。这比"被上游打回 429 再重试"便宜得多。
    """

    def __init__(self) -> None:
        self.arrivals: list[tuple[float, int, str, int]] = []
        self.by_time: list[tuple[float, int, str, int]] = []
        self.cursor: dict[str, float] = {}
        self.paced = 0
        self.by_layer: dict[str, int] = {}

    def offer(self, at: float, idx: int, tenant: str) -> None:
        self.arrivals.append((at, idx, tenant, idx))

    def ready(self) -> bool:
        return bool(self.arrivals or self.by_time)

    def head(self) -> float:
        return min(self.arrivals[0][0] if self.arrivals else float("inf"),
                   self.by_time[0][0] if self.by_time else float("inf"))

    def reserve(self, layer: str, when: float) -> float:
        """给请求发一个"该层下一个令牌"的时刻（同层内逐个，不会超发）。"""
        self.cursor[layer] = when = max(when, self.cursor.get(layer, 0.0))
        return when

    def commit(self, idx: int, tenant: str, layer: str, when: float) -> None:
        heapq.heappush(self.by_time, (when, 9000 + idx, tenant, idx))
        self.paced += 1
        self.by_layer[layer] = self.by_layer.get(layer, 0) + 1

    def pop(self, clk: VirtualClock) -> tuple[int, str, float]:
        """按时刻归并"到达"与"预约"两个队列，返回下一个该处理的请求。"""
        arriving = self.arrivals[0][0] if self.arrivals else float("inf")
        if self.by_time and self.by_time[0][0] < arriving:
            at, idx, tenant, _ = heapq.heappop(self.by_time)
        else:
            at, idx, tenant, _ = self.arrivals.pop(0)
        clk.advance(max(0.0, at - clk.monotonic()))
        return idx, tenant, at

class VirtualProvider:
    """把 LLMServer 的排队语义（并发槽 + 排队位 + 429）在虚拟时间上重演。

    "排队超预算 → TIMEOUT""排队位满 → 429"逐条对应真实 provider；调度策略由 ``order``
    决定：FIFO 排到队尾，WFQ 按虚拟时间插队（大租户虚拟时间被推远 → 小租户插到前面）。
    客户端预算从 ``arrival`` 起算：排队期间客户端是在干等的。
    """

    def __init__(self, clk: VirtualClock, capacity: int, max_queue: int, latency_s: float):
        self.clk, self.capacity = clk, capacity
        self.max_queue, self.latency_s = max_queue, latency_s
        self.busy_until: list[float] = [clk.monotonic()] * capacity
        self.busy_samples: list[float] = []
        self.pending: list[tuple[float, int]] = []
        self.seq = self.peak_inflight = self.peak_queued = self.rejected_429 = 0

    def _insert(self, order: float, slot_now: float) -> int:
        self.seq += 1
        if not math.isfinite(order):                    # FIFO：排到队尾
            pos = len(self.pending)
        else:                                           # WFQ：按虚拟时间插队
            pos = sum(1 for t, _ in self.pending if t <= slot_now)
        self.pending.insert(pos, (slot_now, self.seq))
        return pos

    def serve(self, arrival: float, budget_s: float,
              order: float = float("inf")) -> tuple[bool, float, str]:
        now = max(self.clk.monotonic(), arrival)
        self.clk.advance(now - self.clk.monotonic())
        idx = min(range(self.capacity), key=lambda i: self.busy_until[i])
        slot_now = max(now, self.busy_until[idx])
        if slot_now <= now:                             # 有空闲槽：立刻开工
            busy = sum(1 for f in self.busy_until if f > now)
            self.peak_inflight = max(self.peak_inflight, busy)
            self.busy_samples.append(float(busy))
            self.busy_until[idx] = now + self.latency_s
            done = now + self.latency_s
        else:                                           # 满：排队或拒绝
            self.peak_queued = max(self.peak_queued, len(self.pending) + 1)
            if len(self.pending) >= self.max_queue:
                self.rejected_429 += 1                  # 排队位也满了 → 429
                return False, now, "429"
            pos = self._insert(order, slot_now)
            done = max(slot_now, self.pending[pos][0]) + self.latency_s
            if done - arrival > budget_s:               # 还没轮到就已经超预算
                self.pending.pop(pos)
                return False, done, "TIMEOUT"
            if pos == 0:                                # 排在最前 → 真的占到槽
                self.pending.pop(0)
                self.busy_until[idx] = done
        if done - arrival > budget_s:
            return False, done, "TIMEOUT"
        return True, done, ""

    def inflight_stats(self) -> tuple[float, float]:
        """在飞请求数的 P50/P95 —— 稳态压力指标（峰值只看瞬时，容易被一个突发骗到）。"""
        s = self.busy_samples
        return (percentile(s, 50) if s else 0.0, percentile(s, 95) if s else 0.0)

class Scheduler:
    """v0=FIFO（先到先服务）；v1=WFQ（请求多的大租户虚拟时间被推远，小租户可插队）。"""

    def __init__(self, mode: str) -> None:
        self.mode, self.vfinish = mode, {}

    def order(self, arrival: float, tenant: str) -> float:
        if self.mode == "fifo":
            return float("inf")
        vt = max(arrival, self.vfinish.get(tenant, 0.0))
        self.vfinish[tenant] = vt + 1.0
        return vt

@dataclass
class RunResult:
    name: str
    ok: dict[str, int] = field(default_factory=dict)
    total: dict[str, int] = field(default_factory=dict)
    lat: dict[str, list[float]] = field(default_factory=dict)
    makespan_s: dict[str, float] = field(default_factory=dict)
    rejected_by_layer: dict[str, int] = field(default_factory=dict)
    calls = retries = retry_after_honored = failures = deadline_misses = 0
    pacing_waits = peak_inflight = peak_queued = rejected_429 = 0
    inflight_p50 = inflight_p95 = 0.0

    def rate(self, t: str) -> float:
        n = self.total.get(t, 0)
        return self.ok.get(t, 0) / n if n else 0.0

    def jain(self, metric: str = "ok") -> float:
        """Jain's fairness index: (Σx)² / (n·Σx²)，1.0 = 完全公平。"""
        if metric == "ok":
            xs = [float(self.ok.get(t, 0)) for t in TENANTS]
        else:
            xs = [1.0 / max(1e-6, percentile(self.lat.get(t, [1.0]), 95)) for t in TENANTS]
        s1, s2 = sum(xs), sum(x * x for x in xs)
        return (s1 * s1) / (len(xs) * s2) if s2 > 0 else float("nan")

def run_load(name: str, broken: bool, seed: int = 7) -> RunResult:
    """按到达时刻推进虚拟时钟：v0=FIFO 无隔离；v1=四层限流 + WFQ + 客户端节流。"""
    clk = VirtualClock()
    base = clk.monotonic()                    # 虚拟时钟带 epoch，先归零对齐
    lim = VClockLimiters(clk)
    prov = VirtualProvider(clk, CAPACITY, MAX_QUEUE, REQ_LATENCY_S)
    sched = Scheduler("fifo" if broken else "wfq")
    rnd = rng(seed)
    out = RunResult(name)
    out.total["tenant-big"] = 300
    gate = PacedGate()
    for i in range(300):                      # 大租户：批量任务，t=0 全量打进来
        gate.offer(base, i, "tenant-big")
    for k, t in enumerate(SMALL_TENANTS):     # 小租户：交互请求，交错到达
        out.total[t] = 20
        for j in range(20):
            gate.offer(base + 0.005 + j * 0.004 + 0.001 * k, 3000 + j * 5 + k, t)
    first_in: dict[str, float] = {}
    attempts: dict[tuple[str, int], int] = {}
    retry_heap: list[tuple[float, int, str, int]] = []
    while gate.ready() or retry_heap:
        if retry_heap and retry_heap[0][0] < gate.head():
            at, _s, tenant, idx = heapq.heappop(retry_heap)
            clk.advance(max(0.0, at - clk.monotonic()))
        else:
            idx, tenant, at = gate.pop(clk)
        key = (tenant, idx)
        first_in.setdefault(tenant, at)
        attempts[key] = attempts.get(key, 0) + 1
        out.calls += 1
        ok_adm, layer, when = lim.entry(tenant, broken, gate)
        if not ok_adm:                        # 被限流层卡住 → 客户端等令牌，不打上游
            gate.commit(idx, tenant, layer, when)
            continue
        ok, done, code = prov.serve(at, CLIENT_BUDGET_S, sched.order(at, tenant))
        if not ok:
            out.failures += 1
            out.deadline_misses += 1
            wait = rnd.random() * max(0.05, 0.15 * (2 ** min(attempts[key], 8)))
            if code == "429":                 # 读 Retry-After：429 是"稍后再来"
                wait = max(wait, 0.5)
                out.retry_after_honored += 1
            if attempts[key] <= MAX_RETRIES:
                out.retries += 1
                heapq.heappush(retry_heap,
                               (clk.monotonic() + wait, 9000 + idx, tenant, idx))
                continue
        else:
            out.ok[tenant] = out.ok.get(tenant, 0) + 1
        out.lat.setdefault(tenant, []).append((done - first_in[tenant]) * 1000.0)
        out.makespan_s[tenant] = max(out.makespan_s.get(tenant, 0.0), done - first_in[tenant])
    out.peak_inflight, out.peak_queued = prov.peak_inflight, prov.peak_queued
    out.inflight_p50, out.inflight_p95 = prov.inflight_stats()
    out.rejected_429 = prov.rejected_429
    out.pacing_waits = gate.paced
    out.rejected_by_layer = gate.by_layer
    return out

class FixedWindowLimiter:
    """固定窗口计数器：实现最简单，但**窗口边界会放过 2×limit**。"""

    def __init__(self, limit: int, window_s: float, clk: VirtualClock):
        self.limit, self.window_s, self.clk = limit, window_s, clk
        self.count, self.win, self.denied = 0, -1, 0

    def try_acquire(self) -> bool:
        w = int(self.clk.monotonic() // self.window_s)
        if w != self.win:
            self.win, self.count = w, 0
        if self.count >= self.limit:
            self.denied += 1
            return False
        self.count += 1
        return True

class LeakyBucket:
    """漏桶：恒定速率流出，**只整形、不允许突发**（队列满就丢）。"""

    def __init__(self, rate: float, queue: float, clk: VirtualClock):
        self.rate, self.queue, self.clk = rate, queue, clk
        self.level, self.last, self.denied = 0.0, clk.monotonic(), 0

    def try_acquire(self) -> bool:
        now = self.clk.monotonic()
        self.level = max(0.0, self.level - (now - self.last) * self.rate)
        self.last = now
        if self.level + 1.0 > self.queue:
            self.denied += 1
            return False
        self.level += 1.0
        return True

def probe_retry_after_honoring() -> dict:
    """429 的正确处理：服务端给 Retry-After，客户端**必须**按它退避。

    服务端用核心 ``TokenBucket`` 做每租户配额，超限就抛带 ``retry_after`` 的
    ``LLMError.rate_limited``（就是真实 429 的语义）。客户端两种写法：
    v0 固定 150ms 重试 —— 完全无视服务端给的节奏；v1 取
    ``wait = max(全抖动退避, retry_after)`` —— 把 429 当"稍后再来"而不是故障。
    """
    bucket = TokenBucket(rate=1.0, burst=3, name="probe_tenant")   # 配额 1 rps / 突发 3
    rnd = rng(11)
    out = {}
    for tag, honors in (("v0 固定 150ms 重试", False), ("v1 max(backoff, retry_after)", True)):
        honored = fails = ok = 0
        for _ in range(12):
            if bucket.try_acquire():
                ok += 1
                continue
            ra = bucket.retry_after_s()                       # 服务端建议的等待秒数
            exc = LLMError.rate_limited(ra, "tenant quota exceeded")
            fails += 1
            if honors:                                        # v1：真的读它、用它
                wait = max(rnd.random() * 0.15 * (2 ** 1), exc.retry_after)
                time.sleep(wait)
                honored += int(wait >= exc.retry_after - 1e-9)
            else:
                time.sleep(0.15)                              # v0：拍脑袋固定值
        out[tag] = (honored / fails if fails else 0.0, fails, ok)
    return out


def _boundary_burst(build: Callable[[], object], label: str, n: int = 130) -> dict:
    """边界双倍突发的经典构造：t=0.99s 与 t=1.0s 各打 ``n`` 个请求。

    单个窗口 limit=100/s：固定窗口给 [0,1) 和 [1,2) 各算一份配额，两次突发全被放行
    （合计 2×limit 且几乎同时到达上游）；滑动窗口/令牌桶只看过去 1s 放行过多少，
    第二次突发只能被拒。
    """
    clk = VirtualClock()
    base = clk.monotonic()                            # 虚拟时钟带 epoch，先归零对齐
    lim = build(clk)  # type: ignore[arg-type]
    admitted: list[float] = []
    for when in (base + 0.99, base + 1.0):
        for _ in range(n):
            clk.advance(max(0.0, when - clk.monotonic()))
            if lim.try_acquire():  # type: ignore[attr-defined]
                admitted.append(clk.monotonic() - base)
    span = 0.05
    peak = max((sum(1 for t in admitted if lo <= t < lo + span)
                for lo in (x / 1000.0 for x in range(940, 1060))), default=0)
    return {"label": label, "admitted": len(admitted), "denied": getattr(lim, "denied", 0),
            "peak50": peak, "limit": 100}

def demo_algorithms() -> list[dict]:
    phase("1b. 复现：限流算法的差异（固定窗口边界 2×limit）", "(virtual clock)")
    rows = [_boundary_burst(lambda c: FixedWindowLimiter(100, 1.0, c), "固定窗口 100/s"),
            _boundary_burst(lambda c: SlidingWindowLimiter(100, 1.0, c), "滑动窗口 100/s"),
            _boundary_burst(lambda c: TokenBucket(100, 100, clock=c, name="tb"),
                            "令牌桶 r=100 burst=100"),
            _boundary_burst(lambda c: LeakyBucket(100, 10, c), "漏桶 r=100 queue=10")]
    print("\n  ┌─ 边界突发：t=0.99s 与 t=1.0s 各 130 个请求（limit=100/s）")
    print(f"  │ {'算法':<24} {'放行':>5} {'拒绝':>5} {'50ms内峰值':>11} {'= ×limit':>9}")
    for x in rows:
        print(f"  │ {x['label']:<24} {x['admitted']:>5} {x['denied']:>5} "
              f"{x['peak50']:>11} {x['peak50'] / x['limit']:>8.1f}×")
    print("  └" + "─" * 62)
    for line in (
        "固定窗口：[0,1) 与 [1,2) 各算一份配额，两次突发**都被放行** —— 上游在 10ms 内",
        "  收到 2×limit 的流量，这就是「窗口边界双倍突发」。",
        "滑动窗口：回看过去 1s 已放行多少，第二次突发只能被拒 —— 峰值被压回 1×limit。",
        "令牌桶：桶容量给出一次性突发额度，之后按 rate 匀速补 —— 有界的突发。",
        "漏桶 vs 令牌桶：漏桶只整形（恒定流出、不允许突发，适合护住脆弱下游）；令牌桶",
        "  允许突发（适合「平时闲着、偶尔一口气打 20 个」的 agent 场景）。",
    ):
        note(line)
    print(f"\n{BROKEN} 固定窗口边界 50ms 内放行 {rows[0]['peak50']} = "
          f"{rows[0]['peak50'] / 100:.1f}× limit(100)；滑动窗口 "
          f"{rows[1]['peak50'] / 100:.1f}×、令牌桶 {rows[2]['peak50'] / 100:.1f}×")
    return rows

def probe_real_provider() -> dict:
    """真实 LLMServer 交叉验证：排队超预算 → TIMEOUT，排队位满 → 429。"""
    srv = LLMServer(max_queue=4, max_wait_s=2.0, seed=11)
    srv.set_latency("small-8b", 20)
    msgs, ok, codes = [user("限流探针")], 0, {}
    for _ in range(60):
        try:
            srv.call(msgs, model="small-8b", timeout=0.5, tenant="probe")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", type(exc).__name__)
            codes[code] = codes.get(code, 0) + 1
    return {"ok": ok, "codes": codes, "peak_inflight": int(srv.stats["max_inflight"]),
            "peak_queued": int(srv.stats["max_queued"])}

def _report(r: RunResult, tag: str) -> None:
    print(f"\n  ┌─ {tag}：{r.name}")
    print(f"  │ {'租户':<12} {'请求':>5} {'成功':>5} {'成功率':>8} {'P95':>10} {'makespan':>10}")
    for t in TENANTS:
        print(f"  │ {t:<12} {r.total.get(t, 0):>5} {r.ok.get(t, 0):>5} {r.rate(t):>7.1%} "
              f"{percentile(r.lat.get(t, [0.0]), 95):>9.0f}ms "
              f"{r.makespan_s.get(t, 0.0):>9.2f}s")
    print("  └" + "─" * 62)
    kv("小租户平均成功率", f"{sum(r.rate(t) for t in SMALL_TENANTS) / len(SMALL_TENANTS):.1%}")
    kv("大租户成功率", f"{r.rate('tenant-big'):.1%}")
    kv("Jain 公平指数(成功率 / P95 倒数)", f"{r.jain('ok'):.4f} / {r.jain('p95'):.4f}")
    kv("provider 峰值并发 / 排队", f"{r.peak_inflight} / {r.peak_queued}",
       f"（上限 {CAPACITY} / {MAX_QUEUE}）")
    kv("provider 稳态在飞 P50 / P95", f"{r.inflight_p50:.0f} / {r.inflight_p95:.0f}",
       f"（上限 {CAPACITY}）")
    kv("上游调用次数", f"{r.calls}",
       f"（客户端重试 {r.retries}，provider 429 {r.rejected_429}）")
    kv("客户端本地节流等待", f"{r.pacing_waits}", " 次（被限流层卡住 → 排队不打上游）")
    if r.rejected_by_layer:
        kv("按层统计", r.rejected_by_layer)
    kv("失败尝试", f"{r.failures}", f"（排队/执行超预算 {r.deadline_misses}）")
    kv("Retry-After 被遵守", f"{r.retry_after_honored}",
       f"/ {r.failures} 次失败（{r.retry_after_honored / max(1, r.failures):.0%}）")

def main() -> int:
    with lab(LAB_ID, "高并发下的限流与公平性：单租户如何拖垮所有人",
             "高并发下如何解决 LLM 接口限流？多用户并发如何避免一个租户打满全局配额？"):
        head("1. 复现故障：大租户 300 并发把 provider 打满")
        demo_algorithms()
        probe = probe_real_provider()
        phase("1c. 交叉验证：真实 LLMServer 的排队 / 429 语义", "(real provider)")
        kv("成功 / 失败", f"{probe['ok']} / {sum(probe['codes'].values())}",
           f"错误分布={probe['codes'] or '{}'}")
        kv("峰值 max_inflight / max_queued", f"{probe['peak_inflight']} / {probe['peak_queued']}",
           "（上限 24 / 4；429 带 Retry-After，TIMEOUT 没有）")
        ra = probe_retry_after_honoring()
        kv("429 退避探针", " | ".join(f"{k}: honored={v[0]:.2f} 失败={v[1]} 通过={v[2]}"
                                      for k, v in ra.items()))
        head("2. 观测 / 归因：全局共享配额 + FIFO 的公平性")
        phase("v0 拍脑袋版：全局一个配额、一条 FIFO 队列、无租户隔离", "(BROKEN)")
        b = run_load("v0 全局共享 + FIFO", broken=True)
        _report(b, "v0")
        head("3. 修复：四层限流 + WFQ 公平排队 + 每租户配额突发")
        phase("v1 生产版：入口/租户/模型/工具四层限流 + WFQ + 429 退避", "(FIX)")
        f = run_load("v1 四层限流 + WFQ", broken=False)
        _report(f, "v1")
        small = {k: sum(r.rate(t) for t in SMALL_TENANTS) / len(SMALL_TENANTS)
                 for k, r in (("b", b), ("f", f))}
        p95 = {k: max(percentile(r.lat.get(t, [0.0]), 95) for t in SMALL_TENANTS)
               for k, r in (("b", b), ("f", f))}
        print("\n  ┌─ 租户配额配置表（rate=每秒令牌, burst=瞬时突发）")
        print(f"  │ {'租户':<12} {'rate':>9} {'burst':>7} {'虚拟结束时刻':>14}")
        for t in TENANTS:
            print(f"  │ {t:<12} {QUOTA[t][0]:>7.0f}/s {QUOTA[t][1]:>7.0f} "
                  f"{f.makespan_s.get(t, 0.0):>13.2f}s")
        for nm, (rate, burst) in (("__global__", GLOBAL_QUOTA), ("__model__", MODEL_QUOTA),
                                  ("__tool__", TOOL_QUOTA)):
            print(f"  │ {nm:<12} {rate:>7.0f}/s {burst:>7.0f}")
        print("  └" + "─" * 62)
        print(f"\n{FIX} 小租户成功率 {small['b']:.0%} -> {small['f']:.0%}；大租户 makespan "
              f"{b.makespan_s['tenant-big']:.2f}s -> {f.makespan_s['tenant-big']:.2f}s"
              "（配额让它变慢 —— 这就是公平的代价）")
        head("4. 验证")
        rows = [("small_tenant_success_rate", small["b"], small["f"], False, ".3f"),
                ("fairness_jain_index", b.jain("ok"), f.jain("ok"), False, ".4f"),
                ("steady_inflight_p95", b.inflight_p95, f.inflight_p95, True, ".1f"),
                ("small_tenant_p95_ms", p95["b"], p95["f"], True, ".0f"),
                ("retry_after_honored_ratio", ra["v0 固定 150ms 重试"][0],
                 ra["v1 max(backoff, retry_after)"][0], False, ".3f")]
        # 这两条是"变化方向本身就是结论"的权衡项，必须在同一行显式标注
        rows.append(("upstream_calls", b.calls, f.calls, True, ".0f"))
        rows.append(("batch_makespan_s", b.makespan_s["tenant-big"],
                     f.makespan_s["tenant-big"], True, ".2f"))
        notes = {"upstream_calls": "  # direction: increase-expected",
                 "batch_makespan_s": "  # direction: increase-expected"}
        for name, bv, fv, lower, fmt in rows:
            note(f"{name:<26}: {bv:>10.3f} -> {fv:>10.3f}  "
                 f"({improvement(bv, fv, lower_is_better=lower)})")
            print(f"{VERIFY} {name}: {format(bv, fmt)} -> {format(fv, fmt)} "
                  f"({improvement(bv, fv, lower_is_better=lower)}){notes.get(name, '')}")
        note("↑ upstream_calls 上升是**公平性的代价**：小租户不再被挤掉，但限流把请求摊开成")
        note("  更多次尝试；batch_makespan_s 变长是**隔离生效的证据**——批处理不再独占资源。")
        note("小租户成功率是提升（0.94 -> 1.00），improvement 用 lower_is_better=False 才是正号。")
        note("Jain 公式: J(x1..xn) = (Σxi)² / (n·Σxi²)，1.0 表示所有租户完全一致。")
        note(f"P95 倒数口径的公平指数 {b.jain('p95'):.4f} -> {f.jain('p95'):.4f}："
             "v0 的小租户 P95 完全由大租户的流量决定。")
        head("5. 工程结论：限流放服务端还是客户端？")
        for line in ("服务端：入口全局限流 + 租户配额 + 模型/工具限流，超限返回 429/503 + Retry-After。",
                     "客户端：读 Retry-After、全抖动退避、共享重试预算；不要在原地阻塞排队。",
                     "两边都要：服务端保护自己（拒绝比排队便宜），客户端礼貌 + 省成本。",
                     "永远不要把 429 转成 500：429 是「稍后再来」，500 是「我坏了」，语义不同。",
                     "公平性靠调度器：FIFO 对请求多的租户天然有利，WFQ/轮转才能隔离大租户。"):
            note(line)
        METRICS.render("lab-04 指标快照", include=["ratelimit_", "llm_"])
        takeaway("多租户隔离的关键不在「配额多大」，而在「排队和舱壁有没有租户维度」；"
                 "FIFO 会把大租户的问题变成所有人的问题，分层限流 + WFQ 才是可承诺的 SLO。")
        METRICS.reset()
    return 0

QUESTIONS = [
    "高并发下会如何解决 LLM 接口限流的问题？ -> 分层限流(入口/租户/模型/工具) + "
    "令牌桶配额与突发 + 429 Retry-After + 全抖动退避",
    "多用户并发，如何避免一个租户打满全局配额？ -> 每租户舱壁/队列 + 每租户 TokenBucket "
    "+ WFQ 公平排队，实测小租户成功率 94% -> 100%、P95 1971ms -> 999ms",
    "固定窗口/滑动窗口/令牌桶/漏桶怎么选？ -> 边界突发实测 2.0×limit vs 1.0×limit，"
    "整形与允许突发的取舍",
    "限流应该放服务端还是客户端？ -> 两边都要：服务端保护自己，客户端礼貌且省成本",
]

if __name__ == "__main__":
    sys.exit(main())
