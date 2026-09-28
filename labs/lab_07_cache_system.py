"""Lab: 生产环境完整 Agent 缓存体系 —— 哪些能缓存，哪些绝对不能。

对应生产问题：「生产环境完整的 agent 缓存体系，哪些数据可以去做缓存，哪些不能做缓存？」

复现的故障（"缓存错东西"的五类事故）
    key 里没有 tenant → B 租户命中 A 租户的私有文档（越权命中）；缓存带副作用的操作 →
    真正下单被"缓存吃掉"（漏执行）；无幂等键重试 → 同一单下三次（重复执行）；缓存时效数据
    → 上游已变仍返回旧值（stale served）；缓存创作类回答 → 每次都是同一句；缓存 PII → 泄露。

生产上正确的做法
    分五层：L1 精确结果 / L2 语义 / L3 前缀 / L4 工具与检索 / L5 会话状态，每层的 key 维度、
    TTL、失效方式都显式声明（``POLICY`` 声明式策略表）。key 的维度 = 所有会影响答案的输入，
    少一个就是一次串味/串租户事故。语义缓存必须"阈值 + 实体护栏"两条腿走路。
    能不能缓存是工程硬编码的决策，绝不能交给 LLM 判断。

工程结论
    缓存省的是"重复计算"，不是"必须发生的计算"：对 P50/成本立竿见影，对 P95 的收益要等
    命中率足够高（或上游排队被打掉）才出现；key 的维度少一个就是一次事故。
"""

from __future__ import annotations

import hashlib
import itertools
import re
import sys
import threading
import time
from collections import OrderedDict, namedtuple

from agentlab.clock import VirtualClock
from agentlab.metrics import METRICS
from agentlab.providers import LLMServer, system, user
from agentlab.tokens import MID, count_tokens
from agentlab.util import (BROKEN, FIX, VERIFY, Stats, head, improvement, kv, lab,
                           note, phase, rng, run_concurrently, takeaway)

LAB_ID = "lab-07-cache-system"
MODEL, PARAMS, KB_VERSION, TTL_S = MID.name, "temp=0|tools=on|top_p=1", "kb-3", 900.0
C_SEM_HIT = METRICS.counter("cache_semantic_hits_total", "L2 语义缓存命中")
C_SEM_WRONG = METRICS.counter("cache_wrong_hits_total", "L2 错误命中（假命中）")
C_STALE = METRICS.counter("cache_stale_served_total", "返回过期数据次数")
C_UNAUTH = METRICS.counter("cache_unauthorized_hits_total", "越权命中次数")
C_DUP = METRICS.counter("cache_duplicate_side_effects_total", "副作用重复执行次数")
C_L1_HIT = METRICS.counter("cache_l1_hits_total", "L1 精确缓存命中")
C_L1_MISS = METRICS.counter("cache_l1_misses_total", "L1 精确缓存未命中")
C_EVICT = METRICS.counter("cache_evictions_total", "LRU 驱逐次数")
H_LOOKUP = METRICS.histogram("cache_lookup_ms", "缓存查询耗时")

# --- 0. 声明式缓存策略表：能不能缓存是工程硬编码的决策，不交给 LLM 判断 ---------

#: (层, 数据, 是否可缓存, TTL/失效, key 必须包含的维度, 判定理由)
CacheRule = namedtuple("CacheRule", "layer data ok ttl key_dims why")

