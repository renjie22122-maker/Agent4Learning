"""Bounded SSE assembly: tools are exposed only after a complete response."""
import json


def assemble(lines, on_text=None, cancelled=None):
    text, calls, usage, reason = [], {}, {}, None
    size = 0
    for line in lines:
        if cancelled and cancelled.is_set():
            raise InterruptedError('模型请求已取消')
        size += len(line)
        if size > 32_000_000:
            raise ValueError('模型流式响应超过 32 MB')
        if isinstance(line, bytes): line = line.decode('utf-8')
        if not line.startswith('data:'): continue
        payload = line[5:].strip()
        if payload == '[DONE]': break
        if not payload: continue
        event = json.loads(payload)
        if event.get('error'): raise ValueError(str(event['error']))
        if event.get('usage'): usage = event['usage']
        for choice in event.get('choices', []):
            if choice.get('index', 0) != 0: continue
            delta = choice.get('delta') or {}
            if delta.get('content'):
                text.append(delta['content'])
                if on_text: on_text(delta['content'])
            for piece in delta.get('tool_calls', []):
                index = piece['index']
                if index not in calls:
                    if len(calls) >= 128: raise ValueError('单次流式工具调用数超限')
                    calls[index] = {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}}
                target = calls[index]
                if piece.get('id'): target['id'] += piece['id']
                for key in ('name', 'arguments'):
                    target['function'][key] += (piece.get('function') or {}).get(key) or ''
            if choice.get('finish_reason'): reason = choice['finish_reason']
    if not reason:
        raise ValueError('模型流在完成标记前断开；未执行任何部分工具调用')
    return {'choices': [{'finish_reason': reason, 'message': {'content': ''.join(text),
            'tool_calls': [calls[i] for i in sorted(calls)]}}], 'usage': usage}
