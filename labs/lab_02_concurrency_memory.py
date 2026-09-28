"""Lab 02: 并发一高就 panic / 内存泄漏 —— 五种最常见的生产泄漏与排查修复。

对应生产问题：「生产环境并发量一高 agent 就会 panic，核心原因是什么？」
「最常见的生产环节内存泄漏有哪几种，如何排查修复？」

第一部分（真实压测）：panic 的传递链 —— 无界接收请求 → 上下文全量驻留内存
（RSS↑）→ 线程数↑ → 上游排队 + 上下文切换 → 请求变慢（P95↑）→ 上游超时 +
重试 → 流量再放大（正反馈）→ 雪崩。240 并发 + 无界排队下实测 RSS / 峰值线程
数 / 队列深度 / P95 / 错误率**同时**恶化，再用「有界队列 + 背压 + 单次尝试 +
端到端预算」把它压住。

第二部分：五种真实泄漏 —— ① 全局缓存无上限　② 全局 list 追加　③ 闭包/回调
持有大对象　④ threading.local / 线程池没清理　⑤ 未完成的 asyncio task。每种
按 **成因 → 现场特征 → 定位手段 → 修复写法** 讲，定位用真实的 tracemalloc
top-5 分配点 + gc.get_objects() 对象计数。

关键区分：gc.collect() 后**能回收**的叫内存膨胀（RSS 不还给 OS 但可复用），
**不能回收**的才叫泄漏。结论：并发 panic 的第一性原因是**没有上限**。
"""

from __future__ import annotations

import asyncio
import gc
import os
import queue
import sys
import threading
import time
import tracemalloc
import weakref
from collections import Counter, OrderedDict, deque
from typing import Callable

from agentlab.metrics import METRICS
from agentlab.providers import LLMError, LLMServer, system, user
from agentlab.tokens import ModelSpec, count_messages
from agentlab.util import (BROKEN, FIX, VERIFY, Stats, fmt_bytes, improvement, kv, lab, note,
                           payload_of_kb, phase, run_concurrently, rss_mb, takeaway)

LAB_ID = "lab-02-concurrency-memory"
N_REQUESTS = 240  # 并发请求数（v0 的模型是一个请求一个线程）
PAYLOAD_KB = 256  # 每个请求上下文带的大对象（让内存问题可见）
QUEUE_MAX = 16  # v1 的有界队列深度（背压阈值）
WORKERS = 8  # v1 的消费者线程数（= 舱壁的并发上限）
CALLERS = 64  # v1 的调用方线程上限（有界，不再一人一线程）
ADMIT_WAIT_S = 0.30  # v1 入场等待预算：排队超过它就快速失败（背压）
REQ_TIMEOUT_S = 0.60  # 单次上游调用超时（两版一致，保证可比）
LEAK_N = 80  # 每种泄漏制造的请求上下文字数

class ReqCtx:
    """一次请求在内存里的全部上下文：prompt + 检索结果 + 中间状态。"""

    def __init__(self, i: int, kb: float = PAYLOAD_KB):
        self.i = i
        self.prompt = f"用户问题 {i}：" + "请总结缓存穿透的治理方案。" * 8
        self.payload = payload_of_kb(kb)  # 检索回来的大段文档 / 中间结果
        self.msgs = [system("你是生产级 agent 助手"), user(self.prompt)]