POLICY: tuple[CacheRule, ...] = (
    CacheRule("L1", "FAQ / 固定文档问答最终答案", True, "1h", "tenant+model+params+kb_version+norm(prompt)", "少一个维度就串味"),
    CacheRule("L1", "检索+排序后的候选文档", True, "5m", "tenant+groups+query+kb_version", "权限维度不进 key = 越权"),
    CacheRule("L2", "语义近邻答案", True, "15m", "tenant+intent+阈值+实体护栏", "阈值换召回，护栏防错答"),
    CacheRule("L3", "system+工具定义+few-shot 前缀", True, "provider 侧", "前缀字节", "稳定内容放前，易变内容放后"),
    CacheRule("L4", "只读工具结果（汇率/文档/字典）", True, "60s", "tenant+tool+params+上游版本", "TTL <= 上游刷新周期"),
    CacheRule("L4", "写操作（下单/发邮件/转账）", False, "-", "幂等键（不是 prompt）", "副作用不可重放"),
    CacheRule("L5", "会话短期记忆 / 上下文", True, "会话期", "session_id", "会话级隔离，禁止跨会话复用"),
    CacheRule("L5", "用户长期画像 / 偏好", True, "1h", "tenant+user_id", "按用户隔离，注销要失效"),
    CacheRule("-", "权限 / ACL 判定结论", False, "-", "-", "撤权后仍放行 = 越权窗口"),
    CacheRule("-", "安全审核 / 内容合规结论", True, "30s", "content_hash+rule_version", "规则变更必须主动失效"),
    CacheRule("-", "时效数据（股价/库存/工单状态）", False, "-", "-", "缓存即错，只能直读或极短 TTL"),
    CacheRule("-", "temperature>0 的创作类回答", False, "-", "-", "随机性本身就是产品需求"),
    CacheRule("-", "含 PII / 敏感信息的结果", False, "-", "-", "跨用户复用即泄露"),
    CacheRule("-", "计费 / 配额 / 余额", False, "-", "-", "钱相关必须强一致"),
    CacheRule("-", "模型路由决策（按 query 特征）", True, "5m", "query_shape+策略版本", "决策可缓存，但要能一键失效"),
)

# --- 1. key 规范化 + L1 精确缓存（TTL + LRU + 租户维度） ----------------------

_TS = re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?Z?)?")
_NOISE = re.compile(r"\b(?:sess|req|trace|span|sid)[-_]?[0-9a-z]{2,}\b", re.I)


def normalize(text: str) -> str:
    """去掉"无意义变量"：空白、大小写、时间戳、会话/请求 ID。"""
    return " ".join(_NOISE.sub("<id>", _TS.sub("<ts>", text)).split()).lower()


def cache_key(prompt: str, model: str, params: str, tenant: str, tenant_scoped: bool = True) -> str:
    dims = [normalize(prompt), model, params, KB_VERSION]
    if tenant_scoped:
        dims.insert(0, tenant)
    return hashlib.sha256("|".join(dims).encode("utf-8")).hexdigest()[:16]


class ExactCache:
    def __init__(self, maxsize: int = 256, ttl_s: float = TTL_S, tenant_scoped: bool = True):
        self.maxsize, self.ttl_s, self.tenant_scoped = maxsize, ttl_s, tenant_scoped
        self._d: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = self.evictions = 0

    def get(self, key: str, now: float) -> str | None:
        t0 = time.perf_counter()
        with self._lock:
            item = self._d.get(key)
            if item is None or item[0] <= now:  # 不存在或过期 = miss（TTL 是硬闸门）
                self._d.pop(key, None)
                self.misses += 1
            else:
                self._d.move_to_end(key)
                self.hits += 1
                H_LOOKUP.observe((time.perf_counter() - t0) * 1000.0)
                return item[1]
        H_LOOKUP.observe((time.perf_counter() - t0) * 1000.0)
        return None

    def put(self, key: str, value: str, now: float) -> None:
        with self._lock:
            self._d[key] = (now + self.ttl_s, value)
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)
                self.evictions += 1
                C_EVICT.inc()

    def key_of(self, prompt: str, tenant: str) -> str:
        return cache_key(prompt, MODEL, PARAMS, tenant, self.tenant_scoped)

# --- 2. L2 语义缓存：词袋 + 字符 bigram 相似度 + 阈值 + 实体护栏 --------------
#
# ⚠ 一个必须说清的硬边界（本项目 capstone 的独立复测结论，与此处一致）：
#
#   字符级相似度**无法分离**"同义改写"和"危险近似"—— 两类样本的取值区间是重叠的。
#   实测三组：
#
#       同义改写  "缓存穿透怎么治理" ↔ "缓存穿透的治理方案"   Jaccard 0.308 / 覆盖度 0.667
#       危险近似  "缓存穿透怎么治理" ↔ "缓存击穿怎么治理"     Jaccard 0.333 / 覆盖度 0.500
#       危险近似  "内存泄漏怎么排查" ↔ "内存泄漏怎么修复"     Jaccard 0.455 / 覆盖度 0.667
#
#   注意第二行：**危险近似的相似度(0.333)高于真实改写(0.308)**；第三行的覆盖度
#   又与第一行相同。试过对称 bigram 重叠、判别性覆盖度（非对称包含）两种指标，
#   都做不到分离（间隔分别为 -0.143 / -0.167，即区间重叠）。
#
#   所以字符级方案的正确姿势是**保守**：抬高门槛，只接受"实质内容被完全覆盖"的
#   近似（如"怎么降"→"怎么降低"），接受改写命中率低。理由很直接：
#
#       漏召回 = 少省一点钱（可接受）
#       错误命中 = 把 A 的答案给了 B（事故）
#
#   真正要做好语义缓存必须上 embedding —— 向量空间才能表达"排查≈修复"这种词义关系。
#   这是字符级方法的硬边界，不是参数没调好。

INTENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("退款到账", ("退款多久到账", "退款多久能到账", "退款一般多久到账", "请问退款多久到账", "退款到账需要多久", "退款多久可以到账")),
    ("发票申请", ("怎么开发票", "怎么开发票呢", "请问怎么开发票", "怎么开电子发票", "发票怎么开", "开发票的流程是什么")),
    ("缓存穿透", ("缓存穿透怎么解决", "缓存穿透怎么解决呢", "请问缓存穿透怎么解决", "缓存穿透的解决方案", "怎么防止缓存穿透", "缓存穿透如何处理")),
    ("缓存击穿", ("缓存击穿怎么解决", "缓存击穿怎么解决呢", "请问缓存击穿怎么解决", "缓存击穿的解决方案", "怎么防止缓存击穿", "缓存击穿如何处理")),
    ("限流配置", ("限流阈值怎么配置", "限流阈值怎么配置呢", "请问限流阈值怎么配置", "限流的阈值怎么设置", "如何设置限流阈值", "限流阈值设多少合适")),
    ("熔断配置", ("熔断阈值怎么配置", "熔断阈值怎么配置呢", "请问熔断阈值怎么配置", "熔断的阈值怎么设置", "如何设置熔断阈值", "熔断阈值设多少合适")),
    ("重置密码", ("怎么重置密码", "怎么重置密码呢", "请问怎么重置密码", "密码怎么重置", "如何重置登录密码", "重置密码的流程")),
    ("注销账号", ("怎么注销账号", "怎么注销账号呢", "请问怎么注销账号", "账号怎么注销", "如何注销我的账号", "注销账号的流程")),
)
DOMAIN_TERMS = ("缓存穿透", "缓存击穿", "退款", "到账", "发票", "开票", "限流", "熔断",
                "阈值", "密码", "账号", "注销", "重置")
_CJK_STOP = ("怎么", "什么", "如何", "请问", "一下", "可以", "这个", "那个", "多久")


def grams(text: str) -> frozenset[str]:
    """词袋 + 字符 bigram（不依赖 embedding 模型的极简语义近似）。"""
    t = normalize(text)
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", t))
    toks = set(re.findall(r"[a-z0-9]+", t)) | {cjk[i:i + 2] for i in range(max(0, len(cjk) - 1))}
    return frozenset(w for w in toks if w not in _CJK_STOP)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def guard_ok(q: str, cached_q: str) -> bool:
    """实体护栏：两个 query 的领域实体必须"可比"（互为子集），否则拒绝复用。"""
    a = frozenset(t for t in DOMAIN_TERMS if t in q)
    b = frozenset(t for t in DOMAIN_TERMS if t in cached_q)
    return a <= b or b <= a


def similarity_diagnosis() -> tuple[Stats, list[tuple[float, str, str]]]:
    """量化证据：同义改写的相似度分布 vs 跨意图"易混对"的相似度。"""
    same = [jaccard(grams(qs[0]), grams(q)) for _, qs in INTENTS for q in qs[1:]]
    cross = sorted(((max(jaccard(grams(a), grams(b)) for a in q1 for b in q2), i1, i2)
                    for (i1, q1), (i2, q2) in itertools.combinations(INTENTS, 2)), reverse=True)
    return Stats(same), cross[:2]


def _build_stream(seed: int = 11) -> list[tuple[str, str]]:
    """每个意图的第一个问法先"播种"（缓存已热身），其余问法打乱后到达。"""
    rest = [(it, q) for it, qs in INTENTS for q in qs[1:]]
    rng(seed).shuffle(rest)
    return [(it, qs[0]) for it, qs in INTENTS] + rest


