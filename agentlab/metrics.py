"""指标注册表：Counter / Gauge / Histogram，以及渲染。

生产 Agent 服务"如何评定性能、成本和稳定性"的第一个答案是：
**先把指标定下来并打出来**。这里实现的是一个进程内 Prometheus 风格注册表，
线程安全，足以支撑所有 lab 的观测环节。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .util import Stats, fmt_bytes, percentile


class Counter:
    __slots__ = ("name", "help", "_value", "_lock")

    def __init__(self, name: str, help_: str = ""):
        self.name = name
        self.help = help_
        self._value = 0.0
        self._lock = threading.Lock()

    def inc(self, n: float = 1.0) -> None:
        with self._lock:
            self._value += n

    @property
    def value(self) -> float:
        return self._value

    def reset(self) -> None:
        with self._lock:
            self._value = 0.0


class Gauge:
    __slots__ = ("name", "help", "_value", "_lock")

    def __init__(self, name: str, help_: str = "", value: float = 0.0):
        self.name = name
        self.help = help_
        self._value = float(value)
        self._lock = threading.Lock()

    def set(self, v: float) -> None:
        with self._lock:
            self._value = float(v)

    def inc(self, n: float = 1.0) -> None:
        with self._lock:
            self._value += n

    def dec(self, n: float = 1.0) -> None:
        with self._lock:
            self._value -= n

    @property
    def value(self) -> float:
        return self._value


class Histogram:
    """只存样本的直方图（教学场景数据量小，精确百分位比桶更直观）。"""

    __slots__ = ("name", "help", "unit", "_samples", "_lock")

    def __init__(self, name: str, help_: str = "", unit: str = "ms"):
        self.name = name
        self.help = help_
        self.unit = unit
        self._samples: list[float] = []
        self._lock = threading.Lock()

    def observe(self, v: float) -> None:
        with self._lock:
            self._samples.append(float(v))

    def observe_all(self, values: Iterable[float]) -> None:
        with self._lock:
            self._samples.extend(float(v) for v in values)

    @property
    def samples(self) -> list[float]:
        with self._lock:
            return list(self._samples)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._samples)

    def stats(self) -> Stats:
        return Stats(self.samples)

    def p(self, q: float) -> float:
        return percentile(self.samples, q)

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()

    def __len__(self) -> int:
        return self.count


@dataclass
class Snapshot:
    counters: dict[str, float] = field(default_factory=dict)
    gauges: dict[str, float] = field(default_factory=dict)
    histograms: dict[str, Stats] = field(default_factory=dict)


class Metrics:
    """全局指标注册表。lab 里直接 ``from agentlab.metrics import METRICS``。"""

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = threading.Lock()

    # -- 注册 ---------------------------------------------------------------
    def counter(self, name: str, help_: str = "") -> Counter:
        with self._lock:
            if name not in self._counters:
                self._counters[name] = Counter(name, help_)
            return self._counters[name]

    def gauge(self, name: str, help_: str = "", value: float = 0.0) -> Gauge:
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = Gauge(name, help_, value)
            return self._gauges[name]

    def histogram(self, name: str, help_: str = "", unit: str = "ms") -> Histogram:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = Histogram(name, help_, unit)
            return self._histograms[name]

    # -- 查询 ---------------------------------------------------------------
    def value(self, name: str) -> float:
        if name in self._counters:
            return self._counters[name].value
        if name in self._gauges:
            return self._gauges[name].value
        return 0.0

    def snapshot(self) -> Snapshot:
        return Snapshot(
            counters={k: v.value for k, v in self._counters.items()},
            gauges={k: v.value for k, v in self._gauges.items()},
            histograms={k: v.stats() for k, v in self._histograms.items()},
        )

    def reset(self) -> None:
        with self._lock:
            for c in self._counters.values():
                c.reset()
            for h in self._histograms.values():
                h.reset()
            for g in self._gauges.values():
                g.set(0.0)

    # -- 渲染 ---------------------------------------------------------------
    def render(
        self,
        title: str = "指标快照",
        include: Sequence[str] | None = None,
        histograms: bool = True,
    ) -> None:
        snap = self.snapshot()
        print(f"\n  ┌─ {title} " + "─" * max(0, 60 - len(title)))
        for name, value in sorted(snap.counters.items()):
            if include and not any(i in name for i in include):
                continue
            print(f"  │ {name:<44} {_fmt_value(value)}")
        for name, value in sorted(snap.gauges.items()):
            if include and not any(i in name for i in include):
                continue
            print(f"  │ {name:<44} {_fmt_value(value)}")
        if histograms:
            for name, st in sorted(snap.histograms.items()):
                if include and not any(i in name for i in include):
                    continue
                print(f"  │ {name:<44} {st}")
        print("  └" + "─" * 64)

    def to_prometheus(self) -> str:
        """输出 Prometheus 文本格式（demo 服务的 ``/metrics`` 端点真的用它）。

        两条健壮性要求（都由真实 bug 换来）：

        1. **空直方图不能输出分位数** —— ``Stats`` 在无样本时全是 nan，
           格式化会抛异常，把**整个 /metrics 端点打成 500**。监控端点挂了，
           就等于业务挂了却没人知道；可观测性组件本身必须最健壮。
        2. **单个指标异常不能影响整体** —— 每个指标单独 try/except，
           坏掉的那条跳过并标注，其余照常输出。
        """
        lines: list[str] = []
        snap = self.snapshot()
        for name, value in sorted(snap.counters.items()):
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value:g}")
        for name, value in sorted(snap.gauges.items()):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value:g}")
        for name, hist in sorted(self._histograms.items()):
            lines.append(f"# TYPE {name} summary")
            try:
                samples = hist.samples
                if not samples:
                    lines.append(f"{name}_count 0")
                    continue
                for q in (50, 95, 99):
                    lines.append(
                        f'{name}{{quantile="{q / 100:g}"}} {percentile(samples, q):g}'
                    )
                lines.append(f"{name}_sum {sum(samples):g}")
                lines.append(f"{name}_count {len(samples):g}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"# ERROR exporting {name}: {type(exc).__name__}")
                lines.append(f"{name}_count 0")
        return "\n".join(lines)


def _fmt_value(v: float) -> str:
    if v != v:  # NaN
        return "n/a"
    if abs(v) >= 1_000_000:
        return f"{v:,.0f}"
    if float(v).is_integer():
        return f"{int(v)}"
    return f"{v:.4f}"


METRICS = Metrics()
"""全局注册表。为避免 lab 之间互相污染，lab 结尾通常调用 ``METRICS.reset()``。"""


def render_memory(tag: str = "内存") -> float:
    from .util import rss_mb

    mb = rss_mb()
    print(f"  [mem] {tag}: RSS={mb:.1f}MB")
    return mb


def human(n: float) -> str:
    return fmt_bytes(n)
