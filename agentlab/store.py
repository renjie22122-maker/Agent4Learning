"""模拟知识库：线性扫描 / 倒排索引 / 两阶段检索 / 元数据过滤。

对应生产问题："大量检索的 RT 越来越高，百万级别的知识库如何性能优化"。

三个层次的实现刻意放在一起，方便对照：

1. ``NaiveRetriever`` —— 每查一次就全量扫一遍并算相似度。小库没问题，
   N 上去之后 RT 线性爆炸，而且每次查询都分配大量临时对象（GC 压力）。
2. ``BM25Index`` —— 倒排索引 + BM25，只扫命中的 posting list。
3. ``TwoStageRetriever`` —— 粗排（倒排/向量召回 top-k）→ 精排（rerank 一小批）。
   生产上"RT 降下来"主要靠这一层，而不是换更快的向量库。

另外提供 ``prefilter``：租户/权限过滤**必须在召回阶段做**，不能召回后再丢——
既慢又可能泄露。这是多租户隔离的关键工程点。
"""

from __future__ import annotations

import math
import random
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .metrics import METRICS

STOPWORDS = {"the", "a", "an", "of", "and", "or", "to", "in", "is", "for", "on", "with"}


@dataclass
class Doc:
    doc_id: str
    text: str
    tenant: str = "default"
    acl: frozenset[str] = field(default_factory=frozenset)
    ts: float = 0.0
    meta: dict = field(default_factory=dict)

    def keywords(self) -> list[str]:
        return [w for w in _tokenize(self.text) if w not in STOPWORDS]


@dataclass
class Hit:
    doc: Doc
    score: float
    stage: str = "recall"

    @property
    def text(self) -> str:
        return self.doc.text

    def __str__(self) -> str:
        return f"{self.doc.doc_id}({self.score:.3f})"


@dataclass
class Query:
    text: str
    top_k: int = 5
    tenant: str = "default"
    groups: frozenset[str] = field(default_factory=frozenset)
    min_score: float = 0.0
    rerank: bool = False


@dataclass
class RetrievalResult:
    hits: list[Hit]
    candidates: int = 0  # 进入了打分的候选数（粗排规模）
    scanned: int = 0  # 实际遍历的文档数
    latency_ms: float = 0.0
    stage_ms: dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 语料生成
# --------------------------------------------------------------------------

_TOPICS = [
    "缓存穿透", "熔断降级", "限流算法", "内存泄漏", "协程调度", "连接池",
    "索引重建", "灰度发布", "会话隔离", "权限模型", "任务队列", "背压控制",
]
_VERBS = ["优化", "排查", "治理", "设计", "压测", "重构", "监控", "拆分"]


