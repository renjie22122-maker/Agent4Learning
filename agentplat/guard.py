"""成本与安全护栏：接真实 LLM 后，这是**安全机制**，不是优化项。

为什么必须放在"发起调用之前"
----------------------------
事后统计花费没有意义 —— 钱已经花了。护栏必须在**下一次真实请求出网之前**
就拦住它，所以它包在引擎外层，检查点位于 LLM 阶段真正发请求的那一步之前。

三道闸门（任一触发就立刻停止并明确报错）：

1. ``max_llm_calls_per_run`` —— 防"写错的循环"。这是最常见的烧钱方式。
2. ``max_usd_per_run`` —— 防"单次实验超出预期"。按 token 单价实时累计。
3. ``max_tokens_per_request`` —— 防"上下文爆炸"。超长 prompt 会成倍放大成本。

另外提供 ``dry_run``：**只估算、不出网**。批量实验前先干跑，看清楚要花多少钱。

设计取舍：触发上限时**抛异常**而不是"静默降级到模拟器"。因为静默降级会让你
以为实验跑完了、数据是真的，实际上后半段是本地模板 —— 这比直接失败更危险。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


class CostGuardTripped(RuntimeError):
    """护栏触发。**不是错误，是保护。**"""

    def __init__(self, reason: str, spent_usd: float, calls: int):
        super().__init__(reason)
        self.reason = reason
        self.spent_usd = spent_usd
        self.calls = calls


@dataclass
class CostGuard:
    """实时成本闸门。线程安全（实验会并发跑）。

    ## 上限是可选的（`None` = 不设限）

    早期版本把 `max_usd` / `max_calls` 写成带默认值的必填项（$1.0 / 400 次），
    于是**永远存在一个硬天花板**。实测的体验很糟：

        循环被硬性上限终止：触达成本护栏

    它停下来的位置往往正是"活儿快干完了"的地方 —— 编码 agent 的
    收尾阶段（跑测试 → 修 → 再跑 → finish）本身就要好几轮，
    而每一轮都在计费。于是在离完成一步之遥的地方被砍掉，
    钱已经花了，结果却是"未完成"。这是最差的一种花钱方式。

    现在两个上限都允许为 `None`（不设限）。默认值仍然给建议值，
    但调用方可以显式关掉 —— **约束应该由使用者决定，而不是由框架替他决定**。

    ## 但有一个上限**不能**关

    `max_tokens_per_request` 不是成本控制，是**协议约束**：
    超过模型的上下文窗口，provider 会直接 400。关掉它不会省钱，
    只会把"超限"从"提前拦住"变成"一个看不懂的上游报错"。
    所以它保持有默认值，且不允许设成 None。
    """

    max_usd: float | None = None
    max_calls: int | None = None
    max_tokens_per_request: int = 8192
    dry_run: bool = False

    calls: int = 0
    spent_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    blocked_calls: int = 0
    started_at: float = field(default_factory=time.time)
    by_tag: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------
    def preflight(self, in_tokens: int, tag: str = "",
                  max_tokens: int | None = None) -> None:
        """**发请求之前**调用。任一上限已达就抛异常，绝不"再看看"。

        ``max_tokens`` 可按场景覆盖：编码 agent 的上下文天然比问答大得多
        （工具定义 + 文件内容 + 多轮历史），用问答的阈值去卡它只会自伤 ——
        实测第 14 轮上下文 8250 tokens 就被默认的 8192 拦下，而那次请求
        本来是正常的。
        """
        cap = self.max_tokens_per_request if max_tokens is None else max_tokens
        with self._lock:
            if self.dry_run:
                # 干跑：只计数，不出网（真正的拦截在上层 build_server 里）
                self.calls += 1
                return
            if self.max_calls is not None and self.calls >= self.max_calls:
                self.blocked_calls += 1
                raise CostGuardTripped(
                    f"已达模型调用次数上限 {self.max_calls} 次（防止循环烧钱）。"
                    f"确认无误后调大 max_llm_calls_per_run，或设为不设限。",
                    self.spent_usd, self.calls,
                )
            if self.max_usd is not None and self.spent_usd >= self.max_usd:
                self.blocked_calls += 1
                raise CostGuardTripped(
                    f"已达花费上限 ${self.max_usd:.4f}（当前 ${self.spent_usd:.4f}）。"
                    f"确认无误后调大 max_usd_per_run，或设为不设限。",
                    self.spent_usd, self.calls,
                )
            if in_tokens > cap:
                self.blocked_calls += 1
                raise CostGuardTripped(
                    f"单请求输入 {in_tokens} tokens 超过上限 "
                    f"{cap}（上下文爆炸会让成本成倍放大）。",
                    self.spent_usd, self.calls,
                )
            self.calls += 1

    def record(self, in_tokens: int, out_tokens: int,
               price_in_per_m: float, price_out_per_m: float, tag: str = "") -> float:
        """记账。返回本次花费。"""
        cost = (in_tokens * price_in_per_m + out_tokens * price_out_per_m) / 1_000_000
        with self._lock:
            self.tokens_in += in_tokens
            self.tokens_out += out_tokens
            self.spent_usd += cost
            if tag:
                self.by_tag[tag] = self.by_tag.get(tag, 0.0) + cost
        return cost

    # ------------------------------------------------------------------
    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at

    @property
    def remaining_usd(self) -> float:
        """剩余额度。**不设限时返回 `inf`** —— 返回 0 会让上层以为"没钱了"。"""
        if self.max_usd is None:
            return float("inf")
        return max(0.0, self.max_usd - self.spent_usd)

    @property
    def remaining_calls(self) -> int:
        if self.max_calls is None:
            return 10 ** 9
        return max(0, self.max_calls - self.calls)

    def tripped(self) -> bool:
        if self.max_usd is not None and self.spent_usd >= self.max_usd:
            return True
        return self.max_calls is not None and self.calls >= self.max_calls

    def render(self) -> None:
        from agentlab.util import kv, note, phase

        phase("成本与安全护栏", "(真实 key 下这是安全机制)")
        # 不设限时必须**显示成"无上限"**，而不是显示一个假的数字。
        # 显示 "0 / 0" 会让人以为护栏已经卡死了。
        cap_calls = "无上限" if self.max_calls is None else str(self.max_calls)
        cap_usd = "无上限" if self.max_usd is None else f"${self.max_usd:.4f}"
        kv("模型调用次数", f"{self.calls} / {cap_calls}")
        kv("累计花费", f"${self.spent_usd:.6f} / {cap_usd}")
        if self.max_usd is None and self.max_calls is None:
            kv("剩余额度", "不设限（用完即停由你自己判断）")
        else:
            kv("剩余额度", f"{self.remaining_calls} 次 / ${self.remaining_usd:.4f}")
        kv("token", f"in {self.tokens_in:,} / out {self.tokens_out:,}")
        kv("耗时", f"{self.elapsed_s:.1f}s")
        if self.blocked_calls:
            kv("被护栏拦下", f"{self.blocked_calls} 次")
        if self.by_tag:
            note("按阶段分摊：")
            total = self.spent_usd or 1.0
            for k, v in sorted(self.by_tag.items(), key=lambda kv_: -kv_[1]):
                note(f"    {k:<24} ${v:.6f}  ({v / total * 100:.1f}%)")
        if self.tripped():
            note("⚠ 已触达上限 —— 后续调用会被拦下（这是保护，不是故障）。")


def estimate_run(n_requests: int, prompt_tokens: int, out_tokens: int,
                 price_in_per_m: float, price_out_per_m: float,
                 llm_calls_per_request: float = 1.5) -> dict:
    """干跑估算：跑之前先算清楚要花多少钱。

    为什么要有这个：真实 LLM 下"跑一下试试"是有代价的。批量实验前先估算，
    这是负责任的默认动作。
    """
    calls = n_requests * llm_calls_per_request
    tin = calls * prompt_tokens
    tout = calls * out_tokens
    usd = (tin * price_in_per_m + tout * price_out_per_m) / 1_000_000
    return {
        "requests": n_requests,
        "est_llm_calls": round(calls, 1),
        "est_input_tokens": int(tin),
        "est_output_tokens": int(tout),
        "est_usd": round(usd, 6),
        "est_minutes": round(calls * 2.0 / 60, 2),  # 按每次 2s 粗估
    }
