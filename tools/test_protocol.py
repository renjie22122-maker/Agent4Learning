"""验证"发出去之前的结构校验"：三类会被 provider 直接 400 的问题必须被拦住。

实测报错原文（真实 DeepSeek 端点）：

    400 Duplicate value for 'tool_call_id' of call_00_ET_0IDpK25YalDFjeUi6RWA9637
        in message[4]

这个错误的危险之处在于**本地任何一层都看不出来**：工具执行成功、
会话日志配对正确、不变量也没报（日志本身没错）—— 错的是**发出去的那个数组**。
所以必须有这一层，而且必须测到"它真的会拦"。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentlab.providers import ChatMessage  # noqa: E402
from agentplat.llm import sanitize_messages  # noqa: E402


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  {detail}" if detail else ""))
    return ok


def asst(*ids):
    return ChatMessage("assistant", "", tool_calls=[
        {"id": i, "type": "function",
         "function": {"name": "read_file", "arguments": "{}"}} for i in ids])


def tool(tid, out="ok"):
    return ChatMessage("tool", out, tool_call_id=tid)


def main() -> int:  # noqa: C901
    ok = True

    print("=" * 78)
    print("  ① 复现那一份真实报错：重复的 tool_call_id")
    print("=" * 78)
    # 结构对应 message[0..4]：system / user / assistant(2 个调用) / 结果 / 结果
    msgs = [ChatMessage("system", "你是编码 agent"),
            ChatMessage("user", "写一个俄罗斯方块"),
            asst("call_00_ET_0IDpK25YalDFjeUi6RWA9637",
                 "call_01_ET_SEgum6AIEcMDSJZlG6rt1288"),
            tool("call_00_ET_0IDpK25YalDFjeUi6RWA9637"),
            # ← 这一条是坏的：又给了同一个 id
            tool("call_00_ET_0IDpK25YalDFjeUi6RWA9637", out="重复的")]
    fixed, notes = sanitize_messages(msgs)
    ids = [m.tool_call_id for m in fixed if m.role == "tool"]
    ok &= check("重复的 tool 结果被丢掉", len(ids) == len(set(ids)), str(ids))
    ok &= check("被丢掉的是**后**一条（先到的才是真结果）",
                [m.content for m in fixed if m.role == "tool"
                 and m.tool_call_id == "call_00_ET_0IDpK25YalDFjeUi6RWA9637"]
                == ["ok"])
    ok &= check("缺失的第二个结果被补齐（否则 provider 也会拒）",
                "call_01_ET_SEgum6AIEcMDSJZlG6rt1288" in ids, str(ids))
    ok &= check("问题被如实报出来（不能静默修）", len(notes) == 2, str(notes))

    print("\n" + "=" * 78)
    print("  ② 孤儿 tool 结果（找不到对应调用）")
    print("=" * 78)
    fixed, notes = sanitize_messages(
        [ChatMessage("system", "s"), asst("c1"), tool("c1"), tool("幽灵")])
    ids = [m.tool_call_id for m in fixed if m.role == "tool"]
    ok &= check("孤儿结果被丢掉", "幽灵" not in ids, str(ids))
    ok &= check("报出了原因", any("孤儿" in n for n in notes), str(notes))

    print("\n" + "=" * 78)
    print("  ③ 声明了调用但一个结果都没有")
    print("=" * 78)
    fixed, notes = sanitize_messages(
        [ChatMessage("system", "s"), asst("c1", "c2"), tool("c1")])
    ids = sorted(m.tool_call_id for m in fixed if m.role == "tool")
    ok &= check("缺的结果被补齐", ids == ["c1", "c2"], str(ids))
    ok &= check("补齐的是**占位**而不是空内容",
                any("结果缺失" in (m.content or "") for m in fixed
                    if m.tool_call_id == "c2"))
    ok &= check("报出了原因", any("没有结果" in n for n in notes), str(notes))

    print("\n" + "=" * 78)
    print("  ④ 合法序列必须**原样通过**（否则每次调用都在悄悄改历史）")
    print("=" * 78)
    good = [ChatMessage("system", "s"), ChatMessage("user", "u"),
            asst("c1"), tool("c1"), asst("c2"), tool("c2"),
            ChatMessage("assistant", "完成")]
    fixed, notes = sanitize_messages(good)
    ok &= check("没有改动", len(fixed) == len(good) and not notes,
                f"{len(fixed)} vs {len(good)}, notes={notes}")
    ok &= check("顺序与内容都没变",
                [m.role for m in fixed] == [m.role for m in good])
    ok &= check("tool_call_id 全部保留",
                [m.tool_call_id for m in fixed if m.role == "tool"] == ["c1", "c2"])

    print("\n" + "=" * 78)
    print("  ⑤ 真的接到了发出去的 payload 上吗")
    print("=" * 78)
    import json  # noqa: E402

    from agentplat.llmconfig import LLMConfig  # noqa: E402
    from agentplat.llm import OpenAIChatClient  # noqa: E402

    cfg = LLMConfig(provider="real", model="m", api_key="k",
                    base_url="https://example.invalid/v1", timeout_s=5.0)
    client = OpenAIChatClient(cfg)
    # 用**故意坏掉**的消息序列构造 payload，确认坏 id 没被发出去
    body = json.loads(client._payload("m", msgs).decode("utf-8"))  # noqa: SLF001
    wire_ids = [m.get("tool_call_id") for m in body["messages"]
                if m.get("role") == "tool"]
    ok &= check("payload 里的 tool_call_id 无重复",
                len(wire_ids) == len(set(wire_ids)), str(wire_ids))
    ok &= check("payload 里每个声明的调用都有结果",
                set(wire_ids) == {"call_00_ET_0IDpK25YalDFjeUi6RWA9637",
                                  "call_01_ET_SEgum6AIEcMDSJZlG6rt1288"},
                str(wire_ids))
    from agentplat.llm import PROTOCOL_REPAIRS  # noqa: E402
    ok &= check("修复被计数（页面可以据此把'曾经坏过'讲清楚）",
                sum(PROTOCOL_REPAIRS.values()) >= 2, str(PROTOCOL_REPAIRS))

    print("\n" + "=" * 78)
    print("  ⑥ 就地清理：坏消息不能一直留在历史里")
    print("=" * 78)
    from agentplat.llm import sanitize_in_place  # noqa: E402

    live = [ChatMessage("system", "s"), asst("c1"),
            tool("c1"), tool("c1", out="重复的")]
    first = sanitize_in_place(live)
    second = sanitize_in_place(live)
    ok &= check("第一次清理报了问题", len(first) == 1, str(first))
    ok &= check("**清理后的列表本身**已经合法（重复项被移除）",
                len([m for m in live if m.role == "tool"]) == 1,
                f"{len(live)} 条消息")
    ok &= check("第二次清理无事可做（不是每轮都在改历史）",
                second == [], str(second))

    print("\n" + "=" * 78)
    print(f"  {'结论：协议校验正确，三类 400 都能被拦住 ✅' if ok else '结论：存在失败项 ❌'}")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
