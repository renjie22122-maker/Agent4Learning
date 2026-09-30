"""Independent verifier context and workspace; author claims are not evidence."""
import json
import time
from .runtime import workspace_digest
from .reflection import ReflectionVerdict


def restore_record(agent):
    """Restore an identity, not approval; current files are checked on reuse."""
    record=None
    for event in agent.session.events:
        if event.kind=='independent_review/started':record=dict(event.data)
        elif event.kind=='independent_review/superseded':record=None
    if record and hasattr(agent,'child_manager'):
        try: agent.child_manager().get(record['agent_id'])
        except (KeyError,ValueError): return
        agent._independent_review=record
        agent._acceptance_task=record.get('task',getattr(agent,'_task_text',''))


def status(agent):
    record = getattr(agent, '_independent_review', None)
    if not record: return {'status':'not_started', 'note':'当前任务尚未启动独立验收'}
    state = agent.child_manager().get(record['agent_id'])
    from .review_decision import assess
    decision=assess(agent,record,state)
    return {**{k:state.get(k) for k in ('agent_id','status','verification_progress','summary','error')},
            'host_decision':decision, 'passed':decision['accepted'],
            'budget':{'limit_tokens':state.get('token_budget'), 'unlimited':state.get('token_budget')==0,'used_tokens':state.get('used_tokens')},
            'elapsed_seconds':round(max(0, time.time()-state.get('created_at',time.time())),1),
            'note':'status/summary 是验收子任务的执行状态与报告；最终是否通过以 host_decision.accepted 为准。'
                   'verification_progress 即使为 reporting、checks 写了通过，也只是过程记录，不能替代最终验收。'
                   'used_tokens 与 elapsed_seconds 是整个验收子任务的累计值，不是最后一次模型调用的消耗。'
                   'limit_tokens=0 表示单次不设限，不表示耗尽。'}


def status_question(text):
    import re
    # Exact, conservative queries only; never discard new requirements.
    labels = {'!', '！', '等待独立验收', '宿主正在等待验收结果',
              '验收通过且要求及文件未变时直接收尾，不额外调用主模型。'}
    text = '\n'.join(line for line in text.splitlines() if line.strip() not in labels)
    value = re.sub(r'[\s？?！!。,.，]', '', text)
    return value in {'谁在验收','你在等谁验收','在等谁','验收进度','验收状态','还在验收吗','宿主是谁','验收到哪了'}


def partition_steering(agent, incoming, notify=lambda:None):
    changes = []
    for message in incoming:
        if getattr(agent, '_independent_review', None) and status_question(message):
            agent.session.append('independent_review/status', question=message, result=status(agent))
            notify()
        else: changes.append(message)
    return changes


def retire(agent, reason):
    record = getattr(agent, '_independent_review', None)
    if not record: return
    if not hasattr(agent, 'child_manager'):
        agent._independent_review = None
        return
    from .subagents import TERMINAL
    manager = agent.child_manager()
    previous = manager.get(record['agent_id'])
    if previous['status'] not in TERMINAL:
        manager.cancel(record['agent_id'])
    agent._previous_review = previous
    agent.session.append('independent_review/archived', agent_id=record['agent_id'],
                         status=previous['status'], summary=previous.get('summary',''), error=previous.get('error',''))
    agent.session.append('independent_review/superseded', agent_id=record['agent_id'], reason=reason)
    agent._independent_review = None


def validate_verdict(agent, args):
    """A blocked review may finish honestly without a successful test command."""
    try:
        value = json.loads(args.get('summary', ''))
        if not isinstance(value,dict): raise ValueError()
        status = value.get('verdict')
        if status not in ('pass','fail','blocked','inconclusive'): raise ValueError()
        if not isinstance(value.get('tests'),list) or not isinstance(value.get('findings'),list): raise ValueError()
        if status in ('blocked','inconclusive'):
            if not value.get('reason'): raise ValueError()
        elif status == 'fail':
            if not value['findings']: raise ValueError()
        elif value['findings'] or not value['tests'] or not agent.evidence.valid(agent.ws.scope):
            return ReflectionVerdict(False,'pass 的 findings 必须为空数组，正面观察放在 tests；必须有当前文件上的成功命令、浏览器断言或 check_file_text 文本断言证据。文本断言不能证明程序运行正确。无法执行应返回 blocked。','验收证据',agent._finish_rejects>=2)
    except (ValueError,TypeError):
        return ReflectionVerdict(False,'finish.summary 必须是 JSON，包含 verdict=pass/fail/blocked/inconclusive、tests 数组、findings 数组；受阻须有 reason，失败须有具体 findings。','验收协议',agent._finish_rejects>=2)
    return ReflectionVerdict.ok()


