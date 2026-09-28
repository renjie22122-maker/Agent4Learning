"""Lab: 批处理把 CPU 和内存瞬间打满 —— 资源治理。
学习目标问题：agent 在批处理任务的时候，CPU 和内存瞬间打满，这类问题如何解决？
复现的故障（v0"一把梭"批处理）：1. 一次性加载 —— 整批数据 + 中间结果全读进内存，
RSS 随数据量线性上涨，容器 memory limit 一到就是 OOMKilled（不是慢，是直接死）；
2. 无并发上限 —— 一次起几百个任务，provider 队列瞬间打满 → 429 暴增 + 重试风暴，
线程数跟着请求数一起涨；3. CPU 与 IO 同池 —— CPU 密集后处理占住线程池，IO 任务排在
后面干等，"provider 没满，RT 却炸了"；4. 大对象 + GC —— 每个任务造一个带引用环的
临时上下文，高并发下 GC 次数与停顿双爆。
v1 生产做法：流式/分页（一次只驻留一个 chunk）+ 舱壁（批处理最多占 70% 容量，给
交互式留余量）+ CPU/IO 分池（CPU 池 = 核数，IO 池更大）+ 有界队列背压 + 小任务按
token 预算合并 + 可取消（cancel 标志）+ 资源采样器（RSS/线程/队列/CPU 时间序列）。
工程结论：批处理的问题不是"算得慢"，而是**没有边界**——没有内存边界、没有并发边界、
没有队列边界、没有优先级边界。资源治理 = 给每一维都装上闸门并观测它。
"""
from __future__ import annotations
import gc
import json
import os
import queue
import re
import sys, threading, time, zlib, hashlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterator
from agentlab.metrics import METRICS
from agentlab.orchestration import Bulkhead
from agentlab.providers import LLMError, LLMServer, system, user
from agentlab.tokens import count_messages
from agentlab.util import (BROKEN, FIX, VERIFY, Stats, head, improvement, kv, lab, note,
                           phase, run_concurrently, rss_mb, takeaway)
