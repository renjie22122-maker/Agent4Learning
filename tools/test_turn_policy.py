"""验证终止策略钩子的行为（对应 DSH 的 agent/turn-stopping）。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentplat.loop import (  # noqa: E402
    BudgetPolicy,
    CodingAgent,
    CompositePolicy,
    LoopContext,
    MaxIterationsPolicy,
    Stop,
)


def ctx(it=1, usd=0.0, verified=False, tool_calls=0, elapsed=1.0):
    return LoopContext(
        iteration=it, tool_calls=tool_calls, elapsed_s=elapsed, usd=usd,
        tokens_in=0, tokens_out=0, verified=verified,
    )


print("=== 1. 只给预算策略 ===")
p = BudgetPolicy(max_usd=0.01, max_seconds=60)
for c in (ctx(usd=0.0), ctx(usd=0.02), ctx(usd=0.0, elapsed=90)):
    v = p(c)
    label = v.reason if v else "继续"
    print(f"  usd={c.usd:<6} elapsed={c.elapsed_s:<5} -> {label}")

print("\n=== 2. 只给轮次策略（软上限在验证过之后不再拦）===")
p2 = MaxIterationsPolicy(soft_limit=5, hard_limit=10)
for c in (ctx(it=3), ctx(it=6, verified=False), ctx(it=6, verified=True), ctx(it=10)):
    v = p2(c)
    label = v.reason if v else "继续"
    print(f"  iter={c.iteration:<4} verified={str(c.verified):<6} -> {label}")

print("\n=== 3. 组合：任一策略说要停就停 ===")
p3 = CompositePolicy(BudgetPolicy(max_usd=0.05), MaxIterationsPolicy(soft_limit=4))
for c in (ctx(it=2, usd=0.01), ctx(it=2, usd=0.09), ctx(it=5, verified=False)):
    v = p3(c)
    label = v.reason if v else "继续"
    print(f"  iter={c.iteration:<4} usd={c.usd:<6} -> {label}")

print("\n=== 4. 自定义策略（这就是插件写法的样子）===")


class NoProgressPolicy:
    """连续 N 轮没有进展就停 —— 一个很实用的自定义策略。"""

    def __init__(self, patience: int = 3):
        self.patience = patience
        self.stale = 0
        self.last_calls = -1

    def __call__(self, c: LoopContext) -> Stop | None:
        if c.tool_calls == self.last_calls and not c.verified:
            self.stale += 1
        else:
            self.stale = 0
        self.last_calls = c.tool_calls
        if self.stale >= self.patience:
            return Stop(f"连续 {self.patience} 轮工具调用数没有变化，判定为无进展")
        return None


p4 = NoProgressPolicy(patience=2)
for i in range(1, 6):
    v = p4(ctx(it=i, tool_calls=3))
    label = v.reason if v else "继续"
    print(f"  iter={i}  -> {label}")

print("\n=== 5. 上限常量 ===")
print(f"  单步工具上限 MAX_TOOLS_PER_STEP = {CodingAgent.MAX_TOOLS_PER_STEP}")
print(f"  软轮次兜底 SOFT_ITERATIONS      = {CodingAgent.SOFT_ITERATIONS}")
print(f"  硬轮次兜底 HARD_ITERATIONS      = {CodingAgent.HARD_ITERATIONS}")
import inspect
assert inspect.signature(CodingAgent).parameters["max_wall_s"].default is None
print("  wall-clock 上限默认关闭，显式传 max_wall_s 启用")
print()
print("说明：兜底只在**没有任何策略介入**时生效。主终止机制是 policy 钩子 ——")
print("      这与 DSH 的 agent/turn-stopping 是同一个思路：机制留钩子，策略外部注入。")

# 断言四条核心行为，便于回归。
# 注意别写成 `ok &= p(...) is not None, "说明"` —— 那是元组表达式，不是赋值。
checks = [
    ("预算超限应停止", p(ctx(usd=0.02)) is not None),
    ("未验证不能被误判为死循环", p2(ctx(it=6, verified=False)) is None),
    ("已验证时软上限不应拦", p2(ctx(it=6, verified=True)) is None),
    ("无进展策略应触发", p4(ctx(it=3, tool_calls=3)) is not None),
]
ok = True
print()
print("=== 断言 ===")
for label, passed in checks:
    print(f"  {'✅' if passed else '❌'} {label}")
    ok = ok and passed

print()
print("=" * 74)
print("  结论：" + ("终止策略钩子行为正确 ✅" if ok else "存在失败项 ❌"))
print("=" * 74)
raise SystemExit(0 if ok else 1)
