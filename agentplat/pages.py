"""各标签页的内容渲染。

每个页面都遵循同一条原则：**先给结论，再给数据，最后给"这说明什么"。**
只有数字没有解释的看板，等于没有看板。
"""

from __future__ import annotations

import json
import time

from agentlab.metrics import METRICS

from . import ui
from .cache import CACHE_POLICY
from .context import ContextBuilder, RequestContext
from .engine import AgentRequest
from .ui import bars, card, esc, metric, page, pill, table


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _ms(x: float) -> str:
    return f"{x:,.1f}ms"


def _usd(x: float) -> str:
    return f"${x:,.6f}"


# ==========================================================================
# 1. 对话工作台
# ==========================================================================


def console(demo, qs: dict, resp=None) -> bytes:
    cfg = demo.llm_cfg
    tenants = sorted(demo.tenants)
    tenant_opts = "".join(
        f'<option value="{esc(t)}" {"selected" if qs.get("tenant") == t else ""}>{esc(t)}</option>'
        for t in tenants
    )
    persona = qs.get("persona", "general")
    persona_opts = "".join(
        f'<option value="{esc(k)}" {"selected" if persona == k else ""}>{esc(v[0])}</option>'
        for k, v in ContextBuilder.PERSONAS.items()
    )
    mode = "真实 LLM" if cfg.provider == "real" and cfg.model_or("mid") else "内置模拟器"
    personas = ContextBuilder.PERSONAS
    persona_note = personas.get(persona, personas["general"])[0]

    form = f"""
<form action="/" method=get>
  <div class=row>
    <div class=field style="flex:1;min-width:320px">
      <label>问题</label>
      <input name=q value="{esc(qs.get('q', '用 Python 写一个俄罗斯方块的核心下落与消行逻辑'))}"
             placeholder="问点什么… 写代码、解释概念、做分析都可以">
    </div>
    <div class=field><label>人格</label><select name=persona>{persona_opts}</select></div>
    <div class=field><label>租户</label><select name=tenant>{tenant_opts}</select></div>
    <div class=field><label>用户</label>
      <input name=user value="{esc(qs.get('user','u1'))}" size=5></div>
    <div class=field><label>会话</label>
      <input name=session value="{esc(qs.get('session','s1'))}" size=5></div>
    <button>提问</button>
  </div>
  <div class=row style="margin-top:8px">
    <div class=field>
      <label style="text-transform:none;color:{ui.PALETTE['dim']}">
        <input type=checkbox name=rag value=1 {"checked" if qs.get("rag") else ""}>
        注入内部知识库检索（RAG）</label>
    </div>
    <div class=field>
      <label style="text-transform:none;color:{ui.PALETTE['dim']}">
        <input type=checkbox name=tools value=1 {"checked" if qs.get("tools") else ""}>
        允许 agent 调用工具</label>
    </div>
  </div>
  <div class=hint>
    后端：<b>{esc(mode)}</b>
    {'· <span class=mono>' + esc(cfg.model_or('mid')) + '</span>' if mode == '真实 LLM' else
     '· 回答是本地模板文本，关注链路行为而非答案质量。<a href="/settings">接入真实模型 →</a>'}
    · 人格：<b>{esc(persona_note)}</b>
    <br>默认<b>不检索</b>：agent 首先是通用的，绝大多数问题不需要知识库。
    需要内部制度/资料问答时再勾选 RAG，或切到「严格检索」人格（无依据即拒答）。
  </div>
</form>"""

    # ---- 回答区 ----
    result = ""
    if resp is not None:
        tone = "ok" if resp.ok else "bad"
        body = esc(resp.answer) if resp.ok else esc(resp.error)
        spans = [(n, ms, st) for n, ms, st, _ in resp.spans]
        result = f"""
<h2>回答</h2>
<div class="answer {'' if resp.ok else 'err'}">{body}</div>
<div class="grid g4">
  {metric("端到端", _ms(resp.latency_ms), "trace " + resp.trace_id)}
  {metric("模型", esc(resp.model or "—"), "内部档位 → 真实模型")}
  {metric("tokens", f"{resp.tokens_in} / {resp.tokens_out}", "输入 / 输出")}
  {metric("本次成本", _usd(resp.usd), "缓存命中" if resp.cached else "未命中缓存")}
  {metric("缓存", esc(resp.cache_layer or "未命中"), "L1 精确 / L2 语义")}
  {metric("上下文", f"{resp.context_tokens}", "组装后的 prompt tokens")}
  {metric("工具调用", f"{resp.tool_calls}", "有界循环保护")}
  {metric("重试", f"{resp.retries}", "同一预算内的重试")}
</div>
<h2>这一秒里发生了什么（span 归因）</h2>
{card('<div class=spans>' + _span_lines(resp) + '</div>')}
<div class=hint>点 <a href="/history">请求历史</a> 可以回看每一次请求的完整 trace。</div>
"""

    intro = f"""
<h1>对话工作台</h1>
<p class=lead>这是一个<b>通用 agent</b>：写代码、解释概念、做分析都可以。
检索增强（RAG）是可选加分项，不是前提 —— 不勾选就是普通对话，
勾选后把内部知识库的片段作为<b>参考资料</b>注入（资料不足时模型会用自身知识补充）。</p>
{card(form)}
{result}"""

    return page("对话工作台", "console", intro)


