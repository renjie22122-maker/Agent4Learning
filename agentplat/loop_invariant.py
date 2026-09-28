"""Agent 循环拥有的运行时不变量（对齐 DSH 的 `dsh-agent-loop/invariant`）。

## 这份契约是什么

循环是"一个状态机 + 一本账"。这里检查的就是这两样东西的结构关系：

1. **轮次记账闭合**：`step/end` 的 `iteration` 必须等于本轮 `step/start` 的
   `iteration`。跨轮错位意味着"上一轮的结果算到了这一轮头上" ——
   成本归因、轮数统计、断点续跑全部跟着错。
2. **每轮工具调用数有界**：`assistant/message.tool_calls` 不能超过
   `MAX_TOOLS_PER_STEP`。超了说明循环漏掉了那道闸。
3. **finish_reason 取值合法**：只允许 `stop` / `length` / `tool_calls` /
   `content_filter` / 空。出现别的值说明 provider 层的解析把没识别的东西
   当成了正常值传上来 —— 而 `finish_reason` 直接决定"要不要告诉模型它被截断了"，
   传错就会静默地不告知，模型拿着半截 JSON 猜（实测这会白花 2~3 轮钱）。
4. **成本闭合**：会话日志里所有 LLM 调用与压缩的 `usd` 之和，
   必须等于账本上的 `spent_usd`。
   **这条是整份检查里最有价值的一条**：任何"绕过了记账的模型调用"
   都会在这里露出来。隐性成本是最贵的那种成本 —— 你看不到它，
   所以你的预算护栏永远拦不住它。

## 与 DSH 的差别

DSH 的 `dsh-agent-loop` 伴生入口检查的是"loop 构建请求重建" ——
把事件流重建出来的请求与循环实际发出的请求对比。本项目没有
"请求快照"这类事件（那需要把整个 prompt 也落盘，代价很大），
所以退而求其次，只检查**记账闭合**。

这是一个**真实的覆盖率缺口**，写在这里以免读者以为已经查全了：
"循环发出的 prompt 与事件流能重建出的 prompt 是否一致"这件事，
本项目**没有**运行时检查。想补的话，最省的做法是只在 `--debug-trace`
模式下把 prompt 的哈希落盘，然后比对哈希。
"""

from __future__ import annotations

from typing import Any

OWNER = "agentplat.loop"

#: 与 loop.py 的 MAX_TOOLS_PER_STEP 保持一致。故意写死而不是 import：
#: 如果哪天有人把 loop 里的上限调大，这个数字**必须**跟着人工确认 ——
#: 从 loop import 的话，两边会一起变大，检查就永远通过了。
#: （这类"故意重复常量"是检查代码里少见的正确做法。）
MAX_TOOLS_PER_STEP = 12

LEGAL_FINISH_REASONS = frozenset({"stop", "length", "tool_calls",
                                  "content_filter", "function_call", ""})