STREAM = _build_stream()


def simulate_semantic(threshold: float, guard: bool = False, record: bool = False) -> dict:
    """流式模拟：先到的问法播种缓存，后来的问法按阈值（+可选护栏）尝试命中。"""
    cache: list[tuple[str, str]] = []
    hits = wrong = 0
    for intent, q in STREAM:
        g = grams(q)
        best, best_sim = None, 0.0
        for ci, ctext in cache:
            s = jaccard(g, grams(ctext))
            if s > best_sim:
                best, best_sim = (ci, ctext), s
        if best is not None and best_sim >= threshold and (not guard or guard_ok(q, best[1])):
            same_intent = best[0] == intent
            hits, wrong = hits + same_intent, wrong + (not same_intent)
            if record:
                (C_SEM_HIT if same_intent else C_SEM_WRONG).inc()
        else:
            cache.append((intent, q))
    n = len(STREAM)
    return {"n": n, "hits": hits, "wrong": wrong, "hit_rate": hits / n,
            "wrong_of_hits": wrong / max(1, hits + wrong), "cache_size": len(cache)}

# --- 3. L3 前缀缓存 / L4 工具缓存 / L5 会话缓存 -------------------------------

STABLE_PREFIX = "你是企业知识助手，严格遵守以下规则：\n" + ("规则：先查权限再检索，引用必须带出处。\n" * 60)


def l3_scenario(ts_in_system: bool, n: int = 16, seed: int = 5) -> dict:
    """同一个时间戳：放进 system（前缀每请求都变）vs 放到最后（前缀稳定）。"""
    srv = LLMServer(seed=seed)
    srv.set_latency(MODEL, 45, 0.3)  # 压缩墙钟，缓存语义与计费规则不变
    if not ts_in_system:
        srv.warm_prefix(MODEL, LLMServer.prefix_key([system(STABLE_PREFIX)]), count_tokens(STABLE_PREFIX))

    def one(i: int) -> float:
        ts = f"当前时间: 2025-03-0{i % 9 + 1}T08:0{i % 9}:00Z"
        msgs = ([system(STABLE_PREFIX + "\n" + ts), user(f"问题{i}")] if ts_in_system
                else [system(STABLE_PREFIX), user(f"{ts}\n问题{i}")])
        t0 = time.perf_counter()
        srv.call(msgs, model=MODEL, timeout=3.0, tag="l3")
        return (time.perf_counter() - t0) * 1000.0

    st = Stats([x for x in run_concurrently(one, n, 8) if isinstance(x, float)])
    return {"p50": st.p50, "p95": st.p95, "usd": srv.ledger.usd,
            "hit_rate": srv.stats["cache_hits"] / max(1, srv.stats["requests"]),
            "cached_tokens": srv.ledger.cached_tokens, "in_tokens": srv.ledger.in_tokens}


def demo_l4_tool_cache() -> dict:
    """L4：只读工具结果缓存 —— "外部数据变了但缓存没失效" vs 版本号主动失效。"""
    clk, src = VirtualClock(), {"version": 1, "price": 100.0}

    def run(key_has_version: bool) -> dict:
        cache: dict[tuple, tuple[float, dict]] = {}
        stale = miss = 0
        for step in range(20):
            if step in (6, 13):  # 外部世界变了：调价两次
                src["version"], src["price"] = ((2, 138.0) if step == 6 else (3, 92.0))
            key = ("quote", "sku-1", src["version"]) if key_has_version else ("quote", "sku-1")
            hit = cache.get(key)
            if hit is not None and clk.monotonic() < hit[0]:  # TTL 未到 → 直接返回
                val = hit[1]
            else:
                val = {"price": src["price"], "v": src["version"]}  # 回源
                cache[key] = (clk.monotonic() + 300.0, val)
                miss += 1
            if val["v"] != src["version"]:
                stale += 1
                C_STALE.inc()
            clk.advance(20.0)
        return {"stale": stale, "miss": miss, "entries": len(cache)}

    return {"bad": run(False), "good": run(True), "served": 20}


