# 25 问逐条索引

> 每一个问题都给出：**对应实验 → 一句话结论 → 可复现命令 → 详细答案文档**。
> 表格里的数字是本地实测值（会随机器浮动，方向稳定）。

---

## 一、稳定性与并发

### 1. 生产环境并发量一高 agent 就会 panic，核心原因是什么？

**一句话结论**：panic 通常不是单点故障，而是**正反馈雪崩**：
无界队列 → 内存涨 → 线程/协程数涨 → 上下文切换开销涨 → 请求变慢 → 上游超时重试 →
流量放大 → 队列更深。断掉这个环的任意一处（有界队列、背压、快速失败、熔断）都能止损，
但**只在入口做限流最有效**。

```bash
python -m labs.lab_02_concurrency_memory
```

- 实验：`labs/lab_02_concurrency_memory.py`
- 详细答案：[`docs/answers/lab-02-concurrency-memory.md`](answers/lab-02-concurrency-memory.md)

---

### 2. 最常见的生产环节内存泄漏有哪几种，如何排查修复？

**一句话结论**：五种高频形态是①全局缓存无上限 ②全局 list/Map 只增不减
③闭包/回调持有大对象 ④长生命周期对象（线程池 worker、连接池）不清理
⑤创建后未完成/未取消的异步任务。排查靠**三次采样 + GC 前后对比**：
能回收的是"内存膨胀"，不能回收的才是"泄漏"。

```bash
python -m labs.lab_02_concurrency_memory
```

- 实验：`labs/lab_02_concurrency_memory.py`（含 `tracemalloc` top-5 分配点实测）
- 详细答案：[`docs/answers/lab-02-concurrency-memory.md`](answers/lab-02-concurrency-memory.md)

---

### 3. LLM 响应慢导致整个 agent 链路超时，分层超时的熔断策略是什么？

**一句话结论**：**超时是一个向下传递的预算，不是一个常数。**
`child_timeout = min(阶段上限, 父剩余预算)`；重试必须挂在同一个预算上并共享重试预算；
熔断保护的是**调用方自己**（不发无用请求、不占线程），且**慢调用也要计入失败**。

实测（24 并发、上游 p50 1.2s 且 20% 503），分三种配置逐步加防护：

| 指标 | v0 硬编码超时 | v1a 分层预算 | v1b +熔断 | 说明 |
| --- | --- | --- | --- | --- |
| P95 延迟 | 4064ms | 2187ms | **2187ms** | 预算一上就把尾巴切掉 |
| 超预算请求占比 | 62.5% | 0% | **0%** | 客户端预算终于算数了 |
| **SLO 内成功率** | 4.2% | 12.5% | **12.5%** | 唯一公平的口径 |
| 每美元成功数 | 969 | 1500+ | **1708** | 效率提升 |
| 成本 | $0.00516 | — | **$0.00176** | −66% |
| 原始成功率 | 20.8% | 12.5% | 12.5% | 见下方说明 |

**一个必须讲清的反直觉现象**：v0 的"原始成功率"反而更高，因为它愿意无脑等 4 秒以上，
而用户早在 3.5 秒的预算线就流失了。所以**只有一个成功率指标是不够的**——
必须用 "成功 且 在 SLO 内" 这个口径，否则会奖励"让用户干等"的实现。
这个 lab 就是靠把它拆成两个指标，才让结论站得住。

```bash
python -m labs.lab_06_layered_timeout
```

- 实验：`labs/lab_06_layered_timeout.py`
- 详细答案：`docs/answers/lab-06-layered-timeout.md`

---

### 18. 第三方 LLM 接口不稳定抖动，同时有超时和报错，如何保证可用？

**一句话结论**：五件套 = **全抖动退避重试 + 共享重试预算 + 熔断 + 舱壁 + 降级链**。
特别要注意一个反直觉的事实：**超时不会让服务端的活停下来** —— 客户端走了，
上游仍在跑、并发槽仍被占、token 照样计费。这就是"上游抖动 → 自己被拖垮"的传导路径，
也是必须配熔断的原因（`lab-06` 里会实测这个数字）。

```bash
python -m labs.lab_06_layered_timeout
python -m labs.lab_13_model_routing      # 降级链与灰度
```

- 实验：`labs/lab_06_layered_timeout.py`、`labs/lab_13_model_routing.py`
- 详细答案：`docs/answers/lab-06-layered-timeout.md`

---

## 二、限流、隔离与资源

### 4. 高并发下如何解决 LLM 接口限流的问题？

