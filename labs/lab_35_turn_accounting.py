"""交互轮次与模型步骤：真实执行边界实验，使用临时夹具，无模型调用。"""
from agentlab.util import lab
from agentplat.sandbox_experiments import turn_accounting

def main():
    with lab('lab-35-turn-accounting', '交互轮次与模型步骤', '交互轮次与模型步骤'):
        metric, before, after = turn_accounting()
        assert before > after
        print(f"[BROKEN-REPRODUCED] {metric}={before}")
        print("[FIX-APPLIED] 使用运行时边界与可观测状态，拒绝未经证实的隔离声明")
        print(f"[VERIFY] {metric}: {before} -> {after}")
        print("[TAKEAWAY] 交互轮次与模型步骤需要可复现实验；跳过或拒绝执行不能冒充功能可用。")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
