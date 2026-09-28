"""Windows 文件权限隔离：真实执行边界实验，使用临时夹具，无模型调用。"""
from agentlab.util import lab
from agentplat.sandbox_experiments import native_boundary

def main():
    with lab('lab-30-native-filesystem', 'Windows 文件权限隔离', 'Windows 文件权限隔离'):
        metric, before, after = native_boundary()
        assert before > after
        print(f"[BROKEN-REPRODUCED] {metric}={before}")
        print("[FIX-APPLIED] 使用运行时边界与可观测状态，拒绝未经证实的隔离声明")
        print(f"[VERIFY] {metric}: {before} -> {after}")
        print("[TAKEAWAY] Windows 文件权限隔离需要可复现实验；跳过或拒绝执行不能冒充功能可用。")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
