# 当前项目说明

本页对应当前工作树实现，核对日期为 2026-09-29。它是使用入口和能力边界索引，
不是生产认证或综合能力评分。带版本号、日期的文档保留当时的观察，不追写历史成绩。

## 启动与对话

运行 `python tools/start_desktop.py`，在设置页面配置自己的 LLM API。
`python -m agentplat.knowledge_cli open` 可重新打开本机登录入口。
原生沙箱、浏览器、OCR、向量模型和第三方工具需要各自运行条件，安装 Skill 不会自动提供它们。

普通对话不绑定项目，有独立附件与产物目录；需要执行本地命令时选择项目。
项目支持多个不重叠文件夹。运行期间可追加提示，问题/授权卡按发生位置插入时间线，
已回答记录可折叠。会话恢复、分支与文件快照仍有各自限制，见
[对话分支](CONVERSATION-BRANCHES.md)、[交互时间线](CHAT-TIMELINE.md)。

## 当前能力与对应实现

| 功能 | 使用和边界 | 源码入口 |
|---|---|---|
| 文档导入 | 文本、CSV、Office、PDF、图片 OCR；格式依赖和限制见 [知识库](KNOWLEDGE.md) | `agentplat/knowledge.py`、`document_extract.py` |
| 知识库范围 | 会话私有、项目共享、公共库主动启用；取消选择不会清除既有聊天引用；见 [范围选择](KNOWLEDGE-SCOPES.md) | `agentplat/knowledge_scopes.py` |
| 语义检索 | 本地 embedding + BM25/RRF；大库支持校准 HNSW，缺模型/索引时降级；见 [RAG](RAG_HYBRID.md) | `agentplat/vector_knowledge.py`、`ann_index.py` |
| 多个 Skill | 目录/ZIP 导入、分页目录、多行描述、同名来源区分；加载成功不等于功能验证通过；见 [技能](SKILLS.md) | `agentplat/plugins.py`、`skill_metadata.py`、`skill_import.py` |
| 权限恢复 | 精确命令单次审批，保留宿主正常运行变量，独立执行时限；不永久扩权；见 [权限恢复](PERMISSION-RECOVERY.md) | `agentplat/approvals.py`、`execution_environment.py` |
| 多 Agent | 递归、团队消息与 DAG；独立验收继承选定知识库快照；不是已证明最优的自主委派 | `agentplat/subagents.py`、`team_planner.py` |
| 完成判定 | 子任务 completed、过程 reporting 都不等于宿主通过；要求/产物/知识库范围改变使旧验收失效 | `agentplat/review_decision.py`、`independent_review.py` |
| Spill | 大输出落盘、按内容复用、分段回读；尚无完整引用保护与自动过期回收 | `agentplat/spill.py` |

每次宿主执行返回执行后端、是否成功、后续普通命令后端和诊断说明。
超时不能仅凭退出码 0 判成功；清理失败保留已观察的命令结果。
验收累计 token 和耗时是整个子任务的量，不能归到最后一次模型请求。

## 验证入口与本次结果

默认教学实验为 51 个，清单由 `verify.py` 的 `LABS` 定义；另有 1 个 `--ann`
实验和 3 个 `--native` 实验。编号并非连续，因此不能从最大编号推算数量。

```powershell
python verify.py --list
python verify.py
python verify.py --ann lab-53
python verify.py --native lab-30 lab-31 lab-32 --jobs 1
```

本次宿主执行改动的相关回归为 **91 项通过**，命令如下。这是选定模块的回归，
不是全仓库所有测试，也不是本次重新跑过全部教学实验。

```powershell
python -m unittest tools.test_host_environment tools.test_permission_recovery tools.test_review_hardening tools.test_human_workflow tools.test_approvals tools.test_runtime tools.test_runtime_contracts tools.test_quick_resume tools.test_review_decision tools.test_review_budget tools.test_review_convergence tools.test_chat_timeline tools.test_live_features -q
```

知识库和 Skill 另有对应回归：

```powershell
python -m unittest tools.test_knowledge_scopes tools.test_knowledge tools.test_skill_catalog tools.test_plugins tools.test_skills_http -q
```

实际宿主 conda、numpy、PyTorch 探测成功；原生沙箱仍缺宿主 numpy。
一次真实 LLM 授权流程通过：6 次调用、30.11 秒、约 $0.00156（应用估算，非账单）。
测试器在隔离审批库中只批准固定无害命令，提示包含候选解释器；不代表自由任务中模型总能选对环境。
更多方法及历史失败样本见 [权限恢复](PERMISSION-RECOVERY.md)。

## 尚未解决的边界

- 上游 LLM 超时仍可能发生；过程检查通过不自动转为最终验收通过。
- 模型仍可能误判环境或不合算地委派；提示和状态约束不能保证每次推理正确。
- 跨库检索合并尚无大样本相关性评测；ANN 能运行不等于百万级真实问答质量已经证实。
- Spill、证据与子任务副本的统一生命周期回收尚未实现，不能直接清空仍被引用的目录。
- 本机执行按宿主账户权限访问资源；原生隔离的能力和网络限制见 [沙箱说明](NATIVE-SANDBOX.md)。

## 历史文档与公开仓库

`RUNTIME-*`、`RUNTIME*_VALIDATION`、`SELF-ASSESSMENT-*`、`SOURCE_REVIEW_REMEDIATION`
等记录对应阶段。它们可能描述后来已修复的缺口；当前使用指南优先于历史的“目前/下一步”。
[runtime-20](RUNTIME20_UPGRADE.md) 记录 ANN、语义记忆和 DAG 的升级，
[运行契约](RUNTIME_CONTRACTS.md) 记录接口与宿主完成状态的改进。

公开仓库仅包含代码、测试、实验和文档。不上传 API 凭据、私有会话、附件、
知识库、模型权重、诊断日志或本地下载的 Skill 压缩包。
`.diagnostics/…` 路径仅说明本机证据位置，公开克隆不具备这些原始记录。
