"""Concurrent task claims need compare-and-swap, not check-then-write."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile,threading
from agentlab.util import lab
from agentplat.team_coordination import Coordination


def main():
    with lab('lab-43-team-claims','团队任务认领与交接','两个成员同时读到未认领，不应都成为负责人'):
        owners=[];barrier=threading.Barrier(2)
        def broken(owner):
            available=not owners;barrier.wait()
            if available:owners.append(owner)
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(broken,['a','b']))
        before=len(owners)-1
        with tempfile.TemporaryDirectory() as td:
            store=Coordination(Path(td));job=store.job('root','create',title='implement shared API')
            def claim(owner):
                try:store.job(owner,'claim',task_id=job['id'],expected_revision=1);return 1
                except RuntimeError:return 0
            with ThreadPoolExecutor(max_workers=2) as pool:accepted=sum(pool.map(claim,['a','b']))
            after=accepted-1;assert accepted==1
            current=store.jobs()[0];store.stop_owner(current['owner'],'failed')
            assert store.jobs()[0]['status']=='blocked'
        print('[BROKEN-REPRODUCED] 先读后写导致两个成员都认为自己已认领')
        print('[FIX-APPLIED] 事务与版本检查保证唯一认领；失败成员的工作标为待处理')
        print(f'[VERIFY] duplicate_task_owners: {before} -> {after}')
        print('[TAKEAWAY] 独占任务不代表语义冲突消失，接口约定和交付仍需验证。')
    return 0


if __name__=='__main__':raise SystemExit(main())
