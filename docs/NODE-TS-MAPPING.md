# Node / TypeScript 对照实现指南

> 这个项目的实验是 Python 写的，但**工程结论与语言无关**。这份文档给出一条映射：
> 每个 Python 机制在 Node/TypeScript 里对应什么，以及哪些坑在 Node 里更严重。

---

## 一、为什么实验用 Python，但结论通用于 Node

选 Python 只因为一个实际原因：**标准库自带 `tracemalloc`、`gc`、`threading`、
`asyncio`、`http.server`**，可以做到零依赖复现内存泄漏、并发、探针端点。
Node 的等价能力大多需要额外依赖（`--inspect`、`clinic`、`why-is-node-running`）。

但**结论层面**，Node 后端反而更容易踩这几个坑：

| 问题 | Node 里更严重的原因 |
| --- | --- |
| 事件循环阻塞 | 一个同步 CPU 任务会卡住**所有**并发请求，比 Python 线程模型更致命 |
| 内存泄漏 | 闭包极其容易持有大对象；长驻进程 + 缓存 = 必然泄漏 |
| 请求上下文丢失 | `AsyncLocalStorage` 用错就丢上下文 → 串会话 |
| 无界并发 | `Promise.all` 一个数组就起 N 个并发，没有天然上限 |
| 超时 | 原生 `fetch` 没有默认超时（直到较新版本才有 `signal` 支持） |

---

## 二、逐项映射

### 1. 分层超时预算（lab-06）

```python
# Python
dl = Deadline.root(3000, stages={"llm": 1600})
with dl.stage("llm") as st:
    server.call(msgs, timeout=st.timeout_s())
```

```typescript
// TypeScript
class Deadline {
  constructor(private totalMs: number, private stages: Record<string, number>,
              private started = performance.now()) {}
  elapsedMs() { return performance.now() - this.started; }
  remainingMs() { return this.totalMs - this.elapsedMs(); }
  stageBudgetMs(stage: string) {
    return Math.max(0, Math.min(this.stages[stage] ?? this.remainingMs(), this.remainingMs()));
  }
}

const dl = new Deadline(3000, { llm: 1600 });
const ac = new AbortController();
const timer = setTimeout(() => ac.abort(), dl.stageBudgetMs("llm"));
try {
  const res = await fetch(url, { signal: ac.signal });
} finally {
  clearTimeout(timer);      // 必须清，否则定时器泄漏 + 进程不退出
}
```

**Node 特有的坑**：`setTimeout` 必须 `clearTimeout`，否则每个请求残留一个定时器
（这就是 lab-02 里的"泄漏"在 Node 里的形态）。`AbortController` 的 signal 只能
用一次，重试要新建。

### 2. 熔断器（lab-06）

没有语言差异，纯状态机。注意 Node 单线程，**不需要锁**——这是 Node 的一个真实优势。
但在 `cluster`/`worker_threads` 多进程部署下，熔断状态是**每进程独立**的，
需要权衡"是否用共享存储做全局熔断"（通常不必要，进程级就够）。

### 3. 舱壁与限流（lab-04）

```typescript
// 不要这样：无界并发
await Promise.all(items.map(fetchItem));      // N 个请求同时打出去

// 正确：有界并发 + 背压
import pLimit from "p-limit";                 // 或用队列自实现
const limit = pLimit(8);
await Promise.all(items.map(i => limit(() => fetchItem(i))));
```

Python 的 `run_concurrently` 对应 Node 的手写 worker pool：

```typescript
async function runConcurrently<T>(fn: (i: number) => Promise<T>, n: number, workers: number) {
  const results: (T | Error)[] = new Array(n);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(workers, n) }, async () => {
    while (true) {
      const i = next++;
      if (i >= n) return;
      try { results[i] = await fn(i); } catch (e) { results[i] = e as Error; }
    }
  }));
  return results;
}
```

**关键点**：吞掉异常并放进结果数组，这样错误率可统计而不是整个批次崩掉。

### 4. 内存泄漏排查（lab-02）

