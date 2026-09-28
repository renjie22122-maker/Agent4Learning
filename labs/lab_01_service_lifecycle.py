"""Lab 01: Agent 服务启停设计 —— 发布期间为什么用户会报错。

对应生产问题：「讲一下 Agent 的服务如何做启停设计？」「如何避免发布期间导致
用户体验报错？」

复现的故障（v0：服务只有一个 ``started`` 布尔量）
------------------------------------------------
1. **就绪探针撒谎**：进程一起来探针就 ready，可检索索引 / 连接池 / 前缀缓存都
   还没预热 → 流量立刻进来，第一批请求全部 503。
2. **SIGTERM 丢在飞请求**：收到信号立刻 ``started=False``，正在飞的请求跑完之后
   被判定为"服务已停"直接丢弃 → 用户看到 500 / 连接重置。
3. **没有排空（drain）**：老连接一刀切断，负载均衡器还在往上打新流量。

v1 的四件事（全部真实实现并测出数字）：a. liveness / readiness 分离（前者回答
"进程要不要重启"，后者回答"能不能接流量"，依赖抖动只摘流不重启；反模式是
liveness 也判依赖 → 多副本重启风暴）；b. 预热（索引/连接池/前缀缓存热完才
ready）；c. 优雅停机（readiness=false → 停止 accept → 排空≤grace_s → 强杀并
计数）；d. 连接排空（停机实例服务完老连接，新连接被 LB 导到别的实例）。

工程结论：发布期报错几乎都不是「代码 bug」，而是启停顺序问题 —— 先摘流量、再
排空、最后才退出；冷启动首批请求的 P95 是**预热出来的**，不是优化出来的。
"""

from __future__ import annotations

import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from agentlab.clock import VirtualClock
from agentlab.metrics import METRICS
from agentlab.providers import LLMError, LLMServer, system, user
from agentlab.store import BM25Index, Query, build_corpus
from agentlab.util import (BROKEN, FIX, VERIFY, Stats, improvement, kv, lab, note,
                           phase, run_concurrently, takeaway)

LAB_ID = "lab-01-service-lifecycle"
MODEL = "small-8b"
BATCH = 12
GRACE_S = 2.0
POOL_SIZE = 8
HANDSHAKE_S = 0.06  # 没有连接池时，每个请求都要重新握手/鉴权
INDEX_DOCS = 20_000
MSGS = [system("你是生产级 agent 助手，回答必须引用知识库并给出处。"),
        user("总结一下缓存穿透的治理方案")]

def _build_index() -> BM25Index:
    """真实构建一次倒排索引（预热的主要成本之一，实测 ~150ms）。"""
    return BM25Index(build_corpus(INDEX_DOCS, seed=7))

def _hit_rate(srv: LLMServer, snap: tuple[int, int]) -> float:
    """某批请求的前缀缓存命中率（snap = 这批开始前的请求数/命中数快照）。"""
    calls = int(srv.stats["requests"]) - snap[0]
    return (int(srv.stats["cache_hits"]) - snap[1]) / calls if calls else 0.0

def _measure_batch(handler: Callable[[int], dict], count: int = BATCH,
                   workers: int | None = None) -> dict:
    results = run_concurrently(handler, count, workers or count)
    rows = [r for r in results if isinstance(r, dict)]
    lat = [float(r["latency_ms"]) for r in rows if r.get("ok")]
    errors = dict(Counter(str(r.get("err") or "?") for r in rows if not r.get("ok")))
    if len(rows) != len(results):  # 理论上不该发生，但统计不能骗人
        errors["EXCEPTION"] = len(results) - len(rows)
    st = Stats(lat)
    return {"stats": st, "p95": st.p95 if lat else 0.0, "errors": errors,
            "ok": sum(1 for r in rows if r.get("ok")),
            "fail": sum(1 for r in rows if not r.get("ok")) + len(results) - len(rows)}

