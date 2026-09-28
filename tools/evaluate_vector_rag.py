"""Fixed Chinese paraphrase retrieval set with 1,000 distractors; no LLM grader."""
import sys,json,time,csv
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.knowledge import KnowledgeBase
from agentplat import vector_knowledge as vectors

CASES=[
 ('restart','守护进程观察应用退出状态，并按递增退避间隔重新拉起应用；不得重放结果未知的外部操作。','程序崩了后怎样自己恢复运行？'),
 ('privacy','敏感资料可使用离线神经网络编码器在本机计算稠密向量，原始文档无需传输给第三方。','不想把公司的材料发出去，还能做语义搜索吗？'),
 ('cancel','取消信号传递至任务树所有后代，执行器终止所属进程组，并记录未完成的工具调用。','按停止以后，孙子任务也会跟着停吗？'),
 ('money','财务汇总应使用十进制定点或任意精度 Decimal，禁止二进制浮点金额累计，并测试大量有效位与抵消。','统计账单总额怎么避免小数尾巴出错？'),
 ('source','验收单元直接读取宿主管理的原件和版本摘要，作者复述与自行编写的引用不能作为独立证据。','检查答案时可以只相信写答案的人摘抄的资料吗？'),
 ('approval','宿主命令授权绑定精确命令、工作目录与会话标识；有效期内仅可消费一次，重复提交被拒绝。','允许一次之后，下次还能偷偷用同一张通行证吗？'),
 ('version','导入修订文档后撤销旧版本的检索可见性；当前分块携带原文摘要及版本标识。','更新规章以后怎样防止搜到过期条款？'),
 ('fusion','混合检索结合稀疏词项排名与稠密语义向量排名，通过倒数排名融合兼顾精确术语和同义改写。','专有名词要精确匹配，换个说法也要找得到，怎么办？'),
 ('wait','人机交互暂停期间，宿主持久化待答问题并等待事件通知，不向推理服务持续发起请求。','等我回复的时候会不会一直烧模型额度？'),
 ('isolation','并行协作者使用独立文件副本，合并前比较基础版本摘要，有重叠修改时显式报告冲突。','两个助手同时改同一个文件，怎么避免互相覆盖？'),
 ('evidence','验收通过只对测试时的文件摘要有效，交付文件发生变化后需要重新检查相关内容。','测试都绿了之后又改了一行，还能直接说完成吗？'),
 ('context','上下文压缩保存用户约束、重要决定、待办和证据引用，较旧工具输出转存外部并保留回取路径。','聊天越来越长时怎么腾地方又不忘关键要求？'),
]

def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=False)
    source=out/'corpus.csv'
    with source.open('w',encoding='utf-8',newline='') as f:
        writer=csv.writer(f);writer.writerow(['key','content'])
        writer.writerows((key,text) for key,text,_ in CASES)
        for i in range(1000):
            writer.writerow([f'distractor-{i}',f'仓库物料台账第{i}号：本季度包装箱采购记录，运输批次{i%17}，盘点员核对数量后登记，报价随市场调整。'])
    kb=KnowledgeBase(out/'kb');kb.import_file(source)
    settings={'enabled':True,'model_path':str(vectors.ROOT/'.agent-runtime/models/bge-small-zh-v1.5')}
    with patch.object(vectors,'config',return_value=settings):
        t=time.monotonic();built=vectors.build(kb);build_seconds=time.monotonic()-t
        rows=[]
        for key,_,query in CASES:
            with patch.object(vectors,'config',return_value={}):lexical=kb.search(query,5)
            t=time.monotonic();hybrid=kb.search(query,5);elapsed=time.monotonic()-t
            def rank(result):return next((i+1 for i,h in enumerate(result['hits']) if f'key: {key} |' in h['text']),None)
            rows.append({'key':key,'query':query,'lexical_rank':rank(lexical),'hybrid_rank':rank(hybrid),'hybrid_ms':round(elapsed*1000,2)})
        report={'official_benchmark':False,'queries':len(CASES),'distractors':1000,'index':vectors.status(kb),'build_s':round(build_seconds,2),'results':rows}
        for method in ('lexical','hybrid'):
            ranks=[r[method+'_rank'] for r in rows]
            report[method]={'recall_at_5':sum(r is not None for r in ranks)/len(ranks),'mrr_at_5':sum(1/r for r in ranks if r)/len(ranks)}
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k!='results'},ensure_ascii=False))

if __name__=='__main__':main()
