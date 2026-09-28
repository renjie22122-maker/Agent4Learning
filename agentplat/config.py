"""Capstone 平台配置：把前面所有 lab 的结论固化成一份可调参数表。

生产上一个常见病症是"参数散落在代码各处"。这里把所有旋钮集中到一处，
并且**每一项都标注它来自哪个 lab 的结论**——配置即文档。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field


@dataclass
class PlatformConfig:
    # ---- 服务生命周期（lab-01）------------------------------------------
    shutdown_grace_s: float = 3.0  # 优雅停机的排空上限
    warmup_on_start: bool = True  # readiness 必须等预热完成

    # ---- 成本护栏（真实 key 下这是**安全机制**，不是优化项）-------------
    #: 接入真实 LLM 后，一个写错的循环/一次批量重放会真的花掉你的钱。
    #: 下面三条是硬闸门：达到上限立刻停止并明确报错，而不是"继续跑看看"。
    #: 默认值故意保守 —— 先小后大，确认行为再放开。
    max_usd_per_run: float = 1.0        # 单次实验/整进程累计花费上限（美元）
    max_llm_calls_per_run: int = 400    # 单次实验最大模型调用次数（防循环）
    max_tokens_per_request: int = 8192  # 单请求输入 token 上限（防上下文爆炸）
    #: 干跑模式：只估算花费与调用量，**不发真实请求**。批量实验前先干跑一遍。
    dry_run: bool = False

    # ---- 请求预算（lab-06）----------------------------------------------
    request_budget_ms: float = 3000.0
    stage_budget_ms: dict[str, float] = field(
        default_factory=lambda: {
            "rate_limit": 50.0,
            "cache": 120.0,
            "retrieve": 500.0,
            "llm": 1600.0,
            "tools": 700.0,
            "finalize": 300.0,
        }
    )

    def adapt_to_real_llm(self, timeout_s: float = 30.0) -> None:
        """接上真实 LLM 后自动放宽超时预算。

        这一条是被真实调用教出来的：默认预算是按**内置模拟器**（p50 500ms）
        调的，换成真实模型（p50 1.5s+，而且工具后还要再生成一次）立刻就会
        `BUDGET stage=retry 超出预算`。**用户不应该为了换个模型去调毫秒级参数**
        —— 平台要自己适配。

        预算怎么定：真实模型单次可能慢到十几秒，所以总预算 ≈ 2 × 单次超时，
        留出"首答 + 工具后二次生成"的空间；llm 阶段给到单次超时的 80%，
        保证一次完整尝试能跑完而不是刚发起就被砍。
        """
        single_ms = max(3000.0, timeout_s * 1000.0)
        llm_stage = single_ms * 0.8
        self.request_budget_ms = single_ms * 2 + 1000.0
        self.stage_budget_ms = {
            "rate_limit": 50.0,
            "cache": 150.0,
            "retrieve": 1500.0,
            "llm": llm_stage,
            "tools": 2000.0,
            "finalize": 600.0,
        }
        # 真实上游本来就慢，"慢调用"阈值必须跟着抬，否则每次都被判定为慢调用
        # 累计失败 → 熔断器会误开（把正常的慢当成故障）。
        self.breaker_slow_call_ms = single_ms * 0.9
        # 重试在真实场景下更贵也更慢：减少次数，避免把预算全烧在重试上
        self.max_retries = 1
        self.retry_cap_s = 1.0
        # 客户端自限流：真实账号配额通常不高，默认保守一点
        self.tenant_qps = 20.0
        self.tenant_burst = 30.0

    # ---- 重试（lab-06）--------------------------------------------------
    max_retries: int = 2
    retry_base_s: float = 0.06
    retry_cap_s: float = 0.35
    retry_budget_per_request: int = 6  # 单请求最多允许的重试次数（防重试风暴）

    # ---- 熔断（lab-06 / lab-13）-----------------------------------------
    breaker_failure_threshold: int = 6
    breaker_cooldown_s: float = 1.0
    breaker_slow_call_ms: float = 2500.0
    breaker_half_open_max: int = 1

    # ---- 隔离与闸门（lab-04 / lab-16 / lab-17）--------------------------
    global_concurrency: int = 48
    tenant_concurrency: int = 8
    batch_max_ratio: float = 0.7  # 批处理最多占用的并发比例，给交互式留容量
    queue_max_depth: int = 200
    # 租户 QPS 配额：要显著高于单租户的正常流量（否则配额本身成了故障源），
    # 它的作用是拦住"某个租户突然放大 100 倍"这种异常，而不是限制正常使用。
    tenant_qps: float = 200.0
    tenant_burst: float = 300.0
    # 客户端自限流 = 该模型档位的吞吐上限 × 这个比例。
    # 1.0 表示"和上游容量对齐"：再高就是在替上游排队（排队时间算进你的超时预算），
    # 再低则会人为压低吞吐。这是**容量规划**的入口参数，不是随手填的常数。
    model_headroom_ratio: float = 1.0

    # ---- 缓存（lab-07）--------------------------------------------------
    exact_cache_size: int = 2048
    exact_cache_ttl_s: float = 300.0
    semantic_cache_size: int = 512
    semantic_cache_ttl_s: float = 120.0
    # 语义缓存的相似度阈值。**刻意偏低（0.30）+ 判别性覆盖度护栏（1.0）**：
    # 实测字符级相似度无法分离"同义改写"与"危险近似"（两类区间重叠），
    # 所以策略是"阈值管召回、护栏管正确性"，且护栏取最保守值。
    # 结果是零错误命中，代价是改写命中率低 —— 生产上要真正做语义缓存必须上 embedding。
    semantic_threshold: float = 0.30
    tool_cache_ttl_s: float = 60.0

    # ---- 上下文治理（lab-08）--------------------------------------------
    context_token_budget: int = 6000
    context_keep_turns: int = 6
    context_budget_split: dict[str, float] = field(
        default_factory=lambda: {
            "system": 0.12,
            "state": 0.18,
            "retrieval": 0.35,
            "history": 0.28,
            "output": 0.07,
        }
    )

    # ---- 模型路由（lab-12 / lab-13）-------------------------------------
    routing_mode: str = "cascade"  # all_small | all_large | static | cascade
    cascade_upgrade_confidence: float = 0.58
    tenant_daily_budget_usd: float = 2.0

    # ---- 长任务（lab-09）------------------------------------------------
    checkpoint_dir: str = ".lab_state/capstone"
    max_task_steps: int = 32
    task_max_attempts: int = 3

    # ---- SLO（lab-18）---------------------------------------------------
    # 注意 SLO 不是"越高越好"：上游本身有 6% 错误率，把成功率目标定成 99.9%
    # 等于给自己发一张永远还不完的欠条。SLO 必须建立在可达容量之上。
    slo_p95_ms: float = 2500.0
    slo_success_rate: float = 0.90
    # 有效吞吐目标（每秒成功完成的请求数）。比"成功率"更能反映真实服务能力，
    # 也是唯一能在不同负载强度之间做公平对比的口径。
    slo_goodput_rps: float = 15.0
    slo_cost_per_success_usd: float = 0.0025
    error_budget_fast_window_s: float = 60.0
    error_budget_slow_window_s: float = 600.0

    # ---- Provider 模拟（仅用于 capstone 压测）---------------------------
    provider_error_rate: float = 0.06
    provider_p50_ms: float = 500.0
    corpus_size: int = 20_000

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> "PlatformConfig":
        cfg = cls()
        raw = os.environ.get("AGENTLAB_CONFIG")
        if raw:
            for k, v in json.loads(raw).items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        return cfg

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True)

    def render(self) -> None:
        print("\n  ┌─ 平台配置（每一项都对应一个 lab 的结论）")
        for k, v in sorted(asdict(self).items()):
            if isinstance(v, dict):
                print(f"  │ {k}:")
                for kk, vv in v.items():
                    print(f"  │     {kk:<18} {vv}")
            else:
                print(f"  │ {k:<38} {v}")
        print("  └" + "─" * 62)


# 各阶段预算之和 vs 总预算：刻意允许"之和 > 总预算"，由 Deadline 动态收敛
DEFAULT = PlatformConfig()
