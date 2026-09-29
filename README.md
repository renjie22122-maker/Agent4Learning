# Agent4Learning

**一个可以运行的 Agent 工程教学实验室，附真实模型执行内核。**

包含 51 个默认实验、1 个可选 ANN 实验与 3 个 Windows 原生隔离实验：每个实验都先让系统
崩一次、打印出崩溃时的指标，再给出工程修复，最后用同一份负载跑对照组验证修复确实有效。
实验指标来自固定负载与模拟模型，不能视为真实模型质量或生产认证。真实模型评估另行运行。

当前使用入口、实现范围和验证边界见 [当前项目说明](docs/CURRENT.md)。
按功能查看：[技能导入](docs/SKILLS.md)、[文档知识库](docs/KNOWLEDGE.md)、
[知识库范围选择](docs/KNOWLEDGE-SCOPES.md)、[宿主执行与权限恢复](docs/PERMISSION-RECOVERY.md)、
[交互时间线](docs/CHAT-TIMELINE.md)、[Windows 原生沙箱](docs/NATIVE-SANDBOX.md)。
Docker 与 MCP 均可选；宿主已安装的依赖不等于原生沙箱内可用。

```bash
# 核心教学实验使用 Python 3.11+ 标准库；可选集成另需依赖
python verify.py                 # 51 个默认 lab；加 --native 包含 3 个 Windows 实测
python -m labs.lab_06_layered_timeout   # 单独跑一个
python -m agentplat.run          # 跑完整平台的多租户压测 + SLO 合规报告
```

启动本机网页界面：`python tools/start_desktop.py`，再在设置页面配置自己的模型 API。
聊天内等待确认见 [交互说明](docs/HUMAN_INTERACTION_DESIGN.md)，
embedding 与检索配置见 [本地混合检索](docs/RAG_HYBRID.md)。
`RUNTIME*`、带日期的评测和自评文档记录对应阶段的实现或实验，不能作为当前功能全集；
查阅顺序和历史入口见 [当前项目说明](docs/CURRENT.md)。

公开仓库仅包含源码、实验、测试工具和说明，不包含 API 凭据、聊天记录、附件、
长期记忆、知识库、本地模型、用户工作区及原始运行日志。文档中的历史实测数据是
特定环境的记录；引用的 `.diagnostics/` 等私有产物不随仓库分发。
浏览器集成需要配置 Playwright，文档解析按格式配置依赖；本地向量检索需要
`numpy`、`torch`、`transformers` 及另行下载和配置的模型，默认克隆不附带模型权重。

---

## 这个项目回答什么问题

下面 25 个问题全部来自真实的生产事故和面试追问。每一个都对应一个**可以运行**的实验，
表格里的数字是实测结果（会随机器浮动，但方向稳定）。

