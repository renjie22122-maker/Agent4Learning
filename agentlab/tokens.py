"""Token 估算与成本模型。

生产环境里"token 成本持续增长"这个问题，第一件事是把 token **计量清楚**：
没有计量就没有降本。真实的 tokenizer 是 BPE，这里用字符类别启发式近似
（英文 ~4 字符/token，中文 ~1.6 字符/token），误差对教学结论无影响。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence


def count_tokens(text: str) -> int:
    """近似 token 数。中英混排分开计数。"""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff":
            cjk += 1
        else:
            other += 1
    # 中文约 1.6 字/token；其余按 4 字符/token，再补一点标点开销
    return int(math.ceil(cjk / 1.6 + other / 4.0)) + 1


def count_messages(messages: Iterable[object]) -> int:
    """按 chat 格式估算：每条消息有 3~4 token 的结构开销。"""
    total = 0
    for m in messages:
        content = getattr(m, "content", "") or ""
        total += count_tokens(str(content)) + 4
    return total


@dataclass(frozen=True)
class ModelSpec:
    """一个可路由的模型档位。"""

    name: str
    tier: str  # "small" | "mid" | "large"
    latency_p50_ms: float
    latency_sigma: float = 0.45
    quality: float = 0.8  # 0..1，虚构的质量分，用于演示"大小模型平衡"
    in_price: float = 0.5  # $/1M input tokens
    out_price: float = 1.5  # $/1M output tokens
    max_parallel: int = 8
    error_rate: float = 0.01
    supports_prefix_cache: bool = True

    @property
    def key(self) -> str:
        return self.name

    def capacity_qps(self) -> float:
        """这个档位的**吞吐上限**（请求/秒）。

        这是一个很容易搞错的地方：provider 给出的是**并发上限**（``max_parallel``），
        而限流器需要的是**速率**。两者差一个延迟：

            吞吐 ≈ 并发 / 平均延迟

        例如并发 10、p50 700ms → 约 14 req/s。把并发数直接当 QPS 用，
        会把限流器配错十几倍 —— 生产上这类"看起来合理的错误常数"非常常见。
        """
        return self.max_parallel / max(0.05, self.latency_p50_ms / 1000.0)


# 一条典型的"三层模型"阶梯：便宜快但会错 / 均衡 / 贵但准
SMALL = ModelSpec(
    name="small-8b",
    tier="small",
    latency_p50_ms=220,
    latency_sigma=0.35,
    quality=0.62,
    in_price=0.05,
    out_price=0.15,
    max_parallel=24,
    error_rate=0.006,
)
MID = ModelSpec(
    name="mid-32b",
    tier="mid",
    latency_p50_ms=640,
    latency_sigma=0.42,
    quality=0.84,
    in_price=0.6,
    out_price=1.8,
    max_parallel=10,
    error_rate=0.012,
)
LARGE = ModelSpec(
    name="large-400b",
    tier="large",
    latency_p50_ms=1900,
    latency_sigma=0.55,
    quality=0.95,
    in_price=5.0,
    out_price=15.0,
    max_parallel=4,
    error_rate=0.02,
)

LADDER: tuple[ModelSpec, ...] = (SMALL, MID, LARGE)

MODELS: dict[str, ModelSpec] = {m.name: m for m in LADDER}


@dataclass
class CostLedger:
    """按维度累计花费与 token。降本优化的效果靠它对账。"""

    in_tokens: int = 0
    out_tokens: int = 0
    cached_tokens: int = 0
    usd: float = 0.0
    calls: int = 0
    by_tenant: dict[str, float] = field(default_factory=dict)
    by_model: dict[str, float] = field(default_factory=dict)
    by_tag: dict[str, float] = field(default_factory=dict)

    def add(
        self,
        model: ModelSpec | None,
        in_tokens: int,
        out_tokens: int,
        cached_tokens: int = 0,
        tenant: str = "default",
        tag: str = "",
        latency_s: float = 0.0,
    ) -> float:
        if model is None:
            model = MID
        # 命中的前缀缓存通常按 10% 计价
        billable_in = max(0, in_tokens - cached_tokens) + cached_tokens * 0.1
        cost = (billable_in * model.in_price + out_tokens * model.out_price) / 1_000_000
        self.in_tokens += in_tokens
        self.out_tokens += out_tokens
        self.cached_tokens += cached_tokens
        self.usd += cost
        self.calls += 1
        self.by_tenant[tenant] = self.by_tenant.get(tenant, 0.0) + cost
        self.by_model[model.name] = self.by_model.get(model.name, 0.0) + cost
        if tag:
            self.by_tag[tag] = self.by_tag.get(tag, 0.0) + cost
        return cost

    @property
    def total_tokens(self) -> int:
        return self.in_tokens + self.out_tokens

    @property
    def cache_hit_ratio(self) -> float:
        return self.cached_tokens / self.in_tokens if self.in_tokens else 0.0

    @property
    def avg_usd_per_call(self) -> float:
        return self.usd / self.calls if self.calls else 0.0

    def summary(self) -> str:
        return (
            f"calls={self.calls} in={self.in_tokens} out={self.out_tokens} "
            f"cached={self.cached_tokens} ({self.cache_hit_ratio:.1%}) "
            f"usd=${self.usd:.4f}"
        )


def price_of(model: ModelSpec, in_tokens: int, out_tokens: int, cached: int = 0) -> float:
    billable_in = max(0, in_tokens - cached) + cached * 0.1
    return (billable_in * model.in_price + out_tokens * model.out_price) / 1_000_000


def fit_to_budget(
    texts: Sequence[str], budget_tokens: int, keep_tail: int = 1
) -> list[str]:
    """从后往前保留内容直到塞进 ``budget_tokens``（保尾部的截断策略）。

    真实系统里这是 prompt 组装阶段的最后一道硬闸门——**必须是工程硬编码**，
    不能指望模型自己"注意别超长"。
    """
    kept: list[str] = []
    used = 0
    for i, t in enumerate(reversed(texts)):
        n = count_tokens(t)
        if used + n > budget_tokens and len(kept) >= keep_tail:
            break
        kept.append(t)
        used += n
    return list(reversed(kept))
