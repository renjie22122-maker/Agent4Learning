"""共用执行契约：参数、授权、证据、并发预算与调用结果。

授权来自宿主配置，不接受工具参数中自授予的权限。耗时外部操作由
processes.ProcessSupervisor 管理；本模块不把线程停止等待说成执行已取消。
"""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import threading
import queue
import time
from typing import Callable

from .schema import validate_schema


class PermissionDenied(RuntimeError):
    pass


class BoundedCallbacks:
    """仅给受信、会返回的宿主回调；超时不等于取消，超时后不重试。

    不可协作的工具必须使用 ProcessSupervisor。槽位在回调真正退出后释放，
    不因调用方超时而凭空制造更多执行并发。
    """
    def __init__(self, capacity=8):
        self.slots = threading.BoundedSemaphore(capacity)

    def call(self, fn, timeout_s):
        if not self.slots.acquire(blocking=False):
            raise RuntimeError('工具执行槽位已满（可能仍有超时回调在收尾）')
        result = queue.Queue(maxsize=1)
        def worker():
            try:
                result.put((True, fn()))
            except BaseException as exc:
                result.put((False, exc))
            finally:
                self.slots.release()
        threading.Thread(target=worker, daemon=True).start()
        try:
            ok, value = result.get(timeout=timeout_s)
        except queue.Empty:
            raise TimeoutError('等待超时，执行结果未知；禁止自动重试副作用') from None
        if not ok:
            raise value
        return value


@dataclass(frozen=True)
class CapabilityPolicy:
    allowed_tools: frozenset[str] | None = None
    allow_writes: bool = True
    allow_shell: bool = True
    allow_network: bool = False

    def intersect(self, ceiling):
        allowed = ceiling.allowed_tools if self.allowed_tools is None else self.allowed_tools
        if self.allowed_tools is not None and ceiling.allowed_tools is not None:
            allowed = self.allowed_tools & ceiling.allowed_tools
        return CapabilityPolicy(allowed, self.allow_writes and ceiling.allow_writes,
                                self.allow_shell and ceiling.allow_shell,
                                self.allow_network and ceiling.allow_network)

    def check(self, name: str, *, writes=False, shell=False, network=False):
        if self.allowed_tools is not None and name not in self.allowed_tools:
            raise PermissionDenied(f"未授权工具：{name}")
        if writes and not self.allow_writes:
            raise PermissionDenied(f"只读任务禁止 {name}")
        if shell and not self.allow_shell:
            raise PermissionDenied("该任务未授予命令执行权限")
        if network and not self.allow_network:
            raise PermissionDenied("该任务未授予外网访问权限")


def effective_policy(agent):
    policy = agent.capabilities
    provider = getattr(agent, 'authority_provider', None)
    return policy.intersect(provider()) if provider else policy


def invoke_checked(name, args, schema, fn, policy=None, *, writes=False,
                   shell=False, network=False):
    """教学实验、模拟平台和真实 Agent 的共同调用入口。"""
    validate_schema(args, schema)
    (policy or CapabilityPolicy()).check(name, writes=writes, shell=shell, network=network)
    return fn(**args)


def workspace_digest(root: Path) -> str:
    """验证绑定工作区内容；排除运行日志和工具缓存，不忽略源码和测试。"""
    digest = hashlib.sha256()
    if isinstance(root, dict):
        for alias, folder in sorted(root.items()):
            digest.update(json.dumps([alias, workspace_digest(Path(folder))]).encode())
        return digest.hexdigest()
    excluded = {'.git', '.sessions', '.spill', '.browser', '.agents', '__pycache__',
                '.pytest_cache', '.agent-runtime', '.sources'}
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in excluded
                         and not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            p = Path(folder) / name
            if p.is_symlink():
                digest.update(str(p.readlink()).encode())
                continue
            digest.update(p.relative_to(root).as_posix().encode('utf-8'))
            with p.open('rb') as f:
                for block in iter(lambda: f.read(65536), b''):
                    digest.update(block)
    return digest.hexdigest()


@dataclass
class Evidence:
    command: str = ''
    exit_code: int | None = None
    digest: str = ''

    def valid(self, root: Path) -> bool:
        return self.exit_code == 0 and bool(self.digest) and self.digest == workspace_digest(root)


class BudgetPool:
    """原子预留。运行中的兄弟任务不能重复使用同一份剩余预算。"""
    def __init__(self, total: int | None = None):
        self.total = total
        self.spent = 0
        self.reservations: dict[str, int] = {}
        self.lock = threading.Lock()

    def reserve(self, owner: str, amount: int):
        if amount < 1:
            raise ValueError('预算必须为正数')
        with self.lock:
            if owner in self.reservations:
                raise ValueError('重复预算预留')
            if self.total is not None and self.spent + sum(self.reservations.values()) + amount > self.total:
                raise RuntimeError('可用预算不足')
            self.reservations[owner] = amount

    def settle(self, owner: str, used: int):
        with self.lock:
            reserved = self.reservations.pop(owner)
            # 不丢弃超支：上游超额仍按实际用量记账。
            self.spent += max(0, used)
            return max(0, used - reserved)


@dataclass
class TaskMemory:
    """用户约束不经过模型摘要；工具结果只作为不可信资料。"""
    requests: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)

    def text(self):
        return json.dumps({'user_requests': self.requests, 'pending': self.pending,
                           'evidence_refs': self.evidence_refs}, ensure_ascii=False)
