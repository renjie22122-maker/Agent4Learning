"""Host-owned restrictive guards. No guard may grant authority or rewrite a call.

Callbacks are trusted in-process code, not a security sandbox. The mandatory
capability check still runs after them and immediately before invocation.
"""
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ToolRequest:
    name: str
    writes: bool = False
    shell: bool = False
    network: bool = False


class ToolGuards:
    def __init__(self):
        self._guards: dict[str, Callable] = {}

    def register(self, name, guard):
        if not name or name in self._guards or not callable(guard):
            raise ValueError('Guard requires a unique name and callable')
        self._guards[name] = guard

    def check(self, request, arguments):
        from .runtime import PermissionDenied
        # Snapshot registration order; callbacks cannot mutate execution args.
        for name, guard in tuple(self._guards.items()):
            try:
                reason = guard(request, deepcopy(arguments))
            except Exception as exc:
                raise PermissionDenied(f'工具守卫 {name} 异常，未执行工具：{type(exc).__name__}') from exc
            if reason is not None:
                if not isinstance(reason, str) or not reason:
                    raise PermissionDenied(f'工具守卫 {name} 返回无效决定，未执行工具')
                raise PermissionDenied(f'工具守卫 {name} 拒绝：{reason}')
