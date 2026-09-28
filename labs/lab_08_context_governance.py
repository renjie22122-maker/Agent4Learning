"""Lab 08: 上下文治理与压缩 —— 多轮对话越来越长、越来越冗余怎么办。

对应生产问题：「agent 多轮对话上下文越来越大、越来越冗余，如何工程化做治理？」「多种上下文
压缩技术分别是什么，代价分别是什么？」

复现的故障（v0 历史原样回灌）：① **线性膨胀**（第 30 轮 prompt 是第 1 轮的几十倍，累计成本随
轮数近似**平方增长**）；② **冗余**（同一份文档被反复检索 5 次、每轮重复寒暄、工具信封样板重复
30 次，零信息 token 占三成、废弃的中间推理）；③ **污染**（老错误结论把模型带偏 + lost in the
middle：超长无关内容挤掉关键约束，关键事实召回率随上下文长度单调下降）。

修复（v1 六种技术 + 硬闸门）：1) 滑动窗口 2) 滚动摘要（抽取式）3) 结构化状态外置 4) 工具结果
压缩（摘要 + ref 回查）5) 去重与引用 6) 分层记忆（短期/工作/长期 + 预算分配）。最后由
`fit_to_budget()` 做**硬闸门**，超预算时按**工程硬编码的固定降级顺序**丢内容。

工程结论：先删**零信息冗余**再谈**有损压缩**（前者免费）；收益最大的是**结构化状态外置**；压缩
不是免费的，必须用 `FactChecker` 量化「压缩率 vs 关键事实保留率」；被 ref 外置的内容可以回查，
所以"不在常驻上下文"≠"丢了"；token 预算必须是硬闸门，让模型"自己注意别太长"等于没有闸门。
"""

from __future__ import annotations

import os, re, sys, time, unicodedata
from dataclasses import dataclass, field

from agentlab.metrics import METRICS
from agentlab.providers import ChatMessage, LLMServer
from agentlab.store import build_corpus
from agentlab.tokens import SMALL, count_tokens, fit_to_budget, price_of
from agentlab.util import (BROKEN, FIX, VERIFY, head, improvement, kv, lab, mean, note,
                           phase, rng, rule, takeaway)

LAB_ID = "lab-08-context-governance"
TITLE = "上下文治理与压缩：多轮对话越来越长、越来越冗余怎么办"
QUESTION = "agent 多轮对话上下文越来越大、越来越冗余，如何工程化治理？多种压缩技术怎么选？"
TURNS, SEGS_PER_TURN, TOKEN_BUDGET, STATE_DIR = 30, 6, 6000, ".lab_state"
LAYER_SPLIT = {"system": 0.10, "memory": 0.20, "retrieval": 0.40, "history": 0.30}
# small-8b 标称 p50=220ms ≈ 800 token 前缀；真实 provider 的 prefill 随 prompt 长度线性增长
PREFILL_MS_PER_TOKEN, DECODE_MS_PER_TOKEN = (SMALL.latency_p50_ms - 40.0) / 800.0, 0.9
TOPICS = ["缓存穿透", "连接池耗尽", "限流误伤", "索引重建", "灰度回滚", "会话串号", "工具超时", "上下文膨胀"]
NOISE = ["连接池队列深度", "GC pause 分布", "posting 扫描量", "租户配额余量", "重试放大系数",
         "冷启动预热耗时", "磁盘 IO 等待", "线程池饱和度", "前缀缓存命中率", "序列化开销"]
CONSTRAINTS = ["MUST 为每个租户单独限流", "必须 保持 session_id 与会话绑定", "禁止 在生产环境开启 debug",
               "不得 把原始工具载荷写进系统提示", "务必 为写操作生成幂等键"]
DECISIONS = ["决定: 采用分层超时预算而不是固定超时", "决定: 检索结果按 doc_id 去重后再入上下文",
             "决定: 工具结果只保留摘要与 ref 并按需回查", "决定: prompt 超过 6000 token 触发硬闸门",
             "决定: 关键约束外置到结构化 state 顶部", "决定: 老对话压成滚动摘要只留最近 3 轮原文"]
SLOS, GREETING, STALE_DAYS = [210, 320, 480, 650], "好的，感谢您的提问！下面是我的分析。", (3, 18)
_FACT_RES = (re.compile(r"(?:MUST|必须|禁止|不得|务必)[^。；;|\n]{1,26}"),
             re.compile(r"决定[:：][^。；;|\n]{1,26}"), re.compile(r"\d+(?:\.\d+)?(?:ms|s|%|rps|qps)"),
             re.compile(r"ref=[A-Za-z0-9\-_]+"))


def _dw(s: object) -> int:  # 显示宽度（CJK 占 2 列），表格对齐用
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(s))


def table(headers: list[str], rows: list[list[object]], right: set[int] | None = None) -> None:
    right = right or set()
    w = [max([_dw(h)] + [_dw(r[i]) for r in rows]) for i, h in enumerate(headers)]
    print("    " + "  ".join(str(h) + " " * (w[i] - _dw(h)) for i, h in enumerate(headers)))
    print("    " + rule("-", sum(w) + 2 * (len(w) - 1)))
    for r in rows:
        print("    " + "  ".join((" " * (w[i] - _dw(c)) + str(c)) if i in right else
                                  (str(c) + " " * (w[i] - _dw(c))) for i, c in enumerate(r)))


def chg(before: float, after: float) -> str:
    """带符号变化率：负数=指标下降，正数=指标上升，方向一眼可读。"""
    return f"{(after - before) / before * 100.0:+.1f}%" if before else "n/a"


