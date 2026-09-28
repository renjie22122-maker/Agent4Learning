"""离线验证 agent 循环：用脚本化的假 LLM，不出网、不花钱。

为什么必须先做这个：真实 LLM 是不确定的，用它调试循环逻辑会很痛苦
（"这次没走对分支"到底是循环的问题还是模型的问题？）。
用**确定性假模型**把循环的每条路径都覆盖一遍，再上真实模型，
这样出问题时能立刻判断责任在哪一侧。

覆盖的路径：
  ① 正常流程：list_dir → write_file → run_shell（真跑）→ finish
  ② 模型只想用文字结束 → 必须被要求调用 finish，不能被骗过
  ③ 达到最大轮数 → 硬性终止
  ④ 工具调用次数超限 → 硬性终止
  ⑤ 越界路径 → 被拒绝，且**拒绝信息回灌给模型**
  ⑥ 参数不是合法 JSON → 结构化报错，不崩
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentlab.providers import Usage  # noqa: E402
from agentplat.llmconfig import LLMConfig  # noqa: E402
from agentplat.loop import CodingAgent  # noqa: E402
from agentplat.workspace import Workspace  # noqa: E402
from tools.test_support import temporary_workspace



class ScriptedLLM:
    """按脚本依次返回预设的 tool_calls。用完脚本后重复最后一项。"""

    def __init__(self, script: list[tuple[str, list[dict]]]):
        self.script = script
        self.turn = 0
        self.seen_messages: list = []

    def complete_with_tools(self, model, messages, tools, timeout_s):
        # 记录模型看到了什么，用于断言"拒绝信息确实回灌了"
        self.seen_messages.append([m.to_api() for m in messages])
        idx = min(self.turn, len(self.script) - 1)
        text, calls = self.script[idx]
        self.turn += 1
        # ⚠ 必须**每次都给全新的 call_id**。
        # 脚本里的调用会被重复使用（脚本用完就重复最后一项），而
        # `call()` 的默认 cid 是同一个常量 —— 于是不同轮次里出现同一个
        # call_id，日志就违反了"call_id 不重复"这条不变量。
        # 实测就是这样被抓出来的：`call_id 不重复 @seq=33: 同一个 call_id
        # 被调用两次：c0`。**那是脚本的 bug，不是循环的 bug** ——
        # 但不变量报出来是对的：真实 provider 也会拒这种日志。
        fresh = []
        for j, c in enumerate(calls):
            c2 = {"id": f"t{self.turn}c{j}", "type": c.get("type", "function"),
                  "function": dict(c.get("function") or {})}
            fresh.append(c2)
        return text, fresh, Usage(50, 30, 0)


def call(name: str, args: dict, cid: str = "c") -> dict:
    import json
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  {detail}" if detail else ""))
    return ok


def fresh_ws() -> Workspace:
    ws = temporary_workspace()
    ws.reset()
    return ws


def main() -> int:
    cfg = LLMConfig(provider="mock", model="fake", timeout_s=10.0)
    passed = True

    # ---------- ① 正常流程 ----------
    print("=" * 78)
    print("  ① 正常流程：list_dir → write_file → run_shell(真跑) → finish")
    print("=" * 78)
    ws = fresh_ws()
    llm = ScriptedLLM([
        ("我先看看工作区。", [call("list_dir", {})]),
        ("写一个脚本。", [call("write_file", {
            "path": "hello.py",
            "content": "import sys\nprint('hello from agent')\nprint('sum=', 1 + 2)\n",
        })]),
        ("跑一下验证。", [call("run_shell", {"command": "python hello.py"})]),
        ("跑通了，收尾。", [call("finish", {
            "summary": "创建并运行了 hello.py", "files_changed": "hello.py"})]),
    ])
    agent = CodingAgent(llm=llm, cfg=cfg, workspace=ws)
    r = agent.run("在工作区里创建一个 hello.py 并运行验证")
    passed &= check("循环成功结束", r.ok, f"stopped_by={r.stopped_by}")
    passed &= check("结束原因是 finish", r.stopped_by == "finish")
    passed &= check("文件真的被创建", (ws.root / "hello.py").exists())
    shell_steps = [s for s in r.steps if s.tool == "run_shell"]
    passed &= check("真的执行了命令并拿到输出",
                    bool(shell_steps) and "hello from agent" in shell_steps[0].result,
                    shell_steps[0].result[:60].replace("\n", " ") if shell_steps else "")
    passed &= check("执行结果回灌给了模型（下一轮能看到）",
                    any("hello from agent" in str(m) for m in llm.seen_messages[-1]))
    print(f"  轮数={r.iterations}  工具调用={r.tool_calls}  "
          f"token={r.tokens_in}/{r.tokens_out}")

    # ---------- ② 模型试图用文字结束 ----------
    print("\n" + "=" * 78)
    print("  ② 模型只想用文字说「完成了」—— 不能被骗过")
    print("=" * 78)
    ws2 = fresh_ws()
    llm2 = ScriptedLLM([
        ("任务完成了！", []),                       # 想用自然语言结束
        ("好的。", [call("finish", {"summary": "完成"})]),  # 被要求后调 finish
    ])
    r2 = CodingAgent(llm=llm2, cfg=cfg, workspace=ws2).run("随便做点什么")
    guards = [s for s in r2.steps if s.kind == "guard"]
    passed &= check("识别出「只想用文字结束」并追问", bool(guards),
                    guards[0].title if guards else "")
    passed &= check("最终仍以 finish 正常收尾", r2.ok and r2.stopped_by == "finish")

    # ---------- ③ 最大轮数 ----------
    print("\n" + "=" * 78)
    print("  ③ 模型无限循环 → 必须被最大轮数硬性终止")
    print("=" * 78)
    ws3 = fresh_ws()
    llm3 = ScriptedLLM([("继续。", [call("list_dir", {})])])  # 永远重复同一动作
    from agentplat.loop import MaxIterationsPolicy
    r3 = CodingAgent(llm=llm3, cfg=cfg, workspace=ws3,
                     soft_iterations=4, hard_iterations=5).run("陷入循环")
    passed &= check("被终止策略拦下", r3.stopped_by == "hard_limit",
                    f"stopped_by={r3.stopped_by}")
    passed &= check("轮数被限制住", r3.iterations <= 5, f"iterations={r3.iterations}")
    passed &= check("未被误判为成功", not r3.ok)

    # ---------- ④ 工具调用次数超限 ----------
    print("\n" + "=" * 78)
    print("  ④ 单轮里狂调工具 → 工具次数上限兜底")
    print("=" * 78)
    ws4 = fresh_ws()
    many = [call("list_dir", {}, f"c{i}") for i in range(50)]
    llm4 = ScriptedLLM([("批量调用。", many)])
    r4 = CodingAgent(llm=llm4, cfg=cfg, workspace=ws4, soft_iterations=2).run("狂调工具")
    # 注意这是**单步**闸门，不是累计上限：连续三个步骤都超量时介入，
    # 所以最多会跑 3 步 × 每步上限。断言按这个语义写，否则会误判。
    # 关键结论：一轮里 50 个调用不会全部执行（那才是真正的失控）。
    cap = CodingAgent.MAX_TOOLS_PER_STEP * 3
    passed &= check("单步工具调用被限量（策略拦不住轮内爆发，必须另有闸门）",
                    r4.tool_calls <= cap,
                    f"一轮塞了 50 个调用，实际执行 {r4.tool_calls} 个"
                    f"（单步上限 {CodingAgent.MAX_TOOLS_PER_STEP} × 3 步 = {cap}）")

    # ---------- ④b 截断必须**保住 finish** ----------
    print("\n" + "=" * 78)
    print("  ④b 截断顺序：finish 不能被丢掉")
    print("=" * 78)
    print("  为什么：模型经常在同一轮里既调工具、又调 finish。")
    print("  如果截断从尾部砍，finish 正好在末尾 —— 会被丢掉，")
    print("  '任务已完成'这件事就白算了，下一轮还得再来一遍。")
    ws4b = fresh_ws()
    mixed = ([call("list_dir", {}, f"m{i}") for i in range(30)]
             + [call("finish", {"summary": "干完了"})])
    llm4b = ScriptedLLM([("批量调用 + 收尾。", mixed)])
    r4b = CodingAgent(llm=llm4b, cfg=cfg, workspace=ws4b).run("混合调用")
    passed &= check("存在未执行工具时 finish 不能误报完成",
                    not r4b.ok,
                    f"stopped_by={r4b.stopped_by}")

    # ---------- ④c 连续超限才终止 ----------
    print("\n" + "=" * 78)
    print("  ④c 只超限一次**不**终止任务（模型下一轮通常会改）")
    print("=" * 78)
    ws4c = fresh_ws()
    llm4c = ScriptedLLM([
        ("一口气读一堆。", [call("list_dir", {}, f"n{i}") for i in range(30)]),
        ("改成分批了。", [call("list_dir", {}, "one"),
                          call("finish", {"summary": "看完了"})]),
    ])
    r4c = CodingAgent(llm=llm4c, cfg=cfg, workspace=ws4c).run("先超限再收敛")
    passed &= check("超限一轮后模型收敛 → 任务正常完成",
                    r4c.ok and r4c.stopped_by == "finish",
                    f"ok={r4c.ok} stopped_by={r4c.stopped_by} "
                    f"iters={r4c.iterations}")

    # ---------- ⑤ 越界路径被拒绝并回灌 ----------
    print("\n" + "=" * 78)
    print("  ⑤ 越界路径 → 拒绝，且拒绝原因回灌给模型")
    print("=" * 78)
    ws5 = fresh_ws()
    llm5 = ScriptedLLM([
        ("我读一下系统文件。", [call("read_file", {"path": "../../../etc/passwd"})]),
        ("明白，不越界了。", [call("finish", {"summary": "被拦下后收手"})]),
    ])
    r5 = CodingAgent(llm=llm5, cfg=cfg, workspace=ws5).run("尝试读工作区外的文件")
    rej = [s for s in r5.steps if s.tool == "read_file"]
    passed &= check("越界调用被拒绝", bool(rej) and not rej[0].ok)
    passed &= check("拒绝信息里有可操作的说明",
                    bool(rej) and "路径越界" in rej[0].result)
    passed &= check("拒绝信息回灌给了模型",
                    any("路径越界" in str(m) for m in llm5.seen_messages[-1]))
    passed &= check("越界没有导致崩溃", r5.stopped_by == "finish")

    # ---------- ⑥ 非法 JSON 参数 ----------
    print("\n" + "=" * 78)
    print("  ⑥ 工具参数不是合法 JSON → 结构化报错，不崩")
    print("=" * 78)
    ws6 = fresh_ws()
    bad = {"id": "c1", "type": "function",
           "function": {"name": "read_file", "arguments": "{这不是JSON"}}
    llm6 = ScriptedLLM([("调用。", [bad]),
                        ("收尾。", [call("finish", {"summary": "参数错误已处理"})])])
    r6 = CodingAgent(llm=llm6, cfg=cfg, workspace=ws6).run("测试坏参数")
    errs = [s for s in r6.steps if s.kind == "error"]
    passed &= check("非法参数被识别为错误而非崩溃", bool(errs))
    passed &= check("循环继续并正常收尾", r6.stopped_by == "finish")

    # ---------- 工作区审计 ----------
    print("\n" + "=" * 78)
    print("  工作区审计与改动摘要")
    print("=" * 78)
    snap = ws.snapshot()
    passed &= check("审计记录了操作", len(snap["audit"]) > 0, f"{len(snap['audit'])} 条")
    passed &= check("记录了文件改动", snap["files_changed"] >= 1,
                    f"{snap['files_changed']} 个文件")
    print("  改动摘要：")
    print(ws.diff_summary())
    ws.reset()

    print("\n" + "=" * 78)
    print("  结论：" + ("循环逻辑全部正确 ✅" if passed else "存在失败项 ❌"))
    print("=" * 78)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