def _span_lines(resp) -> str:
    if not resp.spans:
        return '<div class=empty>这次请求没有记录 span（可能是缓存命中提前返回）</div>'
    peak = max((ms for _, ms, _, _ in resp.spans), default=1.0) or 1.0
    out = []
    for name, ms, status, attrs in resp.spans:
        pct = min(100.0, ms / peak * 100.0)
        tone = "ok" if status == "OK" else "bad"
        extra = " ".join(f"{k}={v}" for k, v in list(attrs.items())[:3]) if attrs else ""
        out.append(
            f'<div class=r><span class=n>{esc(name)}</span>'
            f'<span class=barwrap><span class=bar><i style="width:{pct:.1f}%"></i></span>'
            f'<span class="mono {tone}">{ms:>8,.1f}ms</span></span>'
            f'<span class=dim>{esc(extra)}</span></div>'
        )
    return "".join(out)


# ==========================================================================
# 2. 模型与路由
# ==========================================================================


def models(demo) -> bytes:
    cfg = demo.llm_cfg
    eng = demo.engine
    tier = cfg.tier_map()
    rows = []
    for name, spec in demo.engine.server.models.items():
        real = tier.get(name, "—") or "—"
        rows.append([
            f'<span class="mono">{esc(name)}</span>',
            pill(spec.tier, "info"),
            f'<span class="mono">{esc(real)}</span>',
            f"{spec.max_parallel}",
            f"{spec.in_price:g} / {spec.out_price:g}",
            f"{spec.quality:.2f}",
        ])

    counts = eng.router.counts or {}
    total = sum(counts.values()) or 1
    dist = "".join(
        f'<div class=r><span class=n>{esc(k)}</span>'
        f'<span class=barwrap><span class=bar><i style="width:{v / total * 100:.1f}%"></i></span>'
        f'<span class=mono>{v}</span></span>'
        f'<span class=dim>{_pct(v / total)}</span></div>'
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    ) or '<div class=empty>还没有请求经过路由</div>'

    body = f"""
<h1>模型与路由</h1>
<p class=lead>平台内部只认三个<b>档位</b>（small / mid / large），真实模型名由这张映射表决定。
换厂商只改这张表，路由、降级链、预算闸门的逻辑一行都不用动 —— 这就是
<a href="/settings">LLM 设置</a> 里"档位映射"的意义。</p>

<h2>档位 → 真实模型</h2>
{card(table(["内部档位", "层级", "真实模型名", "并发上限", "价格 $/1M (in/out)", "质量分"],
            rows, numeric=(3, 4, 5)))}

<div class="grid g2">
  <div>
    <h2>路由决策分布</h2>
    {card('<div class=spans>' + dist + '</div>'
          + f'<div class=hint>策略 <span class=mono>{esc(cfg.provider)}</span> · '
            f'升级 {eng.router.escalations} 次 · 降级 {eng.router.downgrades} 次 · '
            f'兜底 {eng.router.fallbacks} 次</div>')}
  </div>
  <div>
    <h2>复杂度分档规则（决定起始档位）</h2>
    {card('''<div class=kv>
      <dt>prompt &gt; 4000 tokens</dt><dd>+2 分</dd>
      <dt>prompt &gt; 1600 tokens</dt><dd>+1 分</dd>
      <dt>需要工具</dt><dd>+1 分</dd>
      <dt>多跳（候选多且命中 ≥5）</dt><dd>+2 分</dd>
      <dt>总分 ≥3</dt><dd>''' + pill("hard → mid/large", "warn") + '''</dd>
      <dt>总分 ≥1</dt><dd>''' + pill("medium → small", "info") + '''</dd>
      <dt>总分 0</dt><dd>''' + pill("easy → small", "ok") + '''</dd>
    </div>
    <div class=hint><b>阈值就是容量规划</b>：分档越松，越贵的模型占比越高。
    生产上要拿真实流量回放来调，不能凭感觉设。</div>''')}
  </div>
</div>

<h2>降级链（熔断后谁来给答案）</h2>
{card('''<div class=kv>
  <dt>正常</dt><dd>按复杂度选档位</dd>
  <dt>大模型熔断</dt><dd>↓ 降级到 mid</dd>
  <dt>mid 也熔断</dt><dd>↓ 降级到 small</dd>
  <dt>全链路熔断</dt><dd>↓ 兜底（缓存 / 模板答案）+ <b>如实告知降级</b></dd>
</div>
<div class=hint>熔断只解决"不发无用请求"，它<b>不产生答案</b>。没有降级链时，
用户看到的是"更快地失败"而不是"更快地成功"。</div>''')}
"""
    return page("模型与路由", "models", body)


# ==========================================================================
# 3. 缓存
# ==========================================================================