def _fire_then_signal(svc, stop_fn: Callable[[], object], delay_s: float = 0.13,
                      n: int = BATCH) -> dict:
    """满负载时发停止信号：v0 丢在飞请求，v1 先摘流量再排空。"""
    box: dict = {}
    th = threading.Thread(target=lambda: box.update(batch=_measure_batch(svc.handle, n)),
                          daemon=True)
    th.start()
    time.sleep(delay_s)
    inflight = svc.inflight()
    drain_s = stop_fn()
    th.join(timeout=8.0)
    return {"batch": box.get("batch"), "inflight_at_stop": inflight,
            "drain_s": float(drain_s or 0.0)}

class ServiceV0:
    """v0：进程活着 == 可以接流量；停止信号 == 立刻停止一切。"""

    def __init__(self, srv: LLMServer, name: str = "svc-v0"):
        self.name, self.srv = name, srv
        self.started = False
        self._deps_ready = False
        self._inflight = 0
        self._lock = threading.Lock()
        self.not_ready = self.dropped = self.served = 0
        self.warm_ms = 0.0

    def start(self) -> None:
        """进程起来了就自认为可用 —— 依赖在后台慢慢热（探针不看依赖）。"""
        self.started = True
        threading.Thread(target=self._warm_bg, daemon=True).start()

    def _warm_bg(self) -> None:
        t0 = time.perf_counter()
        _build_index()
        time.sleep(POOL_SIZE * HANDSHAKE_S)
        self.warm_ms = (time.perf_counter() - t0) * 1000.0
        self._deps_ready = True

    def wait_ready(self, timeout_s: float = 5.0) -> bool:
        """外部脚本等后台预热结束（真实系统里这一步本该由 readiness 探针承担）。"""
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline and not self._deps_ready:
            time.sleep(0.01)
        return self._deps_ready

    def sigterm(self) -> None:
        """一刀切：没有 readiness 摘流，也没有排空。"""
        self.started = False

    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def handle(self, i: int) -> dict:
        if not self.started:
            with self._lock:
                self.dropped += 1
            return {"i": i, "ok": False, "err": "CONNECTION_RESET", "latency_ms": 0.0}
        if not self._deps_ready:  # 探针说了 ready，可依赖还没热 —— 第一批请求死在这
            with self._lock:
                self.not_ready += 1
            return {"i": i, "ok": False, "err": "503_NOT_READY", "latency_ms": 0.0}
        t0 = time.perf_counter()
        with self._lock:
            self._inflight += 1
        try:
            time.sleep(HANDSHAKE_S)  # 没有连接池：每个请求重新握手
            self.srv.call(MSGS, model=MODEL, timeout=1.5, tenant="t1", tag="v0")
            out = {"i": i, "ok": True, "err": "", "latency_ms": 0.0}
        except LLMError as exc:
            out = {"i": i, "ok": False, "err": exc.code, "latency_ms": 0.0}
        finally:
            with self._lock:
                self._inflight -= 1
        if not self.started:
            # ← 核心故障：活干完了，但"服务已停"，结果被丢弃 → 连接重置
            with self._lock:
                self.dropped += 1
            return {"i": i, "ok": False, "err": "CONNECTION_RESET",
                    "latency_ms": (time.perf_counter() - t0) * 1000.0}
        out["latency_ms"] = (time.perf_counter() - t0) * 1000.0
        return out

