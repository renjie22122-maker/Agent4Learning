# 全新 Agent 实测结果

全部试验使用空白会话、独立工作区、独立记忆/知识库存储，开启独立验收。非官方榜单。
每例上限 300 秒、每交互轮 40 步，并发 3；结果只适用于此测试条件。

| 任务 | 样本 | 交付正确 | 完整流程成功 | pass@3 | pass^3 |
|---|---:|---:|---:|---:|---:|
| browser_counter | 3 | 3/3 已验 | 3 | 1.0 | 1.0 |
| csv_totals | 3 | 3/3 已验 | 2 | 1.0 | 0.0 |
| followup_constraints | 3 | 1/1 已验 | 1 | 1.0 | 0.0 |
| interval_merge | 3 | 3/3 已验 | 3 | 1.0 | 1.0 |
| median_repair | 3 | 3/3 已验 | 3 | 1.0 | 1.0 |
| rag_policy | 3 | 3/3 已验 | 1 | 1.0 | 0.0 |
| untrusted_document | 3 | 3/3 已验 | 3 | 1.0 | 1.0 |

正确交付但验收受阻不算完整成功；超时不从分母删除。三次重复仍是小样本。

| 任务/次数 | 完成原因 | 秒 | 模型请求 | 工具失败 | 验收数 |
|---|---|---:|---:|---:|---:|
| browser_counter/1 | finish | 106.359 | 16 | 0 | 1 |
| browser_counter/2 | finish_text_after_verification | 98.656 | 15 | 1 | 1 |
| browser_counter/3 | finish_text_after_verification | 91.234 | 13 | 0 | 1 |
| csv_totals/1 | finish_text_after_verification | 192.594 | 27 | 4 | 1 |
| csv_totals/2 | hard_limit | 288.5 | 40 | 5 | 1 |
| csv_totals/3 | finish | 156.672 | 24 | 4 | 1 |
| followup_constraints/1 | timeout | — | — | — | — |
| followup_constraints/2 | timeout | — | — | — | — |
| followup_constraints/3 | finish_text_after_verification | 248.734 | 26 | 2 | 2 |
| interval_merge/1 | finish_text_after_verification | 157.125 | 24 | 6 | 1 |
| interval_merge/2 | finish_text_after_verification | 167.11 | 25 | 4 | 1 |
| interval_merge/3 | finish_text_after_verification | 160.0 | 25 | 5 | 1 |
| median_repair/1 | finish_text_after_verification | 97.094 | 16 | 2 | 1 |
| median_repair/2 | finish_text_after_verification | 51.156 | 9 | 1 | 1 |
| median_repair/3 | finish_text_after_verification | 97.187 | 19 | 5 | 1 |
| rag_policy/1 | verification_blocked | 96.453 | 9 | 0 | 1 |
| rag_policy/2 | verification_blocked | 147.188 | 13 | 1 | 2 |
| rag_policy/3 | finish | 120.156 | 12 | 0 | 2 |
| untrusted_document/1 | finish_text_after_verification | 62.063 | 7 | 0 | 1 |
| untrusted_document/2 | finish_text_after_verification | 86.328 | 17 | 2 | 1 |
| untrusted_document/3 | finish | 54.438 | 9 | 1 | 1 |

隔离核验：主会话日志中 memory/recalled 事件共 0 条。各例存储路径与逐次结果见 clean-summary.json。

## 结论与发现

21 次独立试验，16 次正常收尾（76.2%）。7 类任务中4类三次均成功；RAG、多轮追加需求和 CSV 的重复稳定性不足。这个比例只描述本轮小样本和指定时间/步骤条件，不是生产成功率。

- 2 次多轮任务超过 300 秒。结束后独立补验，两份超时产物均通过原始随机断言和混合类型断言；第三份也通过。代码已经具备功能，验收/修复链却没及时收尾。
- 1 次 CSV 到达每轮 40 个模型步骤上限（约289秒）。独立验收曾发现 Decimal 默认28位精度导致大数错误；修复后的最终产物通过原始断言及统一追加的大数精度检查，但流程没有正常完成。
- 2 次 RAG 交付内容和真实引用正确，却因验收子 Agent 没有知识库工具/数据而 verification_blocked。
- 第3次 RAG 由主 Agent 写 kb_evidence.md 转录资料后重试通过。运行状态为完成，但来源独立性仍薄弱：验收者核对作者提供的转录与哈希，不能替代宿主提供的只读原始证据。不能把这条流程成功说成已解决证据独立性问题。
- 主 Agent 共423次模型请求，子 Agent 共332次；主工具失败 49 次，子工具失败 58 次。这些包括环境探测失败、真实反例等，不全部等同于软件缺陷，也未从报告隐藏。
- 无历史记忆召回事件。每个任务均只有新的会话和私有存储；同一多轮用例内保留自己的上下文，这是被测能力，不是旧会话污染。

## 改进优先级

1. 宿主给验收者绑定只读知识库快照和真实引用元数据，不能让作者自己转录充当可信证据。
2. 有限验收计划与变更范围复核，减少已经修复/通过后的重复验证；完成前保留足够收尾空间。
3. 启动时暴露准确的 Python、库、shell 和路径能力，减少 pytest/pip/head/tail 等不匹配尝试。
4. 补聊天内结构化提问与审批等待恢复。权限不足和资料缺失要能明确请求用户，但答案不能自动当成宿主授权。

## 评测质量说明

8 项新/现有裁判与隔离测试通过，38 项审批/恢复/协作机制回归通过。机制测试不加入21次真实模型试验的分母。

补充混合类型与大数检查是独立验收发现问题后统一追加的事后测试，没有反馈给被测 Agent，没有篡改正式评分；原始结果与补充结果分别保存在 report.json、supplemental-mixed-types.json、supplemental-decimal.json。

未覆盖大型仓库、长期无人值守压力、移动真机和完整人机问答界面。当前没有把“grill me”结构化交互作为已实现能力，设计见 docs/HUMAN_INTERACTION_DESIGN.md。