def ms_since(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0

def _upstream(seed: int = 5) -> LLMServer:
    """一个"很挤"的上游：只有 3 个并发槽，等待队列却几乎无限 —— 最危险的组合：
    调用方的无界并发会全部变成上游排队时间，最后以 TIMEOUT 爆掉（照样计费）。"""
    spec = ModelSpec(name="flaky-8b", tier="small", latency_p50_ms=22, latency_sigma=0.4,
                     quality=0.6, in_price=0.05, out_price=0.15,
                     max_parallel=3, error_rate=0.10)
    return LLMServer(models=[spec], max_queue=512, max_wait_s=5.0, seed=seed)

def _start_sampler(box: dict, depth_fn: Callable[[], int]) -> threading.Event:
    """后台采样器：RSS 曲线 + 线程数 + 队列深度（三个一起看才看得出传递链）。"""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            box["series"].append((rss_mb(), threading.active_count(), depth_fn()))
            box["peak_threads"] = max(box["peak_threads"], threading.active_count())
            box["peak_depth"] = max(box["peak_depth"], depth_fn())
            stop.wait(0.008)

    threading.Thread(target=loop, daemon=True).start()
    return stop

def _summary(box: dict, base: float, results: list, wall_s: float, usd: float) -> dict:
    rows = [r for r in results if isinstance(r, dict)]
    series = box["series"] or [(base, 0, 0)]
    return {"wall_s": wall_s, "stats": Stats([r["latency_ms"] for r in rows]), "usd": usd,
            "rss_growth": max(s[0] for s in series) - base, "peak_threads": box["peak_threads"],
            "peak_depth": box["peak_depth"], "ok": sum(1 for r in rows if r["ok"]),
            "n": len(rows), "errors": dict(Counter(r["err"] for r in rows if r["err"])),
            "series": [round(s[0], 1) for s in series[:: max(1, len(series) // 5)]][:6]}

def avalanche_v0() -> dict:
    """v0：无界接收 —— 240 个请求各自开线程，各自把 256KB 上下文挂在全局队列上。"""
    srv = _upstream()
    registry: list[ReqCtx] = []  # 无界队列（"请求登记表"），没人限制它的长度
    lock = threading.Lock()
    box: dict = {"series": [], "peak_threads": 0, "peak_depth": 0}
    base, t0 = rss_mb(), time.perf_counter()
    stop = _start_sampler(box, lambda: len(registry))

    def handle(i: int) -> dict:
        ctx = ReqCtx(i)  # ← 每个请求都真的分配 256KB，且一直挂到请求结束
        with lock:
            registry.append(ctx)
        box["peak_threads"] = max(box["peak_threads"], threading.active_count())
        err = ""
        try:
            for _ in range(3):  # 硬编码 3 次重试：不看预算、不看上游是否已经排爆
                try:
                    srv.call(ctx.msgs, model="flaky-8b", timeout=REQ_TIMEOUT_S)
                    err = ""
                    break
                except LLMError as exc:
                    err = exc.code
                    time.sleep(0.02)
        finally:
            with lock:
                registry.remove(ctx)
        return {"ok": not err, "err": err, "latency_ms": ms_since(t0)}

    results = run_concurrently(handle, N_REQUESTS, N_REQUESTS)  # 240 个线程一起上
    wall = time.perf_counter() - t0
    stop.set()
    return _summary(box, base, results, wall, srv.ledger.usd)

def avalanche_v1() -> dict:
    """v1：有界队列 + 背压（入场超预算即快速失败）+ 固定 worker + 单次尝试。"""
    srv = _upstream()
    q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
    box: dict = {"series": [], "peak_threads": 0, "peak_depth": 0}
    base, t0 = rss_mb(), time.perf_counter()
    stop = _start_sampler(box, q.qsize)

    def worker() -> None:
        while True:
            item = q.get()
            if item is None:
                q.task_done()
                return
            i, slot = item
            ctx = ReqCtx(i)  # 只有"被接纳"的请求才分配上下文，满了根本走不到这
            try:
                srv.call(ctx.msgs, model="flaky-8b", timeout=REQ_TIMEOUT_S)
            except LLMError as exc:
                slot["err"] = exc.code
            finally:
                del ctx
                slot["ev"].set()
                q.task_done()

    [threading.Thread(target=worker, daemon=True).start() for _ in range(WORKERS)]

    def handle(i: int) -> dict:
        slot = {"ev": threading.Event(), "err": ""}
        deadline = time.perf_counter() + ADMIT_WAIT_S
        while True:  # 入场：队列满就等一小会儿，超过预算立刻失败（背压）
            try:
                q.put_nowait((i, slot))
                break
            except queue.Full:
                if time.perf_counter() >= deadline:
                    return {"ok": False, "err": "QUEUE_FULL", "latency_ms": ms_since(t0)}
                time.sleep(0.002)
        if not slot["ev"].wait(timeout=REQ_TIMEOUT_S):  # 端到端预算：不无限等
            return {"ok": False, "err": "DEADLINE", "latency_ms": ms_since(t0)}
        return {"ok": not slot["err"], "err": slot["err"], "latency_ms": ms_since(t0)}

    results = run_concurrently(handle, N_REQUESTS, CALLERS)  # 调用方线程也是有界的
    wall = time.perf_counter() - t0
    stop.set()
    [q.put(None) for _ in range(WORKERS)]
    return _summary(box, base, results, wall, srv.ledger.usd)

class BoundedLRU:
    """有界 LRU 缓存：容量上限 + TTL + 淘汰统计（OrderedDict 手写实现）。"""

    def __init__(self, capacity: int = 128, ttl_s: float = 60.0):
        self.capacity, self.ttl_s = capacity, ttl_s
        self._d: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self.hits = self.misses = self.evictions = self.expired = 0

    def get(self, key: str, now: float | None = None):
        now = time.monotonic() if now is None else now
        item = self._d.get(key)
        if item is not None and now - item[0] <= self.ttl_s:  # 命中且未过期
            self._d.move_to_end(key)  # LRU：命中就提到队尾
            self.hits += 1
            return item[1]
        if item is not None:  # TTL 过期：过期条目绝不能继续占内存
            del self._d[key]
            self.expired += 1
        self.misses += 1
        return None

    def put(self, key: str, val: object, now: float | None = None) -> None:
        if key in self._d:
            self._d.move_to_end(key)
        self._d[key] = (time.monotonic() if now is None else now, val)
        while len(self._d) > self.capacity:  # 容量上限：超出就淘汰最久未用的
            self._d.popitem(last=False)
            self.evictions += 1

    def stats(self) -> str:
        return (f"BoundedLRU cap={self.capacity} size={len(self._d)} hits={self.hits} "
                f"misses={self.misses} evictions={self.evictions} expired={self.expired}")

_CACHE: dict[str, ReqCtx] = {}
_AUDIT: list = []
_CALLBACKS: dict[str, Callable] = {}
_TLS = threading.local()
_ASYNC: dict = {"loop": None, "tasks": []}

def _alive(put: Callable[[int], None] | None = None, n: int = 0) -> int:
    """真实定位手段：gc 里还有多少个请求上下文活着（put 负责造对象）。"""
    for i in range(n):
        put(i)  # type: ignore[misc]
    gc.collect()
    return sum(1 for o in gc.get_objects() if isinstance(o, ReqCtx))

def leak_cache(n: int) -> int:  # ① 全局缓存无上限
    return _alive(lambda i: _CACHE.__setitem__(f"q{i}", ReqCtx(i)), n)

def fix_cache(n: int) -> int:
    cache = BoundedLRU(capacity=32, ttl_s=30.0)
    alive = _alive(lambda i: cache.put(f"q{i}", ReqCtx(i)), n)
    del cache
    return alive

def leak_audit(n: int) -> int:  # ② 全局 list 追加（"只是记个日志"）
    return _alive(lambda i: _AUDIT.append(ReqCtx(i)), n)  # 想记 id，却挂上了整个上下文

def fix_audit(n: int) -> int:
    log: deque = deque(maxlen=64)  # 有界环形缓冲：只留最近 64 条摘要
    alive = _alive(lambda i: log.append({"i": i, "tokens": count_messages(ReqCtx(i).msgs)}), n)
    del log
    return alive

def leak_callbacks(n: int) -> int:  # ③ 闭包 / 回调持有大对象
    def put(i: int) -> None:
        ctx = ReqCtx(i)

        def on_done(result=None, ctx=ctx):  # 闭包把整个请求上下文钉住了
            return ctx.i

        _CALLBACKS[f"req-{i}"] = on_done  # "取消"只改了状态位，回调没注销

    return _alive(put, n)

def fix_callbacks(n: int) -> int:
    def put(i: int) -> None:
        ref = weakref.ref(ReqCtx(i))  # 只持弱引用：请求结束就能被回收

        def on_done(result=None, ref=ref):
            target = ref()
            return target.i if target is not None else -1

        _CALLBACKS[f"req-{i}"] = on_done

    return _alive(put, n)

def _pool_worker(jobs: queue.Queue, clear_per_task: bool) -> None:  # ④ thread-local
    while True:
        i = jobs.get()
        if i is None:
            return
        try:
            # 长生命周期 worker 把 per-request 数据一路攒在 thread-local 上
            _TLS.buffers = getattr(_TLS, "buffers", []) + [ReqCtx(i)]
            if clear_per_task:  # 修复：每个任务结束时显式清理（等价于 finally）
                _TLS.__dict__.pop("buffers", None)
        finally:
            jobs.task_done()

def _run_pool(n: int, clear_per_task: bool) -> int:
    """必须在**池子还活着的时候**采样才是真实泄漏量：线程退出会顺带释放
    thread-local，可真实线程池的线程是不会退出的。"""
    jobs: queue.Queue = queue.Queue()
    threads = [threading.Thread(target=_pool_worker, args=(jobs, clear_per_task)) for _ in range(4)]
    for t in threads:
        t.start()
    for i in range(n):
        jobs.put(i)
    jobs.join()  # 任务全处理完，此刻 worker 仍活着 → 就地采样
    alive = _alive()
    for _ in threads:
        jobs.put(None)
    for t in threads:
        t.join()
    return alive

async def _wait(i: int, ctx: ReqCtx, ev: asyncio.Event) -> int:  # ⑤ 未完成的 task
    await ev.wait()  # 没有结束条件的任务只能靠 cancel 收敛，闭包里钉着 ctx
    return ctx.i

def _task_run(n: int, cleanup: bool) -> int:
    loop = asyncio.new_event_loop()
    _ASYNC["loop"] = loop  # 长驻进程里 loop 一直活着 → 任务永远不被回收
    tasks: list = _ASYNC["tasks"]
    stop = asyncio.Event()

    async def make() -> None:
        for i in range(n):
            tasks.append(loop.create_task(_wait(i, ReqCtx(i), stop)))
        await asyncio.sleep(0.02)

    loop.run_until_complete(make())
    if cleanup:  # 修复：先让任务正常收敛（否则 cancel 的异常 traceback 会钉住协程帧）
        stop.set()
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    else:
        alive = _alive()  # 采样必须在任务还挂着的时候做
    for t in tasks:  # 收尾：任何路径都不能把 pending task 留给即将关闭的 loop
        if not t.done():
            t.cancel()
    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    if cleanup:
        tasks.clear()  # 从任务注册表里摘掉，别让 task 对象继续被持有
        alive = _alive()
    loop.close(); _ASYNC["loop"] = None
    return alive

leak_tls = lambda n: _run_pool(n, clear_per_task=False)  # noqa: E731
fix_tls = lambda n: _run_pool(n, clear_per_task=True)  # noqa: E731
leak_tasks = lambda n: _task_run(n, cleanup=False)  # noqa: E731
fix_tasks = lambda n: _task_run(n, cleanup=True)  # noqa: E731  (④⑤ 的泄漏/修复入口)

# (名称, 成因, 现场特征, 修复写法, 泄漏实现, 修复实现)
CASES: list[tuple] = [
    ("① 全局缓存无上限", "dict 缓存每次请求的 prompt/结果：无容量上限、无 TTL",
     "RSS/堆单调涨不回落；gc 里 ReqCtx 数 = 请求数", "BoundedLRU(cap=32, ttl=30s)",
     leak_cache, fix_cache),
    ("② 全局 list 追加（'只是记个日志'）", "审计日志 append 了整个 ReqCtx，连检索结果一起",
     "内存随请求数线性涨；日志条数 = 请求数", "deque(maxlen=64) 只记摘要",
     leak_audit, fix_audit),
    ("③ 闭包 / 回调持有大对象", "回调闭包捕获了整个 request 上下文，取消时没注销",
     "gc 里 ReqCtx 数持续不降；回调表长度 = 历史请求数", "weakref.ref / unregister",
     leak_callbacks, fix_callbacks),
    ("④ threading.local / 线程池累积", "长生命周期 worker 把 per-request 缓冲挂在 TLS 上",
     "对象按「线程 × 任务数」累积；RSS 可能被分配器掩盖，必须看计数", "任务结束显式清理",
     leak_tls, fix_tls),
    ("⑤ 未完成的 asyncio task", "create_task 后既不 await 也不 cancel，挂死在事件循环上",
     "pending task 数持续增长；RSS 涨但 CPU 不高", "cancel + gather + 摘出注册表",
     leak_tasks, fix_tasks),
]

def _reset_all() -> None:
    """每个 case 之后彻底清场，保证下一个 case 的基线干净。"""
    _CACHE.clear(); _AUDIT.clear(); _CALLBACKS.clear(); _TLS.__dict__.clear()
    tasks, loop = _ASYNC.get("tasks") or [], _ASYNC.get("loop")
    if loop is not None and not loop.is_closed():
        for t in tasks:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
    _ASYNC["tasks"], _ASYNC["loop"] = [], None
    gc.collect()

def run_case(idx: int, case: tuple) -> dict:
    """一个泄漏 case 的完整讲解 + 观测：成因 → 现场特征 → 定位 → 修复。"""
    name, cause, symptom, fix_desc, leak_fn, fix_fn = case
    _reset_all()
    kv(name, "")
    note(f"成因 {cause} ｜ 现场特征 {symptom}")
    tracemalloc.start()
    heap0, rss0 = tracemalloc.get_traced_memory()[0], rss_mb()
    leaked = leak_fn(LEAK_N)
    heap1, rss1 = tracemalloc.get_traced_memory()[0], rss_mb()
    snap = tracemalloc.take_snapshot()
    tracemalloc.stop()
    note(f"定位     : RSS +{rss1 - rss0:.1f}MB / Python 堆 +{(heap1 - heap0) / 2**20:.1f}MB"
         f"（tracemalloc），gc 中存活 ReqCtx={leaked} 个")
    for stat in snap.statistics("lineno")[:5]:  # tracemalloc top-5 分配点
        note(f"           tracemalloc top{idx}: "
             f"{os.path.basename(stat.traceback[0].filename)}:"
             f"{stat.traceback[0].lineno} {fmt_bytes(stat.size)}")
    del snap
    _reset_all()
    note(f"gc.collect() 后仍存活 {leaked} 个（可达 → 这是**泄漏**，不是膨胀）；"
         f"放掉引用后 RSS {rss1:.1f}MB → {rss_mb():.1f}MB")
    fixed = fix_fn(LEAK_N)
    note(f"修复     : {fix_desc} → 残留 ReqCtx={fixed} 个")
    _reset_all()
    return {"leaked": leaked, "fixed": fixed, "rss_delta": rss1 - rss0,
            "heap_delta_mb": (heap1 - heap0) / 2**20}

def _report(tag: str, r: dict) -> None:
    kv(f"{tag} 墙钟/RSS 增长", f"{r['wall_s']:.2f}s / +{r['rss_growth']:.1f}MB {r['series']}")
    kv(f"{tag} 峰值线程 / 峰值队列深度", f"{r['peak_threads']} / {r['peak_depth']}")
    kv(f"{tag} 延迟 / 成功失败", f"{r['stats']}  {r['ok']}/{r['n'] - r['ok']}",
       f"  错误={r['errors']}  成本=${r['usd']:.5f}（超时/失败也照样计费）")

def main() -> int:
    with lab(LAB_ID, "并发一高就 panic / 内存泄漏：五种最常见的生产泄漏与排查修复",
             "生产环境并发量一高 agent 就会 panic，核心原因是什么？"
             "最常见的生产环节内存泄漏有哪几种，如何排查修复？"):
        phase("1. 复现故障", "(240 并发 + 无界排队 + 无脑重试)")
        v0 = avalanche_v0()
        _report("v0", v0)
        print(f"\n{BROKEN} v0：{N_REQUESTS} 并发（一请求一线程 + 256KB 上下文）→ RSS "
              f"+{v0['rss_growth']:.1f}MB、峰值线程 {v0['peak_threads']}、峰值队列 "
              f"{v0['peak_depth']}、P95={v0['stats'].p95:.0f}ms、失败 "
              f"{v0['n'] - v0['ok']}/{v0['n']}（内存涨+延迟涨+错误率涨同时发生）")
        phase("2. 观测 / 归因", "(RSS 曲线 → gc 对象计数 → tracemalloc 分配点)")
        note("传递链：无界队列 → 上下文全量驻留(RSS↑) → 线程数↑ → 上游排队 + 上下文切换")
        note("        → P95↑ → 上游超时 + 重试 → 流量再放大 → 雪崩（正反馈）")
        note(f"v0 现场：峰值线程 {v0['peak_threads']}（≈请求数）、RSS 曲线 {v0['series']}")
        note("归因三件套：① RSS 曲线是否单调涨 ② gc.get_objects() 数对象 ③ tracemalloc 找分配点")
        print(f"\n{BROKEN} 归因定位：RSS 曲线 {v0['series']} 与 peak_threads="
              f"{v0['peak_threads']} 同时抬升 → 瓶颈是「没有上限」，不是「模型太慢」")
        phase("3. 修复", "(有界队列+背压 / BoundedLRU / weakref / 显式 cancel)")
        v1 = avalanche_v1()
        _report("v1", v1)
        results = [run_case(i, c) for i, c in enumerate(CASES, 1)]
        leak_total = sum(r["leaked"] for r in results)
        fix_total = sum(r["fixed"] for r in results)
        print(f"\n{FIX} 有界队列({QUEUE_MAX})+背压+{WORKERS} worker：峰值队列 "
              f"{v1['peak_depth']}、峰值线程 {v1['peak_threads']}、RSS +{v1['rss_growth']:.1f}MB、"
              f"P95={v1['stats'].p95:.0f}ms；五种泄漏残留对象 {leak_total} → {fix_total} 个")
        phase("4. 验证")
        rows = [("rss_growth_mb", v0["rss_growth"], v1["rss_growth"], "{:.1f}"),
                ("peak_queue_depth", float(v0["peak_depth"]), float(v1["peak_depth"]), "{:.0f}"),
                ("peak_threads", float(v0["peak_threads"]), float(v1["peak_threads"]), "{:.0f}"),
                ("p95_latency_ms", v0["stats"].p95, v1["stats"].p95, "{:.0f}"),
                ("cost_usd", v0["usd"], v1["usd"], "{:.5f}"),
                ("retained_objects", float(leak_total), float(fix_total), "{:.0f}")]
        for name, before, after, fmt in rows:
            print(f"{VERIFY} {name}: {fmt.format(before)} -> {fmt.format(after)} "
                  f"({improvement(before, after)})")
        note("工程结论：")
        note("1) 并发 panic 的第一性原因是「没有上限」：队列、线程、缓存、任务都要有界。")
        note("2) 满了要快速失败（背压）；让请求在内存里排队，排队本身就是内存泄漏。")
        note("3) v1 的 P95 改善有限是因为上游容量没变 —— 买到的是确定性和不雪崩。")
        note("4) gc.collect() 能回收的是膨胀、不能回收的才是泄漏；RSS 高 ≠ 泄漏。")
        note("5) 长生命周期容器（全局 dict/list/回调表/thread-local/事件循环）是泄漏高发区。")
        takeaway("并发一高就 panic = 无界；内存一直涨 = 有一处长生命周期容器在攒对象。")
        METRICS.reset()
    return 0

QUESTIONS = [
    "并发一高就 panic 的核心原因？ -> 无界排队 + 无界并发 + 重试放大，内存/线程/延迟/"
    "错误率同时恶化（本 lab 实测四者同时抬升）",
    "最常见的生产内存泄漏有哪几种、怎么排查修复？ -> 全局缓存无上限、全局 list 追加、"
    "闭包回调持有上下文、thread-local/线程池累积、未完成的 asyncio task；排查用 RSS 曲线 + "
    "gc.get_objects() + tracemalloc top 分配点",
    "泄漏和膨胀怎么区分？ -> gc.collect() 后能回收的是膨胀，不能回收的才是泄漏",
]
if __name__ == "__main__":
    sys.exit(main())
