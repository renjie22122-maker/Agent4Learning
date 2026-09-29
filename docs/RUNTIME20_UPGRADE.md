# runtime-20：检索、记忆、团队规划与执行模块升级

本轮在 runtime-19 基础上落地四项能力，保留旧会话格式、工具协议和 `agentplat.loop` 的公开入口。不把功能实现等同于主流基准成绩。

## 1. 大知识库索引

- 新增 FAISS HNSW，SQLite 仍是原文、版本和启用状态的权威来源。
- 默认 `ann_backend=auto`、`ann_min_vectors=50000`。小库使用精确搜索；知识库页面“补建索引”或 CLI reindex 会为大库建立图索引。
- 新向量先参与精确增量检索，下次发布并入 HNSW。旧文档撤销后立即过滤，向量替换触发代际失效，降级精确检索并显示原因。
- 索引文件与映射有 SHA256 校验；先写完整不可变索引代，再原子切换清单。不会让查询读到半份索引。旧代保留，当前没有自动磁盘回收策略。
- 建索引时用 32 个独立随机查询对照精确搜索，逐步提高 efSearch，目标 recall@10 ≥ 0.95；未达标或同口径单查询 p95 不优于精确检索时，用 FAISS 的精确存储检索。这个抽样校准不保证真实用户查询的召回率。
- 保留关键词与向量 RRF 融合。ANN 是索引升级，不是新增答案生成器，也不是来源真实性证明。

安装其他机器上的可选依赖：`python -m pip install -r requirements-ann.txt`。本机依赖安装在 `.agent-runtime/ann-deps`，没有改动全局包。

```powershell
python -m agentplat.knowledge_cli --workspace D:\项目 reindex
python -m agentplat.knowledge_cli --workspace D:\项目 index-status
python -m agentplat.knowledge_cli --workspace D:\项目 ann-rebuild
```

实现采用 [FAISS 官方 HNSW 接口](https://github.com/facebookresearch/faiss/wiki/Faiss-indexes)。它仍是本机内存索引，不是分布式数据库；百万级容量尚未实测。

## 2. 语义长期记忆

- 已确认记忆使用本地 BGE embedding；结合关键词、向量相似度和时效召回。
- 项目、来源启用状态、记忆状态、过期时间和 revision 都在检索时检查。修改与删除会清除旧向量；后台处理旧版本时不会把过时结果写回。
- 长期记忆页面可建立索引、查看覆盖率和后台错误、检查相似条目。相似条目可能重复或冲突，需要人核实，不自动覆盖偏好。
- 首次明确建立索引后，后续启用或修改条目会自动增量建索引。未完成索引时仍有关键词检索。本机已启用这一模式；升级时已确认条目数为 0，没有导入历史聊天。
- 仍只从用户选定来源提取候选，候选需确认；不会把作者自评自动保存成事实。

向量记忆当前使用精确相似度匹配，主要针对个人/项目记忆量；大规模文档的 HNSW 与记忆数据库是不同存储。

## 3. 自动团队计划

主 Agent 现在可以通过 `plan_team` 自行提交带验收条件的任务图。宿主负责检查环路、按依赖启动、等待及管理状态；不依赖模型反复轮询。

写入节点：`pending → working → reviewing → merged`。验收使用作者副本的独立快照与独立模型上下文；通过后检查作者产物是否变化、父工作区是否冲突，再合并。依赖节点只有在合并后才启动。

只读节点产物标为 `reference_ready`，仅供后续任务核实。整个计划最终为 `ready_for_final_review`，主 Agent 仍需验证集成后的结果，不能把这个状态直接当用户任务成功。

工具：`plan_team`、`get_team_plan`、`wait_team_plan`、`revise_team_plan`、`cancel_team_plan`。团队页面展示计划图的节点、依赖、验收与修订历史。主提示词要求复杂可并行任务优先考虑计划工具，简单任务直接完成。

- 受阻节点可按实际失败证据修订；每节点最多三次尝试，保留旧任务和修订理由。不自动重试未知副作用。
- 沿用团队共享预算、并发、队列限制和动态权限继承。计划最多 24 节点且不超过配置的队列容量。
- 进程重启后未完成计划标为 interrupted，不擅自重新派发或重新合并。
- 这是主 Agent 决策、宿主执行的任务图编排，不是独立训练的规划模型。是否选择了最优拆分，仍需端到端任务集评估。

## 4. 主循环职责拆分

| 模块 | 职责 |
|---|---|
| `loop.py` | CodingAgent 构造、组件组合、兼容入口 |
| `loop_types.py` | 策略、请求/结果类型、主提示词 |
| `conversation_runtime.py` | 新对话、追问、持久化及恢复 |
| `context_runtime.py` | 上下文统计、压缩与压缩记账 |
| `model_runtime.py` | 模型请求、恢复重试、usage 记账 |
| `tool_runtime.py` / `tool_protocol.py` | 调用规范化、批量上限、意图落盘、权限执行、spill 与验证证据 |
| `review_lifecycle.py` | 完成前反射与独立验收门槛 |
| `turn_runtime.py` | 回合顺序、取消、策略判断和终止状态协调 |
| `agent_cli.py` | 命令行适配 |

使用显式生命周期方法组合，没有运行时生成代码或动态 exec。回合协调仍需维护共享 Agent 状态，本轮没有承诺无状态执行器或远程分布式恢复。

## 验证与可复现方式

```powershell
python -m unittest tools.test_upgrade_runtime
python verify.py --ann --jobs 4
python tools/evaluate_upgrade_runtime.py --output .diagnostics/upgrade/scale.json
python tools/check_team_plan_live.py --real .diagnostics/upgrade/new-team-run
```

- 三个新增实验：lab-53 ANN 撤销一致性（`--ann`）、lab-54 语义记忆与版本、lab-55 团队 DAG。默认实验 50 个，加 ANN 共 51 个；Windows 原生三个实验仍需单独 `--native`。
- `verify.py --ann`：51/51，158 条 VERIFY 指标。
- 本地真实 BGE：三组改写查询的 top-1 均正确；这是小规模定向用例，不是记忆准确率承诺。
- 真实模型 API：两个并行文件任务＋一个依赖集成任务，三个独立验收通过并合并；最终外部断言确认 `combined.txt == alpha:beta`。总用量 139270 tokens。报告 `.diagnostics/runtime20/team-live/report.json`，不公开提交私有运行日志。
- 512 维、10 万随机向量的初测发现 efSearch=512 时 recall@10 仅约 0.695；因此增加质量校准，不沿用这个未经验证的默认参数。64 维测试的高召回不能外推到 512 维。
- 检索报告保留了低召回/小库变慢的结果；提高搜索深度到 2048 后，100 个独立查询 recall@10 为 0.98，HNSW p95 8.40 ms、精确 p95 7.15 ms，没有提速。因此随后增加单查询 p95 的自动后端选择，不为 ANN 牺牲性能。最终参数结果见本机 `.diagnostics/runtime20/calibrated512.json`。随机向量延迟不包含 embedding、SQLite 候选过滤或答案生成，不能当作完整 RAG 的延迟。

未声称完成：分布式 ANN、百万文档吞吐证明、通用基准排名、自动消解所有记忆冲突、规划最优性或任意副作用 exactly-once。

相关回归 108 项通过（107 项联合运行，随后新增的未完成计划拦截检查及相关套件通过）；另有 CLI 与六组压缩检查通过。
