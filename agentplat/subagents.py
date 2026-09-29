"""真实 CodingAgent 子任务调度：有界并发、消息、取消、预算及持久化。

独立上下文默认只读；写入任务使用隔离副本，父级显式合并并重新验证。
重启后不自动重放未知副作用，运行中记录转换为 interrupted。
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
from pathlib import Path
import queue
import threading
import time
import uuid

from .runtime import BudgetPool, CapabilityPolicy


TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted', 'budget_exceeded', 'blocked'}


class TokenBudgetExceeded(RuntimeError):
    pass


class AgentManager:
    def __init__(self, cfg, workspace, directory, *, max_workers=3, max_queue=12,
                 total_tokens=None, factory=None, parent_cancel=None, providers=None):
        self.cfg, self.workspace = cfg, workspace
        from .subagent_providers import default_registry
        self.providers=providers or default_registry()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=max_queue, thread_name_prefix='subagent')
        self.model_slots = threading.BoundedSemaphore(max_workers)
        self.max_depth = max(1, int(getattr(cfg, 'subagent_max_depth', 2)))
        self.max_queue = max_queue
        self.budget = BudgetPool(total_tokens)
        self.factory = factory
        self.parent_cancel = parent_cancel
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.tasks = {}
        self.closed = False
        from .team_coordination import Coordination
        self.coordination = Coordination(self.directory)
        for path in self.directory.glob('*.json'):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if data['status'] not in TERMINAL:
                    data['status'] = 'interrupted'
                    data['error'] = '宿主已重启；请检查产物，未自动重放任务'
                self.tasks[data['agent_id']] = dict(data=data, cancel=threading.Event(),
                                                   inbox=queue.Queue(), agent=None, branch=None)
                self.coordination.stop_owner(data['agent_id'], data['status'])
                self.budget.spent += data.get('budget_charged_tokens', data.get('used_tokens', 0)) + data.get('budget_pending_tokens',0)
            except (ValueError, KeyError):
                continue

    def _save(self, task):
        data = task['data']
        data['revision'] = data.get('revision', 0) + 1
        path = self.directory / (data['agent_id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
        self.changed.notify_all()

    def spawn(self, task: str, *, context='', token_budget=None, mode='readonly', depends_on=None, acceptance='', purpose='work', parent_id=None, source_paths=None, source_agent=None, provider='local', category='general'):
        if not task.strip() or mode not in ('readonly', 'isolated'):
            raise ValueError('任务不能为空；mode 必须为 readonly 或 isolated')
        with self.lock:
            self.providers.get(provider)
            decision=None
            if purpose=='work' and getattr(self.cfg,'delegation_policy','manual')=='adaptive':
                from .delegation import decide,load_evidence
                decision=decide(task,acceptance,context,parent_task=self.tasks[parent_id]['data']['task'] if parent_id else '',
                    evidence=load_evidence(getattr(self.cfg,'delegation_evidence_path','')),category=category,model=self.cfg.model_or('mid') or self.cfg.model)
                if decision['action']!='delegate':raise RuntimeError('建议主 Agent 直接完成：'+decision['reason'])
            if source_agent and (purpose!='verification' or source_agent not in self.tasks or self.tasks[source_agent]['data']['status']!='completed' or not self.tasks[source_agent].get('agent')):
                raise ValueError('验收来源必须是已完成且仍有副本的当前任务')
            parent = self.tasks[parent_id] if parent_id else None
            depth = parent['data'].get('depth', 1) + 1 if parent else 1
            if depth > self.max_depth:
                raise RuntimeError('已达到用户配置的子任务委派深度')
            if parent and (parent['cancel'].is_set() or parent['data']['status'] in TERMINAL):
                raise RuntimeError('父任务已结束或取消')
            if parent and parent['data']['mode'] == 'readonly' and mode != 'readonly':
                raise RuntimeError('只读子任务不能派生写入任务')
            if token_budget is None:
                token_budget = max(0, int(getattr(self.cfg, 'subagent_default_tokens', 0)))
            depends_on = list(dict.fromkeys(depends_on or []))
            if any(key not in self.tasks for key in depends_on):
                raise ValueError('依赖必须是已经创建的子任务 ID；禁止未知依赖与循环依赖')
            if self.closed:
                raise RuntimeError('调度器已关闭')
            if self.parent_cancel is not None and self.parent_cancel.is_set():
                raise RuntimeError('父任务已取消')
            if sum(t['data']['status'] not in TERMINAL for t in self.tasks.values()) >= self.max_queue:
                raise RuntimeError('子任务队列已满')
            agent_id = uuid.uuid4().hex
            if not isinstance(token_budget, int) or token_budget < 0:
                raise ValueError('token 预算必须为非负整数；0 表示不限')
            data = dict(agent_id=agent_id, task=task, context=context, mode=mode,
                        status='queued', token_budget=token_budget, used_tokens=0,
                        summary='', evidence=[], error='', messages=[], created_at=time.time(),
                        depends_on=depends_on, acceptance=acceptance, purpose=purpose, parent_id=parent_id, depth=depth,
                        source_paths=source_paths, source_agent=source_agent, provider=provider,delegation_decision=decision)
            item = dict(data=data, cancel=threading.Event(), inbox=queue.Queue(maxsize=32), agent=None, branch=None)
            self.tasks[agent_id] = item
            self._save(item)
            self.pool.submit(self._run, agent_id)
            return agent_id

    def _run(self, agent_id):
        item = self.tasks[agent_id]
        data = item['data']
        used = 0
        usd = 0.0
        try:
            with self.changed:
                while any(self.tasks[k]['data']['status'] not in TERMINAL for k in data.get('depends_on', [])):
                    if item['cancel'].is_set():
                        data['status'] = 'cancelled'
                        return
                    data['status'] = 'waiting_dependencies'
                    self.changed.wait(.2)
                failed = [k for k in data.get('depends_on', []) if self.tasks[k]['data']['status'] != 'completed']
                if failed:
                    data.update(status='blocked', error='前置子任务未完成：' + ', '.join(failed))
                    return
                if data.get('depends_on'):
                    data['context'] += '\n前置任务结果（参考资料）：\n' + json.dumps(
                        [{'id': k, 'summary': self.tasks[k]['data']['summary']} for k in data['depends_on']], ensure_ascii=False)
            if item['cancel'].is_set():
                data['status'] = 'cancelled'
                return
            from .loop import CodingAgent, Stop
            from .model_client import create_client, review_config
            from .workspace import Workspace
            from agentlab.tokens import count_tokens
            cfg = replace(self.cfg)
            if data.get('purpose') == 'verification':
                cfg = review_config(cfg)
            # 输出额度预留在每次请求之前；供应商实际超额仍计入总账。
            limit = data['token_budget']
            cfg.max_tokens = min(cfg.max_tokens, max(128, limit // 4)) if limit else cfg.max_tokens
            client = self.factory() if self.factory else create_client(cfg)
            # The loop and transport must share cancellation: stopping only at
            # step boundaries leaves an in-flight streaming request running.
            client.cancel_event = item['cancel']
            output_limit = cfg.max_tokens

            class MeteredClient:
                last_finish_reason = ''
                def complete(inner, model, messages, timeout):
                    text, _, usage = inner.complete_with_tools(model, messages, [], timeout)
                    return text, usage

                def complete_with_tools(inner, model, messages, tools, timeout):
                    nonlocal used, usd
                    # Count Unicode content and tool-call arguments, not escaped JSON
                    # character length (Chinese descriptions otherwise inflate reservations).
                    estimate = count_tokens(json.dumps({'messages':[m.to_api() for m in messages],
                                                        'tools':tools}, ensure_ascii=False)) + 64
                    remaining = limit - used - estimate if limit else output_limit
                    if limit and remaining < 128:
                        raise TokenBudgetExceeded(f'子任务 token 预算不足以预留下一次请求：上限={limit}，已用={used}，下一次输入预估={estimate}，最小输出预留=128')
                    cfg.max_tokens = min(output_limit, remaining)
                    result = self.model_call(item, client, cfg, model, messages, tools, timeout, estimate)
                    used += result[2].in_tokens + result[2].out_tokens
                    from .billing import record
                    bill = record(cfg, result[2], model=model, client=client)
                    usd += bill['usd']
                    with self.lock:
                        data.update(used_tokens=used, usd=usd, usage_estimated=getattr(client,'last_usage_estimated',True))
                        for field in ('in_tokens','out_tokens','cached_tokens','usd_min'):
                            data[field] = data.get(field,0) + bill[field]
                        self._save(item)
                    inner.last_usage_estimated = getattr(client,'last_usage_estimated',True)
                    inner.last_request_started_at = getattr(client,'last_request_started_at',None)
                    inner.last_finish_reason = getattr(client, 'last_finish_reason', '')
                    return result

            parent_ws = self.tasks[data['parent_id']]['agent'].ws if data.get('parent_id') else self.workspace
            if data.get('source_agent'):parent_ws=self.tasks[data['source_agent']]['agent'].ws
            root = parent_ws.scope
            if data['mode'] == 'isolated':
                from .isolation import IsolatedChanges
                item['branch'] = IsolatedChanges(root, self.directory / agent_id / 'isolated')
                root = item['branch'].root
                data['isolation_root'] = {k:str(v) for k,v in root.items()} if isinstance(root,dict) else str(root)
                data['isolation_base'] = item['branch'].base
                if data.get('purpose') == 'verification':
                    from .review_evidence import snapshot
                    data['source_snapshot'] = snapshot(parent_ws.scope, root, data.get('source_paths'))
            ws = Workspace(root, allow_shell=data['mode'] == 'isolated' and parent_ws.allow_shell)
            # 副本只隔离编辑冲突；执行后端与权限继承父任务。
            ws.execution_mode = parent_ws.execution_mode
            ws.native_network = parent_ws.native_network
            ws.knowledge_root = parent_ws.knowledge_root
            ws.human_session = getattr(parent_ws, 'human_session', '')
            if data.get('purpose') == 'verification':
                from .knowledge import snapshot
                ws.knowledge_root = self.directory / agent_id / 'knowledge-snapshot'
                data['knowledge_snapshot'] = snapshot(parent_ws.knowledge_root, ws.knowledge_root)
            ws.memory_workspace = getattr(parent_ws, 'memory_workspace', parent_ws.root)
            data['execution_mode'] = ws.execution_mode
            data['workspace_isolation'] = 'copy' if data['mode'] == 'isolated' else 'readonly'
            ws.cancel_event = item['cancel']
            def stopping(ctx):
                if self.parent_cancel is not None and self.parent_cancel.is_set():
                    item['cancel'].set()
                if limit and used >= limit:
                    return Stop('子任务 token 预算已耗尽')
                return None
            def steering():
                return self.deliver(agent_id)
            agent = self.providers.get(data.get('provider','local')).create(MeteredClient(), cfg, workspace=ws,
                                session_dir=self.directory / agent_id, stop_flag=item['cancel'],
                                policy=stopping, compaction_enabled=True, enable_subagents=False,
                                steering=steering)
            if not isinstance(agent,CodingAgent):raise TypeError('Provider must preserve CodingAgent host contracts')
            agent.session.append('provider/selected',name=data.get('provider','local'))
            if getattr(parent_ws, 'general_chat', False) and data.get('purpose') == 'verification':
                from .document_assertions import install as install_assertions
                install_assertions(agent)
            agent.run_deadline = getattr(self, 'run_deadline', None)
            from .runtime import effective_policy
            agent.authority_provider = (lambda: effective_policy(self.tasks[data['parent_id']]['agent'])) if data.get('parent_id') else getattr(self, 'authority_provider', lambda:CapabilityPolicy())
            from .attachments import bind,metadata
            bind(agent,[metadata(i) for i in getattr(parent_ws,'attachment_source',lambda:set())()])
            # Auxiliary clients must not bypass the shared child token budget.
            for tool in ('inspect_image', 'expanded_search_knowledge'):
                agent.tools.pop(tool, None)
            readonly = frozenset({'request_user_input','list_dir', 'read_file', 'read_chunk', 'grep', 'finish', 'search_knowledge', 'read_knowledge_chunk', 'list_knowledge', 'list_skills', 'read_skill', 'read_skill_file','search_memory','list_attachments','read_attachment','search_attachment'})
            if data['mode'] == 'readonly':
                agent.tools = {k: v for k, v in agent.tools.items() if k in readonly}
                agent.capabilities = CapabilityPolicy(readonly, False, False, False)
            if data.get('purpose') == 'verification':
                agent.verification_task = True
                from .browser_tools import install as install_browser
                install_browser(agent)
                verification_tools = frozenset({'check_file_text','list_dir','read_file','grep','write_file','run_shell','finish','list_knowledge','search_knowledge','read_knowledge_chunk','list_attachments','read_attachment','search_attachment','browser_preview','browser_snapshot','browser_click','browser_fill','browser_check','browser_screenshot'})
                agent.tools = {k:v for k,v in agent.tools.items() if k in verification_tools}
                from .verification_tools import install
                install(agent, self, agent_id)
                agent.capabilities = CapabilityPolicy(frozenset(agent.tools),True,ws.allow_shell,False)
                # Verdict is checked structurally by the parent. Lexical checklist
                # matching would reject an honest negative verification report.

            if data.get('purpose') != 'verification':
                from .team_tools import install
                install(agent, self, agent_id)
            item['agent'] = agent
            with self.lock:
                data['status'] = 'running'
                self._save(item)
            result = agent.run(data['task'] + ('\n验收条件：' + data['acceptance'] if data.get('acceptance') else ''), context=data['context'])
            while True:
                with self.lock:
                    # Enqueue and terminal transition share the lock: no accepted message is lost.
                    if not item['cancel'].is_set() and self.coordination.pending(agent_id):
                        message = self.deliver(agent_id, limit=1)[0]
                    else:
                        data.update(status='cancelled' if item['cancel'].is_set() else
                                    'completed' if result.ok else 'budget_exceeded' if (limit and used >= limit) or '预算' in result.error else 'failed',
                                    summary=result.summary, error=result.error,
                                    evidence=[e.data for e in agent.session.of_kind('verification/evidence')],
                                    knowledge_reads=[e.data for e in agent.session.of_kind('knowledge/source_read')],
                                    session_path=str(agent.session.path))
                        if item['branch']:
                            data['changes'] = item['branch'].manifest()
                            if data.get('purpose') == 'verification':
                                from .review_evidence import intact
                                data['source_snapshot_intact'] = intact(root, data.get('source_snapshot', []))
                        break
                result = agent.continue_with(message)
        except Exception as exc:
            with self.lock:
                data['status'] = 'cancelled' if item['cancel'].is_set() else 'budget_exceeded' if isinstance(exc, TokenBudgetExceeded) else 'failed'
                data['error'] = f'{type(exc).__name__}: {exc}'
        finally:
            with self.lock:
                data['used_tokens'] = used
                data['usd'] = usd
                data['budget_overrun'] = max(0, used-data['token_budget']) if data['token_budget'] else 0
                self._save(item)
                self.coordination.stop_owner(agent_id, data['status'])
                if data.get('purpose') != 'verification':
                    self.coordination.send('system', data.get('parent_id') or 'root',
                        json.dumps({'agent_id':agent_id, 'status':data['status'], 'summary':data.get('summary','')[:2500],
                                    'error':data.get('error','')[:1000]}, ensure_ascii=False),
                        kind='task_finished', dedup='finished:'+agent_id)

    def model_call(self, item, client, cfg, model, messages, tools, timeout, estimate):
        while not self.model_slots.acquire(timeout=.1):
            if item['cancel'].is_set() or (self.parent_cancel and self.parent_cancel.is_set()):
                raise InterruptedError('子任务请求已取消')
        reservation = uuid.uuid4().hex
        reserved = False
        charged = 0
        try:
            if item['cancel'].is_set():
                raise InterruptedError('子任务请求已取消')
            with self.budget.lock:
                available = (self.budget.total-self.budget.spent-sum(self.budget.reservations.values())
                             if self.budget.total is not None else estimate+cfg.max_tokens)
                if available < estimate+128:
                    raise TokenBudgetExceeded('子任务共享总预算不足以预留下一次请求')
                cfg.max_tokens = min(cfg.max_tokens, available-estimate)
                charged = estimate+cfg.max_tokens
                self.budget.reservations[reservation] = charged
                reserved = True
            with self.lock:
                item['data']['budget_pending_tokens'] = charged
                item['data']['request_started_at'] = time.time()
                self._save(item)
            result = client.complete_with_tools(model, messages, tools, timeout)
            charged = result[2].in_tokens+result[2].out_tokens
            return result
        finally:
            if reserved:
                self.budget.settle(reservation, charged)
                with self.lock:
                    item['data']['budget_charged_tokens'] = item['data'].get('budget_charged_tokens',0)+charged
                    item['data']['budget_pending_tokens'] = 0
                    item['data']['request_started_at'] = None
                    item['data']['last_request_finished_at'] = time.time()
                    self._save(item)
            self.model_slots.release()

    def team_list(self):
        with self.lock:
            return [{'agent_id':k, **{f:v['data'].get(f) for f in
                    ('parent_id','depth','task','status','mode','used_tokens')}}
                    for k,v in self.tasks.items() if v['data'].get('purpose') != 'verification']

    def team_state(self, key='', value=None, expected_revision=0, author='root'):
        with self.lock:
            path = self.directory/'team-state.json'
            state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            if value is None:
                return state.get(key, {}) if key else state
            if not key or len(key)>100 or len(value)>4000 or len(state)>100 and key not in state:
                raise ValueError('共享状态超出大小限制')
            current = state.get(key, {}).get('revision',0)
            if current != expected_revision:
                raise RuntimeError('共享状态版本冲突，请重新读取后合并')
            state[key] = dict(value=value, author=author, revision=current+1, updated_at=time.time())
            temp=path.with_suffix('.tmp');temp.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8');temp.replace(path)
            return state[key]

    def get(self, agent_id):
        with self.lock:
            data=json.loads(json.dumps(self.tasks[agent_id]['data']))
            started=data.get('request_started_at')
            data['request_elapsed_seconds']=round(max(0,time.time()-started),1) if started and data['status'] not in TERMINAL else 0
            return data

    def retry(self, agent_id, instruction=''):
        old = self.get(agent_id)
        if old['status'] not in TERMINAL - {'completed'}:
            raise ValueError('只能显式重试已失败、取消、中断或阻塞的任务')
        return self.spawn(old['task'], context=old['context'] + '\n上次失败：' + old.get('error', '') + '\n' + instruction,
                          mode=old['mode'], token_budget=old['token_budget'], acceptance=old.get('acceptance', ''),
                          purpose=old.get('purpose', 'work'), parent_id=old.get('parent_id'))

    def review(self, agent_id):
        import difflib
        with self.lock:
            item = self.tasks[agent_id]
            branch = item['branch']
            if not branch: return {'changes': item['data'].get('changes', []), 'diff': '', 'available': False}
            diffs = []
            for change in branch.manifest():
                relative = change['path']
                from .isolation import scoped_path
                old, new = scoped_path(branch.parent, relative), scoped_path(branch.root, relative)
                before = old.read_text(encoding='utf-8', errors='replace').splitlines() if old.is_file() else []
                after = new.read_text(encoding='utf-8', errors='replace').splitlines() if new.is_file() else []
                diffs.extend(difflib.unified_diff(before, after, fromfile='parent/' + relative, tofile='child/' + relative, lineterm=''))
                if sum(map(len, diffs)) > 50000: break
            return {'changes': branch.manifest(), 'diff': '\n'.join(diffs)[:50000], 'available': True, 'verification_required': True}

    def deliver(self, owner='root', limit=16):
        with self.lock:
            messages = self.coordination.drain(owner, limit)
            if owner in self.tasks and messages:
                item = self.tasks[owner]
                item['data']['delivered_messages'] = item['data'].get('delivered_messages',0) + len(messages)
                self._save(item)
            return messages

    def send(self, agent_id, message, *, sender='root', reply_to='', dedup_key=None):
        with self.lock:
            if agent_id != 'root':
                item = self.tasks[agent_id]
                if item['data']['status'] in TERMINAL or item['data']['status'] == 'cancelling':
                    raise RuntimeError('子任务已结束或正在取消；请创建后续任务')
                if sender != 'root' and item['data'].get('purpose') == 'verification':
                    raise ValueError('不能给独立验收任务发送团队消息')
            result = self.coordination.send(sender, agent_id, message, reply_to=reply_to, dedup=dedup_key)
            if agent_id != 'root' and not result.get('duplicate'):
                item['data']['messages'].append(self.coordination.format(result))
                self._save(item)
            self.changed.notify_all()
            return {'queued':result['delivered'] is None, 'message_id':result['id'],
                    'duplicate':result.get('duplicate',False), 'delivery':'下一安全步骤边界；不等于已处理'}

    def team_task(self, author='root', **kwargs):
        with self.lock:
            target = kwargs.get('target')
            if target and target != 'root':
                item = self.tasks.get(target)
                if not item or item['data']['status'] in TERMINAL or item['data'].get('purpose') == 'verification':
                    raise ValueError('接手成员必须是仍在运行的普通团队成员')
            result = self.coordination.job(author, **kwargs)
            if kwargs.get('action') == 'handoff':
                self.coordination.send('system', target,
                    json.dumps(result, ensure_ascii=False), kind='task_assigned',
                    dedup='handoff:'+result['id']+':'+str(result['revision']))
            return result

    def followup(self, agent_id, instruction):
        with self.lock:
            old = self.get(agent_id)
            if old.get('purpose') == 'verification': raise ValueError('独立验收请使用重新验收工具')
            if old['status'] not in TERMINAL: raise ValueError('运行中的任务请发送消息')
            if not instruction.strip(): raise ValueError('请提供后续任务要求')
            key = self.spawn(instruction, context='前次结果仅供参考，需重新检查当前文件。\n'+
                             json.dumps({'previous_agent':agent_id,'task':old['task'],'summary':old['summary'],
                                         'error':old['error']}, ensure_ascii=False),
                             mode=old['mode'], token_budget=old['token_budget'], parent_id=old.get('parent_id'))
            self.tasks[key]['data']['continued_from'] = agent_id
            self._save(self.tasks[key])
            return {'agent_id':key, 'continued_from':agent_id, 'fresh_workspace':True}

    def cancel(self, agent_id):
        with self.lock:
            item = self.tasks[agent_id]
            for key, child in list(self.tasks.items()):
                if child['data'].get('parent_id') == agent_id:
                    self.cancel(key)
            if item['data']['status'] not in TERMINAL:
                item['cancel'].set()
                item['data']['status'] = 'cancelling'
                if item['agent']:
                    item['agent'].ws.cancel_event.set()
                self._save(item)
            return self.get(agent_id)

    def wait(self, agent_id, timeout_s=1, after_revision=-1):
        with self.changed:
            self.changed.wait_for(lambda: self.tasks[agent_id]['data']['revision'] > after_revision
                                  or self.tasks[agent_id]['data']['status'] in TERMINAL,
                                  timeout=max(0, min(timeout_s, 60)))
            return self.get(agent_id)

    def wait_for_model(self, agent_id, timeout_s=30, after_revision=-1, owner='root'):
        """Wait for completion/mail, never wake the model for billing revisions."""
        started = time.monotonic()
        with self.changed:
            initial = self.tasks[agent_id]['data']['revision']
            self.changed.wait_for(
                lambda: self.tasks[agent_id]['data']['status'] in TERMINAL
                or self.coordination.pending(owner)
                or self.tasks[agent_id]['cancel'].is_set()
                or (self.parent_cancel is not None and self.parent_cancel.is_set()),
                timeout=max(0,min(float(timeout_s),60)))
            data = self.tasks[agent_id]['data']
            return {**{k:data.get(k) for k in ('agent_id','status','revision','used_tokens')},
                    'summary':data.get('summary','')[:4000], 'error':data.get('error','')[:1500],
                    'evidence_count':len(data.get('evidence',[])),
                    'actual_wait_seconds':round(time.monotonic()-started,3),
                    'task_elapsed_seconds':round(max(0,time.time()-data['created_at']),3),
                    'changed':data['revision'] != initial,
                    'note':'耗时以 actual_wait_seconds 为准，timeout_s 只是最长等待时间；账目变化不会唤醒等待。'}

    def apply(self, agent_id):
        with self.lock:
            item = self.tasks[agent_id]
            if item['branch'] is None and item['data'].get('isolation_root'):
                from .isolation import IsolatedChanges
                saved = item['data']['isolation_root']
                root = {k:Path(v).resolve() for k,v in saved.items()} if isinstance(saved,dict) else Path(saved).resolve()
                for folder in (root.values() if isinstance(root,dict) else [root]):
                    folder.relative_to((self.directory / agent_id).resolve())
                branch = IsolatedChanges.__new__(IsolatedChanges)
                if item['data'].get('parent_id'):
                    raise RuntimeError('重启后的嵌套副本需人工检查后重新创建任务')
                branch.parent, branch.root = self.workspace.scope, root
                branch.base = item['data']['isolation_base']
                branch.applied = bool(item['data'].get('applied'))
                branch.temporary = None
                item['branch'] = branch
            if item['data']['status'] != 'completed' or not item['branch']:
                raise RuntimeError('只有当前宿主中完成的隔离任务可以合并')
            paths = item['branch'].apply()
            item['data']['applied'] = paths
            self._save(item)
            return {'applied': paths, 'verification_required': True}

    def close(self):
        self.closed = True
        if hasattr(self,'planner'):self.planner.closed=True
        for agent_id in list(self.tasks):
            self.cancel(agent_id)
        self.pool.shutdown(wait=False)
