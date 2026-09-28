"""Parents waiting for children must not hold the only model execution slot."""
from concurrent.futures import ThreadPoolExecutor,TimeoutError
from agentlab.util import lab
from tools.test_team_memory import TeamMemoryTests


def main():
    with lab('lab-41-recursive-scheduling','递归委派与线程饥饿','任务生命周期槽位不能等同于模型请求并发槽位'):
        with ThreadPoolExecutor(max_workers=1) as pool:
            def parent():
                child=pool.submit(lambda:True)
                try:return child.result(timeout=.1)
                except TimeoutError:return False
            before=int(pool.submit(parent).result(timeout=2))
        # Reuse the product-level fixture: actual CodingAgent -> spawn -> wait
        # -> grandchild finish with a single model slot, not a mock scheduler.
        TeamMemoryTests('test_grandchild_with_one_model_slot').test_grandchild_with_one_model_slot()
        after=1
        print('[BROKEN-REPRODUCED] 唯一工作线程被等待子任务的父任务占用，子任务无法启动')
        print('[FIX-APPLIED] 限制任务总数，同时仅在模型请求期间占用并发槽')
        print(f'[VERIFY] nested_task_success: {before} -> {after}')
        print('[TAKEAWAY] 提高递归深度必须同时解决调度、权限继承和团队预算。')
    return 0


if __name__=='__main__':raise SystemExit(main())
