"""诊断：为什么反复撞 max_tokens（单轮输出到底有多大、花在哪）。

只跑几轮就停，重点看**每轮的输出 token 数与 finish_reason**。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentlab.providers import ChatMessage, Usage  # noqa: E402
from agentplat.agent_tools import build_agent_tools, schemas  # noqa: E402
from agentplat.llm import OpenAIChatClient  # noqa: E402
from agentplat.llmconfig import LLMConfig  # noqa: E402
from agentplat.loop import CODING_SYSTEM  # noqa: E402
from agentplat.workspace import Workspace  # noqa: E402


def main() -> int:
    cfg = LLMConfig.load()
    if not cfg.is_real:
        print("需要真实 LLM")
        return 2
    print(f"模型 {cfg.model_or('mid')}  max_tokens={cfg.max_tokens}  "
          f"temperature={cfg.temperature}")
    print(f"reasoning_effort={cfg.reasoning_effort!r}  json_mode={cfg.json_mode}")
    print()

    ws = Workspace()
    ws.reset()
    llm = OpenAIChatClient(cfg)
    tools = schemas(build_agent_tools(ws))

    messages = [
        ChatMessage("system", CODING_SYSTEM),
        ChatMessage("user", "写一个快速排序 quicksort.py，写测试并跑通 pytest"),
    ]

    print(f"{'轮':>3}{'in':>9}{'out':>9}{'finish_reason':>16}  工具调用")
    print("-" * 62)
    for turn in range(1, 7):
        try:
            text, calls, usage = llm.complete_with_tools(
                cfg.model_or("mid"), messages, tools, cfg.timeout_s
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  {turn}  调用失败: {type(exc).__name__}: {str(exc)[:80]}")
            break
        names = ",".join((c.get("function") or {}).get("name", "?") for c in calls)
        print(f"{turn:>3}{usage.in_tokens:>9,}{usage.out_tokens:>9,}"
              f"{llm.last_finish_reason:>16}  {names or '(无)'}")

        # 看这次输出到底多长（用于判断是"话多"还是"内容大"）
        detail = f"    文本 {len(text)} 字符"
        if calls:
            arglen = sum(len((c.get("function") or {}).get("arguments", ""))
                         for c in calls)
            detail += f"，工具参数共 {arglen:,} 字符"
        print(detail)

        if calls:
            # 只把一个工具结果回灌（不真执行写文件，避免污染工作区），
            # 用一个短回执代替，观察上下文增长对下一轮输出的影响。
            for i, c in enumerate(calls):
                messages.append(ChatMessage("assistant", text or "", tool_calls=calls))
                messages.append(ChatMessage(
                    "tool", "（诊断模式：未真正执行）", tool_call_id=c.get("id") or f"c{i}"))
                break
        else:
            messages.append(ChatMessage("assistant", text or ""))
            messages.append(ChatMessage("user", "请调用工具推进，或调用 finish 结束。"))

        if turn >= 3 and usage.out_tokens < cfg.max_tokens * 0.5:
            print("\n  输出已明显变小，停止诊断。")
            break

    ws.reset()
    print()
    print("判读：")
    print("  · 若 out ≈ max_tokens 且 finish_reason=length → 输出被上限截断，需要更大额度")
    print("  · 若 out 很小但 finish_reason=length → 上限设置或服务端行为异常")
    print("  · 若每轮 in 快速增长 → 上下文在累积，需要 spill/compaction")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