| # | 问题 | 对应的实验 | 实测的关键结论 |
| --- | --- | --- | --- |
| 1 | 并发量一高 agent 就 panic，核心原因是什么？ | [`lab-02`](labs/lab_02_concurrency_memory.py) | 无界队列 → 内存涨 → 慢 → 重试 → 雪崩，是正反馈而不是单点 |
| 2 | 最常见的生产内存泄漏有哪几种，如何排查修复？ | [`lab-02`](labs/lab_02_concurrency_memory.py) | 5 类泄漏 + `tracemalloc` 定位法；**能回收的只是膨胀，不能回收的才是泄漏** |
| 3 | LLM 响应慢导致整链路超时，分层超时熔断策略怎么设计？ | [`lab-06`](labs/lab_06_layered_timeout.py) | 超时是**预算**不是常数；SLO 内成功率 4.2% → 12.5%，P95 −46%，成本 −66% |
| 4 | 高并发下如何解决 LLM 接口限流问题？ | [`lab-04`](labs/lab_04_rate_limit_fairness.py) | 四层限流 + 公平排队；单租户打满不能拖垮别人 |
| 5 | 长链路（检索 + 多轮推理）如何设计并降低 P95？ | [`lab-10`](labs/lab_10_p95_optimization.py) | 先归因再优化；P95 496→271ms，检索 P95 1288→61ms，LLM 调用 4.0→2.2 次/请求 |
| 6 | 完整的 Agent 缓存体系：哪些能缓存，哪些不能？ | [`lab-07`](labs/lab_07_cache_system.py) | 5 层缓存 + 声明式策略表；命中率 0→67%，成本 −67%，错误命中/越权/过期全部归零 |
| 7 | token 成本持续增长，有哪些降本手段？ | [`lab-12`](labs/lab_12_cost_reduction.py) | 9 种手段逐项对账：$0.0308→$0.0011/请求（−96%），且**质量没有下降** |
| 8 | 多用户并发下会话隔离如何工程化落地，如何避免串会话？ | [`lab-16`](labs/lab_16_multitenant_isolation.py) | `(tenant, user, session)` 三元组 key + 每层断言；跨会话事故 → 0 |
| 9 | 异步任务大量堆积，队列如何治理？ | [`lab-05`](labs/lab_05_queue_governance.py) | 有界 + 背压 + 租约 + DLQ + 老化时间（age 才是核心指标） |
| 10 | 小模型便宜但弱、大模型准但贵，如何平衡？ | [`lab-13`](labs/lab_13_model_routing.py) | 级联路由：成本 −38%，准确率 0.517→0.933，大模型占比 100%→38% |
| 11 | 生产环境如何做模型调度？ | [`lab-13`](labs/lab_13_model_routing.py) | 降级链 + 灰度回滚 + 预算闸门；路由决策**不能问模型自己** |
| 12 | 百万级知识库检索 RT 越来越高，如何优化？ | [`lab-10`](labs/lab_10_p95_optimization.py) | 10 万篇实测：检索 P95 1288→61ms，扫描文档 100000→50 |
| 13 | Agent 服务如何做启停设计，如何避免发布期间报错？ | [`lab-01`](labs/lab_01_service_lifecycle.py) | liveness/readiness 分离 + 预热 + 优雅排空；丢弃请求 24→0 |
| 14 | 长任务执行失败又要从头跑，如何解决？ | [`lab-09`](labs/lab_09_long_task_resume.py) | checkpoint + 幂等键 + saga 补偿；浪费 token → 0 |
| 15 | 哪些逻辑必须工程硬编码，不能交给 LLM 判断？ | [`lab-14`](labs/lab_14_hardcode_boundary.py) | 越权 395→0、金额偏差 185 万→0、失控循环 20→0、解析失败率 0.43→0 |
| 16 | 如何评定 Agent 的性能、成本和稳定性？看哪些指标？ | [`lab-18`](labs/lab_18_observability_slo.py) | 四层指标 + burn-rate 告警；归因准确率 0.425→0.8，误报率 0.5→0 |
| 17 | 批处理时 CPU 和内存瞬间打满，怎么解决？ | [`lab-17`](labs/lab_17_batch_resource.py) | 流式分页 + CPU/IO 分池 + 容量预留；峰值 RSS −99%，交互式 P95 −62% |
| 18 | 第三方 LLM 抖动、超时、报错，如何保证可用？ | [`lab-06`](labs/lab_06_layered_timeout.py) | 全抖动退避 + 共享重试预算 + 熔断 + 舱壁 |
| 19 | 多轮对话上下文越来越大、越来越冗余，如何治理？ | [`lab-08`](labs/lab_08_context_governance.py) | 6 种压缩技术 + 预算分配 + **压缩率 vs 关键事实保留率**权衡 |
| 20 | 企业级 Agent 的权限与数据隔离、多租户如何落地？ | [`lab-16`](labs/lab_16_multitenant_isolation.py) | 串会话 122500→0、越权召回 495→0、串缓存 12→0；检索 P95 19.8→5.8ms |
| 21 | 从 0 搭建生产级 Agent 平台，工程顺序是什么？ | [`lab-03`](labs/lab_03_task_startup_order.py) | 8 层递进路线图 + 每层验收门槛；顺序错了返工代价量化 |
| 22 | 多种上下文压缩技术具体有哪些？ | [`lab-08`](labs/lab_08_context_governance.py) | 窗口/摘要/状态外置/结果引用/去重/分层记忆 |
| 23 | 如何开启子 Agent？ | [`lab-15`](labs/lab_15_tools_subagents.py) | 上下文隔离 + 权限收窄 + 预算继承 + 取消传播；并证明**该不该拆** |
| 24 | 如何调用工具？ | [`lab-15`](labs/lab_15_tools_subagents.py) | 参数校验 + 超时 + 有界循环 + 错误回灌给模型 |
| 25 | 如何编写工具？ | [`lab-15`](labs/lab_15_tools_subagents.py) | 描述质量决定选择正确率；幂等/副作用/确认必须声明 |

