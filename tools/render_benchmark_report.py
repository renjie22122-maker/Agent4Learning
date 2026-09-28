"""Render preserved independent eval results; never pools review variants."""
import argparse,json
from pathlib import Path


def render(paths):
    lines=['# Agent 能力实测报告','',
           '这是本项目自建小型评测，非官方榜单成绩。重复次数很少，不可外推长期成功率。',
           '外部通过表示交付物符合断言；完成声明和超时单独记录。费用为估算。','']
    for path in paths:
        report=json.loads(Path(path).read_text(encoding='utf-8'))
        lines += ['## '+str(Path(path).parent.name),'',
                  '| 任务 | 重复 | 外部通过 | 声明完成 | 秒 | 模型请求 | 工具失败 | 验收次数 |',
                  '|---|---:|---|---|---:|---:|---:|---:|']
        for r in report['results']:
            m=r.get('metrics',{})
            lines.append(f"| {r['task']} | {r['repeat']+1} | {r['passed']} | {r.get('declared_ok')} | {r.get('elapsed_s','—')} | {m.get('model_requests','—')} | {m.get('tool_failures','—')} | {m.get('review_starts','—')} |")
        lines += ['',f"虚假完成数：{report['false_successes']}。",'',
                  '| 任务 | 样本数 | 外部通过率 | pass@2 | pass^2 |', '|---|---:|---:|---:|---:|']
        for r in report['reliability']:
            lines.append(f"| {r['task']} | {r['trials']} | {r['pass_rate']} | {r['pass_at_k']} | {r['pass_pow_k']} |")
        lines += ['', 'null 表示样本不足，不能计算。', '']
    lines += ['## 解读边界','',
              '- 正确性检查独立于 Agent 自测；不能把离线机制测试通过混入真实模型成功率。',
              '- 单 Agent 基线没有强制独立审查；review 组单独开启，均由相同外部裁判评分。',
              '- 原生沙箱与 Windows 命令适配失败计入工具失败，不隐藏。',
              '- 重复验证成本、长上下文等问题另见 tetris-run-audit.json；本轮小任务不代表大型项目性能。']
    return '\n'.join(lines)+'\n'


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('reports',nargs='+');ap.add_argument('--output',required=True)
    args=ap.parse_args();Path(args.output).write_text(render(args.reports),encoding='utf-8')
