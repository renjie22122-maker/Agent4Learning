"""零依赖工具层：输出规范、百分位统计、事件循环跑批。

本模块同时定义了**所有 lab 必须遵守的输出协议**——``verify.py`` 靠这些标记
做端到端校验，所以格式不能随便改。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Iterable, Sequence


def force_utf8() -> None:
    """让中文输出在 Windows 控制台/PowerShell 里不乱码。

    必须在导入时尽早调用；已经写过的内容不受影响。
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", errors="replace")


force_utf8()

# --------------------------------------------------------------------------
# 输出协议
# --------------------------------------------------------------------------

LAB_START = "[LAB-START]"
LAB_END = "[LAB-END]"
BROKEN = "[BROKEN-REPRODUCED]"
FIX = "[FIX-APPLIED]"
VERIFY = "[VERIFY]"
TAKEAWAY = "[TAKEAWAY]"


def rule(char: str = "-", width: int = 78) -> str:
    return char * width


def head(text: str) -> None:
    """章节标题。"""
    print(f"\n{rule('=')}")
    print(f"  {text}")
    print(rule("="))


def phase(title: str, tag: str = "") -> None:
    """阶段标题：复现 / 观测 / 修复 / 验证。"""
    suffix = f"  {tag}" if tag else ""
    print(f"\n>>> {title}{suffix}")
    print(rule("-"))


def note(text: str) -> None:
    print(f"    {text}")


def kv(key: str, value: Any, unit: str = "") -> None:
    print(f"    {key:<34} {value}{unit}")


def takeaway(text: str) -> None:
    print(f"\n{TAKEAWAY} {text}")


def lab_start(lab_id: str, title: str, question: str = "") -> None:
    print(f"{LAB_START} {lab_id} :: {title}")
    if question:
        print(f"    学习目标问题: {question}")


def lab_end(lab_id: str, elapsed_s: float | None = None) -> None:
    extra = f" elapsed={elapsed_s:.2f}s" if elapsed_s is not None else ""
    print(f"{LAB_END} {lab_id}{extra}")


@contextlib.contextmanager
def lab(lab_id: str, title: str, question: str = ""):
    """lab 主入口的上下文管理器，保证 START/END 标记成对出现。"""
    t0 = time.perf_counter()
    lab_start(lab_id, title, question)
    try:
        yield
    finally:
        lab_end(lab_id, time.perf_counter() - t0)


# --------------------------------------------------------------------------
# 百分位统计
# --------------------------------------------------------------------------


