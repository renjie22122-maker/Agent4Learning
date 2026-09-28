# 子 Agent 实测（2026-09-28）

## 真实模型委派

使用当前配置的 deepseek-flash，测试调用采用 low 推理强度。临时工作区生成 alpha.txt、beta.txt，各含随机值；主 Agent 的工具列表不含读文件和 shell，只能 spawn/get/wait/finish。两个子 Agent 使用 readonly 模式。

结果：主任务及两个子任务均 completed，两个随机值完整匹配，主任务未读文件。耗时 18.88 秒，父子累计 38,506 API tokens。按当时公开价格计算上界约 $0.005976；节假日峰谷例外可能降低金额，这不是最终扣款。

主模型实际调用了 2 次 spawn_agent、8 次 wait_agent、1 次 finish。创建两个子任务相隔约 24ms。随机值测试证明信息确实来自子任务，不证明模型的所有解释文字都准确。

## 确定性测试

现有运行内核 16 项测试及新增编排 3 项测试通过，涵盖：

- 依赖先完成，后继才开始；前置结果传入后继上下文。
- 运行中发送补充消息，在后续模型调用中收到。
- 前置失败时后继 blocked；显式重试创建新记录并保留原失败记录。
- 只读子任务无写入和派生工具；当前深度仍为 1。
- 隔离子任务在副本中写文件并实际运行 Python 验证；合并前父目录不变。
- 合并前可读 diff；父文件已修改时拒绝覆盖。
- 预算预留、排队取消、任务状态持久化（现有运行内核测试）。

## 运行中 API 请求取消：已修复并通过真实 API 复测

受控客户端进入等待后取消子任务，500ms 内客户端未收到 cancel_event。释放响应后，任务才收敛为 cancelled。排队取消通过不等于正在执行的网络请求能立即取消。

定位：AgentManager 构建模型客户端后，没有将该子任务的取消 Event 设置到客户端；客户端的流式取消监听因此没有收到信号。

后续修复：将子任务取消 Event 传给实际模型客户端，与循环、工作区共用同一信号。原复现脚本通过，19 项运行内核及编排测试再次通过。

修复后正常委派也用真实 API 重跑：6 项检查全部通过，耗时 27.66 秒，父任务 30,743 tokens，两子任务分别 10,000 和 13,581 tokens。两个随机值匹配；模型关于行尾的解释仍有不准确之处，不属于本次取消修复的验收范围。本次未重启 8800 服务，已运行的服务进程需重启后加载修复。

真实 deepseek-flash 测试：收到 11 字符流式文本，确认请求尚未返回后取消；请求抛出 InterruptedError，子任务变为 cancelled，单次 API 调用且未重试，取消到终态耗时小于 1 毫秒（报告以秒保留三位显示 0.000）。该测试为确保持续输出，关闭 JSON 输出约束并不给模型传工具；正常委派另有真实测试。初次尝试未收到文本、第二次回复过快，均未算通过，调整流式测试条件后通过。

边界：此次证明已开始返回的流式请求可取消，不代表连接建立或响应头等待阶段可即时取消，也不保证供应商停止计费。中断响应可能没有最终 usage，不能将未收到的用量当作零费用。

复现命令：

```powershell
python tools/test_runtime.py
python tools/test_subagent_orchestration.py
python tools/check_subagent_live.py          # 真实 API，计费
python tools/check_subagent_cancellation.py  # 无网络回归；修复后返回 0
python tools/check_subagent_cancellation_live.py # 真实 API 流式取消，计费
```

原始记录在 `.diagnostics/subagent-live-report.json`、`subagent-runtime-tests.txt`、`subagent-cancellation-report.json`。所有测试产物都在临时目录中生成，未修改用户工作区文件。
