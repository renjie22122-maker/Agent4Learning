"""把 capstone 平台跑成一个**带可视化面板的真实 HTTP 服务**。

两种用途：

1. **看链路**：用内置模拟器，观测限流/缓存/熔断/成本归因等工程行为；
2. **真的用**：在「LLM 设置」页填上任意 OpenAI 兼容端点的 key，就换成真实模型，
   **可靠性机制完全不变**。

页面（顶层标签）：

    /            对话工作台     提问、看 span 归因
    /models      模型与路由     档位→真实模型映射、路由分布、降级链
    /cache       缓存           五层命中率、护栏、策略表、实时探测
    /resilience  熔断与限流     熔断器状态、故障注入、限流桶、舱壁
    /history     请求历史       每次请求的 trace 列表
    /trace/<id>  单次 trace     完整 span 树
    /sessions    会话与隔离     三元组会话、劫持拦截
    /metrics-view 指标与成本    四层指标 + 按租户/模型/阶段归因
    /settings    LLM 设置       接真实模型、测连接

运维端点：``/livez`` ``/readyz`` ``/metrics`` ``/api/snapshot``
故障注入：``/admin/degrade`` ``/admin/recover`` ``/admin/hang`` ``/admin/drain``

**只绑定 127.0.0.1**：这是演示服务，不要暴露到公网。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentlab.metrics import METRICS
from agentlab.util import force_utf8

from . import pages, ui
from .config import PlatformConfig
from .context import RequestContext
from .engine import AgentRequest, AgentResponse
from .llmconfig import PRESETS, LLMConfig
from .loadgen import QUERY_POOL, TenantProfile, build_engine
from .service import ServicePlatform, ServiceUnavailable

# --------------------------------------------------------------------------
# 演示用租户
# --------------------------------------------------------------------------

DEMO_TENANTS = {
    "alpha": TenantProfile("alpha", users=2, weight=1.0, batch_ratio=0.1),
    "beta": TenantProfile("beta", users=2, weight=1.0, batch_ratio=0.1),
    "gamma": TenantProfile("gamma", users=1, weight=1.0, batch_ratio=0.9),
}


def session_locked(fn):
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapped


class DemoServer:
    """持有引擎、历史、LLM 配置，并支持运行时切换后端。"""

    def __init__(self, cfg: PlatformConfig, llm_cfg: LLMConfig, seed_cache: bool,
                 degrade: bool, guard=None):
        self.cfg = cfg
        self.llm_cfg = llm_cfg
        self.guard = guard
        self.tenants = DEMO_TENANTS
        self.history: deque[AgentResponse] = deque(maxlen=300)
        self._lock = threading.RLock()
        self._seq = 0
        self.started_at = time.time()
        self._build(degrade=degrade)
        # 编码 agent 的工作区管理 + 运行中的会话状态
        from .workspaces import WorkspaceManager

        self.ws_mgr = WorkspaceManager()
        self.agent_state: dict = {}
        self._stop_flag = threading.Event()
        #: 当前**活着的** agent 实例。多轮对话靠它 —— 它持有 `_conversation`
        #: （包含所有工具结果的完整 messages）。重建它是做不到的：
        #: 会话日志只有工具结果的截断版本（400 字符），用它重建出来的
        #: 上下文和真实上下文不一样，模型看到的东西就变了。
        #: 单机单用户，驻留内存是可以接受的取舍；多用户场景必须换成
        #: "每会话一个进程 + 上下文卸载到存储"。
        self._agent = None
        self.live_sessions = {}
        self.permissions_token = uuid.uuid4().hex
        self.knowledge_jobs = {}
        if seed_cache:
            self.seed_cache()

    def import_knowledge(self, path, *, reindex=False, source=None):
        from .knowledge import KnowledgeBase, database_root
        with self._lock:
            if any(job['status'] == 'running' for job in self.knowledge_jobs.values()):
                raise RuntimeError('已有导入正在执行，请等待完成')
            if len(self.knowledge_jobs) >= 30:
                self.knowledge_jobs.pop(next(iter(self.knowledge_jobs)))
            job_id = uuid.uuid4().hex
            if source is None:
                from .knowledge_scopes import resolve
                source = resolve(self, {})
            workspace = self.ws_mgr.current
            job = {'id': job_id, 'workspace': str(workspace), 'knowledge_root': source['root'], 'scope': source['id'], 'status': 'running', 'results': []}
            self.knowledge_jobs[job_id] = job
        def worker():
            try:
                kb = KnowledgeBase(source['root'])
                job['results'] = [] if reindex else kb.import_path(path)
                from .vector_knowledge import build
                job['vectors'] = build(kb, progress=lambda n:job.update(vector_chunks=n))
                job['status'] = 'completed_with_errors' if any(r['status'] == 'error' for r in job['results']) else 'completed'
            except Exception as exc:
                job.update(status='failed', error=str(exc))
        threading.Thread(target=worker, daemon=True, name='knowledge-import').start()
        return job_id

    # -- 编码 agent ---------------------------------------------------------
    @session_locked
    def start_agent_task(self, task: str, max_iters: int = 0,
                         max_usd: float | None = None, workspace_group: str | None = None, attachment_ids=None) -> str:
        """在后台线程里跑编码 agent 的**首轮**。返回会话 ID。

        **必须放后台线程**：一次任务要几十秒到几分钟，同步跑会把 HTTP 请求挂住
        （浏览器转圈、还会撞上代理超时）。界面靠轮询 /agent 看进度。

        追问走 `continue_agent_task()` —— 那个会复用同一个 agent，
        所以模型记得自己刚做过什么。
        """
        from .guard import CostGuard
        from .llm import OpenAIChatClient
        from .loop import CodingAgent
        from .workspace import Workspace

        from .attachments import validate_ids,describe,bind
        attached=validate_ids(attachment_ids or [])
        task=(task.strip() or '请阅读并概述附件内容')+describe(attached) if attached else task
        self._reject_bad_task(task)
        if not self.llm_cfg.is_real:
            raise RuntimeError(
                "编码 agent 需要真实 LLM（要函数调用能力）。"
                "请先到「LLM 设置」页配置 API Key。"
            )

        from .workspaces import DEFAULT_WORKSPACE
        from .general_chat import storage
        session_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        general = workspace_group == '__general__' or (not workspace_group and
            not self.ws_mgr.current_group and self.ws_mgr.current == DEFAULT_WORKSPACE)
        if general:
            folders, group_id = {'main':storage(self.ws_mgr, session_id)}, ''
        elif workspace_group:
            group = self.ws_mgr.groups.get(workspace_group)
            if not group: raise ValueError('项目不存在，请重新选择项目')
            folders = self.ws_mgr.validate_folders(group['folders'])
            group_id = workspace_group
        else:
            folders, group_id = self.ws_mgr.current_folders(), self.ws_mgr.current_group
        root = next(iter(folders.values()))
        self._check_workspace_available(folders)
        self._stop_flag = threading.Event()
        self.agent_state = {
            "session_id": session_id, "task": task, "status": "running",
            "workspace": str(root), "started_at": time.time(),
            "conversation_kind": 'general' if general else 'project',
            "workspace_roots": {k:str(v) for k,v in folders.items()}, "workspace_group": group_id,
            "steps": [], "summary": "", "usd": 0.0, "iterations": 0,
            "tool_calls": 0, "error": "", "turns": [], "pending": [], "steering_messages": [], "current_text": task,
        }
        self._agent = self._build_agent(folders, session_id, max_iters, max_usd)
        bind(self._agent,attached)
        self.live_sessions[session_id] = (self.agent_state, self._agent, self._stop_flag)
        self._run_agent_thread(task, followup=False)
        return session_id

    @session_locked
    def continue_agent_task(self, message: str, max_iters: int = 0,
                            max_usd: float | None = None, session_id: str = '', attachment_ids=None) -> str:
        """在**同一个会话**里追问。返回会话 ID。

        复用 `self._agent`，所以模型能看到上一轮的全部工具结果 ——
        这正是"只能问一次"和"能反复问答"的区别。
        没有活着的 agent 时（服务重启过）明确报错，而不是偷偷开个新会话：
        偷偷新开会让用户以为"它记得"，实际它什么都不记得。
        """
        from .attachments import validate_ids,describe,bind
        attached=validate_ids(attachment_ids or [])
        message=(message.strip() or '请阅读并概述附件内容')+describe(attached) if attached else message
        if not message.strip():
            raise RuntimeError('消息不能为空')
        target = session_id or self.agent_state.get('session_id', '')
        from .conversations import store_for
        if store_for(self).load().get(target,{}).get('deleted'):
            raise ValueError('请先从回收站恢复会话，再继续对话')
        if target not in self.live_sessions:
            self.restore_agent_session(target)
        state, agent, stop = self.live_sessions[target]
        bind(agent,attached)
        if state.get('status') == 'running':
            item = {'id': uuid.uuid4().hex, 'text': message, 'status': 'queued', 'at': time.time()}
            agent.session.append('steering/queued', **item)
            agent.session.flush('steering_received')
            state['pending'].append(item)
            state['steering_messages'].append(item)
            return target
        self._reject_bad_task(message)
        self._check_workspace_available(agent.ws.scope, target)
        self.agent_state, self._agent, self._stop_flag = state, agent, stop
        if self._agent is None:
            raise RuntimeError(
                "当前没有可继续的对话（服务重启过，或还没提交过任务）。"
                "请在「提交任务」里开一个新任务。"
            )
        self.agent_state["steps"] = []
        self.agent_state['progress_messages'] = []
        self.agent_state['progress_times'] = []
        self.agent_state['streamed_text'] = ''
        self.agent_state['turn_saved'] = False
        self.agent_state['summary'] = ''
        self.agent_state['current_text'] = message
        self.agent_state['steering_start'] = len(self.agent_state['steering_messages'])
        self.agent_state.pop('finished_at', None)
        self.agent_state['started_at'] = time.time()
        self.agent_state["model_calls"] = 0
        self._agent.hard_iterations = max(0, int(max_iters))
        from .loop import CompositePolicy, MaxIterationsPolicy
        self._agent.policy = CompositePolicy(MaxIterationsPolicy(
            soft_limit=self._agent.soft_iterations,
            hard_limit=self._agent.hard_iterations))
        self._agent.guard.max_usd = max_usd if max_usd and max_usd > 0 else None
        self.agent_state["status"] = "running"
        self._stop_flag.clear()
        self._run_agent_thread(message, followup=True)
        return self.agent_state["session_id"]

    # ------------------------------------------------------------------
    def _reject_bad_task(self, task: str) -> None:
        # 空任务必须在**提交时**拒绝，而不是放它进去跑一轮再失败：
        # 那样既花了钱，又让"会话历史"里多一条毫无意义的记录。
        # （实测踩到：检查脚本传了空 task，结果真的启了一个 agent 任务。）
        if not (task or "").strip():
            raise RuntimeError("任务描述不能为空 —— 请写清要让 agent 做什么")
        if len(task.strip()) < 4:
            raise RuntimeError(f"任务描述太短（{len(task.strip())} 字符），"
                               f"请写清目标和验收标准")

    def _check_workspace_available(self, root, sid=''):
        running = [s for key, (s, a, flag) in self.live_sessions.items() if key != sid and s.get('status') == 'running']
        if len(running) >= 3:
            raise RuntimeError('已有 3 个主任务运行，请等待一个完成')
        roots = [Path(p).resolve() for p in (root.values() if isinstance(root,dict) else [root])]
        if any(a.is_relative_to(b) or b.is_relative_to(a) for s in running
               for a in roots for b in [Path(p).resolve() for p in s.get('workspace_roots', {'main':s['workspace']}).values()]):
            raise RuntimeError('该工作区已有运行任务；请追加提示，或为并行任务选择独立工作区')

    def restore_agent_session(self, sid):
        from .session import SessionLog
        entry = next((s for s in self.list_agent_sessions(200) if s['session_id'] == sid and s.get('log_path')), None)
        if not entry:
            raise ValueError('找不到可恢复的会话日志')
        root = Path(entry['workspace']).resolve()
        if hasattr(self.ws_mgr, 'resolve'):
            root = self.ws_mgr.resolve(root)
        if entry.get('workspace_roots'):
            root = self.ws_mgr.validate_folders(entry['workspace_roots'])
        self._check_workspace_available(root, sid)
        view, _ = self.view_agent_session(sid)
        state = {**view, 'status': 'stopped', 'pending': [], 'steering_messages': [],
                 'started_at': time.time(), 'steps': [], 'current_text': '', 'historical': False}
        previous = self.agent_state, self._agent, self._stop_flag
        try:
            self.agent_state = state; self._stop_flag = threading.Event()
            agent = self._build_agent(root, sid, 0, None)
            agent.restore_conversation(entry['log_path'])
            saved_users = [(e.seq, e.data['message'].get('content', '')) for e in agent.session.events
                           if e.kind == 'conversation/message' and e.data['message'].get('role') == 'user']
            for event in agent.session.events:
                if event.kind == 'steering/queued' and not any(seq > event.seq and event.data['text'] in text for seq,text in saved_users):
                    item = {**event.data, 'status': 'queued'}
                    state['pending'].append(item); state['steering_messages'].append(item)
            self.live_sessions[sid] = (state, agent, self._stop_flag)
        finally:
            self.agent_state, self._agent, self._stop_flag = previous

    def _build_agent(self, root, session_id, max_iters, max_usd):
        from .guard import CostGuard
        from .llm import OpenAIChatClient
        from .loop import CodingAgent
        from .workspace import Workspace
        workspace = Workspace(root)
        workspace.group_id = self.agent_state.get('workspace_group', '')
        workspace.general_chat = self.agent_state.get('conversation_kind') == 'general'
        from .knowledge_scopes import configure
        configure(workspace, self.ws_mgr, session_id)
        root = workspace.root

        from dataclasses import replace
        cfg = replace(self.llm_cfg)
        from .model_capacity import discover
        discover(cfg)
        from .billing import refresh
        refresh(cfg)
        state = self.agent_state
        stop_flag = self._stop_flag
        def steering():
            with self._lock:
                pending = list(state['pending'])
                state['pending'].clear()
                for item in pending:
                    item['status'] = 'delivered'
                    agent_ref[0].session.append('steering/delivered', id=item['id'])
                return [item['text'] for item in pending]
        agent_ref = []
        # ⚠ 护栏上限彻底改成**可选**（None = 不设限）。
        #
        # 原来这里硬写 `max_calls=400`、`max_usd` 取默认 $0.30，于是永远存在
        # 一个硬天花板，而它停下来的位置往往正是"活儿快干完了"的地方
        # （编码任务的收尾阶段：跑测试 → 修 → 再跑 → finish，本身要好几轮，
        # 每轮都在计费）。用户的原话是"我不希望有这个上限"。
        #
        # 现在：不传 / 传 0 / 传负数 → 不设限。约束由使用者决定。
        guard = CostGuard(max_usd=max_usd, max_calls=None)
        self.cfg.adapt_to_real_llm(cfg.timeout_s)

        def on_step(step) -> None:
            state["iterations"] = step.index
            if step.kind == "think" and step.title == "正在等待模型响应":
                state['usage_pending'] = True
                if state.get('streamed_text'):
                    state.setdefault('progress_messages',[]).append(state['streamed_text'])
                    state.setdefault('progress_times',[]).append(state.get('streamed_at',time.time()))
                state['streamed_text'] = ''
                state["model_calls"] = state.get("model_calls", 0) + 1
            state["last_progress_at"] = time.time()
            state["phase"] = step.title
            state["steps"].append({**step.to_dict(), "at":time.time()})

        client = OpenAIChatClient(cfg)
        client.cancel_event = stop_flag
        def on_text(delta):
            if not state.get('streamed_text'): state['streamed_at'] = time.time()
            state['streamed_text'] = state.get('streamed_text', '') + delta
            state['last_progress_at'] = time.time()
        client.on_text = on_text
        agent = CodingAgent(
            llm=client, cfg=cfg, workspace=workspace,
            guard=guard, on_step=on_step,
            hard_iterations=max(0, int(max_iters)),
            session_dir=(Path(self.ws_mgr.state_path).parent / '.sessions' if workspace.general_chat
                         else Path(root).parent / ".sessions" if root.name == "workspace" else root / ".sessions"),
            session_id=session_id,
            stop_flag=stop_flag, steering=steering,
        )
        agent_ref.append(agent)
        def on_usage(record):
            state['usage_pending'] = False
            state['billing'] = record
            for field in ('in_tokens', 'out_tokens', 'cached_tokens', 'usd', 'usd_min'):
                state['total_' + field] = state.get('total_' + field, 0) + record[field]
            state['usage_estimated'] = state.get('usage_estimated', False) or record['usage_estimated']
            agent.session.append('ui/billing', **{k:v for k,v in state.items() if k.startswith('total_') or k in ('billing','usage_estimated')})
        agent.on_usage = on_usage
        agent.independent_review_required = True
        from .access_modes import apply
        apply(agent, getattr(self, 'default_permission_mode', 'auto'))
        if workspace.general_chat:
            from .general_chat import install
            install(agent)
        return agent

    def _run_agent_thread(self, text: str, *, followup: bool) -> None:
        agent = self._agent
        state = self.agent_state
        stop = self._stop_flag
        agent.session.append('run/started', text=text, permission_mode=getattr(agent,'permission_mode','auto'),
                             workspace_roots={k:str(v) for k,v in agent.ws.roots.items()},
                             max_iters=agent.hard_iterations, max_usd=agent.guard.max_usd,
                             recovered=bool(getattr(agent,'_crash_recovery',False)))
        agent._crash_recovery = False

        def worker() -> None:
            try:
                if followup:
                    r = agent.continue_with(text)
                else:
                    r = agent.run(text, model=agent.cfg.model_or("mid")
                                  or agent.cfg.model)
                state["turns"].append({
                    "text": text, "at":state["started_at"], "ended_at":time.time(),
                    "progress_times":list(state.get("progress_times",[]))+([state.get("streamed_at",time.time())] if state.get("streamed_text") else []),
                    "summary": r.summary or r.error or "", "steps": list(state['steps']),
                    "progress_messages": list(state.get('progress_messages',[])) + ([state['streamed_text']] if state.get('streamed_text') else []),
                    "steering_messages": [dict(x) for x in state['steering_messages'][state.get('steering_start', 0):]],
                    "usd": round(r.usd, 6), "iterations": r.iterations, "model_calls": r.model_calls,
                    "stopped_by": r.stopped_by,
                })
                from .conversation_versions import capture
                state['turns'][-1]['file_version'] = capture(self.ws_mgr, agent.ws.roots)
                agent.session.append('ui/turn', **state['turns'][-1])
                state['turn_saved'] = True
                from .billing import include_children
                include_children(state, agent)
                agent.session.append('ui/billing', **{k:v for k,v in state.items() if k.startswith(('total_', 'child_')) or k in ('billing','usage_estimated')})
                agent.session.flush('ui_turn_saved')
                state.update({
                    "result_status": "done" if r.ok else "stopped",
                    "summary": r.summary or r.error,
                    "usd": round(r.usd, 6), "iterations": r.iterations, "model_calls": r.model_calls,
                    "tool_calls": r.tool_calls,
                    "stopped_by": r.stopped_by,
                    "spill": agent.spill.summary(),
                    "compaction": [
                        {"before": c.tokens_before, "after": c.tokens_after,
                         "pruned": c.pruned, "summarized": c.summarized}
                        for c in agent.compactor.history
                    ],
                    "chat_messages": len(agent._conversation),
                })
            except Exception as exc:  # noqa: BLE001
                state.update({"result_status": "failed",
                                         "summary": f"{type(exc).__name__}: {exc}"})
                if not state.get('turn_saved'):
                    turn = {'text':text, 'at':state['started_at'], 'ended_at':time.time(), 'progress_times':list(state.get('progress_times',[]))+([state.get('streamed_at',time.time())] if state.get('streamed_text') else []), 'steering_messages':list(state['steering_messages'][state.get('steering_start',0):]), 'summary':state['summary'], 'steps':list(state['steps']),
                            'progress_messages':list(state.get('progress_messages',[])) + ([state['streamed_text']] if state.get('streamed_text') else [])}
                    state['turns'].append(turn)
                    state['turn_saved'] = True
                    agent.session.append('ui/turn', **turn)
                    agent.session.flush('ui_turn_failed')
            finally:
                try:
                    agent.session.append('run/settled', status=state.get('result_status','failed'))
                except Exception:
                    state['result_status'] = 'failed'
                with self._lock:
                    state['status'] = state.pop('result_status', 'failed')
                    state["finished_at"] = time.time()
                    # A message arriving during the final model request must not disappear.
                    if state['pending'] and not stop.is_set():
                        pending = list(state['pending']); state['pending'].clear()
                        for item in pending:
                            item['status'] = 'delivered'
                            agent.session.append('steering/delivered', id=item['id'])
                        next_text = '\n\n'.join(item['text'] for item in pending)
                        self.agent_state, self._agent, self._stop_flag = state, agent, stop
                        self.continue_agent_task(next_text, agent.hard_iterations, agent.guard.max_usd, state['session_id'])

        threading.Thread(target=worker, daemon=True,
                         name=f"coding-agent-{self.agent_state['session_id']}"
                         ).start()

    def agent_context(self) -> dict:
        """当前 agent 的上下文用量与压缩账目。

        **必须有这个接口**：数据本来就在压缩器里，但没有出口 ——
        用户只能看到"轮次/花费"，看不到"离撑爆还有多远、压缩省了多少"。
        没有出口的可观测性等于没有可观测性。
        """
        if self._agent is None:
            return {"available": False}
        try:
            return {"available": True, **self._agent.context_stats()}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    def list_agent_sessions(self, limit: int = 25) -> list[dict]:
        """列出可用会话（含历史会话，支持续跑）。"""
        out: list[dict] = []
        cur = self.agent_state.get("session_id")
        if cur:
            out.append({**self.agent_state,
                        "status": self.agent_state.get("status", "?")})
        # 扫会话目录，把历史会话也列出来
        from .session import SessionLog, replay

        for sid, (state, agent, stop) in self.live_sessions.items():
            if sid != cur:
                out.append(dict(state))
        seen: set[str] = {x['session_id'] for x in out}
        for d in self.ws_mgr.session_directories() | {Path(self.agent_state.get("workspace", "")) / ".sessions" if
                  self.agent_state.get("workspace") else None,
                  self.ws_mgr.current / ".sessions",
                  Path(self.ws_mgr.current).parent / ".sessions"}:
            if d is None or not d.exists():
                continue
            for p in sorted(d.glob("*.jsonl"),
                            key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
                sid = p.stem
                if sid in seen:
                    continue
                seen.add(sid)
                try:
                    log, sk = SessionLog.load(p)
                    st = replay(log, sk)
                    created = log.of_kind("session/created")
                    out.append({
                        "session_id": sid,
                        "log_path": str(p),
                        "_sort_time": p.stat().st_mtime,
                        "status": "finished" if st.finished else "incomplete",
                        "task": created[0].data.get("task", "") if created else "",
                        "workspace_roots": created[0].data.get('workspace_roots', {}) if created else {},
                        "workspace_group": created[0].data.get('workspace_group', '') if created else '',
                        "conversation_kind": created[0].data.get('conversation_kind', 'legacy') if created else 'legacy',
                        "branch_source": created[0].data.get('branch_source') if created else None,
                        "iterations": st.iterations_done,
                        "tool_calls": st.tool_calls_done,
                        "usd": st.usd, "workspace": str(log.of_kind("session/created")[0]
                                                        .data.get("workspace", "")) if created else "",
                    })
                except Exception:  # noqa: BLE001
                    continue
        from .conversations import store_for
        metadata=store_for(self).load()
        out=[{**row,**metadata.get(row['session_id'],{})} for row in out]
        out.sort(key=lambda row:(row.get('pinned',False),row.get('status')=='running', row.get('_sort_time',row.get('started_at',0))), reverse=True)
        return out[:limit]

    def view_agent_session(self, sid=''):
        """Select a browser view without changing the running worker or its workspace."""
        sid = sid or self.agent_state.get('session_id', '')
        if not sid:
            return None, {'available': False}
        if sid in self.live_sessions:
            state, agent, _ = self.live_sessions[sid]
            from .billing import include_children
            include_children(state, agent)
            snapshot = dict(state)
            snapshot['steps'] = list(state.get('steps', []))
            from .chat_timeline import enrich_turns
            snapshot['turns'] = enrich_turns(state.get('turns', []), agent.session.events)
            snapshot['chat_ready'] = True
            snapshot['elapsed'] = round((state.get('finished_at') or time.time()) - state['started_at'], 1)
            return snapshot, {'available': True, **agent.context_stats()}
        from .session import SessionLog
        entry = next((s for s in self.list_agent_sessions(200) if s['session_id'] == sid and s.get('log_path')), None)
        if not entry:
            raise ValueError('找不到该会话')
        log, _ = SessionLog.load(Path(entry['log_path']))
        from .chat_timeline import enrich_turns
        turns = enrich_turns([e.data for e in log.events if e.kind == 'ui/turn'], log.events)
        if not turns:
            # Legacy logs have truncated summaries; expose exactly what was saved.
            current = {'text': entry.get('task', ''), 'summary': '', 'steps': []}
            for event in log.events:
                if event.kind == 'followup/user':
                    turns.append(current)
                    current = {'text': event.data.get('text', ''), 'summary': '', 'steps': []}
                elif event.kind == 'assistant/message' and event.data.get('text'):
                    current['summary'] += event.data['text'] + '\n'
                elif event.kind == 'session/closed':
                    current['summary'] = event.data.get('summary') or current['summary']
                elif event.kind == 'tool/call':
                    current['steps'].append({'kind': 'tool', 'title': event.data.get('tool', ''), 'detail': str(event.data)})
            turns.append(current)
        restorable = any(e.kind in ('conversation/message', 'conversation/snapshot') for e in log.events)
        bills = log.of_kind('ui/billing')
        return {**entry, **(bills[-1].data if bills else {}), 'turns': turns, 'steps': [], 'chat_ready': restorable, 'historical': True}, {'available': False}

    # -- 构建 / 重建 --------------------------------------------------------
    def _build(self, degrade: bool = False) -> None:
        # 接真实 LLM 时先放宽超时预算，否则默认（按模拟器 p50 500ms 调的）
        # 会在真实模型上直接 BUDGET 超时 —— 用户不该为了换模型去调毫秒参数。
        if self.is_real:
            self.cfg.adapt_to_real_llm(self.llm_cfg.timeout_s)
        self.platform, self.engine = build_engine(
            self.cfg, seed=7, llm_cfg=self.llm_cfg, guard=self.guard
        )
        if degrade and not self.is_real:
            self.apply_degrade(error_rate=0.35, p50_ms=2000)
        self.platform.start(warmup=self.cfg.warmup_on_start)
        from .ui_state import mark_backend

        mark_backend("real" if self.is_real else "mock")

    @property
    def is_real(self) -> bool:
        return self.llm_cfg.is_real

    def switch_backend(self, llm_cfg: LLMConfig) -> str:
        """保存配置并**重建引擎**（换后端必须重建：模型档位/价格/并发都变了）。"""
        from .model_capacity import discover
        from .billing import refresh
        discover(llm_cfg)
        refresh(llm_cfg)
        llm_cfg.save()
        self.llm_cfg = llm_cfg
        with self._lock:
            self.history.clear()
            self._seq = 0
        self._build()
        kind = "真实 LLM" if self.is_real else "内置模拟器"
        target = llm_cfg.chat_url() if self.is_real else "（本地模拟，不联网）"
        return f"已切换到「{kind}」：{target}"

    # -- 演示辅助 -----------------------------------------------------------
    def apply_degrade(self, error_rate: float, p50_ms: float, hang: bool = False,
                      threshold: int | None = None, cooldown_s: float | None = None) -> str:
        """注入上游劣化。

        真实后端下这个操作**没有意义**（你不能命令 OpenAI 变慢），所以会明确告知，
        而不是假装改了。这点很重要：让人以为"我把它调慢了"却其实没生效，比不支持更糟。
        """
        if self.is_real:
            return (
                "当前是真实 LLM 后端，无法伪造上游延迟/错误率。"
                "想看熔断行为请切回「内置模拟器」，或用真实网络故障来触发。"
            )
        srv = self.engine.server
        for name in ("small-8b", "mid-32b", "large-400b"):
            srv.set_error_rate(name, error_rate)
            srv.set_latency(name, p50_ms)
            if hang:
                srv.hang(name, True, duration_s=2.0)
        if threshold is not None:
            self.cfg.breaker_failure_threshold = threshold
            self.engine.breakers = type(self.engine.breakers)(self.cfg)
        if cooldown_s is not None:
            self.cfg.breaker_cooldown_s = cooldown_s
        extra = f" 熔断阈值={threshold}" if threshold is not None else ""
        extra += f" 冷却={cooldown_s}s" if cooldown_s is not None else ""
        return f"上游已劣化：error_rate={error_rate} p50={p50_ms}ms hang={hang}{extra}"

    def recover(self) -> str:
        if self.is_real:
            return "当前是真实 LLM 后端，无需恢复（没有注入过故障）。"
        srv = self.engine.server
        for name in ("small-8b", "mid-32b", "large-400b"):
            srv.set_error_rate(name, self.cfg.provider_error_rate)
            srv.set_latency(name, self.cfg.provider_p50_ms)
            srv.hang(name, False)
        return "上游已恢复（熔断器会在冷却后半开探测，能自己回来）"

    def breaker_states(self) -> dict[str, str]:
        return {k: v.state for k, v in self.engine.breakers._breakers.items()}

    def seed_cache(self) -> str:
        """预热缓存：让第一个请求就能看到命中，而不是等第三轮。"""
        n = 0
        for tenant in self.tenants:
            for q, _truth in QUERY_POOL[:4]:
                try:
                    self.handle_ask(q, tenant, f"{tenant}-u0")
                    n += 1
                except Exception:  # noqa: BLE001
                    pass
        with self._lock:
            self.history.clear()
        return f"已预热缓存：{n} 个问答对"

    # -- 核心：处理一次提问 -------------------------------------------------
    def handle_ask(self, q: str, tenant: str, user: str = "u1",
                   session: str = "s1", needs_tools: bool = False,
                   use_retrieval: bool = False, persona: str = "general") -> AgentResponse:
        truth = next((t for query, t in QUERY_POOL if query == q), None)
        ctx = RequestContext(
            tenant_id=tenant,
            user_id=user,
            # session_id 含 tenant+user，保证全局唯一（少了就会串会话）
            session_id=f"{tenant}-{user}-{session}",
            groups=frozenset({f"g{tenant[0]}", "public"}),
            roles=frozenset({"viewer"}),
        )
        req = AgentRequest(
            query=q, ctx=ctx, needs_tools=needs_tools,
            use_retrieval=use_retrieval, persona=persona, truth=truth,
        )
        try:
            resp = self.platform.handle(req)
        except ServiceUnavailable as exc:
            resp = AgentResponse(ok=False, error=f"DRAINING: {exc}", http_status=503)
        with self._lock:
            self._seq += 1
            self.history.append(resp)
            if not resp.trace_id:
                resp.trace_id = ctx.trace_id
        return resp

    def find(self, trace_id: str) -> AgentResponse | None:
        with self._lock:
            for r in reversed(self.history):
                if r.trace_id == trace_id:
                    return r
        return None

    def snapshot(self) -> dict:
        with self._lock:
            hist = list(self.history)
        lat = [r.latency_ms for r in hist]
        return {
            "backend": "real" if self.is_real else "mock",
            "model": self.llm_cfg.model_or("mid"),
            "uptime_s": round(time.time() - self.started_at, 1),
            "requests": len(hist),
            "ok_rate": (sum(1 for r in hist if r.ok) / len(hist)) if hist else 0.0,
            "cache_hits": sum(1 for r in hist if r.cached),
            "p95_ms": _pct(lat, 95),
            "usd": round(self.engine.server.ledger.usd, 6),
            "provider": self.engine.server.summary_lines(),
            "cache": self.engine.cache.exact.stats(),
            "sessions": self.engine.sessions.stats(),
            "breakers": self.breaker_states(),
        }


def _usd_or_none(raw: str | None) -> float | None:
    """把界面上传进来的成本上限解析成 `float` 或 `None`（不设限）。

    空串 / `"none"` / `"0"` / 负数 / 解析不了 → **None（不设限）**。
    为什么是"不设限"而不是"用默认值"：用户把框清空就是在说
    "别拦我"。悄悄换回一个默认上限，等于无视他的意图 ——
    而这次实测的痛点正是"在快要完成的地方被护栏硬停"。
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("", "none", "null", "-", "不设限", "无上限"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def _int_or(raw: str | None, default: int) -> int:
    """整数解析，空/非法时回落到默认值。"""
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    pos = (len(s) - 1) * q / 100.0
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (pos - lo), 1)


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------