| Python | Node | 说明 |
| --- | --- | --- |
| `tracemalloc` top-N | `process.memoryUsage()` + heap snapshot | Node 用 `--inspect` + Chrome DevTools 抓 snapshot，比较两次快照的 retained size |
| `gc.get_objects()` 计数 | `v8.getHeapStatistics()` | |
| `gc.collect()` 验证可回收 | `global.gc()`（需 `--expose-gc`） | |
| `weakref` | `WeakRef` / `WeakMap` | Node 的 `WeakMap` 是防泄漏的**首选**工具 |
| `rss_mb()` | `process.memoryUsage().rss` | |

Node 特有的三种泄漏形态：

```typescript
// ① 事件监听器泄漏（最常见）
emitter.on("data", handler);       // 每次请求都注册，从不 removeListener
// → 用 once() 或确保 removeListener

// ② 闭包持有大对象
const cache = new Map();
function handle(req: Request) {
  const bigPayload = JSON.parse(req.body);       // 1MB
  cache.set(req.id, () => bigResult(bigPayload)); // 闭包把 1MB 永久留下
}
// → 用 WeakMap，或显式只存需要的字段

// ③ 定时器 / setInterval 未清理
setInterval(poll, 1000);           // 进程永远不退出，且持续累积
// → 用 unref() 并保证清理
```

**判断"泄漏 vs 膨胀"的方法完全一样**：手动触发一次 GC（`--expose-gc`），
看 heap 是否回落。回落是膨胀，不回落才是泄漏。

### 5. 会话隔离与请求上下文（lab-16）

```typescript
// Python 里我们坚持"显式传参"，Node 里同样推荐显式传参。
// 如果一定要用 AsyncLocalStorage，必须知道它的边界：

import { AsyncLocalStorage } from "node:async_hooks";
const als = new AsyncLocalStorage<RequestContext>();

app.use((req, res, next) => {
  const ctx = buildContext(req);        // 在入口构造，且必须校验完整性
  als.run(ctx, () => next());
});

// ⚠️ 三个会丢上下文的场景：
//   1. 在 als.run 之外创建的 Promise 里读取（比如模块级缓存的 promise）
//   2. 传给 worker_threads / child_process（上下文不跨线程传播）
//   3. setTimeout/setInterval 里如果注册时机不对（较新 Node 已支持传播）
```

**结论不变**：`tenant_id` / `user_id` / `session_id` 必须是显式参数或至少显式断言。
隐式上下文在任何语言里都只在"没有跨边界"时才安全，而 agent 系统天然跨边界
（检索、工具、子 agent、队列）。

会话存储的 key 设计：

```typescript
// ✗ 会串
const sessions = new Map<string, Session>();     // key = userId
sessions.set(ctx.userId, session);

// ✓ 三元组，且 sessionId 全局唯一
const key = `${ctx.tenantId}:${ctx.userId}:${ctx.sessionId}`;
```

### 6. 流式输出与 TTFT（lab-10）

```typescript
// 流式能显著改善"用户感知延迟"，但要分清两个指标：
const t0 = performance.now();
let ttft: number | null = null, full = 0, chars = 0;

for await (const chunk of stream) {
  if (ttft === null) ttft = performance.now() - t0;   // 首字节
  chars += chunk.length;
  res.write(chunk);
}
full = performance.now() - t0;                        // 完整响应

metrics.histogram("ttft_ms").observe(ttft!);
metrics.histogram("e2e_ms").observe(full);
```

**要监控两个指标**：TTFT（体验）和端到端（吞吐/成本）。只优化端到端可能让体验
更差（比如为了凑完整答案而缓冲）。

### 7. 优雅停机（lab-01）

```typescript
const server = app.listen(port);
let draining = false;
let inflight = 0;

app.use((req, res, next) => {
  if (draining) return res.status(503).set("Retry-After", "1").end();
  inflight++;
  res.on("finish", () => inflight--);
  next();
});

// 探针必须分离：livez 只看进程，readyz 看能否接流量
app.get("/livez", (_, res) => res.status(200).end());          // 永远 200（除非僵死）
app.get("/readyz", (_, res) => res.status(draining ? 503 : 200).end());

process.on("SIGTERM", async () => {
  draining = true;                       // ① 先摘流量
  await sleep(2000);                     // ② 等 k8s 把 endpoint 摘干净
  server.close();                        // ③ 停止接收新连接
  const deadline = Date.now() + 10_000;  // ④ 排空在飞请求，有上限
  while (inflight > 0 && Date.now() < deadline) await sleep(50);
  server.closeAllConnections?.();        // ⑤ 兜底强关
  process.exit(0);
});
```

