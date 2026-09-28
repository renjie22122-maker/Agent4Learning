"""界面与运行时状态的桥接：环境标识、配置缓存、切换后端。

单独一个模块是为了**避免循环导入**：`ui.py` 需要环境标识，而环境标识来自
运行时配置，配置又被页面和 demo 服务共用。把这点状态收在这里最干净。
"""

from __future__ import annotations

import threading

from .llmconfig import LLMConfig

_lock = threading.RLock()
_cfg: LLMConfig | None = None
_runtime: dict = {"backend": "mock", "switched_at": 0.0, "last_error": ""}


def get_config() -> LLMConfig:
    global _cfg
    with _lock:
        if _cfg is None:
            _cfg = LLMConfig.load()
        return _cfg


def set_config(cfg: LLMConfig) -> None:
    global _cfg
    with _lock:
        _cfg = cfg


def mark_backend(name: str, error: str = "") -> None:
    import time

    with _lock:
        _runtime["backend"] = name
        _runtime["switched_at"] = time.time()
        _runtime["last_error"] = error


def runtime() -> dict:
    with _lock:
        return dict(_runtime)


def env_badge() -> str:
    """右上角常驻的"当前后端"标识。

    必须显眼：把模拟器输出误当成真实模型输出，是最容易让人对系统能力产生
    错误判断的一类坑，所以这个标识出现在**每一页**。
    """
    from .ui import esc, pill

    cfg = get_config()
    if cfg.provider == "real" and cfg.base_url and (cfg.model_or("mid")):
        label = cfg.model_or("mid")
        return (
            f'<div class=env>{pill("真实 LLM", "ok")} '
            f'<span class=mono>{esc(label)}</span></div>'
        )
    return (
        f'<div class=env>{pill("内置模拟器", "warn")} '
        f'<a href="/settings">去接入真实模型</a></div>'
    )
