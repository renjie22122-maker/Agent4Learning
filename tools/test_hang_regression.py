"""离线热路径回归；外层子进程硬超时，避免测试自身挂住。"""
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def worker():
    from agentplat.workspace import Workspace, _split_segments
    from agentplat.loop import _salvage_tool_args
    from agentplat.spill import SpillPolicy
    from agentplat.compaction import Compactor
    from agentlab.providers import ChatMessage
    from agentplat.loop import CodingAgent
    from agentplat.llmconfig import LLMConfig
    from tools.test_agent_loop import ScriptedLLM, call

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ws = Workspace(root)
        ws.execution_mode = "local"
        # 后代继承 stdout/stderr；旧实现超时杀 shell 后仍等待管道 EOF。
        (root / 'child.py').write_text(
            "import time\nfrom pathlib import Path\n"
            "time.sleep(3)\nPath('survived').write_text('orphan')\ntime.sleep(3)\n")
        (root / 'parent.py').write_text(
            "import subprocess,sys,time\n"
            "subprocess.Popen([sys.executable,'child.py'])\n"
            "print('started',flush=True)\ntime.sleep(10)\n")
        start = time.monotonic()
        output = ws.run('python parent.py', timeout_s=1)
        assert time.monotonic() - start < 5, output
        assert '超时' in output and 'started' in output, output
        assert '未确认' not in output, output
        time.sleep(3)
        assert not (root / 'survived').exists(), '后代进程在超时后继续执行'
        assert '退出码 0' in ws.run('python -c "print(42)"')

        rng = random.Random(20260928)
        spill = SpillPolicy(root)
        compactor = Compactor(context_window=1000)
        for _ in range(2000):
            value = ''.join(rng.choices('abc012 >;&|\t\n\\\"\'{}:', k=rng.randrange(300)))
            _split_segments(value)
            _salvage_tool_args('write_file', '{"path":"x.py","content":"' + value)
        for n in (0, 1, 8000, 20000):
            value = '中文\\\"\n' * n
            spill.apply('read_file', value)
            msgs = [ChatMessage('system', '保留'), ChatMessage('user', '任务')]
            msgs.extend(ChatMessage('tool', value) for _ in range(20))
            compactor.maybe_compact(msgs)
            assert msgs[0].content == '保留'
        cfg = LLMConfig(provider='mock', model='fake')
        llm = ScriptedLLM([('', [call('list_dir', {})])])
        agent = CodingAgent(llm, cfg, workspace=ws, session_dir=root / '.sessions',
                            hard_iterations=85, soft_iterations=100, reflection=False)
        assert agent.max_wall_s is None
        agent.run('重复读取目录验证硬上限')
        assert llm.turn == 85, llm.turn
        # 间隔超限不会累计成连续超限；真正连续三轮应停止。
        batch = ('', [call('list_dir', {}) for _ in range(15)])
        normal = ('', [call('list_dir', {})])
        finish = ('', [call('finish', {'summary': '完成'})])
        llm = ScriptedLLM([batch, normal, batch, normal, batch, finish])
        agent = CodingAgent(llm, cfg, workspace=ws, session_dir=root / '.sessions',
                            reflection=False)
        assert agent.run('验证非连续超限').ok
        llm = ScriptedLLM([batch])
        agent = CodingAgent(llm, cfg, workspace=ws, session_dir=root / '.sessions',
                            reflection=False)
        result = agent.run('验证连续超限')
        assert not result.ok and llm.turn == 3
        assert result.tool_calls == 3 * agent.MAX_TOOLS_PER_STEP
    print('PASS: process-tree timeout, partial output, 2000 parser inputs, spill, compaction')


if __name__ == '__main__':
    if '--worker' in sys.argv:
        worker()
    else:
        result = subprocess.run([sys.executable, '-X', 'utf8', __file__, '--worker'],
                                cwd=ROOT, timeout=30)
        sys.exit(result.returncode)
