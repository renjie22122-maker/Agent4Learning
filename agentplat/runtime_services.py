"""Typed lifecycle ports. Optional services cannot replace execution authority."""
from typing import Protocol


class MemoryPort(Protocol):
    def recall(self, agent, task, messages): ...
    def refresh(self, session_path): ...


class DefaultMemory:
    def recall(self, agent, task, messages):
        from .memory import inject
        inject(agent,task,messages)

    def refresh(self, session_path):
        from .memory import MemoryStore
        MemoryStore().refresh(session_path)


class RuntimeServices:
    def __init__(self, memory: MemoryPort | None = None):
        self.memory=memory or DefaultMemory()

    def prepare_model(self, cfg):
        from .model_capacity import discover
        from .billing import refresh
        discover(cfg);refresh(cfg)
