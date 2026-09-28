# Runtime 8：递归团队与长期记忆

已部署到本机 8800，运行时接口确认 runtime-8、委派深度 2、团队消息与选择性记忆启用。按用户授权中止原运行任务并重启，保留历史会话与全部日志；未自动选择任何真实用户会话作为记忆来源。

## 如何使用

- 设置页配置委派深度（主 Agent 是 0，默认 2 允许孙 Agent）、模型请求并发（默认 3）、存活任务数（默认 24）、整棵子任务树总预算、普通子任务默认预算。预算 0 表示不设该项上限，仍受账户、模型容量及其他显式限制约束。配置对新任务生效。
- 普通子任务不再固定默认 12,000 / 参数最大 100,000。整棵任务树共享请求预算预留，父任务等待时不占模型并发槽。主 Agent 自身用量不在“子任务树预算”内；页面仍单独统计父子费用。
- 主 Agent 和子 Agent 可 list_agents、send_agent_message、team_state。共享状态使用 expected_revision 防止丢失更新；消息是参考资料，不授予权限。
- 子 Agent 在深度内继续 spawn_agent；只能等待、取消、合并自己的直接子任务。只读任务不能派生可写任务；隔离孙任务从父副本创建，先合并回父副本，再由主 Agent 审查。取消父任务会取消后代。
- `/team?session=...` 展示团队树、状态、预算与共享内容。独立验收者不参加团队消息，也不读取长期记忆，避免作者结论污染验收上下文。

## 长期记忆

打开侧栏“长期记忆”（`/memories`）：

1. 勾选允许成为记忆来源的历史会话，默认项目范围；运行中的会话不能导入。
2. 本地规则提取候选：用户原话作为偏好/决策候选；成功任务中的退出码 0 命令作为狭义历史经验。不会把助手的“已经成功”当作事实，不调用付费提取模型。
3. 编辑候选，将一次性任务整理为可复用内容；选择项目/本机用户范围、过期时间，改为“启用并允许召回”。默认候选不会自动启用。
4. 新任务和后续用户轮次按词项相关性召回；偏好可作为范围内默认参考。memory/recalled 事件记录条目 ID。当前用户要求优先。
5. 可停用、修改、删除或撤销整个来源。删除保留不含正文的墓碑，避免增量提取重新创建它；原会话日志不会被删除。已发送给模型的上下文不能远程撤回。

选择过的来源在后续轮次结束时增量提取候选；未选择会话不提取。全局召回开关在设置页。SQLite 存储默认位于 `.agent-runtime/memory`，测试可用 AGENTLAB_MEMORY_DIR 隔离。

边界：这是本地规则提取、人工确认、词项检索，不是模型自主学习或语义向量记忆。条目过期/撤销、项目范围与编辑版本冲突由代码执行；语义矛盾仍需用户审查。历史测试证据只说明当时命令成功，不保证当前产物正确。常见凭据会遮盖，但不能承诺自动识别所有秘密。

## 验证与实验

真实 deepseek-flash API 验证：

- 单模型并发槽下，父子两级委派完成，孙 Agent 读取随机文件，父 Agent 未自行读文件。
- 两个兄弟子 Agent 直接 send_agent_message，接收者准确返回随机代号，记录了消息送达。
- 新会话只给 finish 工具，仍准确返回已确认的随机偏好，证明来自自动记忆召回。
- 取消已开始返回的子 Agent 流式响应，约 16ms 收敛为 cancelled，无重试。

原始报告：`.diagnostics/team-memory-live.json`、`team-messages-live.json`、`subagent-cancellation-live-report.json`。

受控测试覆盖：递归调度、深度边界、权限继承、嵌套副本合并、递归取消、共享预算、来源选择、确认与跨项目隔离、过期/删除、HTTP CSRF。对话测试固定使用临时目录本机执行，避免混入 AppContainer 宿主权限问题；原生沙箱另有测试。

```powershell
python tools/test_team_memory.py
python tools/test_memory_http.py
python tools/check_team_memory_live.py       # 真实 API，计费
python tools/check_team_messages_live.py     # 真实 API，计费
python verify.py lab-40 lab-41
```

新增 lab-40 展示未确认记忆被误用及范围/删除控制；lab-41 展示父任务占满线程导致孙任务饥饿，并以实际 CodingAgent 编排验证修复。

重启后中途任务标记 interrupted，不自动重放未知副作用。缺失 usage 的失败请求按预留估计计入共享预算，和页面 API 实测用量分开；已落盘的在途预留在恢复时保守扣除。重启后的嵌套副本不自动恢复合并，需要检查并重新创建任务。

## 参考

Codex 官方文档描述了子任务编排与权限继承，以及按会话控制是否使用/贡献记忆。这些是参考方向，不表示本项目实现与其内部机制等同：

- https://learn.chatgpt.com/docs/agent-configuration/subagents
- https://learn.chatgpt.com/docs/customization/memories?surface=app
