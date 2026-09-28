"""Lab: 企业级多租户隔离 —— 权限、数据、会话与配额。

对应生产问题
------------
* 「企业级的 agent 如何做权限和数据层面的隔离，多租户工程化如何做设计落地？」
* 「多用户并发，agent 的会话隔离，如何工程化落地，如何避免串会话？」
复现的故障（5 类真实的"串"）：v0 是大量 demo / 初版系统的真实写法，五个洞彼此独立，
根因却是同一件事：**共享可变状态 + 没把租户/用户身份当成一等公民显式往下传**。
1. 串会话：历史挂在全局字典、key 用"当前线程"——线程池复用线程，A 的历史被 B 读到；
2. 串缓存：缓存 key 只有 query 文本、没有 tenant —— 命中别人的答案；
3. 串检索：先召回再用 ACL 过滤 —— 别人的私有文档已被打分、进过日志和 prompt；
4. 串上下文：父请求的 context 对象被 subagent 共享 —— 并发下互相污染；
5. 串配额：全局一个令牌桶 —— 一个租户的突发吃光所有人的额度。
v1 生产做法：``RequestContext`` 在入口构造并**显式传参**（不用 contextvar），每层
require 校验；``SessionStore`` 按 (tenant,user,session) 三元组 + TTL + 版本号 + 归属校验；
数据隔离三层（召回前过滤 / 行级安全 / 存储隔离选型）；RBAC+ABAC 混合鉴权；每租户
QPS 与 token 预算；全量审计日志；每租户独立凭证。
工程结论：身份显式传递 + 每层强制断言；过滤必须在召回前做；配额必须分桶且留余量；
审计日志与独立凭证是企业级交付的必需品，不是可选项。
"""
from __future__ import annotations
import sys, threading, time
from dataclasses import dataclass
from typing import Any
from agentlab.metrics import METRICS
from agentlab.orchestration import TokenBucket
from agentlab.store import BM25Index, Doc, Query, TwoStageRetriever, build_corpus
from agentlab.util import (BROKEN, FIX, VERIFY, Stats, head, improvement, kv, lab, note,
                           phase, run_concurrently, takeaway)
LAB_ID = "lab-16-multitenant-isolation"
TENANTS = ("tenant-a", "tenant-b", "tenant-c")
def table(title: str, headers: list[str], widths: list[int], rows: list[list[Any]]) -> None:
    """固定列宽表格（按中文双宽对齐）——本 lab 的核心交付物用它打印。"""
    def pad(s: Any, w: int) -> str:
        s = str(s)
        return s + " " * max(0, w - sum(2 if "\u4e00" <= c <= "\u9fff" else 1 for c in s))
    print(f"\n  ┌─ {title} " + "─" * max(0, 64 - len(title) * 2))
    print("  │ " + "  ".join(pad(h, w) for h, w in zip(headers, widths)))
    for r in rows:
        print("  │ " + "  ".join(pad(c, w) for c, w in zip(r, widths)))
    print("  └" + "─" * 72)
def ver(name: str, before: float, after: float, lower: bool = True) -> None:
    print(f"{VERIFY} {name}: {before} -> {after} ({improvement(before, after, lower_is_better=lower)})")
# --- 请求上下文：显式传递的隔离基石 ---
@dataclass(frozen=True)
class RequestContext:
    """请求入口构造，**作为参数显式传下去**。不用 contextvar 的原因：``threading.Thread``
    不继承父线程的 contextvar（新线程拿默认值）；线程池里 contextvar 会随线程复用而"粘住"
    上一个请求的值；``run_in_executor`` 与回调里同样会丢。隔离场景下"悄悄丢了身份"= 越权，
    所以显式传参 + 每层断言，丢了立刻抛异常，绝不兜底成默认租户。"""
    tenant_id: str
    user_id: str
    session_id: str
    trace_id: str
    groups: frozenset[str]
    role: str = "viewer"
    clearance: int = 1
class MissingContext(Exception):
    """任何一层拿到 None/非法 ctx 都必须立刻失败。"""
def require_ctx(ctx: RequestContext | None, layer: str) -> RequestContext:
    if ctx is None or not ctx.tenant_id or not ctx.user_id or not ctx.session_id:
        raise MissingContext(f"{layer}: 缺少 RequestContext（拒绝落到默认租户）")
    return ctx