**一句话结论**：**服务端限流保护自己，客户端限流保护上游，两边都要有。**
分四层：全局（保护自己）→ 租户（公平）→ 模型（保护上游配额）→ 工具（保护下游依赖）。
一个极易搞错的点：上游给的是**并发上限**，而限流器要的是**速率**，
两者差一个延迟（`吞吐 ≈ 并发 / 平均延迟`）。把并发数直接当 QPS 用会把限流器配错十几倍。

```bash
python -m labs.lab_04_rate_limit_fairness
```

- 实验：`labs/lab_04_rate_limit_fairness.py`（含固定窗口边界突发、令牌桶、公平排队）
- 详细答案：[`docs/answers/lab-04-rate-limit-fairness.md`](answers/lab-04-rate-limit-fairness.md)

---

### 8 & 20. 多用户并发会话隔离如何工程化落地，如何避免串会话？企业级多租户如何做权限与数据隔离？

**一句话结论**：会话隔离靠 `(tenant_id, user_id, session_id)` **三元组显式传递**，
并在每一层入口做断言（缺了就抛异常，快速失败）；数据隔离靠**权限过滤下推到召回阶段**
（filter at source），不是召回后再过滤；权限判断**永远不交给 LLM**。

```bash
python -m labs.lab_16_multitenant_isolation
```

- 实验：`labs/lab_16_multitenant_isolation.py`（复现 5 类"串"事故并逐一修复）
- 详细答案：[`docs/answers/lab-16-multitenant-isolation.md`](answers/lab-16-multitenant-isolation.md)

---

### 9. 生产环境出现大量异步任务堆积，如何做任务队列治理？

**一句话结论**：队列治理的核心指标是**最老任务的 age（老化时间）**，不是队列长度 ——
长度会随吞吐波动，age 才反映"用户等了多久"。治理手段：
有界队列 + 背压 → 优先级 + 公平 → 租约/可见性超时（防丢）→ 重试上限 + 指数退避 + DLQ（防毒丸）
→ 按 age 动态扩缩 worker。

```bash
python -m labs.lab_05_queue_governance
```

- 实验：`labs/lab_05_queue_governance.py`
- 详细答案：[`docs/answers/lab-05-queue-governance.md`](answers/lab-05-queue-governance.md)

---

### 16 & 17. 批处理时 CPU 和内存瞬间打满，这类问题如何解决？

**一句话结论**：三件事一起做——①**流式/分页**，一次只驻留一个 chunk；
②**CPU 与 IO 分池**，CPU 池大小 = 核数（否则 IO 任务的 P95 会被 CPU 任务拖垮）；
③**硬性容量预留**，批处理最多用 70% 并发，给交互式留容量（不能"看情况"）。

```bash
python -m labs.lab_17_batch_resource
```

- 实验：`labs/lab_17_batch_resource.py`（含资源采样时间序列）
- 详细答案：[`docs/answers/lab-17-batch-resource.md`](answers/lab-17-batch-resource.md)

---

## 三、性能与缓存

### 5 & 12. 长链路（检索 + 多轮推理）如何优化设计？如何把 P95 的 RT 降下来？

**一句话结论**：**先归因，再优化**（没有 span 级耗时归因，所有优化都是猜）。
80% 的收益来自"别让昂贵计算见到太多候选"：并行化独立步骤、两阶段检索（粗排 + 精排）、
权限过滤下推、缓存、短路提前返回。对冲请求能降尾部但**要花钱**，必须如实计入成本。

```bash
python -m labs.lab_10_p95_optimization
```

- 实验：`labs/lab_10_p95_optimization.py`（逐步优化 + 边际收益递减实测）
- 详细答案：[`docs/answers/lab-10-p95-optimization.md`](answers/lab-10-p95-optimization.md)

---

### 12b. 大量检索的 RT 越来越高，百万级知识库如何性能优化？

**一句话结论**：三个数量级的差别来自三件事：**倒排索引替代线性扫描**（只扫 posting list）、
**两阶段检索**（粗排召回 50 条再精排，而不是对所有候选做昂贵打分）、
**权限过滤下推到召回阶段**（既快又不泄露）。

```bash
python -m labs.lab_10_p95_optimization
```

- 实验：`labs/lab_10_p95_optimization.py`（`agentlab/store.py` 提供三种检索实现对照）

---

### 6. 生产环境完整的 agent 缓存体系，哪些数据可以缓存，哪些不能？

**一句话结论**：五层缓存（精确 → 语义 → 前缀 → 工具/检索 → 会话）。
**能不能缓存是工程硬编码的决策，不能问模型。**
绝对不能缓存：权限相关结果、有副作用的操作、时效性强的数据、个性化创作、
含 PII 的结果、鉴权决策、路由决策。

```bash
python -m labs.lab_07_cache_system
```

