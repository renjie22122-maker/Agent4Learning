"""ConversationRuntime: extracted lifecycle responsibility with stable event semantics."""
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

class ConversationRuntime:
    def resume(self, session_path: Path | str, model: str | None = None) -> LoopResult:
        """从一份会话日志接着跑。

        **重放是纯只读的**：它只重建"已经发生了什么"，绝不重放副作用。
        已经写过的文件不会重写、已经跑过的命令不会重跑 —— 那是日志记录，
        不是待执行的指令。恢复后继续的是**剩下的工作**。

        如果日志显示任务已经 finished，直接返回，不重复花钱。
        """
        from .session import SessionLog as _SL

        # 用 open() 而不是 load()：要把后续事件**续写**到同一份日志，
        # seq 必须接着已有的往下走（load 出来的对象 fsync 是关的，也不适合续写）。
        log, skipped = _SL.open(Path(session_path), fsync=True)
        state = replay(log, skipped)
        self.session = log  # 继续往同一份日志追加，保持一条时间线

        if state.finished:
            r = LoopResult(ok=True, stopped_by="already_finished",
                           summary="（该会话此前已标记完成，未重复执行）")
            r.iterations = state.iterations_done
            r.tool_calls = state.tool_calls_done
            r.usd = state.usd
            r.tokens_in, r.tokens_out = state.tokens_in, state.tokens_out
            r.steps.append(LoopStep(0, "finish", "会话已完成，直接返回", "", ok=True))
            return r

        created = log.of_kind("session/created")
        task = created[0].data.get("task", "") if created else ""
        if not task:
            r = LoopResult(ok=False, stopped_by="resume_failed",
                           error="会话日志里找不到原始任务描述")
            return r

        # 把"已经做过什么"告诉模型 —— 否则它会从头再来一遍，
        # 那还不如不恢复（既浪费钱，还可能覆盖已改好的文件）。
        carried = (
            f"【这是**续跑**，不是新任务】\n"
            f"原任务：{task}\n"
            f"上次已执行 {state.iterations_done} 轮、"
            f"{state.tool_calls_done} 次工具调用，"
            f"花费 ${state.usd:.4f}，"
            f"中断原因：{state.last_step and '进程中断'}\n"
        )
        if state.files_written:
            carried += ("已改动过的文件（**先 read_file 确认现状，不要盲目重写**）：\n"
                        + "\n".join(f"  - {p}" for p in state.files_written[:20]) + "\n")
        if state.commands_run:
            carried += ("已执行过的命令（**不要重复跑**）：\n"
                        + "\n".join(f"  - {c[:90]}" for c in state.commands_run[-10:]) + "\n")
        carried += "请从**上次中断的地方继续**，先用 list_dir / read_file 确认现状。"

        if state.unknown_calls:
            carried += "\n以下调用结果未知，先检查外部状态，禁止自动重放：" + json.dumps(state.unknown_calls, ensure_ascii=False)
        self._iter_offset = state.iterations_done + 1
        self._task_text = task
        self.session.observers.append(self._on_session_event)
        messages = [ChatMessage('system', CODING_SYSTEM)]
        if state.messages:
            messages = [ChatMessage(m['role'], m.get('content') or '',
                                   tool_calls=m.get('tool_calls'), tool_call_id=m.get('tool_call_id', ''))
                        for m in state.messages]
        messages.append(ChatMessage('user', carried))
        self._files_touched = list(state.files_written)
        try:
            r = self._turn(task, messages, model, fresh=False)
        finally:
            self._close_tasks()
        self._conversation = messages
        r.stopped_by = f"resumed:{r.stopped_by}"
        return r


    def _save_conversation(self, messages):
        current = [m.to_api() for m in messages]
        n = len(self._persisted_messages)
        if current[:n] == self._persisted_messages:
            for message in current[n:]:
                self.session.append('conversation/message', message=message)
        else:
            self.session.append('conversation/snapshot', messages=current)
        self._persisted_messages = json.loads(json.dumps(current))


    def restore_conversation(self, path):
        """Restore context without running a model or replaying a tool."""
        log, skipped = SessionLog.open(Path(path), fsync=True)
        state = replay(log, skipped)
        if not state.messages:
            raise ValueError('该旧日志未保存完整对话，不能可靠恢复上下文')
        self.session = log
        self.session.observers.append(self._on_session_event)
        self._conversation = [ChatMessage(m['role'], m.get('content') or '',
                              tool_calls=m.get('tool_calls'), tool_call_id=m.get('tool_call_id', ''))
                              for m in state.messages]
        self._persisted_messages = json.loads(json.dumps(state.messages))
        self._used_call_ids = {e.data['call_id'] for e in log.events if e.kind == 'tool/call' and e.data.get('call_id')}
        self._iter_offset = state.iterations_done + 1
        self._files_touched = list(state.files_written)
        if state.unknown_calls:
            self._conversation.append(ChatMessage('user', '恢复提示：这些调用结果未知，先核对实际状态，不得自动重放：' + json.dumps(state.unknown_calls, ensure_ascii=False)))
        return state


    def run(self, task: str, model: str | None = None,
            context: str = "") -> LoopResult:
        """跑一个**新任务**（首轮）。

        想接着上一次的对话继续问，用 `continue_with()` —— 那才会共用
        同一份 messages，模型才知道自己刚才做过什么。
        """
        messages: list[ChatMessage] = [ChatMessage("system", CODING_SYSTEM)]
        if getattr(self.ws, 'general_chat', False):
            messages.append(ChatMessage('system', '当前为普通对话，不是项目编码任务。'
                '纯问答直接给用户完整答复即可结束，不必调用 finish、list_dir 或命令来证明回答完成。'
                '只有用户要求生成产物时才用本会话文件工具；文本可用 check_file_text 核对并提交独立验收。'
                '没有项目目录，不能执行 shell 或申请宿主命令。用户仅提供路径不会自动授权绑定项目，请引导其通过项目设置选择。'))
        if context:
            messages.append(ChatMessage("system", f"[背景资料]\n{context}"))
        messages.append(ChatMessage(
            "user",
            (f"任务：{task}\n\n这是普通对话，没有绑定任何项目目录。直接回答问题，无需先扫描文件。"
             f"附件可通过附件工具读取；只有需要生成文件时才使用本会话独立产物目录 {self.ws.root}。"
             "不得访问其他会话或默认 workspace；执行本地命令、修改项目或委派项目工作需要用户先选择项目。"
             if getattr(self.ws,'general_chat',False) else
             f"任务：{task}\n\n工作区根目录：{self.ws.root}；可用文件夹：{self.ws.roots}。文件工具支持 @别名/路径，run_shell 的 cwd 可选 @别名；每条沙箱命令仅授权所选文件夹\n"
             f"（先用 list_dir 看看里面有什么，再决定怎么做）"),
        ))
        try:
            res = self._turn(task, messages, model, fresh=True)
        finally:
            self._close_tasks()
        self._note_delivery(messages, res)
        # 失败/中止也要留下对话：用户很可能想"接着把没做完的做完"。
        self._conversation = list(messages)
        return res


    def continue_with(self, message: str, model: str | None = None) -> LoopResult:
        """在**同一次对话**里接着问。

        为什么需要它：`run()` 每次都新建 messages，所以第二次提问对模型来说
        是全新的对话 —— 它不记得自己刚写过什么文件、跑过什么测试，
        于是会重新 `list_dir`、重新读一遍文件、甚至重复问同样的问题。
        那种"每轮从零开始"的体验是：单轮看着还行，连着用就完全没法用。

        这里把上一轮的完整 messages（含所有工具结果）接着用，所以模型知道
        自己已经做了什么。这也意味着**上下文会跨轮累积** —— 压缩策略
        （`Compactor`）会自动接手，不需要在这里额外处理。

        上一轮被中止或失败时仍然可用：对话历史在 `self._conversation` 里，
        不依赖上一轮的成败。
        """
        if not self._conversation:
            raise RuntimeError(
                "还没有任何对话可以继续 —— 先调用 run() 提交第一个任务")
        messages = list(self._conversation)
        messages.append(ChatMessage("user", message))
        self.session.append("followup/user", text=message[:2000])
        try:
            res = self._turn(message, messages, model, fresh=False)
        finally:
            self._close_tasks()
        self._note_delivery(messages, res)
        self._conversation = list(messages)
        return res


    def _note_delivery(self, messages: list[ChatMessage],
                       res: LoopResult) -> None:
        """把这一轮的**交付语义**写回对话。

        为什么必须有这一步：`finish` 是一个**工具调用**，执行完就变成一条
        `role="tool"` 的消息（"工具返回：完成"）。如果就这样结束，
        下一轮看到的最后一条是「某个工具返回了」，而不是
        「我说过：我已经把 X 写好并跑通了」。

        这个差别在多轮里很关键：没有它，模型在追问里会表现得像刚被工具
        打断，而不是刚交付过东西 —— 它会重新确认一遍自己做了什么，
        白白多花一轮钱。
        """
        if res.stopped_by != "finish" or not res.summary:
            return
        text = f"（本轮交付）{res.summary}"[:2000]
        if any((m.content or "").strip() == text for m in messages[-3:]):
            return  # 幂等：重复调用不会堆出一串一样的消息
        messages.append(ChatMessage("assistant", text))