def cache_page(demo, qs: dict) -> bytes:
    c = demo.engine.cache
    exact, sem, tool = c.exact, c.semantic, c.tools

    def hit_ratio(cache) -> str:
        total = cache.hits + cache.misses
        return _pct(cache.hits / total) if total else "—"

    stats = f"""
<div class="grid g4">
  {metric("L1 精确命中率", hit_ratio(exact), f"{exact.hits} 命中 / {exact.misses} 未命中")}
  {metric("L2 语义命中率", hit_ratio(sem), f"命中 {sem.hits} · 错误 {sem.wrong_hits}")}
  {metric("L4 工具缓存", hit_ratio(tool), f"条目 {len(tool._data)}")}
  {metric("累计省下", _ms(c.saved_latency_ms), _usd(c.saved_usd))}
</div>"""

    guard = f"""
<div class="grid g3">
  {metric("判别性护栏拦截", f"{sem.blocked_by_entity_guard}", "相似但实体不同 → 拦下")}
  {metric("低于阈值拒绝", f"{sem.rejected_below_threshold}", "相似度不够")}
  {metric("错误命中", f"{sem.wrong_hits}", "必须为 0 —— 这是事故指标")}
</div>"""

    policy_rows = [
        [esc(r.data_kind), pill("可缓存", "ok") if r.cacheable else pill("禁止", "bad"),
         esc(r.layer), f"{r.ttl_s:.0f}s" if r.cacheable else "—",
         f'<span class=dim>{esc(r.reason)}</span>']
        for r in CACHE_POLICY
    ]

    probe = ""
    if qs.get("probe"):
        probe = _cache_probe(demo, qs["probe"])

    body = f"""
<h1>缓存</h1>
<p class=lead>五层缓存，风险完全不同：L1 精确缓存<b>不会答错</b>，L2 语义缓存
<b>有答错风险</b>，所以两者的监控指标必须分开看。下面第三张卡是"能不能缓存"的
声明式策略表 —— <b>这是工程硬编码的决策，不问模型</b>。</p>

<h2>实时命中情况</h2>
{stats}

<h2>L2 语义缓存的安全护栏</h2>
{guard}
{card('''<div class=hint style="margin:0">
字符级相似度<b>无法分离"同义改写"和"危险近似"</b>：实测"缓存穿透怎么治理 ↔ 缓存击穿怎么治理"
相似度 0.333，而真正的改写"缓存穿透怎么治理 ↔ 缓存穿透的治理方案"只有 0.308 ——
<b>危险近似反而更高</b>。所以本实现取最保守策略（判别性覆盖度门槛 1.0）：
<b>接受大量漏召回，换取零错误命中</b>。漏召回只是少省钱，错误命中是把 A 的答案给了 B。
要做真语义缓存必须上 embedding。
</div>''')}

<h2>缓存实时探测（手工验一层一层怎么命中）</h2>
{card(f'''<form action=/cache method=get>
  <div class=row>
    <div class=field style="flex:1;min-width:300px"><label>问题</label>
      <input name=probe value="{esc(qs.get('probe','会话隔离怎么工程化落地'))}"></div>
    <div class=field><label>仓租户</label>
      <input name=tenant value="{esc(qs.get('tenant','alpha'))}" size=8></div>
    <button>探测</button>
  </div>
  <div class=hint>同一个问题连点两次：第一次落 LLM，第二次 L1 命中（延迟从几百毫秒掉到 0.1ms）。
  换个租户再点：不会复用缓存。</div>
</form>''')}
{probe}

<h2>能不能缓存：策略表（核心交付物）</h2>
{card(table(["数据类型", "可否缓存", "层", "TTL", "理由"], policy_rows, numeric=(3,)))}
"""
    return page("缓存", "cache", body)


def _cache_probe(demo, q: str) -> str:
    """同一个问题打多次 + 换租户，把分层命中过程摊开。"""
    t = demo.tenants.get("alpha")
    rows = []
    seen_other = False
    for i in range(3):
        resp = demo.handle_ask(q, "alpha", "u1", f"probe{i % 2}")
        rows.append([
            f"alpha #{i + 1}", f'<span class="mono">{esc(resp.model)}</span>',
            pill(resp.cache_layer, "ok") if resp.cached else pill("未命中", "warn"),
            _ms(resp.latency_ms), _usd(resp.usd),
        ])
    for tenant in ("beta",):
        if t is not None:
            resp = demo.handle_ask(q, tenant, "u1", "probe1")
            rows.append([
                f"{tenant} #1", f'<span class="mono">{esc(resp.model)}</span>',
                pill(resp.cache_layer, "ok") if resp.cached else pill("未命中（隔离生效）", "info"),
                _ms(resp.latency_ms), _usd(resp.usd),
            ])
            seen_other = resp.cached
    note = (
        '<div class=hint>期望：alpha 第 1 次未命中 → 第 2/3 次 L1 命中；'
        'beta 第 1 次<b>不命中</b>（tenant 是 cache key 的一部分）。</div>'
    )
    return card(
        table(["请求", "模型", "缓存", "延迟", "成本"], rows, numeric=(3, 4)) + note
    )


# ==========================================================================
# 4. 熔断与限流
# ==========================================================================


