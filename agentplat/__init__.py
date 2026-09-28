"""Capstone：把 18 个 lab 的结论装配成一个可运行的生产级 Agent 平台。

分层结构与 lab 的对应关系：

    config.py      所有旋钮集中一处，每项标注来源 lab
    context.py     多租户隔离 + 会话治理 + 上下文压缩   (lab-08 / lab-16)
    cache.py       五层缓存 + 能否缓存的声明式策略表     (lab-07)
    resilience.py  熔断 / 舱壁 / 分层限流 / 模型路由     (lab-04 / lab-06 / lab-13)
    tools.py       工具注册、参数校验、超时、幂等        (lab-15)
    checkpoint.py  长任务断点续跑 + 幂等 + 补偿          (lab-09)
    engine.py      主链路编排（一次请求的完整生命周期）  (lab-06 / lab-10 / lab-12)
    service.py     生命周期、readiness、优雅停机         (lab-01)
    loadgen.py     压测 + SLO 合规报告                   (lab-18)
"""

__all__ = ["config", "context", "cache", "resilience"]
