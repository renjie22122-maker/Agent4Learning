"""Separate correct artifacts from completed workflows; audit history isolation."""
import argparse,collections,json,math
from pathlib import Path


def summarize(root):
    root=Path(root);report=json.loads((root/'report.json').read_text(encoding='utf-8'))
    groups=collections.defaultdict(list);audit=[]
    for row in report['results']:
        row['workflow_success']=bool(row.get('passed') and row.get('all_turns_completed',row.get('declared_ok',False)))
        groups[row['task']].append(row)
        folder=root/f"{row['task']}-{row['repeat']}"
        logs=list((folder/'sessions').glob('*.jsonl'))
        ev=[json.loads(l) for p in logs for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]
        child_logs=list((folder/'sessions').glob('*-children/*/*.jsonl'))
        child_events=[json.loads(l) for p in child_logs for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]
        row['child_model_requests']=sum(e['kind']=='model/request' for e in child_events)
        row['child_tool_failures']=sum(e['kind']=='tool/result' and not e['data'].get('ok') for e in child_events)
        row['parent_model_requests_observed']=sum(e['kind']=='model/request' for e in ev)
        row['parent_tool_failures_observed']=sum(e['kind']=='tool/result' and not e['data'].get('ok') for e in ev)
        recalls=[e for e in ev if e['kind']=='memory/recalled']
        audit.append({'task':row['task'],'repeat':row['repeat'],'parent_logs':len(logs),
                      'session_creations':sum(e['kind']=='session/created' for e in ev),'memory_recalls':len(recalls),
                      'private_memory':str(folder/'private-memory'),'private_knowledge':str(folder/'private-knowledge')})
    stats=[]
    for task,rows in groups.items():
        n=len(rows);c=sum(r['workflow_success'] for r in rows)
        stats.append({'task':task,'n':n,'artifacts_evaluated':sum('grader_details' in r for r in rows),'artifacts_passed':sum(r.get('passed',False) for r in rows),
                      'completed_success':c,'pass_at_3':1-math.comb(n-c,3)/math.comb(n,3) if n>=3 else None,
                      'pass_pow_3':math.comb(c,3)/math.comb(n,3) if n>=3 else None})
    report['workflow_reliability']=stats;report['isolation_audit']=audit
    (root/'clean-summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    settings=report.get('configuration',{})
    limits=f"每例上限 {settings.get('timeout','未记录')} 秒、每交互轮 {settings.get('max_steps','未记录')} 步，并发 {settings.get('jobs','未记录')}；结果只适用于此测试条件。"
    lines=['# 全新 Agent 实测结果','',
           '全部试验使用空白会话、独立工作区、独立记忆/知识库存储，开启独立验收。非官方榜单。',
           limits,'',
           '| 任务 | 样本 | 外部评分通过 | 完整流程成功 | pass@3 | pass^3 |',
           '|---|---:|---:|---:|---:|---:|']
    for s in stats:lines.append(f"| {s['task']} | {s['n']} | {s['artifacts_passed']}/{s['artifacts_evaluated']} 已验 | {s['completed_success']} | {s['pass_at_3']} | {s['pass_pow_3']} |")
    lines+=['','外部评分通过但验收受阻不算完整成功；超时不从分母删除。样本量少，不能代表所有任务的可靠性。','',
            '| 任务/次数 | 完成原因 | 秒 | 模型请求 | 工具失败 | 验收数 |','|---|---|---:|---:|---:|---:|']
    for r in report['results']:
        m=r.get('metrics',{});lines.append(f"| {r['task']}/{r['repeat']+1} | {r.get('stop_reason',r.get('classification',''))} | {r.get('elapsed_s','—')} | {m.get('model_requests','—')} | {m.get('tool_failures','—')} | {m.get('review_starts','—')} |")
    lines+=['','隔离核验：主会话日志中 memory/recalled 事件共 '+str(sum(x['memory_recalls'] for x in audit))+' 条。各例存储路径与逐次结果见 clean-summary.json。']
    (root/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return stats


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('root');a=ap.parse_args();print(json.dumps(summarize(a.root),ensure_ascii=False))