> 更细的逐问索引（含"工程结论 / 代码位置 / 可复现命令"）见 [`docs/ANSWERS.md`](docs/ANSWERS.md)。

---

## 为什么这样设计

教学项目最常见的失败是**只有正确做法，没有错误代价**。所以这里每个 lab 都强制四段结构：

```
1. 复现故障      [BROKEN-REPRODUCED] 打印故障时的真实数字
2. 观测 / 归因    用指标体系说明"你怎么知道是这个原因"
3. 修复          给出生产做法，并说明代价与边界
4. 验证          [VERIFY] 同一份负载下 before -> after，方向必须正确
```

四条硬性约束：

1. **核心教学实验使用标准库**。真实浏览器、文档解析、本地 embedding 等可选集成需要额外依赖，见对应配置文档。
2. **每个结论都有数字**。`verify.py` 会解析所有 `[VERIFY]` 行，若某条结论的方向反了
   （"修复后更差"），验收会失败——防止项目随时间长歪。
3. **逻辑用时序可压缩**。超时预算、熔断冷却、限流窗口这些用可注入的 `VirtualClock`
   在毫秒内演示完，但**语义完全不变**。
4. **不躲开难的部分**。串会话、越权、重复下单、重试放大成本这些"出事就是事故"的点，
   都真实复现出来并统计事故次数。

---

## 目录结构

```
agentlab/            零依赖核心 harness（冻结契约，见 docs/CONTRACT.md）
  util.py            输出协议 + 百分位统计 + 并发跑批 + RSS 观测
  metrics.py         Counter/Gauge/Histogram + Prometheus 风格导出
  tracing.py         span 树 + 耗时归因 + critical path
  tokens.py          近似 tokenizer + 模型阶梯（价格/延迟/质量/并发）
  providers.py       模拟 LLM：并发上限、排队、429、5xx、挂死、前缀缓存
  store.py           模拟知识库：线性扫描 / BM25 倒排 / 两阶段检索
  orchestration.py   Deadline / CircuitBreaker / Bulkhead / Retry / TokenBucket
  clock.py           RealClock / VirtualClock（把逻辑演示压缩到毫秒）

labs/                51 个默认实验，另有 1 个可选 ANN、3 个 Windows 原生实验
agentplat/           Capstone：运行内核、原生隔离、真实文档知识库与压测平台
  config.py          所有旋钮集中一处，每项标注来源 lab
  context.py         多租户隔离 + 会话治理 + 上下文压缩
  cache.py           5 层缓存 + 能否缓存的声明式策略表
  resilience.py      熔断 / 舱壁 / 分层限流 / 模型路由
  tools.py           工具注册、Schema 校验、超时、幂等、权限
  checkpoint.py      长任务断点续跑 + 幂等 + 补偿
  engine.py          一次请求的完整生命周期编排
  service.py         生命周期、readiness、优雅停机、探针端点
  loadgen.py         多租户混合负载 + SLO 合规判定
  run.py             Capstone 入口（含 broken/fixed 对照组）

verify.py            端到端验收：跑通 + 协议完整 + 结论方向正确
docs/                契约、逐问索引、蓝图、硬编码边界清单
```

---

## 建议的学习路径

**第一遍：按问题找答案**（1~2 小时）
从上面的 25 问表里挑你正在踩的坑，跑对应的 lab，看 `[VERIFY]` 的数字。
每个 lab 独立可跑，不用按顺序。

**第二遍：按依赖顺序系统看**（1~2 天）
工程顺序本身就是一个结论（见 `lab-03`）：

```
① 链路跑通 + 打点      lab-18       没有观测就没有优化，先能看见
② 可靠性原语           lab-06       预算/重试/熔断/舱壁，这是分界线
③ 隔离与限流           lab-04 lab-16 lab-17
④ 缓存与成本           lab-07 lab-12 lab-13
⑤ 上下文与状态         lab-08 lab-09
⑥ 工具与子 Agent       lab-15 lab-14
⑦ 队列与长任务         lab-05 lab-09
⑧ 服务生命周期         lab-01
⑨ 串起来压测           agentplat
```

**第三遍：改参数做实验**
这是这个项目真正的用法。每个 lab 里的阈值、容量、超时都是可改的：

```bash
# 把上游调慢 3 倍，看分层预算还兜不兜得住
python -c "..."   # 或直接改 lab 里的 srv.set_latency(...)

# 把客户端限流配额调到容量的 2 倍，观察它是怎么变成故障源的
```