**两个最容易被忽略的点**：
- 探针端点必须**独立于业务逻辑**，且 `livez` 绝不能包含依赖检查；
- `server.close()` 之后要**等一下**再退出，因为 k8s 摘 endpoint 有延迟，
  这期间还会有流量打进来。

### 8. 队列治理（lab-05）

Node 里通常用 BullMQ / SQS，但**结论不变**：
- 队列必须有界，满了要给生产者背压；
- 核心指标是**最老任务的 age**，不是长度；
- 必须有 lease/visibility timeout，否则 worker 崩溃就丢任务；
- 必须有重试上限 + DLQ，否则毒丸会占满 worker。

### 9. 成本与 token 计量（lab-12）

任何语言都必须做同一件事：**给每次模型调用打上标签**（tenant / feature / trace_id），
否则账单无法归因。

```typescript
type CallTags = { tenant: string; feature: string; traceId: string; model: string };

function recordUsage(tags: CallTags, usage: { input: number; output: number; cached?: number }) {
  costLedger.add(tags, priceOf(tags.model, usage));
}
```

Python 版用 `CostLedger`（`agentlab/tokens.py`），Node 里就是同一套结构。

---

## 三、框架层面的对应

| 能力 | Python 生态 | Node 生态 |
| --- | --- | --- |
| 服务框架 | FastAPI / Litestar | Fastify / NestJS |
| LLM SDK | openai / anthropic / litellm | openai / @anthropic-ai/sdk / Vercel AI SDK |
| 编排 | LangGraph / 自研 | LangGraph.js / Mastra / 自研 |
| 追踪 | OpenTelemetry SDK | OpenTelemetry SDK |
| 缓存 | redis-py | ioredis |
| 队列 | Celery / arq | BullMQ |
| 向量库 | faiss / pgvector | 同左（服务端） |

**建议：不要把可靠性原语交给框架。** 超时预算、熔断、舱壁、幂等、输出契约校验
这五件事，自己写一遍（每个 50~150 行）比调框架参数更可控，也更容易解释为什么。
本项目的 `agentlab/orchestration.py` 就是一套 400 行的参考实现。

---

## 四、迁移检查清单

如果你要把这套结论落到 Node 项目里，逐条对照：

- [ ] 所有外部调用都有**超时**，且超时值来自统一预算而不是各处硬编码
- [ ] 重试挂在同一个预算上，且有全局重试预算（防风暴）
- [ ] `setTimeout` / `setInterval` / 事件监听器都有清理路径
- [ ] 缓存有容量上限 + TTL + tenant 维度
- [ ] 请求上下文在入口构造并**显式传递**，每层有断言
- [ ] 会话 key 是 `(tenant, user, session)` 三元组
- [ ] 并发有闸门（不是裸 `Promise.all`）
- [ ] CPU 密集任务与 IO 分离（`worker_threads` 或独立进程）
- [ ] 有 `livez` / `readyz` 分离，SIGTERM 有排空逻辑
- [ ] 每次模型调用都有成本归因标签
- [ ] 输出有 Schema 校验 + 修复 + 兜底
- [ ] 有 trace（至少是 trace_id 贯穿日志）

---

## 五、相关实验

| 想理解什么 | 跑哪个 Python 实验 |
| --- | --- |
| 超时预算与熔断 | `python -m labs.lab_06_layered_timeout` |
| 内存泄漏五种形态 | `python -m labs.lab_02_concurrency_memory` |
| 会话隔离 | `python -m labs.lab_16_multitenant_isolation` |
| 队列治理 | `python -m labs.lab_05_queue_governance` |
| 优雅停机 | `python -m labs.lab_01_service_lifecycle` |
| 成本归因 | `python -m labs.lab_12_cost_reduction` |
