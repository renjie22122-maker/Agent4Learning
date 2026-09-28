"""真实执行内核回归：只用临时目录、脚本模型与本机 MCP 夹具。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.experiments import ScriptedModel
from agentplat.llmconfig import LLMConfig
from agentplat.loop import CodingAgent
from agentplat.workspace import Workspace
from agentplat.runtime import CapabilityPolicy, invoke_checked


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = Workspace(self.root / 'ws')
        self.ws.execution_mode = 'local'
        self.cfg = LLMConfig(model='fake', provider='mock', max_tokens=128)

    def agent(self, script, **kwargs):
        return CodingAgent(ScriptedModel(script), self.cfg, workspace=self.ws,
                           session_dir=self.root / 'sessions', enable_subagents=False, **kwargs)

    def test_source_digest_duplicates_and_reserved_paths(self):
        import hashlib
        from agentplat.sources import SourceStore
        store = SourceStore(self.ws.root)
        store.root.mkdir()
        raw = b'Company A'
        digest = hashlib.sha256(raw).hexdigest()
        path = store.root / (digest + '.txt')
        path.write_bytes(raw)
        claim = {'source_id': digest, 'quote': 'Company A'}
        self.assertTrue(store.validate_claims([claim], 1)['complete'])
        self.assertFalse(store.validate_claims([claim, claim], 2)['complete'])
        path.write_bytes(raw + b' changed')
        self.assertFalse(store.validate_claims([claim], 1)['complete'])
        for method, args in [(self.ws.write_file, ('.sources/fake', 'x')),
                             (self.ws.append_file, ('.sessions/fake', 'x')),
                             (self.ws.edit_file, ('.sources/fake', 'x', 'y'))]:
            with self.assertRaises(Exception):
                method(*args)

    def test_docker_unavailable_never_falls_back(self):
        from unittest.mock import patch
        self.ws.execution_mode = 'docker'
        with patch('agentplat.workspace.shutil.which', return_value=None):
            with self.assertRaisesRegex(Exception, 'Docker'):
                self.ws.run('python --version')
        self.assertFalse(self.ws.processes.tasks)

    def test_default_local_and_explicit_disabled(self):
        from unittest.mock import patch
        from agentplat.execution import execution_status
        with patch.dict(os.environ, {}, clear=True), patch('agentplat.execution.POLICY_PATH', self.root / 'absent-policy.json'):
            workspace = Workspace(self.root / 'default')
            self.assertEqual(workspace.execution_mode, 'local')
            self.assertTrue(execution_status()['shell_available'])
            self.assertFalse(execution_status()['sandbox_ready'])
        workspace.execution_mode = 'disabled'
        with self.assertRaises(Exception):
            workspace.run('python --version')
        with patch.dict(os.environ, {'AGENTLAB_EXECUTION_MODE': 'typo'}):
            with self.assertRaises(ValueError):
                Workspace(self.root / 'invalid')

    def test_local_child_executes_in_copy_and_merges(self):
        from agentplat.subagents import AgentManager, TERMINAL
        script = [[('write_file', {'path': 'answer.py', 'content': 'print(42)'})],
                  [('run_shell', {'command': 'python answer.py'})],
                  [('finish', {'summary': '已运行 answer.py，退出码为零'})]]
        manager = AgentManager(self.cfg, self.ws, self.root / 'local-children',
                               factory=lambda: ScriptedModel(script))
        self.addCleanup(manager.close)
        agent_id = manager.spawn('创建 answer.py 并运行验证', mode='isolated', token_budget=50000)
        state = manager.get(agent_id)
        deadline = time.monotonic() + 15
        while state['status'] not in TERMINAL and time.monotonic() < deadline:
            state = manager.wait(agent_id, 1, state['revision'])
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual(state['execution_mode'], 'local')
        child = manager.tasks[agent_id]['agent']
        self.assertEqual(child.ws.last_execution['exit_code'], 0)
        self.assertFalse((self.ws.root / 'answer.py').exists())
        self.assertEqual(manager.apply(agent_id)['applied'], ['answer.py'])
        self.assertEqual((self.ws.root / 'answer.py').read_text(), 'print(42)')

    def test_unverified_cannot_exhaust_gate(self):
        agent = self.agent([[('write_file', {'path': 'bad.py', 'content': 'syntax !!!'})],
                            [('finish', {'summary': 'done'})]])
        result = agent.run('创建可运行程序')
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_by, 'unverified')
        self.assertEqual(self.ws.commands_run, 0)

    def test_edit_invalidates_success(self):
        agent = self.agent([[('write_file', {'path': 'app.py', 'content': 'print(1)'})],
                            [('run_shell', {'command': 'python app.py'})],
                            [('write_file', {'path': 'app.py', 'content': 'broken !!!'})],
                            [('finish', {'summary': 'done'})]])
        self.assertFalse(agent.run('编写并验证程序').ok)

    def test_exit_code_not_stdout_decides(self):
        agent = self.agent([[('write_file', {'path': 'bad.py', 'content': 'bad'})],
                            [('run_shell', {'command': 'python -c "import sys; print(\'退出码 0\'); sys.exit(1)"'})],
                            [('finish', {'summary': 'done'})]])
        self.assertFalse(agent.run('编写并验证程序').ok)

    def test_schema_rejects_partial_wrong_and_out_of_range(self):
        for args in ['{"path":"a","content":"partial', '[]',
                     {'path': 'a', 'content': 1}, {'path': 'a', 'content': 'x', 'extra': True}]:
            agent = self.agent([[('write_file', args)], [('finish', {'summary': '拒绝错误参数'})]])
            agent.run('验证参数拒绝')
            self.assertFalse((self.ws.root / 'a').exists())
        schema = {'type': 'object', 'properties': {'n': {'type': 'integer', 'maximum': 2}}, 'required': ['n']}
        for value in [3, True, 1.5, float('nan')]:
            with self.assertRaises(Exception):
                invoke_checked('test', {'n': value}, schema, lambda n: n)

    def test_duplicate_id_never_repeats_write(self):
        class Repeated(ScriptedModel):
            def complete_with_tools(inner, *args):
                text, calls, usage = super().complete_with_tools(*args)
                calls[0]['id'] = 'same'
                return text, calls, usage
        agent = self.agent([[('finish', {'summary': 'placeholder'})]], hard_iterations=3)
        agent.llm = Repeated([[('append_file', {'path': 'a.txt', 'content': 'once'})]])
        agent.run('验证重复 ID')
        self.assertEqual((self.ws.root / 'a.txt').read_text(), 'once')

    def test_resume_pairs_intent_and_result(self):
        from agentplat.session import SessionLog, replay
        log = SessionLog(self.root / 'crash.jsonl')
        log.append('session/created', task='只读检查项目', session_id=log.session_id)
        log.append('step/start', iteration=3)
        log.append('tool/call', tool='write_file', destructive=True, ok=True, call_id='unknown', path='x.py')
        state = replay(log)
        self.assertEqual(state.files_written, [])
        self.assertEqual(len(state.unknown_calls), 1)
        agent = self.agent([[('finish', {'summary': '已检查，未重复执行写入'})]])
        result = agent.resume(log.path)
        self.assertTrue(result.ok)
        self.assertEqual(len(agent.session.of_kind('session/created')), 1)
        self.assertEqual(agent.session.of_kind('step/start')[-1].data['iteration'], 4)
        self.assertFalse((self.ws.root / 'x.py').exists())

    def test_large_file_and_spill_retrievable(self):
        from agentplat.spill import SpillPolicy
        raw = ('中文 row\n' * 50000)
        (self.ws.root / 'large.txt').write_text(raw, encoding='utf-8')
        self.assertIn('40000| 中文 row', self.ws.read_file('large.txt', 40000, 1))
        policy = SpillPolicy(self.ws.root, preview_bytes=100)
        inline = policy.apply('read_file', raw)
        path = policy.spilled[0].path
        self.assertEqual((self.ws.root / path).read_bytes(), raw.encode())
        self.assertLess(len(inline.encode()), 2000)
        block = json.loads(self.ws.read_chunk(path, 200001, 30))
        self.assertEqual(block['next_offset'], 200031)

    def test_process_handle_cancel_and_interaction(self):
        from agentplat.processes import ProcessSupervisor
        supervisor = ProcessSupervisor()
        self.addCleanup(supervisor.close)
        task = supervisor.start([sys.executable, '-u', '-c', 'print(input())'], self.ws.root, timeout_s=5)
        supervisor.write(task, 'hello\n')
        state = supervisor.wait(task, 5)
        self.assertEqual(state['exit_code'], 0)
        self.assertIn('hello', state['output'])
        self.assertEqual(supervisor.poll(task, state['cursor'])['output'], '')
        task = supervisor.start([sys.executable, '-c', 'import time;time.sleep(30)'], self.ws.root)
        self.assertEqual(supervisor.cancel(task)['status'], 'cancelled')

    def test_subagent_readonly_budget_cancel_and_persistence(self):
        from agentplat.subagents import AgentManager, TERMINAL
        manager = AgentManager(self.cfg, self.ws, self.root / 'children', max_workers=1,
                               factory=lambda: ScriptedModel(delay=.2), total_tokens=12000)
        self.addCleanup(manager.close)
        first = manager.spawn('读取项目后返回', token_budget=6000)
        second = manager.spawn('随后读取项目', token_budget=6000)
        # Team budget is now reserved per model request, so waiting parents do
        # not reserve the entire budget needed by their grandchildren.
        self.assertEqual(manager.budget.total,12000)
        manager.send(first, '补充：只报告事实')
        manager.cancel(second)
        for agent_id in (first, second):
            state = manager.get(agent_id)
            while state['status'] not in TERMINAL:
                state = manager.wait(agent_id, 5, state['revision'])
        self.assertEqual(manager.get(second)['status'], 'cancelled')
        self.assertEqual(manager.get(first)['status'], 'completed')
        self.assertNotIn('write_file', manager.tasks[first]['agent'].tools)
        restored = AgentManager(self.cfg, self.ws, self.root / 'children')
        self.addCleanup(restored.close)
        self.assertEqual(restored.get(first)['status'], 'completed')

    def test_isolated_merge_detects_conflict(self):
        from agentplat.isolation import IsolatedChanges
        p = self.ws.root / 'x.py'; p.write_text('base')
        changes = IsolatedChanges(self.ws.root)
        self.addCleanup(changes.close)
        (changes.root / 'x.py').write_text('child')
        self.assertEqual(changes.apply(), ['x.py'])
        self.assertEqual(p.read_text(), 'child')
        with self.assertRaises(RuntimeError):
            changes.apply()

    def test_capability_cannot_be_granted_by_tool_args(self):
        policy = CapabilityPolicy(frozenset({'read_file'}), False, False, False)
        with self.assertRaises(RuntimeError):
            invoke_checked('run_shell', {}, {'type': 'object'}, lambda: None, policy, shell=True)

    def test_mcp_allowlist_session_and_tool_schema(self):
        from agentplat.mcp_client import MCPClient
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                message = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(message)
                method = message['method']
                if method == 'notifications/initialized':
                    self.send_response(202); self.end_headers(); return
                result = {'protocolVersion': '2025-03-26', 'capabilities': {}}
                if method == 'tools/list':
                    result = {'tools': [{'name': 'browser_snapshot', 'inputSchema': {'type': 'object'}},
                                        {'name': 'delete_everything', 'inputSchema': {'type': 'object'}}]}
                if method == 'tools/call':
                    result = {'content': [{'type': 'text', 'text': 'untrusted page'}]}
                raw = json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Mcp-Session-Id', 'fixture-session')
                self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = MCPClient(f'http://127.0.0.1:{server.server_port}/mcp', allowed_tools=['browser_snapshot'])
        self.assertEqual([t['name'] for t in client.list_tools()], ['browser_snapshot'])
        self.assertIn('untrusted page', client.call('browser_snapshot', {}))
        with self.assertRaises(PermissionError):
            client.call('delete_everything', {})
        self.assertEqual(client.session, 'fixture-session')


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]], verbosity=2)