---

## Capstone：跑一遍完整平台

```bash
python -m agentplat.run                  # fixed vs broken 对照 + SLO 合规判定
python -m agentplat.run --mode fixed     # 只看生产版本
python -m agentplat.run --probes         # 顺便起 /livez /readyz /metrics 端口
python -m agentplat.run --dump-config    # 打印全部可调参数
```

它用**同一份多租户负载**（3 个租户，其中一个是会打满容量的批量大户）跑两遍：
一遍是"关掉所有防护"的反面版本，一遍是全防护的生产版本。实测对比：

| 指标 | 反面版本 | 生产版本 | 变化 |
| --- | --- | --- | --- |
| 有效吞吐（成功/s） | 2.0 | **22.8** | +1018% |
| P95 延迟 | 1980ms | **460ms** | −77% |
| 每成功成本 | $0.006974 | **$0.000234** | −97% |
| 跨会话事故 | 0 | 0 | 隔离始终生效 |

一个容易被误读的点：**两个版本的成功率很接近**。原因是反面版本把请求堵在慢路径上，
单位时间只处理了很少的请求；生产版本用同样的时间服务了 10 倍的流量。
所以对比必须看**有效吞吐（成功/s）**，只看成功率会得出"两边差不多"的错误结论。

---

## 把它当真实服务跑起来（手工探索）

上面的 `agentplat.run` 是一次性压测。如果你想**亲手打这个 agent**、看它每一步在干什么，
启动 HTTP 服务：

```bash
python -m agentplat.demo --port 8791 --corpus 20000 --seed-cache
# 浏览器打开 http://127.0.0.1:8791/
```

只绑定 `127.0.0.1`（演示服务，别暴露公网）。端点：

| 端点 | 作用 |
| --- | --- |
| `/` | 首页：点一下就能走完整条链路（含"同句命中缓存""换租户不共享"等对照） |
| `/ask` | 提问：`?q=...&tenant=...&user=...&session=...`，加 `&format=json` 拿结构化结果，加 `&stream=1` 走 SSE |
| `/livez` `/readyz` | 探针（k8s 语义：存活只看进程，就绪才看能力） |
| `/metrics` | Prometheus 风格指标 |
| `/history` `/trace/<id>` | 请求列表与**单次请求的完整 span 树** |
| `/sessions` | 会话与租户隔离状态 |
| `/admin/degrade` `/admin/recover` | 注入/恢复上游劣化，观察重试→熔断→降级 |
| `/admin/drain` | 优雅停机（摘流 → 排空 → 进程退出） |

不想开浏览器也行，一条命令自动跑完六个场景：

```bash
python tools/demo_driver.py            # 自己拉起独立实例，跑完自动回收
```

它会依次演示：**五层缓存分层 → 多租户隔离 → 指标导出 → 熔断熔断打开 → 自动恢复 → SSE 流式 → 优雅停机**。
实测片段（真实输出）：

```
冷请求        alpha  mid-32b   -          400.8ms
同租户同句     alpha  cache     L1 精确      0.1ms     ← 缓存命中，快 4000 倍
换租户同句     beta   large-400b -         1501.3ms   ← 缓存按租户隔离，绝不复用

劣化期间: 0-2 次失败(3s) → 3-7 次 CIRCUIT_OPEN(0.3ms)   ← 熔断打开，不再白等
恢复后:   5 次瞬间拒绝 → 第 6 次放行成功 → 熔断闭合     ← 自动恢复，不用重启
SSE:      TTFT=150.7ms  端到端=605.7ms                ← 两个指标必须分开定 SLO
```

`tools/demo_driver.py` 里有一条**排障教训**值得单独记：它不用 PowerShell 的
`Invoke-RestMethod`，因为 `&` 在 PowerShell 里是语句分隔符，`?q=x&tenant=y`
会被吃掉一半参数 —— 我最初就踩了这个坑，服务只收到 `q`，`tenant` 恒为默认值，
现象看起来像"缓存串租户"。**排障第一原则：先怀疑自己的客户端。**

---

## 它首先是一个编码 Agent

这个项目**首先是一个能真的读写代码、跑命令的 agent**，教学 lab 是它之上的第二层。

```bash
python -m agentplat.loop --task "写快速排序 quicksort.py 和测试，跑 pytest 必须全绿"
python -m agentplat.loop                      # 不给 --task 进交互模式，任务共享工作区
```

