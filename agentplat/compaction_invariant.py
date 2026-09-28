"""上下文压缩拥有的运行时不变量（对齐 DSH 的 `dsh-compaction/invariant`
的"压缩流配对"）。

## 这份契约是什么

压缩是**唯一一个会主动丢信息的操作**。别的模块出错最多是慢或贵，
压缩出错是"悄悄把约束弄丢了，然后 agent 带着错的前提继续跑 20 轮"。
所以它的不变量检查得最细：

1. `tokens_after <= tokens_before`。压缩把上下文**变大**了，那它就不叫压缩。
2. `summary_calls == 0` 时不允许 `summarized > 0` —— 没调模型却说摘要了老消息，
   中间一定有一段是假的。
3. `summarized > 0` 必须伴随 `summary_calls > 0`（对称的那一半）。
4. `pruned` 与真实占位符数量必须相等。**这条抓的是"记账与实际不一致"**：
   `pruned=32` 而历史里只有 30 条 `[已剪枝]`，说明有 2 条被算进了账但没真剪，
   于是"省了多少 token"是虚高的 —— 而这个数字会被写进报告给人看。
5. 摘要是**模型写的**，所以消息头（人设 + 用户原始任务书）必须逐字保留。
   本项目真的踩过这条：`summarize()` 原来只固化前两条里的 system 消息，
   用户的原始任务书被折进了模型摘要 —— 任务书经过模型"转述"是不可接受的。
6. 一次压缩最多产生**一条** `[历史摘要]`。多于一条说明连续压缩没有把上一条
   摘要合并掉，摘要会越堆越多（每一条都占上下文，压缩反而变成增长源）。

第 5 条需要一个"读当前消息列表"的函数，通过 `attach_messages()` 注入；
没有注入时这条不生效 —— `tools/test_invariants.py` 里有断言防止忘记注入。
"""

from __future__ import annotations

from typing import Any

OWNER = "agentplat.compaction"

PLACEHOLDER_MARK = "[已剪枝]"
SUMMARY_MARK = "[历史摘要"


def install(registry: Any) -> None:
    from .invariants import InvariantRegistry

    assert isinstance(registry, InvariantRegistry)

    def installer(rep: Any) -> None:
        state: dict[str, Any] = {
            "count": 0,
            "sum_before": 0,
            "sum_after": 0,
        }

        # 钩子收到的是**完整 Event**（`.kind` / `.data` / `.seq`），
        # 由 `InvariantRegistry.dispatch()` 统一保证（见其 docstring）。
        def on_compaction(ev: Any) -> None:
            data = ev.data
            state["count"] += 1
            before = _num(data.get("tokens_before"), "tokens_before", rep, data)
            after = _num(data.get("tokens_after"), "tokens_after", rep, data)
            pruned = _num(data.get("pruned"), "pruned", rep, data, allow_none=True)
            summarized = _num(data.get("summarized"), "summarized", rep,
                              data, allow_none=True)
            calls = _num(data.get("summary_calls"), "summary_calls", rep,
                         data, allow_none=True)
            if before is None or after is None:
                return
            state["sum_before"] += before
            state["sum_after"] += after

            # ---- 1. 压缩不能变大 ----
            if after > before:
                rep.fail(
                    f"压缩后反而更大：{before:,} → {after:,} tokens。"
                    f"压缩把上下文变大了，那它就不是压缩",
                    check="压缩后不更大", before=before, after=after,
                )

            # ---- 2/3. 摘要的两半必须对称 ----
            if calls == 0 and summarized:
                rep.fail(
                    f"没有调用模型（summary_calls=0）却声称摘要了 "
                    f"{summarized} 条老消息",
                    check="摘要与调用对称", summarized=summarized, calls=calls,
                )
            if summarized and not calls:
                rep.fail(
                    f"摘要了 {summarized} 条老消息却没有模型调用记录 —— "
                    f"这段摘要不可能来自模型",
                    check="摘要必有调用", summarized=summarized,
                )
            if calls and not summarized:
                rep.fail(
                    f"调了 {calls} 次摘要模型却没有摘要任何老消息"
                    f" —— 这次调用白花了钱",
                    check="调用必有产出", calls=calls,
                )

            # ---- 4. 剪枝记账与实际一致（需要消息快照）----
            msgs = _messages_for(rep)
            if msgs is not None and pruned is not None:
                actual = sum(1 for m in msgs
                             if PLACEHOLDER_MARK in (getattr(m, "content", "") or ""))
                if actual != pruned:
                    rep.fail(
                        f"剪枝记账与实际不一致：报告说剪了 {pruned} 条，"
                        f"历史里只有 {actual} 条占位符。"
                        f"'省了多少 token'这类数字是给人看的，虚高比不报还糟",
                        check="剪枝记账一致", reported=pruned, actual=actual,
                    )

            # ---- 5. 消息头逐字保留 ----
            if msgs is not None and summarized:
                _check_head(rep, msgs, data)

            # ---- 6. 摘要不堆积 ----
            if msgs is not None:
                n_sum = sum(1 for m in msgs
                            if SUMMARY_MARK in (getattr(m, "content", "") or ""))
                if n_sum > 1:
                    rep.fail(
                        f"历史里同时存在 {n_sum} 条摘要消息 —— 连续压缩没有"
                        f"合并上一条摘要，摘要会越堆越多（压缩变成增长源）",
                        check="摘要不堆积", count=n_sum,
                    )
                if summarized and n_sum == 0:
                    rep.fail(
                        f"报告说摘要了 {summarized} 条老消息，但历史里"
                        f"找不到任何 `{SUMMARY_MARK}` 消息",
                        check="摘要真的插入了",
                    )

        def on_finish() -> None:
            if state["count"] and state["sum_after"] > state["sum_before"]:
                rep.fail(
                    f"累计压缩后总 token 反而变多：{state['sum_before']:,} → "
                    f"{state['sum_after']:,}",
                    check="累计压缩有效",
                    before=state["sum_before"], after=state["sum_after"],
                )

        def on_reset() -> None:
            """清空累积状态（见 session_invariant 里同名的说明）。"""
            state.update(count=0, sum_before=0, sum_after=0)

        rep.on_reset(on_reset)
        rep.on("compaction/applied", on_compaction)
        rep.on_snapshot(on_finish)

    registry.register(OWNER, installer)


