"""会话日志拥有的运行时不变量（对齐 DSH 的 `dsh-session/invariant`）。

## 这份契约是什么

会话日志是**恢复的唯一依据**。恢复逻辑（`session.replay()`）假设日志里
的事件满足一组结构关系；一旦这些关系坏了，恢复出来的状态就是错的，
而且**错得很安静** —— 它不会崩，只会接着从一个错误的前提往下跑。

所以这里检查的不是"方法存在不存在"，而是**日志里事件之间的关系**：

1. `seq` 严格递增且不重复 —— 否则"重放到第 N 条"就没有意义了。
2. 每个 `tool/call` 最终都有配对的 `tool/result`（按 `call_id` 配）。
   允许**尾部**悬空（那就是崩溃的现场，是有效信号）；
   但**中间**悬空意味着后面还有事件却没等到结果 —— 那是日志错了。
3. 同一个 `call_id` 不会被结果两次（重复回灌会让模型看到两份矛盾的输出）。
4. `session/created` 必须存在且只有一条；它是日志的锚点。
5. `step/start` 的轮次不允许回退 —— 恢复后的轮次要接着往上走。

第 2 条那个"中间悬空 vs 尾部悬空"的区分是这份检查的全部价值所在：
把尾部悬空也判为违规，就会把每次正常崩溃都报成 bug；
完全不检查悬空，又会让真正的日志损坏混在崩溃里看不出来。
"""

from __future__ import annotations

from typing import Any

OWNER = "agentplat.session"

#: 允许一条 `tool/call` 悬空的前提：它是**最后一条**相关事件。
#: 也就是说，"悬空"要么是崩溃现场（尾部），要么是错误（中间）。
TAIL_TOLERANCE = "尾部悬空 = 崩溃现场；中间悬空 = 日志损坏"


