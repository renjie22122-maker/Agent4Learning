"""Paid API regression: cancel a child only after actual streamed text arrives."""
from pathlib import Path
import json
import sys
import tempfile
import threading
import time
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.llm import OpenAIChatClient
from agentplat.llmconfig import LLMConfig
from agentplat.subagents import AgentManager, TERMINAL
from agentplat.workspace import Workspace


def main():
    cfg = replace(LLMConfig.load(), reasoning_effort='low', stream_tools=True,
                  json_mode=False, max_tokens=8192)
    received = threading.Event()
    returned = threading.Event()
    fragments = []

    class ObservedClient(OpenAIChatClient):
        def complete_with_tools(self, model, messages, tools, timeout):
            try:
                # This transport test needs a long text stream, not an immediate
                # finish tool call. Delegation/tool execution has its own live test.
                return super().complete_with_tools(model, messages, [], timeout)
            finally:
                returned.set()

    client = ObservedClient(cfg)
    def on_text(text):
        fragments.append(text)
        received.set()
    client.on_text = on_text
    with tempfile.TemporaryDirectory(prefix='child-cancel-live-') as td:
        root = Path(td)
        manager = AgentManager(cfg, Workspace(root/'workspace'), root/'children', factory=lambda: client)
        try:
            key = manager.spawn(
                '这是流式取消测试。请先在普通文本回复中逐行输出从 1 到 2000 的整数，'
                '每行附带一句不同的简短说明，不要省略，不要使用工具生成。全部输出之后才调用 finish。',
                token_budget=60000)
            saw_text = received.wait(60)
            inflight = saw_text and not returned.is_set()
            started = time.monotonic()
            manager.cancel(key)
            state = manager.get(key)
            while state['status'] not in TERMINAL and time.monotonic()-started < 10:
                state = manager.wait(key, .2, state['revision'])
            elapsed = time.monotonic()-started
            checks = dict(actual_stream_received=saw_text, cancelled_while_request_inflight=inflight,
                          request_returned=returned.is_set(), child_cancelled=state['status']=='cancelled',
                          cancellation_under_3s=elapsed < 3, no_retry=client.calls == 1)
            report = dict(checks=checks, model=cfg.model_or('mid'), cancel_elapsed_s=round(elapsed,3),
                          received_characters=sum(map(len,fragments)), api_calls=client.calls,
                          final_status=state['status'], error=state.get('error',''),
                          billing_note='Cancelled stream may not include final API usage; zero recorded usage does not mean free.')
            output = Path(__file__).resolve().parents[1]/'.diagnostics'/'subagent-cancellation-live-report.json'
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if all(checks.values()) else 1
        finally:
            manager.close()
            manager.pool.shutdown(wait=True)


if __name__ == '__main__':
    raise SystemExit(main())
