"""网页授权即时撤销：真实执行边界实验，使用临时夹具，无模型调用。"""
from agentlab.util import lab
from agentplat.sandbox_experiments import domain_revocation

def main():
    with lab('lab-34-domain-revocation', '网页授权即时撤销', '网页授权即时撤销'):
        metric, before, after = domain_revocation()
        assert before > after
        print(f"[BROKEN-REPRODUCED] {metric}={before}")
        print("[FIX-APPLIED] 使用运行时边界与可观测状态，拒绝未经证实的隔离声明")
        print(f"[VERIFY] {metric}: {before} -> {after}")
        print("[TAKEAWAY] 网页授权即时撤销需要可复现实验；跳过或拒绝执行不能冒充功能可用。")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
