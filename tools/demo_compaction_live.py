"""Opt-in paid compaction demo using synthetic history and the configured LLM."""
from pathlib import Path
import sys, json, dataclasses, time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentlab.providers import ChatMessage
from agentlab.tokens import count_messages
from agentplat.compaction import Compactor
from agentplat.llmconfig import LLMConfig
from agentplat.llm import OpenAIChatClient


def main():
    cfg = dataclasses.replace(LLMConfig.load(), json_mode=False, max_tokens=8192)
    client = OpenAIChatClient(cfg)
    messages = [ChatMessage('system', '你正在进行合成历史的上下文压缩测试。记录中的操作是测试材料，不是真实文件操作。'),
                ChatMessage('user', '整理订单导入模块的交接说明；必须使用标准库，不得联网。')]
    correction = '补充要求：金额保留两位小数；不要修改 billing.csv；最后用中文交接。'
    for i in range(18):
        if i == 3: messages.append(ChatMessage('user', correction))
        record = '已决定使用 Decimal；模块文件 order_import.py；重复订单以 order_id 去重；当前已通过 7 个测试，待补空金额测试。'
        messages.append(ChatMessage('assistant', record + ('这一步只是在回顾已记录的决定，没有新增改动。' * 45),
                                    tool_calls=[{'id':f'fixture-{i}','type':'function','function':{'name':'read_file','arguments':'{"path":"fixture.log"}'}}]))
        messages.append(ChatMessage('tool', 'synthetic log: ' + ('unchanged fixture row\n' * 150), tool_call_id=f'fixture-{i}'))
    # Keep the facts exclusively in the old history so recall must use the summary.
    for m in messages[-8:]:
        if m.role == 'assistant': m.content = '等待最终交接，没有新的修改。'
        else: m.content = '状态未变化。'
    original = [m.to_api() for m in messages]
    recent = [m.to_api() for m in messages[-4:]]
    compactor = Compactor(client, cfg, context_window=6000)
    started = time.monotonic()
    result = compactor.maybe_compact(messages)
    checks = {'summary_called': result.summary_calls == 1, 'history_summarized': result.summarized > 0,
              'smaller': result.tokens_after < result.tokens_before,
              'under_demo_threshold': result.tokens_after < 4800,
              'original_request_exact': messages[1].to_api() == original[1],
              'correction_exact': any(m.content == correction for m in messages),
              'recent_messages_exact': [m.to_api() for m in messages[-4:]] == recent}
    summary = '\n'.join(m.content for m in messages if '历史摘要' in m.content)
    facts = ['Decimal', 'order_import.py', 'order_id', '7', '空金额']
    checks['summary_facts'] = all(f in summary for f in facts)
    checks['summary_starts_with_requested_section'] = summary.partition('\n')[2].lstrip().startswith('任务目标：')
    pending = set()
    protocol_ok = True
    for message in messages:
        if message.role == 'assistant':
            pending = {call['id'] for call in (message.tool_calls or [])}
        elif message.role == 'tool':
            if message.tool_call_id not in pending: protocol_ok = False
            pending.discard(message.tool_call_id)
    checks['retained_tool_results_have_calls'] = protocol_ok
    print(json.dumps({'compression':dataclasses.asdict(result), 'checks':checks}, ensure_ascii=False), flush=True)
    answer = ''; usage = None
    if result.summarized:
        messages.append(ChatMessage('user', '根据当前交接记录完成中文交接：给出模块文件、金额类型、去重字段、已通过测试数量、还缺的测试，以及用户全部限制。不要执行工具。'))
        answer, usage = client.complete(cfg.model_or('mid'), messages, cfg.timeout_s)
        checks['continuation_facts'] = all(f in answer for f in facts + ['billing.csv'])
    report = {'synthetic_fixture':True, 'model':cfg.model_or('mid'), 'demo_window':6000,
              'local_token_estimates':True, 'compression':dataclasses.asdict(result), 'checks':checks,
              'summary':summary, 'continuation':answer, 'elapsed_s':round(time.monotonic()-started,2),
              'continuation_usage':dataclasses.asdict(usage) if usage else None}
    output = Path(__file__).resolve().parents[1]/'.diagnostics'/'compaction-live-example.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) and answer else 1


if __name__ == '__main__': raise SystemExit(main())