def make_handler(demo: DemoServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Agent4Learning/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return  # 静音默认访问日志

        # ---- 基础工具 --------------------------------------------------
        def _send(self, code: int, body: bytes | str, ctype: str = "text/html; charset=utf-8"):
            # Normalize before sending headers: a late TypeError would append a
            # second HTTP response inside the first response's HTML body.
            if isinstance(body, str):
                body = body.encode("utf-8")
            if not isinstance(body, bytes):
                raise TypeError("HTTP response body must be bytes or str")
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _q(self) -> tuple[dict[str, str], str]:
            p = urllib.parse.urlparse(self.path)
            return {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}, p.path

        def _safe(self, fn, *a, **kw):
            """任何异常都变成明确的 500 页面。

            实测教训：`/metrics` 曾因为一个空直方图格式化失败而抛异常，handler
            直接死掉 → 客户端收到"连接被关闭且没有任何响应"，排查时完全不知道
            是哪个端点、什么错。**服务端永远不能静默死亡。**
            """
            try:
                return fn(*a, **kw)
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc()
                print(f"[demo] {self.path} 异常：{type(exc).__name__}: {exc}")
                print(tb)
                try:
                    self._send(500, pages.error_page(self.path, exc, tb))
                except Exception:  # noqa: BLE001
                    pass

        def _authorize_browser(self):
            import secrets
            from http.cookies import SimpleCookie
            qs, path = self._q()
            if path == '/livez':
                return True
            hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in hosts:
                self._send(403, b'host not allowed', 'text/plain'); return False
            if path == '/auth' and self.command == 'GET':
                if not secrets.compare_digest(qs.get('token', ''), demo.permissions_token):
                    self._send(403, b'invalid access link', 'text/plain'); return False
                self.send_response(303)
                self.send_header('Set-Cookie', 'agentlab_access=' + demo.permissions_token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000')
                self.send_header('Location', '/agent?panel=1')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0'); self.end_headers(); return False
            cookie = SimpleCookie()
            try: cookie.load(self.headers.get('Cookie', ''))
            except Exception: pass
            value = cookie.get('agentlab_access')
            if not value or not secrets.compare_digest(value.value, demo.permissions_token):
                from .desktop_login import login_page
                self._send(401, login_page(), 'text/html; charset=utf-8')
                return False
            if self.command == 'POST' and self.headers.get('Origin') and self.headers['Origin'] != 'http://' + self.headers['Host']:
                self._send(403, b'origin not allowed', 'text/plain'); return False
            return True

        # ---- 路由 ------------------------------------------------------
        def do_GET(self):  # noqa: N802
            if self._authorize_browser():
                self._safe(self._route_get)

        def do_POST(self):  # noqa: N802
            if self._authorize_browser():
                self._safe(self._route_post)

        # ---- GET -------------------------------------------------------
        def _route_get(self):
            qs, path = self._q()
            if path == '/api/reply-feedback':
                from .conversation_actions import ratings
                return self._json(200, ratings(demo, qs.get('session','')))
            if path == '/api/human-input':
                from .human_input import list_questions
                return self._json(200, {'questions':list_questions(qs.get('session',''))})

            if path == '/workspaces':
                from .pages_workspaces import render
                return self._send(200, render(demo, qs))
            if path=='/agent/tools':
                from .pages_agent import _tools_menu
                from .ui import page
                return self._send(200,page('工具与设置','agent','<p><a href="/agent">返回对话</a></p>'+_tools_menu({'session_id':qs.get('session','')})))
            if path=='/agent/export':
                try: state,_=demo.view_agent_session(qs.get('session',''))
                except ValueError:return self._send(404,b'unknown conversation','text/plain')
                if not state:return self._send(404,b'unknown conversation','text/plain')
                parts=['# 对话 '+state['session_id']]
                for turn in state.get('turns',[]):
                    parts+=['\n## 用户\n',turn.get('text','')]
                    parts += ['\n追加提示：'+item['text'] for item in turn.get('steering_messages',[])]
                    parts+=['\n## Agent\n',*turn.get('progress_messages',[]),turn.get('summary','')]
                if state.get('status')=='running':
                    parts+=['\n## 用户\n',state.get('current_text') or state.get('task',''),'\n## 运行中（尚未完成）\n',*state.get('progress_messages',[]),state.get('streamed_text','')]
                body='\n\n'.join(parts).encode('utf-8')
                self.send_response(200);self.send_header('Content-Type','text/markdown; charset=utf-8')
                self.send_header('Content-Disposition','attachment; filename="conversation.md"')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return

            if path == '/knowledge':
                from .pages_knowledge import render
                return self._send(200, render(demo, qs))
            if path == '/memories':
                from .pages_memory import render
                return self._send(200, render(demo, qs))
            if path == '/team':
                from .pages_team import render
                return self._send(200, render(demo, qs))
            if path == '/approvals':
                from .approvals import list_requests
                rows = []
                for item in list_requests():
                    detail = html.escape(item['command']) + '\n宿主执行时限：' + str(item.get('timeout_s',60)) + ' 秒（不含等待批准）'
                    form = ''
                    if item['status'] == 'pending' and item['expires'] > time.time():
                        form = f'<form method="post" action="/approvals/decide"><input type="hidden" name="token" value="{demo.permissions_token}"><input type="hidden" name="id" value="{item["id"]}"><button name="decision" value="deny">拒绝</button><button name="decision" value="allow">允许这一次宿主命令</button></form>'
                    rows.append(f'<div class="card"><b>{html.escape(item["session"])}</b><p>{html.escape(item["workspace"])}</p><pre>{detail}</pre><p>{html.escape(item["reason"])}</p><p>{item["status"]}</p>{form}</div>')
                return self._send(200, ui.page('命令审批', 'agent', '<h1>宿主命令审批</h1><p>批准后命令以你的宿主权限执行，超出原生文件沙箱。仅绑定所示命令、会话和工作区，30 分钟内一次有效。审批后返回对话通知 Agent 继续。</p>' + ''.join(rows)))
            if path == '/skills':
                from .skill_import import list_skills
                rows = []
                for item in list_skills():
                    state = '启用' if item['enabled'] else '停用'
                    action = 'disable' if item['enabled'] else 'enable'
                    label = '停用' if item['enabled'] else '启用'
                    rows.append(f'<div class="card"><h2>{html.escape(item["name"])}</h2><p>技能名：{html.escape(item.get("skill_name",item["name"]))} · 来源：{html.escape(str(item.get("source","未记录来源")))}</p><p>{state}</p><details><summary>查看 SKILL.md</summary><pre style="white-space:pre-wrap">{html.escape(item["text"])}</pre></details><form method="post" action="/skills/toggle"><input type="hidden" name="token" value="{demo.permissions_token}"><input type="hidden" name="name" value="{item["name"]}"><button name="action" value="{action}">{label}</button></form></div>')
                body = f'<h1>技能管理</h1><p>导入本机技能目录、SKILL.md 或 ZIP。支持包内参考文件；导入不会运行脚本。技能不能增加命令或网络权限。</p><form method="post" action="/skills/import"><input type="hidden" name="token" value="{demo.permissions_token}"><input name="path" required placeholder="技能目录或 ZIP 完整路径" style="width:70%"><button>导入并启用</button></form><p>{html.escape(qs.get("notice", ""))}</p>'
                return self._send(200, ui.page('技能管理', 'agent', body + ''.join(rows)))
            if path == '/agent/access':
                from .access_modes import MODES
                sid = qs.get('session', '')
                entry = demo.live_sessions.get(sid)
                current = getattr(entry[1], 'pending_permission_mode', None) or getattr(entry[1], 'permission_mode', 'auto') if entry else getattr(demo, 'default_permission_mode', 'auto')
                options = ''.join(f'<option value="{key}" {"selected" if key == current else ""}>{label}</option>' for key, label in MODES.items())
                body = f'<h1>执行权限</h1><p>{"当前会话" if entry else "后续新任务默认模式"}：{html.escape(sid if entry else "")}</p><p>只读禁止写入与命令；自动沿用沙箱并逐次审批宿主命令；完全访问允许以你的本机权限执行命令，可能修改工作区以外文件。网络工具遵守网页访问设置。</p><form method="post" action="/agent/access"><input type="hidden" name="token" value="{demo.permissions_token}"><input type="hidden" name="session" value="{html.escape(sid)}"><select name="mode">{options}</select><button>应用权限选择</button></form><p>运行中在下一步骤边界生效；不撤销已执行的操作。重启后恢复任务默认回到自动模式。</p>'
                return self._send(200, ui.page('执行权限', 'agent', body))
            if path == '/knowledge/document':
                from .knowledge import KnowledgeBase
                from .knowledge_scopes import resolve
                import mimetypes
                original, name = KnowledgeBase(resolve(demo, qs)['root']).original(qs.get('id', ''))
                kind = mimetypes.guess_type(name)[0] or 'application/octet-stream'
                if not kind.startswith('image/'):
                    kind = 'application/octet-stream'
                return self._send(200, original.read_bytes(), kind)

            if path == '/permissions':
                from .pages_permissions import render
                return self._send(200, render(demo, qs))

            if path in ("/", "/index.html"):
                resp = None
                if qs.get("q"):
                    resp = demo.handle_ask(
                        qs["q"], qs.get("tenant", "alpha"),
                        qs.get("user", "u1"), qs.get("session", "s1"),
                        needs_tools=bool(qs.get("tools")),
                        use_retrieval=bool(qs.get("rag")),
                        persona=qs.get("persona", "general"),
                    )
                return self._send(200, pages.console(demo, qs, resp))

            if path == "/livez":
                ok, msg = demo.platform.liveness()
                return self._send(200 if ok else 500, f"{ok} {msg}".encode(), "text/plain")
            if path == "/readyz":
                ok, msg = demo.platform.readiness()
                return self._send(200 if ok else 503, f"{ok} {msg}".encode(), "text/plain")
            if path == "/metrics":
                return self._send(200, METRICS.to_prometheus().encode("utf-8"),
                                  "text/plain; version=0.0.4; charset=utf-8")
            if path == "/api/snapshot":
                return self._json(200, demo.snapshot())
            if path == "/api/agent-context":
                # 运行中时页面靠它增量刷新"上下文用了多少"，
                # 不必整页刷新（整页刷新会清掉正在输入的内容）。
                return self._json(200, demo.agent_context())
            if path == '/api/runtime':
                from .execution import execution_status
                current_mode = demo._agent.ws.execution_mode if demo._agent else None
                return self._json(200, {
                    'version': 'runtime-20', **execution_status(current_mode),
                    'review_status_tool': True, 'review_source_snapshot': True, 'ipv6_transport': True,
                    'chat_human_input': True, 'verification_knowledge_snapshot': True,
                    'local_vector_rag': bool(__import__('agentplat.vector_knowledge',fromlist=['config']).config()),
                    'chat_attachments': True, 'direct_settings_links': True,
                    'conversation_context_menu': True,
                    'multi_folder_workspaces': True, 'cumulative_progress': True, 'async_chat_submit': True,
                    'verification_token_budget': demo.llm_cfg.verification_token_budget,
                    'subagent_stream_cancellation': True,
                    'context_window': demo.llm_cfg.resolved_context_window(),
                    'context_source': __import__('agentplat.model_capacity', fromlist=['resolve']).resolve(demo.llm_cfg)[1],
                    'web_access': __import__('agentplat.web_policy', fromlist=['policy']).policy(),
                    'web_domains': __import__('agentplat.web_policy', fromlist=['domains']).domains(),
                    'mcp_configured': bool(os.environ.get('AGENTLAB_MCP_CONFIG')),
                    'subagent_modes': ['readonly', 'isolated'],
                    'subagent_depth': demo.llm_cfg.subagent_max_depth,
                    'subagent_total_tokens': demo.llm_cfg.subagent_total_tokens,
                    'team_messages': True, 'selective_memory': True,
                })
            if path == '/api/agent-events':
                entry = demo.live_sessions.get(qs.get('session', ''))
                if not entry:
                    return self._send(404, b'unknown live session', 'text/plain')
                from .pages_agent import _thread, _context_panel
                from .billing import include_children
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                state = entry[0]
                previous = None
                deadline = time.monotonic() + 25
                try:
                    while time.monotonic() < deadline:
                        payload = None
                        with demo._lock:
                            include_children(state, entry[1])
                            signature = (state.get('streamed_text'), state.get('status'), len(state.get('steps', [])), len(state.get('turns', [])), len(state.get('pending', [])), state.get('total_in_tokens'), state.get('usage_pending'), state.get('child_tokens'))
                            if signature != previous:
                                context = {'available':True, **entry[1].context_stats()} if entry[1] else {'available':False}
                                payload = json.dumps({'html': _thread(state), 'status': state.get('status'), 'context_html':_context_panel(context), 'context':context}, ensure_ascii=False)
                                previous = signature
                            done = state.get('status') != 'running'
                        if payload:
                            self.wfile.write(('event: progress\ndata: ' + payload + '\n\n').encode('utf-8'))
                            self.wfile.flush()
                        if done: break
                        time.sleep(.1)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return
            if path == '/api/agent-tasks':
                manager = demo._agent.children if demo._agent else None
                return self._json(200, {'tasks': [manager.get(k) for k in manager.tasks] if manager else []})

            # ---- 页面 ----
            if path in ("/agent", "/coding-agent", "/agent/session"):
                from . import pages_agent
                import copy
                manager = copy.copy(demo.ws_mgr)
                manager.conversation_view=qs.get('view','active')
                if qs.get('new')=='1' and qs.get('project'):
                    import copy
                    from .workspaces import DEFAULT_WORKSPACE
                    manager = copy.copy(demo.ws_mgr)
                    chosen = qs['project']
                    if chosen=='__general__':
                        manager.current, manager.current_group = DEFAULT_WORKSPACE, ''
                    elif chosen in manager.groups:
                        manager.current_group = chosen
                        manager.current = Path(next(iter(manager.groups[chosen]['folders'].values())))
                    else: return self._send(404,b'project not found','text/plain')

                try:
                    state, context = (None, {'available': False}) if qs.get('new') == '1' else demo.view_agent_session(qs.get('session') or qs.get('id', ''))
                except ValueError as exc:
                    return self._send(404, str(exc).encode('utf-8'), 'text/plain; charset=utf-8')
                return self._send(200, pages_agent.agent_page(
                    manager, demo.list_agent_sessions(200),
                    state,
                    notice=qs.get("notice", ""),
                    error=qs.get("error", ""),
                    panel=qs.get("panel") == "1",
                    ctx=context, csrf_token=demo.permissions_token,
                ))
            if path == "/agent/switch":
                from .workspaces import WorkspaceAccessError

                # 切换后一律把**抽屉打开**并锚到输入框。
                # 否则：切换成功了，但结果显示在收起的抽屉里 ——
                # 用户看到的是"点了没反应"。改动一个控件所在容器时，
                # 跳转目标必须跟着改，这是同一类错误（入口/回显不同步）。
                try:
                    _p, msg = demo.ws_mgr.switch(qs.get("path", ""))
                except WorkspaceAccessError as exc:
                    return self._redirect(
                        "/agent?panel=1&error=" + urllib.parse.quote(str(exc))
                        + "#wspath")
                return self._redirect(
                    "/agent?new=1&panel=1&notice=" + urllib.parse.quote(msg) + "#wspath")
            if path == "/agent/run":
                try:
                    sid = demo.start_agent_task(
                        qs.get("task", "").strip(),
                        _int_or(qs.get("max_iters"), 0),
                        _usd_or_none(qs.get("max_usd")),
                    )
                    return self._redirect(f"/agent?session={sid}&notice=" + urllib.parse.quote(
                        f"任务已提交，会话 {sid}。页面会自动刷新显示进度。"))
                except Exception as exc:  # noqa: BLE001
                    return self._redirect(
                        f"/agent?error={urllib.parse.quote(str(exc))}")
            if path == "/agent/chat":
                # 多轮追问：复用同一个 agent，所以模型记得上一轮做过什么。
                try:
                    sid = demo.continue_agent_task(
                        qs.get("message", "").strip(),
                        _int_or(qs.get("max_iters"), 0),
                        _usd_or_none(qs.get("max_usd")),
                        workspace_group=qs.get('workspace_group'),
                        session_id=qs.get('session', ''),
                    )
                    return self._redirect(f"/agent?session={sid}&notice=" + urllib.parse.quote(
                        "消息已收到；运行中会在步骤边界加入上下文。"))
                except Exception as exc:  # noqa: BLE001
                    return self._redirect(
                        f"/agent?error={urllib.parse.quote(str(exc))}")
            if path == "/agent/stop":
                # 真正通知 agent 线程停 —— 只改状态标记是"假中止"：
                # 界面显示已停止，而线程还在继续调模型花钱。
                target = qs.get('session') or demo.agent_state.get('session_id')
                entry = demo.live_sessions.get(target)
                if not entry:
                    return self._send(404, b'session not running', 'text/plain')
                state, agent, flag = entry
                agent.session.append('run/cancel_requested')
                flag.set()
                if agent.children:
                    agent.children.close()
                state["stop_requested"] = True
                return self._redirect("/agent?notice=" + urllib.parse.quote(
                    "已请求中止：正在取消命令与子任务；模型请求在其超时或响应后结束。"))
            if path == "/agent/log":
                from . import pages_agent
                from .session import SessionLog, replay

                sid = qs.get("session", "")
                import re
                if not re.fullmatch(r'[A-Za-z0-9_-]+', sid):
                    return self._send(400, b'invalid session ID', 'text/plain')
                p = None
                for d in demo.ws_mgr.session_directories():
                    cand = d / f"{sid}.jsonl"
                    if cand.exists():
                        p = cand
                        break
                if p is None:
                    return self._redirect("/agent?error=" + urllib.parse.quote(
                        f"找不到会话 {sid} 的日志"))
                log, sk = SessionLog.load(p)
                st = replay(log, sk)
                events = [{"seq": e.seq, "kind": e.kind, "data": e.data}
                          for e in log.events]
                return self._send(200, pages_agent.log_page(sid, events, {
                    "iterations_done": st.iterations_done,
                    "tool_calls_done": st.tool_calls_done, "usd": st.usd,
                }))
            if path == '/recovery':
                from .recovery import page
                return self._send(200, page(demo))
            if path == "/agent/resume":
                sid = qs.get("session", "")
                return self._redirect('/agent?session=' + urllib.parse.quote(sid, safe='') + '#quick-resume')

            if path == "/models":
                return self._send(200, pages.models(demo))
            if path == "/cache":
                return self._send(200, pages.cache_page(demo, qs))
            if path == "/resilience":
                return self._send(200, pages.resilience(demo))
            if path == "/history":
                return self._send(200, pages.history(demo))
            if path.startswith("/trace/"):
                return self._send(200, pages.trace_page(demo, path.split("/", 2)[2]))
            if path == "/sessions":
                return self._send(200, pages.sessions(demo))
            if path == "/metrics-view":
                return self._send(200, pages.metrics_view(demo))
            if path == "/settings":
                notice = qs.get("notice", "")
                return self._send(200, pages.settings(demo, qs, notice=notice))

            # ---- 提问（JSON / SSE）----
            if path == "/ask":
                return self._ask(qs)

            # ---- 故障注入 ----
            if path == "/admin/degrade":
                return self._admin(demo.apply_degrade(
                    error_rate=float(qs.get("error_rate", 0.35)),
                    p50_ms=float(qs.get("p50_ms", 2000)),
                    hang=qs.get("hang") == "1",
                    threshold=int(qs["threshold"]) if "threshold" in qs else None,
                    cooldown_s=float(qs["cooldown"]) if "cooldown" in qs else None,
                ), "故障已注入", "/resilience")
            if path == "/admin/recover":
                return self._admin(demo.recover(), "已恢复", "/resilience")
            if path == "/admin/hang":
                return self._admin(demo.apply_degrade(1.0, 3000.0, hang=True),
                                   "已模拟挂死", "/resilience")
            if path == "/admin/seed":
                return self._admin(demo.seed_cache(), "缓存已预热", "/cache")
            if path == "/admin/switch":
                cfg = demo.llm_cfg
                cfg.provider = "real" if cfg.provider == "mock" else "mock"
                msg = demo.switch_backend(cfg)
                return self._redirect(f"/settings?notice={urllib.parse.quote(msg)}")
            if path == "/admin/stacks":
                # ★ 现场取证：把所有线程的调用栈 dump 出来。
                #
                # 为什么必须有这个：实测遇到过一次"agent 卡死、CPU 100%、
                # 上下文一个字节不变、会话日志没写"。所有外部观测都只能说明
                # "它卡住了"，**说明不了卡在哪一行** —— 而定位死循环恰恰
                # 只需要那一行。当时我只能靠猜，最后没能定位。
                #
                # `faulthandler.dump_traceback` 是标准库自带的，不需要
                # 任何第三方依赖，也不需要附加调试器。下一次卡住时，
                # 打开这个端点就能直接看到死循环在哪一行。
                import faulthandler
                import io as _io
                import traceback as _tb

                buf = _io.StringIO()
                faulthandler.dump_traceback(file=buf, all_threads=True)
                # 再补一份"Python 层"的更可读的栈（含文件名与行号）
                for tid, frame in sys._current_frames().items():
                    buf.write(f"\n--- thread {tid} ---\n")
                    _tb.print_stack(frame, file=buf, limit=24)
                text = buf.getvalue()
                try:
                    Path(".agentlab_threads.txt").write_text(
                        text, encoding="utf-8")
                except OSError:
                    pass
                return self._send(200, text.encode("utf-8", "replace"),
                                  "text/plain; charset=utf-8")
            if path == "/admin/drain":
                rep = demo.platform.request_shutdown()
                body = f"""<h1>优雅停机完成</h1>
<div class=card><div class=kv>
  <dt>排空耗时</dt><dd class=mono>{rep['drain_ms']:.0f} ms（上限 {rep['grace_s']}s）</dd>
  <dt>被掐断的请求</dt><dd class="{'ok' if rep['interrupted'] == 0 else 'bad'}">{rep['interrupted']}</dd>
  <dt>排空后拒绝</dt><dd>{rep['rejected_after_drain']}</dd>
  <dt>liveness</dt><dd>{demo.platform.liveness()}</dd>
  <dt>readiness</dt><dd class=warn>{demo.platform.readiness()}</dd>
</div></div>
<p class=lead>readiness 变 false 但 liveness 仍 true —— 这就是"摘流量但不重启进程"。
进程即将退出（真实服务里编排器会回收该实例）。</p>"""
                self._send(200, ui.page("已停机", "console", body))
                # 等响应发完再退，否则客户端会看到连接被重置 —— 优雅停机要把
                # 最后一个响应交付出去。
                threading.Timer(0.4, lambda: os._exit(0)).start()
                return

            return self._send(404, ui.page(
                "404", "console",
                f"<h1>404</h1><p class=lead>没有这个页面：<span class=mono>{html.escape(path)}</span></p>"
                f"<p><a href='/'>← 回对话工作台</a></p>"))

        # ---- POST（设置表单）-------------------------------------------
        def _route_post(self):
            qs, path = self._q()
            length = int(self.headers.get("Content-Length") or 0)
            if path=='/agent/attachments':
                import secrets
                if not secrets.compare_digest(self.headers.get('X-Form-Token',''),demo.permissions_token):return self._json(403,{'error':'无效的附件上传授权'})
                if not 0<length<=34_000_000:return self._json(413,{'error':'单个文件最大 25 MB'})
                try:
                    from .attachments import upload
                    data=json.loads(self.rfile.read(length))
                    item=upload(data.get('name',''),data.get('data',''))
                    return self._json(200,item)
                except Exception as exc:return self._json(400,{'error':str(exc)})
            if length < 0 or length > 1_000_000 or length > 8192 and path in {'/permissions/web', '/knowledge/import', '/knowledge/remove'}:
                return self._send(413, b'body too large', 'text/plain')
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            if path in ('/agent/reply-feedback', '/agent/branch'):
                import secrets
                if not secrets.compare_digest(form.get('token',''),demo.permissions_token):
                    return self._json(403,{'error':'无效的表单授权'})
                from .conversation_actions import feedback, branch
                try:
                    with demo._lock:
                        result=(branch if path=='/agent/branch' else feedback)(demo,form)
                    return self._json(200,result)
                except (ValueError, OSError, RuntimeError) as exc:
                    return self._json(409,{'error':str(exc)})
            if path == '/agent/continue':
                import secrets
                if not secrets.compare_digest(form.get('token', ''), demo.permissions_token):
                    return self._json(403, {'error': '无效的表单授权'})
                from .quick_resume import resume
                try:
                    sid = resume(demo, form.get('session', ''))
                    return self._json(200, {'session': sid})
                except (ValueError, RuntimeError, OSError) as exc:
                    return self._json(409, {'error': str(exc)})
            if path == '/agent/human-input':
                import secrets
                if not secrets.compare_digest(form.get('token',''), demo.permissions_token):
                    return self._json(403, {'error':'无效的表单授权'})
                from .human_input import answer
                try:
                    row = answer(form.get('session',''), form.get('id',''), form.get('answer',''))
                    sid = row['session']
                    entry = demo.live_sessions.get(sid)
                    continuing = bool(entry and entry[0].get('status') == 'running')
                    notice = '答复已保存，等待中的 Agent 将自动继续。'
                    if not entry or entry[0].get('status') != 'running':
                        if row['owner'] == sid:
                            demo.continue_agent_task('已收到问题 '+row['id']+' 的用户答复：'+form['answer']+'。问题内容：'+row['payload']+'。请根据已保存状态继续；不得重放已执行或结果未知的命令。', session_id=sid)
                            continuing = True
                        else:
                            notice = '答复已保存；子任务已中断，请在主会话继续协调。'
                    return self._json(200, {'notice':notice,'continuing':continuing})
                except (ValueError, RuntimeError, OSError) as exc:
                    return self._json(400, {'error':str(exc)})
            if path=='/agent/conversation':
                from .conversations import mutate
                try:
                    with demo._lock: result=mutate(demo,form)
                    return self._json(200,{'ok':True,'metadata':result})
                except PermissionError:return self._json(403,{'error':'无效的表单授权'})
                except (ValueError,OSError) as exc:return self._json(400,{'error':str(exc)})
            if path.startswith('/workspaces/'):
                from .pages_workspaces import mutate,browse,authorize
                from .workspaces import WorkspaceAccessError
                try:
                    if path=='/workspaces/browse':return self._json(200,browse(demo,form))
                    if path=='/workspaces/pick':
                        authorize(demo,form)
                        from .native_folder_picker import select_folders
                        return self._json(200,{'paths':select_folders()})
                    with demo._lock: identifier=mutate(demo,path,form)
                    destination=('/workspaces?project='+identifier+'&notice='+urllib.parse.quote('项目设置已保存')) if form.get('id') and path=='/workspaces/save' else '/agent?new=1&project='+identifier
                    if 'application/json' in self.headers.get('Accept',''):return self._json(200,{'project':identifier,'redirect':destination})
                    return self._redirect(destination)
                except PermissionError:return self._json(403,{'error':'无效的表单授权'})
                except (ValueError,KeyError,OSError,RuntimeError,WorkspaceAccessError) as exc:return self._json(400,{'error':str(exc)})
            if path.startswith('/memories/'):
                from .pages_memory import mutate
                try: notice=mutate(demo,path,form)
                except PermissionError: return self._send(403,b'invalid form token','text/plain')
                except (ValueError,KeyError) as exc: return self._send(400,str(exc).encode('utf-8'),'text/plain; charset=utf-8')
                return self._redirect('/memories?notice='+urllib.parse.quote(notice))
            if path == '/agent/access':
                import secrets
                from .access_modes import MODES, apply
                if not secrets.compare_digest(form.get('token', ''), demo.permissions_token):
                    return self._send(403, b'invalid form token', 'text/plain')
                mode, sid = form.get('mode'), form.get('session', '')
                if mode not in MODES:
                    return self._send(400, b'invalid mode', 'text/plain')
                with demo._lock:
                    if sid:
                        entry = demo.live_sessions.get(sid)
                        if not entry: return self._send(404, b'unknown live session', 'text/plain')
                        state, agent, _ = entry
                        if state.get('status') == 'running': agent.pending_permission_mode = mode
                        else: apply(agent, mode)
                        agent.session.append('permission/selected', mode=mode, source='host_form')
                    else:
                        demo.default_permission_mode = mode
                return self._redirect('/agent/access?session=' + urllib.parse.quote(sid))
            if path in ('/skills/import','/skills/toggle'):
                import secrets
                if not secrets.compare_digest(form.get('token',''), demo.permissions_token):
                    return self._send(403, b'invalid form token', 'text/plain')
                from .skill_import import import_skill, set_enabled
                try:
                    if path == '/skills/import': result = import_skill(form.get('path',''))
                    else:
                        if form.get('action') not in ('enable','disable'): raise ValueError('无效操作')
                        set_enabled(form.get('name',''), form['action']=='enable'); result = {'updated':True}
                except (ValueError, OSError) as exc:
                    return self._send(400, str(exc).encode('utf-8'), 'text/plain; charset=utf-8')
                return self._redirect('/skills?notice=' + urllib.parse.quote(json.dumps(result, ensure_ascii=False)))
            if path == '/approvals/decide':
                import secrets
                if not secrets.compare_digest(form.get('token', ''), demo.permissions_token):
                    return self._send(403, b'invalid form token', 'text/plain')
                from .approvals import decide
                if form.get('decision') not in ('allow','deny'):
                    return self._send(400, b'invalid decision', 'text/plain')
                decide(form.get('id',''), form['decision'] == 'allow')
                return self._redirect('/approvals')
            if path in {'/agent/run', '/agent/chat'}:
                if 'application/json' in self.headers.get('Accept',''):
                    try:
                        if path=='/agent/run':
                            sid=demo.start_agent_task(form.get('task','').strip(),_int_or(form.get('max_iters'),0),_usd_or_none(form.get('max_usd')),workspace_group=form.get('workspace_group'),attachment_ids=json.loads(form.get('attachments','[]')))
                        else:
                            sid=demo.continue_agent_task(form.get('message','').strip(),_int_or(form.get('max_iters'),0),_usd_or_none(form.get('max_usd')),session_id=form.get('session',''),attachment_ids=json.loads(form.get('attachments','[]')))
                        from .pages_agent import _sidebar
                        return self._json(200,{'session':sid, 'sidebar_html':_sidebar(demo.ws_mgr,demo.list_agent_sessions(200),demo.live_sessions[sid][0],demo.ws_mgr.summary())})
                    except Exception as exc:return self._json(400,{'error':str(exc)})
                self.path = path + '?' + urllib.parse.urlencode(form)
                return self._route_get()

            if path in {'/knowledge/import', '/knowledge/remove', '/knowledge/reindex', '/knowledge/select'}:
                import secrets
                if not secrets.compare_digest(form.get('token', ''), demo.permissions_token):
                    return self._send(403, b'invalid form token', 'text/plain')
                from .knowledge_scopes import resolve, save_selection
                from urllib.parse import urlencode
                sid = form.get('session','')
                if path == '/knowledge/select':
                    with demo._lock:
                        save_selection(demo,sid,[s for s in ('session','project','public') if form.get('use_'+s)=='on'])
                    return self._redirect('/knowledge?'+urlencode({'session':sid,'scope':form.get('scope','session')}))
                source = resolve(demo, form)
                target = '/knowledge?'+urlencode({'session':sid,'scope':source['id']})
                if path in {'/knowledge/import', '/knowledge/reindex'}:
                    job = demo.import_knowledge(form.get('path', ''), reindex=path=='/knowledge/reindex', source=source)
                    return self._redirect(target+'&job=' + job)
                from .knowledge import KnowledgeBase, database_root
                KnowledgeBase(source['root']).remove(form.get('id', ''))
                return self._redirect(target)

            if path == '/permissions/web':
                import secrets
                host = self.headers.get('Host', '')
                origin = self.headers.get('Origin', '')
                allowed_hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
                if self.client_address[0] not in {'127.0.0.1', '::1'} or host not in allowed_hosts or (origin and origin != 'http://' + host) or not secrets.compare_digest(form.get('token', ''), demo.permissions_token):
                    return self._send(403, b'permission form authorization failed', 'text/plain')
                from .web_policy import change, save_domains
                try:
                    if form.get('action'):
                        change(form['action'], form.get('value', ''))
                    else:
                        save_domains(form.get('domains', ''))
                    notice = '网页访问设置已生效'
                except ValueError as exc:
                    notice = str(exc)
                return self._redirect('/permissions?notice=' + urllib.parse.quote(notice))

            if path == "/settings/probe":
                return self._settings_probe(form)
            if path == "/settings/save":
                return self._settings_save(form)
            return self._send(404, b"not found", "text/plain")

        # ---- 设置：保存 ------------------------------------------------
        def _settings_save(self, form: dict[str, str]):
            cfg = demo.llm_cfg
            preset = form.get("preset", cfg.preset)
            if preset != cfg.preset:
                cfg.apply_preset(preset)
            cfg.preset = preset
            cfg.provider = form.get("provider", cfg.provider)
            cfg.base_url = form.get("base_url", cfg.base_url).strip()
            # 留空表示"不改动"，避免用户只改别的字段时把 key 清掉
            new_key = form.get("api_key", "").strip()
            if new_key:
                cfg.api_key = new_key
            from .llmconfig import ModelRouting

            cfg.routing = ModelRouting(
                form.get("m_small", "").strip(),
                form.get("m_mid", "").strip(),
                form.get("m_large", "").strip(),
            )
            for field_name, attr, cast in (
                ("temperature", "temperature", float),
                ("max_tokens", "max_tokens", int),
                ("context_window", "context_window", int),
                ("verification_token_budget", "verification_token_budget", int),
                ("subagent_max_depth", "subagent_max_depth", int),
                ("subagent_max_parallel", "subagent_max_parallel", int),
                ("subagent_max_tasks", "subagent_max_tasks", int),
                ("subagent_total_tokens", "subagent_total_tokens", int),
                ("subagent_default_tokens", "subagent_default_tokens", int),
                ("timeout_s", "timeout_s", float),
                ("max_parallel", "max_parallel", int),
                ("price_in_per_m", "price_in_per_m", float),
                ("price_out_per_m", "price_out_per_m", float),
            ):
                v = form.get(field_name)
                if v not in (None, ""):
                    try:
                        setattr(cfg, attr, cast(v))
                    except ValueError:
                        pass
            cfg.offline_mock_fallback = form.get("offline_mock_fallback") == "1"
            cfg.memory_enabled = form.get('memory_enabled') == '1'
            for name,allowed in (('review_profile',('strict','balanced')),('delegation_policy',('manual','adaptive'))):
                value=form.get(name,getattr(cfg,name))
                if value not in allowed:return self._send(400,b'invalid runtime policy','text/plain')
                setattr(cfg,name,value)
            if 'delegation_evidence_path' in form:cfg.delegation_evidence_path=form['delegation_evidence_path'].strip()
            for name,low,high in (('subagent_max_depth',1,8),('subagent_max_parallel',1,32),('subagent_max_tasks',1,128)):
                if not low<=getattr(cfg,name)<=high:
                    return self._send(400,f'{name} must be {low}..{high}'.encode(),'text/plain')
            if min(cfg.subagent_total_tokens,cfg.subagent_default_tokens,cfg.verification_token_budget)<0:
                return self._send(400,b'budgets must be nonnegative','text/plain')
            if not cfg.routing.mid and cfg.model:
                cfg.routing = ModelRouting(cfg.model, cfg.model, cfg.model)

            msg = demo.switch_backend(cfg)
            return self._redirect(f"/settings?notice={urllib.parse.quote(msg)}")

        # ---- 设置：测试连接 --------------------------------------------
        def _settings_probe(self, form: dict[str, str]):
            from .llm import OpenAIChatClient

            from dataclasses import replace
            probe_cfg = replace(demo.llm_cfg)
            if form.get("base_url"):
                probe_cfg.base_url = form["base_url"].strip()
            if form.get("api_key", "").strip():
                probe_cfg.api_key = form["api_key"].strip()
            if form.get("m_mid", "").strip():
                probe_cfg.routing.mid = form["m_mid"].strip()
            if form.get("m_small", "").strip():
                probe_cfg.routing.small = form["m_small"].strip()
            model = probe_cfg.model_or("mid")
            if not probe_cfg.base_url or not model:
                result = {"ok": False, "code": "CONFIG",
                          "error": "还没填 Base URL 或模型名",
                          "hint": "选一个厂商预设会自动填好，或手工填 OpenAI 兼容端点。",
                          "url": probe_cfg.chat_url()}
            else:
                result = OpenAIChatClient(probe_cfg).probe(model)
            return self._send(200, pages.settings(demo, {}, probe=result,
                                                  notice="测试连接不会保存配置"))

        # ---- 小工具 ----------------------------------------------------
        def _redirect(self, to: str):
            self.send_response(303)
            self.send_header("Location", to)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _admin(self, msg: str, title: str, back: str):
            clean = msg.replace("上游已劣化：", "").replace("false", "否").replace("True", "是")
            body = (f"<h1>{html.escape(title)}</h1>"
                    f"<div class=card>{html.escape(clean)}</div>"
                    f"<p><a href='{back}'>← 返回</a> · <a href='/'>对话工作台</a></p>")
            return self._send(200, ui.page(title, "resilience", body))

        # ---- /ask ------------------------------------------------------
        def _ask(self, qs: dict[str, str]):
            q = qs.get("q", "").strip() or "缓存穿透怎么治理"
            tenant = qs.get("tenant", "alpha")
            user = qs.get("user", "u1")
            session = qs.get("session", "s1")
            if tenant not in demo.tenants:
                tenant = "alpha"

            if qs.get("stream") == "1":
                return self._ask_stream(q, tenant, user, session)

            resp = demo.handle_ask(
                q, tenant, user, session,
                needs_tools=bool(qs.get("tools")),
                use_retrieval=bool(qs.get("rag")),
                persona=qs.get("persona", "general"),
            )
            if qs.get("format") == "json" or "application/json" in (self.headers.get("Accept") or ""):
                return self._json(resp.http_status or 200, {
                    "ok": resp.ok, "answer": resp.answer, "error": resp.error,
                    "model": resp.model, "cached": resp.cached,
                    "cache_layer": resp.cache_layer,
                    "latency_ms": round(resp.latency_ms, 1),
                    "usd": round(resp.usd, 6),
                    "tokens": {"in": resp.tokens_in, "out": resp.tokens_out},
                    "context_tokens": resp.context_tokens,
                    "tool_calls": resp.tool_calls, "retries": resp.retries,
                    "degraded": resp.degraded, "trace_id": resp.trace_id,
                    "spans": [{"span": n, "ms": ms, "status": st, "attrs": a}
                              for n, ms, st, a in resp.spans],
                })
            return self._send(200, pages.console(demo, qs, resp))

        def _ask_stream(self, q: str, tenant: str, user: str, session: str):
            """SSE：演示 TTFT 与端到端 RT 是两个指标。"""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            t0 = time.perf_counter()

            def ev(name: str, data: dict) -> None:
                payload = f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                try:
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            ev("start", {"query": q, "tenant": tenant,
                         "note": "先推阶段事件，再逐块推答案 —— TTFT 与端到端 RT 分开看"})
            resp = demo.handle_ask(q, tenant, user, session)
            for n, ms, st, _a in resp.spans:
                ev("span", {"name": n, "ms": ms, "status": st})
            ttft_ms = (time.perf_counter() - t0) * 1000.0
            ev("ttft", {"ttft_ms": round(ttft_ms, 1)})
            text = resp.answer if resp.ok else f"[{resp.error}]"
            step = max(1, len(text) // 12)
            for i in range(0, len(text), step):
                ev("chunk", {"text": text[i:i + step]})
                time.sleep(0.03)
            ev("done", {
                "e2e_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                "ttft_ms": round(ttft_ms, 1),
                "cached": resp.cached, "model": resp.model,
                "usd": round(resp.usd, 6), "trace_id": resp.trace_id,
                "note": "注意 TTFT 远小于端到端 RT —— 用户感知的是前者",
            })

    return Handler


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="Agent 平台可视化服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--seed-cache", action="store_true", help="预热缓存，立刻能看到命中")
    ap.add_argument("--degrade", action="store_true", help="启动即让上游抖动（仅模拟器）")
    ap.add_argument("--corpus", type=int, default=None, help="知识库规模")
    ap.add_argument("--real", action="store_true", help="强制使用真实 LLM（需已配置 key）")
    ap.add_argument("--mock", action="store_true", help="强制使用内置模拟器")
    # ---- 成本护栏（真实 key 下这是安全机制）----
    ap.add_argument("--max-usd", type=float, default=None,
                    help="本进程累计花费上限（美元），触达后拒绝新调用。"
                         "不传 = 用配置文件的默认值")
    ap.add_argument("--max-calls", type=int, default=None,
                    help="本进程最大模型调用次数，防循环烧钱。"
                         "不传 = 用配置文件的默认值")
    ap.add_argument("--unlimited", action="store_true",
                    help="**关掉成本护栏**（本进程不设任何花费/调用数上限）。"
                         "编码 agent 的 /agent 任务默认就已经不设限，"
                         "这个开关影响的是 /ask 那类请求")
    ap.add_argument("--dry-run", action="store_true",
                    help="干跑：不发真实请求，只看调用量与链路行为")
    args = ap.parse_args(argv)

    cfg = PlatformConfig.from_env()
    if args.corpus:
        cfg.corpus_size = args.corpus

    from .guard import CostGuard

    guard = CostGuard(
        max_usd=(None if args.unlimited else
                 (args.max_usd if args.max_usd is not None else cfg.max_usd_per_run)),
        max_calls=(None if args.unlimited else
                   (args.max_calls if args.max_calls is not None
                    else cfg.max_llm_calls_per_run)),
        max_tokens_per_request=cfg.max_tokens_per_request,
        dry_run=args.dry_run or cfg.dry_run,
    )

    llm_cfg = LLMConfig.load()
    from .model_capacity import discover
    from .billing import refresh
    discover(llm_cfg)
    refresh(llm_cfg)
    if args.mock:
        llm_cfg.provider = "mock"
    elif args.real:
        llm_cfg.provider = "real"
    if llm_cfg.provider == "real" and not (llm_cfg.base_url and llm_cfg.model_or("mid")):
        print("！已选择真实 LLM，但 base_url / 模型名没配全 —— 先按模拟器启动，"
              "然后到 /settings 页填写。")

    print("正在初始化 Agent 平台（预热前缀缓存 / 检索索引 / 连接池）…")
    if cfg.corpus_size >= 50000:
        print(f"  知识库规模 {cfg.corpus_size:,}，首次构建需要几秒…")
    demo = DemoServer(cfg, llm_cfg, seed_cache=args.seed_cache, degrade=args.degrade,
                      guard=guard)

    live, lmsg = demo.platform.liveness()
    ready, rmsg = demo.platform.readiness()
    print(f"  liveness  = {live}  ({lmsg})")
    print(f"  readiness = {ready}  ({rmsg})")
    backend = "真实 LLM" if demo.is_real else "内置模拟器"
    target = demo.llm_cfg.chat_url() if demo.is_real else "本地模拟，不联网"
    print(f"  后端      = {backend}  ({target})")
    if demo.is_real:
        mode = "干跑（不发真实请求）" if guard.dry_run else "真实调用"
        # ⚠ 上限现在是可选的（None = 不设限），格式化时必须分支。
        # 写成 `${guard.max_usd:.4f}` 在 None 上会崩：
        # `TypeError: unsupported format string passed to NoneType.__format__`
        # —— 而且崩在**启动阶段**，服务根本起不来。
        # 实测就是这么踩的：改完护栏语义忘了改这行打印。
        # 教训：**把某个字段改成 Optional 时，要搜一遍所有格式化它的地方。**
        usd_cap = "不设限" if guard.max_usd is None else f"${guard.max_usd:.4f}"
        call_cap = "不设限" if guard.max_calls is None else f"{guard.max_calls} 次"
        print(f"  成本护栏  = {mode}  花费 {usd_cap} / 调用数 {call_cap}")
        if not guard.dry_run:
            print("              触达上限会自动拦截并报错 —— 这是保护，不是故障。")

    from .desktop_login import existing_token
    credential_path = Path(__file__).resolve().parents[1] / '.agent-runtime' / 'desktop-access.json'
    demo.permissions_token = existing_token(credential_path, args.port, demo.permissions_token)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(demo))
    access_path = Path(__file__).resolve().parents[1] / '.agent-runtime' / 'desktop-access.json'
    access_path.parent.mkdir(parents=True, exist_ok=True)
    access_url = f'http://127.0.0.1:{args.port}/auth?token={demo.permissions_token}'
    access_temp = access_path.with_suffix('.tmp')
    access_temp.write_text(json.dumps({'url': access_url, 'token': demo.permissions_token, 'port': args.port}), encoding='utf-8')
    access_temp.replace(access_path)
    print('  本机登录入口：python -m agentplat.knowledge_cli open（访问凭据不写入日志）')
    print(f"\n  面板已就绪 →  http://{args.host}:{args.port}/")
    print("  标签页：对话工作台 / 模型与路由 / 缓存 / 熔断与限流 / 请求历史 / "
          "会话与隔离 / 指标与成本 / LLM 设置")
    print("  按 Ctrl+C 停止（或访问 /admin/drain 观察优雅停机）\n")
    from .recovery import recover_server
    threading.Thread(target=recover_server, args=(demo,), daemon=True, name='crash-recovery').start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，执行优雅停机…")
        rep = demo.platform.request_shutdown()
        print(f"  排空 {rep['drain_ms']:.0f}ms，掐断 {rep['interrupted']} 个请求")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
