"""Opt-in real API experiment: repeated automatic compaction and blind recall."""
import json
import os
from pathlib import Path
import sys
import time
from dataclasses import asdict
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def protocol_valid(messages):
    pending = set()
    for m in messages:
        if m.role == 'tool':
            if m.tool_call_id not in pending: return False
            pending.remove(m.tool_call_id)
        else:
            if pending: return False
            pending = {c['id'] for c in (m.tool_calls or [])}
    return not pending


def answer_json(text):
    start, end = text.find('{'), text.rfind('}')
    try: return json.loads(text[start:end+1])
    except (ValueError, TypeError): return {}


def main():
    if len(sys.argv) != 3 or sys.argv[1] != '--real':
        raise SystemExit('Usage: --real NEW_OUTPUT_DIRECTORY')
    root=Path(sys.argv[2]).resolve();root.mkdir(parents=True,exist_ok=False)
    os.environ['AGENTLAB_MEMORY_DIR']=str(root/'memory')
    os.environ['AGENTLAB_KB_DIR']=str(root/'knowledge')
    os.environ['AGENTLAB_HUMAN_DB']=str(root/'human.sqlite3')
    from agentlab.providers import ChatMessage
    from agentlab.tokens import count_messages
    from agentplat.llmconfig import LLMConfig
    from agentplat.model_client import create_client
    from agentplat.loop import CodingAgent
    from agentplat.workspace import Workspace
    from agentplat.general_chat import install
    from agentplat.evaluation_report import envelope
    cfg=LLMConfig.load();cfg.memory_enabled=False;cfg.timeout_s=90
    cfg.max_tokens=int(os.environ.get('COMPACTION_TEST_MAX_OUTPUT','4096'))
    if os.environ.get('COMPACTION_TEST_REASONING'):cfg.reasoning_effort=os.environ['COMPACTION_TEST_REASONING']
    workspace=root/'files';workspace.mkdir()
    (workspace/'do-not-change.txt').write_text('UNCHANGED',encoding='utf-8')
    def build(sid):
        a=CodingAgent(create_client(cfg),cfg,workspace=Workspace(workspace),session_dir=root/'sessions',
                      session_id=sid,context_window=16000,hard_iterations=8,
                      max_wall_s=240,enable_subagents=False)
        install(a);return a
    agent=build('repeated');checks={};cycles=[];expected={};started=time.monotonic()
    constraint='全程只读，禁止修改文件、运行命令、联网或者委派。缺失信息必须写 UNKNOWN，不得猜测。'
    report={'real_api':True,'synthetic_history':True,'configuration':{'cycles':3,'context_window':16000,
            'threshold_ratio':.8,'max_steps_per_probe':8,'wall_limit_per_probe_s':240,
            'max_output_tokens':cfg.max_tokens,'reasoning_effort':cfg.reasoning_effort},'checks':checks,'cycles':cycles}
    # Diagnostic wrapper only; do not change the production summarizer policy.
    import agentplat.model_client as clients
    original_summary_client=clients.summary_client
    report['summary_errors']=[]
    def observed_summary_client(*args):
        client=original_summary_client(*args);complete=client.complete
        def observed(*a,**kw):
            try:return complete(*a,**kw)
            except Exception as exc:
                report['summary_errors'].append({'type':type(exc).__name__,'code':getattr(exc,'code',''),
                    'message':str(exc)[:300]})
                raise
        client.complete=observed;return client
    clients.summary_client=observed_summary_client
    def save():
        report['elapsed_s']=round(time.monotonic()-started,3)
        report['evaluation']=envelope('integration',[{'id':k,'status':'passed' if v else 'failed'} for k,v in checks.items()],report['configuration'],planned_cases=len(checks))
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        initial=agent.run('这是隔离的上下文压缩实验。'+constraint+' 后续会提供合成工作记录。现在仅回复 READY，不必调用工具或 finish。')
        checks['initial_completed']=initial.ok
        for cycle in range(1,4):
            messages=agent._conversation
            # Answers are stored in harness expectations, never inserted into probe questions.
            additions={f'file_{cycle}':f'module_{cycle}_739{cycle}.py',
                       f'decision_{cycle}':f'retry-{cycle+6}-idempotent',
                       f'code_{cycle}':f'K{cycle}-824{cycle}-ORCHID'}
            expected.update(additions)
            messages.append(ChatMessage('assistant','合成记录中的已确认事实（后续要准确恢复）：'+json.dumps(additions)))
            cid=f'fixture-read-{cycle}'
            messages.append(ChatMessage('assistant','合成工具记录，作为压缩协议夹具。',tool_calls=[{'id':cid,'type':'function','function':{'name':'read_file','arguments':'{"path":"do-not-change.txt"}'}}]))
            messages.append(ChatMessage('tool','UNCHANGED',tool_call_id=cid))
            filler=('合成的重复检查进度：已核对常规格式，无新增决定、无文件改动；本段没有关键事实。\n'*85)
            while count_messages(messages)<20500:
                messages.append(ChatMessage('assistant',f'合成批次 {cycle} 常规记录。\n'+filler))
            # Keep recent tail disposable so all new facts lie in the summarized region.
            for n in range(5):messages.append(ChatMessage('assistant',f'合成批次 {cycle} 收尾 {n}：继续核对，没有新增事实。'))
            agent._save_conversation(messages)
            before=len(agent.compactor.history)
            question='只从上下文恢复以下键的值，以一个 JSON 对象直接回答，不要工具或 finish。键：'+', '.join(expected)+', never_recorded。never_recorded 从未记录，应按原约束处理。'
            result=agent.continue_with(question)
            got=answer_json(result.summary)
            events=agent.compactor.history[before:]
            checks[f'cycle_{cycle}_completed']=result.ok
            checks[f'cycle_{cycle}_actual_summary']=any(c.summarized>0 and c.summary_calls>0 and c.tokens_after<c.tokens_before for c in events)
            checks[f'cycle_{cycle}_all_facts']=all(got.get(k)==v for k,v in expected.items())
            checks[f'cycle_{cycle}_unknown_not_invented']=got.get('never_recorded')=='UNKNOWN'
            checks[f'cycle_{cycle}_constraint_verbatim']=any(constraint in m.content for m in agent._conversation if m.role=='user')
            checks[f'cycle_{cycle}_protocol']=protocol_valid(agent._conversation)
            cycles.append({'cycle':cycle,'compactions':[asdict(c) for c in events],'answer':got,
                           'missing_or_wrong':[k for k,v in expected.items() if got.get(k)!=v],
                           'model_calls':result.model_calls,'usd':result.usd,'stopped_by':result.stopped_by})
            save();print(json.dumps(cycles[-1],ensure_ascii=False),flush=True)
        checks['three_separate_summary_cycles']=sum(any(c['summarized']>0 for c in row['compactions']) for row in cycles)>=3
        calls=agent.session.of_kind('tool/call')
        checks['no_tool_side_effects']=not calls and (workspace/'do-not-change.txt').read_text()=='UNCHANGED'
        agent.session.flush('before_restore')
        restored=build('restored');restored.restore_conversation(agent.session.path)
        result=restored.continue_with('仅根据已有记录回答一个 JSON 对象，列出 '+', '.join(expected)+'。禁止查询文件，不必 finish。')
        got=answer_json(result.summary)
        checks['restore_completed']=result.ok
        checks['restore_all_facts']=all(got.get(k)==v for k,v in expected.items())
        checks['persisted_compaction_events']=len(restored.session.of_kind('compaction/applied'))>=3
        report['restore_answer']=got
        report['summary_calls']=sum(c.summary_calls for c in agent.compactor.history)
        report['summary_usd']=sum(c.summary_usd for c in agent.compactor.history)
        report['tokens_saved']=sum(c.saved for c in agent.compactor.history)
        save();print(json.dumps({'checks':checks,'summary_calls':report['summary_calls'],'tokens_saved':report['tokens_saved']},ensure_ascii=False),flush=True)
        if not all(checks.values()):raise SystemExit(1)
    except BaseException as exc:
        report['error']=str(exc);save();raise


if __name__=='__main__':main()
