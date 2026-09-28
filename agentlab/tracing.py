"""链路追踪：span 树 + 耗时归因。

"agent 链路很长，怎么把 P95 的 RT 降下来"——不先把耗时**按 span 归因**，
所有优化都是猜。本模块提供最小可用的 span 树实现：父子关系、状态、属性、
以及"哪个 span 吃掉了尾部延迟"的统计。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from .util import Stats


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: int
    parent_id: int | None = None
    start: float = field(default_factory=time.perf_counter)
    end: float | None = None
    status: str = "OK"
    error: str = ""
    attrs: dict[str, Any] = field(default_factory=dict)
    children: list["Span"] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.end is None:
            return (time.perf_counter() - self.start) * 1000.0
        return (self.end - self.start) * 1000.0

    def set(self, **attrs: Any) -> "Span":
        self.attrs.update(attrs)
        return self

    def finish(self, status: str = "OK", error: str = "") -> None:
        self.end = time.perf_counter()
        if status != "OK":
            self.status = status
        if error:
            self.error = error

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.finish("ERROR", f"{type(exc).__name__}: {exc}")
        else:
            self.finish()
        return False


class Tracer:
    """一个请求一棵 span 树。线程内用显式传入（不搞 contextvar 魔法，便于教学）。"""

    def __init__(self, trace_id: str = "trace"):
        self.trace_id = trace_id
        self._next_id = 0
        self._lock = threading.Lock()
        self.roots: list[Span] = []
        self.all_spans: list[Span] = []

    def _alloc(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    def start(self, name: str, parent: Span | None = None, **attrs: Any) -> Span:
        span = Span(
            name=name,
            trace_id=self.trace_id,
            span_id=self._alloc(),
            parent_id=parent.span_id if parent else None,
            attrs=dict(attrs),
        )
        if parent is not None:
            parent.children.append(span)
        else:
            self.roots.append(span)
        self.all_spans.append(span)
        return span

    @contextmanager
    def span(self, name: str, parent: Span | None = None, **attrs: Any) -> Iterator[Span]:
        sp = self.start(name, parent, **attrs)
        try:
            yield sp
        except BaseException as exc:  # noqa: BLE001
            sp.finish("ERROR", f"{type(exc).__name__}: {exc}")
            raise
        else:
            sp.finish()

    # -- 统计 ---------------------------------------------------------------
    def spans_named(self, name: str) -> list[Span]:
        return [s for s in self.all_spans if s.name == name]

    def durations(self, name: str) -> list[float]:
        return [s.duration_ms for s in self.spans_named(name)]

    def stage_stats(self) -> dict[str, Stats]:
        """按 span 名聚合：每个阶段贡献了多少耗时。"""
        grouped: dict[str, list[float]] = {}
        for s in self.all_spans:
            if s.parent_id is None:  # 只统计顶层阶段，避免父子重复计数
                grouped.setdefault(s.name, []).append(s.duration_ms)
        return {k: Stats(v) for k, v in sorted(grouped.items())}

    def total_ms(self) -> float:
        return sum(s.duration_ms for s in self.roots)

    def critical_path(self) -> list[Span]:
        """最耗时的那条链（用于看"长尾是谁贡献的"）。"""
        if not self.roots:
            return []

        def deepest(node: Span) -> tuple[float, list[Span]]:
            if not node.children:
                return node.duration_ms, [node]
            best_cost, best_path = -1.0, []
            for c in node.children:
                cost, path = deepest(c)
                if cost > best_cost:
                    best_cost, best_path = cost, path
            return node.duration_ms, [node] + best_path

        root = max(self.roots, key=lambda s: s.duration_ms)
        return deepest(root)[1]

    # -- 渲染 ---------------------------------------------------------------
    def render(self, title: str | None = None, max_depth: int = 6) -> None:
        print(f"\n  ┌─ trace {self.trace_id} {title or ''}".rstrip())
        for root in self.roots:
            self._render_span(root, 0, max_depth)
        print("  └" + "─" * 60)

    def _render_span(self, span: Span, depth: int, max_depth: int) -> None:
        if depth > max_depth:
            return
        indent = "  " * depth
        flag = "" if span.status == "OK" else f"  !!{span.status}"
        attr = ""
        if span.attrs:
            attr = "  " + " ".join(f"{k}={v}" for k, v in span.attrs.items())
        print(f"  │ {indent}{span.name:<34} {span.duration_ms:8.1f}ms{flag}{attr}")
        for child in sorted(span.children, key=lambda s: -s.duration_ms):
            self._render_span(child, depth + 1, max_depth)

    def render_stage_table(self) -> None:
        """耗时归因表：这是做 P95 优化的起点。"""
        st = self.stage_stats()
        if not st:
            return
        print("\n  ┌─ 耗时归因（按顶层 span） " + "─" * 34)
        print(f"  │ {'stage':<30} {'n':>4} {'p50':>9} {'p95':>9} {'max':>9}")
        for name, s in st.items():
            print(
                f"  │ {name:<30} {s.n:>4} {s.p50:>8.1f}ms {s.p95:>8.1f}ms {s.mx:>8.1f}ms"
            )
        print("  └" + "─" * 62)


class TraceStore:
    """把多个请求的 Tracer 存起来，做跨请求的尾部归因。"""

    def __init__(self) -> None:
        self.tracers: list[Tracer] = []
        self._lock = threading.Lock()

    def add(self, tracer: Tracer) -> Tracer:
        with self._lock:
            self.tracers.append(tracer)
        return tracer

    def slowest(self, k: int = 1) -> list[Tracer]:
        return sorted(self.tracers, key=lambda t: -t.total_ms())[:k]

    def aggregate(self) -> dict[str, Stats]:
        grouped: dict[str, list[float]] = {}
        for t in self.tracers:
            for name, st in t.stage_stats().items():
                grouped.setdefault(name, []).extend(
                    s.duration_ms for s in t.spans_named(name) if s.parent_id is None
                )
        return {k: Stats(v) for k, v in sorted(grouped.items())}
