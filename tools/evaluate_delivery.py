"""评测 agent 的交付质量：真跑一次任务，然后**独立验证**它交付的东西。

为什么需要这个
--------------
"agent 说完成了"不等于"交付物是对的"。本项目实测就出现过：
agent 写的 quicksort 有 23 个测试通过，但在随机数组上会挂起 ——
如果只看"测试通过率"或"agent 自己的总结"，会直接把这个 bug 放进生产。

所以这个脚本做三件事，顺序不能反：
  ① 让 agent 真跑任务（用它的循环、工具、策略钩子）
  ② **独立**验证交付物（不用 agent 的话，自己跑测试、自己探测边界）
  ③ 给出可信度结论：交付物能用吗？哪里不可信？

独立验证是重点：它是"工程侧"对"模型侧"的制衡。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentplat.config import PlatformConfig  # noqa: E402
from agentplat.guard import CostGuard  # noqa: E402
from agentplat.llm import OpenAIChatClient  # noqa: E402
from agentplat.llmconfig import LLMConfig  # noqa: E402
from agentplat.loop import BudgetPolicy, CodingAgent, CompositePolicy  # noqa: E402
from agentplat.workspace import Workspace  # noqa: E402

WS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")
PY = sys.executable


def head(t: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {t}")
    print("=" * 78)


def run_in_subprocess(code: str, timeout: float = 8.0) -> tuple[str, str]:
    """在子进程里跑代码，硬超时。跨平台可靠（不依赖信号）。"""
    try:
        p = subprocess.run([PY, "-c", code], cwd=WS_DIR, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return ("OK" if p.returncode == 0 else "ERROR",
                ((p.stdout or "") + (p.stderr or "")).strip()[-500:])
    except subprocess.TimeoutExpired:
        return "HANG", f"超过 {timeout:.0f}s 未返回（死循环或无限递归）"


# --------------------------------------------------------------------------
# ② 独立验证器 —— 不听 agent 的总结，自己判断
# --------------------------------------------------------------------------


def verify_deliverable() -> dict:
    """独立验证交付物。返回结构化结论。"""
    qs = os.path.join(WS_DIR, "quicksort.py")
    ts = os.path.join(WS_DIR, "test_quicksort.py")
    report: dict = {
        "files": sorted(f for f in os.listdir(WS_DIR) if not f.startswith(".")),
        "has_module": os.path.exists(qs),
        "has_tests": os.path.exists(ts),
        "syntax_ok": False,
        "pytest": {},
        "boundary_probes": [],
        "verdict": "",
    }
    if not report["has_module"]:
        report["verdict"] = "交付物缺失：没有 quicksort.py"
        return report

    # 语法
    st, out = run_in_subprocess(
        "import ast,io;ast.parse(io.open('quicksort.py',encoding='utf-8').read());print('ok')",
        timeout=10,
    )
    report["syntax_ok"] = (st == "OK")

    # agent 自己的测试套件（限时，防止它自己就挂住）
    if report["has_tests"]:
        st, out = run_in_subprocess(
            "import pytest,sys;sys.exit(pytest.main(['test_quicksort.py','-q','--no-header','-p','no:cacheprovider']))",
            timeout=60,
        )
        report["pytest"] = {"status": st, "tail": out[-400:]}
        if st == "HANG":
            report["pytest"]["note"] = "agent 自己的测试套件都跑不完"

    # 边界探测：不看 agent 的测试，自己用不同数据形态打
    probes = {
        "随机小数组(size=50)": "[__import__('random').Random(1).randrange(100) for _ in range(50)]",
        "随机中数组(size=300)": "[__import__('random').Random(2).randrange(100) for _ in range(300)]",
        "全相等(size=200)": "[7]*200",
        "已排序(size=300)": "list(range(300))",
        "逆序(size=300)": "list(range(300,0,-1))",
        "只两个值(size=200)": "[1,2]*100",
        "大量重复(size=400)": "[i%5 for i in range(400)]",
        "含负数与浮点": "[-1.5,3,0,-2,2.5,1]",
        "字符串列表": "['pear','apple','fig']",
        "空数组": "[]",
        "单元素": "[42]",
    }
    for name, expr in probes.items():
        code = textwrap.dedent(f"""
            import sys
            sys.setrecursionlimit(200000)
            from quicksort import quicksort
            data = {expr}
            out = quicksort(list(data))
            exp = sorted(data)
            print("correct" if out == exp else "WRONG")
        """)
        st, out = run_in_subprocess(code, timeout=8.0)
        report["boundary_probes"].append(
            {"case": name, "status": st, "detail": out[:100]}
        )

    hangs = [p["case"] for p in report["boundary_probes"] if p["status"] == "HANG"]
    wrongs = [p["case"] for p in report["boundary_probes"] if p["detail"] == "WRONG"]
    errs = [p["case"] for p in report["boundary_probes"] if p["status"] == "ERROR"]
    total = len(report["boundary_probes"])

    if hangs:
        report["verdict"] = (
            f"❌ 不可用：{len(hangs)}/{total} 个边界用例**挂起**（{', '.join(hangs)}）。"
            f"agent 自己的测试可能没覆盖这些形态。"
        )
    elif wrongs:
        report["verdict"] = f"❌ 不可用：{len(wrongs)}/{total} 个用例结果错误（{', '.join(wrongs)}）"
    elif errs:
        report["verdict"] = f"⚠ 部分可用：{len(errs)}/{total} 个用例报错（{', '.join(errs)}）"
    else:
        report["verdict"] = f"✅ 通过全部 {total} 个独立边界用例"
    return report


def main() -> int:
    cfg = LLMConfig.load()
    if not cfg.is_real:
        print("需要真实 LLM 才能跑这个评测（见 /settings 配置）。")
        return 2

    pcfg = PlatformConfig.from_env()
    guard = CostGuard(max_usd=0.60, max_calls=200)
    ws = Workspace(WS_DIR)
    ws.reset()

    head("① 让 agent 真跑任务")
    llm = OpenAIChatClient(cfg)
    policy = CompositePolicy(
        BudgetPolicy(max_usd=0.55, max_seconds=420),
    )
    agent = CodingAgent(
        llm=llm, cfg=cfg, workspace=ws, guard=guard, policy=policy,
        on_step=lambda s: print(f"    [{s.index:>2}] {s.kind:<7} {s.title[:88]}"),
    )
    t0 = time.perf_counter()
    r = agent.run(
        "写快速排序 quicksort.py 和 pytest 测试 test_quicksort.py。"
        "实现要正确处理：重复元素、已排序、逆序、全相等、含负数浮点。"
        "必须用 pytest 跑通全部测试，全绿后调用 finish。"
    )
    print()
    print(f"    agent 自评   : {'成功' if r.ok else '未完成'} ({r.stopped_by})")
    print(f"    轮数/工具调用 : {r.iterations} / {r.tool_calls}")
    print(f"    耗时/成本     : {time.perf_counter() - t0:.0f}s / ${r.usd:.6f}")
    if r.summary:
        print(f"    agent 的话   : {r.summary[:200]}")

    head("② 独立验证交付物（不听 agent 的总结）")
    rep = verify_deliverable()
    print(f"  工作区文件 : {rep['files']}")
    print(f"  语法检查   : {'通过' if rep['syntax_ok'] else '失败'}")
    if rep.get("pytest"):
        print(f"  agent 自己的测试 : {rep['pytest']['status']}")
        tail = rep["pytest"]["tail"].splitlines()
        for ln in tail[-4:]:
            print(f"      {ln.strip()[:110]}")
    print()
    print("  独立边界探测（这些用例是**我写的**，不是 agent 写的）：")
    print(f"    {'用例':<22}{'结果':<8}说明")
    for p in rep["boundary_probes"]:
        mark = {"OK": "通过", "HANG": "挂起", "ERROR": "报错"}[p["status"]]
        detail = "" if p["status"] == "OK" else p["detail"][:50]
        print(f"    {p['case']:<22}{mark:<8}{detail}")

    head("③ 可信度结论")
    print(f"  {rep['verdict']}")
    print()
    print("  这说明什么：")
    print("    · agent 的**自评**（finish 里的总结）不能作为验收依据 ——")
    print("      它会说'全部测试通过'，而测试是它自己写的，覆盖不到的地方它不知道。")
    print("    · 所以工程侧必须有**独立的验收**：换数据形态、加边界、限时跑。")
    print("    · 这也是为什么 lab-18 把'任务成功率'和'每成功任务成本'分开统计：")
    print("      'agent 说成功'与'交付物真的可用'是两件事。")

    out = os.path.join(WS_DIR, "_verify_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"agent": {"ok": r.ok, "stopped_by": r.stopped_by,
                             "iterations": r.iterations, "tool_calls": r.tool_calls,
                             "usd": round(r.usd, 6), "summary": r.summary},
                   "verify": rep}, f, ensure_ascii=False, indent=2)
    print(f"\n  报告已写入 {out}")
    guard.render()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