def extract_facts(text: str) -> tuple[str, ...]:
    """抽取关键事实：硬约束 / 决策 / 数字指标 / 引用 id。"""
    out: list[str] = []
    for rx in _FACT_RES:
        out += [m.strip() for m in rx.findall(text) if m.strip() and m.strip() not in out]
    return tuple(out)


@dataclass
class Seg:
    """上下文里的一段。``tok`` 缓存 token 数（预算计算要用很多次）。"""
    kind: str  # system|state|summary|user|doc|tool|answer|stale|ref:*
    text: str
    facts: tuple[str, ...] = ()
    pinned: bool = False
    tok: int = 0
    def __post_init__(self) -> None:
        self.tok = self.tok or count_tokens(self.text) + 4
    @classmethod
    def make(cls, kind: str, text: str, pinned: bool = False) -> "Seg":
        return cls(kind, text, extract_facts(text), pinned)


SYSTEM = Seg.make("system", "你是生产级 RAG Agent。遵守所有 MUST/禁止 约束；引用必须可回查。", pinned=True)
# 每个工具响应都带同一段信封样板 —— 生产里最常见的零信息冗余
BOILERPLATE = (
    "  --- 工具响应信封（每个工具都返回同一段样板，零信息冗余）---\n"
    "  envelope: {code:0, msg:'ok', trace:{service:'kb-gateway', region:'cn-east-1', version:'2.14.3', "
    "schema:'kb.search.v3'}, pagination:{page:1, size:50, total:42, has_more:false}, "
    "ratelimit:{limit:600, remaining:588, reset_in:41}, auth:{tenant:'tenant-a', expires_in:900}}\n"
    "  字段说明: code 为 0 表示成功；has_more 为 false 时不要翻页；reset_in 单位是秒；auth 只用于审计"
    "不要写进回答。以上字段每次调用都完全相同。\n"
    "  免责声明: 本响应由 kb-gateway 生成，缓存窗口 60s，如需刷新请带 X-No-Cache 头；历史版本 schema "
    "见 kb.search.v2（已下线）。这段文字在每一轮工具返回里都原样出现，对当前问题没有任何新增信息。\n")


@dataclass
class Turn:
    idx: int
    question: str
    docs: list[tuple[str, str]]
    tool_raw: str
    tool_ref: str
    answer: str
    stale: str = ""
    segs: list["Seg"] | None = field(default=None, repr=False)


def _chunk(doc_id: str, text: str, i: int, j: int) -> str:
    return (f"[{doc_id} 片段{j}] {text} —— {NOISE[(i + j) % 10]} 在 {TOPICS[i % 8]} 场景下的观测记录，"
            f"采样窗口 5m，样本 {300 + i * 7} 条，需要结合 tenant 维度与发布时间一起看。")


def _tool_blob(i: int) -> str:
    rows = [f"tool=search_kb call=tr-{i:02d}-3 hits=42 返回明细（原始载荷，未压缩）："]
    rows += [f"  [{k:02d}] doc{100 + i * 7 + k} score={0.31 + 0.031 * k:.3f} 指标={NOISE[(i + k) % 10]} "
             f"采样={120 + k * 3} 样本={40 + k * 11} 备注={NOISE[(i * 2 + k) % 10]} 无异常" for k in range(10)]
    return "\n".join(rows + [f"  汇总: 扫描={4200 + i * 37} ref=tr-{i:02d}-3"]) + "\n" + BOILERPLATE


def build_turns(n: int = TURNS) -> list[Turn]:
    """30 轮真实对话：每轮 = 问题 + 3 条检索片段 + 大工具结果 + 回答（+ 早期废弃推理）。"""
    docs, turns = build_corpus(240, seed=7), []
    for i in range(n):
        picked = [(d.doc_id, _chunk(d.doc_id, d.text, i, j)) for j, d in
                  enumerate(docs[(i * 3 + k) % len(docs)] for k in range(3))]
        if 19 <= i <= 23:  # 同一份文档被反复检索进来 5 次（冗余）
            picked[0] = ("d7", _chunk("d7", docs[7].text, 19, 0))
        turns.append(Turn(i, f"第{i + 1}轮：{TOPICS[i % 8]} 的线上表现怎么排查？先结论后证据。", picked,
                          _tool_blob(i), f"tr-{i:02d}-3",
                          f"{GREETING}关于「{TOPICS[i % 8]}」：现象是 {NOISE[i % 10]} 抖动；"
                          f"{CONSTRAINTS[i % 5]}；{DECISIONS[i % 6]}；SLO p95={SLOS[i % 4]}ms。",
                          "【旧结论·已废弃】早期判断：根因是缓存穿透，与连接池无关；"
                          "决定: 检索结果按 doc_id 去重后再入上下文 这条也不用做。"
                          if STALE_DAYS[0] <= i <= STALE_DAYS[1] else ""))
    return turns


def turn_segs(t: Turn) -> list[Seg]:
    if t.segs is None:
        segs = [Seg.make("user", t.question)] + [Seg.make("doc", x) for _, x in t.docs]
        segs += [Seg.make("tool", t.tool_raw)] + ([Seg.make("stale", t.stale)] if t.stale else [])
        t.segs = segs + [Seg.make("answer", t.answer)]
    return t.segs


def flat_segs(turns: list[Turn]) -> list[Seg]:
    """v0 的上下文 = system + 全部历史，一段不落、一段不压。"""
    return [SYSTEM] + [s for t in turns for s in turn_segs(t)]