def build_corpus(
    n: int,
    seed: int = 7,
    tenants: Sequence[str] = ("tenant-a", "tenant-b", "tenant-c"),
    vocab_size: int = 512,
) -> list[Doc]:
    """生成 ``n`` 篇文档。用紧凑短句，保证百万级也跑得起。"""
    r = random.Random(seed)
    vocab = [f"t{i}" for i in range(vocab_size)]
    docs: list[Doc] = []
    for i in range(n):
        topic = _TOPICS[i % len(_TOPICS)]
        verb = _VERBS[(i // len(_TOPICS)) % len(_VERBS)]
        words = [topic, verb, vocab[r.randrange(vocab_size)], vocab[r.randrange(vocab_size)]]
        text = f"doc{i} {' '.join(words)}"
        tenant = tenants[i % len(tenants)]
        groups = frozenset({f"g{tenant[-1]}", "public"} if i % 5 else {"secret"})
        docs.append(Doc(f"d{i}", text, tenant, groups, float(i), {"topic": topic}))
    return docs


def _tokenize(text: str) -> list[str]:
    """中英混排的粗糙分词：英文按空白，中文按 2-gram。"""
    out: list[str] = []
    buf: list[str] = []
    cjk_run: list[str] = []

    def flush_buf() -> None:
        if buf:
            out.extend("".join(buf).lower().split())
            buf.clear()

    def flush_cjk() -> None:
        if cjk_run:
            s = "".join(cjk_run)
            out.extend(s[i : i + 2] for i in range(max(1, len(s) - 1)))
            cjk_run.clear()

    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            flush_buf()
            cjk_run.append(ch)
        else:
            flush_cjk()
            buf.append(ch)
    flush_buf()
    flush_cjk()
    return out


# --------------------------------------------------------------------------
# 1) 朴素线性检索（反面教材）
# --------------------------------------------------------------------------


class NaiveRetriever:
    """全量扫描 + 每次查询现算打分。N 大时 RT 线性增长，临时对象爆炸。"""

    name = "naive-linear"

    def __init__(self, docs: Sequence[Doc], embed_dim: int = 32):
        self.docs = list(docs)
        self.embed_dim = embed_dim
        # 预计算文档向量：即使如此，每次查询仍是 O(N) 点积
        self._vecs = [_pseudo_vec(d.keywords(), embed_dim) for d in self.docs]
        self.m_scan = METRICS.counter("retrieval_docs_scanned_total", "扫描文档数")
        self.m_alloc = METRICS.counter("retrieval_temp_alloc_total", "临时对象分配量")

    def search(self, q: Query) -> RetrievalResult:
        import time as _t

        t0 = _t.perf_counter()
        qv = _pseudo_vec(_tokenize(q.text), self.embed_dim)
        scored: list[tuple[float, int]] = []
        scanned = 0
        for i, doc in enumerate(self.docs):
            scanned += 1
            if not self._visible(doc, q):
                continue
            # 每个候选都造一个中间列表 —— 高并发下这就是 GC 的来源
            terms = doc.keywords()
            self.m_alloc.inc(len(terms) + 1)
            dot = _dot(qv, self._vecs[i])
            if dot > 0:
                scored.append((dot, i))
            _ = terms  # 保留以体现分配开销
        scored.sort(reverse=True)
        hits = [Hit(self.docs[i], s, "linear") for s, i in scored[: q.top_k]]
        self.m_scan.inc(scanned)
        return RetrievalResult(
            hits=hits,
            candidates=len(scored),
            scanned=scanned,
            latency_ms=(_t.perf_counter() - t0) * 1000.0,
        )

    @staticmethod
    def _visible(doc: Doc, q: Query) -> bool:
        if q.tenant and doc.tenant != q.tenant and "public" not in doc.acl:
            return False
        if q.groups and not (doc.acl & q.groups):
            return False
        return True


# --------------------------------------------------------------------------
# 2) 倒排索引 + BM25
# --------------------------------------------------------------------------


class BM25Index:
    """标准倒排 + BM25。查询只触达命中的 posting list。"""

    name = "bm25-inverted"

    def __init__(
        self,
        docs: Sequence[Doc],
        k1: float = 1.2,
        b: float = 0.75,
        postings_cap: int = 20_000,
    ):
        self.docs = list(docs)
        self.k1 = k1
        self.b = b
        self.postings_cap = postings_cap
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.doc_len: list[int] = []
        self.avgdl = 1.0
        self._build()
        self.m_scan = METRICS.counter("bm25_postings_scanned_total", "倒排 posting 扫描数")

    def _build(self) -> None:
        total_len = 0
        for i, doc in enumerate(self.docs):
            terms = doc.keywords()
            self.doc_len.append(len(terms) or 1)
            total_len += len(terms) or 1
            tf: dict[str, int] = {}
            for t in terms:
                tf[t] = tf.get(t, 0) + 1
            for t, c in tf.items():
                self.postings[t].append((i, c))
        self.avgdl = total_len / max(1, len(self.docs))

    def _idf(self, term: str) -> float:
        n = len(self.postings.get(term, ()))
        if n == 0:
            return 0.0
        N = len(self.docs)
        return math.log(1 + (N - n + 0.5) / (n + 0.5))

    def search(self, q: Query) -> RetrievalResult:
        import time as _t

        t0 = _t.perf_counter()
        terms = [t for t in _tokenize(q.text) if t not in STOPWORDS]
        scores: dict[int, float] = defaultdict(float)
        scanned = 0
        for term in terms:
            posting = self.postings.get(term)
            if not posting:
                continue
            idf = self._idf(term)
            # 高频词截断：posting 过长时只取前 N 条（生产上的常见近似）
            subset = posting[: self.postings_cap]
            for i, tf in subset:
                doc = self.docs[i]
                if not NaiveRetriever._visible(doc, q):
                    continue
                scanned += 1
                dl = self.doc_len[i]
                denom = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                scores[i] += idf * tf * (self.k1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        hits = [Hit(self.docs[i], s, "bm25") for i, s in ranked[: q.top_k] if s > q.min_score]
        self.m_scan.inc(scanned)
        return RetrievalResult(
            hits=hits,
            candidates=len(scores),
            scanned=scanned,
            latency_ms=(_t.perf_counter() - t0) * 1000.0,
        )


# --------------------------------------------------------------------------
# 3) 两阶段检索（粗排 + 精排）
# --------------------------------------------------------------------------


class TwoStageRetriever:
    """粗排召回 ``recall_k`` → 精排只对 ``recall_k`` 做昂贵打分。

    生产的 RT 优化，80% 的收益来自"别让昂贵计算见到太多候选"。
    """

    name = "two-stage"

    def __init__(
        self,
        index: BM25Index,
        recall_k: int = 50,
        rerank_cost_ms: float = 0.45,
        cache_size: int = 512,
    ):
        self.index = index
        self.recall_k = recall_k
        self.rerank_cost_ms = rerank_cost_ms
        self._cache: dict[tuple, RetrievalResult] = {}
        self._cache_size = cache_size
        self.m_rerank = METRICS.counter("retrieval_rerank_candidates_total", "精排候选数")

    def search(self, q: Query) -> RetrievalResult:
        import time as _t

        t0 = _t.perf_counter()
        ckey = (q.text, q.top_k, q.tenant, tuple(sorted(q.groups)))
        cached = self._cache.get(ckey)
        if cached is not None:
            hit = RetrievalResult(
                hits=cached.hits,
                candidates=cached.candidates,
                scanned=0,
                latency_ms=(_t.perf_counter() - t0) * 1000.0,
                stage_ms={"cache": (t0 - t0) * 1000.0},
            )
            hit.stage_ms["hit"] = 1.0
            return hit

        t1 = _t.perf_counter()
        coarse = self.index.search(Query(q.text, self.recall_k, q.tenant, q.groups, 0.0))
        t2 = _t.perf_counter()
        if q.rerank:
            # 精排：只对粗排结果打分，成本 ~O(recall_k)
            self.m_rerank.inc(len(coarse.hits))
            _ = self.rerank_cost_ms * len(coarse.hits) / 1000.0  # 记账用，真实实现在这里算
            import time as _tt

            _tt.sleep(self.rerank_cost_ms * len(coarse.hits) / 1000.0)
        t3 = _t.perf_counter()
        if q.rerank:
            hits = sorted(coarse.hits, key=lambda h: -h.score)[: q.top_k]
        else:
            hits = coarse.hits[: q.top_k]
        res = RetrievalResult(
            hits=hits,
            candidates=coarse.candidates,
            scanned=coarse.scanned,
            latency_ms=(t3 - t0) * 1000.0,
            stage_ms={
                "recall": (t2 - t1) * 1000.0,
                "rerank": (t3 - t2) * 1000.0,
            },
        )
        if len(self._cache) < self._cache_size:
            self._cache[ckey] = res
        return res

    def cache_info(self) -> str:
        return f"two-stage cache={len(self._cache)}/{self._cache_size}"


# --------------------------------------------------------------------------
# 伪向量
# --------------------------------------------------------------------------


def _stable_hash(text: str) -> int:
    """跨进程稳定的哈希。

    **绝不能用内置 ``hash()``**：字符串哈希受 ``PYTHONHASHSEED`` 加盐，同一个词
    在不同进程里落在不同维度上，导致检索分数与排序不可复现。这会让"数字要实测
    可复现"这条约定失效 —— lab 里看到的名次第二次运行就变了，而没人知道为什么。
    """
    return zlib.crc32(text.encode("utf-8"))


def _pseudo_vec(terms: Iterable[str], dim: int) -> list[float]:
    vec = [0.0] * dim
    for t in terms:
        h = _stable_hash(t) % dim
        vec[h] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# --------------------------------------------------------------------------
# 便捷
# --------------------------------------------------------------------------


def build_index(
    n: int = 2000, seed: int = 7, with_two_stage: bool = True
) -> tuple[list[Doc], BM25Index, TwoStageRetriever | None]:
    docs = build_corpus(n, seed)
    idx = BM25Index(docs)
    two = TwoStageRetriever(idx) if with_two_stage else None
    return docs, idx, two