LAB_ID = "lab-17-batch-resource"
N_RECORDS = 30_000          # 批处理数据量（100 万条 = 这个规模 ×33，按 数量×单条大小 外推）
REC_KB = 2                  # 单条记录正文大小
CHUNK = 500                 # 流式处理 chunk 大小（500 × 2KB ≈ 1MB 驻留）
CAPACITY = 20               # 应用给 LLM 的总并发容量
BATCH_LIMIT = int(CAPACITY * 0.7)   # 批处理最多用 70%，剩下 30% 预留给交互式
BATCH_TASKS = 144
MERGE_SIZE = 3              # 小任务合并：3 个小任务 → 1 次 LLM 调用
class Rec:
    """紧凑记录：__slots__ + 定长正文，模拟一条待处理的数据。"""
    __slots__ = ("rid", "tenant", "text", "vec")
    def __init__(self, i: int, kb: int = REC_KB) -> None:
        self.rid, self.tenant, self.vec = i, f"t{i % 3}", [0.5] * 8
        self.text = "缓存穿透 治理 方案 " * (kb * 1024 // 27)
def make_records(n: int, kb: int = REC_KB) -> Iterator[Rec]:
    """生成器：v1 流式处理靠它做到"一次只驻留一个 chunk"。"""
    for i in range(n):
        yield Rec(i, kb)
def chunked(it, size: int = CHUNK) -> Iterator[list]:
    """把任意可迭代对象切块 —— 流式批处理的地基。"""
    buf: list = []
    for x in it:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
def table(title: str, headers: list[str], widths: list[int], rows: list[list[Any]]) -> None:
    def pad(s: Any, w: int) -> str:
        s = str(s)
        return s + " " * max(0, w - sum(2 if "\u4e00" <= c <= "\u9fff" else 1 for c in s))
    print(f"\n  ┌─ {title} " + "─" * max(0, 60 - len(title) * 2))
    print("  │ " + "  ".join(pad(h, w) for h, w in zip(headers, widths)))
    for r in rows:
        print("  │ " + "  ".join(pad(c, w) for c, w in zip(r, widths)))
    print("  └" + "─" * 74)
def ver(name: str, before: float, after: float, lower: bool = True) -> None:
    print(f"{VERIFY} {name}: {before} -> {after} "
          f"({improvement(before, after, lower_is_better=lower)})")
class GCMeter:
    """用 gc.callbacks 真实测量 GC 次数与耗时（start→stop 的时间差）。"""
    def __init__(self) -> None:
        self.n, self.ms, self._t0 = 0, 0.0, 0.0
    def _cb(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._t0 = time.perf_counter()
        else:
            self.n, self.ms = self.n + 1, self.ms + (time.perf_counter() - self._t0) * 1000.0
    def __enter__(self) -> "GCMeter":
        gc.collect()
        gc.callbacks.append(self._cb)
        return self
    def __exit__(self, *exc) -> None:
        gc.callbacks.remove(self._cb)
class ResourceSampler:
    """后台线程定期采样 RSS / 线程数 / 队列深度 / CPU 时间 —— "如何发现被打满"的答案。"""
    def __init__(self, interval: float = 0.1, depth=None) -> None:
        self.interval, self.depth = interval, depth
        self.rows: list[tuple[float, float, int, int, float]] = []
        self._stop = threading.Event()
        self.peak_rss = self.peak_threads = self.peak_depth = 0
    def _run(self) -> None:
        t0, cpu0 = time.perf_counter(), time.process_time()
        while not self._stop.is_set():
            rss, th, d = rss_mb(), threading.active_count(), self.depth() if self.depth else 0
            self.rows.append((time.perf_counter() - t0, rss, th, d,
                              (time.process_time() - cpu0) * 1000.0))
            self.peak_rss = max(self.peak_rss, rss)
            self.peak_threads, self.peak_depth = max(self.peak_threads, th), max(self.peak_depth, d)
            self._stop.wait(self.interval)
    def __enter__(self) -> "ResourceSampler":
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()
        return self
    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._th.join(timeout=1.0)
    def render(self, title: str) -> None:
        step = max(1, len(self.rows) // 8)
        rows = [[f"{t:.2f}s", f"{r:.1f}", str(th), str(d), f"{c:.0f}"]
                for t, r, th, d, c in self.rows[::step][:8]]
        table(title, ["t", "RSS(MB)", "线程", "队列", "CPU(ms)"], [8, 10, 8, 8, 10], rows)
# --- 1. 复现 -----------------------------------------------------------------
def demo_memory(stream: bool) -> dict:
    """1a 一次性加载 vs 3a chunked 流式：RSS 曲线、峰值增量、GC 次数与耗时。"""
    base, peak = rss_mb(), 0.0
    with GCMeter() as m:
        if stream:                                   # v1：一次只驻留一个 chunk
            for ch in chunked(make_records(N_RECORDS), CHUNK):
                chars = sum(len(r.text) for r in ch)  # 只保留聚合值，不保留对象
                peak = max(peak, rss_mb() - base)
                del ch, chars
        else:                                        # v0：整批 + 中间结果全进内存
            recs: list[Rec] = []
            curve = []
            for _ in range(5):
                recs.extend(make_records(N_RECORDS // 5))
                curve.append(rss_mb() - base)
            staging = [r.text.replace(" ", "_") for r in recs]   # 中间结果再复制一份
            peak = rss_mb() - base
            note("RSS 曲线（每处理 1/5 数据后的增量）: "
                 + ", ".join(f"{mb:.0f}MB" for mb in curve) + f", {peak:.0f}MB(+staging)")
            note(f"中间结果 staging 又复制了一份：{len(staging)} 条 × {REC_KB}KB")
            del staging, recs
    gc.collect()
    return {"peak": peak, "gc_n": m.n, "gc_ms": m.ms, "extrap": peak * 33.3 / 1024}
def batch_call(srv: LLMServer, i: int, timeout: float = 0.6, retries: int = 0,
               tag: str = "batch") -> dict:
    """一次批处理子任务：调 LLM。``retries=2`` 复现 429 重试风暴。"""
    msgs = [system("批处理 worker"), user(f"处理第 {i} 条")]
    t0, last = time.perf_counter(), ""
    for attempt in range(retries + 1):
        try:
            srv.call(msgs, model="small-8b", timeout=timeout, tag=tag)
            return {"ok": True, "err": "", "attempts": attempt + 1,
                    "ms": (time.perf_counter() - t0) * 1000.0}
        except LLMError as exc:
            last = exc.code
            if last != "429" or attempt == retries:
                break
            time.sleep(0.05 * (attempt + 1))
    return {"ok": False, "err": last, "attempts": retries + 1,
            "ms": (time.perf_counter() - t0) * 1000.0}
def _summarize(res: list, wall: float, smp: ResourceSampler, extra: dict) -> dict:
    ok = [r for r in res if isinstance(r, dict) and r["ok"]]
    errs: dict[str, int] = {}
    for r in res:
        if isinstance(r, dict) and not r["ok"]:
            errs[r["err"]] = errs.get(r["err"], 0) + 1
    lat = [r["ms"] for r in res if isinstance(r, dict)]  # 失败也要记耗时，否则统计骗人
    tries = sum(r.get("attempts", 1) for r in res if isinstance(r, dict))
    return {"wall": wall, "ok": len(ok), "errs": errs, "stats": Stats(lat), "tries": tries,
            "peak_threads": smp.peak_threads, "peak_queue": smp.peak_depth, **extra}
def demo_concurrency(bounded: bool, n: int = 240) -> dict:
    """1b/3b 一次丢出 n 个任务：无上限（429→重试风暴）vs 舱壁限流（闸门=线程上限）。"""
    srv = LLMServer(max_queue=32, max_wait_s=5.0, seed=11)
    srv.set_latency("small-8b", 45)
    bh = Bulkhead("batch_llm", limit=CAPACITY - 4, wait_s=0.05) if bounded else None
    def task(i: int) -> dict:
        if bh is None:
            return batch_call(srv, i, retries=2)
        if not bh.acquire():
            return {"ok": False, "err": "BULKHEAD_FULL", "attempts": 1, "ms": 0.0}
        try:
            return batch_call(srv, i, timeout=1.5, retries=0)
        finally:
            bh.release()
    with ResourceSampler(0.01, lambda: srv.stats["queued_now"]) as smp:
        t0 = time.perf_counter()
        res = run_concurrently(task, n, CAPACITY - 4 if bounded else 200)
        wall = time.perf_counter() - t0
    extra = {"provider": srv.summary_lines()}
    if bh is not None:
        extra.update({"rejected": bh.rejected, "bulkhead": bh.stats()})
    return _summarize(res, wall, smp, extra)
_JSON_BLOB = json.dumps({"text": "缓存穿透 治理 方案 限流 熔断 " * 40})
def cpu_postprocess(rounds: int = 250) -> int:
    """1c CPU 密集的真实后处理：JSON 解析 + 正则清洗 + 压缩归档 + 内容哈希。
    注意：纯 Python 代码持有 GIL，分池只能解决"排队"，不能解决"并行"；这里用
    zlib/hashlib 这类会释放 GIL 的 C 实现，才是生产上真实的 CPU 密集形态
    （要真多核并行，纯 Python 逻辑必须上进程池或 C 扩展）。
    """
    acc = 0
    for _ in range(rounds):
        obj = json.loads(_JSON_BLOB)
        s = re.sub(r"\s+", " ", obj["text"])
        raw = s.encode("utf-8") * 40
        acc += len(zlib.compress(raw, 1)) + int(hashlib.sha256(raw).hexdigest()[:8], 16)
    return acc
def demo_cpu_io(shared_pool: bool) -> tuple[float, float]:
    """1c/3c CPU 密集后处理与 IO 任务：同池 vs 分池，看 IO 的 P95 被拖成什么样。"""
    cpu_n, io_n = 16, 24
    io_lat: list[float] = []
    lock = threading.Lock()
    def run_io(pool: ThreadPoolExecutor) -> None:
        submitted = time.perf_counter()   # 延迟必须从提交时刻算，否则测不到排队时间
        def one(i: int) -> None:
            time.sleep(0.04)
            with lock:
                io_lat.append((time.perf_counter() - submitted) * 1000.0)
        list(pool.map(one, range(io_n)))
    t0 = time.perf_counter()
    if shared_pool:                       # v0：一个池子，CPU 任务先把 worker 占满
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="pool") as pool:
            cpus = [pool.submit(cpu_postprocess) for _ in range(cpu_n)]
            run_io(pool)
            [c.result() for c in cpus]
    else:                                 # v1：CPU 池 = 核数，IO 池单独放大
        cpu_pool = ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 4),
                                      thread_name_prefix="cpu")
        io_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="io")
        cpus = [cpu_pool.submit(cpu_postprocess) for _ in range(cpu_n)]
        run_io(io_pool)
        [c.result() for c in cpus]
        cpu_pool.shutdown()
        io_pool.shutdown()
    return time.perf_counter() - t0, Stats(io_lat).p95
def _cycle_ctx(children: int = 1200) -> dict:
    """任务上下文：子节点回指 parent（traceback / 闭包 / 回调都是这样）。有引用环 →
    引用计数回收不掉，只能等 GC —— 这才是真实 GC 压力的来源。"""
    root: dict = {"name": "root", "children": []}
    for k in range(children):
        root["children"].append({"i": k, "p": "z" * 300, "parent": root})
    return root
def demo_gc_buffers(n: int = 240, workers: int = 24, reuse: bool = False) -> tuple[float, int, float]:
    """1d/3e 每个任务造一个 ~0.7MB 带环的临时上下文；复用则每线程只造一个。"""
    base = rss_mb()
    tl = threading.local()
    def task(_i: int) -> int:
        ctx = getattr(tl, "buf", None)
        if reuse and ctx is not None:          # v1：复用，不再制造垃圾
            ctx["children"][0]["i"] += 1
            return len(ctx["children"])
        tmp = _cycle_ctx()                     # v0：每次新建一个带环的上下文
        tl.buf = tmp if reuse else None
        return len(tmp["children"])
    with GCMeter() as m:
        run_concurrently(task, n, workers)
        peak = rss_mb() - base
    gc.collect()
    return peak, m.n, m.ms
# --- 3. 修复 -----------------------------------------------------------------
def run_batch_load(reserve: bool) -> dict:
    """3c 批处理负载 + 交互式负载同时打；reserve=True 时批处理只用 70% 容量 + 合并小任务。"""
    srv = LLMServer(max_queue=256, max_wait_s=5.0, seed=11)
    bh = Bulkhead("batch_llm", limit=BATCH_LIMIT, wait_s=1.0)
    inter: list[float] = []
    ilock = threading.Lock()
    def interactive(_i: int) -> None:
        for k in range(2):
            t0 = time.perf_counter()
            try:
                srv.call([user(f"交互式提问 {k}")], model="small-8b", timeout=2.0, tag="inter")
            except LLMError:
                pass
            with ilock:
                inter.append((time.perf_counter() - t0) * 1000.0)
            time.sleep(0.02)
    driver = threading.Thread(target=lambda: (time.sleep(0.03), run_concurrently(interactive, 6, 6)),
                              daemon=True)
    t0 = time.perf_counter()
    driver.start()
    if reserve:
        groups = [list(range(j, min(j + MERGE_SIZE, BATCH_TASKS)))
                  for j in range(0, BATCH_TASKS, MERGE_SIZE)]
        res = run_concurrently(lambda i: _merged_call(srv, bh, groups[i]), len(groups),
                               len(groups))
    else:
        res = run_concurrently(lambda i: batch_call(srv, i, timeout=2.0), BATCH_TASKS,
                               BATCH_TASKS)
    wall = time.perf_counter() - t0
    driver.join(timeout=10)
    ok = sum(r.get("n", 1) for r in res if isinstance(r, dict) and r["ok"])
    return {"wall": wall, "ok": ok, "items": BATCH_TASKS, "inter": Stats(inter),
            "usd": srv.ledger.usd, "calls": srv.ledger.calls, "bh": bh.stats(),
            "provider": srv.summary_lines()}
def _merged_call(srv: LLMServer, bh: Bulkhead, group: list[int]) -> dict:
    """3c 批量合并：把多个小任务塞进一次调用（受 token 预算约束）。"""
    msgs = [system("批处理 worker（合并模式）"),
            user("；".join(f"处理第 {i} 条" for i in group))]
    if not bh.acquire():
        return {"ok": False, "err": "BULKHEAD_FULL", "ms": 0.0, "n": len(group)}
    try:
        t0 = time.perf_counter()
        srv.call(msgs, model="small-8b", timeout=2.0, tag="batch_merged")
        return {"ok": True, "err": "", "ms": (time.perf_counter() - t0) * 1000.0,
                "n": len(group), "tokens": count_messages(msgs)}
    except LLMError as exc:
        return {"ok": False, "err": exc.code, "ms": 0.0, "n": len(group)}
    finally:
        bh.release()
def demo_backpressure(n: int = 120, qmax: int = 8) -> dict:
    """3f v0 无界缓冲 vs v1 有界队列：生产快于消费时必须阻塞或拒绝。"""
    unbounded = list(range(n))          # v0：无界缓冲，深度 = 生产总量
    q: queue.Queue = queue.Queue(maxsize=qmax)
    state = {"rejected": 0, "blocked": 0, "depth": 0}
    done = threading.Event()
    def consumer() -> None:
        while not (done.is_set() and q.empty()):
            try:
                q.get(timeout=0.02)
                time.sleep(0.004)          # 消费速度故意慢于生产速度
            except queue.Empty:
                pass
    ct = threading.Thread(target=consumer, daemon=True)
    ct.start()
    for i in range(n):
        try:
            q.put_nowait(i)                # 先试非阻塞：满 = 触发背压
        except queue.Full:                 # 要么阻塞等待，要么直接拒绝
            state["blocked"] += 1
            try:
                q.put(i, timeout=0.05)
            except queue.Full:
                state["rejected"] += 1
        state["depth"] = max(state["depth"], q.qsize())
    done.set()
    ct.join(timeout=3)
    return {"unb_peak": len(unbounded), "qmax": qmax, "maxdepth": state["depth"],
            "blocked": state["blocked"], "rejected": state["rejected"], "produced": n}
def demo_cancel(n: int = 64, workers: int = 16, cancel_after_s: float = 0.09) -> dict:
    """3g 可取消：批处理跑到一半被抢占（cancel 标志 + 协作式检查）。"""
    cancel = threading.Event()
    def task(_i: int) -> str:
        if cancel.is_set():
            return "cancelled"
        time.sleep(0.05)
        return "done"
    timer = threading.Timer(cancel_after_s, cancel.set)  # 抢占信号
    t0 = time.perf_counter()
    timer.start()
    res = run_concurrently(task, n, workers)
    wall = time.perf_counter() - t0
    timer.cancel()
    done = sum(1 for r in res if r == "done")
    return {"done": done, "cancelled": n - done, "wall": wall, "full_wall": n / workers * 0.05}
def main() -> int:
    with lab(LAB_ID, "批处理把 CPU 和内存瞬间打满：资源治理",
             "agent 在批处理任务的时候，CPU 和内存瞬间打满，这类问题如何解决？"):
        phase("1. 复现故障", "(v0 一把梭)")
        big = demo_memory(stream=False)
        kv("1a 一次性加载峰值增量（基线 %.0fMB）" % rss_mb(), f"{big['peak']:.0f}MB",
           f"（GC {big['gc_n']} 次 {big['gc_ms']:.0f}ms；外推 100 万条 {big['extrap']:.1f}GB→OOM）")
        unb = demo_concurrency(bounded=False)
        kv("1b 无上限 240 任务线程峰值", unb["peak_threads"],
           f"（排队峰值 {unb['peak_queue']}，429 {unb['errs'].get('429', 0)}/{unb['tries']} 次调用）"
           f" 延迟 {unb['stats']}")
        wall0, io_p95_0 = demo_cpu_io(shared_pool=True)
        kv("1c CPU+IO 同池：IO 任务 P95", f"{io_p95_0:.0f}", f"ms（整批 {wall0:.2f}s）")
        gc_peak0, gc_n0, gc_ms0 = demo_gc_buffers(reuse=False)
        kv("1d 240×1MB 临时对象：峰值", f"{gc_peak0:.0f}", f"MB / GC {gc_n0} 次 {gc_ms0:.0f}ms")
        print(f"\n{BROKEN} 一次性加载峰值 {big['peak']:.0f}MB（外推 100 万条 {big['extrap']:.1f}GB）"
              f" / 线程峰值 {unb['peak_threads']} / 429 {unb['errs'].get('429', 0)} 次 / "
              f"IO P95 被 CPU 拖到 {io_p95_0:.0f}ms / GC {gc_ms0:.0f}ms")
        phase("2. 观测 / 归因", "(ResourceSampler 时间序列)")
        srv2 = LLMServer(max_queue=8, max_wait_s=5.0, seed=5)
        srv2.set_latency("small-8b", 45)  # 快模型：问题在排队而不在算力
        with ResourceSampler(0.08, lambda: srv2.stats["queued_now"]) as smp:
            run_concurrently(lambda i: batch_call(srv2, i, timeout=0.8), 120, 120)
        smp.render("批处理期间的资源时间序列（v0：无上限）")
        kv("RSS / 线程 / 队列 峰值",
           f"{smp.peak_rss:.1f}MB / {smp.peak_threads} / {smp.peak_depth}")
        note("没有这四列就只能说'任务卡了'；有了它才分得清是线程爆了、队列积压了，还是 CPU")
        note("时间斜率已打满（≈核数×1000ms/s 就是饱和信号）。")
        METRICS.render("资源类指标（v0）", include=["bulkhead_", "llm_inflight", "llm_queued"])
        phase("3. 修复", "(v1 生产做法)")
        small = demo_memory(stream=True)
        kv("3a 流式处理峰值增量", f"{small['peak']:.1f}MB",
           f"（{N_RECORDS // CHUNK} 个 chunk，GC {small['gc_n']} 次 {small['gc_ms']:.0f}ms）")
        bnd = demo_concurrency(bounded=True)
        kv("3b 舱壁限流 240 任务：线程峰值", bnd["peak_threads"],
           f"（429 {bnd['errs'].get('429', 0)} 次；{bnd['bulkhead']}）  延迟 {bnd['stats']}")
        v0 = run_batch_load(reserve=False)
        v1 = run_batch_load(reserve=True)
        kv("3c v0 批处理 144 次裸调用", f"{v0['ok']}/{v0['items']} 成功 {v0['wall']:.2f}s")
        kv(f"   v1 舱壁 {BATCH_LIMIT}/{CAPACITY}+合并 {MERGE_SIZE}",
           f"{v1['ok']}/{v1['items']} 成功 {v1['wall']:.2f}s（{v1['calls']} 次调用）")
        note(f"v0 舱壁 {v0['bh']} / v1 舱壁 {v1['bh']}"
             f"（交互式只受预留的 {CAPACITY - BATCH_LIMIT} 个槽影响）")
        wall1, io_p95_1 = demo_cpu_io(shared_pool=False)
        kv("3d CPU/IO 分池：IO 任务 P95", f"{io_p95_1:.0f}", f"ms（整批 {wall1:.2f}s）")
        note("代价/边界：分池解决的是『排队』；纯 Python 的 CPU 逻辑仍受 GIL 限制，要真多核")
        note("并行必须上进程池或释放 GIL 的 C 实现 —— 本 lab 的后处理正是后者。")
        gc_peak1, gc_n1, gc_ms1 = demo_gc_buffers(reuse=True)
        kv("3e 临时对象复用：峰值 / GC", f"{gc_peak1:.0f}MB / {gc_n1} 次 {gc_ms1:.0f}ms")
        bp = demo_backpressure()
        kv("3f 背压：缓冲深度 无界→有界", f"{bp['unb_peak']} -> {bp['maxdepth']}",
           f"（上限 {bp['qmax']}，背压 {bp['blocked']} 次，拒绝 {bp['rejected']} 次）")
        cx = demo_cancel()
        kv("3g 可取消：完成/取消", f"{cx['done']} / {cx['cancelled']}",
           f"墙钟 {cx['wall']:.2f}s（跑完需 {cx['full_wall']:.2f}s）")
        print(f"\n{FIX} 流式峰值 {small['peak']:.1f}MB / 线程峰值 {bnd['peak_threads']} / 吞吐 "
              f"{v1['items'] / v1['wall']:.1f} 条/s / 交互式 P95 {v1['inter'].p95:.0f}ms")
        table("资源治理参数表（本 lab 核心交付物）",
              ["参数", "建议值", "依据", "超了会怎样"], [16, 16, 26, 26],
              [["chunk_size", "500 条(≈1MB)", "单 chunk 内存 × 并发数", "峰值内存线性上涨→OOM"],
               ["批处理并发上限", f"{BATCH_LIMIT}/{CAPACITY}(70%)", "给交互式留 30% 容量", "交互式 P95 被拖垮"],
               ["cpu pool size", "os.cpu_count()", "CPU 密集受核数+GIL 限制", "上下文切换，吞吐不涨"],
               ["io pool size", "4× 核数 / 并发预算", "IO 等待不占 CPU", "线程暴涨，栈内存吃光"],
               ["队列上限", "2× 并发(≈32)", "背压：满则阻塞或拒绝", "无界缓冲=延迟内存双爆"],
               ["单任务内存上限", "8MB，buffer 复用", "短生命周期大对象=GC 压力", "GC 停顿 + 峰值翻倍"],
               ["LLM 调用超时", "1.5s（P99 附近）", "孤儿线程仍占并发槽", "槽位被慢调用占死"],
               ["批合并 token 预算", "≤4k tokens/批", "模型上下文 + 单批重试代价", "单批失败放大成本"]])
        phase("4. 验证", "(VERIFY)")
        note(f"v0 provider: {v0['provider'][1]}  |  v1 provider: {v1['provider'][1]}")
        ver("peak_rss_mb", round(big["peak"], 1), round(small["peak"], 1))
        ver("peak_threads", unb["peak_threads"], bnd["peak_threads"])
        ver("interactive_p95_ms", round(v0["inter"].p95, 1), round(v1["inter"].p95, 1))
        ver("batch_throughput_per_s", round(BATCH_TASKS / v0["wall"], 1),
            round(BATCH_TASKS / v1["wall"], 1), lower=False)
        ver("gc_time_ms", round(gc_ms0, 1), round(gc_ms1, 1))
        ver("io_p95_ms_cpu_io_shared_vs_split", round(io_p95_0, 1), round(io_p95_1, 1))
        ver("batch_cost_usd", round(v0["usd"], 6), round(v1["usd"], 6))
        ver("unbounded_queue_depth", bp["unb_peak"], bp["maxdepth"])
        for line in ("1) 内存：chunked 流式让峰值从 O(数据量) 降到 O(chunk)；中间结果别复制整份。",
                     "2) 并发：舱壁是硬闸门，必须**预留**给交互式（70/30），否则批处理永远赢。",
                     "3) 池：CPU 密集与 IO 密集必须分池，CPU 池 = 核数；同池 = IO 排在后面干等。",
                     "4) 队列必须有界：满了就阻塞或拒绝（背压），无界缓冲只是把 OOM 推迟几分钟。",
                     "5) 合并小任务：结构开销被摊薄，调用数下降 → 吞吐上升、成本下降。",
                     "6) 可取消/可抢占：批处理要能被交互式抢占，否则它会把整个系统拖死。",
                     "7) 资源采样器（RSS/线程/队列/CPU）是『如何发现被打满』的唯一答案。"):
            note(line)
        takeaway("批处理治理 = 流式内存边界 + 舱壁并发边界 + CPU/IO 分池 + 有界队列背压 + 资源采样。")
        METRICS.reset()
    return 0
QUESTIONS = [
    "agent 批处理时 CPU 和内存瞬间打满，如何解决？ -> chunked 流式 + Bulkhead 舱壁"
    "（70% 容量预留）+ CPU/IO 分池 + 有界队列背压 + 资源采样",
    "为什么'一次性加载'在容器里会直接 OOMKilled 而不是变慢？ -> 内存是硬限制，"
    "超过 memory limit 内核直接杀进程，取决于峰值而非平均值",
    "为什么 CPU 密集和 IO 密集不能共用一个线程池？ -> CPU 任务占满 worker，IO 任务排队"
    "干等；分池后 IO P95 由 IO 池大小决定，而不是由 CPU 工作量决定",
    "批处理如何在资源紧张时让位于交互式请求？ -> 容量预留（70/30）+ 可取消标志 + 优先级",
    "如何发现系统正在被打满？ -> ResourceSampler 时间序列：RSS/线程数/队列深度/CPU 斜率",
]
if __name__ == "__main__":
    sys.exit(main())
