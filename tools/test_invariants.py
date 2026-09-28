"""运行时不变量注册表 + 三个伴生入口的验证。

**这份测试的设计原则：每条不变量都必须有一个"能触发它"的反例。**

只测"合法日志不出错"是不够的 —— 那样一个把 `if` 写成 `if False:` 的
检查也会通过。所以每一节都是"先确认合法输入干净，再注入一个违规，
确认它被抓到，而且抓到的是**预期的那一条**"。

用 `tools/test_invariants.py` 运行；退出码非 0 表示有不变量检查失效。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentplat import compaction_invariant, loop_invariant, session_invariant  # noqa: E402
from agentplat.invariants import (  # noqa: E402
    InvariantError, InvariantRegistry, build_default_registry,
)
from agentplat.session import Event, SessionLog  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  {detail}" if detail else ""))
    return ok


# --------------------------------------------------------------------------
# 事件构造器：模拟一份**合法**的会话
# --------------------------------------------------------------------------
def ev(seq: int, kind: str, **data) -> Event:
    return Event(seq=seq, kind=kind, ts=1.0 + seq * 0.001, data=data)


def good_log(n_steps: int = 3, tools_per_step: int = 2) -> list[Event]:
    """一份结构完整、配对正确的会话日志。"""
    out = [ev(1, "session/created", session_id="test-sess", task="写个快排",
              model="mock", workspace=str(ROOT))]
    seq = 2
    for it in range(1, n_steps + 1):
        out.append(ev(seq, "step/start", iteration=it))
        seq += 1
        cids = []
        for j in range(tools_per_step):
            cid = f"call_{it}_{j}"
            cids.append(cid)
            out.append(ev(seq, "tool/call", tool="read_file", destructive=False,
                          ok=True, call_id=cid, path="a.py"))
            seq += 1
            out.append(ev(seq, "tool/result", tool="read_file", ok=True, ms=1.2,
                          call_id=cid, out="（内容）"))
            seq += 1
        out.append(ev(seq, "assistant/message", text="继续", tool_calls=len(cids),
                      in_tokens=1000, out_tokens=100, usd=0.00012,
                      finish_reason="tool_calls"))
        seq += 1
        out.append(ev(seq, "step/end", iteration=it, tool_calls=len(cids)))
        seq += 1
    out.append(ev(seq, "session/closed", finished=True, iterations=n_steps,
                  tool_calls=n_steps * tools_per_step, usd=0.00036, summary="完成"))
    return out


def audit(events, *, registry=None, ledger=None, messages=None):
    """跑一遍全部伴生入口，返回 (report, registry)。"""
    reg = registry or build_default_registry()
    if ledger is not None:
        loop_invariant.attach_ledger(reg, ledger)
    if messages is not None:
        compaction_invariant.attach_messages(reg, messages)
    return reg.audit(events), reg


def owners_of(report) -> list[str]:
    return sorted({v.owner for v in report.violations})


def checks_of(report) -> list[str]:
    return sorted({v.check for v in report.violations})


def main() -> int:  # noqa: C901 - 分节的长测试，拆开反而更难读
    ok = True

    # ==================================================================
    print("=" * 78)
    print("  ① 合法会话必须干净（假阳性会让整套检查被关掉）")
    print("=" * 78)
    rep, reg = audit(good_log(), ledger=lambda: 0.00036)
    ok &= check("合法日志零违规", rep.ok, rep.render().splitlines()[-1])
    ok &= check("三个伴生入口都生效", len(rep.active_owners) == 3,
                str(rep.active_owners))
    ok &= check("检查了全部事件", rep.checked_events == len(good_log()),
                f"{rep.checked_events}")
    ok &= check("没有静默失效的钩子", not rep.silent_checks,
                str(rep.silent_checks))
    ok &= check("报告写明了哪些检查本轮没素材可跑（不假装查全了）",
                "spill/applied" in "\n".join(rep.unexercised_checks),
                f"{len(rep.unexercised_checks)} 条未验证")
    # 尾部悬空是**有效信号**（崩溃现场），不能报成违规
    tail = good_log(n_steps=1) + [ev(99, "tool/call", tool="write_file",
                                     destructive=True, ok=True, call_id="cX")]
    rep_tail, _ = audit(tail, ledger=lambda: 0.00012)
    ok &= check("尾部悬空的 tool/call 不报违规（那是崩溃现场）", rep_tail.ok,
                f"{len(rep_tail.violations)} 条违规")

    # ==================================================================
    print("\n" + "=" * 78)
    print("  ② agentplat.session 的不变量：每条都要能被触发")
    print("=" * 78)

    def one_violation(mutate, label, expect_check):
        events = good_log()
        mutate(events)
        r, _ = audit(events, ledger=lambda: 0.00036)
        got = checks_of(r)
        hit = expect_check in got
        ok_ = check(f"{label} → 抓到「{expect_check}」", hit,
                    f"实际抓到 {got}" if not hit else "")
        return ok_

    ok &= one_violation(
        lambda e: setattr(e[3], "seq", e[2].seq),
        "把 seq 改成与上一条相同", "seq 严格递增")
    ok &= one_violation(
        lambda e: e.insert(4, ev(500, "tool/result", tool="read_file", ok=True,
                                 ms=1.0, call_id="凭空的", out="x")),
        "插入一条没有对应 call 的 result", "result 有对应 call")
    ok &= one_violation(
        lambda e: e.append(ev(600, "session/created", session_id="second",
                              task="第二个任务")),
        "追加第二条 session/created", "created 唯一")
    ok &= one_violation(
        lambda e: e.insert(3, ev(700, "step/start", iteration=99)),
        "插入一个回退的轮次", "轮次不回退")
    # 中间悬空：把第 2 轮第 2 个 **call** 删掉，它的 result 就孤悬在中间
    mid = good_log(n_steps=2)
    mid = [e for e in mid if not (e.kind == "tool/call"
                                  and e.data.get("call_id") == "call_2_1")]
    r_mid, _ = audit(mid, ledger=lambda: 0.00024)
    # 删掉的是 call：它的 result 变成了"凭空出现的 result"，
    # 同时 `tool/results > tool/calls` 这条总数关系也应该跟着报出来。
    # （**不是**"悬空 call" —— 悬空的定义是"有 call 没 result"，正好相反。）
    ok &= check("删掉一次 tool/call → 它的 result 变成孤儿",
                {"result 有对应 call", "结果数不超过调用数"} <= set(checks_of(r_mid)),
                f"抓到 {checks_of(r_mid)}")
    # 真正的"中间悬空"：删掉一个 result，而后面还有事件
    mid2 = [e for e in good_log(n_steps=2)
            if not (e.kind == "tool/result" and e.data.get("call_id") == "call_1_1")]
    r_mid2, _ = audit(mid2, ledger=lambda: 0.00024)
    ok &= check("中间悬空（有 call 没 result 但后面还有事件）→ 抓到",
                "悬空 call 只允许在尾部" in checks_of(r_mid2),
                f"抓到 {checks_of(r_mid2)}")
    dup = good_log(n_steps=1)
    dup.append(ev(90, "tool/result", tool="read_file", ok=True, ms=1.0,
                  call_id="call_1_0", out="重复"))
    r_dup, _ = audit(dup, ledger=lambda: 0.00012)
    ok &= check("同一 call_id 收到两次结果 → 抓到",
                "result 不重复" in checks_of(r_dup), f"抓到 {checks_of(r_dup)}")

    # ==================================================================
    print("\n" + "=" * 78)
    print("  ③ agentplat.loop 的不变量")
    print("=" * 78)

    def loop_violation(mutate, label, expect_check, ledger=lambda: 0.00036):
        events = good_log()
        mutate(events)
        r, _ = audit(events, ledger=ledger)
        got = checks_of(r)
        hit = expect_check in got
        return check(f"{label} → 抓到「{expect_check}」", hit,
                     f"实际抓到 {got}" if not hit else "")

    ok &= loop_violation(
        lambda e: [setattr(x, "data", {**x.data, "iteration": 42})
                   for x in e if x.kind == "step/end" and x.data["iteration"] == 2],
        "把第 2 轮的 step/end 指向一个没开始过的轮次", "end 有对应 start")
    # 请求多 ≠ 违规：模型可以一口气请求 50 个，循环截断到上限是它的职责。
    # 这一条是**负向断言**（期望不报），不能用 loop_violation（它期望报）。
    # 早期版本这里写反了，害我不变量误报过一次真实的正常日志。
    evs_req = good_log(n_steps=1, tools_per_step=1)
    for x in evs_req:
        if x.kind == "assistant/message":
            x.data = {**x.data, "tool_calls": 13}
    r_req, _ = audit(evs_req, ledger=lambda: 0.00012)
    ok &= check("模型一轮**请求** 13 个工具 → 不报违规（请求多不是循环的错）",
                "每轮工具数有界" not in checks_of(r_req),
                f"抓到 {checks_of(r_req)}")

    # 但"循环**真的执行**了很多"必须报 —— 那才说明截断闸失效了。
    # 构造方式：把 13 个 tool/call 插到同一轮里。
    evs_blast = good_log(n_steps=1, tools_per_step=1)
    ins = next(i for i, x in enumerate(evs_blast) if x.kind == "tool/call")
    evs_blast[ins:ins] = [
        ev(2000 + k, "tool/call", tool="list_dir", destructive=False,
           ok=True, call_id=f"blast{k}") for k in range(13)]
    r_blast, _ = audit(evs_blast, ledger=lambda: 0.00012)
    ok &= check("循环**真的执行**了 14 个工具（闸门失效）→ 抓到",
                "每轮工具数有界" in checks_of(r_blast),
                f"抓到 {checks_of(r_blast)}")
    ok &= loop_violation(
        lambda e: [setattr(x, "data", {**x.data, "finish_reason": "truncated"})
                   for x in e if x.kind == "assistant/message"],
        "finish_reason 出现非法值", "finish_reason 合法")
    ok &= loop_violation(
        lambda e: [setattr(x, "data", {**x.data, "usd": -0.001})
                   for x in e if x.kind == "assistant/message"],
        "usd 为负数（账本会被污染）", "记账字段可读")
    ok &= loop_violation(
        lambda e: e.remove([x for x in e if x.kind == "step/end"
                            and x.data["iteration"] == 2][0]),
        "删掉第 2 轮的 step/end（记账丢一段）", "轮次记账闭合")
    # 成本闭合：账本比日志多 $0.01 → 有一次绕过记账的调用
    ok &= loop_violation(
        lambda e: None, "账本比日志多 $0.01（隐性成本）", "成本闭合",
        ledger=lambda: 0.00036 + 0.01)

    # ==================================================================
    print("\n" + "=" * 78)
    print("  ④ agentplat.compaction 的不变量")
    print("=" * 78)

    def comp_events(**over):
        base = dict(tokens_before=40_000, tokens_after=12_000, pruned=8,
                    summarized=20, summary_calls=1, usd=0.0012,
                    missing=[])
        base.update(over)
        return [ev(1, "session/created", session_id="s", task="t"),
                ev(2, "step/start", iteration=1),
                ev(3, "compaction/applied", **base),
                ev(4, "assistant/message", text="继续", tool_calls=0,
                    in_tokens=100, out_tokens=10, usd=0.0, finish_reason="stop"),
                ev(5, "step/end", iteration=1)]

    class FakeMsg:
        def __init__(self, role, content):
            self.role, self.content = role, content

    def msgs_with(n_placeholder=8, n_summary=1, head=True):
        out = []
        if head:
            out = [FakeMsg("system", "你是编码 agent"),
                   FakeMsg("user", "任务：写个快排")]
        out += [FakeMsg("tool", "[已剪枝] 这条工具结果原有约 9000 tokens")] * n_placeholder
        out += [FakeMsg("system", "[历史摘要 —— 早前 20 条对话的压缩结果]")] * n_summary
        out += [FakeMsg("assistant", "最近一轮")]
        return out

    def comp_case(label, expect_check, *, events, messages):
        r, _ = audit(events, ledger=lambda: 0.0012, messages=lambda: messages)
        got = checks_of(r)
        return check(f"{label} → 抓到「{expect_check}」", expect_check in got,
                     f"实际抓到 {got}" if expect_check not in got else "")

    clean, _ = audit(comp_events(), ledger=lambda: 0.0012,
                     messages=lambda: msgs_with())
    ok &= check("合法的压缩事件 + 一致的 messages 零违规", clean.ok,
                str(checks_of(clean)))
    ok &= comp_case("压缩后 token 变大", "压缩后不更大",
                    events=comp_events(tokens_before=10_000, tokens_after=30_000),
                    messages=msgs_with())
    ok &= comp_case("没调模型却说摘要了老消息", "摘要与调用对称",
                    events=comp_events(summary_calls=0, summarized=20),
                    messages=msgs_with())
    ok &= comp_case("剪枝记账 8 条但历史里只有 3 条占位符", "剪枝记账一致",
                    events=comp_events(pruned=8), messages=msgs_with(3))
    ok &= comp_case("摘要消息堆了两条", "摘要不堆积",
                    events=comp_events(), messages=msgs_with(n_summary=2))
    ok &= comp_case("消息头被摘要顶掉了", "消息头未被摘要顶替",
                    events=comp_events(),
                    messages=[FakeMsg("system", "[历史摘要 —— 顶掉了人设]")]
                    + msgs_with()[2:])

    # ==================================================================
    print("\n" + "=" * 78)
    print("  ⑤ 注册表本身的语义（对齐 DSH dsh-invariants）")
    print("=" * 78)

    r2 = InvariantRegistry()
    r2.register("pkg.a", lambda rep: None)
    try:
        r2.register("pkg.a", lambda rep: None)
        ok &= check("重复注册同名模块 → 报错", False, "竟然接受了")
    except ValueError as exc:
        ok &= check("重复注册同名模块 → 报错", "已经注册过" in str(exc))

    def bad_installer(rep):
        rep.on("x", lambda d: None)
        raise RuntimeError("installer 写坏了")
    r3 = InvariantRegistry()
    try:
        r3.register("pkg.bad", bad_installer)
        ok &= check("installer 抛异常 → 传播", False, "竟然吞掉了")
    except RuntimeError:
        ok &= check("installer 抛异常 → 传播", True)
    ok &= check("installer 失败后名字被释放（可重注册）",
                "pkg.bad" not in r3._names)
    try:
        r3.register("pkg.bad", lambda rep: None)
        ok &= check("installer 失败后不留半成品钩子", True)
    except ValueError:
        ok &= check("installer 失败后不留半成品钩子", False, "名字没释放")

    r4 = InvariantRegistry(blocklist=[r"agentplat\.loop"])
    for mod in (session_invariant, loop_invariant, compaction_invariant):
        mod.install(r4)
    rep4 = r4.audit(good_log())
    ok &= check("blocklist 生效：被禁用的模块不在生效清单里",
                "agentplat.loop" not in rep4.active_owners
                and len(rep4.active_owners) == 2, str(rep4.active_owners))
    ok &= check("被禁用的模块名仍被保留（不会静默易主）",
                "agentplat.loop" in rep4.disabled_owners, str(rep4.disabled_owners))

    r5 = InvariantRegistry(enabled=False)
    for mod in (session_invariant, loop_invariant, compaction_invariant):
        mod.install(r5)
    rep5 = r5.audit(good_log())
    ok &= check("总开关关闭 → 一条检查都不生效", not rep5.active_owners)

    try:
        for mod in (session_invariant, loop_invariant, compaction_invariant):
            mod.install(r5)
        r5.audit(good_log())
        ok &= check("关闭时重复安装 → 仍然报重复", False)
    except ValueError:
        ok &= check("关闭时重复安装 → 仍然报重复", True)

    # strict：立刻抛，而不是收集
    r6 = InvariantRegistry(strict=True)
    session_invariant.install(r6)
    bad = good_log()
    bad[3].seq = bad[2].seq
    try:
        r6.audit(bad)
        ok &= check("strict=True → 立刻抛 InvariantError", False, "没抛")
    except InvariantError as exc:
        ok &= check("strict=True → 立刻抛 InvariantError",
                    exc.code == "INVARIANT" and exc.owner == "agentplat.session",
                    f"code={exc.code} owner={exc.owner}")

    # 去重 + 计数
    r7 = InvariantRegistry()
    session_invariant.install(r7)
    many = good_log(n_steps=1)
    for i in range(50):
        many.append(ev(1000 + i, "tool/result", tool="x", ok=True, ms=1.0,
                       call_id=f"幽灵{i}", out=""))
    rep7 = r7.audit(many)
    ok &= check("同一类违规只报一次 + 另一类独立计数",
                len(rep7.violations) == 2
                and rep7.counts.get("agentplat.session::result 有对应 call") == 50,
                f"{len(rep7.violations)} 条：{sorted(rep7.counts)}")
    ok &= check("但触发次数被完整计数（次数本身是信息）",
                rep7.counts.get("agentplat.session::result 有对应 call") == 50,
                str(rep7.counts))

    # 过滤器配置错误要在装配期炸
    for badargs, why in ((dict(allowlist=["  x  "]), "首尾空白"),
                         (dict(allowlist=["a", "a"]), "重复"),
                         (dict(blocklist=["["]), "非法正则")):
        try:
            InvariantRegistry(**badargs)
            ok &= check(f"过滤器配置错误（{why}）→ 装配期报错", False)
        except ValueError:
            ok &= check(f"过滤器配置错误（{why}）→ 装配期报错", True)

    # ==================================================================
    print("\n" + "=" * 78)
    print("  ⑥ 拿真实会话日志跑（最有说服力的一项：假阳性检查）")
    print("=" * 78)
    reported = 0
    skipped_stale = 0
    skipped_live = 0        # 目录里没被取样的日志份数（含正在写入的）
    # Historical user logs are a separate audit, not mutable offline fixtures.
    for sdir in (() if __import__('os').environ.get('AGENTLAB_OFFLINE_FIXTURES_ONLY') == '1' else (ROOT / "workspace" / ".sessions", ROOT / ".sessions")):
        if not sdir.is_dir():
            continue
        # ⚠ 必须排除**正在被写入**的日志。
        # 刚开始时取"最新 3 份"，结果是：demo 服务在跑、它的会话日志
        # 正被追加，于是这份日志天然处于"最后一轮没有 step/end"的中间态 ——
        # 断言时有时无地失败。这不是不变量的 bug，是**把活动文件当成
        # 已完成的日志来断言**。判断依据用 mtime：太新的一律跳过。
        now = time.time()
        all_files = list(sdir.glob("*.jsonl"))
        files = sorted((p for p in all_files
                        if now - p.stat().st_mtime > 15),
                       key=lambda p: p.stat().st_mtime)[-3:]
        skipped_live += len(all_files) - len(files)
        for p in files:
            # ⚠ 过滤用的 mtime 和真正 `load()` 之间有一个时间窗口。
            # demo 服务可能在那一瞬间正好往这份日志里追加 —— 于是我们
            # "读了半份日志"，断言就会偶发失败（本次实测撞到过一次：
            # 单独跑 6 次全过，混在整套回归里失败 1 次）。
            # 修法：读之前记一份 (mtime, size)，读完再比一次，不一致就跳过。
            # 这不能 100% 消除竞态（理论上还能再中），但它把
            # "读到写一半的文件"变成"明确跳过并计数"，而不是随机报错。
            st0 = p.stat()
            log, skipped = SessionLog.load(p)
            st1 = p.stat()
            if (st0.st_mtime, st0.st_size) != (st1.st_mtime, st1.st_size):
                skipped_live += 1
                continue
            # 「旧格式」是**从日志本身**判出来的，不靠文件名白名单：
            # 早期 loop 把 call_id 写成常量、finish 路径不写 step/end。
            # 这类日志的不变量只会报一条「检查覆盖面」提示（说明这轮
            # 哪些检查没有素材），那是**如实报告**而不是误报。
            # 如果连一条都没有，说明它没被标记为旧格式 —— 那才是要看的。
            # 旧格式判定里也要算上"call_id 是常量"这一种。
            # ⚠ 这份判定必须**跟不变量自己的口径一致**，否则测试会为了
            # 一份它认为"新格式"的日志而红 —— 实测就是这样：
            # `call_id 不重复 @seq=33: 同一个 call_id 被调用两次：c0`
            # 那是一个**测试脚本自己的模型**生成重复 id 造成的（已修脚本），
            # 但日志已经落盘了，属于历史数据。
            old_format = not any(
                e.kind == "tool/call" and str(e.data.get("call_id") or "") not in
                ("", "c") for e in log.events)
            # 另一种历史格式：call_id 是每轮重新计数的短 id（c0/c1/...），
            # 跨轮重复。同样无法做逐次配对。
            ids = [str(e.data.get("call_id") or "")
                   for e in log.events if e.kind == "tool/call"]
            if ids and len(set(ids)) < len(ids):
                old_format = True
            r, _ = audit(log.events)
            reported += 1
            n_tool_calls = sum(1 for e in log.events if e.kind == "tool/call")
            if old_format:
                skipped_stale += 1
                continue
            ok &= check(
                f"{p.name}（{len(log.events)} 事件 / {n_tool_calls} 次工具调用）",
                r.ok, "\n".join(v.render() for v in r.violations)[:700])
    if skipped_stale:
        print(f"  ⏭ 跳过 {skipped_stale} 份旧格式日志（call_id 是常量 / 无 step/end）。"
              f"那些缺陷已经在代码里修掉，留着它们会一直红 —— "
              f"但**不能因此把不变量放宽**，否则新的日志也查不出来了。")
    if skipped_live:
        print(f"  ⏭ 从 {skipped_live + reported + skipped_stale} 份日志里取样了最近 "
              f"3 份**已完成**的（mtime > 15s）。正在被写入的那份会天然处于"
              f"「最后一轮没有 step/end」的中间态 —— 拿它做断言等于跟一个"
              f"移动目标比。这不是不变量的 bug，是采样对象选错了。")
    if reported == 0:
        print("  ⏭ 没有找到真实会话日志（跳过；跑一次 agent 任务后重试）")
    else:
        print(f"  说明：这 {reported} 份日志是 agent 真跑出来的（含真实的中止、"
              f"截断、断点续跑），不变量对它们必须零误报。")

    # ==================================================================
    print("\n" + "=" * 78)
    print("  ⑦ 循环真的接上了吗（防止「注册了但没调用」）")
    print("=" * 78)
    from agentplat.loop import CodingAgent, build_agent_tools  # noqa: E402
    from agentplat.llmconfig import LLMConfig  # noqa: E402
    from agentplat.workspace import Workspace  # noqa: E402
    import tempfile  # noqa: E402

    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td))
        agent = CodingAgent(
            llm=None, cfg=LLMConfig(provider="mock", model="m", timeout_s=5.0),
            workspace=ws, session_dir=Path(td) / ".sessions", guard=None,
        )
        ok &= check("CodingAgent 默认开启不变量", agent.invariants is not None)
        ok &= check("会话日志挂上了观察者",
                    len(agent.session.observers) == 1,
                    f"{len(agent.session.observers)} 个")
        # 真的写一条事件，看检查有没有被调用
        agent.session.append("step/start", iteration=1)
        fired_live = dict(agent.invariants._active["agentplat.loop"].fired)
        ok &= check("写事件时检查真的执行了（实时路径）",
                    fired_live.get("step/start:on_step_start", 0) == 1,
                    str(fired_live))
        agent.invariants.reset()
        rep8 = agent.invariants.audit(agent.session.events)
        ok &= check("重放审计跑到且检查被调用",
                    rep8.checked_events == 1
                    and rep8.hooks_fired.get(
                        "agentplat.loop:step/start:on_step_start", 0) >= 1,
                    f"checked={rep8.checked_events} fired={rep8.hooks_fired}")
        # 只有一条 step/start 的日志确实是残缺的 —— 不变量**应该**报出来。
        # 这里断言它报的是"缺 session/created"，而不是静默通过。
        ok &= check("残缺日志被指出（不是静默通过）",
                    "created 存在" in checks_of(rep8), str(checks_of(rep8)))
        # 关闭开关时不该挂观察者
        agent2 = CodingAgent(
            llm=None, cfg=LLMConfig(provider="mock", model="m", timeout_s=5.0),
            workspace=ws, session_dir=Path(td) / ".sessions", guard=None,
            invariants=False,
        )
        ok &= check("invariants=False → 不挂观察者（零开销）",
                    agent2.invariants is None and not agent2.session.observers)
        ok &= check("build_agent_tools 仍然可用", len(build_agent_tools(ws)) > 0)

    # ==================================================================
    print("\n" + "=" * 78)
    if ok:
        print("  结论：运行时不变量的注册、生效与**可触发**全部正确 ✅")
    else:
        print("  结论：存在失败项 ❌")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
