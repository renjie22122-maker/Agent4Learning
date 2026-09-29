"""ContextRuntime: extracted lifecycle responsibility with stable event semantics."""
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
from .loop_types import *
from .tool_protocol import _brief, _salvage_tool_args, _clean_path

class ContextRuntime:
    def context_stats(self) -> dict:
        """当前的上下文占用与压缩账目。

        ## 为什么必须把它暴露出来

        在加这个之前，上下文用量**完全不可见** —— 用户只看到"轮次/花费"，
        看不到"离撑爆还有多远"，也看不到压缩到底省了多少。
        结果是两个都很难受的体验：

        * 任务突然变慢变贵，不知道为什么（其实是每轮都在重发一份很长的历史）；
        * 压缩悄悄发生了，用户不知道自己的原始要求已经被摘要替代了一部分。

        数据一直都在（`Compactor.pressure()` / `Compactor.history`），
        只是没人把它端出来。**可观测性缺的往往不是采集，而是呈现** ——
        这和本项目里"没有指标就没有优化"是同一件事。

        返回的字段都是"当前值 + 窗口 + 阈值"三元组，而不是一个百分比：
        百分比单独出现时无法判断好坏（60% 算高吗？取决于窗口是 64k 还是 8k）。
        """
        msgs = self._last_messages or []
        used, ratio = self.compactor.pressure(msgs)
        win = self.compactor.context_window or 0
        hist = self.compactor.history
        saved = sum(c.saved for c in hist)
        spent = sum(c.summary_usd for c in hist)
        return {
            "used_tokens": used,
            "window_tokens": win,
            "window_source": ('显式任务配置' if self._explicit_context_window is not None else __import__('agentplat.model_capacity', fromlist=['resolve']).resolve(self.cfg)[1]),
            "ratio": ratio,
            "threshold_ratio": self.compactor.threshold_ratio,
            "threshold_tokens": int(win * self.compactor.threshold_ratio),
            "messages": len(msgs),
            "compactions": len(hist),
            "pruned_total": sum(c.pruned for c in hist),
            "summarized_total": sum(c.summarized for c in hist),
            "tokens_saved": saved,
            "summary_calls": sum(c.summary_calls for c in hist),
            "summary_usd": round(spent, 6),
            # 净收益 = 省下的 token − 压缩自己花掉的成本折算。
            # 两个数字都给出来，让用户自己判断值不值 —— 只报"省了多少"
            # 是半个真相（压缩是要花钱调模型的）。
            "net_usd": round(-spent, 6),
            "events": [
                {"before": c.tokens_before, "after": c.tokens_after,
                 "pruned": c.pruned, "summarized": c.summarized,
                 "usd": round(c.summary_usd, 6),
                 "reason": c.reason}
                for c in hist[-20:]
            ],
            "spill": self.spill.summary(),
        }


    def _compact_for_turn(self, messages, res, it, emit):
        if self.compactor.needs_compaction(messages):
            _, ratio = self.compactor.pressure(messages)
            cres = self.compactor.maybe_compact(messages)
            if cres.applied:
                emit(LoopStep(
                    it, "guard",
                    f"🗜 上下文压缩：{cres.tokens_before:,} → "
                    f"{cres.tokens_after:,} tokens（-{cres.ratio:.0%}）",
                    f"压力 {ratio:.0%} 达阈值。剪枝 {cres.pruned} 条工具结果"
                    f"（零调用），摘要 {cres.summarized} 条消息"
                    f"（${cres.summary_usd:.6f}）。"
                    + (f" ⚠ 摘要缺段落：{cres.missing_sections}"
                       if cres.missing_sections else ""),
                    ok=not cres.missing_sections,
                ))
                self.session.append(
                    "compaction/applied",
                    tokens_before=cres.tokens_before,
                    tokens_after=cres.tokens_after,
                    pruned=cres.pruned, summarized=cres.summarized,
                    summary_calls=cres.summary_calls,
                    usd=round(cres.summary_usd, 8),
                    missing=cres.missing_sections,
                )
                if self.guard is not None and cres.summary_usd:
                    # 摘要自己也要记账 —— 否则"压缩省的钱"和"压缩花的钱"
                    # 混在一起，看不出净收益。
                    self.guard.record(0, 0, 0.0, 0.0, tag="compaction")
                    self.guard.spent_usd += cres.summary_usd
                if self.compactor.last_billing:
                    bill = self.compactor.last_billing
                    res.usd += bill['usd']
                    res.tokens_in += bill['in_tokens']
                    res.tokens_out += bill['out_tokens']
                    self.session.append('billing/usage', **bill)
                    if getattr(self, 'on_usage', None): self.on_usage(bill)
