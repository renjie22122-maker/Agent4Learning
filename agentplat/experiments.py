"""教学与回归共用的离线故障场景；数值来自实际执行，不调用外部模型。"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time

from agentlab.providers import ChatMessage, Usage
from .llmconfig import LLMConfig
from .runtime import BudgetPool, CapabilityPolicy, Evidence, invoke_checked, workspace_digest


class ScriptedModel:
    def __init__(self, script=None, delay=0):
        self.script = script or [[('finish', {'summary': '已完成只读检查'})]]
        self.delay = delay
        self.turn = 0

    def complete_with_tools(self, model, messages, tools, timeout):
        time.sleep(self.delay)
        batch = self.script[min(self.turn, len(self.script) - 1)]
        self.turn += 1
        calls = [{'id': f't{self.turn}c{i}', 'type': 'function',
                  'function': {'name': name, 'arguments': args if isinstance(args, str) else json.dumps(args)}}
                 for i, (name, args) in enumerate(batch)]
        return '', calls, Usage(100, 20, 0)


def process_ownership():
    from .processes import ProcessSupervisor
    import sys
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / 'child.py').write_text("import time\nfrom pathlib import Path\ntime.sleep(.7)\nPath('late').touch()\n")
        (root / 'parent.py').write_text("import subprocess,sys,time\nsubprocess.Popen([sys.executable,'child.py'])\ntime.sleep(3)\n")
        import subprocess
        parent = subprocess.Popen([sys.executable, 'parent.py'], cwd=root)
        time.sleep(.2)
        parent.kill(); parent.wait()
        time.sleep(.8)
        before = int((root / 'late').exists())
        (root / 'late').unlink(missing_ok=True)
        supervisor = ProcessSupervisor()
        task = supervisor.start([sys.executable, 'parent.py'], root, timeout_s=.2)
        state = supervisor.wait(task, 3)
        assert state['status'] == 'timeout', state
        time.sleep(.8)
        after = int((root / 'late').exists())
        supervisor.close()
        return 'orphan_side_effects', before, after


def finish_evidence():
    from .reflection import ReflectionRequest, default_reflector, MAX_REJECTS
    req = ReflectionRequest(task='创建可运行文件', summary='完成', files_touched=['bad.py'],
                            verified=False, rejects=MAX_REJECTS)
    old_allow = req.rejects >= MAX_REJECTS or req.verified
    new_allow = default_reflector().review(req).allow
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = root / 'a.py'; p.write_text('print(1)')
        evidence = Evidence('python a.py', 0, workspace_digest(root))
        assert evidence.valid(root)
        p.write_text('broken !')
        assert not evidence.valid(root)
    return 'false_completions', int(old_allow), int(new_allow)


def crash_recovery():
    from .session import SessionLog, replay
    with tempfile.TemporaryDirectory() as td:
        log = SessionLog(Path(td) / 's.jsonl')
        log.append('tool/call', tool='write_file', destructive=True, ok=True,
                   path='not-written.py', call_id='intent')
        state = replay(log)
        before = sum(bool(e.data.get('ok')) for e in log.of_kind('tool/call'))
        assert len(state.unknown_calls) == 1
        after = len(state.files_written)
        log.append('tool/result', tool='write_file', ok=False, call_id='intent')
        assert not replay(log).files_written and len(replay(log).failed_calls) == 1
        log.append('tool/call', tool='write_file', destructive=True, ok=True, path='yes.py', call_id='done')
        log.append('tool/result', tool='write_file', ok=True, call_id='done')
        assert replay(log).files_written == ['yes.py']
    return 'false_recovered_writes', before, after


def parallel_agents():
    from .subagents import AgentManager, TERMINAL
    from .workspace import Workspace
    durations = []
    for workers in (1, 3):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = AgentManager(LLMConfig(provider='mock', model='fake', max_tokens=128),
                                   Workspace(root / 'ws'), root / 'children', max_workers=workers,
                                   factory=lambda: ScriptedModel(delay=.2))
            start = time.perf_counter()
            ids = [manager.spawn('检查项目并返回结论') for _ in range(3)]
            for agent_id in ids:
                state = manager.get(agent_id)
                while state['status'] not in TERMINAL:
                    state = manager.wait(agent_id, 5, state['revision'])
                assert state['status'] == 'completed', state
            durations.append((time.perf_counter() - start) * 1000)
            manager.close()
    return 'parallel_latency_ms', *durations


def edit_conflicts():
    from .isolation import IsolatedChanges
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        target = root / 'app.py'; target.write_text('base')
        isolated = IsolatedChanges(root)
        (isolated.root / 'app.py').write_text('child change')
        target.write_text('parent change')
        try:
            isolated.apply()
        except RuntimeError:
            pass
        else:
            raise AssertionError('冲突未阻止')
        after = int(target.read_text() != 'parent change')
        target.write_text((isolated.root / 'app.py').read_text())  # 复现无冲突检查的覆盖
        before = int(target.read_text() != 'parent change')
        isolated.close()
    return 'lost_parent_changes', before, after


def budget_race():
    total, request, count = 100, 70, 3
    # 旧设计：三个请求同时读到相同剩余额，各自判断足够。
    barrier = threading.Barrier(count)
    def unchecked(i):
        available = total
        barrier.wait()
        return request if available >= request else 0
    with ThreadPoolExecutor(count) as pool:
        before = max(0, sum(pool.map(unchecked, range(count))) - total)
    budget = BudgetPool(total)
    def checked(i):
        try:
            budget.reserve(str(i), request)
            return request
        except RuntimeError:
            return 0
    with ThreadPoolExecutor(count) as pool:
        after = max(0, sum(pool.map(checked, range(count))) - total)
    assert sum(budget.reservations.values()) == request
    return 'budget_overrun', before, after


def tool_protocol():
    from .loop import CodingAgent, _salvage_tool_args
    from .workspace import Workspace
    raw = '{"path":"partial.py","content":"incomplete'
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        legacy = Workspace(root / 'legacy')
        args = _salvage_tool_args('write_file', raw)
        legacy.write_file(args['path'], args['content'])
        before = int((legacy.root / 'partial.py').exists())
        ws = Workspace(root / 'fixed')
        model = ScriptedModel([[('write_file', raw)], [('finish', {'summary': '参数拒绝，无文件改动'})]])
        CodingAgent(model, LLMConfig(model='fake'), workspace=ws,
                    session_dir=root / 'sessions', enable_subagents=False).run('验证截断参数不得执行')
        after = int((ws.root / 'partial.py').exists())
    return 'partial_side_effects', before, after


def injection_boundary():
    actions = []
    schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
    def write():
        actions.append('write')
    # 工具结果要求扩大权限；裸执行器照做。
    write()
    before = len(actions)
    actions.clear()
    try:
        invoke_checked('write_file', {}, schema, write,
                       CapabilityPolicy(frozenset({'read_file'}), False, False), writes=True)
    except PermissionError:
        pass
    except RuntimeError:
        pass
    return 'unauthorized_actions', before, len(actions)


def memory_retention():
    from .compaction import Compactor, SUMMARY_SECTIONS
    class Summarizer:
        def complete(self, *args):
            return '\n'.join(s + '：概述' for s in SUMMARY_SECTIONS), Usage(100, 20, 0)
    constraint = '后续追加约束：禁止删除用户数据，保持接口 v2 兼容'
    messages = [ChatMessage('system', '编码任务'), ChatMessage('user', '修改程序'),
                ChatMessage('user', constraint)]
    messages.extend(ChatMessage('assistant', '工作记录 ' * 20) for _ in range(12))
    original = list(messages)
    compactor = Compactor(Summarizer(), LLMConfig(model='fake'))
    result = compactor.summarize(messages, keep_last=4)
    assert result is not None
    before = int(constraint not in '\n'.join(m.content for m in original[:2] + original[-4:]))
    after = int(constraint not in '\n'.join(m.content for m in messages))
    assert all(m.role != 'system' for m in messages if '历史摘要' in m.content)
    return 'lost_user_constraints', before, after


def source_integrity():
    from .sources import SourceStore
    with tempfile.TemporaryDirectory() as td:
        store = SourceStore(td)
        store.root.mkdir()
        raw = b'2024 Employers: A, B'
        digest = hashlib.sha256(raw).hexdigest()
        (store.root / (digest + '.txt')).write_bytes(raw)
        claims = [{'source_id': digest, 'quote': '2026 Employers: A, B, C'}]
        result = store.validate_claims(claims, 100)
        # 旧设计只要 HTTP 200/拿到内容，就把结果称为完整榜单。
        before = int(bool(raw))
        after = int(result['complete'])
        assert result['unsupported'] == [0] and result['actual_count'] == 1
    return 'false_complete_reports', before, after


SCENARIOS = [process_ownership, finish_evidence, crash_recovery, parallel_agents, edit_conflicts,
             budget_race, tool_protocol, injection_boundary, memory_retention, source_integrity]
