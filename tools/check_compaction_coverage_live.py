"""Opt-in API check: late facts beyond both former truncation boundaries."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    if len(sys.argv)!=3 or sys.argv[1]!='--real':raise SystemExit('Usage: --real NEW_REPORT_PATH')
    from agentlab.providers import ChatMessage
    from agentplat.llmconfig import LLMConfig
    from agentplat.model_client import create_client
    from agentplat.compaction import Compactor
    path=Path(sys.argv[2]);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():raise SystemExit('Report already exists')
    cfg=LLMConfig.load();cfg.max_tokens=2048
    filler='常规进度：已读取说明，继续检查；没有新增文件或决定。\n'*1100
    constraint='不得删除原始数据，也不得上传用户文件。'
    messages=[ChatMessage('system','你是编码助手。'),ChatMessage('user','检查代码并记录已改动文件。'),
              ChatMessage('assistant',filler+'\n已改动文件：late_tail_9137.py；关键决定：重试次数设为 7，避免重复写入。'),
              ChatMessage('user',constraint),ChatMessage('assistant','检查完成，等待汇总。'),
              ChatMessage('user','继续'),ChatMessage('assistant','准备继续')]
    compactor=Compactor(create_client(cfg),cfg)
    result=compactor.summarize(messages,2)
    summary='\n'.join(m.content for m in messages if m.role=='assistant')
    checks={'summarized':result[0]>0,'multiple_batches':result[1]>1,
            'late_filename':'late_tail_9137.py' in summary,
            'constraint_verbatim':constraint in [m.content for m in messages]}
    report={'checks':checks,'summary_calls':result[1],'summary_usd':result[2],
            'missing':result[3],'billing':compactor.last_billing}
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False));assert all(checks.values()),checks


if __name__=='__main__':main()
