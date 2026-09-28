# Agent 能力评测方法

本项目新增的是自建小型能力集和机制实验，不是官方 SWE-bench、τ-bench 或 AgentBench 成绩，不可据此与排行榜比较。

## 参考的主流方法

- [SWE-bench 官方 harness](https://www.swebench.com/SWE-bench/reference/harness/)：将补丁置入固定环境，执行测试决定是否解决问题。本地采用“预置缺陷 → Agent 修复 → 外部随机断言”的方式。没有运行官方数据集；官方环境使用 Docker，本机并未因此安装 Docker。
- [τ-bench](https://github.com/sierra-research/tau-bench)：评价工具使用、交互与重复成功。采用逐次结果及 pass^k 可靠性统计；其旧仓库已提示任务过时，正式复现应使用仓库指向的新版本。这里不使用其旧任务。
- [THUDM AgentBench](https://github.com/THUDM/AgentBench)：在不同交互环境评价 Agent。本地覆盖代码修复、结构化数据、非可信文档、多轮需求与独立验收，而非只测单次文字回答。

## 能力任务与独立判定

| 任务 | 主要能力 | 外部验收 |
|---|---|---|
| median_repair | 定位缺陷、边界、避免修改输入 | 400 组固定种子随机输入、空列表、标准库参照 |
| csv_totals | CSV 解析、Unicode、精确金额 | 引号、嵌入换行、负数、100 行随机聚合 |
| untrusted_document | 指令与数据分离 | 金额 JSON 正确且受保护文件保持原样 |
| followup_constraints | 多轮追加需求保留旧约束 | 稳定去重、输入不变、limit 边界及随机输入 |
| --review 对照 | 编码与独立验收整合 | 同一代码任务开启子 Agent，最后仍由外部断言判定 |

不将 Agent 自己编写的测试作为最终裁判。编码完成后只复制目标源文件到新的原生沙箱执行检查，检查代码不提前提供给被测 Agent。保护文件由评测器在外部直接检查。测试输入固定，但真实模型生成具有随机性。评测目录保留任务、交付物、完整事件日志与裁判结果。

这只是正常协作模型的独立评测：不是针对有意攻击裁判的强安全竞赛。原生沙箱沿用宿主网络模式。隐藏断言用于避免自评偏差，不等于不可发现或防篡改的远程裁判。

## 指标与限制

- passed：外部结果通过。declared_ok：Agent 是否自称完成。false_success：自称完成但外部断言失败。
- pass@k：k 次至少成功一次；pass^k：k 次全成功。按组合数从重复试验估计，不足 k 次返回 null。两次都成功只是小样本观察，不证明真实成功率为 100%。
- 同时报告时间、模型请求、工具失败、输入 token 总量/峰值、验收次数、主子费用估计。输入总量含缓存和重复上下文，不等于全部按未缓存价格计费。
- 每例默认最多 24 个模型步骤、240 秒宿主进程超时；这些是可见的**评测条件**，不修改线上 Agent 的预算。超时会回收评测进程树并单独记录。
- 超时、沙箱/评测器错误单独分类；不伪装为通过。轨迹中的已完成工具输出也不自动等于整个任务完成。
- 覆盖面有限：尚无官方大规模仓库任务、长期真实用户模拟、移动真机、长时间压力跑分。单例子 Agent 对照不能说明多 Agent 普遍优于单 Agent。

## 使用

```powershell
# 只列计划，不发 API
python tools/benchmark_agent.py
# 全部任务，每类重复两次；输出目录必须不存在
python tools/benchmark_agent.py --real --runs 2 --output .diagnostics/benchmark-new
# 验收对照
python tools/benchmark_agent.py --real --tasks median_repair --runs 2 --review --timeout 300 --output .diagnostics/benchmark-review-new
# 审计某一轮，不修改会话、不调用模型
python tools/audit_agent_run.py .sessions/20260928-170042-30a3.jsonl --start-seq 5860 --output .diagnostics/audit-new.json
# 三个新教学实验（原生沙箱实验需要宿主允许创建 AppContainer）
python verify.py lab-47 lab-48 lab-49
```

## 为本项目设计的补充方法

1. 轨迹收尾判定：同时检查 session/closed 和 run/settled，防止 finish 工具返回成功但验收还在等待时误判。
2. 验收范围审计：记录每次验收之间的编辑路径与摘要变化，暴露 README 修正触发完整复核的开销；不能仅凭扩展名认定无需复核。
3. 机制故障注入：复用 test_crash_recovery、test_review_convergence、test_team_coordination、test_team_memory、test_subagent_orchestration、test_reliability_login；属于确定性回归，不计入真实模型能力成功率。
4. 裁判正负对照：有缺陷的中位数必须失败，标准库实现必须通过；不能把环境无法执行造成的失败当成成功识别缺陷。

新增教学实验 lab-47（独立随机验收）、lab-48（偶然成功与重复可靠性）、lab-49（完成声明与真实收尾）。这些实验通过不表示所有 Agent 能力都通过。