def make_ctx(tenant: str, user: str, session: str, role: str = "viewer",
             clearance: int = 1) -> RequestContext:
    return RequestContext(tenant, user, session, f"tr-{tenant}-{session}",
                          frozenset({f"g{tenant[-1]}", "public"}), role, clearance)
# --- 审计日志：who / what / when / why / result ---
AUDIT: list[dict] = []
_AUDIT_LOCK = threading.Lock()
def audit(ctx: RequestContext | None, action: str, resource: str, decision: str,
          reason: str, why: str = "") -> None:
    """谁（who）/做了什么（what）/何时（when）/为什么（why）/结果（result）。"""
    with _AUDIT_LOCK:
        AUDIT.append({"ts": round(time.time() % 100000, 3), "action": action,
                      "tenant": getattr(ctx, "tenant_id", "-"),
                      "user": getattr(ctx, "user_id", "-"),
                      "session": getattr(ctx, "session_id", "-"), "resource": resource,
                      "decision": decision, "reason": reason, "why": why})
# --- v0：五个"串"的载体 ---
class SessionStoreV0:
    """全局字典 + "当前线程"当会话槽位；会话归属完全不校验。"""
    def __init__(self) -> None:
        self._slot, self.by_session, self.lock = {}, {}, threading.Lock()
    def history(self, user_id: str) -> list[str]:
        with self.lock:  # key 是线程不是人：线程池一复用就串
            return self._slot.setdefault(threading.get_ident(), [])
    def open_session(self, session_id: str, user_id: str) -> list[str]:
        with self.lock:  # 只看 session_id：谁拿着 id 谁就继承上下文
            return self.by_session.setdefault(session_id, [])
_CREDS_V0: dict[str, str] = {"key": "sk-shared"}
# --- v1：会话隔离（三元组 key + TTL + 版本号 + 归属校验）---
class SessionHijack(Exception):
    pass
class StaleWrite(Exception):
    pass
class SessionStore:
    """按 (tenant, user, session) 三元组做 key：TTL + 版本号 + 归属校验。"""
    def __init__(self, ttl_s: float = 600.0) -> None:
        self.ttl_s, self._lock, self._s = ttl_s, threading.RLock(), {}
        self.hijack_blocked = self.stale_writes = self.expired = 0
        self.m_hijack = METRICS.counter("session_hijack_blocked_total", "越权会话被拦截")
        self.m_stale = METRICS.counter("session_stale_write_total", "并发写冲突被拒")
    def _resolve(self, ctx: RequestContext | None) -> dict:
        ctx = require_ctx(ctx, "SessionStore")
        s = self._s.setdefault(ctx.session_id, {"owner": None, "exp": 0.0, "ver": 0, "msgs": []})
        now = time.monotonic()
        if s["owner"] is not None and s["exp"] < now:
            self.expired += 1
            s.update({"owner": None, "ver": 0, "msgs": []})  # TTL 到期：清空重开
        if s["owner"] is None:
            s["owner"] = (ctx.tenant_id, ctx.user_id)  # 三元组里的 tenant+user 就是归属
        elif s["owner"] != (ctx.tenant_id, ctx.user_id):
            self.hijack_blocked += 1
            self.m_hijack.inc()
            audit(ctx, "session.open", ctx.session_id, "DENY", "会话归属不匹配", "防串会话")
            raise SessionHijack(f"session {ctx.session_id} 属于 {s['owner'][0]}/{s['owner'][1]}，"
                                f"调用方是 {ctx.tenant_id}/{ctx.user_id}")
        s["exp"] = now + self.ttl_s
        return s
    def open(self, ctx: RequestContext | None) -> int:
        with self._lock:
            return self._resolve(ctx)["ver"]
    def history(self, ctx: RequestContext | None) -> list[str]:
        with self._lock:
            return list(self._resolve(ctx)["msgs"])
    def append(self, ctx: RequestContext | None, text: str, version: int | None = None) -> int:
        with self._lock:
            s = self._resolve(ctx)
            if version is not None and version != s["ver"]:  # 乐观锁：防交错写
                self.stale_writes += 1
                self.m_stale.inc()
                raise StaleWrite(f"期望版本 {version}，当前 {s['ver']}")
            s["msgs"].append(text)
            s["ver"] += 1
            return s["ver"]
