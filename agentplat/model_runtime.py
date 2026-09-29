from __future__ import annotations
from .loop_types import LoopStep

class ModelRuntime:
    def _request_model(self, model, messages, tool_schema, res, it, it_offset, emit, tracer):
        from urllib.parse import urlsplit
        from agentlab.providers import ChatMessage
        import json
        host = urlsplit(self.cfg.base_url).hostname or ''
        general = getattr(self.ws, 'general_chat', False)
        host_context = '[宿主当前请求信息] ' + json.dumps({
            'requested_model': model, 'api_host': host,
            'conversation_mode': '普通对话（未绑定项目）' if general else '项目任务',
        }, ensure_ascii=False) + '。这是宿主实际请求配置，可直接回答型号问题，无需执行命令或探测环境变量；网关别名不能证明底层真实型号。'
        request_messages = list(messages)
        prefix = 0
        while prefix < len(request_messages) and request_messages[prefix].role == 'system':
            prefix += 1
        request_messages.insert(prefix, ChatMessage('system', host_context))
        if general:
            excluded = {'run_shell','start_process','spawn_agent','plan_team','request_host_command','run_approved_command','create_git_worktree'}
            tool_schema = [s for s in tool_schema if s.get('function', {}).get('name') not in excluded]
        with tracer.span(f"llm_turn_{it}") as sp:
            emit(LoopStep(it, "think", "正在等待模型响应",
                          f"单次模型超时 {self.cfg.timeout_s}s"))
            try:
                from .reliability import call_with_recovery
                def request_attempt():
                    res.model_calls += 1
                    self.session.append('model/request', iteration=it + it_offset - 1, model=model, api_host=host)
                def request_retry(attempt, delay, code):
                    self.session.append('model/retry', attempt=attempt, delay_s=delay, code=code)
                    emit(LoopStep(it,'guard','模型服务暂不可用，等待重试',f'HTTP {code}；{delay} 秒后重试，不重放已执行工具。'))
                text, tool_calls, usage = call_with_recovery(
                    lambda:self.llm.complete_with_tools(model,request_messages,tool_schema,self.cfg.timeout_s),
                    cancel=self.stop_flag,on_attempt=request_attempt,on_retry=request_retry)
            except InterruptedError as exc:
                res.error = '任务已中止，已完成的工作与日志已保留。'
                res.stopped_by = 'user_aborted'
                emit(LoopStep(it, 'guard', '任务已中止', res.error, ok=False))
                return None
            except Exception as exc:  # noqa: BLE001
                if self.stop_flag is not None and self.stop_flag.is_set():
                    res.error = '任务已中止，已完成的工作与日志已保留。'
                    res.stopped_by = 'user_aborted'
                    emit(LoopStep(it, 'guard', '任务已中止', res.error, ok=False))
                    return None
                res.error = f"{type(exc).__name__}: {exc}"
                res.stopped_by = "error"
                emit(LoopStep(it, "error", "模型调用失败", str(exc)[:400], ok=False))
                return None
            res.tokens_in += usage.in_tokens
            res.tokens_out += usage.out_tokens
            from .billing import record
            billing = record(self.cfg, usage, self.guard, model, self.llm)
            cost = billing['usd']
            self.session.append('billing/usage', **billing)
            callback = getattr(self, 'on_usage', None)
            if callback: callback(billing)
            res.usd += cost
            sp.set(tool_calls=len(tool_calls))
            self.session.append(
                "assistant/message",
                text=(text or "")[:2000], tool_calls=len(tool_calls),
                in_tokens=usage.in_tokens, out_tokens=usage.out_tokens,
                usd=round(cost, 8), finish_reason=getattr(
                    self.llm, "last_finish_reason", ""),
            )

        return text, tool_calls, usage, cost