def install(registry: Any) -> None:
    from .invariants import InvariantRegistry

    assert isinstance(registry, InvariantRegistry)

    def installer(rep: Any) -> None:
        state: dict[str, Any] = {
            "cur_step": None,
            "steps_started": 0,
            "steps_ended": 0,
            "sum_usd": 0.0,
            "llm_calls": 0,
            "max_tools_in_step": 0,
            "requested_in_step": 0,
            "executed_in_step": 0,
            "missing_end": [],
            "closed_finished": False,
            "any_step_end": False,
            "started_its": [],
            "ended_its": [],
            "unmatched_ends": [],
        }

        # 钩子收到的是**完整 Event**（`.kind` / `.data` / `.seq`），
        # 由 `InvariantRegistry.dispatch()` 统一保证（见其 docstring）。
        # ⚠ 不要按 data 字典写 —— 那样 `.get` 会 AttributeError，
        # 而 dispatch 会把它包成"检查自身异常"，看起来只像个无关紧要的小毛病。
        def on_step_start(ev: Any) -> None:
            it = ev.data.get("iteration")
            state["cur_step"] = it
            state["steps_started"] += 1
            state["executed_in_step"] = 0
            if isinstance(it, int):
                state["started_its"].append(it)

        def on_tool_call(ev: Any) -> None:
            """统计**实际执行**的工具调用数，超限才判违规。

            这是"每轮工具数有界"这条检查的正确观测点：模型请求多少不重要，
            循环执行了多少才是契约。用请求数判会误报（实测：模型请求 50、
            循环执行 12，闸门正常却报"漏了"）。
            """
            state["executed_in_step"] = state.get("executed_in_step", 0) + 1
            if state["executed_in_step"] > MAX_TOOLS_PER_STEP:
                rep.fail(
                    f"本轮**执行了** {state['executed_in_step']} 个工具，"
                    f"超过每轮上限 {MAX_TOOLS_PER_STEP} —— 循环那道截断闸失效了",
                    check="每轮工具数有界",
                    got=state["executed_in_step"], limit=MAX_TOOLS_PER_STEP,
                )

        def on_step_end(ev: Any) -> None:
            data = ev.data
            state["any_step_end"] = True
            it = data.get("iteration")
            if isinstance(it, int):
                state["ended_its"].append(it)
            if state["cur_step"] is None:
                # 循环开始下一轮时 `iteration` 已经自增了，所以"finish 路径的
                # step/end 结束的是上一轮" —— 这条消息本身**不足以**判定违规，
                # 要留到读完日志用集合差分来判断（见 on_finish）。
                state["unmatched_ends"].append(it)
            state["cur_step"] = None

        def on_assistant(ev: Any) -> None:
            data = ev.data
            state["llm_calls"] += 1
            n = data.get("tool_calls", 0) or 0
            if not isinstance(n, int) or n < 0:
                rep.fail(f"tool_calls 不是非负整数：{n!r}", check="tool_calls 可读")
            # ⚠ 这里**不能**用"模型请求了几个"来判违规。
            #
            # 原来写的是 `elif n > MAX_TOOLS_PER_STEP: fail("循环漏掉了那道闸")`，
            # 结果在真实日志上误报：那一轮模型一口气请求了 50 个工具，
            # 日志如实记下「请求 50，保留 12」，而循环**确实只执行了 12 个**
            # —— 闸门工作正常，不变量却在喊"漏了"。
            #
            # 不变量要盯**实际发生了什么**（执行了几个），而不是
            # **上游提出了什么**。模型可以请求 50 个，那是它的自由；
            # 该被约束的是循环执行了多少。判定放到下面的 on_tool_call。
            state["requested_in_step"] = max(state["requested_in_step"], n)

            fr = data.get("finish_reason", "")
            if fr not in LEGAL_FINISH_REASONS:
                rep.fail(
                    f"finish_reason 取值非法：{fr!r}。合法值只有 "
                    f"{sorted(LEGAL_FINISH_REASONS)}。"
                    f"非法值会让'输出被截断'的判断失效，模型会拿着半截 JSON 猜",
                    check="finish_reason 合法", got=repr(fr),
                )

            for f, name in (("in_tokens", "in_tokens"), ("out_tokens", "out_tokens"),
                            ("usd", "usd")):
                v = data.get(f)
                if not isinstance(v, (int, float)) or v < 0:
                    rep.fail(f"{name} 不是非负数字：{v!r} —— 账本会被污染",
                             check="记账字段可读", field=name, got=repr(v))
            try:
                state["sum_usd"] += float(data.get("usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                pass

        def on_compaction(ev: Any) -> None:
            data = ev.data
            # 摘要自己也要花钱，也必须进账本 —— 否则"压缩省的钱"看起来
            # 比实际多（省的记了，花的没记）。
            try:
                state["sum_usd"] += float(data.get("usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                rep.fail(f"compaction/applied 的 usd 不可读：{data.get('usd')!r}",
                         check="压缩花费可读")

        def on_finish() -> None:
            # 用**集合差分**判断轮次记账是否闭合，而不是"每来一个 step/start
            # 就把上一个记成缺失"。后者看着等价，其实很容易写错配平方向 ——
            # 实测写错过一次：配平写反之后，**测试里删掉一个 step/end
            # 反而报不出违规**（假绿），而正常日志会被误报。差分与事件顺序
            # 无关，写不出这种错。
            started = set(state["started_its"])
            ended = set(state["ended_its"])

            # step/end 指向一个**从未开始过**的轮次 → 记账错位
            spurious = sorted(ended - started)
            if spurious:
                rep.fail(
                    f"step/end 指向没有 step/start 的轮次 {spurious}"
                    f" —— '第几轮出的问题'这个归因会失效",
                    check="end 有对应 start", iterations=spurious[:8],
                )

            missing = sorted(started - ended)
            # 整份日志一条 step/end 都没有 → 旧版本 loop 的记录格式
            if missing and not ended:
                rep.fail(
                    f"整份日志没有任何 step/end，但开始了 "
                    f"{state['steps_started']} 轮 —— 这是旧版本 loop 的记录格式"
                    f"（finish 路径不写 step/end，已修）。轮次记账闭合这条检查"
                    f"对这份日志**没有生效**",
                    check="检查覆盖面（旧格式日志）",
                    steps=state["steps_started"],
                )
                missing = []
            elif missing and not state["closed_finished"] and len(missing) == 1:
                # 没正常收尾 → 最后一轮被打断，没有 step/end 是**有效信号**
                missing = []
            if missing:
                rep.fail(
                    f"有 {len(missing)} 个轮次（{missing[:8]}）没有 step/end，"
                    f"但会话继续往下跑了 —— 轮次记账丢了一段",
                    check="轮次记账闭合", iterations=missing[:8],
                )
            if state["llm_calls"] == 0 and state["steps_started"] > 0:
                rep.fail(
                    f"开始了 {state['steps_started']} 轮却一次模型调用都没有 —— "
                    f"日志不完整（或者循环在空转）",
                    check="轮次与调用数匹配",
                )

        def on_closed(ev: Any) -> None:
            state["closed_finished"] = bool(ev.data.get("finished", False))

        def on_reset() -> None:
            """清空累积状态（见 session_invariant 里同名的说明）。"""
            state.update(cur_step=None, steps_started=0, steps_ended=0,
                         sum_usd=0.0, llm_calls=0, max_tools_in_step=0,
                         requested_in_step=0, executed_in_step=0,
                         missing_end=[], closed_finished=False,
                         any_step_end=False, started_its=[], ended_its=[],
                         unmatched_ends=[])

        rep.on_reset(on_reset)
        rep.on("session/closed", on_closed)
        rep.on("step/start", on_step_start)
        rep.on("tool/call", on_tool_call)
        rep.on("step/end", on_step_end)
        rep.on("assistant/message", on_assistant)
        rep.on("compaction/applied", on_compaction)
        rep.on_snapshot(on_finish)

        # 成本闭合是**跨模块**的：它要把会话日志的总额与账本对比，
        # 所以由调用方通过 `attach_ledger()` 注入一个读取函数。
        # 没有注入时这条检查不生效，但**必须在报告里体现为未生效** ——
        # 见 invariants.AuditReport.quiet_checks。
        rep.on_snapshot(lambda: _check_ledger(rep, state))

    registry.register(OWNER, installer)


# --------------------------------------------------------------------------
# 账本闭合：调用方注入
# --------------------------------------------------------------------------
_LEDGER_READERS: list[Any] = []


def attach_ledger(registry: Any, read: Any) -> None:
    """注入"读账本已花金额"的函数，用于成本闭合检查。

    `read()` 应返回账本上的累计美元数（本项目是 `guard.spent_usd`）。

    为什么用注入而不是直接 import guard：邀请式的依赖 —— 注册表与检查
    都不认识 `CostGuard`，这样它们可以在没有 guard 的场景（单测、
    lab、离线重放）里照样跑。代价是**忘记注入就会静默不检查**，
    所以 `tools/test_invariants.py` 里专门有一条断言防止这件事。
    """
    _LEDGER_READERS.append((registry, read))


def _check_ledger(rep: Any, state: dict[str, Any]) -> None:
    readers = [r for reg, r in _LEDGER_READERS if reg is rep.registry]
    if not readers:
        return
    try:
        ledger = float(readers[-1]())
    except Exception as exc:  # noqa: BLE001
        rep.fail(f"读账本失败：{type(exc).__name__}: {exc}", check="账本可读")
        return
    diff = abs(ledger - state["sum_usd"])
    # 容差取 1e-9：日志里的 usd 是 round(x, 8)，所以量级在 1e-9 以下的
    # 差异只可能来自四舍五入。**不要把这个容差放大**：它一小步一小步地
    # 放大，就等于把"隐性成本"重新放回系统里。
    if diff > 1e-9:
        rep.fail(
            f"成本不闭合：会话日志累计 ${state['sum_usd']:.9f}，"
            f"账本 ${ledger:.9f}，差 ${diff:.9f}。"
            f"差额说明有**绕过了记账的模型调用**（或者记了账却没落日志）",
            check="成本闭合",
            logged=round(state["sum_usd"], 9), ledger=round(ledger, 9),
            diff=round(diff, 9),
        )