def demo_never_cache() -> dict:
    """五类绝对不可缓存的数据，各自跑出一个事故数字。"""
    q, private = "我的报销制度是什么", "tenant-a 的私有文档：《A 公司 2025 报销制度》"
    now = time.monotonic()
    bad = ExactCache(tenant_scoped=False)  # 坏 key：只有 prompt
    bad.put(cache_key(q, MODEL, PARAMS, "tenant-a", False), private, now)
    unauthorized = sum(1 for _ in range(3)
                       if bad.get(cache_key(q, MODEL, PARAMS, "tenant-b", False), now) == private)
    good = ExactCache(tenant_scoped=True)  # 好 key：带 tenant
    good.put(cache_key(q, MODEL, PARAMS, "tenant-a"), private, now)
    unauthorized_fixed = sum(1 for _ in range(3)
                             if good.get(cache_key(q, MODEL, PARAMS, "tenant-b"), now) == private)
    C_UNAUTH.inc(unauthorized)
    cache = ExactCache()
    k = cache.key_of("帮我下单 商品X 数量1", "tenant-a")
    cache.put(k, "已下单成功", now)
    orders: list[str] = []  # 真实副作用（下单）的记录
    missed = sum(1 for _ in range(3) if cache.get(k, now) is not None) - len(orders)  # 漏执行
    dup = [f"order-{i}" for i in range(3)]  # 没有幂等键：3 次重试 = 真的下了 3 单
    C_DUP.inc(len(dup) - 1)
    r, creative, seen = rng(3), ["讲个笑话", "给我一个创意 slogan", "写首打油诗"], {}
    for _ in range(2):
        for c in creative:
            seen.setdefault(c, f"随机创作#{r.randrange(10 ** 6)}")
    pii = {"我的手机号是多少": "user-A 的手机号 138****8888"}
    return {"unauthorized": unauthorized, "unauthorized_fixed": unauthorized_fixed,
            "missed": missed, "dup": len(dup) - 1, "repeated_creative": 6 - len(seen),
            "pii_leaked": sum(1 for u in ("user-B", "user-C") if pii.get("我的手机号是多少"))}

# --- 4. L1 全链路对账：150 次请求，无缓存 vs 有缓存 ---------------------------

BASE_Q = ["退款多久到账", "怎么开发票", "缓存穿透怎么解决", "缓存击穿怎么解决",
          "限流阈值怎么配", "熔断阈值怎么配", "怎么重置密码", "账号怎么注销"]
UNIQUE = [(t, q) for t in ("tenant-a", "tenant-b", "tenant-c") for q in BASE_Q]
WORKLOAD_N = 150


def run_workload(use_cache: bool, seed: int = 7, workers: int = 16) -> dict:
    srv = LLMServer(max_queue=64, seed=seed)
    srv.set_latency(MODEL, 60, 0.35)  # 压缩墙钟，计费规则不变
    cache = ExactCache(maxsize=64) if use_cache else None
    rr = rng(seed)
    picks = [rr.randrange(len(UNIQUE)) for _ in range(WORKLOAD_N)]
    now = time.monotonic()

    def one(i: int) -> dict:
        tenant, q = UNIQUE[picks[i]]
        # 每次请求都带"无意义变量"：会话 ID + 时间戳（key 规范化必须吃掉它们）
        prompt = f"  {q} \n(会话 req-{i:03d} 时间 2025-03-0{i % 9 + 1}T08:00:00Z)"
        t0 = time.perf_counter()
        if cache is not None:
            if cache.get(cache.key_of(prompt, tenant), now) is not None:
                C_L1_HIT.inc()
                return {"ms": (time.perf_counter() - t0) * 1000.0, "hit": True}
            C_L1_MISS.inc()
        try:
            reply = srv.call([system("你是企业知识助手"), user(prompt)],
                             model=MODEL, timeout=5.0, tenant=tenant, tag="l1")
            if cache is not None:
                cache.put(cache.key_of(prompt, tenant), reply.text, now)
        except BaseException:  # noqa: BLE001 - 上游抖动按 miss 计
            pass
        return {"ms": (time.perf_counter() - t0) * 1000.0, "hit": False}

    rows = [r for r in run_concurrently(one, WORKLOAD_N, workers) if isinstance(r, dict)]
    return {"stats": Stats([r["ms"] for r in rows]),
            "hit_rate": sum(1 for r in rows if r["hit"]) / max(1, len(rows)),
            "usd": srv.ledger.usd, "calls": srv.ledger.calls,
            "evictions": cache.evictions if cache else 0, "provider": srv.summary_lines()}


