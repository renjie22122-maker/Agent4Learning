"""Compaction must preserve tool-call/result groups, not just recent message counts."""
from agentlab.util import lab
from agentlab.providers import ChatMessage, Usage
from agentplat.compaction import Compactor, SUMMARY_SECTIONS
from agentplat.llmconfig import LLMConfig


def orphans(messages):
    calls=set();missing=0
    for message in messages:
        if message.role=='assistant':calls={c['id'] for c in message.tool_calls or []}
        elif message.role=='tool':
            missing+=message.tool_call_id not in calls
            calls.discard(message.tool_call_id)
    return missing


def main():
    with lab('lab-38-compaction-protocol','压缩与工具协议边界','按消息数裁剪会留下孤立工具结果'):
        class Summary:
            def complete(self,*args):return '\n'.join(s+'：fixture' for s in SUMMARY_SECTIONS),Usage(10,10,0)
        messages=[ChatMessage('system','rules'),ChatMessage('user','task')]+[ChatMessage('assistant','old') for _ in range(4)]
        messages += [ChatMessage('assistant','',tool_calls=[{'id':'a'},{'id':'b'}]),ChatMessage('tool','A',tool_call_id='a'),ChatMessage('tool','B',tool_call_id='b'),ChatMessage('assistant','recent')]
        before=orphans(messages[-3:]);assert before==2
        print('[BROKEN-REPRODUCED] 保留最后三条，调用消息被剪掉，但两个工具结果还在')
        Compactor(Summary(),LLMConfig()).summarize(messages,3)
        after=orphans(messages);assert after==0
        print('[FIX-APPLIED] 切分边界向前移到完整工具批次开始')
        print(f'[VERIFY] orphan_results: {before} -> {after}')
        print('[TAKEAWAY] 摘要内容正确与消息协议完整，是两个独立验收条件。')
    return 0


if __name__=='__main__':raise SystemExit(main())