def resilience(demo) -> bytes:
    eng = demo.engine
    srv = eng.server
    st = dict(srv.stats)

    # 熔断器状态
    br = eng.breakers._breakers
    br_rows = []
    for name, cb in sorted(br.items()):
        s = cb.st
        tone = {"closed": "ok", "open": "bad", "half_open": "warn"}.get(s.state, "")
        br_rows.append([
            f'<span class="mono">{esc(name)}</span>',
            pill(s.state, tone),
            f"{s.failures}", f"{s.opened_count}", f"{s.rejected}",
            f'<span class=dim>阈值 {cb.failure_threshold} · 冷却 {cb.cooldown_s:.1f}s · '
            f'慢调用 {cb.slow_call_ms or 0:.0f}ms</span>',
        ])

    # 限流桶
    rl_rows = []
    for store, tag in ((eng.ratelimit._tenant, "租户"),
                       (eng.ratelimit._model, "模型"),
                       (eng.ratelimit._tool, "工具")):
        for k, b in sorted(store.items()):
            rl_rows.append([
                pill(tag, "info"), f'<span class="mono">{esc(k)}</span>',
                f"{b.rate:,.1f}/s", f"{b.burst:,.0f}", f"{b.tokens:,.1f}", f"{b.denied}",
            ])
    rl_rows.insert(0, [
        pill("全局", "info"), '<span class="mono">global_bucket</span>',
        f"{eng.ratelimit.global_bucket.rate:,.1f}/s",
        f"{eng.ratelimit.global_bucket.burst:,.0f}",
        f"{eng.ratelimit.global_bucket.tokens:,.1f}",
        f"{eng.ratelimit.global_bucket.denied}",
    ])

    # 舱壁
    bh_rows = []
    for label, bh in (
        ("全局", eng.bulkheads.global_pool),
        ("批处理", eng.bulkheads.batch_pool),
        *[(t, b) for t, b in sorted(eng.bulkheads._tenant.items())],
    ):
        used = bh.inflight
        pct = used / bh.limit * 100 if bh.limit else 0
        bh_rows.append([
            esc(label),
            f'<span class=barwrap><span class=bar><i style="width:{pct:.1f}%"></i></span>'
            f'<span class=mono>{used}/{bh.limit}</span></span>',
            f"{bh.max_used}", f"{bh.rejected}",
        ])

    body = f"""
<h1>熔断与限流</h1>
<p class=lead>这一层是"demo"和"生产"的分界线：上游出问题时，它保证你的系统
<b>不会跟着一起死</b>。下面所有数字都是实时的，可以用右侧按钮现场制造故障来观察。</p>

<h2>实时状态</h2>
<div class="grid g4">
  {metric("在飞请求", f"{int(st.get('inflight_now', 0))}", f"并发峰值 {int(st.get('max_inflight', 0))}")}
  {metric("排队中", f"{int(st.get('queued_now', 0))}", f"排队峰值 {int(st.get('max_queued', 0))}")}
  {metric("429 拒绝", f"{int(st.get('rejected_429', 0))}", "上游限流或队列满")}
  {metric("客户端超时", f"{int(st.get('client_timeouts', 0))}", f"其中服务端仍在跑 {int(st.get('orphaned_workers', 0))}")}
</div>

<h2>熔断器</h2>
{card(table(["熔断器", "状态", "连续失败", "打开次数", "拒绝次数", "配置"], br_rows, numeric=(2, 3, 4))
      + '<div class=hint>半开（half_open）时<b>只放 1 个探针</b>：放量会把刚恢复的上游二次打死，'
        '于是永久卡在 open/half_open 循环里。</div>')}

<h2>故障注入（现场观察）</h2>
<div class="grid g2">
  {card('''<h3>把上游打坏</h3>
    <form action=/resilience method=get>
      <div class=row>
        <div class=field><label>错误率</label><input name=error_rate value="0.85" size=6></div>
        <div class=field><label>p50 延迟(ms)</label><input name=p50_ms value="3000" size=7></div>
        <div class=field><label>熔断阈值</label><input name=threshold value="3" size=4></div>
        <div class=field><label>冷却(s)</label><input name=cooldown value="2" size=4></div>
        <button class=danger formaction="/admin/degrade" formmethod=get>注入劣化</button>
      </div>
      <div class=hint>阈值调低是因为<b>手工点请求是串行的</b>：每次失败要 3 秒，默认阈值 6
      在 30 秒内凑不够。生产上并发几十上百，几秒就累计够了。</div>
    </form>
    <div class=row style="margin-top:10px">
      <a href="/admin/recover"><button class=ghost type=button>恢复上游</button></a>
      <a href="/admin/hang"><button class=ghost type=button>模拟"挂死"（永不返回）</button></a>
    </div>''')}
  {card('''<h3>怎么读这些数字</h3>
    <div class=kv>
      <dt>熔断打开</dt><dd>请求被<b>瞬间拒绝</b>（&lt;5ms），不再占线程、不再花钱</dd>
      <dt>失败次数累积</dt><dd>连续失败到阈值才打开；成功后清零</dd>
      <dt>慢调用也算失败</dt><dd>"不报错但很慢"比报错更能拖死自己</dd>
      <dt>客户端超时 ≠ 结束</dt><dd>客户端走了，上游仍在跑、token 照样计费</dd>
      <dt>限流被打到</dt><dd>看"拒绝次数"增长 —— 这是保护生效，不是故障</dd>
    </div>''')}
</div>

<h2>限流桶（四层）</h2>
{card(table(["层", "桶", "速率", "突发容量", "当前令牌", "拒绝次数"], rl_rows, numeric=(2, 3, 4, 5))
      + '<div class=hint>四层各保护不同对象：全局保护自己 · 租户做公平 · 模型保护上游配额 · 工具保护下游依赖。'
        '<b>把并发上限当 QPS 用会把限流器配错十几倍</b>（真实吞吐 ≈ 并发 / 平均延迟）。</div>')}

<h2>舱壁（并发闸门）</h2>
{card(table(["池", "占用", "峰值", "拒绝"], bh_rows, numeric=(2, 3))
      + '<div class=hint>满了就<b>快速失败</b>，而不是让请求烂在内存队列里 —— '
        '这是"并发一高就 panic"的直接解药。</div>')}
"""
    return page("熔断与限流", "resilience", body, refresh_s=15)


# ==========================================================================
# 5. 请求历史
# ==========================================================================


