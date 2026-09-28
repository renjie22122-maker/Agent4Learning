# agentlab 核心契约（冻结）

> 这份文档是所有 lab 与 `agentlab/*` 之间的**接口契约**。写 lab 之前先读这里；
> 需要新能力时，优先在 lab 内实现，只有确属通用能力才扩展核心。

## 0. 运行环境

- Python **3.11+**（用到 `X | Y` 类型语法、`asyncio.timeout` 风格）
- **零硬依赖**：只用标准库。环境里恰好有 `psutil` 时会在 `rss_mb()` 里被兜底使用，
  但不是必须。
- 执行方式一律是 `python -m labs.<模块名>`，从仓库根目录运行。
- 每个 lab 必须**独立可跑**、不联网、默认 20 秒内结束。
- Windows 控制台中文：`agentlab.util` 导入时已 `force_utf8()`；PowerShell 侧建议先执行
  `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`。

## 1. 输出协议（**硬性要求**，`verify.py` 依赖它）

每个 lab 的 `main()` 必须用 `agentlab.util.lab()` 上下文包住，并按顺序打印四段：

```
[LAB-START] <lab_id> :: <title>
    学习目标问题: ...
>>> 1. 复现故障            <- phase()
    ...
[BROKEN-REPRODUCED] <一行关键数字>       <- 必打，且必须是可解析的数字行
>>> 2. 观测 / 归因
    ...
>>> 3. 修复
    ...
[FIX-APPLIED] <一行关键数字>
>>> 4. 验证
[VERIFY] p95: 1234.0ms -> 210.0ms (-83.0%)    <- 必打，格式见下
[TAKEAWAY] <一句话结论>
[LAB-END] <lab_id> elapsed=1.23s
```

- `[VERIFY]` 行格式：`[VERIFY] <指标名>: <before> -> <after> (<变化率>)`，
  before/after 必须是纯数字（可带单位后缀），变化率用 `%`。
  一个 lab 可以有多行 `[VERIFY]`。
- 关键结论必须通过 `kv()` / `note()` / 表格打印，**不要只写在注释里**。
- 允许在 `[VERIFY]` 之后再打印一小段「工程结论」，但 `[TAKEAWAY]` 只打一次。

## 2. 必须用到的工具函数（`agentlab.util`）

| 函数 | 用途 |
| --- | --- |
| `lab(lab_id, title, question)` | 上下文管理器，自动打 START/END |
| `phase(title, tag)` | 阶段标题 |
| `kv(key, value, unit)` | 对齐的键值行 |
| `note(text)` | 缩进说明 |
| `rule(char, width)` / `head(text)` | 分隔线与大标题 |
| `BROKEN` / `FIX` / `VERIFY` / `TAKEAWAY` | 协议标记常量 |
| `run_concurrently(fn, count, workers)` | 线程并发执行，异常收集进结果列表 |
| `partition(results)` / `error_rate(results)` / `error_histogram(results)` | 结果统计 |
| `Stats(samples_ms)` | n/avg/p50/p95/p99/max，`__str__` 可直接打印 |
| `percentile(samples, q)` / `mean(samples)` | 百分位 |
| `improvement(before, after, lower_is_better=True)` | `-63.2%` 形式的对比串。**正负号只表示数值升降，不表示好坏** |
| `verdict(before, after, lower_is_better=True)` | `改善 50.0%` / `恶化 12.0%` / `持平`，无歧义，打印结论时优先用 |
| `rss_mb()` / `fmt_bytes(n)` / `payload_of_kb(kb)` | 内存观测与造数据 |
| `rng(seed)` / `lognormal_latency(r, p50_ms, sigma)` | 可复现随机 |

> **关于 `improvement()` 的坑**：早期版本对 `lower_is_better=False` 会翻转符号，
> 于是"提升 8.4%"被打印成 `-8.4%`，读的人会以为退化。现在符号只表示升降方向。
> 需要"好坏结论"时用 `verdict()`。想把指标交给 `verify.py` 校验方向，
> 用 `[VERIFY] name: before -> after` 格式即可，不要依赖这个函数。

## 3. `agentlab.metrics`

