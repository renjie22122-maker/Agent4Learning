"""Command-line adapter, independent from execution internals."""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence
from agentlab.providers import ChatMessage
from agentlab.tracing import Tracer
from .agent_tools import AgentTool, build_agent_tools, schemas
from .compaction import Compactor
from .guard import CostGuardTripped
from .session import CheckpointError, SessionLog, replay
from .spill import DEFAULT_MAX_INLINE_BYTES, SpillPolicy
from .workspace import Workspace, WorkspaceError
from .model_client import ModelClient
from .loop_types import *
from .loop import CodingAgent
def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    from agentlab.util import force_utf8, head, kv, note, rule, takeaway

    from .guard import CostGuard
    from .llm import OpenAIChatClient
    from .llmconfig import LLMConfig

    force_utf8()
    ap = argparse.ArgumentParser(
        description="编码 agent：在工作区里真的读写文件、跑命令",
        epilog="示例：python -m agentplat.loop --task \"写一个快排并跑通测试\"",
    )
    ap.add_argument("--task", "-t", default="", help="要做的事（不给则进交互模式）")
    ap.add_argument("--workspace", "-w", default=None, help="工作区目录，默认 ./workspace")
    ap.add_argument("--model", "-m", default="", help="覆盖模型名")
    ap.add_argument("--max-iters", type=int, default=CodingAgent.HARD_ITERATIONS, help="最大轮数")
    ap.add_argument("--max-tools", type=int, default=60, help="最大工具调用次数")
    ap.add_argument("--max-usd", type=float, default=None, help="成本上限")
    ap.add_argument("--context", default="", help="附加背景资料")
    ap.add_argument("--quiet", action="store_true", help="只打结果，不打过程")
    ap.add_argument("--reset", action="store_true", help="先清空工作区")
    ap.add_argument("--resume", nargs="?", const="auto", default="",
                    help="续跑会话：--resume 取最近一次，或 --resume <session.jsonl>")
    ap.add_argument("--sessions-dir", default=None, help="会话日志目录")
    args = ap.parse_args(argv)

    cfg = LLMConfig.load()
    if not cfg.is_real:
        print(rule("="))
        print("  这个 agent 需要真实 LLM：工作区操作与工具调用依赖模型的函数调用能力。")
        print(rule("="))
        print("\n配置方式（任选其一）：")
        print("  ① 在面板里配： python -m agentplat.demo  打开 /settings 填 key")
        print("  ② 环境变量：   set AGENTLAB_LLM_KEY=sk-xxx")
        print("                 set AGENTLAB_LLM_BASE=https://api.deepseek.com")
        print("                 set AGENTLAB_LLM_MODEL=deepseek-chat")
        return 2

    from .config import PlatformConfig

    pcfg = PlatformConfig.from_env()
    guard = CostGuard(
        max_usd=args.max_usd if args.max_usd is not None else pcfg.max_usd_per_run,
        max_calls=pcfg.max_llm_calls_per_run,
    )
    ws = Workspace(args.workspace)
    if args.reset:
        note(ws.reset())

    model = args.model or cfg.model_or("mid") or cfg.model
    print(rule("="))
    print("  编码 Agent")
    print(rule("="))
    kv("工作区", str(ws.root))
    kv("模型", model)
    kv("端点", cfg.chat_url())
    # 上限可为 None（不设限），格式化要分支 —— 否则 `None.__format__` 直接崩。
    usd_cap = "不设限" if guard.max_usd is None else f"${guard.max_usd:.4f}"
    call_cap = "不设限" if guard.max_calls is None else f"{guard.max_calls} 次"
    kv("上限", f"{args.max_iters} 轮 / {args.max_tools} 次工具 / "
               f"花费 {usd_cap} / 调用数 {call_cap}")
    note("")
    note("安全边界：所有文件与命令操作限制在工作区内，越界会被拒绝；")
    note("          命令有白名单，破坏性命令会被拦下。这是**防手滑**，不是防恶意。")

    llm = OpenAIChatClient(cfg)

    def on_step(step: LoopStep) -> None:
        if args.quiet:
            return
        icon = {"think": "💭", "tool": "🔧", "observe": "📄",
                "finish": "🏁", "error": "❌", "guard": "🛡"}.get(step.kind, "·")
        print(f"\n{icon} [{step.index}] {step.title}")
        if step.detail:
            for ln in step.detail.splitlines()[:14]:
                print(f"     {ln[:160]}")

    agent = CodingAgent(
        llm=llm, cfg=cfg, workspace=ws, guard=guard, on_step=on_step,
        hard_iterations=args.max_iters,
        session_dir=args.sessions_dir,
    )

    # ---- 续跑：进程挂了之后接着跑，不重付已经花掉的钱 ----
    if args.resume:
        from .session import SessionLog, find_latest_session, replay

        if args.resume == "auto":
            base = Path(args.sessions_dir) if args.sessions_dir else (
                Path(ws.root).parent / ".sessions")
            path = find_latest_session(base, unfinished_only=True)
            if path is None:
                note(f"在 {base} 下没有找到**未完成**的会话日志。")
                note("（已完成的会话不会被续跑 —— 那只会白跑一趟。）")
                return 2
        else:
            path = Path(args.resume)
        log, skipped = SessionLog.load(path)
        state = replay(log, skipped)
        print(rule("="))
        print("  续跑已有会话")
        print(rule("="))
        state.render()
        note("")
        note("重放是**只读**的：已经写过的文件不会重写、已经跑过的命令不会重跑。")
        note("恢复后继续的是剩下的工作，不是把做过的再做一遍。")
        head(f"从第 {state.iterations_done} 轮之后继续")
        r = agent.resume(path, model=model)
        print()
        kv("结果", "成功" if r.ok else "未完成")
        kv("结束原因", r.stopped_by)
        kv("轮数 / 工具调用", f"{r.iterations} / {r.tool_calls}")
        kv("累计花费", f"${r.usd:.6f}")
        if r.error:
            note(f"错误：{r.error[:300]}")
        print()
        note("工作区改动：")
        for ln in ws.diff_summary().splitlines():
            note(ln)
        guard.render()
        return 0 if r.ok else 1

    def run_one(task: str) -> LoopResult:
        head(f"任务：{task}")
        r = agent.run(task, model=model, context=args.context)
        print()
        kv("结果", "成功" if r.ok else "未完成")
        kv("结束原因", r.stopped_by)
        kv("轮数 / 工具调用", f"{r.iterations} / {r.tool_calls}")
        kv("耗时", f"{r.elapsed_ms:.0f}ms")
        kv("token", f"in {r.tokens_in} / out {r.tokens_out}")
        kv("花费", f"${r.usd:.6f}")
        if r.summary:
            print()
            note("总结：")
            for ln in r.summary.splitlines():
                note(f"  {ln}")
        if r.error:
            note(f"错误：{r.error[:300]}")
        print()
        note(f"工具结果 spill：{self.spill.summary()}")
        note("工作区改动：")
        for ln in ws.diff_summary().splitlines():
            note(ln)
        return r

    if args.task:
        r = run_one(args.task)
        guard.render()
        return 0 if r.ok else 1

    # 交互模式
    note("")
    note("交互模式：输入任务回车执行；空行退出。任务会**共享同一个工作区**，")
    note("所以可以接着说「刚才那个函数加个测试」。")
    while True:
        try:
            task = input("\n任务> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not task:
            break
        run_one(task)
        if guard.tripped():
            note("护栏已触达上限，停止接受新任务。")
            break
    guard.render()
    takeaway(f"本次共改动 {ws.snapshot()['files_changed']} 个文件，"
             f"执行 {ws.snapshot()['commands_run']} 条命令。")
    return 0