- 实验：`labs/lab_07_cache_system.py`（含"能不能缓存"声明式策略表）
- 详细答案：[`docs/answers/lab-07-cache-system.md`](answers/lab-07-cache-system.md)

---

## 四、成本与模型

### 7. token 成本持续增长，有哪些降本的优化手段？

**一句话结论**：九种手段按收益排序：前缀缓存复用 → 上下文裁剪 → 模型路由 →
prompt 精简 → 输出长度硬约束 → 精确/语义缓存 → 批处理合并 → 预算熔断 → 减少重试放大。
**降本必须与质量一起度量**，否则省下的钱会被返工吃掉。成本归因要做到
"每成功任务成本"这个粒度，只看总账单会被"请求变少"误导。

```bash
python -m labs.lab_12_cost_reduction
```

- 实验：`labs/lab_12_cost_reduction.py`
- 详细答案：[`docs/answers/lab-12-cost-reduction.md`](answers/lab-12-cost-reduction.md)

---

### 10 & 11. 小模型便宜但弱、大模型准但贵，如何平衡？生产环境如何做模型调度？

**一句话结论**：**级联路由**是答案的核心 —— 先用小模型，用自置信度 + 校验判断
要不要升级，只把不确定的升级到大模型。这样成本接近全小模型，准确率接近全大模型。
调度层还要有：多端点负载均衡、降级链（large 熔断 → mid → small → 缓存/模板）、
灰度与回滚开关、按租户的预算闸门。**路由决策依赖实时预算与健康度，是硬编码逻辑。**

```bash
python -m labs.lab_13_model_routing
```

- 实验：`labs/lab_13_model_routing.py`（全小/全大/静态规则/级联 四策略对比）
- 详细答案：[`docs/answers/lab-13-model-routing.md`](answers/lab-13-model-routing.md)

---

## 五、上下文与状态

### 19 & 22. 多轮对话上下文越来越大、越来越冗余，如何工程化治理？多种上下文压缩技术有哪些？

**一句话结论**：六种技术配合使用：滑动窗口 → 滚动摘要 → **结构化状态外置**
（最有效）→ 工具结果压缩 + 引用 → 去重 → 分层记忆。
关键是必须有 **token 预算 + 固定丢弃顺序**，且**系统指令与安全约束永不丢弃**。
压缩不是免费的：必须同时观测"压缩率"和"**关键事实保留率**"。

```bash
python -m labs.lab_08_context_governance
```

- 实验：`labs/lab_08_context_governance.py`（含压缩率 vs 关键事实召回率权衡表）
- 详细答案：[`docs/answers/lab-08-context-governance.md`](answers/lab-08-context-governance.md)

---

### 14. 生产环境如何解决 agent 长任务执行失败、又需要从头跑的问题？

**一句话结论**：**可恢复的前提是每一步执行前先把状态落盘**，而不是失败后再想办法。
四件事：checkpoint（含 version/run_id/completed）、幂等键（工程生成，`run_id + step`）、
重试分级（可重试瞬时错误 vs 不可重试业务错误）、saga 补偿（逆序回滚）。
**哪些步骤能重跑、哪些绝对不能，是硬编码的步骤属性。**

```bash
python -m labs.lab_09_long_task_resume
```

- 实验：`labs/lab_09_long_task_resume.py`、`agentplat/checkpoint.py`
- 详细答案：[`docs/answers/lab-09-long-task-resume.md`](answers/lab-09-long-task-resume.md)

---

## 六、工具、子 Agent 与硬编码边界

### 23 & 24 & 25. 如何开启子 Agent？如何调用工具？如何编写工具？

**一句话结论**：
- **写工具**：参数 Schema、独立超时、幂等声明、副作用声明、权限声明、结果截断上限、
  单请求调用上限 —— 七个属性一个都不能少。**描述质量直接决定工具选择正确率。**
- **调用工具**：调用前 Schema 校验（失败的结构化错误要回灌给模型自修复）、
  调用中有界循环与重复检测、调用后结果截断 + 引用。
- **开子 Agent**：判据是"是否需要独立上下文/收窄权限/独立预算/并行探索"。
  简单任务开子 Agent 只会更贵更慢 —— 这个 lab 会**诚实地打出"更贵"的数字**。

```bash
python -m labs.lab_15_tools_subagents
```

- 实验：`labs/lab_15_tools_subagents.py`
- 详细答案：[`docs/answers/lab-15-tools-subagents.md`](answers/lab-15-tools-subagents.md)

---

### 15. agent 工程中哪些逻辑必须通过工程化硬编码来解决，不能交给 LLM 自主判断？