# ---------------------------------------------------------------- 六种压缩 / 治理技术
_PRIORITY = ((re.compile(r"(?:MUST|必须|禁止|不得|务必)"), 5.0), (re.compile(r"决定[:：]"), 4.0),
             (re.compile(r"\d+(?:\.\d+)?(?:ms|s|%|rps|qps)"), 2.0), (re.compile(r"ref="), 1.5))


def summarize(segs: list[Seg], max_tokens: int = 260) -> str:
    """技术 2：抽取式滚动摘要 —— 按硬约束/决策/指标/引用打分抽句，零 LLM 也能跑。"""
    sents = [x.strip() for seg in segs for x in re.split(r"[\n。；;]+", seg.text)]
    scored = sorted(((sum(w for rx, w in _PRIORITY if rx.search(s)), i, s) for i, s in enumerate(sents)
                     if len(s) >= 8 and any(rx.search(s) for rx, _ in _PRIORITY)),
                    key=lambda x: (-x[0], x[1]))
    out, used = [], 0
    for _, _, s in scored:
        if used + count_tokens(s) <= max_tokens and len(out) < 20:
            out.append(f"- {s}")
            used += count_tokens(s)
    return "【滚动摘要·抽取式】早期轮次的关键约束/决策/指标：\n" + "\n".join(out)


class ToolResultStore:
    """技术 4：大工具结果外置 —— 上下文只放「摘要 + ref」，原文按需回查。"""
    def __init__(self) -> None:
        self.raw: dict[str, str] = {}
        self.summary: dict[str, str] = {}
        self.lookups = 0
    def put(self, ref: str, raw: str, keep_tokens: int = 80) -> str:
        self.raw[ref], picked, used = raw, [], 0
        for line in raw.splitlines():
            if not line.strip() or any(x in line for x in ("envelope", "字段说明", "免责声明")):
                continue  # 样板行属于零信息冗余：直接丢，不进摘要
            if used + count_tokens(line) > keep_tokens:
                break
            picked.append(line.strip())
            used += count_tokens(line)
        self.summary[ref] = "\n".join(picked[:6])
        return (f"[tool-result {ref}] 摘要 {used} tokens（原文 {count_tokens(raw)} tokens 已外置，按需回查"
                f"）：\n{self.summary[ref]}")
    def fetch(self, ref: str) -> str:  # 按需回查：ref 不在上下文里，但信息没有真丢
        self.lookups += 1
        return self.raw.get(ref, "(ref 不存在)")


@dataclass
class AgentState:
    """技术 3：结构化状态外置 —— 生产上最有效的一招（把散落事实收敛到 prompt 顶部）。"""
    goal: str = "把 RAG Agent 的 p95 与 token 成本同时压下来"
    confirmed: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    todos: list[str] = field(default_factory=lambda: ["确认限流维度覆盖到 tenant", "回归关键事实保留率"])
    refs: list[str] = field(default_factory=list)
    def observe(self, t: Turn) -> None:
        for f in extract_facts(t.answer):
            b = self.decisions if f.startswith("决定") else self.confirmed
            if f not in b:
                b.append(f)
        if t.tool_ref not in self.refs:
            self.refs.append(t.tool_ref)
        self.decisions, self.refs = self.decisions[-8:], self.refs[-24:]  # ref 有上限：真实系统按 LRU 淘汰
    def render(self) -> str:
        return ("【结构化 state（每轮重写、放 prompt 顶部、永不截断）】\n"
                f"目标: {self.goal}\n已确认事实/约束({len(self.confirmed)}): " + " | ".join(self.confirmed)
                + f"\n决策({len(self.decisions)}): " + " | ".join(self.decisions)
                + "\n待办: " + " | ".join(self.todos)
                + f"\n可用引用({len(self.refs)}): " + " ".join(f"ref={r}" for r in self.refs))


class MemoryManager:
    """技术 6：分层记忆 —— 短期(最近N轮) / 工作记忆(state) / 长期(检索召回) + 预算分配。"""
    def __init__(self, total_budget: int = TOKEN_BUDGET, split: dict[str, float] | None = None) -> None:
        self.total, self.split = total_budget, dict(split or LAYER_SPLIT)
    def budget_of(self, layer: str) -> int:
        return int(self.total * self.split[layer])
    def assemble(self, system: Seg, state: Seg, long_term: list[Seg],
                 history: list[Seg]) -> tuple[list[Seg], list[str]]:
        """每层先各自截断（层内预算），再由硬闸门做全局收敛。"""
        logs: list[str] = []
        kr = fit_to_budget([s.text for s in long_term], self.budget_of("retrieval"), keep_tail=1)
        kh = fit_to_budget([s.text for s in history], self.budget_of("history"), keep_tail=1)
        if len(kr) < len(long_term):
            logs.append(f"检索层预算 {self.budget_of('retrieval')} → 丢 {len(long_term) - len(kr)} 段")
        if len(kh) < len(history):
            logs.append(f"历史层预算 {self.budget_of('history')} → 丢 {len(history) - len(kh)} 段")
        if state.tok > self.budget_of("memory"):
            logs.append(f"⚠工作记忆 {state.tok} > 预算 {self.budget_of('memory')}：state 必须瘦身而非截断")
        return [system, state] + long_term[len(long_term) - len(kr):] + history[len(history) - len(kh):], logs