def wait_pending(agent, notify=lambda:None):
    """Suspend the author while the reviewer runs; wake for user steering/cancel."""
    record = getattr(agent, '_independent_review', None)
    if not record: return []
    if 'task' in record and record['task'] != getattr(agent, '_acceptance_task', getattr(agent, '_task_text', '')) and record.get('digest') != 'superseded':
        retire(agent, '用户要求已改变')
        return []
    from .subagents import TERMINAL
    manager = agent.child_manager()
    if manager.get(record['agent_id'])['status'] in TERMINAL: return []
    started=time.monotonic();notify(); last_notify=started
    while True:
        if agent.stop_flag is not None and agent.stop_flag.is_set(): return []
        if hasattr(manager,'coordination') and manager.coordination.pending('root'): return []
        if agent.steering:
            incoming=agent.steering()
            if incoming:
                changes = partition_steering(agent, incoming, notify)
                if changes:return changes
        state=manager.get(record['agent_id'])
        if time.monotonic()-last_notify >= 10:
            notify(); last_notify=time.monotonic()
        if state['status'] in TERMINAL:
            seconds=round(time.monotonic()-started,3)
            agent._review_wait_notice='独立验收已结束；实际等待 '+str(seconds)+' 秒。'+json.dumps(
                {k:state.get(k) for k in ('agent_id','status','summary','error')},ensure_ascii=False)[:6000]
            agent.session.append('independent_review/waited',seconds=seconds,agent_id=record['agent_id'])
            return []
        manager.wait(record['agent_id'],timeout_s=.5,after_revision=state['revision'])


def retry(agent):
    """Retry the review itself; never require a dummy edit to invalidate it."""
    from .subagents import TERMINAL
    record = getattr(agent, '_independent_review', None)
    if record and agent.child_manager().get(record['agent_id'])['status'] not in TERMINAL:
        raise RuntimeError('独立验收仍在运行，请等待或取消后重试')
    verdict = check(agent)
    if verdict.allow:
        agent._review_retry_ready=True
        return {'agent_id':agent._independent_review['agent_id'],'passed':True,'reused':True,'instruction':'要求及产物未变，复用宿主已接受的验收，不重复运行。'}
    if (getattr(agent,'_independent_review',None) or {}).get('decision',{}).get('code')=='existing_files_modified':
        return {'agent_id':agent._independent_review['agent_id'],'passed':False,'instruction':verdict.instruction}
    if record and agent._independent_review == record and verdict.by != '独立验收等待':
        agent._previous_review = agent.child_manager().get(record['agent_id'])
        agent._independent_review = None
        verdict = check(agent)
    if verdict.by == '独立验收等待':
        incoming = wait_pending(agent)
        if incoming:
            agent._review_steering = incoming
        elif not (agent.stop_flag is not None and agent.stop_flag.is_set()):
            verdict = check(agent)
    agent._review_retry_ready = verdict.allow
    return {'agent_id': (getattr(agent, '_independent_review', None) or {}).get('agent_id'),
            'passed':verdict.allow, 'instruction': verdict.instruction}