def percentile(samples: Sequence[float], q: float) -> float:
    """线性插值百分位。``q`` 取 0..100。"""
    if not samples:
        return float("nan")
    if len(samples) == 1:
        return float(samples[0])
    ordered = sorted(samples)
    pos = (len(ordered) - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(ordered[int(pos)])
    frac = pos - lo
    return float(ordered[lo] * (1 - frac) + ordered[hi] * frac)


def mean(samples: Sequence[float]) -> float:
    return float(sum(samples) / len(samples)) if samples else float("nan")


class Stats:
    """一批延迟样本的统计摘要，``__str__`` 直接可读。"""

    __slots__ = ("n", "avg", "p50", "p95", "p99", "mx", "mn")

    def __init__(self, samples: Sequence[float]):
        self.n = len(samples)
        self.avg = mean(samples)
        self.p50 = percentile(samples, 50)
        self.p95 = percentile(samples, 95)
        self.p99 = percentile(samples, 99)
        self.mx = max(samples) if samples else float("nan")
        self.mn = min(samples) if samples else float("nan")

    def as_dict(self) -> dict[str, float]:
        return {
            "n": float(self.n),
            "avg": self.avg,
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
            "max": self.mx,
        }

    def __str__(self) -> str:
        if not self.n:
            return "n=0"
        return (
            f"n={self.n:<5} avg={self.avg:8.1f}ms  p50={self.p50:8.1f}ms  "
            f"p95={self.p95:8.1f}ms  p99={self.p99:8.1f}ms  max={self.mx:9.1f}ms"
        )


def stats_of_ms(samples_s: Sequence[float]) -> Stats:
    """把秒样本转成毫秒统计。"""
    return Stats([s * 1000.0 for s in samples_s])


def improvement(
    before: float,
    after: float,
    lower_is_better: bool = True,
    choose_better: bool = False,
) -> str:
    """生成 ``-63.2%`` 这样的对比字符串。

    约定：**输出的正负号表示"变化方向"**（``-`` 表示数值下降），不表示好坏。
    好坏由 ``lower_is_better`` 判断，但不再通过翻转符号来表达 —— 早期版本用
    "取负表示变好"，结果 ``+8.4%`` 这种字符串会被读成退化，制造了大量歧义。

    参数
    ----
    lower_is_better
        该指标是否越小越好。只影响 ``choose_better=True`` 时的选择逻辑。
    choose_better
        当基准值是 0 时，百分比无法计算。此时若 ``choose_better=True``，返回一个
        表示"变好/变差"的可读结论（例如 ``0 -> 120 (↑ 新增)``）；否则返回 ``n/a``。

    用法建议：
    * 想让 ``verify.py`` 校验方向，就用 ``[VERIFY] name: before -> after`` 那套格式，
      不需要依赖本函数；
    * 想打印人类可读的结论，用 ``ValueError`` 之外的默认调用即可。
    """
    if before == 0 or math.isnan(before) or math.isnan(after):
        if choose_better and not math.isnan(after):
            if after == 0:
                return "0 -> 0 (持平)"
            better = after < 0 if lower_is_better else after > 0
            arrow = "↑" if after > 0 else "↓"
            return f"0 -> {after:g} ({arrow} {'改善' if better else '恶化'})"
        return "n/a"
    delta = (after - before) / abs(before) * 100.0
    return f"{delta:+.1f}%"


def verdict(before: float, after: float, lower_is_better: bool = True) -> str:
    """返回 ``改善 32.0%`` / ``恶化 12.0%`` / ``持平``。

    和 ``improvement()`` 的区别：这个函数直接给结论，不需要读的人自己记住
    "负号到底代表什么"。**在打印"修复效果"时优先用它**，歧义为零。
    """
    if math.isnan(before) or math.isnan(after):
        return "n/a"
    if before == 0:
        if after == 0:
            return "持平"
        better = after < 0 if lower_is_better else after > 0
        return f"{'改善' if better else '恶化'}（0 -> {after:g}）"
    delta = (after - before) / abs(before) * 100.0
    if abs(delta) < 0.5:
        return "持平"
    better = delta < 0 if lower_is_better else delta > 0
    return f"{'改善' if better else '恶化'} {abs(delta):.1f}%"


# --------------------------------------------------------------------------
# 并发原语
# --------------------------------------------------------------------------


def run_concurrently(
    fn: Callable[[int], Any],
    count: int,
    workers: int | None = None,
) -> list[Any]:
    """用线程把 ``fn(i)`` 跑 ``count`` 次，最多 ``workers`` 个并发。

    返回与 ``range(count)`` 对齐的结果列表；抛异常的槽位放异常对象本身，
    这样调用方可以统计错误率而不是直接崩掉。
    """
    workers = max(1, workers if workers is not None else count)
    results: list[Any] = [None] * count
    lock = threading.Lock()
    nxt = 0

    def worker() -> None:
        nonlocal nxt
        while True:
            with lock:
                if nxt >= count:
                    return
                i = nxt
                nxt += 1
            try:
                results[i] = fn(i)
            except BaseException as exc:  # noqa: BLE001 - 故意收集
                results[i] = exc

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def run_async(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """在一个全新事件循环里跑协程（lab 常用入口）。"""
    return asyncio.run(coro_factory())


def errors_of(results: Iterable[Any]) -> list[BaseException]:
    return [r for r in results if isinstance(r, BaseException)]


def error_rate(results: Sequence[Any]) -> float:
    if not results:
        return 0.0
    return len(errors_of(results)) / len(results)


def partition(results: Sequence[Any]) -> tuple[list[Any], list[BaseException]]:
    oks = [r for r in results if not isinstance(r, BaseException)]
    errs = [r for r in results if isinstance(r, BaseException)]
    return oks, errs


def error_histogram(results: Sequence[Any]) -> dict[str, int]:
    """按错误类型/错误码归类，便于看"错在哪"。"""
    hist: dict[str, int] = {}
    for r in results:
        if isinstance(r, BaseException):
            key = getattr(r, "code", None) or type(r).__name__
            hist[str(key)] = hist.get(str(key), 0) + 1
    return dict(sorted(hist.items(), key=lambda kv_: -kv_[1]))


# --------------------------------------------------------------------------
# 随机数：可复现
# --------------------------------------------------------------------------


def rng(seed: int = 7) -> random.Random:
    return random.Random(seed)


def lognormal_latency(r: random.Random, p50_ms: float, sigma: float = 0.5) -> float:
    """对数正态延迟（毫秒）——真实 LLM 延迟的尾巴比正态分布重得多。"""
    mu = math.log(max(p50_ms, 1e-6))
    return math.exp(r.gauss(mu, sigma))


# --------------------------------------------------------------------------
# 内存
# --------------------------------------------------------------------------


def rss_mb() -> float:
    """当前进程 RSS（MB）。Windows 走 ctypes，其他平台走 resource，最后兜底 psutil。"""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            # 关键：必须声明 restype/argtypes，否则 64 位伪句柄会被截断成 -1
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.GetCurrentProcess.argtypes = []
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD,
            ]
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            handle = k32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return counters.WorkingSetSize / (1024 * 1024)
        except Exception:  # noqa: BLE001 - 尽力而为的观测
            pass
    else:
        try:  # POSIX
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return usage / 1024 if sys.platform != "darwin" else usage / (1024 * 1024)
        except Exception:  # noqa: BLE001
            pass
    try:  # 兜底：环境里恰好装了 psutil
        import psutil  # type: ignore

        return float(psutil.Process().memory_info().rss) / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return float("nan")


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def payload_of_kb(kb: float) -> bytes:
    """造一段可预估内存的字节串（用于演示内存膨胀）。"""
    return b"x" * int(kb * 1024)