def history(demo) -> bytes:
    with demo._lock:
        rows_raw = list(reversed(demo.history))
    rows = []
    for r in rows_raw[:120]:
        tone = "ok" if r.ok else "bad"
        rows.append([
            f'<a href="/trace/{esc(r.trace_id)}"><span class=mono>{esc(r.trace_id)}</span></a>',
            pill("OK", "ok") if r.ok else pill(esc((r.error or "ERR").split(":")[0]), "bad"),
            f'<span class=mono>{esc(r.model or "—")}</span>',
            _ms(r.latency_ms),
            pill(r.cache_layer, "info") if r.cached else '<span class=dim>否</span>',
            f"{r.tokens_in + r.tokens_out}",
            _usd(r.usd),
            f"{r.context_tokens}",
            f"{r.tool_calls}",
        ])
    ok = sum(1 for r in rows_raw if r.ok)
    body = f"""
<h1>请求历史</h1>
<p class=lead>点 trace_id 看那一次请求的完整 span 树 —— 排查线上问题靠的就是这个。
这里刻意保留了<b>失败请求</b>的耗时与成本：把它们算成 0 会让 P95 和账单看起来很美。</p>
<div class="grid g4">
  {metric("请求数", f"{len(rows_raw)}", "本进程累计")}
  {metric("成功率", _pct(ok / len(rows_raw)) if rows_raw else "—", f"{ok} 成功")}
  {metric("缓存命中", f"{sum(1 for r in rows_raw if r.cached)}", "跨全部租户")}
  {metric("累计成本", _usd(demo.engine.server.ledger.usd), "含失败请求的计费")}
</div>
<h2>最近 {len(rows)} 条</h2>
{card(table(["trace", "结果", "模型", "延迟", "缓存", "tokens", "成本", "上下文", "工具"],
            rows, numeric=(3, 5, 6, 7, 8), empty="还没有请求，去 <a href='/'>对话工作台</a> 问一个"))}
"""
    return page("请求历史", "history", body, refresh_s=20)


def trace_page(demo, trace_id: str) -> bytes:
    r = demo.find(trace_id)
    if r is None:
        return page("未找到", "history",
                    f"<h1>没有这个 trace</h1><p class=mono>{esc(trace_id)}</p>"
                    f"<p><a href='/history'>← 回请求历史</a></p>")
    body = f"""
<h1>trace <span class=mono>{esc(trace_id)}</span></h1>
{card(f'''<div class=kv>
  <dt>结果</dt><dd>{pill("OK", "ok") if r.ok else pill(esc(r.error[:80]), "bad")}</dd>
  <dt>端到端</dt><dd class=mono>{_ms(r.latency_ms)}</dd>
  <dt>模型</dt><dd class=mono>{esc(r.model or "—")}</dd>
  <dt>缓存</dt><dd>{esc(r.cache_layer or "未命中")}</dd>
  <dt>tokens</dt><dd class=mono>{r.tokens_in} / {r.tokens_out}</dd>
  <dt>成本</dt><dd class=mono>{_usd(r.usd)}</dd>
  <dt>上下文</dt><dd class=mono>{r.context_tokens} tokens</dd>
  <dt>工具 / 重试</dt><dd class=mono>{r.tool_calls} / {r.retries}</dd>
  <dt>降级</dt><dd>{"是" if r.degraded else "否"}</dd>
</div>''')}
{f'<div class="answer">{esc(r.answer)}</div>' if r.ok and r.answer else ''}
<h2>span 归因</h2>
{card('<div class=spans>' + _span_lines(r) + '</div>')}
<h2>原始 span 数据</h2>
<pre>{esc(json.dumps([{"span": n, "ms": ms, "status": s, "attrs": a}
                    for n, ms, s, a in r.spans], ensure_ascii=False, indent=1))}</pre>
<p><a href="/history">← 回请求历史</a></p>
"""
    return page(f"trace {trace_id}", "history", body)


# ==========================================================================
# 6. 会话与隔离
# ==========================================================================


def sessions(demo) -> bytes:
    eng = demo.engine
    with eng.sessions._lock:
        items = sorted(eng.sessions._data.items(), key=lambda kv: -kv[1].touched_at)
    rows = []
    for (t, u, s), st in items[:80]:
        rows.append([
            f'<span class=mono>{esc(t)}</span>',
            f'<span class=mono>{esc(u)}</span>',
            f'<span class=mono>{esc(s[:26])}</span>',
            f"{len(st.turns)}", f"{st.token_used}", f"{len(st.facts)}",
            f'<span class=dim>{time.strftime("%H:%M:%S", time.localtime(st.touched_at))}</span>',
        ])
    body = f"""
<h1>会话与隔离</h1>
<p class=lead>会话的 key 是 <b>(tenant_id, user_id, session_id)</b> 三元组 ——
<b>少任何一个维度都会串会话</b>。这是 lab-16 真实复现过的根因：
用全局字典按 user_id 存、或者用线程 local 存，并发下 A 用户就会看到 B 用户的对话。</p>

<div class="grid g4">
  {metric("活跃会话", f"{len(items)}", "key = 三元组")}
  {metric("劫持拦截", f"{eng.sessions.hijack_blocked}", "session 归属校验拦下")}
  {metric("串会话自检", f"{eng.sessions.cross_talk_detected}",
          "必须为 0", "ok" if eng.sessions.cross_talk_detected == 0 else "bad")}
  {metric("TTL 清理", "启用", f"闲置 {eng.sessions.ttl_s:.0f}s 回收")}
</div>

{card('''<h3>怎么自己验证隔离</h3>
<div class=hint style="margin:0">
去 <a href="/">对话工作台</a>，用同一句话分别以
<span class=mono>tenant=alpha</span> 和 <span class=mono>tenant=beta</span> 提问：<br>
① 回答内容各自独立，不会互相污染；<br>
② 缓存不共享（beta 第一次问会重新落 LLM）；<br>
③ 本页会多出两条属于不同租户的会话记录。<br>
如果看到同一条 session 下混进了别的租户内容，那就是隔离被破坏了。
</div>''')}

<h2>会话明细（最近 {len(rows)} 条）</h2>
{card(table(["租户", "用户", "会话 ID", "轮次", "tokens", "facts", "最近活跃"],
            rows, numeric=(3, 4, 5), empty="还没有会话，去对话工作台问一句"))}
"""
    return page("会话与隔离", "sessions", body, refresh_s=20)


