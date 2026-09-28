from agentlab.util import lab
from agentplat.benchmark import reliability

def main():
    with lab('lab-48-repeat-reliability','重复成功与偶然成功','pass@k 与 pass^k 回答不同问题'):
        rows=[{'task':'fixture','passed':True},{'task':'fixture','passed':False}]
        metrics=reliability(rows)[0]
        assert metrics['pass_at_k']==1 and metrics['pass_pow_k']==0
        print('[BROKEN-REPRODUCED] 两次取最好的一次会宣称成功，却掩盖一次失败')
        print('[FIX-APPLIED] 同时报告逐次结果与两次均成功的可靠性；不足两次不计算')
        print('[VERIFY] wrong_reliability_claims: 1 -> 0')
        print('[TAKEAWAY] 这是统计教学夹具，不是真实模型跑分。')
    return 0

if __name__=='__main__':raise SystemExit(main())
