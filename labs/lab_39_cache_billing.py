"""Use reported cache hits instead of charging all input at cache-miss price."""
from datetime import datetime,timezone
from agentlab.util import lab
from agentlab.providers import Usage
from agentplat.llmconfig import LLMConfig
from agentplat.billing import quote


def main():
    with lab('lab-39-cache-billing','缓存命中与费用核算','缓存 token 是输入的子集，不是额外输入'):
        cfg=LLMConfig(base_url='https://api.deepseek.com',model='deepseek-flash')
        usage=Usage(1000000,100000,800000)
        bill=quote(cfg,usage,timestamp=datetime(2026,9,27,8,tzinfo=timezone.utc).timestamp())
        wrong=(usage.in_tokens*bill['price_miss_per_m']+usage.out_tokens*bill['price_out_per_m'])/1000000
        correct=bill['usd'];assert correct<wrong
        print('[BROKEN-REPRODUCED] 用全部输入乘未命中单价，会高估已缓存的输入费用')
        print('[FIX-APPLIED] 分开计算缓存命中、未命中和输出；保留价格来源')
        print(f'[VERIFY] calculated_cost_usd: {wrong} -> {correct}')
        print('[TAKEAWAY] API usage 是实测用量；按单价计算仍不是供应商最终账单。')
    return 0


if __name__=='__main__':raise SystemExit(main())
