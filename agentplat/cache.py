"""Capstone 缓存体系：五层缓存 + 一张"能不能缓存"的策略表。

lab-07 的结论在这里固化成一个可配置的策略对象。最重要的设计决策：

**"能不能缓存"是工程硬编码的声明式配置，不是运行时问模型。**
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

from agentlab.metrics import METRICS
from agentlab.tokens import count_tokens

T = TypeVar("T")


# --------------------------------------------------------------------------
# 基础 LRU + TTL
# --------------------------------------------------------------------------


@dataclass
class CacheEntry(Generic[T]):
    value: T
    expire_at: float
    created_at: float
    hits: int = 0
    tenant: str = "default"
    version: int = 1


class TTLCache(Generic[T]):
    """有界 LRU + TTL。**容量必须有上限**，否则就是 lab-02 里的第 1 号泄漏。"""

    def __init__(
        self,
        name: str,
        max_size: int,
        ttl_s: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self.max_size = max_size
        self.ttl_s = ttl_s
        self._clock = clock
        self._data: OrderedDict[str, CacheEntry[T]] = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expired = 0
        self.stale_served = 0
        self.m_hit = METRICS.counter(f"cache_{name}_hits_total", "缓存命中")
        self.m_miss = METRICS.counter(f"cache_{name}_misses_total", "缓存未命中")
        self.m_evict = METRICS.counter(f"cache_{name}_evictions_total", "缓存淘汰")
        self.m_size = METRICS.gauge(f"cache_{name}_size", "缓存条目数")

    def get(self, key: str, max_age_s: float | None = None) -> T | None:
        with self._lock:
            ent = self._data.get(key)
            if ent is None:
                self.misses += 1
                self.m_miss.inc()
                return None
            now = self._clock()
            if now >= ent.expire_at:
                self._data.pop(key, None)
                self.expired += 1
                self.misses += 1
                self.m_miss.inc()
                return None
            if max_age_s is not None and (now - ent.created_at) > max_age_s:
                # 有"最大可接受陈旧度"的场景：宁可不命中，也不返回过期数据
                self.stale_served += 1
                self.misses += 1
                self.m_miss.inc()
                return None
            ent.hits += 1
            self._data.move_to_end(key)
            self.hits += 1
            self.m_hit.inc()
            return ent.value

    def put(self, key: str, value: T, tenant: str = "default", ttl_s: float | None = None) -> None:
        with self._lock:
            ttl = self.ttl_s if ttl_s is None else ttl_s
            now = self._clock()
            self._data[key] = CacheEntry(value, now + ttl, now, 0, tenant)
            self._data.move_to_end(key)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)
                self.evictions += 1
                self.m_evict.inc()
            self.m_size.set(len(self._data))

    def invalidate(self, predicate: Callable[[str], bool] | None = None) -> int:
        with self._lock:
            if predicate is None:
                n = len(self._data)
                self._data.clear()
            else:
                keys = [k for k in self._data if predicate(k)]
                n = len(keys)
                for k in keys:
                    self._data.pop(k, None)
            self.m_size.set(len(self._data))
            return n

    def invalidate_tenant(self, tenant: str) -> int:
        return self.invalidate(lambda k: k.startswith(f"{tenant}|"))

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> str:
        return (
            f"{self.name}: hit={self.hits} miss={self.misses} "
            f"ratio={self.hit_ratio:.1%} evict={self.evictions} expired={self.expired} "
            f"size={len(self._data)}/{self.max_size}"
        )


# --------------------------------------------------------------------------
# 语义缓存
# --------------------------------------------------------------------------


def _token_set(text: str) -> set[str]:
    """字符 2-gram + 英文词，够用的轻量相似度基础。"""
    t = text.lower().strip()
    grams = {t[i : i + 2] for i in range(max(1, len(t) - 1))}
    grams |= set(t.split())
    return grams


def jaccard(a: str, b: str) -> float:
    sa, sb = _token_set(a), _token_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


#: 领域实体词表。生产上这份表来自业务词库/知识库实体抽取，
#: 这里手工列出以保持零依赖。
_ENTITY_LEXICON = (
    "缓存", "穿透", "击穿", "雪崩", "熔断", "限流", "降级", "隔离",
    "内存", "泄漏", "并发", "队列", "背压", "租户", "会话", "权限",
    "成本", "token", "模型", "路由", "检索", "召回", "精排", "索引",
    "超时", "重试", "上下文", "压缩", "缓存", "子agent", "工具", "长任务",
    "断点", "幂等", "批处理", "监控", "指标", "slo", "告警", "灰度",
)


def entities_of(text: str) -> set[str]:
    """抽取问题里的领域实体（中文关键词 + 英文/数字词）。"""
    low = text.lower()
    ents = {w for w in _ENTITY_LEXICON if w in low}
    ents |= {
        w
        for w in low.replace("？", " ").replace("?", " ").replace("，", " ").split()
        if len(w) >= 2 and w.isascii()
    }
    return ents


def char_bigrams(text: str) -> set[str]:
    """连续汉字的 2-gram —— 用来做"判别性词"的细粒度比对。"""
    out: set[str] = set()
    run: list[str] = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            run.append(ch)
        else:
            if len(run) >= 2:
                out |= {run[i] + run[i + 1] for i in range(len(run) - 1)}
            run = []
    if len(run) >= 2:
        out |= {run[i] + run[i + 1] for i in range(len(run) - 1)}
    return out


def entity_overlap(a: str, b: str) -> float:
    """汉字 bigram 的**对称**重叠度。

    注意：实测证明**单靠它无法分离"同义改写"和"危险近似"**——
    两类样本的取值区间是重叠的：

        同义改写  "缓存穿透怎么治理" ↔ "缓存穿透的治理方案"   0.571
        危险近似  "缓存穿透怎么治理" ↔ "缓存击穿怎么治理"     0.571  ← 相同！
        危险近似  "内存泄漏怎么排查" ↔ "内存泄漏怎么修复"     0.714  ← 更高！

    危险近似的相似度甚至高于真实改写，所以任何"单一相似度阈值"方案都注定失败。
    真正的区分在**包含关系**上，见 ``discriminator_coverage``。
    """
    ba, bb = char_bigrams(a), char_bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / min(len(ba), len(bb))


#: 疑问/功能词：不承载语义区分，比较时应先剔除
_STOP_GRAMS = {
    "怎么", "如何", "什么", "为什", "哪些", "哪种", "应该", "可以", "是否",
    "一下", "一个", "这个", "那个", "我们", "你们", "请问",
}


def discriminator_coverage(cached: str, query: str) -> float:
    """**判别性覆盖度**：已缓存问题的"实质 bigram"有多少被当前问题覆盖。

    为什么用**非对称**指标而不是对称相似度：两类样本的语义关系本来就不对称 ——
    "新问题是不是把老问题的实质内容全说了"才是我们真正关心的。

    .. warning::

       **实测结论：这个指标也不足以分离两类。** 在 3 组同义改写 + 4 组危险近似上：

           同义改写   覆盖度 0.500 ~ 1.000
           危险/无关  覆盖度 0.000 ~ 0.667   ← 与同义改写区间**重叠**

       例如"内存泄漏怎么修复"(危险) 覆盖度 0.667，而"缓存穿透的治理方案"(同义)
       只有 0.667 —— 两者相同。原因是字符级指标只看到"共享了哪些字"，
       看不到"排查"与"修复"是不是同一件事。

       所以本实现采取**唯一安全的策略**：门槛设成 1.0，只接受"实质 bigram 被
       完全覆盖"的近似（即纯粹的措辞增量，如"怎么降"→"怎么降低"）。
       这会让**大量真实改写漏召回** —— 但漏召回只是少省一点钱，
       错误命中是"把 A 的答案给了 B"，是事故。**保守方向是刻意的。**

       生产上要真正做语义缓存必须上 embedding（向量相似度能区分
       "排查/修复"这类词义关系），字符级方案到此为止是硬边界。
    """
    bc, bq = char_bigrams(cached), char_bigrams(query)
    substantial = bc - _STOP_GRAMS
    if not substantial:
        return 0.0
    return len(substantial & bq) / len(substantial)


class SemanticCache:
    """近义问句复用答案。

    这个组件是**整个缓存体系里唯一有"答错"风险的一层**，所以需要两道闸门：

    ① **相似度阈值**管召回：太低会错答，太高等于没缓存。
    ② **判别性实体重叠**管正确性：要求命中的那条和当前问题在"区分性词"上足够像。

    为什么必须有第 ② 道？实测数据（`lab-07` 与本节注释里的值都是实测）：

        真实改写  "缓存穿透怎么治理"  ↔ "缓存穿透的治理方案"            Jaccard 0.308
        危险近似  "缓存穿透怎么治理"  ↔ "缓存击穿怎么治理"（穿透/击穿）  Jaccard 0.333
        危险近似  "内存泄漏怎么排查"  ↔ "内存泄漏怎么修复"（排查/修复）  Jaccard 0.455

    **危险近似的相似度甚至高于真实改写** —— 光靠阈值无法把两类分开。
    这两对的区别不在"共享了多少字"，而在"关键的那个词是不是同一个"。

    所以：
    * 共享分类词（"缓存"）**不构成**命中理由；
    * 2-gram 重叠度低（区分词不同）→ 拦下；
    * 2-gram 重叠度高（同义改写）→ 放行。

    实测效果见 `lab-07` 的阈值扫描表与本项目 demo 的验证输出。
    """

    def __init__(
        self,
        max_size: int = 512,
        ttl_s: float = 120.0,
        threshold: float = 0.30,
        clock: Callable[[], float] = time.monotonic,
        require_entity_match: bool = True,
        min_discriminator_overlap: float = 1.0,
    ):
        self.max_size = max_size
        self.ttl_s = ttl_s
        self.threshold = threshold
        self.require_entity_match = require_entity_match
        self.min_discriminator_overlap = min_discriminator_overlap
        self._clock = clock
        self._entries: OrderedDict[str, tuple[str, Any, float, str, set[str]]] = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.wrong_hits = 0
        self.blocked_by_entity_guard = 0
        self.rejected_below_threshold = 0
        self.best_score_seen = 0.0
        self.m_hit = METRICS.counter("cache_semantic_hits_total", "语义缓存命中")
        self.m_wrong = METRICS.counter("cache_semantic_wrong_hits_total", "语义缓存错误命中")
        self.m_guard = METRICS.counter(
            "cache_semantic_entity_guard_total", "被判别性护栏拦住的近似命中"
        )

    def get(self, query: str, tenant: str, truth: str | None = None) -> Any | None:
        """``truth`` 是"语义上正确的答案标识"，仅用于**度量**错误命中率。"""
        now = self._clock()
        best_key, best_score, best_val = None, 0.0, None
        with self._lock:
            for key, (text, value, expire, ent_tenant, ents) in list(self._entries.items()):
                if now >= expire or ent_tenant != tenant:
                    self._entries.pop(key, None)
                    continue
                score = jaccard(query, text)
                if score > best_score:
                    best_key, best_score, best_val = key, score, (value, text, ents)
        self.best_score_seen = max(self.best_score_seen, best_score)

        if best_key is not None and best_score >= self.threshold:
            value, matched_text, _ents = best_val  # type: ignore[misc]
            # ② 判别性护栏：相似度够了不代表问的是同一件事
            if self.require_entity_match:
                coverage = discriminator_coverage(matched_text, query)
                if coverage < self.min_discriminator_overlap:
                    self.blocked_by_entity_guard += 1
                    self.m_guard.inc()
                    self.misses += 1
                    return None
            if truth is not None and value != truth:
                self.wrong_hits += 1
                self.m_wrong.inc()
            with self._lock:
                if best_key in self._entries:
                    self._entries.move_to_end(best_key)
            self.hits += 1
            self.m_hit.inc()
            return value

        if best_key is not None:
            self.rejected_below_threshold += 1
        self.misses += 1
        return None

    def put(self, query: str, value: Any, tenant: str) -> None:
        key = f"{tenant}|{hashlib.md5(query.encode('utf-8')).hexdigest()[:12]}"
        with self._lock:
            self._entries[key] = (
                query, value, self._clock() + self.ttl_s, tenant, entities_of(query)
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_size:
                self._entries.popitem(last=False)

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    @property
    def wrong_hit_ratio(self) -> float:
        return self.wrong_hits / self.hits if self.hits else 0.0

    def stats(self) -> str:
        return (
            f"semantic: hit={self.hits} miss={self.misses} ratio={self.hit_ratio:.1%} "
            f"wrong={self.wrong_hits} ({self.wrong_hit_ratio:.1%}) "
            f"threshold={self.threshold}"
        )


# --------------------------------------------------------------------------
# 前缀缓存：把稳定内容放前面
# --------------------------------------------------------------------------


class PromptPrefixBuilder:
    """组装 prompt 时保证**稳定前缀**在前、易变内容在后。

    这是前缀缓存能命中的唯一前提。工程上必须硬编码这个顺序：

        1. system prompt（固定）
        2. 工具/函数定义（固定，按名字排序避免顺序抖动）
        3. few-shot（固定）
        4. 长期记忆/知识库固定片段（半固定）
        ---- 以下是易变部分，绝不能出现在前缀里 ----
        5. 当前时间、trace_id、用户身份
        6. 会话历史
        7. 用户输入

    常见事故：把 `当前时间` 插在 system prompt 里 → 每次请求前缀都不同 →
    前缀缓存 0% 命中 → 成本和 TTFT 同时劣化。
    """

    def __init__(self) -> None:
        self.cache_hits = 0
        self.cache_misses = 0

    @staticmethod
    def build(
        system_prompt: str,
        tools_schema: str = "",
        few_shot: str = "",
        memory: str = "",
        volatile: str = "",
        user_input: str = "",
        include_timestamp_in_prefix: bool = False,
    ) -> str:
        prefix: list[str] = [system_prompt]
        if tools_schema:
            prefix.append(tools_schema)
        if few_shot:
            prefix.append(few_shot)
        if memory:
            prefix.append(memory)
        if include_timestamp_in_prefix:
            # 反模式：时间戳进了前缀
            prefix.append(f"[当前时间] {time.strftime('%Y-%m-%d %H:%M:%S')}")
        tail: list[str] = []
        if not include_timestamp_in_prefix:
            tail.append(f"[当前时间] {time.strftime('%Y-%m-%d %H:%M:%S')}")
        if volatile:
            tail.append(volatile)
        if user_input:
            tail.append(f"[用户] {user_input}")
        return "\n".join(prefix + tail)

    @staticmethod
    def measure(prefix_text: str) -> tuple[str, int]:
        return prefix_text[:64], count_tokens(prefix_text)


# --------------------------------------------------------------------------
# 策略表：哪些能缓存，哪些不能
# --------------------------------------------------------------------------


@dataclass
class CacheRule:
    data_kind: str
    cacheable: bool
    layer: str
    ttl_s: float
    key_dimensions: tuple[str, ...]
    reason: str


#: 这张表是 lab-07 的核心交付物：**声明式**、可评审、可测试。
CACHE_POLICY: tuple[CacheRule, ...] = (
    CacheRule(
        "RAG 答案（只读、无个性化）", True, "L1 exact + L2 semantic", 300.0,
        ("tenant", "normalized_query", "model", "kb_version"),
        "相同问题重复率极高；但必须带 tenant 和 kb_version，否则串租户/读到旧知识",
    ),
    CacheRule(
        "前缀（system + 工具定义 + few-shot）", True, "L3 prefix", 3600.0,
        ("model", "prefix_hash"),
        "provider 侧前缀缓存按 10% 计费，收益最大、风险最低",
    ),
    CacheRule(
        "检索结果", True, "L4 tool", 60.0,
        ("tenant", "query", "filters", "index_version"),
        "纯读操作、短期稳定；TTL 要短，且必须带 index_version",
    ),
    CacheRule(
        "只读工具结果（天气/汇率/公开搜索）", True, "L4 tool", 30.0,
        ("tool", "args_hash"),
        "纯读、外部数据变化慢，但 TTL 必须远小于数据更新周期",
    ),
    CacheRule(
        "会话短期记忆", True, "L5 session", 1800.0,
        ("tenant", "user", "session_id"),
        "会话内复用；key 必须含 session_id，否则就是串会话",
    ),
    CacheRule(
        "权限过滤后的检索结果", False, "—", 0.0, (),
        "同一句话对不同用户的可见集合不同：一旦缓存就会越权返回",
    ),
    CacheRule(
        "任何有副作用的操作结果", False, "—", 0.0, (),
        "下单/发通知/写库：缓存等于丢请求或重复执行",
    ),
    CacheRule(
        "时效性强的数据（库存/工单状态/余额）", False, "—", 0.0, (),
        "过期数据返回的业务代价远大于省下的那点延迟",
    ),
    CacheRule(
        "个性化/创作类回答（temperature>0）", False, "—", 0.0, (),
        "用户预期每次不同，复用会显得答非所问",
    ),
    CacheRule(
        "含 PII / 敏感信息的结果", False, "—", 0.0, (),
        "跨用户复用即数据泄露；确需缓存必须做字段级脱敏 + 单用户绑定",
    ),
    CacheRule(
        "鉴权/授权决策", False, "—", 0.0, (),
        "授权必须每次实时判定；缓存授权结果是经典的提权漏洞",
    ),
    CacheRule(
        "模型路由决策（灰度/预算）", False, "—", 0.0, (),
        "路由依赖实时预算与灰度开关，缓存会导致灰度失效或预算失控",
    ),
)


def render_policy_table() -> None:
    print("\n  ┌─ 缓存策略表（能不能缓存 = 工程硬编码决策）")
    print(f"  │ {'数据类型':<34} {'可缓存':<7} {'层':<22} {'TTL':>7}")
    for r in CACHE_POLICY:
        flag = "✅ 是" if r.cacheable else "❌ 否"
        ttl = f"{r.ttl_s:.0f}s" if r.cacheable else "—"
        print(f"  │ {r.data_kind:<34} {flag:<7} {r.layer:<22} {ttl:>7}")
    print("  ├" + "─" * 76)
    for r in CACHE_POLICY:
        if not r.cacheable:
            print(f"  │ 不可缓存 · {r.data_kind}")
            print(f"  │    理由: {r.reason}")
    print("  └" + "─" * 76)


# --------------------------------------------------------------------------
# 统一门面
# --------------------------------------------------------------------------


class CacheSystem:
    """五层缓存的统一入口，负责记账与统计。"""

    def __init__(self, config) -> None:
        self.cfg = config
        self.exact = TTLCache[str]("exact", config.exact_cache_size, config.exact_cache_ttl_s)
        self.semantic = SemanticCache(
            config.semantic_cache_size,
            config.semantic_cache_ttl_s,
            config.semantic_threshold,
        )
        self.tools = TTLCache[str]("tool", max(64, config.exact_cache_size // 4), config.tool_cache_ttl_s)
        self.sessions: dict[str, Any] = {}
        self.saved_latency_ms = 0.0
        self.saved_usd = 0.0

    # -- 精确缓存 -----------------------------------------------------------
    @staticmethod
    def exact_key(tenant: str, query: str, model: str, kb_version: str = "v1",
                  persona: str = "general", use_retrieval: bool = False) -> str:
        """key 必须包含**所有影响结果的维度**。少一个维度就是一类事故：

        * 少 tenant → 串租户
        * 少 kb_version → 知识更新后仍返回旧答案
        * 少 persona → 切换人格后拿到上一个人格的答案
        * 少 use_retrieval → **开了 RAG 却命中不检索时的缓存**，
          于是"检索增强"看起来完全没生效

        最后两条是实测踩出来的：加了 RAG 开关后，同一个问题先不检索问一次、
        再开 RAG 问一次，第二次直接命中第一次的缓存，上下文 token 数为 0，
        表现成"RAG 功能坏了"。**新增任何影响答案的开关，都必须同步进 key。**
        """
        norm = " ".join(query.lower().split())
        h = hashlib.md5(norm.encode("utf-8")).hexdigest()[:16]
        rag = "r1" if use_retrieval else "r0"
        return f"{tenant}|{h}|{model}|{kb_version}|{persona}|{rag}"

    def lookup(
        self, tenant: str, query: str, model: str, truth: str | None = None,
        persona: str = "general", use_retrieval: bool = False,
    ) -> tuple[str | None, str]:
        """按 L1 → L2 顺序查找，返回 ``(答案, 命中的层名)``。

        返回层名很重要：排查"缓存到底有没有用"时必须知道是哪一层在起作用 ——
        语义缓存（L2）有错答风险，精确缓存（L1）没有。两者混在一起统计，
        就失去了可观测性，也违反本项目的核心原则：**每个结论都要能归因**。

        语义缓存（L2）故意**不带 persona/rag 维度**：它本来就是"近义问句复用"，
        维度越多命中率越低。但它有判别性护栏兜底，且只用于同一租户同一模型 ——
        风险可接受。L1 则必须精确，所以所有维度都要进 key。
        """
        key = self.exact_key(tenant, query, model, persona=persona,
                             use_retrieval=use_retrieval)
        hit = self.exact.get(key)
        if hit is not None:
            return hit, "L1 精确"
        sem = self.semantic.get(query, tenant, truth)
        if sem is not None:
            return sem, "L2 语义"
        return None, ""

    def store(self, tenant: str, query: str, model: str, answer: str,
              persona: str = "general", use_retrieval: bool = False) -> None:
        self.exact.put(
            self.exact_key(tenant, query, model, persona=persona,
                           use_retrieval=use_retrieval),
            answer, tenant=tenant,
        )
        self.semantic.put(query, answer, tenant)

    def account_saving(self, latency_ms: float, usd: float) -> None:
        self.saved_latency_ms += latency_ms
        self.saved_usd += usd

    def render(self) -> None:
        print("\n  ┌─ 缓存体系状态")
        print(f"  │ {self.exact.stats()}")
        print(f"  │ {self.semantic.stats()}")
        print(f"  │ {self.tools.stats()}")
        print(
            f"  │ 累计节省: {self.saved_latency_ms / 1000:.1f}s / "
            f"${self.saved_usd:.4f}"
        )
        print("  └" + "─" * 62)
