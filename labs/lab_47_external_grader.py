"""A visible happy-path test is insufficient; randomized independent checks matter."""
from pathlib import Path
import tempfile
from agentlab.util import lab
from agentplat.benchmark import grade,TASKS

def main():
    with lab('lab-47-external-grader','独立随机验收','自测全绿不是正确性的充分条件'):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);ws=root/'ws';ws.mkdir()
            bad=TASKS['median_repair']['files']['stats.py'];(ws/'stats.py').write_text(bad)
            scope={};exec(bad,scope)
            before=int(scope['median']([3,1,2])==2)
            passed,_=grade('median_repair',ws,root/'grader');after=int(passed)
            (ws/'stats.py').write_text('from statistics import median')
            good,details=grade('median_repair',ws,root/'positive-grader')
            assert good, 'Positive control failed: '+details
            assert before==1 and after==0
            print('[BROKEN-REPRODUCED] 奇数示例通过，但偶数与输入不变性有缺陷')
            print('[FIX-APPLIED] Agent 结束后用外部随机边界断言验收')
            print(f'[VERIFY] false_accepts: {before} -> {after}')
            print('[TAKEAWAY] 把模型声明与外部可执行判定分开记录。')
    return 0

if __name__=='__main__':raise SystemExit(main())