# ==========================================================================
# 7. 指标与成本
# ==========================================================================


def metrics_view(demo) -> bytes:
    eng = demo.engine
    srv = eng.server
    led = srv.ledger
    snap = METRICS.snapshot()

    def h(name: str) -> str:
        st = snap.histograms.get(name)
        if not st or not st.n:
            return "—"
        return f"p50 {st.p50:.0f} / p95 {st.p95:.0f}"

    top = f"""
<div class="grid g4">
  {metric("总请求", f"{int(snap.counters.get('agent_requests_total', 0))}", "本进程")}
  {metric("成功率", _pct(snap.counters.get('agent_ok_total', 0) /
          max(1, snap.counters.get('agent_requests_total', 0))), "succeeded / total")}
  {metric("端到端延迟", h("agent_latency_ms"), "p50 / p95")}
  {metric("LLM 延迟", h("llm_latency_ms"), "含排队时间")}
  {metric("累计成本", _usd(led.usd), f"{led.calls} 次调用")}
  {metric("每成功任务成本",
          _usd(led.usd / max(1, snap.counters.get('agent_ok_total', 0))),
          "比「每请求成本」更该盯的指标")}
  {metric("输入 token", f"{led.in_tokens:,}", f"其中缓存命中 {led.cached_tokens:,}")}
  {metric("输出 token", f"{led.out_tokens:,}", f"缓存占比 {_pct(led.cache_hit_ratio)}")}
</div>"""

    tenant_rows = []
    total_usd = led.usd or 1.0
    for t, v in sorted(led.by_tenant.items(), key=lambda kv: -kv[1]):
        tenant_rows.append([
            f'<span class=mono>{esc(t)}</span>',
            _usd(v), _pct(v / total_usd),
            f'<span class=barwrap><span class=bar><i style="width:{v / total_usd * 100:.1f}%"></i></span></span>',
        ])
    model_rows = [
        [f'<span class=mono>{esc(k)}</span>', _usd(v), _pct(v / total_usd)]
        for k, v in sorted(led.by_model.items(), key=lambda kv: -kv[1])
    ]
    tag_rows = [
        [f'<span class=mono>{esc(k)}</span>', _usd(v), _pct(v / total_usd)]
        for k, v in sorted(led.by_tag.items(), key=lambda kv: -kv[1])[:12]
    ]

    hist_rows = []
    for name, st in sorted(snap.histograms.items()):
        if not st.n:
            continue
        hist_rows.append([
            f'<span class=mono>{esc(name)}</span>', f"{st.n}",
            f"{st.avg:,.1f}", f"{st.p50:,.1f}", f"{st.p95:,.1f}", f"{st.p99:,.1f}",
            f"{st.mx:,.1f}",
        ])

    counter_rows = [
        [f'<span class=mono>{esc(k)}</span>', f"{v:,.0f}"]
        for k, v in sorted(snap.counters.items()) if v
    ]

    body = f"""
<h1>指标与成本</h1>
<p class=lead>四层指标：<b>RED</b>（服务）· <b>USE</b>（资源）· <b>Agent 专有</b>
（调用次数/解析失败率/缓存命中率）· <b>业务</b>（成功率、每成功任务成本）。
下面还提供了 Prometheus 格式导出，可以直接接到你的监控里。</p>

<h2>核心指标</h2>
{top}

<div class="grid g2">
  <div><h2>按租户分摊</h2>
    {card(table(["租户", "成本", "占比", ""], tenant_rows, numeric=(1, 2), empty="暂无数据")
          + '<div class=hint>没有按租户归因，账单就无法解释 —— 这是多租户产品的基本要求。</div>')}</div>
  <div><h2>按模型 / 调用阶段</h2>
    {card(table(["模型", "成本", "占比"], model_rows, numeric=(1, 2), empty="暂无数据"))}
    {card(table(["阶段", "成本", "占比"], tag_rows, numeric=(1, 2), empty="暂无阶段数据"))}</div>
</div>

<h2>延迟分布（直方图）</h2>
{card(table(["指标", "样本", "avg", "p50", "p95", "p99", "max"], hist_rows,
            numeric=(1, 2, 3, 4, 5, 6), empty="还没有样本"))}

<h2>计数器</h2>
{card(table(["指标", "值"], counter_rows, numeric=(1,), empty="暂无计数"))}

<h2>Prometheus 导出</h2>
{card('<div class=hint>给 Prometheus / VictoriaMetrics 抓取的文本格式，'
      '<a href="/metrics">点这里看原始输出</a>（共 ' + str(len(METRICS.to_prometheus().splitlines())) +
      ' 行）。</div>')}
"""
    return page("指标与成本", "metrics", body, refresh_s=15)


# ==========================================================================
# 8. LLM 设置
# ==========================================================================


