# Runtime 7：独立验收预算与取消（2026-09-28）

- 设置页新增独立验收累计 token 预算，默认 0（不设单次上限），新任务生效。显式配置的子任务共享总预算仍有效；没有提高供应商上下文容量或账户额度。
- 移除独立验收写死的 100,000 tokens。有限预算下按剩余额度缩减输出预留，不能容纳最小请求时报告上限、已用及输入估计。
- 验收基础设施失败标为“验收未完成”，不得作为修改代码的依据；明确反例才报告“发现缺陷”。缺少有效证据仍不允许完成。
- retry_independent_review 可重新验收同一产物，保留旧记录；普通子任务 retry 保留 verification 身份；等待验收不消耗反射拒绝次数。
- 给独立验收提供改动文件线索，要求优先运行最小测试，避免扫描无关文件。
- 子任务取消 Event 接入真实模型客户端，能够中断已开始流式返回的请求。连接建立及响应头等待不在此次即时取消验证范围内。

## 验证

27 项单元/集成测试通过：runtime 16、orchestration 3、live_features 4、review_budget 2、recovery_edges 2；取消复现脚本通过。

真实 DeepSeek API：正确 median 实现独立验收通过；有缺陷实现独立验收完成并指出偶数中位数、空列表异常及输入被修改等反例；收到流式文本后取消，耗时 0.016 秒，单次请求、无重试。

原始报告位于 .diagnostics/independent-review-live-positive.json、independent-review-live.json 和 subagent-cancellation-live-report.json。

8800 服务已重启至 runtime-7，旧日志保留，新日志写入带时间戳的 runtime7 文件。旧会话可查看；原来失败的验收记录不会被改成通过，需要在后续任务中重新验收。