def install(registry: Any) -> None:
    from .invariants import InvariantRegistry

    assert isinstance(registry, InvariantRegistry)

    def installer(rep: Any) -> None:
        state: dict[str, Any] = {
            "last_seq": 0,
            "open_calls": {},      # call_id -> seq
            "called_ids": set(),
            "result_ids": set(),
            "created": 0,
            "seen_steps": [],
            "tool_results": 0,
            "tool_calls": 0,
            "anon_calls": 0,
            "anon_results": 0,
            "n_events": 0,
            "open_seq_at": {},
            "last_event_kind": "",
            "last_event_seq": None,
        }

        def on_event(ev: Any) -> None:
            # 钩子收到的是**完整 Event**（`.kind` / `.data` / `.seq`），
            # 由 `InvariantRegistry.dispatch()` 统一保证（见其 docstring）。
            #
            # ⚠ 这里曾经踩过一个静默失效的坑：dispatch 早期版本只传 `data`
            # 字典，而这个钩子按 `ev.kind` 读 —— 于是每个事件都被读成
            # `kind=""`，**所有分支都不命中**，检查等于没跑。
            # 更糟的是表现：报告上只出现一条被去重合并掉的
            # "检查自身异常"，看起来像个无关紧要的小毛病。
            # 教训：钩子绝不猜入参形状；分发方必须给完整对象。
            kind = getattr(ev, "kind", "")
            data = getattr(ev, "data", {}) or {}
            seq = getattr(ev, "seq", None)
            state["n_events"] += 1

            # ---- 1. seq 严格递增 ----
            if isinstance(seq, int):
                if seq <= state["last_seq"]:
                    rep.fail(
                        f"seq 必须严格递增，但 {seq} 出现在 {state['last_seq']} 之后",
                        check="seq 严格递增", event_seq=seq,
                        prev=state["last_seq"], cur=seq,
                    )
                state["last_seq"] = seq
            state["last_event_kind"] = kind
            state["last_event_seq"] = seq

            # ---- 4. session/created 恰好一条 ----
            if kind == "session/created":
                state["created"] += 1
                if state["created"] > 1:
                    rep.fail("session/created 出现了不止一次 —— 日志锚点不唯一",
                             check="created 唯一", event_seq=seq,
                             count=state["created"])
                for f in ("session_id", "task"):
                    if not data.get(f):
                        rep.fail(f"session/created 缺少 {f} —— 恢复时无法定位任务",
                                 check="created 完整", event_seq=seq, missing=f)

            # ---- 5. step/start 轮次不回退 ----
            elif kind == "step/start":
                it = data.get("iteration")
                if not isinstance(it, int):
                    rep.fail(f"step/start 的 iteration 不是整数：{it!r}",
                             check="step 轮次可读", event_seq=seq, got=repr(it))
                else:
                    if state["seen_steps"] and it <= state["seen_steps"][-1]:
                        rep.fail(
                            f"轮次回退：{it} 出现在 {state['seen_steps'][-1]} 之后"
                            f"（恢复后必须接着往上走）",
                            check="轮次不回退", event_seq=seq,
                            prev=state["seen_steps"][-1], cur=it,
                        )
                    state["seen_steps"].append(it)

            # ---- 2. tool/call 登记 ----
            elif kind == "tool/call":
                state["tool_calls"] += 1
                # 老版本 loop 把 call_id 写成常量 `"c"`（实测抓到过：
                # 20260927-224230-* 那几份日志里所有调用都是 `call_id="c"`），
                # 于是"同一 call_id 调两次"这条检查会把**所有**旧日志都判违规。
                # 那种日志无法再做逐次配对，检查降级为只统计总数，
                # 并把这件事作为提示报出来，而不是伪装成一个新发现的违规。
                cid = data.get("call_id") or ""
                if cid in ("", "c"):
                    state["anon_calls"] += 1
                    state["tool_calls"] += 0
                else:
                    if cid in state["called_ids"]:
                        rep.fail(f"同一个 call_id 被调用两次：{cid}",
                                 check="call_id 不重复", event_seq=seq, call_id=cid)
                    state["called_ids"].add(cid)
                    state["open_calls"][cid] = seq
                    # 关键补充：记录"这条 call 之后又来了多少事件"。
                    # 只数"悬空了几条"是不够的 —— 一条悬空且**后面还有事件**
                    # 同样是错误（真崩溃只会悬空在最后）。早期版本只判断
                    # "悬空数 > 1"，于是恰好 1 条中间悬空被完全漏掉：
                    # 检查看着是绿的，而它要抓的东西就在眼前。
                    state["open_seq_at"][cid] = state["n_events"]
                if not data.get("tool"):
                    rep.fail("tool/call 缺少工具名 —— 日志无法说明发生了什么",
                             check="call 可读", event_seq=seq)

            # ---- 3. tool/result 配对 ----
            elif kind == "tool/result":
                state["tool_results"] += 1
                cid = data.get("call_id") or ""
                if cid in ("", "c"):
                    state["anon_results"] += 1
                    # 匿名（旧格式）日志没法逐次配对，直接跳过配对检查。
                    # **跳过必须被记录**：否则"配对全对"和"根本没配对"
                    # 在报告上看起来一样。
                else:
                    if cid not in state["called_ids"]:
                        rep.fail(
                            f"tool/result 没有对应的 tool/call：{cid}"
                            f"（日志里出现了凭空的工具结果）",
                            check="result 有对应 call", event_seq=seq, call_id=cid,
                        )
                    if cid in state["result_ids"]:
                        rep.fail(
                            f"同一个 call_id 收到了两次结果：{cid}"
                            f"（模型会看到两份互相矛盾的输出）",
                            check="result 不重复", event_seq=seq, call_id=cid,
                        )
                    state["result_ids"].add(cid)
                    state["open_calls"].pop(cid, None)
                    state["open_seq_at"].pop(cid, None)

        def on_finish() -> None:
            """快照检查：日志读完后，只允许**最后一条** tool/call 悬空。"""
            open_seqs = sorted(state["open_calls"].values())
            # 悬空的判定不能只看"几条" —— 要看"悬空之后还有没有事件"。
            # 真崩溃只会悬空在**最后一条**；中间悬空说明后面的事件
            # 是在一个没有结果的前提下继续跑的，那个状态是错的。
            overtaken = {cid: state["n_events"] - at
                         for cid, at in state["open_seq_at"].items()
                         if cid in state["open_calls"] and state["n_events"] - at > 1}
            if overtaken:
                rep.fail(
                    f"{len(overtaken)} 个 tool/call 悬空之后**还有事件继续发生**"
                    f"（{sorted(overtaken)}，后面的 {sorted(overtaken.values())} 个"
                    f"事件是在没有结果的前提下跑的）。{TAIL_TOLERANCE}",
                    check="悬空 call 只允许在尾部",
                    dangling=sorted(overtaken),
                )
            elif len(open_seqs) > 1:
                rep.fail(
                    f"{len(open_seqs)} 个 tool/call 悬空（seq={open_seqs}）。"
                    f"{TAIL_TOLERANCE}",
                    check="悬空 call 只允许在尾部",
                    dangling=len(open_seqs), seqs=open_seqs[:8],
                )
            if state["created"] == 0:
                rep.fail("日志里没有 session/created —— 这不是一份可恢复的会话",
                         check="created 存在")
            # 旧格式（call_id 是常量）日志：配对检查被跳过了，必须**说出来**。
            # 不然"配对全对"和"根本没做配对"在报告上看起来是一样的。
            if state["anon_calls"]:
                rep.fail(
                    f"{state['anon_calls']} 次调用没有可用的 call_id"
                    f"（旧格式日志把 call_id 写成了常量）—— 逐次配对检查"
                    f"对这些事件**没有生效**，只统计了总数。"
                    f"这类日志无法再判断「哪一次调用的结果丢了」",
                    check="配对检查覆盖面", unpaired=state["anon_calls"],
                )
            # 调用与结果的**总数**关系：结果不可能比调用多。
            if state["tool_results"] > state["tool_calls"]:
                rep.fail(
                    f"tool/result 比 tool/call 还多：{state['tool_results']} > "
                    f"{state['tool_calls']}",
                    check="结果数不超过调用数",
                    results=state["tool_results"], calls=state["tool_calls"],
                )

        def on_reset() -> None:
            """清空累积状态，让 `audit()` 可以重复跑。

            不清的话第二次审计会从第一次的残留继续 —— 报出来的违规
            指向上一次的事件，而报告上看起来像是在说这一次。

            ⚠ 必须**显式列出每个字段**。原来写成"遍历 state、
            是 int 就置 0"，看着很聪明，但漏字段时会静默留下脏值：
            实测 `created` 没被清零，于是第二次审计凭空报出
            "日志里没有 session/created" —— 而这条错误的报错看起来
            完全合理，极难定位。宁可啰嗦，不要聪明。
            """
            state["last_seq"] = 0
            state["open_calls"] = {}
            state["called_ids"] = set()
            state["result_ids"] = set()
            state["created"] = 0
            state["seen_steps"] = []
            state["tool_results"] = 0
            state["tool_calls"] = 0
            state["anon_calls"] = 0
            state["anon_results"] = 0
            state["n_events"] = 0
            state["open_seq_at"] = {}
            state["last_event_kind"] = ""
            state["last_event_seq"] = None

        rep.on_reset(on_reset)
        rep.on("session/created", on_event)
        rep.on("step/start", on_event)
        rep.on("step/end", on_event)
        rep.on("assistant/message", on_event)
        rep.on("tool/call", on_event)
        rep.on("tool/result", on_event)
        rep.on("spill/applied", on_event)
        rep.on("compaction/applied", on_event)
        rep.on("session/closed", on_event)
        rep.on_snapshot(on_finish)

    registry.register(OWNER, installer)