def pct(before: float, after: float, lower_is_better: bool = True) -> str:
    """变化率字符串；before 为 0 时给相对增幅（避免 n/a）。"""
    if before == 0:
        return f"+{after * 100:.1f}%" if after > 0 else "+0.0%"
    return improvement(before, after, lower_is_better=lower_is_better)


def main() -> int:
    with lab(LAB_ID, "生产环境完整 Agent 缓存体系：哪些能缓存，哪些绝对不能",
             "生产环境完整的 agent 缓存体系，哪些数据可以去做缓存，哪些不能做缓存？"):
        head("1. 复现故障：缓存错东西的五类事故")
        phase("1. 复现故障", "(越权 / 副作用 / 时效 / 随机性 / PII)")
        nc, tool = demo_never_cache(), demo_l4_tool_cache()
        kv("越权命中：key 不含 tenant / 含 tenant", f"{nc['unauthorized']} / {nc['unauthorized_fixed']}", " 次")
        print(f"\n{BROKEN} key 少一个 tenant 维度：越权命中 {nc['unauthorized']} 次，B 租户拿到了 A 租户的私有文档")
        kv("漏执行下单 / 无幂等键重试导致重复执行", f"{nc['missed']} / {nc['dup']}", " 次")
        print(f"{BROKEN} 副作用被缓存/被无脑重试：漏执行 {nc['missed']} 次 + 重复执行 {nc['dup']} 次")
        kv("TTL 缓存返回过期报价", tool["bad"]["stale"], f" / {tool['served']} 次")
        print(f"{BROKEN} 外部数据变了但缓存没失效：{tool['bad']['stale']} 次返回过期数据（真值 100 → 138）")
        kv("创作类被缓存重复回答 / PII 跨用户泄露", f"{nc['repeated_creative']} / {nc['pii_leaked']}", " 次")
        print(f"{BROKEN} 缓存创作类/PII：{nc['repeated_creative']} 次重复回答 + {nc['pii_leaked']} 次 PII 跨用户泄露")

        head("2. 观测 / 归因：声明式策略表 + 分层实测")
        phase("2. 观测 / 归因", "(声明式缓存策略表 = 工程交付物)")
        yes = sum(1 for r in POLICY if r.ok)
        note(f"共 {len(POLICY)} 条规则：{yes} 类可缓存 / {len(POLICY) - yes} 类绝对不可缓存")
        for r in POLICY:
            print(f"    [{r.layer}] {r.data:<30} {'可缓存' if r.ok else '★绝对不可缓存':<14} "
                  f"TTL={r.ttl:<11} key=({r.key_dims})\n          理由: {r.why}")
        a = cache_key(" 退款多久到账 \n", MODEL, PARAMS, "tenant-a")
        b = cache_key("退款多久到账", MODEL, PARAMS, "tenant-a")
        e1 = cache_key("退款多久到账 (会话 req-9f3a 时间 2025-03-07T08:00:00Z)", MODEL, PARAMS, "tenant-a")
        e2 = cache_key("退款多久到账 (会话 req-1b2c 时间 2025-03-08T19:31:07Z)", MODEL, PARAMS, "tenant-a")
        c, d = cache_key("退款多久到账", MODEL, PARAMS, "tenant-b"), cache_key("退款多久到账", MODEL, "temp=0.9", "tenant-a")
        kv("同问法，空白/大小写不同", f"{a[:8]}.. == {b[:8]}..", f"  -> 同 key? {a == b}（必须相同）")
        kv("同问法，时间戳/会话ID不同", f"{e1[:8]}.. == {e2[:8]}..", f"  -> 同 key? {e1 == e2}（必须相同）")
        kv("换租户 tenant-b / 换参数 temp=0.9", f"{c[:8]}.. / {d[:8]}..", f"  同 key? {c == b} / {d == b}")

        phase("2. 观测 / 归因", "(L2 语义缓存：命中率 vs 错误命中率，本 lab 最有价值的输出)")
        ss, cross = similarity_diagnosis()
        note(f"同义改写（相对播种问法）相似度: min={ss.mn:.2f} mean={ss.avg:.2f} p95={ss.p95:.2f} max={ss.mx:.2f}（n={ss.n}）")
        for s, i1, i2 in cross:
            note(f"跨意图「易混对」相似度: {i1} <-> {i2} = {s:.3f}  ← 字符几乎一样、语义不同")
        note(f"最像的跨意图对（{cross[0][0]:.3f}）高于同义改写的均值（{ss.avg:.2f}）：想靠抬阈值挡住错答，必然连正确命中一起砍掉。")
        print(f"    {'阈值':<6} {'命中':>5} {'命中率':>8} {'错命中':>7} {'错命中/命中':>11} | {'+实体护栏 命中':>14} {'错命中':>7}")
        for th in (0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60):
            raw, gd = simulate_semantic(th), simulate_semantic(th, guard=True)
            print(f"    {th:<6.2f} {raw['hits']:>5} {raw['hit_rate']:>7.1%} {raw['wrong']:>7} {raw['wrong_of_hits']:>10.1%} | {gd['hits']:>14} {gd['wrong']:>7}")
        NAIVE_TH, TUNED_TH = 0.50, 0.30
        naive = simulate_semantic(NAIVE_TH, record=True)
        tuned = simulate_semantic(TUNED_TH, guard=True, record=True)
        only_th = simulate_semantic(TUNED_TH)
        note(f"拍脑袋阈值 {NAIVE_TH}：命中 {naive['hits']} 次里错答 {naive['wrong']} 次（错命中率 {naive['wrong_of_hits']:.0%}）")
        note(f"阈值 {TUNED_TH} 调优（吃满召回，无护栏）：命中 {only_th['hits']} 次，错答 {only_th['wrong']} 次")
        note(f"阈值 {TUNED_TH} + 实体护栏（生产配置）：命中 {tuned['hits']} 次，错答 {tuned['wrong']} 次")

        phase("2. 观测 / 归因", "(L3 前缀缓存：稳定在前 vs 易变在前 / L4 失效 / L5 会话)")
        bad3, good3 = l3_scenario(True), l3_scenario(False)
        kv("时间戳放 system（前缀每请求都变）", f"命中率 {bad3['hit_rate']:.0%}", f"  p50={bad3['p50']:.0f}ms cost=${bad3['usd']:.6f}")
        kv("时间戳放最后（前缀稳定）", f"命中率 {good3['hit_rate']:.0%}", f"  p50={good3['p50']:.0f}ms cost=${good3['usd']:.6f}")
        kv("前缀命中省下的输入 token", f"{good3['cached_tokens']}", f" / {good3['in_tokens']}")
        print(f"\n{BROKEN} 同一个时间戳放进 system：前缀命中率 {good3['hit_rate']:.0%} 掉到 {bad3['hit_rate']:.0%}，成本 ${good3['usd']:.6f} 涨到 ${bad3['usd']:.6f}")
        kv("版本号进 key 后：过期返回 / 回源次数", f"{tool['good']['stale']} / {tool['good']['miss']}", " 次")
        turns = [f"第{i}轮对话：用户问题与助手回答的正文内容。" * 8 for i in range(6)]
        kv("每轮重放完整历史 → 会话缓存复用",
           f"{sum(count_tokens(t) + 4 for t in turns) * len(turns) // 2} -> {count_tokens(''.join(turns)) + 4 * len(turns)}", " tokens")
        METRICS.render("缓存指标快照（复现 + 观测累计）", include=["cache_"])

        head("3. 修复：租户隔离 + 阈值&护栏 + 前缀排布 + 版本失效 + 禁用清单")
        phase("3. 修复", "(FIX)")
        note("① key = tenant + model + params + kb_version + 规范化 prompt（一个维度都不能少）")
        note(f"② 语义缓存：阈值降到 {TUNED_TH} 吃满召回，再加实体护栏挡住跨意图假命中")
        note("③ 前缀排布：system/工具定义/few-shot 放最前，时间戳与会话历史放最后")
        note("④ 工具结果：key 带上游版本号 + TTL 双保险，变更事件主动失效")
        note("⑤ 副作用/PII/时效/权限结论：直接从缓存白名单剔除（见策略表 ★ 行）")
        print(f"\n{FIX} 越权命中 {nc['unauthorized']} → {nc['unauthorized_fixed']} 次；语义错命中 {naive['wrong']} → "
              f"{tuned['wrong']} 次（护栏在 {TUNED_TH} 阈值下保住 {tuned['hits']} 次正确命中）；工具脏数据 "
              f"{tool['bad']['stale']} → {tool['good']['stale']} 次")

        head("4. 验证：同一份负载，无缓存 vs 有缓存")
        phase("4. 验证", f"(n={WORKLOAD_N}, 模型={MODEL})")
        before, after = run_workload(False), run_workload(True)
        kv("请求数 / 上游调用次数", f"{WORKLOAD_N} / {before['calls']} -> {after['calls']}")
        kv("P50 延迟 / P95 延迟",
           f"{before['stats'].p50:.0f}->{after['stats'].p50:.0f}ms / "
           f"{before['stats'].p95:.0f}->{after['stats'].p95:.0f}ms")
        kv("缓存命中率", f"{before['hit_rate']:.1%} -> {after['hit_rate']:.1%}")
        kv("成本", f"${before['usd']:.6f} -> ${after['usd']:.6f}")
        kv("节省延迟(均值×请求数)", f"{(before['stats'].avg - after['stats'].avg) * WORKLOAD_N / 1000:.3f}",
           f" s；LRU 驱逐 {after['evictions']} 次")
        for line in after["provider"]:
            note(line)
        checks = [
            ("cache_hit_rate", before["hit_rate"], after["hit_rate"], "0.3f", False),
            ("p95_latency_ms", before["stats"].p95, after["stats"].p95, "0.1f", True),
            ("cost_usd", before["usd"], after["usd"], "0.6f", True),
            ("wrong_hits", float(naive["wrong"]), float(tuned["wrong"]), "0.0f", True),
            ("unauthorized_hits", float(nc["unauthorized"]), float(nc["unauthorized_fixed"]), "0.0f", True),
            ("stale_served", float(tool["bad"]["stale"]), float(tool["good"]["stale"]), "0.0f", True)]
        print()
        for name, b_, a_, fs, lower in checks:
            print(f"{VERIFY} {name}: {b_:{fs}} -> {a_:{fs}} ({pct(b_, a_, lower)})")

        head("4. 工程结论")
        note("1) 分层：L1 精确 / L2 语义 / L3 前缀 / L4 工具 / L5 会话，key 维度逐层不同。")
        note("2) key 的维度 = 所有影响答案的输入；tenant 缺失是最贵的一类 bug。")
        note("3) 语义缓存是「召回 ↔ 错答」的交换：两条曲线一起看，并且必须加实体护栏。")
        note("4) 前缀缓存零风险高收益，前提是稳定内容真的在前面（时间戳别进 system）。")
        note("5) 缓存对 P50/成本立竿见影；P95 的收益来自「打掉上游排队」，命中率不够就看不到。")
        note("6) 绝对的禁区：副作用、时效数据、PII、权限结论、随机创作、钱相关 —— 硬编码。")
        takeaway("缓存省的是重复计算，不是必须发生的计算；能不能缓存是工程硬编码的决策，"
                 "key 的维度少一个就是一次越权或串味事故。")
        METRICS.reset()
    return 0


