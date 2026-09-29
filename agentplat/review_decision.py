"""One host verdict for both status display and completion enforcement."""
import json
from .runtime import workspace_digest


def assess(agent, record, result, digest=None):
    def decision(code, reason, accepted=False, **details):
        return dict(accepted=accepted, code=code, reason=reason, **details)
    if 'digest' not in record:
        return decision('unbound_record','旧验收记录缺少产物版本绑定，不能作为当前交付通过的依据。')
    if record.get('knowledge_scopes') != getattr(agent.ws, 'knowledge_sources', None):
        return decision('stale_knowledge_scope','知识库检索范围已改变；旧验收不能批准当前交付。')
    current = workspace_digest(agent.ws.scope) if digest is None else digest
    if record.get('digest') != current:
        return decision('stale_artifact','产物版本已改变；旧验收不适用于当前文件。')
    task=getattr(agent,'_acceptance_task',getattr(agent,'_task_text',''))
    if record.get('task',task) != task:
        return decision('stale_requirements','验收要求已改变；旧验收不适用于当前任务。')
    state=result.get('status')
    if state in ('queued','running'):
        return decision('pending','独立验收正在运行。')
    if state != 'completed':
        if state=='budget_exceeded':
            return decision('budget_exceeded','独立验收未完成：已触达配置的单次或共享预算；请核对实际预算配置。')
        error=result.get('error','')
        code='timeout' if 'TIMEOUT' in error or '超时' in error else 'execution_failed'
        return decision(code,'独立验收未完成：'+(error or str(state))+'。这不是交付缺陷结论；排除执行原因后可重试。')
    if result.get('source_snapshot_intact') is False:
        return decision('source_modified','验收副本中的来源证据被修改，不能接受结论。')
    if any(x.get('missing') for x in result.get('source_snapshot',[])):
        return decision('source_missing','验收缺少引用的来源快照；请恢复原始证据。')
    from .review_evidence import intact
    if not intact(agent.ws.scope,result.get('source_snapshot',[])):
        return decision('source_stale','作者工作区的来源已改变；验收快照已过期。')
    try: verdict=json.loads(result.get('summary',''))
    except (ValueError,TypeError): verdict={}
    if not isinstance(verdict,dict):verdict={}
    if verdict.get('verdict')=='fail' and verdict.get('findings'):
        return decision('findings','独立验收发现缺陷；根据证据修复后重新验证：'+json.dumps(verdict,ensure_ascii=False)[:2500])
    if verdict.get('verdict') in ('blocked','inconclusive'):
        return decision('review_blocked','独立验收受阻：'+json.dumps(verdict,ensure_ascii=False)[:2500])
    if verdict.get('verdict')!='pass' or verdict.get('findings')!=[] or not isinstance(verdict.get('tests'),list) or not verdict['tests']:
        return decision('invalid_report','验收执行已结束，但结果不符合验收协议：需 pass、非空 tests 和空 findings；不能当作预算不足。')
    modified=[c.get('path','?') for c in result.get('changes',[]) if c.get('before') is not None]
    if modified:
        return decision('existing_files_modified',
            '验收者报告通过，但宿主未接受：验收期间修改或删除了已有交付文件。'
            '若为测试生成物，请将测试改为在新建临时目录中创建输入与输出，不能覆盖已有文件；'
            '不要修改业务代码来凑验收，也不要原样重复验收。涉及文件：'+json.dumps(modified[:20],ensure_ascii=False),
            modified_files=modified)
    if not any(e.get('exit_code')==0 for e in result.get('evidence',[])):
        return decision('missing_evidence','验收者报告通过，但宿主未接受：缺少成功执行的验证证据。')
    source_used=False
    for event in getattr(agent.session,'events',[]):
        if event.kind=='followup/user':source_used=False
        if event.kind=='tool/call' and event.data.get('tool') in ('search_knowledge','read_knowledge_chunk','expanded_search_knowledge'):source_used=True
    if source_used and not result.get('knowledge_reads'):
        return decision('knowledge_not_read','验收者未直接读取知识库来源；作者转录不能代替独立来源证据。')
    return decision('accepted','宿主已接受当前要求和产物版本的独立验收。',True,tests=verdict['tests'])