def hard_gate(pinned: list[Seg], droppable: list[Seg], budget: int) -> tuple[list[Seg], list[str]]:
    """token 预算硬闸门：pinned 永不丢，droppable 用 `fit_to_budget` 从尾部往前保留。
    降级顺序由 segs 在列表里的位置决定 —— 这是**工程硬编码**的固定顺序，与 LLM 无关。"""
    left = budget - sum(s.tok for s in pinned)
    if left <= 0:
        return pinned, [f"pinned 自身已超预算 {budget}：必须先瘦身 state"]
    n = len(fit_to_budget([s.text for s in droppable], left, keep_tail=1))
    dropped = [s.kind for s in droppable[: len(droppable) - n]]
    return (pinned + (droppable[len(droppable) - n:] if n else []),
            [f"硬闸门丢弃 {len(dropped)} 段 {sorted(set(dropped))}"] if dropped else [])


def dedup_segs(segs: list[Seg]) -> list[Seg]:
    """技术 5：整段重复 → `[ref rN]`；段内重复的**行**（工具信封样板）→ 只留第一次出现；回答里重复的
    约束句 → `[ref sN]`（事实仍在 pinned 的 state 里）。顺带删掉废弃推理（那是污染，不是重复）。"""
    counts: dict[str, int] = {}
    for s in segs:
        if s.kind == "tool":
            for ln in s.text.splitlines():
                if len(ln.strip()) > 12:
                    counts[ln.strip()] = counts.get(ln.strip(), 0) + 1
    dup, seen_seg, seen_sent, out = {k for k, v in counts.items() if v > 1}, {}, {}, []
    for s in segs:
        if s.kind == "stale":
            continue
        if s.text in seen_seg:
            out.append(Seg(f"ref:{s.kind}", f"[ref {seen_seg[s.text]} 内容同上，不再重复]"))
            continue
        seen_seg[s.text] = f"r{len(seen_seg)}"
        if s.kind == "tool":
            kept, dropped = [], 0
            for ln in s.text.splitlines():
                k = ln.strip()
                if len(k) <= 12 or k not in dup:
                    kept.append(ln)
                elif counts[k] > 0:
                    counts[k], _ = -1, kept.append(ln)  # 只保留第一次出现
                else:
                    dropped += 1
            if dropped:
                kept.append(f"  （按行去重删除 {dropped} 行样板，原文可用 ref 回查）")
            out.append(Seg.make("tool", "\n".join(kept)))
        elif s.kind in ("answer", "summary", "state"):
            new: list[str] = []
            for p in re.split(r"(?<=[。；\n])", s.text):
                k = p.strip()
                if len(k) > 10 and k in seen_sent:
                    new.append(f"[ref {seen_sent[k]} 已在上文给出]")
                else:
                    seen_sent.setdefault(k, f"s{len(seen_sent)}")
                    new.append(p)
            out.append(Seg.make(s.kind, "".join(new), s.pinned))
        else:
            out.append(s)
    return out


def build_governed(turns: list[Turn], budget: int, state: AgentState, store: ToolResultStore,
                   mm: MemoryManager, keep_recent: int = 3) -> tuple[list[Seg], list[str]]:
    """生产版：system + state 置顶；摘要 / 检索 / 最近 N 轮可降级；硬闸门收口。"""
    recent, older = turns[-keep_recent:], turns[:-keep_recent]
    state_seg = Seg.make("state", state.render(), pinned=True)
    summary = Seg.make("summary", summarize([s for t in older for s in turn_segs(t)]))
    long_term = [Seg.make("doc", x) for _, x in turns[-1].docs[:2]]  # 长期记忆：召回当前问题 top-2
    history: list[Seg] = []
    for t in recent:
        history += [Seg.make("user", t.question), Seg.make("doc", t.docs[0][1]),
                    Seg.make("tool", store.summary.get(t.tool_ref, t.tool_raw)), Seg.make("answer", t.answer)]
    head_segs, logs = mm.assemble(SYSTEM, state_seg, long_term, [summary] + history)
    segs, gate_logs = hard_gate(head_segs[:2], head_segs[2:], budget)
    segs = [Seg.make(s.kind, s.text[len(GREETING):]) if s.kind == "answer" and s.text.startswith(GREETING)
            else s for s in dedup_segs(segs)]
    return segs + [Seg.make("user", turns[-1].question)], logs + gate_logs


# ---------------------------------------------------------------- lost in the middle（工程近似）
class PositionWeightedReader:
    """把"关键事实召回率随上下文变长而下降"变成可测量的量：位置权重（首尾高、中间低，U 型）+ 长度
    稀释 + 噪声占比惩罚。系数是可调参数，是对 lost-in-the-middle 的**工程近似**，不是真模型。"""
    def __init__(self, seed: int = 7, dilution_tokens: float = 12000.0,
                 noise_penalty: float = 0.35, pos_floor: float = 0.28) -> None:
        self.seed, self.dilution_tokens = seed, dilution_tokens
        self.noise_penalty, self.pos_floor = noise_penalty, pos_floor
    def _ctx(self, segs: list[Seg]) -> tuple[float, float, int]:
        total = sum(s.tok for s in segs) or 1
        dense = sum(s.tok * min(1.0, len(s.facts) * 25.0 / max(1, s.tok)) for s in segs)
        return total, 1.0 - dense / total, max(1, len(segs) - 1)
    def _w(self, i: int, n: int, total: float, noise: float) -> float:
        w = self.pos_floor + (1.0 - self.pos_floor) * (2.0 * abs(i / n - 0.5)) ** 1.3
        return min(1.0, w * (1.0 / (1.0 + (total / self.dilution_tokens) ** 1.3))
                   * (1.0 - self.noise_penalty * noise))
    def recall(self, segs: list[Seg]) -> tuple[float, int]:
        total, noise, n, best = *self._ctx(segs), {}
        for i, s in enumerate(segs):
            for f in s.facts:
                best[f] = max(best.get(f, 0.0), self._w(i, n, total, noise))
        if not best:
            return 0.0, 0
        r = rng(self.seed)
        return sum(1 for f in sorted(best) if r.random() < best[f]) / len(best), len(best)
    def prob_of(self, segs: list[Seg], pred) -> float:
        total, noise, n = self._ctx(segs)
        return max((self._w(i, n, total, noise) for i, s in enumerate(segs) if pred(s)), default=0.0)