**一句话结论**：判据只有一条 —— **这件事做错了，是会"答得不太好"，还是会"造成不可逆的后果"?**
后者必须是代码。具体清单（20 项）见
[`docs/HARDCODE-BOUNDARY.md`](HARDCODE-BOUNDARY.md)，核心是这五类：
①权限与鉴权 ②金额/额度/配额 ③循环与终止条件 ④幂等与副作用 ⑤参数与输出校验。
另一半同样重要：**该交给模型的要交出去**（语义理解、查询改写、工具选择、任务拆解、措辞），
否则就退化成规则引擎。文档里有完整的**决策归属表**。

```bash
python -m labs.lab_14_hardcode_boundary
```

- 实验：`labs/lab_14_hardcode_boundary.py`
- 决策归属表：[`docs/HARDCODE-BOUNDARY.md`](HARDCODE-BOUNDARY.md)
- 详细答案：[`docs/answers/lab-14-hardcode-boundary.md`](answers/lab-14-hardcode-boundary.md)

---

## 七、生命周期、观测与落地顺序

### 13. 讲一下 Agent 的服务如何做启停设计？如何避免发布期间导致用户体验报错？

**一句话结论**：三件事：①**liveness 与 readiness 分离** —— liveness 只看进程
（绝不能包含依赖健康，否则依赖抖动会被放大成滚动重启），readiness 看真实能力；
②**预热**（前缀缓存/索引/连接池），readiness 必须等预热完成才为 true；
③**优雅停机** —— 收到信号先摘 readiness（让流量先走）→ 再排空在飞请求 →
超时才强杀，并记录被中断数量。

```bash
python -m labs.lab_01_service_lifecycle
```

- 实验：`labs/lab_01_service_lifecycle.py`、`agentplat/service.py`（含真实 `/livez` `/readyz`）
- 详细答案：[`docs/answers/lab-01-service-lifecycle.md`](answers/lab-01-service-lifecycle.md)

---

### 16b. 生产环境的 agent 如何评定性能、成本和稳定性？核心指标观测哪些？

**一句话结论**：四层指标 ——
**RED**（QPS/错误率/延迟分位）、**USE**（利用率/饱和度/拒绝数）、
**Agent 专有**（LLM 调用数、工具调用数、推理轮数、检索候选数、解析失败率、压缩率、
缓存命中率、工具选择正确率）、**业务**（任务成功率、人工介入率、放弃率）。
成本要看"**每成功任务成本**"而不是每请求成本。SLO 要配 error budget 和
**burn-rate 双窗口告警**，且告警要盯**症状**（用户可感知）而不是原因。

```bash
python -m labs.lab_18_observability_slo
```

- 实验：`labs/lab_18_observability_slo.py`（含指标字典表 + burn-rate 告警器）
- 详细答案：[`docs/answers/lab-18-observability-slo.md`](answers/lab-18-observability-slo.md)

---

### 21. 从 0 搭建一个生产级的企业级 agent 平台，核心落地的工程顺序是什么？

**一句话结论**：八层递进，**观测必须最先做**（不是最后）：
①单请求链路 + 打点 → ②可靠性原语（预算/重试/熔断/舱壁）→ ③隔离与资源治理 →
④缓存与成本 → ⑤上下文与状态 → ⑥工具与子 Agent → ⑦队列与生命周期 →
⑧评估、灰度与多 Agent。
**多 Agent 是能力上限的优化，前七层是下限的保障 —— 先保下限。**

```bash
python -m labs.lab_03_task_startup_order
```

- 实验：`labs/lab_03_task_startup_order.py`
- 完整蓝图（含每层 DoD）：[`docs/BLUEPRINT.md`](BLUEPRINT.md)
- 详细答案：[`docs/answers/lab-03-startup-order.md`](answers/lab-03-startup-order.md)

---

## 八、把一切串起来

```bash
python -m agentplat.run
```

用**同一份多租户负载**跑两遍（关掉所有防护 vs 全防护），实测对比：

| 指标 | 反面版本 | 生产版本 | 变化 |
| --- | --- | --- | --- |
| 有效吞吐（成功/s） | 2.0 | **22.8** | +1018% |
| P95 延迟 | 1980ms | **460ms** | −77% |
| 每成功成本 | $0.006974 | **$0.000234** | −97% |
| 跨会话事故 | 0 | 0 | 隔离始终生效 |

关键提醒：**两个版本的成功率很接近**（因为反面版本把请求堵在慢路径上、
单位时间处理的请求少得多）。所以对比必须看**有效吞吐（成功/s）**，
只看成功率会得出"两边差不多"的错误结论。

- 平台代码：`agentplat/`
- 全部结论的自动化校验：`python verify.py`
- 上线前检查表：[`docs/ACCEPTANCE.md`](ACCEPTANCE.md)
