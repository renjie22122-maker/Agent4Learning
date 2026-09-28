"""真实模型评估入口：固定任务、独立验收、多次运行；默认不发模型请求。"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TASKS = [
    dict(name='normalize', prompt='实现 normalize.py，其中 normalize(xs) 返回去重并升序排序的整数列表；输入不得修改。写测试并验证。',
         check="from normalize import normalize; x=[3,1,3,-2]; assert normalize(x)==[-2,1,3]; assert x==[3,1,3,-2]; assert normalize([])==[]"),
    dict(name='rle', prompt='实现 rle.py：encode(text) 返回 [(字符,连续次数)]；decode(pairs) 返回原字符串。覆盖空串和 Unicode 并验证。',
         check="from rle import encode,decode; assert encode('aa中中b')==[('a',2),('中',2),('b',1)]; assert decode(encode('🙂🙂中'))=='🙂🙂中'; assert encode('')==[]; assert decode([])==''"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--real', action='store_true', help='明确运行真实模型并计费')
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--variant', choices=['single', 'multi'], default='single')
    parser.add_argument('--output', default='runtime-evaluation.json')
    args = parser.parse_args()
    if not args.real:
        print(json.dumps({'mode': 'plan_only', 'tasks': TASKS, 'runs': args.runs,
                          'variant': args.variant, 'model_calls': 0}, ensure_ascii=False, indent=2))
        return 0
    if args.runs < 1:
        parser.error('--runs 必须为正数')
    from agentplat.llm import OpenAIChatClient
    from agentplat.llmconfig import LLMConfig
    from agentplat.loop import CodingAgent
    from agentplat.workspace import Workspace
    from agentplat.guard import CostGuard
    from agentplat.processes import ProcessSupervisor
    cfg = LLMConfig.load()
    if not cfg.is_real:
        parser.error('请先配置真实模型')
    reports = []
    for task in TASKS:
        for repeat in range(args.runs):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ws = Workspace(root / 'workspace')
                ws.execution_command('python --version')  # fail before any paid model call
                agent = CodingAgent(OpenAIChatClient(cfg), cfg, workspace=ws,
                                    guard=CostGuard(), session_dir=root / 'sessions',
                                    enable_subagents=args.variant == 'multi')
                prompt = task['prompt'] + (' 请委派一个只读子 Agent 审查实现，主 Agent 负责修改与验证。' if args.variant == 'multi' else '')
                start = time.perf_counter()
                result = agent.run(prompt)
                # 验收脚本由评估器提供；不采信模型自己的总结或测试输出。
                import shlex
                import subprocess
                command = shlex.join(['python', '-c', task['check']]) if ws.execution_mode == 'docker' else subprocess.list2cmdline(['python', '-c', task['check']])
                ws.run(command, timeout_s=10)
                observed = ws.last_execution
                children = list(agent.children.tasks.values()) if agent.children else []
                reports.append(dict(task=task['name'], repeat=repeat, variant=args.variant,
                    model=cfg.model_or('mid') or cfg.model, declared_ok=result.ok,
                    cost_is_estimate=True,
                    independently_passed=observed['status'] == 'exited' and observed['exit_code'] == 0,
                    elapsed_s=time.perf_counter() - start, parent_usd=result.usd,
                    parent_tokens=result.tokens_in + result.tokens_out,
                    child_tokens=sum(t['data'].get('used_tokens', 0) for t in children),
                    child_usd=sum(t['data'].get('usd', 0) for t in children),
                    stop_reason=result.stopped_by))
                Path(args.output).write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'完成 {len(reports)} 次独立验收，结果：{args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