def _num(v: Any, name: str, rep: Any, data: dict[str, Any],
         allow_none: bool = False) -> int | None:
    """读一个必须是整数（≥0）的记账字段。读不出来就是违规，不是跳过。

    为什么不容忍缺失：这些数字全部会用在一份给人看的报告里。
    缺字段时"静默跳过检查"会让报告看起来一切正常，而实际什么都没查。
    """
    if v is None and allow_none:
        return 0
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        rep.fail(f"compaction/applied 的 {name} 不是非负整数：{v!r}",
                 check="压缩记账可读", field=name, got=repr(v))
        return None
    return v


def _check_head(rep: Any, msgs: list[Any], data: dict[str, Any]) -> None:
    """消息头（连续 system 前缀 + 紧跟的第一条 user）必须逐字保留。"""
    head_n = 0
    while head_n < len(msgs) and getattr(msgs[head_n], "role", "") == "system":
        head_n += 1
    if head_n < len(msgs) and getattr(msgs[head_n], "role", "") == "user":
        head_n += 1
    if head_n == 0:
        rep.fail(
            "压缩之后消息头为空 —— 连系统消息都没了。"
            "摘要绝不会是合法的替代品：它是模型写的，不是原始人设",
            check="消息头保留",
        )
        return
    joined = "\n".join(getattr(m, "content", "") or "" for m in msgs[:head_n])
    if SUMMARY_MARK in joined:
        rep.fail(
            "摘要消息挤进了消息头位置 —— 说明头部的原始消息（人设 / 用户任务书）"
            "已经不在原位，被摘要顶掉了",
            check="消息头未被摘要顶替", head_n=head_n,
        )


_MESSAGES_READERS: list[Any] = []


def attach_messages(registry: Any, read: Any) -> None:
    """注入"读当前 messages 列表"的函数（返回 list[ChatMessage]）。

    有三条检查依赖它（剪枝记账一致、消息头保留、摘要不堆积）。
    用注入而不是 import loop：压缩模块不该认识 Agent 类型，
    这样它在单测与离线重放里都能跑。
    """
    _MESSAGES_READERS.append((registry, read))


def _messages_for(rep: Any) -> list[Any] | None:
    for reg, read in reversed(_MESSAGES_READERS):
        if reg is rep.registry:
            try:
                msgs = read()
            except Exception:  # noqa: BLE001
                return None
            return msgs if isinstance(msgs, list) else None
    return None
