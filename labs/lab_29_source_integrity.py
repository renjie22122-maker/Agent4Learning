"""来源引用与数据完整性：同一执行内核的故障对照，离线脚本模型，不代表真实 LLM 质量。"""
from agentlab.util import lab, phase
from agentplat.experiments import source_integrity

LAB_ID = 'lab-29-source-integrity'
QUESTIONS = ['来源引用与数据完整性']

def main():
    with lab(LAB_ID, '来源引用与数据完整性', '来源引用与数据完整性'):
        phase("1. 复现故障")
        metric, before, after = source_integrity()
        print(f"[BROKEN-REPRODUCED] {metric}={before:.3f}")
        phase("2. 观测 / 归因")
        print("同一故障输入；断言检查状态、副作用或证据，不依赖模型自评。")
        phase("3. 修复")
        print(f"[FIX-APPLIED] {metric}={after:.3f}")
        phase("4. 验证")
        assert after < before, (metric, before, after)
        print(f"[VERIFY] {metric}: {before:.3f} -> {after:.3f} ({(after-before)/before*100:.1f}%)")
        print("[TAKEAWAY] 来源引用与数据完整性必须由执行内核保证；脚本化实验与真实模型评估分开报告。")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
