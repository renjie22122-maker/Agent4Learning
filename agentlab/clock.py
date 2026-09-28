"""可注入时钟。

为什么需要它：很多生产问题（分层超时、熔断冷却、队列老化、限流窗口）如果
用 ``time.sleep`` 演示，一个 lab 要跑几分钟。注入虚拟时钟后，真实耗时被压缩，
但**语义完全不变**——这点对教学很重要：结论来自逻辑而不是"等出来"的。
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable


class RealClock:
    """真实时钟（默认）。"""

    kind = "real"

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.perf_counter()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)

    def sleep_sync(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class VirtualClock:
    """虚拟时钟：``sleep`` 只推进内部时间，不真的等。

    只适用于单线程 asyncio 场景。它把"超时预算 / 熔断冷却 / 限流窗口"这类
    **逻辑**从"真实等待"里解放出来。
    """

    kind = "virtual"

    def __init__(self, start: float = 1_700_000_000.0):
        self._t = float(start)
        self._sleeps = 0
        self._virtual_elapsed = 0.0

    # -- 时间 ---------------------------------------------------------------
    def now(self) -> float:
        return self._t

    def monotonic(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        if seconds > 0:
            self._t += seconds
            self._virtual_elapsed += seconds

    async def sleep(self, seconds: float) -> None:
        self._sleeps += 1
        self.advance(seconds)
        # 让出一次控制权，保证并发协程能交错推进
        await asyncio.sleep(0)

    def sleep_sync(self, seconds: float) -> None:
        self._sleeps += 1
        self.advance(seconds)

    # -- 观测 ---------------------------------------------------------------
    @property
    def virtual_elapsed(self) -> float:
        return self._virtual_elapsed

    @property
    def sleep_count(self) -> int:
        return self._sleeps

    def stats(self) -> str:
        return (
            f"virtual_elapsed={self._virtual_elapsed:.3f}s "
            f"sleeps={self._sleeps} (真实耗时≈0)"
        )


Clock = RealClock | VirtualClock


def default_clock() -> Clock:
    return RealClock()


def timed(fn: Callable[[], object], clock: Clock | None = None) -> tuple[object, float]:
    """跑一个函数并返回 (结果, 耗时秒)。"""
    c = clock or default_clock()
    t0 = c.monotonic()
    out = fn()
    return out, c.monotonic() - t0
