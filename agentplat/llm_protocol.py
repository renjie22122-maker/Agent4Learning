"""Repair wire ordering without claiming missing operations were never executed."""
from copy import deepcopy
from dataclasses import replace
from agentlab.providers import ChatMessage

PROTOCOL_REPAIRS = {}
MISSING = ('[结果缺失] 这条工具调用没有可核实的结果。执行状态未知；'
           '先检查日志、状态和产物，不得盲目重放写入、命令或外部操作。')


def sanitize_messages(messages):
    fixed, notes, used = [], [], set()
    pending = {}
    def close():
        for original, identifier in pending.items():
            fixed.append(ChatMessage('tool', MISSING, tool_call_id=identifier))
            notes.append(f'tool_call_id={original} 的调用没有结果，已补一条占位结果')
        pending.clear()
    for index, message in enumerate(messages):
        if message.role == 'tool':
            identifier = str(message.tool_call_id or '')
            if identifier not in pending:
                notes.append(f'message[{index}]：孤儿或重复的 tool 结果已丢弃')
                continue
            fixed.append(replace(message, tool_call_id=pending.pop(identifier)))
            continue
        close()
        if message.role == 'assistant' and message.tool_calls:
            calls = []
            for call_index, original in enumerate(message.tool_calls):
                call = deepcopy(original)
                identifier = str(call.get('id') or '')
                if not identifier or identifier in pending:
                    # Within one batch identical IDs have no unambiguous result
                    # association. Refuse to invent which call ran.
                    raise ValueError('Ambiguous tool-call IDs within one assistant message')
                wire_id = identifier
                suffix = 0
                while wire_id in used:
                    suffix += 1
                    wire_id = f'repaired_{index}_{call_index}_{suffix}'
                if wire_id != identifier:
                    notes.append(f'message[{index}]：重复调用 ID {identifier} 已重新编号')
                call['id'] = wire_id
                used.add(wire_id)
                pending[identifier] = wire_id
                calls.append(call)
            message = replace(message, tool_calls=calls)
        fixed.append(message)
    close()
    return fixed, notes


def sanitize_in_place(messages):
    fixed, notes = sanitize_messages(messages)
    if notes:
        messages[:] = fixed
    return notes