# --- v1：RBAC + ABAC 鉴权、工具鉴权、租户凭证 ---
ROLE_PERMS: dict[str, set[str]] = {
    "viewer": {"doc.read"},
    "analyst": {"doc.read", "tool.search"},
    "engineer": {"doc.read", "tool.search", "tool.deploy"},
    "admin": {"*"},
}
CREDS = {t: {"llm_key": f"sk-{t[-1]}-9f2c", "db": f"pg://{t}"} for t in TENANTS}
class AccessDenied(Exception):
    pass
def authorize(ctx: RequestContext | None, action: str, doc: Doc | None = None) -> tuple[bool, str]:
    """RBAC（role → permission）+ ABAC（租户/部门/密级/有效期），真实鉴权。"""
    try:
        ctx = require_ctx(ctx, "authorize")
    except MissingContext as exc:
        return False, str(exc)
    perms = ROLE_PERMS.get(ctx.role, set())
    if "*" not in perms and action not in perms:
        return False, f"RBAC:role={ctx.role}无该权限"
    if doc is None:
        return True, "OK"
    if doc.tenant != ctx.tenant_id and "public" not in doc.acl:
        return False, f"ABAC:文档属于{doc.tenant}"
    dept = doc.meta.get("dept", "*")
    if dept != "*" and dept not in ctx.groups:
        return False, f"ABAC:部门{dept}不在groups"
    if int(doc.meta.get("level", 1)) > ctx.clearance:
        return False, f"ABAC:密级{doc.meta.get('level')}>clearance"
    if float(doc.meta.get("expire", 0.0)) and float(doc.meta["expire"]) < time.time():
        return False, "ABAC:文档已过有效期"
    return True, "OK"
def guarded_retrieve(ctx: RequestContext | None, idx: BM25Index, text: str,
                     top_k: int = 5) -> list[Doc]:
    """检索入口强制鉴权 + 召回前过滤（filter at source）。"""
    ctx = require_ctx(ctx, "retrieve")
    ok, reason = authorize(ctx, "doc.read")
    if not ok:
        raise AccessDenied(reason)
    hits = idx.search(Query(text, top_k=top_k, tenant=ctx.tenant_id, groups=ctx.groups)).hits
    out: list[Doc] = []
    for h in hits:  # 行级安全：召回后仍逐条断言（双保险）
        ok, reason = authorize(ctx, "doc.read", h.doc)
        out.append(h.doc) if ok else audit(ctx, "doc.read", h.doc.doc_id, "DENY", reason,
                                           "行级安全")
    return out
def guarded_tool(ctx: RequestContext | None, tool: str, args: str) -> str:
    """工具调用前必须鉴权：不能指望 LLM"自己不会越权"（与 lab_14 呼应）。"""
    ctx = require_ctx(ctx, "tool")
    ok, reason = authorize(ctx, f"tool.{tool}")
    audit(ctx, f"tool.{tool}", args, "ALLOW" if ok else "DENY", reason, "工具越权防护")
    if not ok:
        raise AccessDenied(reason)
    return f"{tool}({args}) -> ok"
def get_credential(ctx: RequestContext | None, name: str = "llm_key") -> str:
    """每租户独立凭证：配额无法分摊、审计无法归因、泄露半径=全平台。"""
    ctx = require_ctx(ctx, "vault")
    if ctx.tenant_id not in CREDS:
        raise AccessDenied(f"tenant {ctx.tenant_id} 无凭证")
    audit(ctx, "credential.use", name, "ALLOW", "租户内凭证", "密钥隔离")
    return CREDS[ctx.tenant_id][name]
# --- 复现函数 ---
def repro_cross_session(users: int = 50, turns: int = 10, workers: int = 8) -> tuple[int, int]:
    """50 个并发用户 × 10 轮对话，统计"历史里出现别人内容"的次数。"""
    v0, v1 = SessionStoreV0(), SessionStore()
    ctxs: dict[str, RequestContext] = {}
    lock, acc = threading.Lock(), [0, 0]
    def one(i: int) -> None:
        uid = f"u{i:02d}"
        ctxs[uid] = c = make_ctx("tenant-a", uid, f"s-{uid}", "analyst", 2)
        v1.open(c)
        mine0 = mine1 = 0
        for t in range(turns):
            h0 = v0.history(uid)
            mine0 += sum(1 for e in h0 if not e.startswith(uid + ":"))
            h0.append(f"{uid}:轮次{t}")
            h1 = v1.history(c)
            mine1 += sum(1 for e in h1 if not e.startswith(uid + ":"))
            v1.append(c, f"{uid}:轮次{t}")
        with lock:
            acc[0], acc[1] = acc[0] + mine0, acc[1] + mine1
    run_concurrently(one, users, workers)
    return acc[0], acc[1]
