"""Capstone 服务生命周期：启动预热、readiness 摘流、优雅停机。

对应 lab-01。发布期间用户报错，几乎总是这三件事之一：

1. readiness 在依赖就绪之前返回 true → 流量进来了但能力还没有（预热缺失）；
2. 收到停止信号立刻退出 → 在飞请求被掐断（缺少排空）；
3. liveness 把依赖健康也算进去 → 依赖一抖就滚动重启，把抖动放大成故障。

另外一个真实细节：真正的探针端口必须只有一个实例的**排空状态**，而不是
业务逻辑的结果。所以这里把"能不能接流量"和"进程要不要重启"分开。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agentlab.metrics import METRICS


class Phase(str, Enum):
    INIT = "init"
    WARMING = "warming"
    READY = "ready"
    DRAINING = "draining"
    STOPPED = "stopped"


@dataclass
class LifecycleState:
    phase: Phase = Phase.INIT
    started_at: float = field(default_factory=time.monotonic)
    ready_at: float = 0.0
    inflight: int = 0
    accepted: int = 0
    rejected: int = 0
    dropped: int = 0
    drain_started_at: float = 0.0
    warmup_ms: float = 0.0
    history: list[tuple[str, str, float]] = field(default_factory=list)


class ServicePlatform:
    """把引擎包成一个"能被发布系统正确管理"的服务。"""

    def __init__(self, cfg, engine) -> None:
        self.cfg = cfg
        self.engine = engine
        self.st = LifecycleState()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._drained = threading.Event()
        self.m_phase = METRICS.gauge("service_phase", "0=init 1=warming 2=ready 3=draining 4=stopped")
        self.m_inflight = METRICS.gauge("service_inflight", "在飞请求数")
        self.m_rejected = METRICS.counter("service_rejected_total", "摘流后拒绝的请求")
        self.m_dropped = METRICS.counter("service_dropped_total", "停机时被掐断的请求")
        self._warmup_done = False

    # -- 生命周期 -----------------------------------------------------------
    def start(self, warmup: bool = True) -> None:
        self._transition(Phase.WARMING)
        t0 = time.monotonic()
        if warmup and self.cfg.warmup_on_start:
            self._warmup()
        self.st.warmup_ms = (time.monotonic() - t0) * 1000.0
        self._warmup_done = True
        self.st.ready_at = time.monotonic()
        self._transition(Phase.READY)

    def _warmup(self) -> None:
        """预热三类东西：前缀缓存、检索索引、连接池。

        为什么必须做：冷启动时第一批请求全部 miss 前缀缓存，延迟和成本都会有
        "启动尖刺"。发布是滚动进行的，如果每个实例都有尖刺，用户就会看到错误。

        注意预热必须用**和真实请求完全相同的前缀**：这里只放稳定部分的 system
        消息，易变内容（时间戳/检索/历史）绝不能进前缀，否则预热等于白做。
        """
        from agentlab.providers import system as _sys
        from agentlab.tokens import count_tokens

        srv = self.engine.server
        prefix_msgs = [_sys(self.engine.context.SYSTEM_PROMPT)]
        prefix_key = srv.prefix_key(prefix_msgs)
        prefix_tokens = srv.prefix_tokens(prefix_msgs)
        for spec in srv.models.values():
            srv.warm_prefix(spec.name, prefix_key, prefix_tokens)
        # 预热检索：跑一次查询把索引页读进内存
        if self.engine.index is not None:
            from agentlab.store import Query

            for q in ("缓存 优化", "限流 治理", "内存 泄漏"):
                self.engine.index.search(Query(q, 3))
        self._warmup_flag = True

    def _transition(self, phase: Phase) -> None:
        with self._lock:
            self.st.phase = phase
            self.st.history.append((phase.value, "", time.monotonic()))
            self.m_phase.set(
                {Phase.INIT: 0, Phase.WARMING: 1, Phase.READY: 2, Phase.DRAINING: 3, Phase.STOPPED: 4}[phase]
            )

    # -- 探针 ---------------------------------------------------------------
    def liveness(self) -> tuple[bool, str]:
        """只回答"进程是否需要重启"。

        **绝不能把依赖健康放进来**：依赖抖 2 秒就把所有实例判定为 dead，
        编排器会把整个集群滚动重启一遍 —— 这才是真正的事故放大器。
        """
        if self.st.phase is Phase.STOPPED:
            return False, "stopped"
        if time.monotonic() - self.st.started_at > 600:
            return True, "alive(long-running)"
        return True, f"alive({self.st.phase.value})"

    def readiness(self) -> tuple[bool, str]:
        """只回答"现在能不能接流量"。"""
        if not self._warmup_done:
            return False, "warming: 预热未完成，摘流中（但不要重启我）"
        if self.st.phase is Phase.DRAINING:
            return False, "draining: 正在排空，请勿再派流量"
        if self.st.phase is not Phase.READY:
            return False, f"not-ready({self.st.phase.value})"
        # 停机信号一到，readiness 立刻转 false（**先摘流量再停服务**）
        if self._stop.is_set():
            return False, "shutdown-signalled"
        return True, "ready"

    # -- 停机 ---------------------------------------------------------------
    def request_shutdown(self, grace_s: float | None = None) -> dict:
        """优雅停机：摘流 → 等在飞排空 → 超时强杀。返回排空报告。"""
        grace = self.cfg.shutdown_grace_s if grace_s is None else grace_s
        t0 = time.monotonic()
        self._stop.set()
        self._transition(Phase.DRAINING)
        self.st.drain_started_at = t0
        deadline = t0 + grace
        while time.monotonic() < deadline:
            with self._lock:
                if self.st.inflight <= 0:
                    break
            time.sleep(0.01)
        with self._lock:
            remaining = self.st.inflight
            self.st.dropped += max(0, remaining)
        if remaining:
            self.m_dropped.inc(remaining)
        self._transition(Phase.STOPPED)
        self._drained.set()
        return {
            "drain_ms": (time.monotonic() - t0) * 1000.0,
            "grace_s": grace,
            "interrupted": remaining,
            "accepted": self.st.accepted,
            "rejected_after_drain": self.st.rejected,
        }

    # -- 请求入口 -----------------------------------------------------------
    def handle(self, req):
        """接流量前先检查 readiness —— 这一步就是"发布期间不报错"的关键。"""
        with self._lock:
            if self._stop.is_set() or self.st.phase is not Phase.READY:
                self.st.rejected += 1
                self.m_rejected.inc()
                raise ServiceUnavailable("实例正在排空/未就绪，请重试其他实例")
            self.st.inflight += 1
            self.st.accepted += 1
            self.m_inflight.set(self.st.inflight)
        try:
            return self.engine.handle(req)
        finally:
            with self._lock:
                self.st.inflight -= 1
                self.m_inflight.set(self.st.inflight)

    def render(self) -> None:
        print("\n  ┌─ 服务生命周期")
        print(f"  │ phase={self.st.phase.value} warmup={self.st.warmup_ms:.1f}ms")
        print(
            f"  │ accepted={self.st.accepted} rejected={self.st.rejected} "
            f"dropped={self.st.dropped} inflight={self.st.inflight}"
        )
        live, lmsg = self.liveness()
        ready, rmsg = self.readiness()
        print(f"  │ liveness={live} ({lmsg})")
        print(f"  │ readiness={ready} ({rmsg})")
        print("  └" + "─" * 62)


class ServiceUnavailable(Exception):
    pass


# --------------------------------------------------------------------------
# 探针端点（真的起一个 HTTP 服务，验证 k8s 那套探针语义）
# --------------------------------------------------------------------------


class ProbeServer:
    """``/livez`` ``/readyz`` ``/metrics`` 三个端点。

    capstone 里默认不启（避免端口冲突），用 ``--probes`` 打开。探针语义与
    k8s 一致：livez 返回 200 就代表"别重启我"，readyz 返回 503 代表"别给我流量"。
    """

    def __init__(self, platform: ServicePlatform, port: int = 0):
        self.platform = platform
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        plat = self.platform

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静音
                return

            def do_GET(self):  # noqa: N802
                if self.path == "/livez":
                    ok, msg = plat.liveness()
                    self._send(200 if ok else 500, msg)
                elif self.path == "/readyz":
                    ok, msg = plat.readiness()
                    self._send(200 if ok else 503, msg)
                elif self.path == "/metrics":
                    self._send(200, METRICS.to_prometheus(), "text/plain")
                else:
                    self._send(404, "not found")

            def _send(self, code: int, body: str, ctype: str = "text/plain; charset=utf-8"):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