def check(agent):
    from .subagents import TERMINAL
    digest = workspace_digest(agent.ws.scope)
    record = getattr(agent, '_independent_review', None)
    budget = max(0, int(getattr(getattr(agent, 'cfg', None), 'verification_token_budget', 0)))
    task_text = getattr(agent, '_acceptance_task', agent._task_text)
    scope_key = getattr(agent.ws, 'knowledge_sources', None)
    if not record or record['digest'] != digest or record.get('task', task_text) != task_text or record.get('knowledge_scopes') != scope_key:
        manager = agent.child_manager()
        previous = manager.get(record['agent_id']) if record else getattr(agent, '_previous_review', None)
        if record:
            retire(agent, '要求或产物已改变')
        # A cancelled reviewer may still be leaving its model call. Do not overlap replacements.
        stale = [t['data'] for t in getattr(manager,'tasks',{}).values()
                 if t['data'].get('purpose') == 'verification' and t['data']['status'] not in TERMINAL]
        if stale:
            agent._independent_review = {'agent_id':stale[0]['agent_id'], 'digest':'superseded', 'task':''}
            return ReflectionVerdict(False,'正在等待旧验收取消完成，随后按新要求验收。','独立验收等待')
        from .review_contract import INSTRUCTIONS
        task = (
            'You are an independent reviewer. Verify the user requirements against actual artifacts, '
            'not the author self-assessment. Use verification_environment to inspect available tools '
            'and report_verification_progress to record a finite plan focused on this change. '
            'For web UI, prefer browser_preview, browser_click and browser_check in a real browser. '
            'A missing sandbox package or denied path does not prove host software is damaged. '
            'For a necessary check, request_execution may request fresh, single-use host approval '
            'for this review copy. Never reuse author approval, install automatically, or replay '
            'commands with unknown effects. If tools are unavailable or approval is denied, report blocked. '
            'Do not emulate a runtime, rewrite the project, or modify delivered code to replace a real check. '
            'Read relevant implementation and independently construct boundary/counterexample tests. '
            'For knowledge claims, use list_knowledge, search_knowledge and read_knowledge_chunk '
            'against the selected host snapshot. Missing source snapshots are platform evidence gaps, '
            'not proof that the author lacked the source. PATH absence is not proof of absent host hardware. '
            'Inspect relevant changes and dependencies only; avoid unrelated projects, logs and caches. '
            'Prefer a small standard-library test early. Report a confirmed defect promptly. '
            'Put generated tests in create_verification_scratch or a new temporary directory. '
            'Never overwrite existing delivery files, including test fixtures. If author tests overwrite '
            'fixed existing paths, report those paths and require isolation rather than fixing them yourself. '
            'Repeatedly running the same assertions through different runners is not independent coverage. '
            'Derive checks from requirements, artifact promises and the input domain, not an unrelated template. '
            + INSTRUCTIONS +
            ' Finish by calling finish with summary containing only JSON: '
            '{"verdict":"pass/fail/blocked/inconclusive","findings":[],"tests":[],"reason":""}. '
            'For pass, findings must be empty; record successful observations in tests. '
            'For fail, findings must identify concrete defects. An unavailable necessary check cannot pass; '
            'blocked does not require a successful command and does not mean the deliverable is defective. '
            '\nUser requirements:\n' + task_text +
            '\nChanged paths (navigation hints, not correctness evidence):\n' +
            json.dumps(getattr(agent, '_files_touched', []), ensure_ascii=False))
        if previous and previous.get('status') == 'completed':
            task += ('\nPrevious host-stored independent verdict (not author self-assessment):\n' +
                     previous.get('summary', '')[:6000] +
                     '\nThis is a repair review: reproduce the prior counterexample and verify the fix, '
                     'then run necessary regressions. Do not rebuild the entire suite.')
        if getattr(agent.ws, 'general_chat', False):
            task += ('\nThis is a projectless conversation. For text artifacts, check_file_text can '
                     'assert expected content derived from the original requirements. Shell/browser '
                     'execution is not required for text assertions, which do not validate code behavior.')
        try:
            identifier = manager.spawn(task, mode='isolated', token_budget=budget, purpose='verification',
                                       source_paths=list(getattr(agent, '_files_touched', [])))
        except RuntimeError as exc:
            return ReflectionVerdict(False, '无法启动独立验收：' + str(exc), '独立验收', True)
        record = {'agent_id': identifier, 'digest': digest, 'token_budget': budget, 'task':task_text, 'knowledge_scopes':scope_key}
        agent._independent_review = record
        agent.session.append('independent_review/started', **record)
    result = agent.child_manager().get(record['agent_id'])
    if result['status'] not in TERMINAL:
        return ReflectionVerdict(False, '独立验收正在运行，宿主会挂起等待结果；无需反复调用 wait_agent 或 finish。', '独立验收等待')
    from .review_decision import assess
    decision=assess(agent,record,result,digest)
    if record.get('decision') != decision:
        agent.session.append('independent_review/result',agent_id=record['agent_id'],
                             passed=decision['accepted'],digest=digest,decision=decision)
    record['decision']=decision
    record['passed']=decision['accepted']
    record['tests']=decision.get('tests',[])
    if decision['accepted']:return ReflectionVerdict.ok()
    label='独立验收' if decision['code']=='findings' else '独立验收受阻' if decision['code'] in {'review_blocked','source_missing','source_stale','source_modified','knowledge_not_read'} else '独立验收未完成'
    exhausted = decision['code'] in {'existing_files_modified','review_blocked','source_missing','source_stale','source_modified','knowledge_not_read'} or agent._finish_rejects>=2
    return ReflectionVerdict(False,decision['reason'],label,exhausted)
