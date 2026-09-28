"""验证会话持久化与断点续跑。

覆盖：
  ① 事件日志只追加、可加载、可重放
  ② **重放是纯只读的** —— 不重复执行副作用（这是最关键的一条）
  ③ 三个检查点屏障真的被触发
  ④ 持久化失败 → **阻止副作用**（不是先干了再说）
  ⑤ 崩溃后 resume：把"已经做过什么"告诉模型，不重跑已完成的工作
  ⑥ 已完成的会话 resume 会直接返回，不重复花钱
  ⑦ 日志损坏容忍（崩溃时的半行写入）
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentlab.providers import Usage  # noqa: E402
from agentplat.llmconfig import LLMConfig  # noqa: E402
from agentplat.loop import CodingAgent  # noqa: E402
from agentplat.session import (  # noqa: E402
    CheckpointError,
    SessionLog,
    find_latest_session,
    replay,
)
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
        fresh = [{**c, 'id': f't{self.turn}c{i}'} for i, c in enumerate(calls)]
        return text, fresh, Usage(40, 20, 0)


def call(name, args, cid="c"):
    return {"id": cid, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def check(name, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  {detail}" if detail else ""))
    return ok


def main() -> int:
    cfg = LLMConfig(provider="mock", model="fake", timeout_s=10.0)
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="agentlab-sess-"))

    # ---------------- ① 完整跑一次，日志落地 ----------------
    print("=" * 78)
    print("  ① 跑一个任务，检查事件日志")
    print("=" * 78)
    ws = temporary_workspace()
    ws.reset()
    sess = tmp / "run1.jsonl"
    llm = ScriptedLLM([
        ("看看目录。", [call("list_dir", {})]),
        ("写文件。", [call("write_file", {"path": "a.py", "content": "x = 1\n"})]),
        ("跑一下。", [call("run_shell", {"command": "python -c \"print(1)\""})]),
        ("完成。", [call("finish", {"summary": "写好并跑通了 a.py"})]),
    ])
    agent = CodingAgent(llm=llm, cfg=cfg, workspace=ws, session_dir=tmp,
                        session_id="run1")
    r = agent.run("创建 a.py 并运行")
    ok &= check("任务成功", r.ok, f"stopped_by={r.stopped_by}")
    ok &= check("日志文件已生成", sess.exists(),
                f"{sess.stat().st_size if sess.exists() else 0} 字节")

    log, skipped = SessionLog.load(sess)
    st = replay(log, skipped)
    kinds = log.summary()["by_kind"]
    print(f"  事件类型统计: {kinds}")
    ok &= check("记录了 session/created", kinds.get("session/created", 0) >= 1)
    ok &= check("记录了 step/start", kinds.get("step/start", 0) >= 1)
    ok &= check("记录了工具调用与结果",
                kinds.get("tool/call", 0) >= 2 and kinds.get("tool/result", 0) >= 2)
    ok &= check("记录了会话完成", kinds.get("session/closed", 0) == 1)
    ok &= check("重放出 session_id", st.session_id == "run1", st.session_id)
    ok &= check("重放出已改文件", "a.py" in st.files_written, str(st.files_written))
    ok &= check("重放出已跑命令", len(st.commands_run) >= 1, str(st.commands_run[:1]))

    # ---------------- ② 重放是纯只读的 ----------------
    print("\n" + "=" * 78)
    print("  ② 重放不得重复执行副作用（最关键的一条）")
    print("=" * 78)
    before = (ws.root / "a.py").read_text(encoding="utf-8")
    mtime_before = (ws.root / "a.py").stat().st_mtime_ns
    for _ in range(3):
        log2, sk2 = SessionLog.load(sess)
        replay(log2, sk2)
    after = (ws.root / "a.py").read_text(encoding="utf-8")
    mtime_after = (ws.root / "a.py").stat().st_mtime_ns
    ok &= check("文件内容未被改动", before == after)
    ok &= check("文件 mtime 未变（说明没被重写）", mtime_before == mtime_after)
    ok &= check("重放没有新增事件", len(log2.events) == len(log.events),
                f"{len(log2.events)} vs {len(log.events)}")

    # ---------------- ③ 三个屏障都被触发 ----------------
    print("\n" + "=" * 78)
    print("  ③ 三个检查点屏障")
    print("=" * 78)
    barriers = [e.data.get("reason", "") for e in log.of_kind("checkpoint/barrier")]
    ok &= check("屏障①模型请求前", any("before_model_request" in b for b in barriers))
    ok &= check("屏障②副作用执行前",
                any("before_side_effect" in b for b in barriers),
                str([b for b in barriers if "side_effect" in b][:2]))
    # 屏障③在"循环收尾"处。注意它有**两种形态**，取决于循环怎么结束：
    #   · 正常完成（模型调 finish）→ before_session_close / session_finished
    #   · 非正常退出（预算/策略/异常）→ loop_exit
    # 断言要认这两种，而不是只认一个 —— 我第一版就写死了 loop_exit 而误判失败。
    ok &= check("屏障③循环收尾（两种形态任一）",
                any(("before_session_close" in b or "session_finished" in b
                     or "loop_exit" in b) for b in barriers),
                str(barriers))
    print(f"  共 {len(barriers)} 次屏障: {barriers}")

    # ---------------- ④ 持久化失败 → 阻止副作用 ----------------
    print("\n" + "=" * 78)
    print("  ④ 持久化失败必须阻止副作用（不能先干了再说）")
    print("=" * 78)
    ws2 = temporary_workspace()
    ws2.reset()
    sess2 = tmp / "run2.jsonl"
    llm2 = ScriptedLLM([
        ("写文件。", [call("write_file", {"path": "should_not_exist.py",
                                          "content": "bad\n"})]),
        ("完成。", [call("finish", {"summary": "完成"})]),
    ])
    agent2 = CodingAgent(llm=llm2, cfg=cfg, workspace=ws2, session_dir=tmp,
                         session_id="run2")

    # 让 flush 抛错，模拟磁盘满/权限不足
    orig_flush = agent2.session.flush

    def failing_flush(reason=""):
        raise CheckpointError("模拟：磁盘不可写")

    agent2.session.flush = failing_flush
    r2 = agent2.run("写一个文件")
    ok &= check("检查点失败终止了循环", r2.stopped_by == "checkpoint_failed",
                f"stopped_by={r2.stopped_by}")
    ok &= check("副作用被阻止（文件没被创建）",
                not (ws2.root / "should_not_exist.py").exists())

    # ---------------- ⑤ resume 不重跑已完成的工作 ----------------
    print("\n" + "=" * 78)
    print("  ⑤ 崩溃后续跑：把「已做过什么」告诉模型")
    print("=" * 78)
    ws3 = temporary_workspace()
    ws3.reset()
    sess3 = tmp / "run3.jsonl"
    # 先造一个"跑到一半"的日志
    half = SessionLog(sess3, session_id="run3")
    half.append("session/created", session_id="run3", task="创建 b.py 和 c.py")
    half.flush("before_model_request")
    half.append("step/start", iteration=1)
    half.append("assistant/message", in_tokens=500, out_tokens=100, usd=0.0012)
    half.append("tool/call", tool="write_file", destructive=True,
                path="b.py", ok=True)
    half.append("tool/result", tool="write_file", ok=True, out="已创建 b.py")
    half.append("step/end", iteration=1, stopped_by="crash")
    # b.py 确实存在于磁盘
    ws3.write_file("b.py", "y = 2\n")

    llm3 = ScriptedLLM([("继续。", [call("finish", {"summary": "接着做完了 c.py"})])])
    agent3 = CodingAgent(llm=llm3, cfg=cfg, workspace=ws3, session_dir=tmp)
    r3 = agent3.resume(sess3)
    ok &= check("续跑成功", r3.ok, f"stopped_by={r3.stopped_by}")
    ok &= check("stopped_by 标记为 resumed", r3.stopped_by.startswith("resumed:"),
                r3.stopped_by)
    ctx = str(llm3.seen[0]) if llm3.seen else ""
    ok &= check("提示了这是续跑", "续跑" in ctx)
    ok &= check("告知了已改动的文件", "b.py" in ctx)
    ok &= check("要求先确认现状再动手", "确认现状" in ctx)

    # ---------------- ⑥ 已完成的会话不重复执行 ----------------
    print("\n" + "=" * 78)
    print("  ⑥ 已完成的会话 resume → 直接返回，不重复花钱")
    print("=" * 78)
    llm4 = ScriptedLLM([("不该被调用。", [call("finish", {"summary": "x"})])])
    agent4 = CodingAgent(llm=llm4, cfg=cfg, workspace=ws, session_dir=tmp)
    r4 = agent4.resume(sess)   # sess 是 ① 里已完成的
    ok &= check("识别出已完成", r4.stopped_by == "already_finished", r4.stopped_by)
    ok &= check("没有发起任何模型调用", llm4.turn == 0, f"turn={llm4.turn}")

    # ---------------- ⑦ 日志损坏容忍 ----------------
    print("\n" + "=" * 78)
    print("  ⑦ 崩溃导致日志半行损坏 → 跳过坏行，其余可用")
    print("=" * 78)
    corrupt = tmp / "corrupt.jsonl"
    good = SessionLog(corrupt, session_id="cx")
    good.append("session/created", session_id="cx", task="任务X")
    good.append("step/start", iteration=1)
    with open(corrupt, "a", encoding="utf-8") as f:
        f.write('{"seq": 3, "kind": "step/sta')  # 半行（崩溃现场）
    # 进程重启后继续往同一个文件追加 —— **必须用 open()**。
    # 用 SessionLog(path) 是"新建"，seq 会从 1 重来、与已有事件冲突（实测踩到）。
    good2, _ = SessionLog.open(corrupt, fsync=False)
    # 顺序：**先 append 再 flush**（flush 自己会追加一条 barrier 事件）。
    good2.append("step/end", iteration=1, stopped_by="x")
    good2.flush("test")
    log5, sk5 = SessionLog.load(corrupt)
    st5 = replay(log5, sk5)
    ok &= check("坏行被跳过而不是整份作废", sk5 >= 1, f"跳过 {sk5} 行")
    ok &= check("好行仍可重放", st5.session_id == "cx", st5.session_id)
    ok &= check("续写的事件确实落到了文件里",
                any(e.kind == "step/end" for e in log5.events),
                f"{len(log5.events)} 条, kinds={log5.summary()['by_kind']}")
    seqs = [e.seq for e in log5.events]
    ok &= check("seq 严格递增无冲突", seqs == sorted(set(seqs)), f"seqs={seqs}")

    ok &= check("能找到最近的会话日志", find_latest_session(tmp) is not None)

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    ws.reset(); ws2.reset(); ws3.reset()

    print("\n" + "=" * 78)
    print("  结论：" + ("会话持久化与断点续跑全部正确 ✅" if ok else "存在失败项 ❌"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
