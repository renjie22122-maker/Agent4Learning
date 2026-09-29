"""Tool argument compatibility helpers and display formatting."""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence
from agentlab.providers import ChatMessage
from agentlab.tracing import Tracer
from .agent_tools import AgentTool, build_agent_tools, schemas
from .compaction import Compactor
from .guard import CostGuardTripped
from .session import CheckpointError, SessionLog, replay
from .spill import DEFAULT_MAX_INLINE_BYTES, SpillPolicy
from .workspace import Workspace, WorkspaceError
from .model_client import ModelClient
def _salvage_tool_args(name: str, raw: str) -> dict | None:
    """从**被截断的工具参数 JSON** 里抢救出可用的字段。

    实测动机：模型用 write_file/append_file 写大文件时，参数 JSON 会在
    content 字符串中间断掉。此时若整个丢弃，模型会把同样的内容重写一遍 ——
    又撞上限、又烧钱，形成"重试 → 再截断"的死循环（真跑时连续出现 4 次）。

    做法：只对"字符串字段"做抢救 —— 找到 `"key": "` 之后把余下内容按
    JSON 字符串转义还原来，直到结尾（没有收尾引号就是截断）。
    这样模型已经生成的那部分代码不会白费。
    """
    if not raw or not raw.lstrip().startswith("{"):
        return None
    import re as _re

    out: dict = {}
    for m in _re.finditer(r'"(\w+)"\s*:\s*"', raw):
        key = m.group(1)
        body = raw[m.end():]
        chars: list[str] = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                chars.append({"n": "\n", "t": "\t", "r": "\r",
                              '"': '"', "\\": "\\", "/": "/"}.get(nxt, nxt))
                if nxt == "u" and i + 5 < len(body):
                    try:
                        chars[-1] = chr(int(body[i + 2:i + 6], 16))
                        i += 6
                        continue
                    except ValueError:
                        pass
                i += 2
                continue
            if ch == '"':
                # 只有遇到"后面紧跟 , 或 }"才算真正结束，否则是转义序列的一部分
                rest = body[i + 1:].lstrip()
                if rest[:1] in (",", "}"):
                    break
            chars.append(ch)
            i += 1
        val = "".join(chars)
        if val:
            out[key] = val
    # **路径字段要清洗**：截断处常残留杂字符（实测出现过 `test_quicksort.py<`，
    # 直接拿去写文件会得到一个意料之外的文件名）。路径只保留合法字符，
    # 清完仍不合法就整条丢弃 —— 宁可让这次调用失败，也不要写错地方。
    if "path" in out:
        cleaned = _clean_path(out["path"])
        if not cleaned:
            return None
        out["path"] = cleaned
    return out if any(v for v in out.values()) else None


_PATH_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
               "-_./\\ ")


def _clean_path(raw: str) -> str:
    """截断残留的路径清洗。只保留路径合法字符，并去掉首尾杂讯。"""
    s = raw.strip().strip('"').strip("'")
    # 只取到第一个明显不属于路径的字符为止
    buf: list[str] = []
    for ch in s:
        if ch in _PATH_OK or ord(ch) > 127:  # 允许中文文件名
            buf.append(ch)
        else:
            break
    out = "".join(buf).strip()
    # 去掉末尾的点和分隔符（`a.py.` / `dir/` 这类截断残留）
    out = out.rstrip(".\\/ ")
    return out if out and out not in (".", "..") else ""


def _brief(args: dict, limit: int = 70) -> str:
    """把参数压成一行短摘要，便于界面展示。"""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        s = str(v).replace("\n", "\\n")
        if len(s) > 26:
            s = s[:26] + "…"
        parts.append(f"{k}={s!r}")
    out = ", ".join(parts)
    return out[:limit] + ("…" if len(out) > limit else "")


def normalize_calls(tool_calls,used_call_ids):
    normalized = []
    batch_ids = set()
    for c in tool_calls:
        c = {**c, 'function': dict(c.get('function') or {})}
        cid = c.get('id')
        if not cid or cid in used_call_ids or cid in batch_ids:
            c['id'] = 'rejected_' + uuid.uuid4().hex
            c['function'] = {'name': '__invalid_call_id', 'arguments': '{}'}
        batch_ids.add(c['id'])
        normalized.append(c)
    tool_calls = normalized
    return tool_calls