QUESTIONS = [
    "生产环境完整的 agent 缓存体系分几层？ -> L1 精确结果 / L2 语义 / L3 前缀 / L4 工具与检索结果 / L5 会话状态（full agent cache taxonomy: exact / semantic / prefix / tool-result / session-state）",
    "哪些数据可以缓存？ -> FAQ 答案、检索候选、稳定前缀、只读工具结果、会话上下文（cacheable: stable, tenant-scoped, reproducible payloads）",
    "哪些数据绝对不能缓存？ -> 权限结论、副作用操作、时效数据（股价/库存/工单）、temperature>0 创作、PII、计费余额（never cache: authorization decisions, side-effecting operations, time-sensitive data, randomized/PII payloads）",
    "缓存 key 怎么设计才不串味/串租户？ -> 规范化 prompt + tenant + model + 参数 + 知识库版本，缺一维即事故（key dimensions must include every answer-affecting input）",
    "语义缓存阈值怎么定？ -> 扫出「命中率 vs 错误命中率」曲线，再用实体护栏把跨意图假命中压到 0（tune threshold for recall, then add an entity guard）",
    "缓存对 P95 有用吗？ -> 对 P50/成本立竿见影；P95 需要高命中率或打掉上游排队（cache helps p50/cost first; p95 only after load-related queueing disappears）"]

if __name__ == "__main__":
    sys.exit(main())
