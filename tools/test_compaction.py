"""验证上下文压缩（compaction）。

覆盖：
  ① 剪枝层：老的大块工具结果被换掉，**零模型调用**
  ② 摘要层：剪枝后仍超阈值 → 走结构化摘要（需要 LLM）
  ③ **摘要必须保住关键事实**（文件名/数字/约束）—— 这是压缩最危险的失败模式
  ④ 近期消息与系统消息**永不压缩**
  ⑤ 端到端：长会话下开/关压缩的输入 token 对比
  ⑥ 压缩本身也要记账（摘要的花费不能变成隐性成本）
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentlab.providers import ChatMessage, Usage  # noqa: E402
from agentlab.tokens import count_messages  # noqa: E402
from agentplat.compaction import (  # noqa: E402
    SUMMARY_SECTIONS,
    Compactor,
    verify_summary_keeps_facts,
)
from agentplat.llmconfig import LLMConfig  # noqa: E402
from agentplat.loop import CodingAgent  # noqa: E402
from agentplat.workspace import Workspace  # noqa: E402
from tools.test_support import temporary_workspace



class FakeSummarizer:
    """假装是模型的摘要器，输出结构化摘要并**故意包含关键事实**。"""

    def __init__(self, facts_to_keep=()):
        self.calls = 0
        self.facts = list(facts_to_keep)

    def complete(self, model, messages, timeout_s):
        self.calls += 1
        src = messages[0].content
        body = []
        for s in SUMMARY_SECTIONS:
            if s == "已改动文件" and self.facts:
                body.append(f"{s}：{', '.join(self.facts)}")
            elif s == "约束与要求":
                body.append(f"{s}：必须跑通 pytest；输出上限 3000 tokens")
            elif s == "任务目标":
                body.append(f"{s}：写一个快速排序模块并配测试")
            else:
                body.append(f"{s}：（摘要内容，源文本 {len(src)} 字符）")
        return "\n".join(body), Usage(800, 200, 0)


def check(name, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  {detail}" if detail else ""))
    return ok


def build_long_history(n_turns=40, tool_chars=3000):
    msgs = [ChatMessage("system", "你是编码 agent"),
            ChatMessage("user", "写一个快速排序并配测试，文件名 quicksort.py")]
    for i in range(n_turns):
        msgs.append(ChatMessage("assistant", f"第{i}轮：继续推进 " + "x" * 200))
        msgs.append(ChatMessage("tool", "工具输出：" + "y" * tool_chars,
                                tool_call_id=f"c{i}"))
    return msgs


def build_history_for(assistant_tokens_per_turn: int, n_turns: int = 30,
                      tool_chars: int = 36_000):
    """构造一个**剪枝后仍然超阈值**的长历史。

    为什么需要它：① 的 fixture（60 轮 × 200 字符工具结果）只有 ~7k tokens，
    剪枝后必然掉到阈值以下 —— 摘要层**永远不会被走到**。这类"因为 fixture
    太小所以没测到"的假绿最危险：断言永远通过，而代码路径从未执行。

    按 ``count_tokens`` 的真实公式（非 CJK 4 字符/token，每条消息 +4）反推：
      - 工具结果固定 ``tool_chars`` 字符（≈ 9000 tokens ≫ 400，
        保证剪枝层一定会动它）；
      - assistant 文本承担**不可剪枝**的份额 → 剪枝之后压力仍 ≥ 阈值。

    消息顺序与真实 loop 一致：``[system(人设), user(原始任务书), ...对话...]``。
    （真实 loop 是 ``messages = [ChatMessage("system", CODING_SYSTEM)]``，
    注入背景资料时变成 ``[system, system, user, ...]``。）
    """
    head = [ChatMessage("system", "你是编码 agent"),
            ChatMessage("user", "写一个快速排序并配测试，文件名 quicksort.py")]
    filler = "x" * max(0, assistant_tokens_per_turn * 4 - 12)
    msgs = list(head)
    for i in range(n_turns):
        msgs.append(ChatMessage("assistant", f"第{i}轮：继续推进 " + filler))
        msgs.append(ChatMessage("tool", "工具输出：" + "y" * tool_chars,
                                tool_call_id=f"c{i}"))
    return msgs, head


def main() -> int:
    ok = True

    # ---------------- ① 剪枝层 ----------------
    print("=" * 78)
    print("  ① 剪枝层：老的大块工具结果被换掉（零模型调用）")
    print("=" * 78)
    msgs = build_long_history()
    before = count_messages(msgs)
    c = Compactor(llm=None, cfg=None, context_window=20_000)
    res = c.maybe_compact(msgs)
    ok &= check("触发了压缩", res.applied)
    ok &= check("剪枝了多条工具结果", res.pruned > 0, f"{res.pruned} 条")
    ok &= check("没有调用模型", res.summary_calls == 0)
    ok &= check("token 明显下降", res.tokens_after < before * 0.6,
                f"{before:,} → {res.tokens_after:,}（-{res.ratio:.0%}）")
    placeholders = sum(1 for m in msgs if m.role == "tool" and "已剪枝" in m.content)
    ok &= check("占位符写进去了", placeholders == res.pruned, f"{placeholders} 条")
    ok &= check("占位符提示了不要猜内容",
                any("不要凭这段摘要猜" in m.content for m in msgs if m.role == "tool"))

    # ---------------- ② 摘要层 ----------------
    print("\n" + "=" * 78)
    print("  ② 摘要层：剪枝后仍超阈值 → 走结构化摘要")
    print("=" * 78)
    # 30 轮 × 800 tokens 的 assistant 文本（剪枝动不了）+ 30 条 36k 字符的
    # 工具结果（剪枝会动）→ 剪枝后剩 ~30k tokens，仍是阈值 16k 的近两倍。
    msgs2, head2 = build_history_for(assistant_tokens_per_turn=800, n_turns=30)
    # 先**验证 fixture 真的能走到摘要层**：剪枝必须发生，而且剪枝之后
    # 压力仍然 ≥ 阈值。否则后面的断言全是"代码路径没跑到"的假绿。
    probe = [ChatMessage(m.role, m.content, m.tool_call_id) for m in msgs2]
    before2 = count_messages(msgs2)
    pruned_probe = Compactor.prune_tool_results(probe, keep_last=8)
    after_prune_probe = count_messages(probe)
    ok &= check("压缩前就超阈值（否则根本不会触发）",
                before2 >= 20_000 * 0.8, f"{before2:,} tokens")
    ok &= check("剪枝后仍超阈值（fixture 有效）",
                after_prune_probe >= 20_000 * 0.8,
                f"压缩前 {before2:,} → 剪枝后 {after_prune_probe:,} tokens"
                f"（阈值 {int(20_000 * 0.8):,}，剪枝 {pruned_probe} 条）")
    fake = FakeSummarizer(facts_to_keep=["quicksort.py", "test_quicksort.py"])
    cfg = LLMConfig(provider="mock", model="fake", timeout_s=10.0)
    c2 = Compactor(llm=fake, cfg=cfg, context_window=20_000)
    res2 = c2.maybe_compact(msgs2)
    ok &= check("确实触发了压缩", res2.applied, res2.reason)
    ok &= check("调用了摘要模型", res2.summary_calls == fake.calls and res2.summary_calls > 0, f"{res2.summary_calls} 次")
    ok &= check("摘要了老消息", res2.summarized > 0, f"{res2.summarized} 条")
    ok &= check("出现历史摘要消息",
                any("历史摘要" in m.content for m in msgs2))
    ok &= check("摘要花费被记录", res2.summary_usd > 0, f"${res2.summary_usd:.6f}")
    ok &= check("六个段落齐全", not res2.missing_sections, str(res2.missing_sections))
    # 注意：summarize 会**重建** head_sys 的 ChatMessage 对象（内容相同、
    # 对象不同），所以这里比内容而不是 is —— 这是实现的真实语义。
    ok &= check("头两条（原始任务 + 系统约束）逐字保留",
                msgs2[0].role == head2[0].role
                and msgs2[0].content == head2[0].content
                and msgs2[1].role == head2[1].role
                and msgs2[1].content == head2[1].content,
                f"[{msgs2[0].role}] {msgs2[0].content[:20]}… / "
                f"[{msgs2[1].role}] {msgs2[1].content[:20]}…")
    ok &= check("摘要后压力真的降下来",
                res2.tokens_after < res2.tokens_before,
                f"{res2.tokens_before:,} → {res2.tokens_after:,}")

    # ---------------- ③ 关键事实保留 ----------------
    print("\n" + "=" * 78)
    print("  ③ 摘要必须保住关键事实（压缩最危险的失败模式）")
    print("=" * 78)
    summary_text = "\n".join(m.content for m in msgs2
                             if "历史摘要" in (m.content or ""))
    facts = ["quicksort.py", "test_quicksort.py", "pytest", "3000"]
    rate = verify_summary_keeps_facts(summary_text, facts)
    ok &= check("关键事实保留率 = 100%", rate == 1.0,
                f"{rate:.0%}（检查 {facts}）")
    # 反例：摘要里丢掉文件名时，校验必须发现
    bad_rate = verify_summary_keeps_facts("任务目标：写个排序\n已完成：没有",
                                          ["quicksort.py"])
    ok &= check("丢事实时校验能发现", bad_rate == 0.0, f"{bad_rate:.0%}")

    # ---------------- ④ 系统消息与近期消息不被压 ----------------
    print("\n" + "=" * 78)
    print("  ④ 系统消息与最近几条永不压缩")
    print("=" * 78)
    msgs3 = build_long_history(n_turns=40)
    sys_before = [m.content for m in msgs3 if m.role == "system"]
    tail_before = [m.content for m in msgs3[-4:]]
    c3 = Compactor(llm=None, cfg=None, context_window=20_000)
    c3.maybe_compact(msgs3)
    sys_after = [m.content for m in msgs3 if m.role == "system"]
    tail_after = [m.content for m in msgs3[-4:]]
    ok &= check("系统消息未改", sys_before[0] == sys_after[0])
    ok &= check("最近 4 条未改", tail_before == tail_after)

    # 关键：真实的 loop 在注入了背景资料时，历史头是
    # [system 人设, system 背景资料, user 原始任务书, ...]。
    # 只固化"前两条里的 system"会把 user 任务书挤掉 —— 这正是 ② 暴露的 bug。
    msgs3b, _ = build_history_for(assistant_tokens_per_turn=800, n_turns=30)
    msgs3b.insert(1, ChatMessage("system", "[背景资料]\n注入的检索结果 " + "z" * 400))
    msgs3b.insert(2, ChatMessage("user", "硬约束：不得引入第三方依赖，必须能离线跑"))
    c3b = Compactor(llm=FakeSummarizer(), cfg=LLMConfig(provider="mock", model="f",
                                                        timeout_s=10.0),
                    context_window=20_000)
    res3b = c3b.maybe_compact(msgs3b)
    head_texts = [m.content for m in msgs3b[:3]]
    ok &= check("注入背景资料时，user 任务书仍逐字保留",
                res3b.applied
                and "硬约束：不得引入第三方依赖，必须能离线跑" in head_texts
                and "[背景资料]" in "\n".join(head_texts),
                f"头部 {len(head_texts)} 条：{[h[:14] for h in head_texts]}")

    # ---------------- ⑤ 端到端：多轮长会话 ----------------
    print("\n" + "=" * 78)
    print("  ⑤ 端到端：长会话下开/关压缩的实际效果")
    print("=" * 78)

    class ScriptedLLM:
        """每轮读一个文件（产生大工具输出），制造真实的上下文增长。"""
        def __init__(self):
            self.turn = 0
            self.peak_in = 0
            self.total_in = 0

        def complete_with_tools(self, model, messages, tools, timeout_s):
            import json
            self.turn += 1
            # 记录本轮的输入规模
            n = count_messages(messages)
            self.peak_in = max(self.peak_in, n)
            if self.turn >= 18:
                return "收尾。", [{"id": "f", "type": "function",
                                  "function": {"name": "finish",
                                               "arguments": json.dumps(
                                                   {"summary": "完成"})}}], Usage(n, 20, 0)
            return "读文件。", [{"id": f"c{self.turn}", "type": "function",
                                "function": {"name": "read_file",
                                             "arguments": json.dumps(
                                                 {"path": "big.py", "max_lines": 400})}}], Usage(n, 30, 0)

    def run(with_compaction: bool) -> tuple[int, int]:
        ws = temporary_workspace()
        ws.reset()
        ws.write_file("big.py", "\n".join(f"line {i}: " + "z" * 60
                                          for i in range(2000)))
        llm = ScriptedLLM()
        agent = CodingAgent(
            llm=llm, cfg=LLMConfig(provider="mock", model="f", timeout_s=5),
            workspace=ws, compaction_enabled=with_compaction,
            context_window=12_000, spill_enabled=False, max_inline_bytes=10**9,
        )
        r = agent.run("反复读 big.py 直到我让你停")
        ws.reset()
        return llm.peak_in, sum(count_messages([]) for _ in [0]) or r.tool_calls

    peak_off, _ = run(False)
    peak_on, _ = run(True)
    print(f"  不压缩：峰值输入 {peak_off:,} tokens")
    print(f"  开压缩：峰值输入 {peak_on:,} tokens")
    ok &= check("压缩显著压住了峰值上下文", peak_on < peak_off * 0.7,
                f"{peak_off:,} → {peak_on:,}（-{1 - peak_on / peak_off:.0%}）")

    # ---------------- ⑥ 检查压缩记账 ----------------
    print("\n" + "=" * 78)
    print("  ⑥ 摘要花费必须计入账本（不能变成隐性成本）")
    print("=" * 78)
    ws2 = temporary_workspace()
    ws2.reset()
    ws2.write_file("big.py", "\n".join(f"l{i}: " + "z" * 60 for i in range(2000)))
    llm2 = ScriptedLLM()
    agent2 = CodingAgent(
        llm=llm2, cfg=cfg, workspace=ws2, compaction_enabled=True,
        context_window=8_000, spill_enabled=False, max_inline_bytes=10**9,
    )
    r2 = agent2.run("反复读 big.py")
    recs = agent2.compactor.history
    ok &= check("压缩发生过", len(recs) > 0, f"{len(recs)} 次")
    if recs:
        total_saved = sum(x.saved for x in recs)
        ok &= check("累计省下的 token 为正", total_saved > 0, f"{total_saved:,}")
    ws2.reset()

    print("\n" + "=" * 78)
    print("  结论：" + ("上下文压缩全部正确 ✅" if ok else "存在失败项 ❌"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