### 它和"问答机器人"的区别

| | 问答机器人 | 这个 agent |
| --- | --- | --- |
| 执行形状 | 检索 → 拼 prompt → 调一次模型 | **循环**：调模型 → 执行工具 → 回灌结果 → 再调模型 |
| 工具 | 只有检索/计算 | `list_dir` `read_file` `grep` `write_file` `append_file` `edit_file` `delete_file` `run_shell` `finish` |
| 改代码 | 只能"说"代码 | **真的落盘**，真的跑 `pytest` |
| 终止 | 一次性返回 | 模型调 `finish`；策略钩子可介入 |

实测（真实 DeepSeek，19 轮 / 48 秒 / **$0.17**）：agent 自己跑 `pytest` → 看到失败 → 改代码 → 重跑 → 跑 `flake8`，
最终交付 **44 个测试全绿 + 11 个我独立写的边界用例全部通过**，而且实现是工程化的
（随机主元避免 O(n²)、小区间插入排序、递归只深入较小一侧保证栈深对数级）。

### 安全边界：`workspace/` 是唯一的可操作区

```bash
python -m agentplat.loop --workspace ./myproject   # 换工作区
```

* 所有路径必须落在工作区内 —— 用 `resolve()` 解真实路径再判前缀，**不是**检查字符串里有没有 `..`（后者能被符号链接/编码绕过）
* 命令白名单 + 破坏性模式拦截（`rm -rf /`、`curl|sh`、`git push` 等一律拒绝）
* **定位是防手滑，不是防恶意** —— 真隔离要靠容器

### 终止：机制与策略分离（这一点我一开始写错了）

我最初把"最大轮数 / 最大工具数 / 预算"三条硬编码进循环，并声称这是必须的。
**核对 DSH 源码后确认这个说法不对**（`dsh-agent-loop` 的 `README.zh.md:200`）：

> 没有内置轮次预算：工具调用或 steering 会让当前轮次继续；限制失控轮次的策略
> 必须从既有生命周期扩展点（如 `agent/turn-stopping`）执行取消。

DSH 循环里只有 `maxParallelToolCalls`（单 step 并发）和 `maxTokens`（单请求输出）；
轮次预算放在**目标层**（`dsh-goal` 的 `defaultMaxGoalRounds` + 四态持久化）。

所以本项目改成**策略钩子**，机制与策略分离：

```python
class TurnPolicy(Protocol):
    def __call__(self, ctx: LoopContext) -> Stop | None: ...
```

已实现 `BudgetPolicy`（成本/时间）、`MaxIterationsPolicy`（软上限**在验证过之后不再拦**）、
`CompositePolicy`（组合）、以及自定义策略（如"连续 N 轮无进展就停"）。

**一处刻意偏离**：测试用例④发现"一轮里塞 50 个工具调用"时，策略只在轮次边界检查、
根本来不及介入（100 次调用全部跑完）。DSH 靠外层目标层 + `dsh-timeout` 看门狗兜底，
本项目是单进程脚本、没有外层治理，所以额外加了 `MAX_TOOLS_PER_STEP = 12`。
这不是更好的设计，是现实妥协 —— 理由写在代码注释里。

### 工具结果 spill：上下文有界的关键（对齐 `dsh-spill-policy`）

编码 agent 的上下文是被**工具输出**撑爆的，不是被用户输入撑爆的。
实测：一次"写 quicksort + 跑测试"任务，31 轮消耗 **188,425 输入 token**，
绝大部分是反复回灌的工具输出 —— 而模型每轮都要把历史**重发一遍**，
所以成本随轮数平方增长。

`agentplat/spill.py` 实现 DSH 的 spill 策略（`maxInlineBytes`）：

| | 效果 |
| --- | --- |
| 14,090B 的 pytest 输出 | 上下文里只留 **775B 预览**（-94.4%） |
| 落盘文件 | 与原始**逐字节一致**（信息不丢，只是移出热路径） |
| 回灌内容 | 带 locator，模型可 `read_file` 按需回取 |
| 同内容重复 spill | 复用同一文件，不重复占盘 |

**spill ≠ 截断**：截断真的丢信息，spill 只是把它挪出热路径 —— 所以能用在生产。

### 独立验证：不要相信 agent 的自评

```bash
python tools/evaluate_delivery.py     # 让 agent 真跑任务 + 独立验证交付物
```

