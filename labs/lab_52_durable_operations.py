"""A new trace is not a new business operation; crashes require reconciliation."""
from pathlib import Path
import tempfile
from agentlab.util import lab
from agentplat.operation_ledger import OperationLedger
from agentplat.tools import ToolResult, ToolError


def main():
    with lab('lab-52-durable-operations','跨重启副作用去重','trace ID 不能替代逻辑操作 ID'):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'operations.db';effects=[]
            def charge():effects.append('charge');return ToolResult(True,'receipt')
            # A process-local cache disappears when the gateway restarts.
            charge();charge();before=len(effects)-1;effects.clear()
            OperationLedger(path).execute(['tenant','user','order-1'],{'amount':10},charge)
            OperationLedger(path).execute(['tenant','user','order-1'],{'amount':10},charge)
            after=len(effects)-1
            def uncertain():raise RuntimeError('lost response')
            try:OperationLedger(path).execute(['order-2'],{},uncertain)
            except RuntimeError:pass
            try:OperationLedger(path).execute(['order-2'],{},charge)
            except ToolError as error:assert error.code=='OUTCOME_UNKNOWN'
            else:raise AssertionError('unknown side effect replayed')
            assert before==1 and after==0
            print('[BROKEN-REPRODUCED] 新 trace 和新进程丢失内存去重状态，重复执行同一业务操作')
            print('[FIX-APPLIED] 宿主逻辑 ID 持久化；结果未知保持待核对，不自动重放')
            print(f'[VERIFY] duplicate_effects: {before} -> {after}')
            print('[TAKEAWAY] 成功结果可以复用；未知结果需要外部对账。账本不是任意 shell 的 exactly-once 保证。')
    return 0


if __name__=='__main__':raise SystemExit(main())