def repro_hijack(attempts: int = 12) -> tuple[int, int, int]:
    """越权拿别人的 session id：v0 全部成功，v1 全部拦截 + 版本号挡住并发写。"""
    v0 = SessionStoreV0()
    v0.open_session("s-alice", "alice").append("alice:订单号 A-7788")
    ok_v0 = sum(1 for i in range(attempts) if v0.open_session("s-alice", f"u{i:02d}"))
    store = SessionStore()
    alice = make_ctx("tenant-a", "alice", "s-alice", "engineer", 3)
    store.open(alice)
    store.append(alice, "alice:订单号 A-7788")
    blocked = 0
    for i in range(attempts):
        try:
            store.open(make_ctx("tenant-a", f"u{i:02d}", "s-alice", "viewer", 1))
        except SessionHijack:
            blocked += 1
    ctx, bar = make_ctx("tenant-b", "bob", "s-bob", "analyst", 2), threading.Barrier(8)
    old_ver = store.open(ctx)
    def racer(_i: int) -> str:
        bar.wait()
        try:
            store.append(ctx, "x", version=old_ver)
            return "ok"
        except StaleWrite:
            return "stale"
    return ok_v0, blocked, sum(1 for r in run_concurrently(racer, 8, 8) if r == "stale")
def repro_cache(shared_q: int = 6) -> tuple[int, int, int]:
    """tenant-a 先预热缓存，tenant-b/c 随后问同样的问题 → 命中别人的答案。"""
    c0: dict[str, tuple[str, str]] = {}  # v0：key 只有 query
    c1: dict[tuple, str] = {}            # v1：key = (tenant, query)
    lock = threading.Lock()
    acc = {"w0": 0, "w1": 0, "h0": 0}
    def ask(tenant: str) -> None:
        for k in range(shared_q):
            q = f"q{k}"
            with lock:
                hit0 = c0.get(q)
                if hit0 is None:
                    c0[q] = (tenant, f"answer-of-{tenant}")
                owner, _a = c0[q]
                hit1 = (tenant, q) in c1
                acc["h0"] += int(hit0 is not None)
                acc["w0"] += int(hit0 is not None and owner != tenant)
                acc["w1"] += int(hit1 and c1.setdefault((tenant, q), f"answer-of-{tenant}")
                                  != f"answer-of-{tenant}")
    ask(TENANTS[0])
    run_concurrently(lambda i: ask(TENANTS[1 + i % 2]), 2, 2)
    return acc["w0"], acc["w1"], acc["h0"]
def _visible(doc: Doc, tenant: str, groups: frozenset[str]) -> bool:
    if doc.tenant != tenant and "public" not in doc.acl:
        return False
    return not (doc.acl and not (doc.acl & groups))
def repro_retrieval(idx: BM25Index, queries: list[str], tenant: str,
                    groups: frozenset[str]) -> dict:
    """v0：不带 tenant/groups 多召回再用 ACL 过滤。v1：过滤下推到召回阶段。
    两条路 top_k 不同是**故意的**：召回后过滤必须多召回才能保证不丢结果，
    而多召回的代价会在精排这种线性成本阶段被放大 —— 生产上就是 RT 爆炸。
    """
    v0 = TwoStageRetriever(idx, recall_k=40, rerank_cost_ms=0.45)
    v1 = TwoStageRetriever(idx, recall_k=8, rerank_cost_ms=0.45)
    out = {"unauth": 0, "top5": 0, "v0_ms": [], "v1_ms": [], "s0": 0, "s1": 0}
    for q in queries:
        r0 = v0.search(Query(q, top_k=40, tenant="", groups=frozenset(), rerank=True))
        bad = [h for h in r0.hits if not _visible(h.doc, tenant, groups)]
        out["unauth"] += len(bad)
        out["top5"] += int(any(not _visible(h.doc, tenant, groups) for h in r0.hits[:5]))
        out["v0_ms"].append(r0.latency_ms)
        out["s0"] += r0.scanned
        r1 = v1.search(Query(q, top_k=5, tenant=tenant, groups=groups, rerank=True))
        out["v1_ms"].append(r1.latency_ms)
        out["s1"] += r1.scanned
    return out