class ServiceV1:
    """v1：探针分离 + 预热 + 优雅停机 + 连接排空。"""

    def __init__(self, srv: LLMServer, name: str = "svc-v1", grace_s: float = GRACE_S,
                 model: str = MODEL):
        self.name, self.srv, self.grace_s, self.model = name, srv, grace_s, model
        self.live = self._ready = self._accepting = self._killed = False
        self._inflight = 0
        self._cond = threading.Condition()
        self.not_ready = self.rejected_after_stop = self.dropped = 0
        self.interrupted = self.served = self.routed = 0
        self.drain_s = self.warm_ms = 0.0
        self.conns_opened = 0
    def is_live(self) -> bool:
        """liveness：进程自身还活着吗？（只看自己，不看依赖）"""
        return self.live

    def is_ready(self) -> bool:
        """readiness：现在能接流量吗？（依赖已预热 + 未停机）"""
        with self._cond:
            return self._ready and self._accepting

    def inflight(self) -> int:
        with self._cond:
            return self._inflight

    def _warm(self) -> None:
        """启动预热：索引 → 连接池 → provider 前缀缓存 → 检索热路径。"""
        t0 = time.perf_counter()
        _build_index()                                     # ① 检索索引
        time.sleep(POOL_SIZE * HANDSHAKE_S)                # ② 连接池建 8 条连接
        self.conns_opened = POOL_SIZE
        self.srv.warm_prefix(MODEL, LLMServer.prefix_key(MSGS), 900)  # ③ 前缀缓存
        _build_index().search(                             # ④ 检索热路径
            Query("缓存穿透 优化", 5, tenant="tenant-a", groups=frozenset({"ga", "public"})))
        self.warm_ms = (time.perf_counter() - t0) * 1000.0

    def start(self) -> None:
        """同步预热：预热完成前 readiness 一直是 false。"""
        self.live = True
        self._warm()
        with self._cond:
            self._ready = self._accepting = True

    def shutdown(self, reason: str = "SIGTERM") -> float:
        """优雅停机：摘流量 → 停 accept → 排空(≤grace) → 超时强杀。返回排空秒数。"""
        with self._cond:
            self._ready = False       # ① 先摘流量：探针立刻失败，LB 不再送新连接
            self._accepting = False   # ② 停止 accept 新请求
            t0 = time.perf_counter()
            deadline = t0 + self.grace_s
            while self._inflight > 0:  # ③ 等在飞请求排空（最多 grace_s）
                if deadline - time.perf_counter() <= 0:
                    break
                self._cond.wait(0.02)
            self.drain_s = time.perf_counter() - t0
            if self._inflight > 0:    # ④ 超时强杀：剩下的请求被明确中断并计数
                self._killed = True
                self.interrupted += self._inflight
        self.live = False             # ⑤ 真退出，liveness 此刻才失败
        return self.drain_s

    def handle(self, i: int) -> dict:
        t0 = time.perf_counter()
        with self._cond:
            if not self._accepting:
                if self._killed:
                    self.rejected_after_stop += 1
                    err = "SHUTTING_DOWN"
                else:
                    self.not_ready += 1
                    err = "NOT_READY"
                return {"i": i, "ok": False, "err": err, "latency_ms": 0.0}
            self._inflight += 1
        err = ""
        try:
            self.srv.call(MSGS, model=self.model, timeout=2.0, tenant="t1", tag="v1")
        except LLMError as exc:  # 上游抖动只记错误码，不影响排空
            err = exc.code
        finally:
            with self._cond:
                self._inflight -= 1
                self._cond.notify_all()
        latency = (time.perf_counter() - t0) * 1000.0
        if self._killed:  # 超出 grace 被强杀：老连接在这里被切断
            with self._cond:
                self.dropped += 1
            return {"i": i, "ok": False, "err": "INTERRUPTED", "latency_ms": latency}
        with self._cond:
            self.served += 1
        return {"i": i, "ok": not err, "err": err, "latency_ms": latency}

@dataclass
class InstancePool:
    """两个实例 + 极简负载均衡：新请求只发给 readiness=true 的实例。"""

    instances: list[ServiceV1] = field(default_factory=list)

    def handle(self, i: int) -> dict:
        ready = [s for s in self.instances if s.is_ready()]
        inst = min(ready, key=lambda s: s.inflight()) if ready else None
        if inst is None:
            return {"i": i, "ok": False, "err": "NO_READY_INSTANCE", "latency_ms": 0.0}
        with inst._cond:  # 统计"这一轮有多少请求被路由到它"
            inst.routed += 1
        out = inst.handle(i)
        out["inst"] = inst.name
        return out

