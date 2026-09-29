"""Tool execution boundary: intent persistence, live authority, invocation."""
from .loop_types import LoopStep
from agentlab.providers import ChatMessage
from .tool_protocol import _brief
from .session import CheckpointError
from .workspace import WorkspaceError

class ToolRuntime:
    def _execute_tool(self, name, args, tool, call_id, it, emit):
        call_ok=True;ok=False;out='[被阻止] 持久化失败，未执行工具'
        try:
            self.session.append(
                "tool/call", tool=name, destructive=tool.destructive,
                ok=True, call_id=call_id,
                brief=_brief(args, 120), path=args.get("path", ""),
                command=args.get("command", ""),
            )
            if tool.destructive:
                self.session.flush(f"before_side_effect:{name}")
        except CheckpointError as exc:
            call_ok = False
            if tool.destructive:
                out = f"[被阻止] 持久化失败，未执行该副作用：{exc}"
                ok = False
                emit(LoopStep(it, "error",
                              f"{name} 因检查点失败被阻止",
                              str(exc)[:300], tool=name, ok=False))
        if not call_ok:
            # 检查点写不进去 → 这个副作用**故意不执行**。
            # 但 `tool/call` 已经落盘了，所以必须补一条
            # `tool/result` —— 否则日志里留下一个"永远悬空"的
            # 调用，配对不变量会把一次**受控拒绝**误报成崩溃，
            # 而"悬空调用"这个信号本身是留给真崩溃用的。
            self.session.append(
                "tool/result", tool=name, ok=False, ms=0.0,
                call_id=call_id, out=str(out or "")[:400],
            )
        else:
            emit(LoopStep(it, "think", f"正在执行 {name}",
                          _brief(args, 120)))
            try:
                from .runtime import invoke_checked, effective_policy
                out = invoke_checked(name, args, tool.parameters, tool.fn,
                                     effective_policy(self), writes=tool.destructive,
                                     shell=name in ('run_shell', 'start_process'),
                                     network=getattr(tool, 'network', False))
                ok = True
                if name == 'run_shell':
                    state = self.ws.last_execution or {}
                    ok = state.get('status') == 'exited' and state.get('exit_code') == 0
            except WorkspaceError as exc:
                # 越界/违规是**预期内的拒绝**，要把原因讲清楚让模型改做法，
                # 而不是让它以为是系统故障然后重试同样的调用。
                out = f"[被拒绝] {exc}"
                ok = False
            except TypeError as exc:
                out = f"[参数错误] {exc}。请检查参数名与类型。"
                ok = False
            except Exception as exc:  # noqa: BLE001
                out = f"[执行失败] {type(exc).__name__}: {exc}"
                ok = False
        return out,ok,call_ok

    def _prepare_tool_batch(self, tool_calls, messages, it, emit, over_budget_steps):
        if len(tool_calls) > self.MAX_TOOLS_PER_STEP:
            dropped = len(tool_calls) - self.MAX_TOOLS_PER_STEP

            def _rank(c: dict) -> int:
                nm = ((c.get("function") or {}).get("name") or "")
                t = self.tools.get(nm)
                if t is None:
                    return 0          # 未知工具（多半是幻觉）最先丢
                if t.terminal:
                    return 3          # finish 必留
                return 2 if not t.destructive else 1

            keep = sorted(range(len(tool_calls)),
                          key=lambda i: (-_rank(tool_calls[i]), i)
                          )[: self.MAX_TOOLS_PER_STEP]
            kept = [tool_calls[i] for i in sorted(keep)]
            terminal_kept = any(
                (self.tools.get(((c.get("function") or {}).get("name") or ""))
                 is not None)
                and self.tools[((c.get("function") or {}).get("name") or "")
                               ].terminal for c in kept)
            emit(LoopStep(
                it, "guard",
                f"本轮工具调用 {len(tool_calls)} 个超过单步上限 "
                f"{self.MAX_TOOLS_PER_STEP}，保留 {len(kept)} 个、丢弃 {dropped} 个",
                "保留优先级：finish > 只读 > 有副作用。"
                "请把剩余工作拆到后续轮次，不要在一轮里塞太多调用。"
                + ("（本轮仍执行了 finish）" if terminal_kept else ""),
                ok=False,
            ))
            kept_ids = {c['id'] for c in kept}
            for skipped in tool_calls:
                if skipped['id'] not in kept_ids:
                    messages.append(ChatMessage('tool', '[未执行] 单步工具上限，请分批重发。',
                                                tool_call_id=skipped['id']))
            # 被截掉的工作尚未执行，本轮 finish 不能宣称全部完成。
            for c in kept:
                if c['function'].get('name') == 'finish':
                    c['function'] = {'name': '__deferred_finish', 'arguments': '{}'}
            tool_calls = kept
            # ★ 用标志位而不是在内层 `break`：内层 `break` 只跳出
            # `for call in tool_calls`，外层 while 会继续跑 ——
            # 表现是"任务第一次工具调用之后就一头撞进硬上限（80 轮）"。
            # 实测踩到过，比"看起来只跑了一步"更隐蔽。
            exceeded_tool_budget = True
        else:
            # 这一轮在限内 → 说明模型**能**按上限工作，重置计数。
            # 连续超限才判定为"它改不了这个习惯"，那时才停。
            over_budget_steps = 0
            exceeded_tool_budget = False

        terminals = [c for c in tool_calls if c['function'].get('name') == 'finish']
        for extra in terminals[1:]:
            extra['function'] = {'name': '__duplicate_finish', 'arguments': '{}'}
        tool_calls = sorted(tool_calls, key=lambda c: c['function'].get('name') == 'finish')
        return tool_calls,exceeded_tool_budget,over_budget_steps

    def _process_tool_output(self, name, args, out, ok, it, emit):
        # ---- spill：超大结果只留预览，全文落盘可回取 ----
        # 放在回灌之前是必须的：一旦进了 messages，它就每轮都被重发，
        # 成本随轮数平方增长。
        before_bytes = len(out.encode("utf-8"))
        spilled_before = len(self.spill.spilled)
        out = self.spill.apply(name, out)
        if len(out.encode("utf-8")) < before_bytes:
            emit(LoopStep(
                it, "guard",
                f"📦 {name} 输出过大，已 spill 到文件",
                f"{before_bytes:,} 字节 → 上下文只留预览 "
                f"{len(out.encode('utf-8')):,} 字节。"
                f"需要细节用 read_file 取。",
                ok=True,
            ))
            # 落进会话日志，/agent 的日志页才能把 spill 讲清楚。
            # 复用同一份落盘文件时不重复记（spilled 没变长），
            # 否则统计会把同一份输出算两次、看起来省得更多。
            if len(self.spill.spilled) > spilled_before:
                rec = self.spill.spilled[-1]
                self.session.append(
                    "spill/applied", tool=name,
                    original_bytes=before_bytes,
                    inline_bytes=len(out.encode("utf-8")),
                    path=rec.path,
                )

        # 记录"是否验证过"：成功的 pytest / 运行脚本才算证据。
        # 只看命令名还不够，必须退出码为 0 —— 跑挂了不算验证。
        #
        # 这两个计数是**反射闸门的输入**：`_verified` 决定"能不能收尾"，
        # `_failed_verifies` 记录"验证过但又改坏了"。后者单独存是因为
        # "跑过一次绿"和"最后一次跑是绿的"是两件事 —— 反射要求的是后者
        # 那种证据（见 reflection.EvidenceBeforeFinish 的说明）。
        if name == 'run_shell':
            from .runtime import Evidence, workspace_digest
            state = self.ws.last_execution or {}
            if ok:
                self.evidence = Evidence(str(args.get('command', '')),
                                         state.get('exit_code'), workspace_digest(self.ws.scope))
                self.session.append('verification/evidence', command=self.evidence.command,
                                    exit_code=self.evidence.exit_code, digest=self.evidence.digest)
            else:
                self.evidence = Evidence()
                self._failed_verifies += 1
            self._verified = self.evidence.valid(self.ws.scope)
        if name in ('write_file', 'edit_file', 'append_file', 'delete_file') and ok:
            self._verified = False
            p = str(args.get('path', ''))
            if p and p not in self._files_touched:
                self._files_touched.append(p)

        return out