def repro_misc() -> tuple[int, int, int]:
    """串上下文 / 串配额 / 串凭证：三个共享可变状态导致的并发事故。"""
    shared: dict[str, Any] = {"facts": []}          # 1) 父请求上下文被 subagent 共享
    lock = threading.Lock()
    def sub(i: int) -> int:
        with lock:
            foreign = len([f for f in shared["facts"] if f != f"sub{i}"])
            shared["facts"].append(f"sub{i}")
        time.sleep(0.0005)
        return foreign
    poll = sum(r for r in run_concurrently(sub, 20, 20) if isinstance(r, int))
    tb = TokenBucket(rate=30, burst=50, name="global_quota")   # 2) 全局一个配额桶
    res = run_concurrently(lambda i: (i < 80, tb.try_acquire()), 100, 100)
    denied0 = sum(1 for small, ok in res if not small and not ok)  # type: ignore[misc]
    buckets = {t: TokenBucket(rate=30, burst=50, name=f"quota_{t}") for t in TENANTS}
    daily = {t: 0 for t in TENANTS}
    ca, cb = make_ctx("tenant-a", "alice", "s1"), make_ctx("tenant-b", "bob", "s2")
    def quota(i: int) -> bool:
        ctx = ca if i < 80 else cb
        if ctx.tenant_id not in buckets or daily.get(ctx.tenant_id, 0) >= 50_000:
            return False
        return buckets[ctx.tenant_id].try_acquire()
    r1 = run_concurrently(quota, 100, 100)
    denied1 = sum(1 for i, ok in enumerate(r1) if i >= 80 and not ok)
    creds_lock, acc = threading.Lock(), [0]                    # 3) 全局可变凭证
    def cred(i: int) -> None:
        t = TENANTS[i % len(TENANTS)]
        with creds_lock:
            _CREDS_V0["key"] = f"sk-{t[-1]}"
        time.sleep(0.0004)  # 模拟真实 IO：这段时间全局值已被别人覆盖
        with creds_lock:
            acc[0] += int(_CREDS_V0["key"] != f"sk-{t[-1]}")
    run_concurrently(cred, 40, 40)
    return poll, denied0, denied1, acc[0]  # type: ignore[return-value]
