"""验证多轮对话：同一个 agent 连续追问时，模型必须**看得见**前面做过什么。

这个测试要回答的问题：`continue_with()` 之后，第二次请求发给模型的
messages 里，是否真的带着第一轮的工具结果？

如果不带，表现是"每轮都从零开始"：模型重新 list_dir、重新读文件、
重复问同样的问题 —— 单轮看着还行，连着用完全没法用。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentlab.providers import ChatMessage, Usage  # noqa: E402
from agentplat.llmconfig import LLMConfig  # noqa: E402
from agentplat.loop import CodingAgent  # noqa: E402
from agentplat.workspace import Workspace  # noqa: E402


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  {detail}" if detail else ""))
    return ok


class ScriptedLLM:
    """按脚本回答，并**记下每次收到的 messages**（这才是被测对象）。"""

    def __init__(self):
        self.calls: list[list[ChatMessage]] = []
        self.last_finish_reason = "tool_calls"
        self.turn = 0
        self.rejected = False

    def complete_with_tools(self, model, messages, tools, timeout_s):
        self.calls.append([ChatMessage(m.role, m.content or "",
                                       getattr(m, "tool_calls", None),
                                       getattr(m, "tool_call_id", None))
                           for m in messages])
        blob = "\n".join((m.content or "") for m in messages)
        if "这次完成声明被拒绝" in blob:
            self.rejected = True
        self.turn += 1
        n = self.turn
        if n == 1:                       # 首轮：写个文件
            return "", [_tc("c1", "write_file",
                            {"path": "hello.py", "content": "print('hi')\n"})], _usage()
        if n == 2:
            # 写完直接收尾 —— **不验证**。反射闸门会拒它（这是对的：
            # 写了文件一次都没跑，没有证据）。脚本模型接着走正路。
            return "", [_tc("c2", "finish", {"summary": "已写入 hello.py"})], _usage()
        if self.rejected and n == 3:
            return "", [_tc("c3", "run_shell", {
                "command": "python -c \"import hello; print('OK')\""})], _usage()
        if self.rejected and n == 4:
            return "", [_tc("c4", "finish", {
                "summary": "已写入 hello.py 并跑了 python -c 自测："
                           "退出码 0，输出 OK。"})], _usage()
        # 追问：**第一轮就收尾**。让脚本 LLM 一直返回纯文本是错的 ——
        # `MaxIterationsPolicy` 会一路跑到软上限 41 轮，测试自己制造出
        # 一堆空转（每一轮都真的在"假装思考"），断言也就跟着失真了。
        self.last_finish_reason = "tool_calls"
        return "", [_tc(f"c{n}", "finish",
                        {"summary": f"追问已回答（第 {n - 2} 次追问）"})], _usage()


def _usage():
    return Usage(100, 20, 0)


def _tc(cid, name, args):
    import json
    return {"id": cid, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def main() -> int:
    ok = True

    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td))
        ws.execution_mode = 'local'  # Conversation test; native isolation has a separate suite.
        llm = ScriptedLLM()
        agent = CodingAgent(
            llm=llm, cfg=LLMConfig(provider="mock", model="m", timeout_s=5.0),
            workspace=ws, session_dir=Path(td) / ".sessions", guard=None,
            invariants=True,
        )

        print("=" * 78)
        print("  ① 首轮 run()")
        print("=" * 78)
        r1 = agent.run("写一个 hello.py")
        ok &= check("首轮完成", r1.ok, f"stopped_by={r1.stopped_by}")
        ok &= check("文件真的写出来了", (Path(td) / "hello.py").exists())
        ok &= check("对话被记住", len(agent._conversation) > 0,
                    f"{len(agent._conversation)} 条消息")
        n_after_run = len(llm.calls[-1])
        print(f"    首轮最后一次请求带了 {n_after_run} 条消息")

        print("\n" + "=" * 78)
        print("  ② continue_with()：追问必须带着上一轮的工具结果")
        print("=" * 78)
        r2 = agent.continue_with("你刚才写了什么文件？")
        # 追问用脚本 LLM 不返回 finish，所以会被 `MaxIterationsPolicy` 的
        # **软上限**停下来 —— 那是设计好的机制（机制与策略分离），不是失败。
        # 这里断言"停得有理由"，而不是断言 ok：ok 只代表模型声明了完成。
        ok &= check("追问停下且有明确理由",
                    r2.stopped_by in ("finish", "policy", "unverified"),
                    f"stopped_by={r2.stopped_by} iters={r2.iterations}")
        ok &= check("追问没有失控（轮次落在软硬上限之间）",
                    r2.iterations >= 1 and (not agent.HARD_ITERATIONS or r2.iterations <= agent.HARD_ITERATIONS),
                    f"{r2.iterations} 轮")
        sent = llm.calls[-1]
        roles = [m.role for m in sent]
        contents = "\n".join(m.content or "" for m in sent)
        ok &= check("第二次请求带上了上一轮的消息（不只是 system + 新提问）",
                    len(sent) > n_after_run,
                    f"{n_after_run} → {len(sent)} 条")
        ok &= check("上一轮的 tool 结果还在里面", "tool" in roles, str(roles[-6:]))
        ok &= check("上一轮的 assistant 消息还在里面", "assistant" in roles)
        # 用"**第一次**把追问发出去的请求"来验证注入形状。
        # 不用"最后一次请求"：追问之后模型还会继续跑（跑验证、再收尾），
        # 后续轮次里的 user 消息是循环自己加的提示，跟追问的形状无关。
        # 断言要挑对**观测点**，否则测的是别的东西。
        first_ask = next((c for c in llm.calls
                          if any(m.role == "user" and "刚才写了什么文件"
                                 in (m.content or "") for m in c)), None)
        roles_at_ask = ([m.role for m in first_ask] if first_ask else [])
        # 追问之后：只允许循环机制加的提示（也计为 user），但**不允许出现
        # 第二条内容相同的追问** —— 那才是"重复注入"这个 bug。
        dup = sum(1 for m in (first_ask or [])
                  if m.role == "user" and "刚才写了什么文件" in (m.content or ""))
        ok &= check("追问在第一次发出的请求里只出现一次（没有重复注入）",
                    dup == 1, f"出现 {dup} 次；roles={roles_at_ask[-6:]}")
        ok &= check("追问紧跟在已有历史之后（形状正确）",
                    bool(first_ask) and first_ask[-1].role == "user"
                    and "刚才写了什么文件" in (first_ask[-1].content or ""),
                    f"最后一条：{roles_at_ask[-1] if roles_at_ask else '无'}")
        ok &= check("上一轮的交付语义（summary）被写回了对话",
                    any(m.role == "assistant" and "已写入 hello.py"
                        in (m.content or "") for m in sent[:-1]),
                    "没找到「（本轮交付）…」这条 assistant 消息")
        ok &= check("模型能看到'已写入 hello.py'这个事实",
                    "hello.py" in contents,
                    f"长度 {len(contents)} 字符")
        ok &= check("工作区说明只在首轮出现（没有重复注入）",
                    contents.count("工作区根目录") == 1,
                    f"出现 {contents.count('工作区根目录')} 次")

        print("\n" + "=" * 78)
        print("  ③ 会话日志：续轮不写第二个 session/created")
        print("=" * 78)
        log = agent.session.events
        created = [e for e in log if e.kind == "session/created"]
        followups = [e for e in log if e.kind == "followup/user"]
        ok &= check("session/created 只有一条（日志锚点唯一）",
                    len(created) == 1, f"{len(created)} 条")
        ok &= check("追问被记进日志", len(followups) == 1, f"{len(followups)} 条")
        ok &= check("日志里能看出续轮（followup/user 事件）",
                    len(followups) == 1
                    and "刚才写了什么文件" in followups[0].data.get("text", ""))
        ok &= check("续轮之后轮次编号继续往上走（没有回退）",
                    [e.data["iteration"] for e in log if e.kind == "step/start"]
                    == sorted(set(e.data["iteration"] for e in log
                                  if e.kind == "step/start")),
                    str([e.data.get("iteration") for e in log
                         if e.kind == "step/start"]))

        print("\n" + "=" * 78)
        print("  ④ 不变量：整段多轮会话必须零违规")
        print("=" * 78)
        agent.invariants.reset()
        rep = agent.invariants.audit(log)
        ok &= check("多轮会话零违规", rep.ok,
                    "\n".join(v.render() for v in rep.violations)[:500])
        ok &= check("没有静默失效的钩子", not rep.silent_checks,
                    str(rep.silent_checks))
        print(f"    检查了 {rep.checked_events} 个事件")

        print("\n" + "=" * 78)
        print("  ⑤ 没有对话就问 → 必须明确报错，而不是静默开新会话")
        print("=" * 78)
        agent2 = CodingAgent(
            llm=llm, cfg=LLMConfig(provider="mock", model="m", timeout_s=5.0),
            workspace=ws, session_dir=Path(td) / ".sessions", guard=None,
        )
        try:
            agent2.continue_with("喂")
            ok &= check("空对话时追问被拒绝", False, "竟然接受了")
        except RuntimeError as exc:
            ok &= check("空对话时追问被拒绝", "还没有任何对话" in str(exc),
                        str(exc)[:60])

    print("\n" + "=" * 78)
    print(f"  {'结论：多轮对话上下文正确传递 ✅' if ok else '结论：存在失败项 ❌'}")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