`tools/evaluate_delivery.py` 先让 agent 跑任务，再**不看它的总结**，用自己的
11 个边界用例（随机/已排序/逆序/全相等/只两个值/大量重复/负数浮点/字符串/空数组…）
独立验收，并且每个用例都在**子进程里带硬超时**跑。

为什么必须这样：本项目实测出现过 agent 写的 quicksort **23 个自测通过**，
但在随机数组上会挂起、在逆序数组上退化成 **O(n²)**（964ms@n=8000）。
如果只看"测试通过率"或 agent 自己的总结，这些 bug 会直接进生产。

---

## 接入真实 LLM

面板默认用内置模拟器（零依赖、不联网）。想用真模型，打开 **`/settings`** 页
填上任意 **OpenAI 兼容**端点的 key 即可 —— **可靠性机制完全不变**：
并发闸门、超时预算、重试、熔断、缓存、成本归因全部照旧生效，
因为真实后端只替换了"怎么产生这一次回答"。

已验证可直连的厂商（本机实测，均返回正确的 401 而非超时）：

| 厂商 | Base URL | 说明 |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com` | 国内直连，便宜 |
| OpenAI | `https://api.openai.com/v1` | 本机实测**可直连** |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 三档模型齐全，适合演示路由 |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` 有免费额度 |
| Moonshot | `https://api.moonshot.cn/v1` | 长上下文 |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | 完全本地、不需要 key |

### 成本护栏（接真实 key 后这是**安全机制**）

真实 key 一接上，"实验"就不再是免费的 —— 一个写错的循环会真的花掉你的钱。
所以 `agentplat/guard.py` 提供三道闸门，**默认开启**：

| 闸门 | 默认 | 防什么 |
| --- | --- | --- |
| `max_llm_calls_per_run` | 400 次 | 防"写错的循环"（最常见的烧钱方式） |
| `max_usd_per_run` | $1.00 | 防"单次实验超预期"，按 token 单价实时累计 |
| `max_tokens_per_request` | 8192 | 防"上下文爆炸"（超长 prompt 会成倍放大成本） |

触发时**抛异常并停止**，而不是静默降级 —— 静默降级会让你以为数据是真的，
实际后半段是本地模板，比直接失败更危险。

```bash
python -m agentplat.demo --max-usd 0.5 --max-calls 200   # 调紧护栏
python -m agentplat.demo --dry-run                       # 干跑：不发真实请求
```

**干跑必须真的不出网**：这一点我踩过坑 —— 只在 guard 里标了 `dry_run` 是不够的，
实验代码直接调用 client 会绕过它，"干跑"真的打出了网络请求。现在干跑路径
在实验层就短路返回（实测 `用时 0.0s`、延迟 0ms）。

### 速度优化

| 手段 | 效果 |
| --- | --- |
| **并发请求合并（singleflight）** | 10 个用户同时问同一句话 → 只发 **1 次**真实请求。缓存只挡得住"先后到达"的重复，挡不住"同时到达"的 |
| 前缀缓存 | 稳定前缀命中的输入 token 按 10% 计费 |
| L1/L2 缓存前置 | 命中即返回（0.1ms），不再花钱 |
| 精确缓存先于语义缓存 | L1 无错答风险，优先走 L1 |

---

## 实验：推理等级 / 模型档位的成本-质量权衡

```bash
python -m agentplat.experiment_levels --dry-run       # 先干跑看要花多少钱
python -m agentplat.experiment_levels --tasks 8       # 小样本真跑
python -m agentplat.experiment_levels --tasks 20 --budget-usd 0.5
python -m agentplat.experiment_levels --levels L0,L2,L4 --json out.json
```

用**同一批带标准答案的任务**跑 5 个推理等级，每个测四项：准确率、每任务成本、
P50/P95 延迟、输出 token 数。然后算出**帕累托前沿**（哪些等级没有被"更便宜且更准"
的等级全面碾压）。

| 等级 | 模型 | 推理参数 | 采样 | 说明 |
| --- | --- | --- | --- | --- |
| L0 | small | — | 1 | 最省，能答就答 |
| L1 | small | `reasoning_effort=high` | 1 | 同模型多想一会儿 |
| L2 | mid | — | 1 | 换更大的模型 |
| L3 | mid | — | **3** | 自一致性投票，成本×3 |
| L4 | large | `reasoning_effort=high` | 1 | 最贵最准 |