```python
from agentlab.metrics import METRICS
METRICS.counter(name, help).inc(n)
METRICS.gauge(name, help).set(v) / .inc() / .dec()
METRICS.histogram(name, help).observe(ms)
METRICS.render("标题", include=["llm_", "cache_"])   # 打印快照
METRICS.snapshot()                                    # 取结构化数据
METRICS.to_prometheus()                               # 模拟 /metrics
METRICS.reset()                                       # lab 收尾清理，避免互相污染
```

命名约定：`<域>_<对象>_<单位>`，延迟直方图统一 **毫秒** 且以 `_ms` 结尾。

## 4. `agentlab.providers` —— 模拟 LLM

```python
from agentlab.providers import (
    LLMServer, ChatMessage, LLMReply, LLMError, CircuitOpen, BudgetExceeded,
    user, system, assistant, tool_msg, default_server,
    SMALL, MID, LARGE, LADDER, MODELS,   # 从 tokens 重新导出，方便一处导入
)
# 也可以直接从 tokens 拿：from agentlab.tokens import SMALL, MID, LARGE, LADDER

srv = LLMServer(max_queue=64, max_wait_s=5.0, seed=7)
srv.call(messages, model="mid-32b", timeout=1.5, tenant="t1", tag="rag")  -> LLMReply
await srv.acall(messages, model=..., timeout=...)                          -> LLMReply
```

`LLMReply`: `.text` / `.content` / `.model` / `.usage.{in_tokens,out_tokens,cached_tokens}` /
`.latency_ms`（含排队）/ `.queued_ms` / `.cached` / `.attempts`

`LLMError.code` 取值：`400` `429` `503` `TIMEOUT` `BUDGET` `CIRCUIT_OPEN`；
`429/503/TIMEOUT` 的 `.retryable is True`，`.retry_after` 是建议等待秒数。

故障注入（只在 lab 里用）：

```python
srv.set_error_rate("mid-32b", 0.35)      # 上游抖动加剧
srv.set_latency("mid-32b", p50_ms=2500)  # 变慢
srv.hang("mid-32b", True)                # 对端挂死：永不返回，只能靠超时自捩
srv.warm_prefix("mid-32b", key, tokens)  # 预热前缀缓存
srv.reset_stats()                        # 同时清零 stats 与 ledger
srv.reset_stats(clear_ledger=False)      # 保留累计花费（跨场景对账用）
srv.clear_prefix_cache()
srv.ledger                               # CostLedger：in/out/cached tokens、usd、by_tenant
srv.summary_lines()                      # 三行可读摘要
```

> **成本对账的正确姿势**：``reset_stats()`` 默认连 ``ledger`` 一起清零，所以
> "清空后重跑"是安全的。但如果你想在**不 reset** 的情况下测某个场景的花费，
> 必须自己先记下 ``srv.ledger.usd`` 作为基线，跑完再作差 —— 否则测到的是
> 从进程启动至今的累计值，会得出离谱的单价。

**重要语义**：provider 自己**不做超时**。你不传 `timeout` 就会一直挂着（`hang` 模式
下真的会挂住）。token 在"计算"之前就计量，所以**超时和失败也会产生成本**——
这就是重试放大成本的原因，lab 里可以直接对账。

`hang(model, True, duration_s=3.0)` 的行为细节（用它写 lab 前必读）：
* 挂死期间**调用方只能靠自己的超时自救**；provider 不会替你收尾。
* 客户端超时返回后，服务端线程仍会跑完并**继续占用并发槽**，此时
  `stats["orphaned_workers"]` 与 `stats["hangs_inflight"]` 计数上升 ——
  这正是"上游抖动把自己拖垮"的传导路径，`lab-06` 会实测它。
* `duration_s` 默认 3 秒（教学用）。**不要**设成很大再让主线程等它，
  否则 lab 会卡住；超时返回后 `finally` 会正常释放并发槽，不会残留。

## 5. `agentlab.orchestration` —— 编排原语

