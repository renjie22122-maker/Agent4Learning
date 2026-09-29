"""TurnRuntime: extracted lifecycle responsibility with stable event semantics."""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence
from agentlab.providers import ChatMessage
from agentlab.tracing import Tracer
from .agent_tools import AgentTool, build_agent_tools, schemas
from .compaction import Compactor
from .guard import CostGuardTripped
from .session import CheckpointError, SessionLog, replay
from .spill import DEFAULT_MAX_INLINE_BYTES, SpillPolicy
from .workspace import Workspace, WorkspaceError
from .model_client import ModelClient
from .loop_types import *
from .tool_protocol import _brief, _salvage_tool_args, _clean_path

class TurnRuntime:
    def _turn(self, task: str, messages: list[ChatMessage],
              model: str | None, *, fresh: bool) -> LoopResult:
        from .memory import inject
        inject(self, task, messages)
        # Unexpected exceptions used to skip offset advancement and step/end.
        # Derive the next ID from recorded starts, including unfinished steps.
        starts = [int(e.data.get('iteration', 0)) for e in self.session.of_kind('step/start')]
        self._iter_offset = max(self._iter_offset, max(starts, default=0) + 1)
        min_step = self._iter_offset
        try:
            return self._turn_impl(task, messages, model, fresh=fresh)
        finally:
            starts = {int(e.data.get('iteration', 0)) for e in self.session.of_kind('step/start')}
            ends = {int(e.data.get('iteration', 0)) for e in self.session.of_kind('step/end')}
            self._iter_offset = max(self._iter_offset, max(starts, default=0) + 1)
            # Only close steps from this turn; never rewrite historical defects.
            for step in sorted(starts - ends):
                if step >= min_step:
                    self._end_step(step, reason='exception_or_interruption')
            from .memory import MemoryStore
            try:
                MemoryStore().refresh(self.session.path)
            except (OSError, ValueError) as exc:
                self.session.append('memory/extraction_error', error=str(exc))


    def _turn_impl(self, task: str, messages: list[ChatMessage],
                   model: str | None, *, fresh: bool) -> LoopResult:
        """一轮 = "问模型 → 执行工具 → 回灌"的循环，直到 finish 或触限。"""
        model = model or self.cfg.model_or("mid") or self.cfg.model
        if self._explicit_context_window is None:
            from .model_capacity import discover
            discover(self.cfg)
            self.compactor.context_window = self.cfg.resolved_context_window(model)
        from .billing import refresh as refresh_prices
        refresh_prices(self.cfg)
        res = LoopResult(ok=False)
        t0 = time.perf_counter()
        tracer = Tracer("agent")

        def emit(step: LoopStep) -> None:
            res.steps.append(step)
            if self.on_step:
                try:
                    self.on_step(step)
                except Exception:  # noqa: BLE001 - 回调失败不能影响 agent
                    pass

        # 让压缩不变量能读到当前的 messages（记账 vs 实际、消息头是否被摘要顶掉）。
        self._last_messages = messages
        # 反射要用任务原文抽需求条目。从 messages 里反推不行 ——
        # 压缩可能已经把那条 user 消息折成摘要了。
        if fresh:
            self._task_text = task
        else:
            self._task_text += "\n" + task
            # A follow-up is a new delivery scope. Historical edits are still
            # in the log, but must not force a fresh review for a read-only reply.
            from .runtime import workspace_digest
            self._initial_digest = workspace_digest(self.ws.scope)
            self._files_touched = []
            from .independent_review import retire
            retire(self, '开始新的对话任务')
        self._acceptance_task = task
        self.task_memory.requests.append(task)
        self._finish_rejects = 0
        emit(LoopStep(0, "think",
                      "收到任务，开始规划" if fresh else "收到追问，接着做",
                      task[:500]))
        if fresh:
            self.session.append("session/created",
                                session_id=self.session.session_id,
                                task=task, model=model,
                                workspace=str(self.ws.root), workspace_roots={k:str(v) for k,v in self.ws.roots.items()}, workspace_group=getattr(self.ws,'group_id',''))
        # 续轮**不写** session/created（日志锚点必须唯一）——
        # 锚点重复会让 `find_latest_session()` / 重放都失去依据。
        #
        # 也**不额外插一条 step/start 当"轮次分隔标记"**。
        # 试过了，是错的：那条标记永远不会有配对的 step/end，于是
        # "轮次记账闭合"不变量正确地把每一次追问都报成"记账丢了一段"。
        # 想给日志加结构就必须同时给它闭合；加不了闭合就别加结构 ——
        # 续轮这件事 `followup/user` 事件已经说清楚了。
        it_offset = self._iter_offset

        from .attachments import install as refresh_attachments
        refresh_attachments(self)
        tool_schema = schemas(self.tools)
        for existing in self.session.of_kind('tool/call'):
            self._used_call_ids.add(existing.data.get('call_id', ''))
        budget_exhausted = ""
        #: 是否已经成功跑过验证命令（pytest / 运行脚本）。用它作为
        #: "可以用文字收尾"的证据，见下面终止判定处的说明。
        self._verified = False
        #: 循环不设固定轮次上限 —— 用硬上限兜底防止无限循环（无人决策时的保险）。
        #: 真正的终止由 self.policy 决定，见文件头"机制与策略分离"。
        iteration = 0
        last_logged_it = 0
        closed_it = 0        # 已经写过 step/end 的最后一轮（本地 `it` 口径）
        exceeded_tool_budget = False
        over_budget_steps = 0
        from .reliability import FailureCircuit
        failure_circuit = FailureCircuit()
        repeated_failure = False
        last_failure_key, failure_repeats = None, 0
        last_text, text_repeats = None, 0
        budget_notices = set()
        while not self.hard_iterations or iteration < self.hard_iterations:
            from .independent_review import wait_pending, partition_steering
            queued_steering = wait_pending(self, lambda: emit(LoopStep(iteration, 'guard', '等待独立验收',
                self._review_status_text())))
            queued_steering += getattr(self, '_review_steering', [])
            self._review_steering = []
            for message in queued_steering:
                messages.append(ChatMessage('user', message))
                self._task_text += '\n' + message
                self._acceptance_task += '\n' + message
                self.session.append('followup/user', text=message)
            notice = getattr(self, '_review_wait_notice', '')
            if notice:
                messages.append(ChatMessage('user', notice))
                self._review_wait_notice = ''
            from .access_modes import apply_pending
            apply_pending(self)
            from .plugins import refresh as refresh_plugins
            refresh_plugins(self)
            iteration += 1
            it = iteration
            if self.steering:
                for message in partition_steering(self, self.steering(),
                        lambda: emit(LoopStep(it, 'guard', '独立验收状态', self._review_status_text()))):
                    messages.append(ChatMessage('user', message))
                    self._task_text += '\n' + message
                    self._acceptance_task += '\n' + message
                    self.session.append('followup/user', text=message)
            if self.children and hasattr(self.children, 'deliver'):
                for message in self.children.deliver('root'):
                    messages.append(ChatMessage('user', message))
                    self.session.append('team/message', text=message)
            refresh_attachments(self)
            tool_schema = schemas(self.tools)
            res.iterations = it            # 外部中止：在**步骤边界**检查，保证不会把一次工具调用劈成两半。
            if self.stop_flag is not None and self.stop_flag.is_set():
                if self.children:
                    self.children.close()
                res.stopped_by = "user_aborted"
                res.error = "被用户中止"
                emit(LoopStep(it, "guard", "收到中止请求，在步骤边界停下",
                              "已完成的进度保存在会话日志里，可以续跑。", ok=False))
                break
            if self.max_wall_s is not None and \
                    time.perf_counter() - t0 > self.max_wall_s:
                budget_exhausted = "wall_clock"
                break
            if self.guard is not None and self.guard.tripped():
                budget_exhausted = "cost"
                break

            # ---- 0) 问策略：还允许继续吗？（对应 DSH 的 agent/turn-stopping）----
            verdict = self.policy(LoopContext(
                iteration=it - 1, tool_calls=res.tool_calls,
                elapsed_s=time.perf_counter() - t0, usd=res.usd,
                tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                verified=self._verified,
            ))
            if verdict is not None:
                res.stopped_by = "policy"
                res.error = verdict.reason
                emit(LoopStep(it, "guard", f"终止策略生效：{verdict.reason}", ok=False))
                break

            # ---- 1) 问模型下一步做什么 ----
            # Tell the model about caller-selected bounds before they terminate it.
            # Unlimited tasks acquire no new bound here.
            remaining = self.hard_iterations - it + 1 if self.hard_iterations else None
            deadline = getattr(self, 'run_deadline', None)
            wall_left = max(0, deadline-time.monotonic()) if deadline else None
            notice_key = ('start' if it == 1 else
                          'last_steps' if remaining is not None and remaining <= 3 else
                          'closing_steps' if remaining is not None and remaining <= max(4,self.hard_iterations//4) else
                          'closing_time' if wall_left is not None and wall_left <= 90 else None)
            if notice_key and notice_key not in budget_notices and (remaining is not None or wall_left is not None):
                budget_notices.add(notice_key)
                limits = {'remaining_model_calls_including_this':remaining,
                          'remaining_run_seconds':round(wall_left,1) if wall_left is not None else None}
                self.session.append('budget/status', **limits)
                messages.append(ChatMessage('user', '宿主告知调用方显式设置的剩余额度：'+json.dumps(limits,ensure_ascii=False)+
                    '。优先完成核心需求、运行必要验证并 finish；停止新增可选功能或重复测试。独立验收等待不额外消耗主模型步骤。额度不足时如实交代未完成项，不能冒称通过。'))
            #
            # ★ 检查点屏障 ①：模型请求前 flush。
            # 否则崩溃后重放会重发一个日志里不存在的请求 —— 计费与实际调用对不上，
            # 而且恢复出来的 token/成本统计是错的。
            try:
                self.session.flush("before_model_request")
            except CheckpointError as exc:
                res.error = str(exc)
                res.stopped_by = "checkpoint_failed"
                emit(LoopStep(it, "error", "持久化失败，已阻止模型请求", str(exc)[:300],
                              ok=False))
                break
            self.session.append("step/start", iteration=it + it_offset - 1)
            last_logged_it = it

            # ---- 0.5) 上下文压缩：压力到阈值就把老历史压掉 ----
            # 放在"模型请求前"而不是"工具结果回灌后"：这样每轮的 prompt 都是有界的，
            # 而不是等撑爆了再补救。
            self._compact_for_turn(messages, res, it, emit)
            if self.guard is not None:
                from agentlab.tokens import count_messages, count_tokens
                estimate = count_messages(messages) + count_tokens(json.dumps(tool_schema))
                try:
                    self.guard.preflight(estimate, tag='coding-agent',
                                         max_tokens=self.compactor.context_window)
                except CostGuardTripped as exc:
                    budget_exhausted = 'cost'
                    res.error = str(exc)
                    break
            self._save_conversation(messages)
            if getattr(self, 'permission_mode', '') == 'readonly':
                from .runtime import workspace_digest
                self.session.append('recovery/checkpoint', digest=workspace_digest(self.ws.roots))
            response=self._request_model(model,messages,tool_schema,res,it,it_offset,emit,tracer)
            if response is None:break
            text,tool_calls,usage,cost=response

            # 将重复/缺失的 provider ID 转成唯一错误调用，不能复用旧副作用。
            from .tool_protocol import normalize_calls
            tool_calls=normalize_calls(tool_calls,self._used_call_ids)
            messages.append(ChatMessage("assistant", text or "",
                                        tool_calls=tool_calls))
            # **主动告知截断**：finish_reason=length 时，工具参数大概率是残的
            # （写文件的参数往往含整个文件内容，最容易撞上限）。
            # 与其让模型从"JSON 解析失败"里猜，不如直接告诉它发生了什么、
            # 以及正确做法。实测能省掉 2~3 轮无效重试 —— 那几轮都是白花的钱。
            if getattr(self.llm, "last_finish_reason", "") == "length":
                emit(LoopStep(
                    it, "guard", "⚠ 输出被 max_tokens 截断（finish_reason=length）",
                    "内容太长导致工具参数不完整。请改用 append_file 分块写入，"
                    "或先 write_file 写骨架、再用 edit_file 逐步补充。",
                    ok=False,
                ))
            if text and text.strip():
                emit(LoopStep(it, "think", "模型的判断", text.strip()[:800],
                              tokens_in=usage.in_tokens, tokens_out=usage.out_tokens,
                              usd=cost))

            # 模型没要求调工具 —— 它想用自然语言结束。
            #
            # 终止条件必须是硬编码的，否则模型一句"我做好了"就能骗过系统。
            # 但**也不能死板到只认 finish**：实测写文件很吃轮次，模型经常在
            # "刚跑完 pytest 全绿"之后用一句话收尾，此时若坚持要它再调 finish，
            # 就会因为轮次耗尽而被判为"未完成"—— 活儿明明干完了。
            # 折中：允许在**已经成功执行过验证命令**之后用文字收尾。
            # 这不是放松要求，而是把判定依据从"模型说了什么"换成"证据是什么"。
            if not tool_calls:
                if self._verified and (text or "").strip() and self._review_finish({"summary": text}).allow:
                    res.ok = True
                    res.summary = (text or "").strip()
                    res.stopped_by = "finish_text_after_verification"
                    emit(LoopStep(it, "finish",
                                  "验证通过后用文字收尾（视为完成）",
                                  res.summary[:500]))
                    res.elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    return res
                text_repeats = text_repeats + 1 if text == last_text else 1
                last_text = text
                if text_repeats >= 3:
                    res.stopped_by = 'repeated_empty_action'
                    res.error = '连续三次返回相同文字且没有工具动作，请检查模型工具调用能力或补充指令'
                    emit(LoopStep(it, 'guard', res.error, ok=False))
                    break
                if self.hard_iterations and it >= self.hard_iterations:
                    break
                messages.append(ChatMessage(
                    "user",
                    "请继续：要么调用工具推进任务，要么调用 finish 声明完成。"
                    "只用文字回复不会被当作完成（除非你已经成功跑过验证命令）。",
                ))
                emit(LoopStep(it, "guard", "模型只想用文字结束 → 已要求它调用 finish",
                              ok=False))
                self._end_step(it + it_offset - 1, reason="text_only")
                continue

            last_text, text_repeats = None, 0
            # ---- 2) 执行工具 ----
            # 单 step 内限量：策略只在轮次边界生效，拦不住"一轮塞 50 个调用"。
            #
            # ⚠ 截断**不能简单从尾部砍**。实测的失效场景：模型在同一轮里
            # 既调了若干工具、又调了 `finish` —— 从尾部砍正好把 `finish`
            # 砍掉，于是"任务已经完成"这件事被丢弃，下一轮又要重来一遍。
            # 这批调用明明都算出来了，却因为截断顺序白干。
            #
            # 所以按"丢了最可惜"排序保留：**终止类 > 只读类 > 有副作用类**。
            # 有副作用类（写文件/跑命令）最后保留 —— 它们的代价最高，
            # 而且丢弃它们最安全（下一轮再做一次不会更糟）。
            tool_calls,exceeded_tool_budget,over_budget_steps=self._prepare_tool_batch(tool_calls,messages,it,emit,over_budget_steps)
            for call in tool_calls:
                fn = (call.get("function") or {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                call_id = call.get("id") or f"call_{it}_{name}"
                if call_id in self._used_call_ids:
                    messages.append(ChatMessage('tool', '拒绝重复调用 ID；请使用新 ID', tool_call_id=call_id))
                    continue
                self._used_call_ids.add(call_id)
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    from .schema import validate_schema
                    if name in self.tools:
                        validate_schema(args, self.tools[name].parameters)
                    elif not isinstance(args, dict):
                        raise ValueError('参数必须是对象')
                except Exception as exc:
                    messages.append(ChatMessage('tool', f'[参数错误] {exc}；请重发完整合法 JSON，未执行任何部分写入。', tool_call_id=call_id))
                    emit(LoopStep(it, 'error', f'{name} 参数解析或校验失败', str(exc), tool=name, ok=False))
                    continue

                res.tool_calls += 1

                tool = self.tools.get(name)
                ts = time.perf_counter()
                known = tool is not None
                call_ok = True
                if not known:
                    out = (f"没有名为 {name} 的工具。可用工具："
                           f"{', '.join(self.tools)}")
                    ok = False
                else:
                    # ★ 检查点屏障 ②：**有副作用的工具执行前**先落盘意图。
                    #
                    # 为什么这条最重要：写文件、跑命令是不可逆的。如果先执行、
                    # 崩溃在"执行完成"与"日志写入"之间，恢复时就无法判断
                    # 这个副作用到底发生过没有 —— 于是要么漏做、要么重做
                    # （重做可能就是重复下单、重复发通知）。
                    #
                    # 所以顺序是：**先记意图 → flush → 再执行**。
                    # 日志里有 `tool/call` 但没有对应的 `tool/result`，
                    # 就说明"崩溃在这一步中间"，恢复时应按不确定处理。
                    #
                    # 注意 `tool/call` 对**所有**已知工具都记，不只 destructive：
                    # 原来只有 destructive 才记 call，于是日志里"有 result 没
                    # call"是常态 —— 调用/结果配对的不变量根本立不住，
                    # 而且从日志看不出某个只读工具到底跑没跑。
                    # 记上 `call_id`，配对才能精确到"哪一次调用"。
                    out,ok,call_ok=self._execute_tool(name,args,tool,call_id,it,emit)
                failure_key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False), out) if not ok else None
                for extra_usage in getattr(self, '_aux_usage', []):
                    res.usd += extra_usage['usd']
                    res.tokens_in += extra_usage['in_tokens']
                    res.tokens_out += extra_usage['out_tokens']
                self._aux_usage = []
                failure_repeats = failure_repeats + 1 if failure_key and failure_key == last_failure_key else (1 if failure_key else 0)
                last_failure_key = failure_key
                from .runtime import workspace_digest
                repeated_failure = repeated_failure or failure_circuit.observe(
                    name,args,out,ok,workspace_digest(self.ws.scope) if not ok else '')
                ms = (time.perf_counter() - ts) * 1000.0
                if known and call_ok:
                    self.session.append("tool/result", tool=name, ok=ok,
                                        ms=round(ms, 1), call_id=call_id,
                                        out=(out or "")[:400])

                # ⚠ 这里**不要**再 append 一次 tool 消息。
                # 曾经有过两条一模一样的 `messages.append(ChatMessage("tool", ...))`
                # —— 一条在这里、一条在 spill 处理之后。于是每条工具结果
                # 都被回灌两遍，同一个 tool_call_id 在数组里出现两次。
                # 后果有两层：
                #   ① provider 直接 400（Duplicate value for 'tool_call_id'）
                #      —— 这正是实测撞到的那个错；
                #   ② 更隐蔽的一层：本地看不出任何异常（会话日志配对正确、
                #      不变量也不报），只是消息数组悄悄变长一倍、成本翻倍。
                # 回灌只做一次，放在 spill 之后（见下面），因为要塞进上下文的
                # 是**剪过之后**的预览而不是原文。

                out=self._process_tool_output(name,args,out,ok,it,emit)
                messages.append(ChatMessage("tool", out, tool_call_id=call_id))
                self._save_conversation(messages)
                emit(LoopStep(
                    it, "tool" if ok else "error",
                    f"{name}({_brief(args)})", detail=out[:800],
                    tool=name, args=args, result=out, ok=ok, ms=ms,
                ))

                # ---- 3) 终止判定：模型显式声明完成 ----
                retry_ready = name == 'retry_independent_review' and getattr(self, '_review_retry_ready', False)
                if retry_ready:
                    args = {'summary':'本次任务已完成，独立验收通过。\n'+self._acceptance_task}
                    self._review_retry_ready = False
                if tool is not None and (tool.terminal or retry_ready) and ok:
                    # ★ 反射闸门：finish **不是**无条件接受的。
                    #
                    # 这一处是补上真实缺口的：原来只要模型调 finish 就
                    # `res.ok = True`，schema 里除了 summary 没有任何要求 ——
                    # 于是"写完代码一次都没跑就说已完成"也能通过。
                    # 上面那道 `_verified` 闸只管"没调工具、想用文字收尾"那条路，
                    # 模型直接调 finish 就绕过去了。
                    verdict = self._review_finish(args)
                    if verdict.by == '独立验收等待':
                        from .independent_review import wait_pending
                        incoming = wait_pending(self, lambda: emit(LoopStep(it, 'guard', '等待独立验收',
                            self._review_status_text())))
                        if self.steering:
                            incoming += partition_steering(self, self.steering(),
                                lambda: emit(LoopStep(it, 'guard', '独立验收状态', self._review_status_text())))
                        if self.stop_flag is not None and self.stop_flag.is_set():
                            res.stopped_by = 'user_aborted'
                            res.error = '被用户中止'
                            break
                        if incoming:
                            for message in incoming:
                                messages.append(ChatMessage('user', message))
                                self._task_text += '\n' + message
                                self._acceptance_task += '\n' + message
                                self.session.append('followup/user', text=message)
                            from .independent_review import retire
                            retire(self, '用户补充了任务要求')
                            from .reflection import ReflectionVerdict
                            verdict = ReflectionVerdict(False, '已收到新要求；旧验收已归档并请求取消。请处理新要求，完成后宿主会按新产物验收。', '任务要求更新')
                        else:
                            verdict = self._review_finish(args)
                        self._review_wait_notice = ''
                    if not verdict.allow:
                        if verdict.by not in ('独立验收等待', '任务要求更新'):
                            self._finish_rejects += 1
                        emit(LoopStep(
                            it, "guard",
                            f"⛔ 完成声明被拒（{verdict.by}）",
                            verdict.instruction[:600], ok=False,
                        ))
                        self.session.append(
                            "reflection/rejected", by=verdict.by,
                            instruction=verdict.instruction[:1500],
                            rejects=self._finish_rejects,
                        )
                        # 把拒绝理由作为 user 消息回灌 —— 这是"反射"的落点：
                        # 模型必须在**同一轮对话里**处理这条反馈，
                        # 而不是被外部悄悄判为失败。
                        messages.append(ChatMessage("user", verdict.instruction))
                        if verdict.exhausted:
                            res.stopped_by = 'verification_blocked' if verdict.by == '独立验收受阻' else 'unverified'
                            res.error = verdict.instruction
                            break
                        continue

                    res.ok = True
                    res.summary = args.get("summary", "") or out
                    res.stopped_by = "finish"
                    # 反射结论必须**如实写下来**，包括"策略存在但都放行了"。
                    # 只在被拒时记录的话，"检查通过"和"没有检查"看起来一样 ——
                    # 这正是本项目反复踩的同一个坑。
                    if self.reflector is not None:
                        res.reflection = (verdict.by or
                                          "全部反射策略放行：" +
                                          "、".join(self.reflector.names()))
                    emit(LoopStep(it, "finish", "任务声明完成", res.summary[:500]))
                    self.session.append(
                        "reflection/accepted", by=verdict.by or "无策略介入",
                        rejects=self._finish_rejects,
                    )
                    res.elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    # 收尾也落盘：这样即使进程随后被杀，"已完成"这个事实也在日志里，
                    # 下次不会被误判成"跑到一半"而重跑一遍。
                    try:
                        # 收尾前的屏障：让"已完成"这个事实在日志里是**有屏障保护的**，
                        # 而不只是普通追加 —— 恢复逻辑用它判断"要不要接着跑"。
                        self.session.flush("before_session_close")
                        # ★ 收尾也必须写 `step/end`。
                        # 原来这里直接 append session/closed 就 return 了，
                        # 于是"最后一个 step/start 没有对应的 step/end"
                        # 同时出现在两种完全不同的情形里：
                        #   ① 正常完成（没问题）；
                        #   ② 记账真的丢了一段（循环漏写了）。
                        # 日志里既然分不出来，不变量就没法只报警② ——
                        # 只能放宽成"允许尾部悬空"，于是②永远抓不到。
                        # 记上这一条，两者就分开了：正常完成一定闭合。
                        self.session.append("step/end", iteration=it + it_offset - 1,
                                            tool_calls=res.tool_calls,
                                            finished=True)
                        self.session.append("session/closed", finished=True,
                                            iterations=res.iterations,
                                            tool_calls=res.tool_calls,
                                            usd=round(res.usd, 6),
                                            summary=res.summary[:1000])
                        self.session.flush("session_finished")
                    except CheckpointError:
                        pass  # 已完成是既成事实，落盘失败不该把结果降级为失败
                    # ⚠ 交接必须在 return 之前做。
                    # 只在函数末尾写 `self._iter_offset = ...` 是不够的：
                    # finish 路径**直接从循环里 return**，根本走不到函数末尾 ——
                    # 于是下一轮的偏移还是 0，日志里就出现"轮次回退"。
                    # 实测就是这么炸的（不变量报「轮次回退：0 出现在 2 之后」）。
                    self._iter_offset = it_offset + res.iterations
                    res.reflection_rejects = self._finish_rejects
                    self._save_conversation(messages)
                    return res
            else:
                # ⚠ 这个 `else` 属于**内层 `for call in tool_calls`**。
                # 不要在这里 `break` —— 那只跳出内层。超限的处理见下面
                # 紧跟 while 体的 `if exceeded_tool_budget: break`。
                pass
            # 本轮正常走完：闭合它。
            #
            # ⚠ 这一步以前是**缺的**：循环只在整体结束时写一条 step/end，
            # 于是 18 轮的会话日志里只有 1 条 step/end。后果有两个：
            #   ① 无法回答"第 3 轮花了多久"—— 每轮的结束时刻根本没记；
            #   ② "每个 step/start 都有配对的 step/end"这条不变量立不住，
            #      只能放宽成"允许 17 个中间悬空"，于是真正的记账丢失
            #      反而查不出来（放宽后的检查等于没检查）。
            self._save_conversation(messages)
            closed_it = it if self._end_step(it + it_offset - 1) else closed_it
            if failure_repeats >= 6 or repeated_failure:
                res.stopped_by = 'repeated_tool_failure'
                res.error = '同一工具和参数在文件未变化时重复失败六次（包括交替失败）；已停止重复尝试，请处理具体阻塞后继续'
                emit(LoopStep(it, 'guard', res.error, ok=False))
                break
            if res.stopped_by in ('unverified','verification_blocked'):
                break

            # ★ 单轮工具调用数**连续**超限 → 才跳出外层循环。
            #
            # 为什么不是"超一次就停"：截断本身不致命，模型下一轮少调几个就行。
            # 实测模型经常第一轮一口气调 15 个（批量读文件），被截断之后
            # 自己就改成分批了 —— 这时候终止整个任务是纯粹的自伤。
            # 只有**连续 N 轮**都改不过来，才说明它卡在这个习惯上。
            #
            # 这一句必须在外层（while 体），不能放进 `for call in tool_calls`
            # 里 —— 放进去 `break` 只跳出内层，外层继续跑。
            if exceeded_tool_budget:
                over_budget_steps += 1
                if over_budget_steps >= 3:
                    res.stopped_by = 'tool_budget'
                    res.error = '连续三次模型响应请求的工具数超过单步上限'
                    emit(LoopStep(
                        it, "guard",
                        f"连续 {over_budget_steps} 轮工具调用超限，停下",
                        "模型改不掉一次调太多工具的习惯，继续大概率是无效循环。",
                        ok=False,
                    ))
                    break

        # ---- 循环结束（非 finish 路径）----
        res.elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # ★ 检查点屏障 ③：进入下一步前 / 循环非正常结束时落盘。
        # 这样"跑到第 N 轮被打断"这个事实是可恢复的 —— 下次能接着跑。
        #
        # 注意这里**不再补写 step/end**：每一轮的 step/end 已经在轮内闭合了
        # （见 _end_step）。在这里再写一条会造出重复的 end。
        # 只在"最后一轮被中断、它的 step/end 还没写"时兜底。
        try:
            if last_logged_it and last_logged_it != closed_it:
                self._end_step(last_logged_it + it_offset - 1,
                               reason=res.stopped_by or "interrupted")
            self.session.flush("loop_exit")
        except CheckpointError:
            pass
        if budget_exhausted:
            res.stopped_by = budget_exhausted
            reason = {
                "wall_clock": f"超出 {self.max_wall_s:.0f}s 墙钟预算",
                "cost": "触达成本护栏",
                }.get(budget_exhausted, budget_exhausted)
            emit(LoopStep(res.iterations, "guard", f"循环被硬性上限终止：{reason}",
                          ok=False))
        elif not res.stopped_by:
            res.stopped_by = "hard_limit"
            emit(LoopStep(res.iterations, "guard",
                          f"达到配置的模型调用上限 {self.hard_iterations}（不是死循环判定）", ok=False))
        # 记住这一轮用了多少轮次，下一轮接着往上编号（见 _turn 开头的说明）。
        # finish 路径在循环里已经自己交接过了（并且 return 了），走不到这里。
        self._iter_offset = it_offset + res.iterations
        res.reflection_rejects = self._finish_rejects
        if self.reflector is not None and not res.reflection:
            # 非 finish 收尾（被中止/触限）：如实记下"反射没有做判断"，
            # 而不是留空让人以为"检查过了、没问题"。
            res.reflection = "未做判断（循环非正常结束）"
        return res


    def _end_step(self, iteration: int, reason: str = "ok") -> bool:
        """闭合一轮的记账：写 `step/end`。返回是否写成功。

        每一轮**必须恰好写一次**。漏写会在日志里留下"中间悬空的 step/start"，
        和崩溃现场长得一样 —— 于是不变量只能放宽到"允许悬空"，
        放宽之后真正的记账丢失就再也查不出来了。
        """
        try:
            self.session.append("step/end", iteration=iteration, reason=reason)
            return True
        except CheckpointError:
            return False  # 记账失败不该把任务本身降级为失败
