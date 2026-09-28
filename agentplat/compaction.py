"""上下文压缩（compaction）：长会话不让历史无限膨胀。

和 spill 的分工（两者解决**不同**的问题）
-----------------------------------------
* **spill** 管"单条工具结果过大" —— 一次 pytest 输出 14KB，挪出热路径。
* **compaction** 管"轮数多了历史本身太长" —— 45 轮之后，每轮重发的历史
  就是几万 token，跟单条大小无关。

本项目实测：一次编码任务 31 轮消耗 **188,425 输入 token**；45 轮时涨到
**347,774**，而且模型每一步都要把整段历史重发一遍 —— 成本随轮数近似平方增长。
spill 压不住这个增长，因为它是**逐条**封顶，累积起来照样线性上涨。

三层策略（顺序不能反，这是 DSH `dsh-compaction-basic` 的思路）
--------------------------------------------------------------
1. **先剪枝**（`prune_tool_results`）：把老的大块工具输出换成占位符。
   **改历史但零 LLM 调用** —— 最便宜，所以先做。
2. **不够再摘要**（`summarize`）：把最老的一段历史压成结构化 checkpoint，
   保留近期原文。这一步要花一次模型调用。
3. **必须验证没丢关键信息**：摘要最容易的失败方式是"压得很干净，
   但把任务约束弄丢了"，然后 agent 开始跑偏。

阈值语义（对齐 DSH）
--------------------
``threshold_ratio = 0.8`` —— 上下文压力到窗口的 80% 才触发，不必过早压缩。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from agentlab.providers import ChatMessage
from agentlab.tokens import count_messages, count_tokens

#: 触发压缩的压力比例（DSH `dsh-compaction-basic` 的 `thresholdRatio: 0.8`）
DEFAULT_THRESHOLD_RATIO = 0.8

#: 压缩后保留的近期原文比例（其余变摘要）。
#: DSH 用 0.16；本项目取 0.2 —— 编码任务里"最近改了什么"比"大概在干什么"重要，
#: 留多一点原文能显著降低 agent 重复劳动的概率。
DEFAULT_KEEP_RATIO = 0.20

#: 剪枝时，超过这个 token 数的老工具结果会被换成占位符
PRUNE_TOOL_ABOVE_TOKENS = 400

#: 最新 N 条消息永不压缩（正在进行的上下文）
NEVER_COMPACT_LAST = 4

#: 结构化摘要的段落。用固定结构而不是自由总结 ——
#: 固定结构能被**代码校验**（哪段空了就说明丢了信息），自由总结不能。
SUMMARY_SECTIONS = (
    "任务目标", "已完成", "已改动文件", "关键决定", "约束与要求", "待办",
)


@dataclass
class CompactionResult:
    applied: bool = False
    reason: str = ""
    tokens_before: int = 0
    tokens_after: int = 0
    pruned: int = 0
    summarized: int = 0
    summary_calls: int = 0
    summary_usd: float = 0.0
    missing_sections: list[str] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)

    @property
    def ratio(self) -> float:
        return self.saved / self.tokens_before if self.tokens_before else 0.0

    def render(self) -> None:
        from agentlab.util import kv, note, phase

        phase("上下文压缩", f"({self.reason})")
        kv("压缩前", f"{self.tokens_before:,} tokens")
        kv("压缩后", f"{self.tokens_after:,} tokens")
        kv("省下", f"{self.saved:,} tokens（{self.ratio:.1%}）")
        kv("剪枝工具结果", f"{self.pruned} 条（零模型调用）")
        kv("摘要消息", f"{self.summarized} 条（{self.summary_calls} 次模型调用，"
                       f"${self.summary_usd:.6f}）")
        if self.missing_sections:
            note(f"⚠ 摘要缺少段落：{self.missing_sections} —— 可能丢了关键信息")


class Compactor:
    """上下文压缩器。不改模型行为，只改"给模型看多少历史"。"""

    def __init__(
        self,
        llm=None,
        cfg=None,
        context_window: int = 64_000,
        threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
        keep_ratio: float = DEFAULT_KEEP_RATIO,
        enabled: bool = True,
    ):
        self.llm = llm
        self.cfg = cfg
        # ⚠ 上下文窗口是**配置项，不是猜出来的**。
        # 本次实测：服务端的 /models 端点只返回模型名，不返回窗口大小，
        # 所以无法自动探测。默认 64000 是保守值，请按你所用模型的实际规格改。
        # 设小了会过早压缩（丢信息、多花摘要钱）；设大了会撞 provider 上限报错。
        self.context_window = context_window
        self.threshold_ratio = threshold_ratio
        self.keep_ratio = keep_ratio
        self.enabled = enabled
        self.history: list[CompactionResult] = []
        self.last_billing = None

    # ------------------------------------------------------------------
    def pressure(self, messages: Sequence[ChatMessage]) -> tuple[int, float]:
        """返回 (当前 token 数, 压力比)。"""
        n = count_messages(messages)
        return n, (n / self.context_window if self.context_window else 0.0)

    def needs_compaction(self, messages: Sequence[ChatMessage]) -> bool:
        if not self.enabled:
            return False
        _, ratio = self.pressure(messages)
        return ratio >= self.threshold_ratio

    # ------------------------------------------------------------------
    def maybe_compact(self, messages: list[ChatMessage]) -> CompactionResult:
        """按需压缩。**原地修改 messages**（列表会被重排）。"""
        before = count_messages(messages)
        self.last_billing = None
        res = CompactionResult(tokens_before=before, reason="未触发")
        if not self.enabled:
            res.reason = "已禁用"
            return res
        _, ratio = self.pressure(messages)
        if ratio < self.threshold_ratio:
            res.reason = f"压力 {ratio:.0%} 未达阈值 {self.threshold_ratio:.0%}"
            return res

        res.reason = f"压力 {ratio:.0%} ≥ 阈值 {self.threshold_ratio:.0%}"
        res.applied = True

        # ---- 第 1 层：剪枝（零模型调用，先做最便宜的）----
        keep_tail = max(NEVER_COMPACT_LAST, int(len(messages) * self.keep_ratio))
        res.pruned = self.prune_tool_results(messages, keep_tail)

        # ---- 第 2 层：还不够就摘要 ----
        after_prune = count_messages(messages)
        if after_prune / self.context_window >= self.threshold_ratio:
            summarized = self.summarize(messages, keep_tail)
            if summarized is not None:
                res.summarized = summarized[0]
                res.summary_calls = summarized[1]
                res.summary_usd = summarized[2]
                if summarized[3]:
                    res.missing_sections = summarized[3]

        res.tokens_after = count_messages(messages)
        self.history.append(res)
        return res

    # ------------------------------------------------------------------
    @staticmethod
    def prune_tool_results(messages: list[ChatMessage],
                           keep_last: int) -> int:
        """把老的大块工具输出换成占位符。**零模型调用**。

        为什么先做这个：工具结果常常占了历史的一大半（一次 pytest 输出
        几万字符），而它们**大多已经没用了** —— agent 已经据此改完了代码。
        剪掉它们几乎不丢"决策依据"，但能省掉大量 token。

        保留最后 ``keep_last`` 条不动：那是 agent 正在处理的内容。
        """
        cutoff = max(0, len(messages) - keep_last)
        pruned = 0
        for i, m in enumerate(messages[:cutoff]):
            if m.role != "tool":
                continue
            n = count_tokens(m.content)
            if n <= PRUNE_TOOL_ABOVE_TOKENS:
                continue
            head = m.content[:180].replace("\n", " ")
            messages[i] = ChatMessage(
                "tool",
                f"[已剪枝] 这条工具结果原有约 {n} tokens，"
                f"内容已随上下文压缩移除。开头：{head}…\n"
                f"（需要原文请回取会话记录或 spill；禁止据此重放有副作用的工具；不要凭这段摘要猜原始内容）",
                tool_call_id=m.tool_call_id,
            )
            pruned += 1
        return pruned

    # ------------------------------------------------------------------
    def summarize(self, messages: list[ChatMessage], keep_last: int):
        """把老历史压成结构化摘要，插在系统消息之后、近期原文之前。

        返回 ``(压缩条数, 模型调用次数, 花费, 缺失段落)``；无法摘要时返回 None。
        """
        if self.llm is None or self.cfg is None:
            return None
        if len(messages) <= keep_last + 2:
            return None

        # 切分：消息头 + 最老的对话 → 摘要；其余保留原文。
        #
        # 哪些属于"消息头"，必须逐字保留、**绝不能经过模型转述**：
        #   1) 开头连续的 system 消息（人设、背景资料、注入的检索结果）；
        #   2) 紧跟其后的第一条 user 消息 —— 那是用户的**原始任务书**。
        # 原来的实现只挑 ``messages[:2]`` 里的 system 角色，于是：
        #   · [system, user]           → user 被丢进 old，任务书被折进摘要；
        #   · [system, system, user]   → 上面那条同样中招（背景资料挤掉了 user）。
        # 实测就是靠 test_compaction 的 ② 才发现的：压缩把任务书"转述"没了。
        head_n = 0
        while head_n < len(messages) and messages[head_n].role == "system":
            head_n += 1
        if head_n < len(messages) and messages[head_n].role == "user":
            head_n += 1
        head_sys = list(messages[:head_n])
        start = head_n
        cutoff = len(messages) - keep_last
        # A tool result must keep its originating assistant call (including
        # multi-tool batches); raw message counts are not protocol boundaries.
        while cutoff > start and messages[cutoff].role == 'tool':
            cutoff -= 1
        old = messages[start:cutoff]
        tail = messages[cutoff:]
        if len(old) < 2:
            return None
        # 用户补充的约束逐字保留，不依赖摘要模型记住它们。
        pinned = [m for m in old if m.role == 'user']

        transcript = "\n".join(
            f"{m.role}: {(m.content or '')[:1500]}" for m in old
        )[:24_000]
        prompt = (
            "把下面这段 agent 工作记录压缩成结构化摘要，供后续继续工作使用。\n"
            "**只输出以下段落，每段都要有内容，不要省略任何一段**：\n"
            + "\n".join(f"{s}：" for s in SUMMARY_SECTIONS)
            + "\n\n要求：\n"
            "- 任务目标：一句话说清要做什么。\n"
            "- 已完成：已经做完的事（列点）。\n"
            "- 已改动文件：具体文件名，这是最重要的信息。\n"
            "- 关键决定：做过的技术选择及原因。\n"
            "- 约束与要求：用户提出的硬性要求（必须保留，不得遗漏）。\n"
            "- 待办：还没做完的事。\n"
            "不要编造原文里没有的内容。\n\n"
            f"=== 工作记录 ===\n{transcript}"
        )
        client = self.llm
        from .llm import OpenAIChatClient
        if isinstance(client, OpenAIChatClient):
            from dataclasses import replace
            client = OpenAIChatClient(replace(self.cfg, json_mode=False))
        try:
            text, usage = client.complete(
                self.cfg.model_or("mid"), [ChatMessage("user", prompt)],
                getattr(self.cfg, "timeout_s", 60.0),
            )
        except Exception:  # noqa: BLE001 - 摘要失败不能拖垮主循环
            usage = getattr(client, 'last_usage', None)
            if usage is not None:
                from .billing import record
                self.last_billing = record(self.cfg, usage, client=client, tag='compaction')
                return 0, 1, self.last_billing['usd'], ['摘要正文未完成，保留原文；已返回的用量仍计费']
            return None

        missing = [s for s in SUMMARY_SECTIONS if not re.search(r'^\s*(?:#+\s*)?(?:\*\*)?' + re.escape(s) + r'[：:]', text, re.MULTILINE)]
        if not text.lstrip().startswith(('任务目标：', '任务目标:', '# 任务目标', '**任务目标')):
            missing.append('摘要必须从任务目标段落开始')
        from .billing import record
        self.last_billing = record(self.cfg, usage, client=client, tag='compaction')
        usd = self.last_billing['usd']

        summary_msg = ChatMessage(
            "assistant",
            f"[历史摘要 —— 这是早前 {len(old)} 条对话的压缩结果，"
            f"原文已移除以节省上下文]\n{text}",
        )
        if missing:
            return 0, 1, usd, missing
        messages[:] = head_sys + pinned + [summary_msg] + tail
        return len(old), 1, usd, missing


def verify_summary_keeps_facts(summary_text: str, facts: Sequence[str]) -> float:
    """检查摘要是否保住了给定的关键事实，返回保留率。

    **压缩必须被验证**：最容易的失败方式是"压得很干净但把约束弄丢了"，
    然后 agent 开始跑偏，而你要花很久才发现问题出在摘要上。
    """
    if not facts:
        return 1.0
    kept = 0
    for f in facts:
        # 数字与文件名要精确匹配；中文短语允许宽松匹配
        if re.search(r"[\d./]", f):
            if f in summary_text:
                kept += 1
        elif f in summary_text:
            kept += 1
    return kept / len(facts)