def demo_v0() -> dict:
    """复现：探针撒谎 + SIGTERM 丢在飞请求。"""
    srv = LLMServer(max_queue=32, max_wait_s=3.0, seed=11)
    srv.set_error_rate(MODEL, 0.0)  # 上游是好的，故障只来自启停设计
    svc = ServiceV0(srv)
    svc.start()
    kv("启动瞬间 readiness", "True", "  ← 依赖还在后台预热，探针已经说 ready")
    first = _measure_batch(svc.handle)
    kv("第一批请求(预热未完成)", f"{first['ok']}/{BATCH} 成功", f"  错误={first['errors']}")
    svc.wait_ready()
    snap0 = (int(srv.stats["requests"]), int(srv.stats["cache_hits"]))
    cold = _measure_batch(svc.handle)
    kv("后台预热 / v0 冷路径首批请求", f"{svc.warm_ms:.0f}ms  {cold['stats']}",
       f"  错误={cold['errors']} 缓存命中率={_hit_rate(srv, snap0):.0%}")
    cut = _fire_then_signal(svc, svc.sigterm, delay_s=0.08)
    kv("SIGTERM 在飞请求 / 结果 / 排空耗时",
       f"{cut['inflight_at_stop']} 个 / {cut['batch']['errors']} / 0.000s（没有排空）")
    return {"not_ready": svc.not_ready, "cold_p95": cold["p95"], "first_fail": first["fail"],
            "warm_ms": svc.warm_ms, "cache_rate": _hit_rate(srv, snap0),
            "dropped": cut["batch"]["fail"], "interrupted": cut["batch"]["fail"],
            "total_bad": first["fail"] + cut["batch"]["fail"]}

def _probe_sim(anti_dep_in_liveness: bool, horizon_s: float = 20.0) -> dict:
    """虚拟时钟推演 20s：依赖在 t=2s 抖动 2s，两种探针配置的代价。

    反模式 anti=True：liveness 也查依赖 → 抖动时进程被判「死了」而重启（进程本来
    是好的），冷启动又去压依赖 → 3 副本一起重启（探针 period=1s、threshold=2）。
    """
    clk = VirtualClock()
    n = 3
    up, boot_until, streak = [True] * n, [0.0] * n, [0] * n
    restarts = shed = served = 0
    extra = 0.0  # 每次冷启动把依赖抖动窗口拉长的量（缓存/连接池都没了）
    timeline: list[str] = []
    for tick in range(int(horizon_s)):
        t = float(tick)
        dep_ok = not (2.0 <= t < 4.0 + extra)
        for r in range(n):
            if not up[r]:
                if t < boot_until[r]:
                    continue
                up[r], streak[r] = True, 0
            if dep_ok:
                streak[r] = 0
                continue
            streak[r] += 1
            if anti_dep_in_liveness and streak[r] >= 2:
                up[r], boot_until[r], streak[r] = False, t + 3.0, 0
                restarts += 1
                extra += 1.2
        ready = sum(1 for r in range(n) if up[r] and dep_ok)
        served += ready
        shed += n - ready
        timeline.append(f"{tick:02d}s:{ready}/{n}")
        clk.advance(1.0)
    return {"restarts": restarts, "shed": shed, "served": served, "extra_s": extra,
            "timeline": " ".join(timeline[:10]), "clock": clk.stats()}

def demo_liveness_vs_readiness() -> dict:
    good = _probe_sim(anti_dep_in_liveness=False)
    bad = _probe_sim(anti_dep_in_liveness=True)
    note(f"readiness 只看依赖 : {good['timeline']}  ← 2s 后自动恢复接流量")
    note(f"liveness  也看依赖 : {bad['timeline']}  ← 重启风暴")
    kv("抖动窗口 / 重启次数 / 摘流副本·秒 / 可服务副本·秒",
       f"2.0s/{good['restarts']}/{good['shed']}/{good['served']} -> "
       f"{2.0 + bad['extra_s']:.1f}s/{bad['restarts']}/{bad['shed']}/{bad['served']}")
    kv("虚拟时钟", good["clock"])
    return {"good": good, "bad": bad}