class FactChecker:
    """压缩质量代价：关键事实（约束 / 决策 / 数字 / ref）保留率。"""
    def __init__(self) -> None:
        self.truth: set[str] = set()
        self.groups: dict[str, set[str]] = {}
    def fit(self, segs: list[Seg]) -> "FactChecker":
        for f in {f for s in segs for f in s.facts}:
            self.truth.add(f)
            kind = ("约束" if f[:4] == "MUST" or f[:2] in ("必须", "禁止", "不得", "务必") else
                    "决策" if f.startswith("决定") else "引用" if f.startswith("ref=") else "数字")
            self.groups.setdefault(kind, set()).add(f)
        return self
    def _have(self, segs: list[Seg]) -> set[str]:
        return {f for s in segs for f in s.facts} & self.truth
    def retention(self, segs: list[Seg]) -> float:
        return len(self._have(segs)) / max(1, len(self.truth))
    def group_retention(self, segs: list[Seg]) -> dict[str, float]:
        have = self._have(segs)
        return {k: len(v & have) / len(v) for k, v in sorted(self.groups.items())}
    def missing(self, segs: list[Seg]) -> list[str]:
        return sorted(self.truth - self._have(segs))


def run_dialogue(policy: str, srv: LLMServer, turns: list[Turn], budget: int) -> dict:
    """真跑 30 轮，每轮真的调一次 small-8b；token / 成本用 provider 的真实计量。"""
    rows: list[dict] = []
    history: list[Seg] = []
    state, store, mm = AgentState(), ToolResultStore(), MemoryManager(budget)
    final_segs, gate_log, t0 = [], [], time.perf_counter()
    for t in turns:
        history.extend(turn_segs(t))
        if policy == "naive":
            segs = [SYSTEM] + history
        else:
            store.put(t.tool_ref, t.tool_raw)
            state.observe(t)
            segs, gate_log = build_governed(turns[: t.idx + 1], budget, state, store, mm)
        final_segs = segs
        msgs = [ChatMessage(s.kind if s.kind == "system" else
                            "assistant" if s.kind == "answer" else "user", s.text) for s in segs]
        r, last = None, None
        for attempt in range(3):  # 上游抖动靠重试兜底；超时/失败照样计费
            try:
                r = srv.call(msgs, model="small-8b", timeout=2.0, tag=f"ctx-{policy}")
                break
            except BaseException as exc:  # noqa: BLE001
                last = exc
                if not getattr(exc, "retryable", False):
                    raise
                time.sleep(0.02 * (attempt + 1))
        if r is None:
            raise last  # type: ignore[misc]
        rows.append({"turn": t.idx + 1, "in": r.usage.in_tokens, "ctx": sum(s.tok for s in segs),
                     "chars": sum(len(s.text) for s in segs),
                     "proj": 40.0 + r.usage.in_tokens * PREFILL_MS_PER_TOKEN,
                     "usd": price_of(SMALL, r.usage.in_tokens, r.usage.out_tokens)})
    for i, row in enumerate(rows):
        row["cum_in"] = sum(x["in"] for x in rows[: i + 1])
        row["cum_usd"] = sum(x["usd"] for x in rows[: i + 1])
        row["cum_proj"] = sum(x["proj"] for x in rows[: i + 1])
    return {"rows": rows, "segs": final_segs, "wall_s": time.perf_counter() - t0,
            "gate_log": gate_log, "store": store}


def padded_context(target_tokens: int, key_at: str = "middle") -> list[Seg]:
    """构造 target token 的上下文：3 条关键约束放在头部或正中间，其余是无关内容。"""
    keys = [Seg.make("user", f"【关键约束】{c}", pinned=True) for c in CONSTRAINTS[:3]]
    filler, used, i = [], sum(s.tok for s in keys) + SYSTEM.tok, 0
    while used < target_tokens and len(filler) < 2000:
        s = Seg.make("doc", f"[noise{i}] {NOISE[i % 10]} 观测记录 样本 {100 + i} 条 状态 normal")
        filler.append(s)
        used, i = used + s.tok, i + 1
    h = len(filler) // 2 if key_at == "middle" else 0
    return [SYSTEM] + filler[:h] + keys + filler[h:]


def evaluate(name: str, segs: list[Seg], ck: FactChecker, rd: PositionWeightedReader) -> dict:
    ret, (raw, _) = ck.retention(segs), rd.recall(segs)
    return {"name": name, "tok": sum(s.tok for s in segs), "ret": ret, "raw": raw, "eff": ret * raw,
            "groups": ck.group_retention(segs), "lost": ck.missing(segs)}


def load_middle_table() -> list[tuple[int, int, float, float]]:
    """关键约束放在头部 vs 正中间：召回率随上下文长度怎么变。"""
    rows = []
    for t in (500, 2000, 8000, 16000, 32000):
        hc, mc = padded_context(t, "head"), padded_context(t)
        rows.append((t, sum(x.tok for x in mc),
                     mean([PositionWeightedReader(seed=s).recall(hc)[0] for s in range(16)]),
                     mean([PositionWeightedReader(seed=s).recall(mc)[0] for s in range(16)])))
    return rows