实测片段（真实 DeepSeek 调用，4 题 × 2 等级，总计 **$0.0019**）：

```
等级  说明              准确率    $/任务      P50      P95  out tok   相对成本
L0   小模型·直接答         75%  0.000225    914ms    995ms      89      1.0×
L2   中模型·直接答         75%  0.000248    982ms   1151ms     101      1.1×

帕累托前沿：✓ L0
被碾压：    ✗ L2 （被 L0 碾压 —— 同样准确率、却更贵更慢）
```

**注意这次 L2 被碾压是因为配置里 small 和 mid 指向了同一个模型** ——
这恰好演示了帕累托分析的价值：它会把"花了更多钱却没买到任何提升"的选项直接标出来。
换成三档不同的模型，曲线才会展开。

三条纪律（评估类实验的通用要求）：

1. **判分器必须是确定性代码**，不能靠另一个模型打分 —— 否则评估器本身成了变量，结论无法复现。
2. **成本是乘法放大的**：自一致性 ×3 就是 3 倍成本；提高推理等级让输出 token 变多也是更贵。
3. **推理等级对题型不敏感**：事实题提高推理等级几乎没有收益，多步推理题才有。
   所以生产上应该**按题型路由**，而不是全局调高推理等级。

---

## 环境

- **Python 3.11+**（用到 `X | Y` 类型语法），无第三方依赖
- Windows / macOS / Linux 均可
- PowerShell 下建议先执行一次（避免中文乱码）：

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
```

代码本身已做处理（`agentlab.util` 导入时会把 stdout 切到 UTF-8），
但把控制台编码也对齐会更稳。

---

## 一句话总结每个 lab 的结论

| Lab | 一句话结论 |
| --- | --- |
| 01 启停 | readiness 摘流要早于停止服务；liveness 绝不能包含依赖健康，否则抖动会被放大成滚动重启 |
| 02 并发/内存 | panic 是正反馈不是单点；先分清"内存膨胀"和"内存泄漏"，前者靠 gc、后者靠 tracemalloc |
| 03 落地顺序 | 观测必须在优化之前；多智能体是最后一步，不是第一步 |
| 04 限流 | 服务端限流保护自己，客户端限流保护上游；没有按租户隔离就是一个租户拖垮全站 |
| 05 队列 | 队列的核心指标是**最老任务的 age**，不是长度；毒丸必须靠重试上限 + DLQ 隔离 |
| 06 超时熔断 | 超时是往下传的预算；重试必须挂在同一预算上；熔断保护的是调用方自己 |
| 07 缓存 | 能不能缓存是工程硬编码的决策；权限相关、有副作用、时效性强的一律不缓存 |
| 08 上下文 | 压缩不是免费的，必须同时看"压掉多少"和"关键事实留下多少" |
| 09 长任务 | 可恢复的前提是每步落盘；幂等键必须由工程生成，不能问模型"这是不是重复的" |
| 10 P95 | 先归因再优化；80% 收益来自"别让昂贵计算见到太多候选" |
| 12 降本 | 降本必须与质量一起度量，否则省下来的钱会被返工吃掉 |
| 13 路由 | 级联路由用小的跑大部分、把不确定的升级；路由决策依赖实时预算，不能缓存 |
| 14 硬编码 | 凡是有副作用、涉及权限金钱、影响控制流的，都必须是代码 |
| 15 工具/子 Agent | 工具描述质量直接决定选择正确率；子 Agent 是隔离手段，不是架构装饰 |
| 16 多租户 | `(tenant, user, session)` 三元组必须显式传递并在每层断言，缺一不可 |
| 17 批处理 | CPU 密集与 IO 密集必须分池；给交互式留容量要硬性预留，不能"看情况" |
| 18 观测 | 没有观测就没有结论；告警要盯症状（burn rate），不要盯原因 |

---

## 许可

用于学习与教学。代码里的模型价格、延迟、质量分是**近似值**，用于演示权衡关系，
不要当作真实报价使用。

检索、语义记忆、自动团队规划和模块化执行升级见 [runtime-20 说明](docs/RUNTIME20_UPGRADE.md)。`python verify.py --ann` 包含真实 HNSW 实验。

工具守卫、宿主最终验收事实、统一评测格式和进程树清理见 [运行契约改进](docs/RUNTIME_CONTRACTS.md)。`python verify.py lab-56` 运行相应负对照实验。