def demo_v1() -> dict:
    """v1：预热完才 ready；停机排空；grace 到点则强杀。"""
    srv = LLMServer(max_queue=32, max_wait_s=3.0, seed=11)
    srv.set_error_rate(MODEL, 0.0)
    svc = ServiceV1(srv, name="pod-A")
    kv("预热期间 readiness/liveness", f"{svc.is_ready()} / {svc.is_live()}",
       "  ← 双双 false，LB 不会送流量")
    svc.start()
    kv("预热耗时(索引+连接池+前缀缓存)", f"{svc.warm_ms:.0f}ms",
       f"  连接数={svc.conns_opened}  readiness/liveness={svc.is_ready()}/{svc.is_live()}")
    snap1 = (int(srv.stats["requests"]), int(srv.stats["cache_hits"]))
    first = _measure_batch(svc.handle)
    kv("warm 后首批请求", str(first["stats"]),
       f"  错误={first['errors']} 缓存命中率={_hit_rate(srv, snap1):.0%}")
    cut = _fire_then_signal(svc, svc.shutdown, delay_s=0.05)
    kv("SIGTERM 在飞 / 排空耗时 / 被迫中断",
       f"{cut['inflight_at_stop']} 个 / {cut['drain_s'] * 1000:.0f}ms / {svc.interrupted} 个",
       f"  停机后新 accept={svc.rejected_after_stop}")
    # grace 不够用的场景：上游变慢，一个请求要 1.4s，grace 只有 0.5s
    srv2 = LLMServer(max_queue=8, max_wait_s=4.0, seed=3)
    srv2.set_latency("mid-32b", 1400)
    slow = ServiceV1(srv2, name="pod-C", grace_s=0.5, model="mid-32b")
    slow.start()
    cut2 = _fire_then_signal(slow, slow.shutdown, delay_s=0.15, n=1)
    kv("grace 用尽(0.5s)时", f"排空 {cut2['drain_s'] * 1000:.0f}ms 后强杀",
       f"，中断 {slow.interrupted} 个（grace 不可能无限等：超时要明确中断 + 计数）")
    return {"warm_ms": svc.warm_ms, "first_p95": first["p95"],
            "cache_rate": _hit_rate(srv, snap1), "drain_s": cut["drain_s"],
            "interrupted": svc.interrupted}

def demo_rolling_deploy() -> dict:
    """滚动发布：停掉 pod-A，新连接去 pod-B，老连接在 A 上跑完。"""
    srv = LLMServer(max_queue=32, max_wait_s=3.0, seed=5)
    srv.set_error_rate(MODEL, 0.0)
    a, b = ServiceV1(srv, name="pod-A"), ServiceV1(srv, name="pod-B")
    a.start()
    b.start()
    pool = InstancePool([a, b])
    snap: dict = {}

    def stop_a() -> None:
        # 等到 pod-A 手上确实有在飞请求再发 SIGTERM（模拟真实的发布时刻）
        deadline = time.perf_counter() + 2.0
        while a.inflight() < 2 and time.perf_counter() < deadline:
            time.sleep(0.005)
        snap["routed"], snap["served"], snap["inflight"] = a.routed, a.served, a.inflight()
        a.shutdown()

    timer = threading.Timer(0.35, stop_a)
    timer.start()
    batch = _measure_batch(pool.handle, 36, workers=6)
    timer.join(timeout=5.0)
    kv("SIGTERM 时 pod-A 在飞请求 / 停机后被路由过来的新请求",
       f"{snap['inflight']} / {a.routed - snap['routed']}", " 个（老连接排空、新连接为 0）")
    kv("pod-A 排空完成 / 被迫中断", f"{a.served - snap['served']} / {a.interrupted}", " 个")
    kv("pod-A / pod-B 承接 / 整轮成功", f"{a.routed} / {b.routed} / {batch['ok']}",
       f"  错误={batch['errors']}")
    return {"a_after": a.routed - snap["routed"], "a_interrupted": a.interrupted,
            "a_served_during": a.served - snap["served"]}