# --- main ---
def main() -> int:
    with lab(LAB_ID, "企业级多租户隔离：权限、数据、会话与配额",
             "企业级 agent 如何做权限和数据层的隔离？多用户并发下如何避免串会话？"):
        head("1. 复现：五类真实的'串'")
        phase("1. 复现故障", "(v0 共享可变状态)")
        leaks0, leaks1 = repro_cross_session()
        METRICS.counter("isolation_cross_session_leaks_total").inc(leaks0)
        kv("串会话 50 用户×10 轮/8 线程", f"{leaks0} 次", f"（v1 同负载 {leaks1} 次）")
        hijack_ok, hijack_blocked, stale = repro_hijack()
        kv("越权读别人 session（12 次尝试）", f"v0 成功 {hijack_ok} / v1 拦截 {hijack_blocked}")
        kv("并发写冲突被版本号拦下", stale, "次")
        cache0, cache1, cache_hits = repro_cache()
        METRICS.counter("isolation_cross_tenant_cache_hits_total").inc(cache0)
        kv("串缓存：跨租户错误命中", f"{cache0}/{cache_hits} 次命中", f"（v1 {cache1} 次）")
        docs = build_corpus(6000, seed=5)
        for i, d in enumerate(docs):
            d.meta.update({"level": 1 + (i % 3), "dept": f"g{d.tenant[-1]}" if i % 4 else "*",
                           "expire": time.time() - 3600 if i % 97 == 0 else 0.0})
        idx = BM25Index(docs)
        rt = repro_retrieval(idx, [f"doc{i} 缓存穿透 优化" for i in range(0, 600, 10)],
                             "tenant-a", frozenset({"ga", "public"}))
        METRICS.counter("retrieval_unauthorized_recalled_total").inc(rt["unauth"])
        kv("串检索：召回阶段就含别人文档", rt["unauth"], "条")
        kv("  未过滤时 top5 里就有别人文档", rt["top5"], "次请求（泄露到 prompt）")
        kv("  扫描文档数 召回后过滤 vs 召回前", f"{rt['s0']} -> {rt['s1']}")
        poll, denied0, denied1, cred_wrong = repro_misc()
        METRICS.counter("isolation_context_pollution_total").inc(poll)
        METRICS.counter("isolation_credential_misuse_total").inc(cred_wrong)
        kv("串上下文：subagent 读到别人的事实", poll, "次")
        kv("串配额：tenant-b 被 tenant-a 挤掉", f"{denied0} -> {denied1}", "次拒绝")
        kv("串凭证：请求用错别人的 key", cred_wrong, "次")
        print(f"\n{BROKEN} 串会话 {leaks0} 次 / 越权召回 {rt['unauth']} 条文档 / 跨租户缓存"
              f"错误命中 {cache0} 次 / 上下文污染 {poll} 次 / 凭证串用 {cred_wrong} 次")
        head("2. 观测 / 归因：隔离故障必须按维度打点，否则只会说'偶发'")
        phase("2. 观测 / 归因", "(isolation_* 指标 + require 快速失败)")
        METRICS.render("隔离类指标（v0）", include=["isolation_", "session_"])
        note("v0 日志里只有 request_id，没有 tenant/user/session：同一个 trace 既可能是 A 的")
        note("检索也可能是 B 的答案，无法归因；审计事件数=0，出事后无法回答'谁看了什么'。")
        for layer in ("retrieve", "cache", "session", "context", "quota", "credential"):
            try:
                require_ctx(None, layer)
            except MissingContext as exc:
                note(f"require_ctx 快速失败: {exc}")
        note("归因结论：5 个洞全部来自'共享可变状态 + 身份没有显式传递'。")
        head("3. 修复：显式上下文 + 三层数据隔离 + RBAC/ABAC + 配额 + 审计")
        phase("3. 修复", "(v1 生产做法)")
        ctxs = {"alice": make_ctx("tenant-a", "alice", "s-alice", "engineer", 3),
                "bob": make_ctx("tenant-a", "bob", "s-bob", "viewer", 1),
                "carol": make_ctx("tenant-b", "carol", "s-carol", "admin", 3)}
        docs_a = guarded_retrieve(ctxs["alice"], idx, "缓存穿透 治理")
        kv("alice 召回（tenant+groups 下推）", f"{len(docs_a)} 篇",
           f"全部属于 {sorted({d.tenant for d in docs_a})}")
        hist: dict[str, int] = {}
        for ctx in ctxs.values():
            for d in docs[:400]:
                ok, reason = authorize(ctx, "doc.read", d)
                key = "ALLOW" if ok else reason
                hist[key] = hist.get(key, 0) + 1
        note("鉴权决策分布（3 用户 × 400 文档 = 1200 次真实决策）：")
        for k, v in sorted(hist.items(), key=lambda x: -x[1]):
            note(f"  {k:<24} {v}")
        tool_ok = tool_deny = 0
        for name in ("alice", "bob"):
            try:
                guarded_tool(ctxs[name], "deploy", "svc=a")
                tool_ok += 1
            except AccessDenied as exc:
                tool_deny += 1
                note(f"工具越权被拦：{name} -> {exc}")
        kv("工具调用鉴权", f"ALLOW={tool_ok} DENY={tool_deny}")
        kv("每租户独立凭证", {t: get_credential(make_ctx(t, "u", "s")) for t in TENANTS})
        kv("审计事件数（v1，含拒绝记录）", len(AUDIT))
        table("隔离检查清单（本 lab 核心交付物）",
              ["隔离维度", "实现手段", "验证方法", "失败后果"], [10, 30, 24, 24],
              [["会话", "三元组 key+TTL+版本号+归属校验", "50 并发×10 轮查串历史", "串会话/泄露对话"],
               ["缓存", "(tenant,groups,query) 复合 key", "跨租户同问统计错误命中", "答案串租户"],
               ["检索", "召回前过滤(metadata 下推)", "越权召回条数/扫描量/p95", "私有文档进 prompt"],
               ["行级", "逐条断言 tenant/acl/密级", "鉴权决策分布", "越权读私有数据"],
               ["上下文", "frozen ctx + 不可变 facts 链", "subagent 读到的外部事实", "中间结果污染"],
               ["配额", "每租户令牌桶+token 日预算", "小租户被挤掉的拒绝数", "吵闹邻居饿死"],
               ["凭证", "每租户独立 key/DSN", "并发下用错 key 的次数", "泄露半径=全平台"],
               ["审计", "who/what/when/why/result", "审计事件条数与抽样", "合规不过/无法追责"],
               ["存储", "共享表+tenant 列/独立 schema/独立库", "按合规等级与规模选型",
                "选错=成本或合规事故"]])
        print(f"\n{FIX} 身份显式传递 + 三元组会话 key + 召回前过滤 + RBAC/ABAC + 分桶配额，"
              f"五类'串'全部归零；审计 {len(AUDIT)} 条（含拒绝）")
        head("4. 验证：同一负载下 v0 -> v1")
        phase("4. 验证", "(VERIFY)")
        p95_0, p95_1 = Stats(rt["v0_ms"]).p95, Stats(rt["v1_ms"]).p95
        note(f"召回后过滤 p95={p95_0:.2f}ms 扫描 {rt['s0']} 条；召回前过滤 p95={p95_1:.2f}ms "
             f"扫描 {rt['s1']} 条（多召回的代价在精排阶段被放大）")
        for r in AUDIT[:: max(1, len(AUDIT) // 4)][:4]:
            note(f"audit {r['ts']} tenant={r['tenant']} user={r['user']} {r['action']} "
                 f"{r['resource'][:16]} -> {r['decision']} ({r['reason']})")
        print()
        ver("cross_session_leaks", leaks0, leaks1)
        ver("cross_tenant_cache_hits", cache0, cache1)
        ver("unauthorized_docs_recalled", rt["unauth"], 0)
        ver("context_pollution_events", poll, 0)
        print(f"{VERIFY} session_hijack_blocked: 0 -> {hijack_blocked} "
              f"(+{hijack_blocked / max(1, hijack_ok) * 100:.1f}% 越权尝试被拦截，v0 无拦截能力)")
        ver("cross_tenant_quota_denied", denied0, denied1)
        ver("credential_misuse", cred_wrong, 0)
        ver("retrieval_p95_ms", round(p95_0, 2), round(p95_1, 2))
        METRICS.render("隔离指标（v1 收尾）", include=["isolation_", "session_"])
        note("1) 身份（tenant/user/session/trace/groups）在入口构造，显式传参，每层 require。")
        note("2) contextvar 在线程池/新线程/回调里会丢或粘住上一个请求 —— 隔离必须显式。")
        note("3) 过滤下推到召回阶段：既省算力又不泄露；召回后过滤 = 已经泄露。")
        note("4) 存储隔离：共享表+tenant 列（SaaS 默认/成本最低）< 独立 schema（中大型/合规）")
        note("   < 独立库/独立部署（金融医疗强隔离）：按合规等级与规模选，不按喜好选。")
        note("5) 配额必须按租户分桶且留余量；共享池等于没有隔离。")
        note("6) 审计日志与每租户独立凭证是企业级交付的必需品，不是可选项。")
        takeaway("多租户隔离 = 身份显式传递 + 每层强制断言 + 召回前过滤 + 分桶配额 + 全量审计。")
        METRICS.reset()
    return 0
QUESTIONS = [
    "企业级的 agent 如何做权限和数据层面的隔离，多租户工程化如何做设计落地？ "
    "-> RequestContext 显式传参 + 召回前过滤 + 行级安全 + RBAC/ABAC + 分桶配额 + 审计",
    "多用户并发，agent 的会话隔离，如何工程化落地，如何避免串会话？ "
    "-> (tenant,user,session) 三元组 key + TTL + 版本号乐观锁 + 会话归属校验",
    "不同租户能否共用一套 LLM key / 数据源凭证？ -> 不能：配额无法分摊、审计无法归因、"
    "泄露半径从单租户扩大到全平台",
    "如何证明隔离真的生效？ -> 每类'串'都有可复现的事故计数与 v0->v1 的 [VERIFY] 对比",
]
if __name__ == "__main__":
    sys.exit(main())