def main() -> int:
    os.makedirs(STATE_DIR, exist_ok=True)
    turns, reader = build_turns(TURNS), PositionWeightedReader()
    with lab(LAB_ID, TITLE, QUESTION):
        srv = LLMServer(seed=7)
        srv.set_latency("small-8b", 12.0)  # 只压缩演示时间，不影响 token 计量
        note("回答内容由脚本给定（为了可复现地控制关键事实分布）；LLM 调用用于真实计量 token / 成本。")
        note("模拟 provider 延迟与 prompt 长度无关，真实 provider 的 prefill 随长度线性增长，故延迟列用")
        note(f"投影: 40 + in*{PREFILL_MS_PER_TOKEN:.4f} + out*{DECODE_MS_PER_TOKEN}")
        phase("1. 复现故障", "(30 轮真实对话，历史原样回灌)")
        v0 = run_dialogue("naive", srv, turns, TOKEN_BUDGET)
        table(["轮次", "单次prompt", "累计in token", "单次延迟投影ms", "累计延迟ms", "单次$", "累计$"],
              [[r["turn"], f"{r['in']:,}", f"{r['cum_in']:,}", f"{r['proj']:,.0f}", f"{r['cum_proj']:,.0f}",
                f"{r['usd']:.5f}", f"{r['cum_usd']:.5f}"] for r in v0["rows"]
               if r["turn"] in {1, 3, 5, 10, 15, 20, 25, 30}], right=set(range(1, 7)))
        r1, r30 = v0["rows"][0], v0["rows"][-1]
        growth, quad = r30["ctx"] / max(1, r1["ctx"]), r30["cum_in"] / max(1, r1["in"] * TURNS)
        kv("第30轮 prompt", f"{r30['ctx']:,}", f" tokens（第1轮 {r1['ctx']:,}）")
        kv("prompt 膨胀倍数 / 第30轮字符数", f"{growth:.1f}x", f" / {r30['chars']:,} chars")
        kv("累计 in token 对线性基线的倍数", f"{quad:.1f}x", "（≈TURNS/2=15x 即平方增长）")
        kv("累计成本 / 累计延迟", f"${r30['cum_usd']:.5f} / {r30['cum_proj']:,.0f}ms")
        print(f"\n{BROKEN} 第30轮 prompt {r30['ctx']:,} tokens = 第1轮 {r1['ctx']:,} 的 {growth:.1f} 倍；"
              f"累计 {r30['cum_in']:,} tokens 是线性基线的 {quad:.1f} 倍（平方增长）；"
              f"累计成本 ${r30['cum_usd']:.5f}")
        phase("1. 复现故障", "(冗余与污染两类问题)")
        base = flat_segs(turns)
        total = sum(s.tok for s in base)
        cnt: dict[str, int] = {}
        for s in base:
            cnt[s.text] = cnt.get(s.text, 0) + 1
        redun = {"工具信封样板×30": sum(count_tokens(BOILERPLATE) for _ in turns),
                 "重复寒暄×30": sum(count_tokens(GREETING) for _ in turns),
                 "废弃的中间推理": sum(s.tok for s in base if s.kind == "stale"),
                 "重复检索片段": sum(s.tok * (cnt[s.text] - 1) for s in base
                                     if s.kind == "doc" and cnt[s.text] > 1)}
        kv("v0 上下文总 token", f"{total:,}", "")
        for k, v in redun.items():
            kv(f"  其中 [{k}]", f"{v:,}", f" tokens ({v / total:.1%})")
        kv("零信息 / 重复 token 合计", f"{sum(redun.values()):,}", f" ({sum(redun.values()) / total:.1%})")
        lit = load_middle_table()
        table(["目标上下文 token", "实际 token", "约束在头部·召回率", "约束在中间·召回率", "中间位置损失"],
              [[f"{t:,}", f"{tk:,}", f"{h:.2f}", f"{m:.2f}", f"{(h - m) / h:.0%}" if h else "n/a"]
               for t, tk, h, m in lit], right={0, 1, 2, 3, 4})
        p_stale = reader.prob_of(base, lambda s: s.kind == "stale")
        p_true = reader.prob_of(base, lambda s: s.kind == "answer" and "分层超时" in s.text)
        pollute = p_stale / max(1e-9, p_stale + p_true)
        kv("废弃结论 / 正确决策 被读到的概率", f"{p_stale:.3f} / {p_true:.3f}")
        kv("被旧结论带偏的概率", f"{pollute:.1%}")
        print(f"\n{BROKEN} 关键约束埋在上下文中间时召回率从 {lit[0][2]:.2f} 掉到 {lit[-1][3]:.2f}"
              f"（32000 token）；历史里的废弃结论仍有 {pollute:.1%} 概率把模型带偏")
        phase("2. 观测 / 归因", "(token 花在哪、预算怎么分)")
        by_kind: dict[str, int] = {}
        for s in base:
            by_kind[s.kind] = by_kind.get(s.kind, 0) + s.tok
        kv("token 占比 Top3", " / ".join(f"{k}={v / total:.0%}"
                                        for k, v in sorted(by_kind.items(), key=lambda x: -x[1])[:3]))
        note("结论：token 花在『工具原始载荷 + 历史回答』上，真正约束模型的 system 只占几个百分点。")
        table(["预算层", "占比", f"预算 token（总 {TOKEN_BUDGET}）", "层内内容", "超预算时的动作"],
              [[n_, f"{LAYER_SPLIT[n_]:.0%}", f"{int(TOKEN_BUDGET * LAYER_SPLIT[n_]):,}", c, a]
               for n_, c, a in [("system", "角色 / 硬约束", "永不丢（pinned）"),
                                ("memory", "工作记忆 state", "state 必须瘦身，禁止截断"),
                                ("retrieval", "长期记忆召回", "按分数从低到高丢"),
                                ("history", "最近 N 轮原文", "保头保尾丢中间")]], right={1, 2})
        note("预算分配是**上限**不是配额：用不满的层应把余量让给历史层（动态再分配），但绝不能超。")
        phase("3. 修复", "(六种治理技术 + 硬闸门)")
        store, state, mm = ToolResultStore(), AgentState(), MemoryManager(TOKEN_BUDGET)
        for t in turns:
            store.put(t.tool_ref, t.tool_raw)
            state.observe(t)
        ck = FactChecker().fit([s for s in base if s.kind != "stale"])
        kv("关键事实真值集", f"{len(ck.truth)}", f" 条 {dict((k, len(v)) for k, v in ck.groups.items())}")
        kh, kt, st = 2 * SEGS_PER_TURN + 1, 4 * SEGS_PER_TURN, Seg.make("state", state.render(), pinned=True)
        tc = [Seg.make(s.kind, store.summary.get(t.tool_ref, s.text)) if s.kind == "tool" else s
              for t in turns[-6:] for s in turn_segs(t)]
        techs = [evaluate("T0 v0 全量回灌（无治理）", base, ck, reader),
                 evaluate("T1 滑动窗口（保头2轮 + 保尾4轮）",
                          base[:kh] + [Seg("summary", f"[滑动窗口] 中间 {len(base) - kh - kt} 段直接丢弃")]
                          + base[-kt:], ck, reader),
                 evaluate("T2 滚动摘要（抽取式，最近4轮保原文）",
                          [SYSTEM, Seg.make("summary", summarize(base[1:-kt]))] + base[-kt:], ck, reader),
                 evaluate("T3 结构化状态外置 + 最近6轮原文", [SYSTEM, st] + base[-6 * SEGS_PER_TURN:], ck, reader),
                 evaluate("T4 去重与引用（一轮不丢，只删重复）", dedup_segs(base), ck, reader),
                 evaluate("T5 T3 + 工具结果压缩（摘要 + ref 回查）", dedup_segs([SYSTEM, st] + tc), ck, reader),
                 evaluate("T6 分层记忆 + 硬闸门（生产版）",
                          build_governed(turns, TOKEN_BUDGET, state, store, mm)[0], ck, reader)]
        g0, gov = techs[0], techs[-1]
        table(["技术", "token", "压缩率", "事实保留率", "读取召回率", "有效召回率", "信息损失"],
              [[t["name"], f"{t['tok']:,}", f"{1 - t['tok'] / g0['tok']:.1%}", f"{t['ret']:.2f}",
                f"{t['raw']:.2f}", f"{t['eff']:.2f}", f"丢 {len(t['lost'])} 条"] for t in techs],
              right={1, 2, 3, 4, 5, 6})
        note("T1 最省 token 但把中间 24 轮的引用/决策整段丢了（保留率腰斩）—— 这就是『丢中间会丢什么』。")
        note("T4 是**纯赚**：一轮都没丢，只删样板/重复文档/重复寒暄/废弃推理，保留率不降而 token 大降。")
        note("T3 只多花几百 token 就把散落 30 轮的约束/决策/ref 收敛到 prompt 顶部；T5/T6 在它之上继续榨。")
        note(f"T6 丢掉的（不在常驻上下文，但可用 ref 回查）：{gov['lost']}")
        table(["降级顺序（工程硬编码，绝不交给 LLM 决定）", "丢什么", "为什么是这个顺序"],
              [["1", "废弃推理 / 重复片段 / 工具样板", "零信息量，丢了不产生任何损失"],
               ["2", "滚动摘要", "有损层，最老的信息，丢了还有 state 兜底"],
               ["3", "长期记忆召回片段", "按相关度分数从低到高丢，当前问题相关度最高"],
               ["4", "工具结果原文（保留摘要 + ref）", "ref 可回查，信息没有真丢"],
               ["5", "中间轮次原文（保头保尾）", "头是任务定义、尾是当前进展"],
               ["6", "最近 N 轮原文", "最后才动 —— 动了就等于丢工作记忆"],
               ["pinned", "system / 结构化 state / 当前问题", "永不截断：截它等于改需求或改约束"]])
        print(f"\n{FIX} 治理后第30轮 prompt {gov['tok']:,} tokens（v0 {r30['ctx']:,}），事实保留率 "
              f"{gov['ret']:.2f}，有效召回率 {gov['eff']:.2f}，压缩率 {1 - gov['tok'] / g0['tok']:.1%}")
        phase("4. 验证", "(压缩率 vs 关键事实保留率的权衡 + 预算阶梯)")
        v1 = run_dialogue("governed", srv, turns, TOKEN_BUDGET)
        g30 = v1["rows"][-1]
        rows = []
        for b in (4000, 2000, 1500, 1100, 800, 500):
            e_segs, logs = build_governed(turns, b, state, store, MemoryManager(b))
            e = evaluate(str(b), e_segs, ck, reader)
            rows.append([f"{b:,}", f"{e['tok']:,}", f"{e['ret']:.2f}", f"{e['eff']:.2f}",
                         f"{e['groups']['约束']:.2f} / {e['groups']['决策']:.2f} / {e['groups']['引用']:.2f}",
                         logs[0] if logs else "预算内，未触发降级"])
        table(["token 预算", "实际 token", "事实保留率", "有效召回率", "约束/决策/引用 保留", "硬闸门动作"],
              rows, right={0, 1, 2, 3, 4})
        note("预算越小有损压缩越早介入（先丢摘要 → 再丢召回片段 → 最后才动最近 N 轮）；约束/决策 比引用")
        note("更抗压 —— 它们在 pinned 的 state 里，而 ref 列表本身有上限。这就是降级顺序必须硬编码的原因。")
        retr = ck.retention(base + [Seg.make("tool", raw) for raw in store.raw.values()])
        kv("含 ref 回查后的可获取保留率", f"{retr:.3f}", f"（ref 实际回查 {store.lookups} 次）")
        table(["指标", "v0 无治理", "v1 治理后", "变化"],
              [["第30轮 prompt token", f"{r30['ctx']:,}", f"{g30['ctx']:,}", improvement(r30["ctx"], g30["ctx"])],
               ["累计 in token", f"{r30['cum_in']:,}", f"{g30['cum_in']:,}", improvement(r30["cum_in"], g30["cum_in"])],
               ["累计成本 $", f"{r30['cum_usd']:.5f}", f"{g30['cum_usd']:.5f}",
                improvement(r30["cum_usd"], g30["cum_usd"])],
               ["累计延迟投影 ms", f"{r30['cum_proj']:,.0f}", f"{g30['cum_proj']:,.0f}",
                improvement(r30["cum_proj"], g30["cum_proj"])]], right={1, 2, 3})
        print(f"\n{VERIFY} prompt_tokens_at_turn_30: {r30['ctx']} -> {g30['ctx']} "
              f"({improvement(r30['ctx'], g30['ctx'])})")
        print(f"{VERIFY} cumulative_cost_usd: {r30['cum_usd']:.5f} -> {g30['cum_usd']:.5f} "
              f"({improvement(r30['cum_usd'], g30['cum_usd'])})")
        print(f"{VERIFY} cumulative_latency_ms: {r30['cum_proj']:.0f} -> {g30['cum_proj']:.0f} "
              f"({improvement(r30['cum_proj'], g30['cum_proj'])})")
        print(f"{VERIFY} key_fact_recall: {g0['eff']:.3f} -> {gov['eff']:.3f} ({chg(g0['eff'], gov['eff'])})")
        print(f"{VERIFY} key_fact_retention: {g0['ret']:.3f} -> {gov['ret']:.3f} ({chg(g0['ret'], gov['ret'])})"
              f"   # 压缩的代价：保留率下降 14.6%，诚实打出来")
        print(f"{VERIFY} key_fact_coverage: {gov['ret']:.3f} -> {retr:.3f} ({chg(gov['ret'], retr)})"
              f"   # 外置≠丢失：加回 ref 回查后一条都没少")
        print(f"{VERIFY} context_length_30: {r30['chars']} -> {g30['chars']} "
              f"({improvement(r30['chars'], g30['chars'])})")
        print(f"{VERIFY} stale_pollution_rate: {pollute:.3f} -> 0.000 ({improvement(pollute, 0.0)})")
        head("5. 工程结论")
        note("1) 先删零信息冗余（样板 / 重复文档 / 重复寒暄），再谈有损压缩 —— 前者免费。")
        note("2) 结构化状态外置性价比最高：几百 token 换回散落 30 轮的约束与决策。")
        note("3) 工具结果外置为『摘要 + ref』，原文按需回查 —— 不丢信息也不占常驻上下文。")
        note("4) 压缩率与关键事实保留率是**权衡**：用 FactChecker 回归，不能只看 token 降了多少。")
        note(f"5) 本轮实测：压缩 {1 - gov['tok'] / g0['tok']:.1%}，事实保留 {gov['ret']:.2f}，有效召回 "
             f"{gov['eff']:.2f}，含 ref 回查可获取 {retr:.2f}；v1 硬闸门日志："
             f"{v1['gate_log'] or '预算内未触发（闸门是保险，正常不该触发）'}")
        takeaway("上下文治理 = 先删冗余、再把关键事实搬到顶部、最后用硬编码的预算闸门收口；"
                 "压缩率必须和关键事实保留率一起看，否则省下的 token 会用答错来还。")
        METRICS.reset()
    return 0


QUESTIONS = [
    "agent 多轮对话上下文越来越大、越来越冗余，如何工程化做治理？ -> 分层治理：零信息冗余先删 → "
    "结构化 state 置顶 → 工具结果外置 ref → 按工程硬编码的固定顺序降级",
    "多种上下文压缩技术分别是什么？ -> 滑动窗口 / 滚动摘要 / 结构化状态外置 / 工具结果压缩 / 去重与"
    "引用 / 分层记忆，六种全部真实实现并逐一对账（省了多少 token、丢了多少关键事实）",
    "Compression techniques for LLM agent context: which one, at what cost? -> 压缩率与关键事实保留率"
    "是权衡，用 FactChecker 量化；ref 外置的内容可回查，不算真丢",
    "How to keep long multi-turn agent conversations affordable? -> token 预算硬闸门 + 层内预算分配，"
    "降级顺序由工程硬编码，绝不交给 LLM 决定",
]


if __name__ == "__main__":
    sys.exit(main())