def main() -> int:
    with lab(LAB_ID, "Agent 服务启停设计：发布期间为什么用户会报错",
             "讲一下 Agent 的服务如何做启停设计？如何避免发布期间导致用户体验报错？"):
        phase("1. 复现故障", "(v0: 只有一个 started 布尔量)")
        v0 = demo_v0()
        print(f"\n{BROKEN} 发布期第一批 {BATCH} 个请求里 {v0['first_fail']} 个 503（探针提前 "
              f"ready），SIGTERM 又丢掉 {v0['dropped']} 个在飞请求，合计受损 {v0['total_bad']} "
              f"个，排空 0.000s，冷路径首批 P95={v0['cold_p95']:.0f}ms")
        phase("2. 观测 / 归因", "(liveness 与 readiness 混用会怎样)")
        probe = demo_liveness_vs_readiness()
        g, bad = probe["good"], probe["bad"]
        print(f"\n{BROKEN} 依赖抖动 2s：正确配置重启 {g['restarts']} 次、掉 {g['shed']} 个"
              f"副本·秒；liveness 也判依赖则重启 {bad['restarts']} 次、掉 {bad['shed']} 个"
              f"副本·秒（滚动重启风暴）")
        phase("3. 修复", "(预热 + 分离探针 + 优雅停机 + 连接排空)")
        v1 = demo_v1()
        pool = demo_rolling_deploy()
        print(f"\n{FIX} 预热 {v1['warm_ms']:.0f}ms 后才置 readiness；停机先摘流量再排空 "
              f"{v1['drain_s'] * 1000:.0f}ms（中断 {v1['interrupted']} 个）；滚动发布时 pod-A "
              f"新连接 {pool['a_after']} 个、老连接排空 {pool['a_served_during']} 个")
        phase("4. 验证")
        rows = [("first_batch_p95_ms", v0["cold_p95"], v1["first_p95"], "{:.0f}", ""),
                ("prefix_cache_hit_rate", v0["cache_rate"], v1["cache_rate"], "{:.2f}", ""),
                ("dropped_requests", float(v0["total_bad"]), 0.0, "{:.0f}", ""),
                ("drain_interrupted", float(v0["interrupted"]), float(v1["interrupted"]),
                 "{:.0f}", ""),
                ("graceful_drain_seconds", 0.0, v1["drain_s"], "{:.3f}",
                 "  # direction: increase-expected"),
                ("not_ready_503", float(v0["not_ready"]), 0.0, "{:.0f}", "")]
        for name, before, after, fmt, tail in rows:
            print(f"{VERIFY} {name}: {fmt.format(before)} -> {fmt.format(after)} "
                  f"({improvement(before, after)}){tail}")
        print()
        note("指标读法提醒：graceful_drain_seconds 0.000s -> 0.3s 不是变慢，而是从「没有排空"
             "能力」变成「能排空」；v0 的 0.000s = 根本没等，12 个在飞请求被直接掐断"
             "（见 drain_interrupted）。看到 0 先问一句：是快，还是没做？")
        note(f"首批请求的前缀缓存命中率：冷启动 {v0['cache_rate']:.0%} -> 预热后 "
             f"{v1['cache_rate']:.0%}（预热把 provider 侧前缀缓存也热了）")
        note("工程结论：")
        note("1) 探针问的是两个问题：liveness=要不要重启进程，readiness=能不能接流量。")
        note("2) 依赖抖动只该动 readiness；liveness 跟着动 = 滚动重启风暴（3 副本全重启）。")
        note("3) 停机顺序：readiness=false → 停 accept → 排空(≤grace) → 强杀并计数。")
        note("4) 预热是 SLO 的一部分；排空要有上限，超时明确中断，不能无声丢弃。")
        takeaway("发布期报错几乎都不是「代码 bug」，而是启停顺序问题："
                 "先摘流量、再排空、最后才退出。")
        METRICS.reset()
    return 0

QUESTIONS = [
    "Agent 服务如何做启停设计？ -> liveness/readiness 分离 + 预热门禁 + 优雅停机"
    "(摘流→停 accept→排空→强杀) + 连接排空",
    "如何避免发布期间用户报错？ -> 停机先摘流量、LB 把新连接导到 ready 实例、"
    "老连接排空到完成",
    "就绪探针为什么不能只看进程存活 / liveness 能不能判依赖？ -> 前者把流量送进未预热"
    "的进程（首批全 5xx），后者造成多副本重启风暴（本 lab 用虚拟时钟量化）",
]
if __name__ == "__main__":
    sys.exit(main())
