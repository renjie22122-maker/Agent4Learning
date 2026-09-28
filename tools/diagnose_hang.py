"""定位 agent 生成的 quicksort 挂起：每次尝试都在**子进程**里跑，硬超时。

为什么必须用子进程：挂起是一段死循环或无限递归，在本进程里连
KeyboardInterrupt 都可能进不去（纯 Python 循环确实收得到，但如果陷在
C 层或异常被吞掉就收不到）。子进程 + kill 是唯一可靠的手段。

Windows 没有 SIGALRM，所以上一版复现脚本的"超时保护"其实完全没生效 ——
这就是为什么它把终端卡住了。教训：**跨平台写超时不要依赖信号**。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

WS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")
PY = sys.executable


def run_snippet(code: str, timeout: float = 6.0) -> tuple[str, str]:
    """在子进程里跑一段代码，返回 (状态, 输出)。"""
    try:
        p = subprocess.run(
            [PY, "-c", code], cwd=WS, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        if p.returncode == 0:
            return "OK", (p.stdout or "").strip()
        return "ERROR", ((p.stdout or "") + (p.stderr or "")).strip()[-600:]
    except subprocess.TimeoutExpired:
        return "HANG", f"超过 {timeout:.0f}s 未返回"


print("=" * 78)
print("  1. 查看 agent 生成的 quicksort 的分区实现")
print("=" * 78)
src = open(os.path.join(WS, "quicksort.py"), encoding="utf-8").read()
lines = src.splitlines()
# 打印 _partition 与主递归体，这两处最可能有问题
for i, ln in enumerate(lines, 1):
    if ln.startswith(("def ", "INSERTION", "RECUR")):
        print(f"  {i:>4}| {ln}")

print()
print("=" * 78)
print("  2. 逐尺寸探测（子进程 + 硬超时）")
print("=" * 78)
for size in (10, 50, 100, 200, 400, 800, 1500):
    code = textwrap.dedent(f"""
        import random, sys
        sys.setrecursionlimit(100000)
        from quicksort import quicksort
        rng = random.Random(42)
        data = [rng.randrange(100) for _ in range({size})]
        out = quicksort(data)
        print("correct" if out == sorted(data) else "WRONG")
    """)
    status, out = run_snippet(code)
    print(f"  随机数组 size={size:<6} -> {status:<6} {out[:70]}")

print()
print("=" * 78)
print("  3. 换不同数据形态（找到触发条件）")
print("=" * 78)
shapes = {
    "全相等": "[7] * 200",
    "已排序": "list(range(200))",
    "逆序": "list(range(200, 0, -1))",
    "只两个值": "[1, 2] * 100",
    "少量重复": "[i % 5 for i in range(200)]",
}
for name, expr in shapes.items():
    code = textwrap.dedent(f"""
        import sys
        sys.setrecursionlimit(100000)
        from quicksort import quicksort
        data = {expr}
        out = quicksort(data)
        print("correct" if out == sorted(data) else "WRONG")
    """)
    status, out = run_snippet(code)
    print(f"  {name:<10} -> {status:<6} {out[:60]}")

print()
print("=" * 78)
print("  4. 递归深度探测")
print("=" * 78)
code = textwrap.dedent("""
    import sys
    from quicksort import quicksort
    sys.setrecursionlimit(100000)
    rng_data = __import__("random").Random(1)
    for n in (50, 100, 200, 400):
        sys.setrecursionlimit(100000)
        try:
            quicksort([rng_data.randrange(1000) for _ in range(n)])
            print(n, "ok")
        except RecursionError:
            print(n, "RecursionError")
            break
""")
status, out = run_snippet(code, timeout=10)
print(f"  {status}: {out[:300]}")
