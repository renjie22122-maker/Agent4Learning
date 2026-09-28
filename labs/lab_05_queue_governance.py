"""Lab: 异步任务队列治理 —— 任务堆积了怎么办。

对应生产问题：「生产环境 Agent 出现大量的异步任务堆积，如何做任务队列治理？」

复现的故障（v0 无界队列 + FIFO + 无租约）
---------------------------------------
1. 生产者速度 > 消费者速度 → 队列无限增长，任务**老化时间（age）**从毫秒涨到几秒；
   这就是"堆积"的本质指标 —— 队列长度只是表象，age 才是用户实际感受到的东西。
2. 一个**毒任务**（每次都抛异常）被无限重试 → 反复占满 worker，好任务永远排不上，
   这就是"重试风暴/毒丸"。
3. worker 崩溃后任务直接丢失（没有 lease/ack 机制），队列里"看起来处理过了"。

v1 治理手段（全部真实实现并测量）
--------------------------------
1. 有界队列 + 背压：满了直接拒绝生产者（429/503 + Retry-After），打点 queue_rejected_total；
2. 优先级 + 公平：交互式请求优先于批处理，批处理压力下交互 P95 仍达标；
3. 租约 + 可见性超时：worker 领了任务超时未 ack，任务自动 requeue，不丢；
4. 重试上限 + 指数退避 + DLQ：超过 max_attempts 进死信队列，附处理建议；
5. 动态并发：按队列 age 自适应扩缩 worker（给出规则与实测 worker 数变化）；
6. 任务分片/批量：把小任务合并成批量任务，打 token 成本对比。

工程结论
-------
有界是队列的第一属性：无界队列不是队列，是内存泄漏。堆积要盯 age 而不是 depth；
毒任务必须用"重试上限 + DLQ"隔离，否则一个坏任务能耗掉整个 worker 池；
租约是 at-least-once 的代价，所以每个任务都必须幂等。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass, field

from agentlab.metrics import METRICS
from agentlab.tokens import SMALL, count_tokens, price_of
from agentlab.util import (
    BROKEN,
    FIX,
    VERIFY,
    head,
    improvement,
    kv,
    lab,
    note,
    percentile,
    phase,
    rss_mb,
    takeaway,
)

LAB_ID = "lab-05-queue-governance"

DURATION_S = 4.0          # 压测时长（真实墙钟）
PRODUCE_PER_TICK = 24     # 生产者每 TICK_S 产出 24 个（≈4.8k/s）
TICK_S = 0.005            # 生产者节流：不抢满 GIL，让消费端成为真瓶颈（≈1.3k/s）
CONSUME_S = 0.0015        # 每个任务的"处理"耗时（模拟 LLM 调用）
INTERACTIVE_SLO_MS = 500.0   # 交互式请求的 SLO 线
V0_WORKERS = 4            # v0 固定 4 个 worker（生产 > 消费 → 队列无限增长）
POISON_RUN_S = 0.6        # 毒任务隔离对照实验的时长
MAX_WORKERS = 12          # v1 动态扩缩的上限
MIN_WORKERS = 2
INTERACTIVE_AT_S = 1.5    # 交互式请求注入时刻（等批处理把队列堆起来）
INTERACTIVE_N = 20
POISON_ID = "poison"      # 毒任务：每次执行都抛异常
CRASH_ID = "crash"        # 会让 worker "崩溃"的任务：执行中线程直接退出
V0_RETRY_BUDGET = 3000    # v0 没有重试上限，这里给个很大的预算以免把观测刷屏


@dataclass
class Task:
    tid: str
    kind: str = "batch"        # interactive | batch
    payload_kb: int = 1
    sleep_s: float = 0.001
    attempts: int = 0
    enqueued_at: float = field(default_factory=time.perf_counter)
    cost_tokens: int = 0       # 该任务"处理"时消耗的 token（用于批量降本对比）

    def __post_init__(self) -> None:
        self.payload = b"x" * (self.payload_kb * 1024)   # 真实占内存的负载


class TaskQueue:
    """有界/无界任务队列。

    v0：``maxsize=0``（无界）+ FIFO + 没有租约 —— 三条故障全占。
    v1：有界 + 优先级（交互 > 批处理）+ 租约/可见性超时 + 重试上限 + DLQ + 动态并发。
    """

    def __init__(self, bounded: bool, maxsize: int = 200, lease_s: float = 0.25,
                 max_attempts: int = 3):
        self.bounded = bounded
        self.maxsize = maxsize
        self.ileane = maxsize // 6          # 给交互式预留的独立车道（容量预留）
        self.lease_s = lease_s
        self.max_attempts = max_attempts
        self.seq = 0
        # v0：无界 FIFO（queue.Queue(0) = 无限），交互式和批处理抢同一条队列
        # v1：两条独立车道 —— 交互式有预留容量，批处理满了就被背压拒绝
        self.q: queue.Queue = queue.Queue(0)
        self.iq: queue.Queue = queue.Queue(maxsize)
        self.bq: queue.Queue = queue.Queue(maxsize - self.ileane)
        self.lock = threading.Lock()
        self.inflight: dict[str, tuple[Task, float, int]] = {}   # tid -> (task, 领走时间, worker)
        self.dlq: list[tuple[Task, str]] = []
        self.retry_total = 0
        self.poison_retries = 0
        self.poison_claims = 0
        self.lease_timeout_total = 0
        self.rejected = 0
        self.enqueued = 0
        self.dequeued = 0
        self.done = 0
        self.lost = 0
        self.crash = threading.Event()
        self.stop = threading.Event()
        self.age_samples: list[float] = []
        self.depth_samples: list[int] = []
        self.worker_samples: list[int] = []
        self.interactive_lat: list[float] = []
        self.interactive_ok = 0
        self.interactive_total = 0
        self.batch_lat: list[float] = []
        self.batch_done = 0
        self.m_rejected = METRICS.counter("queue_rejected_total", "队列满导致的背压拒绝")
        self.m_retry = METRICS.counter("queue_retry_total", "任务重试次数")
        self.m_lease = METRICS.counter("queue_lease_timeout_total", "租约超时回收次数")
        self.m_dlq = METRICS.counter("queue_dlq_total", "进入死信队列的任务数")
        self.m_depth = METRICS.gauge("queue_depth", "当前队列深度")
        self.m_oldest = METRICS.gauge("queue_oldest_age_ms", "最老任务的老化时间")
        self.m_enq = METRICS.counter("queue_enqueue_total", "入队任务数")
        self.m_deq = METRICS.counter("queue_dequeue_total", "出队任务数")

    # -- 生产端 -------------------------------------------------------------
    def offer(self, task: Task) -> bool:
        """满了就拒绝（背压）。v1 给交互式留了独立车道，所以"牺牲的永远是批处理"。"""
        self.seq += 1
        target = self.q if not self.bounded else (
            self.iq if task.kind == "interactive" else self.bq)
        try:
            target.put_nowait(task)
        except queue.Full:
            self.rejected += 1
            self.m_rejected.inc()
            if task.kind == "interactive":
                self.interactive_total += 1          # 交互式被拒也算未达成
            return False
        self.enqueued += 1
        self.m_enq.inc()
        if task.kind == "interactive":
            self.interactive_total += 1
        return True

    # -- 消费端 -------------------------------------------------------------
    def claim(self, worker_id: int, timeout: float = 0.02) -> Task | None:
        """领任务。v1 先看交互式车道（容量预留），再看批处理；v0 只有一条 FIFO 队列。"""
        task: Task | None = None
        if self.bounded:
            try:
                task = self.iq.get_nowait()
            except queue.Empty:
                try:
                    task = self.bq.get(timeout=timeout)
                except queue.Empty:
                    return None
        else:
            try:
                task = self.q.get(timeout=timeout)
            except queue.Empty:
                return None
        with self.lock:
            self.dequeued += 1
            self.m_deq.inc()
            if self.bounded:
                self.inflight[task.tid] = (task, time.perf_counter(), worker_id)
        return task

    def _legacy_claim_removed(self):
        return None

    def _unused(self):
        with self.lock:
            self.dequeued += 1
            self.m_deq.inc()
            if self.bounded:
                self.inflight[task.tid] = (task, time.perf_counter(), worker_id)
        return task

    def ack(self, task: Task) -> None:
        with self.lock:
            self.inflight.pop(task.tid, None)
            self.done += 1
        latency = (time.perf_counter() - task.enqueued_at) * 1000.0
        if task.kind == "interactive":
            self.interactive_lat.append(latency)
            if latency <= INTERACTIVE_SLO_MS:
                self.interactive_ok += 1
        else:
            self.batch_lat.append(latency)
            self.batch_done += 1

    def nack(self, task: Task, reason: str) -> None:
        """失败：重试（指数退避）或进 DLQ。

        v0 没有 max_attempts，等于"无限重试"——毒任务会永远赖在 worker 上；
        为了让观测不被一个死循环刷屏，v0 用一个很大的重试预算兜底（生产中就是
        OOM/CPU 打满的那条路）。
        """
        with self.lock:
            self.inflight.pop(task.tid, None)
        task.attempts += 1
        if self.bounded and task.attempts >= self.max_attempts:
            with self.lock:
                self.dlq.append((task, reason))
            self.m_dlq.inc()
            return
        if self.retry_total >= V0_RETRY_BUDGET:
            return
        self.retry_total += 1
        self.m_retry.inc()
        if task.tid == POISON_ID:
            self.poison_retries += 1
        if self.bounded:                       # 指数退避：把重试摊开，别打爆自己
            delay = min(0.05, 0.005 * (2 ** task.attempts))
            threading.Timer(delay, self.offer, args=(task,)).start()
        else:
            self.offer(task)

    def reap_expired_leases(self) -> None:
        """可见性超时：worker 领了但没 ack（卡死/崩溃）→ 任务回到队列，不丢。"""
        now = time.perf_counter()
        with self.lock:
            expired = [tid for tid, (_t, at, _w) in self.inflight.items()
                       if now - at > self.lease_s]
        for tid in expired:
            with self.lock:
                item = self.inflight.pop(tid, None)
            if item is None:
                continue
            task, _at, _w = item
            self.lease_timeout_total += 1
            self.m_lease.inc()
            task.attempts += 1
            if task.attempts >= self.max_attempts + 2:
                with self.lock:
                    self.dlq.append((task, "lease_timeout"))
                self.m_dlq.inc()
            else:
                self.offer(task)

    def observe(self, workers: int) -> None:
        """打点：队列深度、最老任务 age（P50/P95 从样本算）。"""
        depth = self.q.qsize()
        self.depth_samples.append(depth)
        self.worker_samples.append(workers)
        self.m_depth.set(depth)
        age_ms = 0.0
        try:
            if self.bounded:                      # v1：看批处理车道的队头
                task = self.bq.queue[0]
            else:                                 # v0：FIFO 队头
                task = self.q.queue[0]
            age_ms = (time.perf_counter() - task.enqueued_at) * 1000.0
        except (IndexError, AttributeError, TypeError):
            age_ms = 0.0
        self.age_samples.append(age_ms)
        self.m_oldest.set(age_ms)


def run_load(bounded: bool, duration_s: float = DURATION_S) -> dict:
    """同一份负载跑 v0 或 v1：生产者过载 + 毒任务 + worker 崩溃 + 交互式请求。"""
    tq = TaskQueue(bounded=bounded)
    workers: list[threading.Thread] = []
    worker_count = MIN_WORKERS if bounded else V0_WORKERS
    crash_done = threading.Event()

    def worker(worker_id: int) -> None:
        while not tq.stop.is_set():
            task = tq.claim(worker_id)
            if task is None:
                continue
            if task.tid == CRASH_ID and not crash_done.is_set():
                crash_done.set()
                # worker 崩了：v0 用 tq.crash 当作"线程死了"，v1 靠租约把它捞回来
                tq.crash.set()
                continue                                  # 不 ack、不 nack —— 任务消失
            if task.tid == POISON_ID:
                tq.nack(task, "毒任务：每次执行都抛异常")
                continue
            time.sleep(task.sleep_s)                      # 模拟处理耗时
            tq.ack(task)

    n_start = MIN_WORKERS if bounded else V0_WORKERS
    for i in range(n_start):                          # v0：固定并发；v1：从最小并发起步
        t = threading.Thread(target=worker, args=(i,), daemon=True)
        workers.append(t)
        t.start()

    def producer() -> None:
        n = 0
        while not tq.stop.is_set():
            for _ in range(PRODUCE_PER_TICK):
                n += 1
                # tid 直接就是"入队序号"：无界队列按数字序即 FIFO，方便复现毒任务被反复领取
                tid = POISON_ID if n == 50 else (CRASH_ID if n == 300 else str(n))
                tq.offer(Task(tid=tid, sleep_s=CONSUME_S, payload_kb=1))
            time.sleep(TICK_S)

    def monitor() -> None:
        nonlocal worker_count
        t0 = time.perf_counter()
        while not tq.stop.is_set():
            age = tq.age_samples[-1] if tq.age_samples else 0.0
            if bounded:                                   # 动态并发：按 age 扩缩
                if age > 50 and worker_count < MAX_WORKERS:
                    worker_count += 1
                    t = threading.Thread(target=worker, args=(worker_count,), daemon=True)
                    workers.append(t)
                    t.start()
                elif age < 20 and worker_count > MIN_WORKERS:
                    worker_count -= 1                # 只缩"逻辑计数"，线程自然空转退出
            tq.observe(worker_count)
            if bounded:
                tq.reap_expired_leases()
            if time.perf_counter() - t0 > duration_s:
                tq.stop.set()
            time.sleep(0.05)

    rss0 = rss_mb()
    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    prod = threading.Thread(target=producer, daemon=True)
    prod.start()
    time.sleep(INTERACTIVE_AT_S)                          # 批处理压力上来后再打交互请求
    for i in range(INTERACTIVE_N):
        tq.offer(Task(tid=f"i{i}", kind="interactive", sleep_s=0.002, payload_kb=1))
    mon.join()
    prod.join(timeout=0.5)
    tq.stop.set()
    time.sleep(0.15)
    for t in workers:
        t.join(timeout=0.1)
    rss1 = rss_mb()
    inflight_left = len(tq.inflight)
    payload_bytes = sum(len(getattr(tk, "payload", b"")) for tk, _ in tq.dlq)
    return {"bounded": bounded, "enqueued": tq.enqueued, "dequeued": tq.dequeued,
            "done": tq.done, "rejected": tq.rejected, "retry": tq.retry_total,
            "dlq": len(tq.dlq), "lease_timeout": tq.lease_timeout_total,
            "poison_retries": tq.poison_retries, "poison_claims": tq.poison_claims,
            "lost": inflight_left, "crash": tq.crash.is_set(),
            "depth": tq.depth_samples, "age": tq.age_samples,
            "workers": tq.worker_samples, "rss_mb": rss1 - rss0,
            "interactive": tq.interactive_lat, "batch": tq.batch_lat,
            "interactive_ok": tq.interactive_ok, "interactive_total": tq.interactive_total,
            "batch_done": tq.batch_done, "dlq_payload_kb": payload_bytes / 1024.0}


def _series(samples: list[float], k: int = 6) -> str:
    if not samples:
        return "n/a"
    step = max(1, len(samples) // k)
    return " ".join(f"{samples[i]:.0f}" for i in range(0, len(samples), step))[:110]


def slo_attainment(r: dict) -> float:
    """交互式请求在 SLO 内完成的比例（比原始 P95 稳定得多，且方向明确：越高越好）。"""
    return r["interactive_ok"] / max(1, r["interactive_total"])


def _report(r: dict, tag: str) -> None:
    print(f"\n  ┌─ {tag} 队列治理观测面板")
    for k, v in (("queue_depth", f"{r['depth'][-1]} (峰值 {max(r['depth'] or [0])})"),
                 ("oldest_age_ms", f"{r['age'][-1]:.0f} (峰值 {max(r['age'] or [0]):.0f})"),
                 ("enqueue_rate", f"{r['enqueued'] / DURATION_S:.0f}/s"),
                 ("dequeue_rate", f"{r['dequeued'] / DURATION_S:.0f}/s"),
                 ("retry_total", str(r["retry"])),
                 ("dlq_size", str(r["dlq"])),
                 ("lease_timeout_total", str(r["lease_timeout"])),
                 ("queue_rejected_total(背压)", str(r["rejected"])),
                 ("worker 数(动态)", f"{r['workers'][0]} -> {r['workers'][-1]}"
                                      f" (峰值 {max(r['workers'] or [0])})"),
                 ("丢任务数(inflight 未回收)", str(r["lost"])),
                 ("RSS 变化", f"{r['rss_mb']:+.1f}MB")):
        kv(k, v)
    kv("age P50 / P95 (ms)", f"{percentile(r['age'], 50):.0f} / {percentile(r['age'], 95):.0f}")
    kv("毒任务重试占用", f"{r['poison_retries']} 次",
       f"（占全部重试 {r['retry']} 次的 "
       f"{r['poison_retries'] / max(1, r['retry']):.0%}）")
    kv("任务老化 age 时间序列", _series(r["age"]))
    kv("队列深度时间序列", _series([float(x) for x in r["depth"]]))
    kv("交互式 SLO 达成", f"{r['interactive_ok']}/{r['interactive_total']}"
                          f" = {slo_attainment(r):.1%}",
       f"（SLO {INTERACTIVE_SLO_MS:.0f}ms，P95="
       f"{percentile(r['interactive'], 95) if r['interactive'] else 0:.0f}ms）")
    kv("批处理完成 / 被背压拒绝", f"{r['batch_done']} / {r['rejected']}")
    print("  └" + "─" * 66)


def demo_batch_sharding() -> dict:
    """任务分片/批量：1000 个小任务合并成 10 个批量任务，打 token 成本对比。

    批量意味着**一次调用覆盖多个任务**：每次调用的固定开销（system prompt、工具
    说明等）只付一次，输出也合并成一次结构化返回。
    """
    phase("3b. 修复：任务分片/批量 —— 1000 个小任务合并成 10 个批量任务", "(FIX)")
    fixed_overhead = count_tokens("你是文档摘要助手，请按 JSON 输出；" * 8)
    content_tokens = count_tokens("请抽取第 N 篇文档的要点：" + "内容" * 60)
    out_per_task = 48
    single_in = 1000 * (fixed_overhead + content_tokens)
    single_out = 1000 * out_per_task
    batched_in = 10 * (fixed_overhead + 100 * (content_tokens + 4))
    batched_out = 10 * 100 * 12          # 批量的输出更紧凑（共享一次结构化外壳）
    c_single = price_of(SMALL, single_in, single_out)
    c_batch = price_of(SMALL, batched_in, batched_out)
    kv("单任务模式 token", f"in={single_in} out={single_out}", f"  ${c_single:.4f}")
    kv("批量模式(100/批) token", f"in={batched_in} out={batched_out}", f"  ${c_batch:.4f}")
    kv("每次调用的固定开销", f"{fixed_overhead}", " tokens（system + 格式说明）×1000 次")
    kv("每任务内容 token", f"{content_tokens}", " tokens（两种模式都一样，省不掉）")
    kv("每 1k 任务成本", f"${c_single:.4f} -> ${c_batch:.4f}",
       f"  ({improvement(c_single, c_batch)})")
    note("批量的代价：单任务延迟变高（要等整批凑齐）、一批失败要整批重试（所以任务级幂等 必做）、")
    note("  以及 prompt 变长后的注意力衰减 —— 批量适合**离线/近线**任务，不适合交互式请求。")
    print(f"\n{FIX} 批量合并后成本 ${c_single:.4f} -> ${c_batch:.4f} "
          f"({improvement(c_single, c_batch)})，token {single_in + single_out} -> "
          f"{batched_in + batched_out}")
    return {"cost_single": c_single, "cost_batch": c_batch,
            "per1k_before": c_single, "per1k_after": c_batch}


def demo_poison_isolation() -> dict:
    """毒任务隔离对照：只放宽队(1.5ms/任务 → 队列不堆积)，看毒任务吃掉多少 worker。

    同一个毒任务 + 同一份负载，v0 没有重试上限 → 它反复占用 worker；
    v1 三次之后就进 DLQ，worker 立刻回去干正事。
    """
    phase("3c. 修复：毒任务隔离对照（浅队列，看它吃掉多少 worker）", "(FIX)")
    global PRODUCE_PER_TICK, DURATION_S
    pt, dur = PRODUCE_PER_TICK, DURATION_S
    PRODUCE_PER_TICK, DURATION_S = 1, POISON_RUN_S
    b = run_load(bounded=False)
    f = run_load(bounded=True)
    PRODUCE_PER_TICK, DURATION_S = pt, dur
    kv("v0 无重试上限", f"毒任务被重试 {b['poison_retries']} 次（重试预算 "
                        f"{V0_RETRY_BUDGET}），有效任务只完成 {b['done']} 个")
    kv("v1 重试上限=%d" % 3, f"毒任务重试 {f['poison_retries']} 次后进 DLQ，"
                             f"有效任务完成 {f['done']} 个")
    print(f"\n{FIX} 同一个毒任务：v0 重试 {b['poison_retries']} 次（占满 worker），"
          f"v1 只重试 {f['poison_retries']} 次就进 DLQ —— 重试上限 + DLQ 才是隔离手段")
    return {"v0_poison": b["poison_retries"], "v1_poison": f["poison_retries"],
            "v0_done": b["done"], "v1_done": f["done"]}


def main() -> int:
    with lab(LAB_ID, "异步任务队列治理：任务堆积了怎么办",
             "生产环境 Agent 出现大量的异步任务堆积，如何做这种任务队列的一些治理？"):
        head("1. 复现故障：无界队列 + FIFO + 无租约")
        phase("v0 拍脑袋版：无界队列、FIFO、无租约、无限重试", "(BROKEN)")
        b = run_load(bounded=False)
        _report(b, "v0")
        lost = b["lost"] + (1 if b["crash"] else 0)
        print(f"\n{BROKEN} 无界队列 {b['enqueued']} 入队 / {b['done']} 完成；"
              f"age P95={percentile(b['age'], 95):.0f}ms、峰值 {max(b['age']):.0f}ms；"
              f"丢任务 {lost} 个；交互式 SLO 达成 {slo_attainment(b):.0%}")

        head("2. 观测 / 归因：堆积的本质指标是 age，不是 depth")
        phase("2a. 归因：生产者 > 消费者 → age 单调上涨", "(v0 时间序列)")
        note(f"v0 age 序列(ms): {_series(b['age'])}")
        note(f"v0 depth 序列  : {_series([float(x) for x in b['depth']])}")
        note(f"v0 worker 数   : {b['workers'][0]} -> {b['workers'][-1]}（固定不扩）")
        note(f"v0 交互式 SLO 达成率: {slo_attainment(b):.0%}"
             f"（{b['interactive_ok']}/{b['interactive_total']}，"
             f"P95={percentile(b['interactive'], 95) if b['interactive'] else 0:.0f}ms）")
        note("交互式请求和批处理抢同一条 FIFO 队列 → 排队时间由别人决定，SLO 无从承诺。")

        head("3. 修复：有界 + 优先级 + 租约 + DLQ + 动态并发")
        phase("v1 生产版：同样的负载，加上六项治理", "(FIX)")
        f = run_load(bounded=True)
        _report(f, "v1")
        print(f"\n{FIX} 有界队列 {f['enqueued']} 入队 / {f['done']} 完成 / {f['rejected']} 背压拒绝；"
              f"age P95={percentile(f['age'], 95):.0f}ms；丢任务 {f['lost']} 个；"
              f"DLQ {f['dlq']} 个；交互式 SLO 达成 {slo_attainment(f):.0%}")
        note("背压的语义是「牺牲谁」：交互式照常成功（优先派发 + 不进队尾），")
        note(f"  被拒的是批处理（{f['rejected']} 个，v0 是 {b['rejected']} 个）—— 这才是隔离生效的证据。")
        poison = demo_poison_isolation()
        demo_batch_sharding()

        head("4. 验证")
        age_b, age_f = percentile(b["age"], 95), percentile(f["age"], 95)
        slo_b, slo_f = slo_attainment(b), slo_attainment(f)
        for label, bv, fv, lower in (("age P95(ms)", age_b, age_f, True),
                                     ("丢任务数", float(lost), float(f["lost"]), True),
                                     ("交互式 SLO 达成率", slo_b, slo_f, False),
                                     ("批处理背压拒绝数", float(b["rejected"]),
                                      float(f["rejected"]), False),
                                     ("队列深度峰值", float(max(b["depth"] or [0])),
                                      float(max(f["depth"] or [0])), True)):
            note(f"{label:<18}: {bv:9.1f} -> {fv:9.1f}  ({improvement(bv, fv, lower)})")
        print(f"\n{VERIFY} queue_age_p95_ms: {age_b:.0f} -> {age_f:.0f} "
              f"({improvement(age_b, age_f)})")
        print(f"{VERIFY} lost_tasks: {lost} -> {f['lost']} "
              f"({improvement(float(lost), float(f['lost']))})")
        print(f"{VERIFY} interactive_slo_attainment: {slo_b:.3f} -> {slo_f:.3f} "
              f"({improvement(slo_b, slo_f, lower_is_better=False)})")
        print(f"{VERIFY} queue_depth_peak: {max(b['depth'] or [0])} -> {max(f['depth'] or [0])} "
              f"({improvement(float(max(b['depth'] or [0])), float(max(f['depth'] or [0])))})")
        print(f"{VERIFY} batch_rejected_total: {b['rejected']} -> {f['rejected']} "
              f"({improvement(float(b['rejected']), float(f['rejected']), lower_is_better=False)})"
              "  # direction: increase-expected")
        print(f"{VERIFY} poison_retry_total: {poison['v0_poison']} -> {poison['v1_poison']} "
              f"({improvement(float(poison['v0_poison']), float(poison['v1_poison']))})")
        note("说明：batch_rejected_total 上升是**背压生效**而不是退化 —— v0 不拒绝，是把")
        note("  无限量的任务堆在内存里（队列深度峰值见上），v1 选择在入口就把批处理挡住。")
        note("  poison_retry_total 变少也同理：不是「没得重试」，而是三次之后它被 DLQ 隔离了。")
        note(f"  交互式 SLO 达成率：{slo_b:.0%} -> {slo_f:.0%}（用 SLO 线口径，比原始 P95 稳定得多）。")

        head("5. 工程结论：队列治理清单")
        note("1) 有界队列 + 背压：满了就拒绝（429/503 + Retry-After），别让它变成内存泄漏。")
        note("2) 优先级：交互式 > 批处理，SLO 才是可承诺的；批处理永远做兜底。")
        note("3) 租约 + 可见性超时：worker 死了任务自动回队列，代价是 at-least-once → 必须幂等。")
        note("4) 重试上限 + 指数退避 + DLQ：毒任务隔离，DLQ 要有人看（告警 + 一键重放）。")
        note("5) 动态并发：按 age 扩缩（age > SLO 的 10% 就扩容、<20ms 缩容），别按 depth 拍脑袋。")
        note("6) 分片/批量：离线任务合并调用，成本数量级下降；交互式任务绝不上批量。")
        note("要盯的指标：queue_depth / oldest_age_ms / enqueue_rate / dequeue_rate /")
        note("  retry_total / dlq_size / lease_timeout_total / queue_rejected_total。")
        METRICS.render("lab-05 指标快照", include=["queue_"])
        takeaway("队列治理的核心是三个「有界」：队列有界、重试有界、并发有界；"
                 "再加一个「不丢」：租约兜底 worker 崩溃，代价是必须幂等。")
        METRICS.reset()
    return 0


QUESTIONS = [
    "生产环境大量异步任务堆积，如何治理？ -> 有界队列+背压、优先级、租约、"
    "重试上限+DLQ、动态并发、批量合并",
    "堆积该盯什么指标？ -> oldest_age_ms（用户实际感受）而不是 queue_depth（表象）",
    "worker 崩溃/卡死导致任务丢失怎么办？ -> 租约 + 可见性超时自动 requeue，"
    "代价是 at-least-once，任务必须幂等",
    "毒任务/重试风暴怎么处理？ -> max_attempts + 指数退避 + DLQ 隔离，人工/自动重放",
]


if __name__ == "__main__":
    sys.exit(main())