```python
from agentlab.orchestration import (
    Deadline, CircuitBreaker, Bulkhead, BulkheadSet, RetryPolicy, RetryBudget,
    call_with_retry, TokenBucket, SlidingWindowLimiter, await_with_deadline, hedged_call,
)

dl = Deadline.root(total_ms=6000, stages={"retrieve": 800, "llm": 3000}, clock=clk)
with dl.stage("llm") as st:            # 预算为 0 时 __enter__ 抛 BudgetExceeded
    timeout_s = st.timeout_s(floor_s=0.05)
    reply = srv.call(msgs, timeout=timeout_s)

cb = CircuitBreaker("llm:mid", failure_threshold=5, cooldown_s=2.0)
cb.call(lambda: srv.call(...))         # 熔断打开时抛 CircuitOpen
cb.state; cb.stats(); cb.force_open(); cb.force_close()

bh = Bulkhead("llm", limit=8)          # 满则快速失败
with bh: ...
bh.call(fn); bh.stats()
pools = BulkheadSet("tenant", limit_per_key=4); pools.get("t1")

policy = RetryPolicy(max_retries=2, jitter="full")
call_with_retry(fn, policy, deadline=dl, budget=RetryBudget(10))

tb = TokenBucket(rate=20, burst=40); tb.try_acquire(); tb.retry_after_s()
sw = SlidingWindowLimiter(limit=100, window_s=60)
```

## 6. `agentlab.tokens`

`count_tokens(text)` / `count_messages(msgs)` / `ModelSpec` / `SMALL|MID|LARGE|LADDER` /
`CostLedger` / `price_of(model, in, out, cached)` / `fit_to_budget(texts, budget, keep_tail)`

模型阶梯（用于「大小模型平衡 / 模型调度」）：

| 模型 | tier | p50 延迟 | 质量分 | 输入 $/1M | 输出 $/1M | 并发上限 |
| --- | --- | --- | --- | --- | --- | --- |
| `small-8b` | small | 220ms | 0.62 | 0.05 | 0.15 | 24 |
| `mid-32b` | mid | 640ms | 0.84 | 0.60 | 1.80 | 10 |
| `large-400b` | large | 1900ms | 0.95 | 5.00 | 15.00 | 4 |

## 7. `agentlab.store` —— 模拟知识库

```python
from agentlab.store import build_corpus, build_index, BM25Index, NaiveRetriever, TwoStageRetriever, Query, Doc, Hit

docs = build_corpus(200_000, seed=7)                 # 紧凑文档，百万级也跑得起
idx  = BM25Index(docs)
naive = NaiveRetriever(docs)                          # 反面教材：O(N) 全量扫描
two   = TwoStageRetriever(idx, recall_k=50, rerank_cost_ms=0.45)

res = idx.search(Query("缓存穿透 优化", top_k=5, tenant="tenant-a",
                       groups=frozenset({"ga", "public"}), rerank=False))
res.hits; res.candidates; res.scanned; res.latency_ms; res.stage_ms
```

**权限过滤必须在召回阶段完成**（`Query.tenant` / `Query.groups`），召回后再过滤
既慢又会泄露——这是多租户 lab 的核心论点。

## 8. `agentlab.tracing`

```python
from agentlab.tracing import Tracer, TraceStore
tr = Tracer("req-1")
with tr.span("retrieve", tenant="t1") as sp:
    sp.set(candidates=50)
tr.render(); tr.render_stage_table(); tr.stage_stats(); tr.critical_path()
store = TraceStore(); store.add(tr); store.aggregate()
```

## 9. `agentlab.clock`

`RealClock()`（默认）/ `VirtualClock()`；两者都有
`now()` `monotonic()` `async sleep(s)` `sleep_sync(s)`。
虚拟时钟用于把「超时预算 / 熔断冷却 / 限流窗口」的逻辑在**毫秒内**演示完。

## 10. 写作规范

1. 文件头 docstring 用中文，写清楚：**这个 lab 回答什么问题、复现什么故障、
   生产上正确的做法是什么**。
2. 结构固定：`1. 复现故障` → `2. 观测/归因` → `3. 修复` → `4. 验证`。
3. 每个 lab 结尾必须有 `工程结论` 小节，用 `[TAKEAWAY]` 收口，并在文件末尾写
   一个 `QUESTIONS` 列表中英对照，说明它回答了需求清单里的哪几条。
4. 数字要真实打印（实测），不要硬编码"看起来合理"的假数字。
5. 单文件控制在 400 行以内；超了说明这个 lab 该拆分。
6. 收尾调用 `METRICS.reset()`，避免影响同进程内的后续 lab（`verify.py` 会在
   独立子进程里跑，但保持干净是好习惯）。