def settings(demo, qs: dict, probe: dict | None = None, notice: str = "") -> bytes:
    from .llmconfig import PRESETS

    cfg = demo.llm_cfg
    from .billing import quote
    from agentlab.providers import Usage
    bill = quote(cfg, Usage(0,0,0))
    price_info = f'当前模型单价（USD / 百万 tokens）：缓存命中 {bill["price_hit_per_m"]:g}，输入未命中 {bill["price_miss_per_m"]:g}，输出 {bill["price_out_per_m"]:g}；{esc(bill["price_note"])}。来源：{esc(bill["price_source"])}；更新时间：{esc(bill["price_checked_at"])}。下方手工单价仅作未知供应商回退，费用计算不等同最终账单。'
    preset_opts = "".join(
        f'<option value="{esc(k)}" {"selected" if cfg.preset == k else ""}>'
        f'{esc(v["label"])}</option>'
        for k, v in PRESETS.items()
    )
    cur = PRESETS.get(cfg.preset, {})
    provider_opts = "".join(
        f'<option value="{v}" {"selected" if cfg.provider == v else ""}>{label}</option>'
        for v, label in (("mock", "内置模拟器（零依赖、不联网）"), ("real", "真实 LLM（OpenAI 兼容）"))
    )

    notice_html = f'<div class="card" style="border-color:{ui.PALETTE["ok"]}">{esc(notice)}</div>' if notice else ""

    probe_html = ""
    if probe:
        if probe.get("ok"):
            probe_html = card(
                f'<h3>连接测试成功</h3><div class=kv>'
                f'<dt>返回</dt><dd class=mono>{esc(probe.get("reply", ""))}</dd>'
                f'<dt>模型</dt><dd class=mono>{esc(probe.get("model", ""))}</dd>'
                f'<dt>耗时</dt><dd class=mono>{probe.get("elapsed_ms")}ms</dd>'
                f'<dt>tokens</dt><dd class=mono>{probe.get("in_tokens")} / {probe.get("out_tokens")}</dd>'
                f'<dt>端点</dt><dd class=mono>{esc(probe.get("url", ""))}</dd>'
                f"</div>"
            )
        else:
            probe_html = card(
                f'<h3 class=bad>连接测试失败：{esc(probe.get("code", ""))}</h3>'
                f'<div class=kv>'
                f'<dt>错误</dt><dd class=bad>{esc(probe.get("error", "")[:600])}</dd>'
                f'<dt>怎么办</dt><dd>{esc(probe.get("hint", ""))}</dd>'
                f'<dt>端点</dt><dd class=mono>{esc(probe.get("url", ""))}</dd>'
                f"</div>"
            )

    body = f"""
<h1>LLM 设置</h1>
<p class=lead>平台默认用<b>内置模拟器</b>（零依赖、不联网，用来看链路行为）。
这里填上任意 <b>OpenAI 兼容</b> 端点的 key，就能换成真实模型 ——
<b>可靠性机制完全不变</b>：并发闸门、超时预算、重试、熔断、缓存、成本归因
全部照旧生效，因为真实后端只是替换了"怎么产生这一次回答"。</p>

{notice_html}

<h2>当前状态</h2>
{card(f'''<div class=kv>
  <dt>后端</dt><dd>{pill("真实 LLM", "ok") if cfg.is_real else pill("内置模拟器", "warn")}</dd>
  <dt>API Key</dt><dd class=mono>{esc(cfg.masked_key())}</dd>
  <dt>端点</dt><dd class=mono>{esc(cfg.chat_url() or "（未设置）")}</dd>
  <dt>档位映射</dt><dd class=mono>small={esc(cfg.model_or('small') or '—')} ·
    mid={esc(cfg.model_or('mid') or '—')} · large={esc(cfg.model_or('large') or '—')}</dd>
  <dt>配置文件</dt><dd class=mono>.agentlab_llm.json（已 gitignore，不会提交）</dd>
</div>''')}

<h2>配置</h2>
{card(f'''<form action="/settings/save" method="post">
  <div class=row>
    <div class=field><label>后端</label><select name=provider>{provider_opts}</select></div>
    <div class=field><label>API 协议</label><select name=transport>{''.join('<option value="'+t+'" '+('selected' if cfg.transport==t else '')+'>'+t+'</option>' for t in ('openai_chat','openai_responses','anthropic','gemini'))}</select></div>
    <div class=field><label>厂商预设</label><select name=preset id=preset>{preset_opts}</select></div>
  </div>
  <div class=hint>{esc(cur.get("note", ""))}</div>
  <div class=hint>原生协议目前支持文本与函数工具；尚无原生流式、多模态或服务端工具。选择原生协议时请显式关闭流式，Anthropic 还需关闭 JSON 模式并清空 reasoning_effort。不会自动降级。</div>
  <div class=row>
    <label>工具响应流式 <select name=stream_tools><option value=1 {'selected' if cfg.stream_tools else ''}>开启</option><option value=0 {'' if cfg.stream_tools else 'selected'}>关闭</option></select></label>
    <label>JSON 模式 <select name=json_mode><option value=1 {'selected' if cfg.json_mode else ''}>开启</option><option value=0 {'' if cfg.json_mode else 'selected'}>关闭</option></select></label>
    <label>reasoning_effort <input name=reasoning_effort value="{esc(cfg.reasoning_effort)}"></label>
  </div>
  <div class=row style="margin-top:12px">
    <div class=field style="flex:1;min-width:330px"><label>Base URL</label>
      <input name=base_url class=wide value="{esc(cfg.base_url)}"
             placeholder="https://api.deepseek.com"></div>
    <div class=field style="flex:1;min-width:260px"><label>API Key</label>
      <input name=api_key class=wide type=password
             placeholder="{'已保存（留空则不改动）' if cfg.api_key else 'sk-...'}"></div>
  </div>
  <div class=row style="margin-top:12px">
    <div class=field><label>small 档模型</label><input name=m_small value="{esc(cfg.routing.small)}" size=18></div>
    <div class=field><label>mid 档模型</label><input name=m_mid value="{esc(cfg.routing.mid)}" size=18></div>
    <div class=field><label>large 档模型</label><input name=m_large value="{esc(cfg.routing.large)}" size=18></div>
  </div>
  <div class=row style="margin-top:12px">
    <div class=field><label>temperature</label><input name=temperature value="{cfg.temperature}" size=5></div>
    <div class=field><label>max_tokens</label><input name=max_tokens value="{cfg.max_tokens}" size=6></div>
    <div class=field><label>上下文窗口（0=自动，当前 {cfg.resolved_context_window():,}）</label><input type=number min=0 name=context_window value="{cfg.context_window}" size=9></div>
    <div class=field><label>独立验收累计 token 预算（0=不限；仍遵守子任务总预算，新任务生效）</label><input type=number min=0 name=verification_token_budget value="{cfg.verification_token_budget}" size=9></div>
    <div class=field><label>验收强度（新任务生效）</label><select name=review_profile><option value=strict {'selected' if cfg.review_profile=='strict' else ''}>严格：所有变更独立验收</option><option value=balanced {'selected' if cfg.review_profile=='balanced' else ''}>分级：纯文档检查证据，代码/来源/未知改动独立验收</option></select></div>
    <div class=field><label>委派策略（新团队生效）</label><select name=delegation_policy><option value=manual {'selected' if cfg.delegation_policy=='manual' else ''}>按需委派</option><option value=adaptive {'selected' if cfg.delegation_policy=='adaptive' else ''}>实测收益：无有效对照证据时主 Agent 直接完成</option></select></div>
    <div class=field><label>委派收益证据 JSON 路径（宿主可信报告）</label><input name=delegation_evidence_path value="{esc(cfg.delegation_evidence_path)}"></div>
    <div class=field><label>委派深度（主 Agent 为 0；2 允许孙 Agent）</label><input type=number min=1 max=8 name=subagent_max_depth value="{cfg.subagent_max_depth}"></div>
    <div class=field><label>团队模型请求并发</label><input type=number min=1 max=32 name=subagent_max_parallel value="{cfg.subagent_max_parallel}"></div>
    <div class=field><label>同时存活的团队任务数</label><input type=number min=1 max=128 name=subagent_max_tasks value="{cfg.subagent_max_tasks}"></div>
    <div class=field><label>整棵子任务树累计 token 预算（0=不限）</label><input type=number min=0 name=subagent_total_tokens value="{cfg.subagent_total_tokens}"></div>
    <div class=field><label>普通子任务默认 token 预算（0=不限）</label><input type=number min=0 name=subagent_default_tokens value="{cfg.subagent_default_tokens}"></div>
    <div class=field><label><input type=checkbox name=memory_enabled value=1 {'checked' if cfg.memory_enabled else ''}>新任务召回已确认的长期记忆</label><a href="/memories">选择来源和管理记忆</a></div>
    <div class=field><label>超时(s)</label><input name=timeout_s value="{cfg.timeout_s}" size=5></div>
    <div class=field><label>并发上限</label><input name=max_parallel value="{cfg.max_parallel}" size=4></div>
    <div class=field><label>回退输入 $/1M</label><input name=price_in_per_m value="{cfg.price_in_per_m}" size=6></div>
    <div class=field><label>回退输出 $/1M</label><input name=price_out_per_m value="{cfg.price_out_per_m}" size=6></div>
  </div>
  <p class=hint>{price_info}</p>
  <div class=row style="margin-top:12px">
    <div class=field>
      <label>暂时性故障时退回模拟器</label>
      <label style="text-transform:none"><input type=checkbox name=offline_mock_fallback value=1
        {"checked" if cfg.offline_mock_fallback else ""}> 允许（429/超时才退回）</label>
    </div>
  </div>
  <div class=row style="margin-top:16px">
    <button>保存并切换</button>
    <button class=ghost type=submit formaction="/settings/probe" formmethod=post>测试连接（不保存）</button>
  </div>
  <div class=hint><b>鉴权类错误（401/403/404/400）永远不会退回模拟器</b> ——
  否则"key 填错了"会被伪装成"模型答得怪"，你永远查不出问题。</div>
</form>''')}
{probe_html}

<h2>怎么选厂商</h2>
{card(table(["厂商", "Base URL", "备注"],
            [[esc(v["label"]), f'<span class=mono>{esc(v["base_url"] or "自填")}</span>',
              f'<span class=dim>{esc(v["note"])}</span>'] for v in PRESETS.values()]))}

{card('''<h3>安全说明</h3>
<div class=hint style="margin:0">
① Key 只存在两个地方：环境变量，或本地文件 <span class=mono>.agentlab_llm.json</span>（<b>已加入 .gitignore</b>）。<br>
② 界面上<b>永远不回显完整 key</b>，只显示 <span class=mono>sk-a****mnop</span> 形式。<br>
③ 本服务只绑定 <span class=mono>127.0.0.1</span>，不要暴露到公网。<br>
④ 换厂商只需改 Base URL + 模型名，因为协议是 OpenAI 兼容的。
</div>''')}
"""
    return page("LLM 设置", "settings", body)


# ==========================================================================
# 错误页
# ==========================================================================


def error_page(path: str, exc: BaseException, tb: str) -> bytes:
    body = f"""
<h1 class=bad>500 · 内部错误</h1>
<p class=lead>服务端不会静默死亡 —— 任何异常都会变成一个明确的响应，
并把堆栈打到服务端日志。这样"没反应"永远不会成为你唯一能看到的线索。</p>
{card(f'''<div class=kv>
  <dt>端点</dt><dd class=mono>{esc(path)}</dd>
  <dt>异常</dt><dd class=bad class=mono>{esc(type(exc).__name__)}: {esc(str(exc))}</dd>
</div>''')}
<h2>堆栈（尾部）</h2>
<pre>{esc(tb[-2500:])}</pre>
<p><a href="/">← 回对话工作台</a></p>
"""
    return page("500", "console", body)
