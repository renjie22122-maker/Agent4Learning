"""Conservative measured delegation decisions. No fabricated learned benefit."""
import json
import statistics
from pathlib import Path


def decide(task, acceptance, context, *, parent_task='', evidence=None, category='general', model=''):
    normalized=lambda text:''.join(text.lower().split())
    if parent_task and normalized(task)==normalized(parent_task):
        return {'action':'local','reason':'相同问题不能原样递归委派'}
    if not acceptance.strip():
        return {'action':'local','reason':'缺少可检查的交付条件；先明确输出再委派'}
    if len(context)>16000:
        return {'action':'local','reason':'上下文超过16000字符；请提供必要文件路径和约束，不转发完整历史'}
    rows=(evidence or {}).get('observations',[])
    rows=[r for r in rows if r.get('category')==category and r.get('model')==model and r.get('independently_graded') is True]
    groups={mode:[r for r in rows if r.get('mode')==mode] for mode in ('local','delegate')}
    if any(len(v)<3 for v in groups.values()):
        return {'action':'local','reason':'没有至少3+3个同类、同模型的独立实测样本，暂不假定委派更划算','sample_count':len(rows)}
    if any(not isinstance(r.get('elapsed_s'),(int,float)) or r['elapsed_s']<0 or not isinstance(r.get('total_usd'),(int,float)) or r['total_usd']<0 or type(r.get('passed')) is not bool for r in rows):
        return {'action':'local','reason':'收益样本缺失有效耗时、费用或评分'}
    quality={k:sum(r['passed'] for r in v)/len(v) for k,v in groups.items()}
    latency={k:statistics.median(r['elapsed_s'] for r in v) for k,v in groups.items()}
    cost={k:statistics.median(r['total_usd'] for r in v) for k,v in groups.items()}
    gain=quality['delegate']>=quality['local'] and latency['delegate']<latency['local'] and cost['delegate']<=cost['local']
    return dict(action='delegate' if gain else 'local',reason='基于同类历史实测中位数的保守选择，不保证新任务最优',quality=quality,latency=latency,cost=cost,sample_count=len(rows))

def load_evidence(path):
    if not path:return {}
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema_version')!=1 or not isinstance(data.get('observations'),list):raise ValueError('Invalid delegation evidence')
    return data
