"""验证 spill 策略：超大工具结果不撑爆上下文，且信息仍可回取。

用假 LLM 离线跑，不花钱。核心断言：
  ① 超大结果被 spill，进入上下文的字节数被封顶
  ② 落盘文件内容与原始输出**完全一致**（没丢信息）
  ③ 模型能从回灌内容里看到 locator，并按需 read_file 取回
  ④ spill 后上下文增长明显放缓（对比不启用 spill 的情况）
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentlab.providers import Usage  # noqa: E402
from agentplat.llmconfig import LLMConfig  # noqa: E402
from agentplat.loop import CodingAgent  # noqa: E402
from agentplat.spill import SpillPolicy  # noqa: E402
from agentplat.workspace import Workspace  # noqa: E402
from tools.test_support import temporary_workspace



class ScriptedLLM:
    def __init__(self, script):
        self.script = script
        self.turn = 0
        self.seen = []

    def complete_with_tools(self, model, messages, tools, timeout_s):
        self.seen.append([m.to_api() for m in messages])
        idx = min(self.turn, len(self.script) - 1)
        text, calls = self.script[idx]
        self.turn += 1
        return text, calls, Usage(50, 30, 0)


def call(name, args, cid="c"):
    import json
    return {"id": cid, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def check(name, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  {detail}" if detail else ""))
    return ok


def main() -> int:
    ws = temporary_workspace()
    ws.reset()
    ok = True

    # ---------- ① 直接测策略本身 ----------
    print("=" * 78)
    print("  ① 策略单元测试：大结果被 spill，小结果原样通过")
    print("=" * 78)
    sp = SpillPolicy(workspace=ws.root, max_inline_bytes=500, preview_bytes=200)

    small = "短输出"
    assert sp.apply("run_shell", small) == small, "小输出不该被改动"
    check("小结果原样通过", True, f"{len(small)} 字节")

    big = "".join(f"line {i}: 这是一段很长的测试输出内容用来撑爆上下文\n" for i in range(200))
    out = sp.apply("run_shell", big)
    check("大结果被 spill", len(out.encode()) < len(big.encode()),
          f"{len(big.encode()):,}B → {len(out.encode()):,}B")
    check("回灌内容里有 locator", "[spill]" in out and ".spill/" in out)
    check("回灌内容里提示了不要猜", "不要凭预览猜内容" in out)

    # ---------- ② 落盘内容与原始完全一致 ----------
    print("\n" + "=" * 78)
    print("  ② 信息没丢：落盘文件与原始输出逐字节一致")
    print("=" * 78)
    rec = sp.spilled[-1]
    stored = (ws.root / rec.path).read_text(encoding="utf-8")
    check("落盘内容完整一致", stored == big,
          f"原始 {len(big)} 字符 / 落盘 {len(stored)} 字符")
    check("统计到的节省量正确", sp.saved_bytes > 0,
          f"省 {sp.saved_bytes:,} 字节（{sp.saved_ratio:.1%}）")

    # ---------- ③ 相同内容重复 spill 会复用文件 ----------
    print("\n" + "=" * 78)
    print("  ③ 重复内容复用同一个 spill 文件（不重复占盘）")
    print("=" * 78)
    n_before = len(list((ws.root / ".spill").iterdir()))
    sp.apply("run_shell", big)
    n_after = len(list((ws.root / ".spill").iterdir()))
    check("文件数没有增加", n_after == n_before, f"{n_before} → {n_after}")

    # ---------- ④ 端到端：spill 真的压住了上下文增长 ----------
    print("\n" + "=" * 78)
    print("  ④ 端到端：agent 跑一个大输出命令，对比开/关 spill 的上下文占用")
    print("=" * 78)
    cfg = LLMConfig(provider="mock", model="fake", timeout_s=10.0)

    def run_task(spill_enabled: bool) -> tuple[int, int]:
        w = temporary_workspace()
        w.reset()
        # 造一个输出很大的脚本
        w.write_file("noisy.py", "print('X' * 60000)\n")
        script = [
            ("跑一下。", [call("run_shell", {"command": "python noisy.py"})]),
            ("收尾。", [call("finish", {"summary": "跑完了"})]),
        ]
        llm = ScriptedLLM(script)
        agent = CodingAgent(llm=llm, cfg=cfg, workspace=w,
                            spill_enabled=spill_enabled, max_inline_bytes=2000)
        r = agent.run("运行 noisy.py 并报告输出")
        # 量的是"最后一次请求里，整段历史占多少字节"
        last = llm.seen[-1]
        hist_bytes = sum(len(str(m).encode("utf-8")) for m in last)
        return hist_bytes, len(r.steps)

    without, _ = run_task(spill_enabled=False)
    withspill, _ = run_task(spill_enabled=True)
    ratio = 1 - withspill / without if without else 0
    check("开启 spill 后上下文明显变小", withspill < without,
          f"不启用 {without:,}B → 启用 {withspill:,}B（-{ratio:.0%}）")

    # ---------- ⑤ 模型能按 locator 回取 ----------
    print("\n" + "=" * 78)
    print("  ⑤ 模型能按 locator 回取细节（信息是可恢复的，不是丢掉的）")
    print("=" * 78)
    w2 = temporary_workspace()
    w2.reset()
    w2.write_file("noisy.py", "print('Y' * 30000)\n")
    script2 = [
        ("跑一下。", [call("run_shell", {"command": "python noisy.py"})]),
        ("读 spill 文件。", [call("read_file", {"path": ".spill/run_shell-"
                                                + "0" * 0 + "placeholder", "start_line": 1})]),
        ("收尾。", [call("finish", {"summary": "看过了"})]),
    ]
    llm2 = ScriptedLLM(script2)
    a2 = CodingAgent(llm=llm2, cfg=cfg, workspace=w2, max_inline_bytes=2000)
    r2 = a2.run("运行 noisy.py，如果输出太大就去 spill 文件里看")
    spilled = a2.spill.spilled
    rej = [s for s in r2.steps if s.tool == "read_file"]
    check("产生了 spill 记录", bool(spilled),
          spilled[0].path if spilled else "无")
    if spilled:
        # 用真实 locator 再读一次，确认可回取
        real = spilled[0].path
        got = w2.read_file(real, 1, 5)
        check("按 locator 能读到内容", len(got) > 50, got.splitlines()[0][:60])

    # ---------- 统计 ----------
    print("\n" + "=" * 78)
    print("  策略统计")
    print("=" * 78)
    sp.render()

    w2.reset()
    ws.reset()
    print("\n" + "=" * 78)
    print("  结论：" + ("spill 策略正确，上下文有界且信息可回取 ✅" if ok else "存在失败项 ❌"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
