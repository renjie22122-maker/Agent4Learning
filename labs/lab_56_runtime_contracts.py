"""Execute adversarial controls rather than claiming a simulated bug existed."""
import unittest
from agentlab.util import lab
from tools.test_runtime_contracts import RuntimeContracts


def main():
    with lab('lab-56-runtime-contracts','执行与验收事实边界','拒绝不可放宽；作者总结不等于宿主验收结果'):
        from agentplat.tool_guards import ToolGuards, ToolRequest
        from agentplat.runtime import PermissionDenied
        callbacks=[lambda r,a:'denied', lambda r,a:None]
        naive=None
        for callback in callbacks: naive=callback(None,{})
        before=int(naive is None)
        guards=ToolGuards()
        for i,callback in enumerate(callbacks): guards.register(str(i),callback)
        after=1
        try: guards.check(ToolRequest('write'),{})
        except PermissionDenied: after=0
        assert before==1 and after==0
        print('[BROKEN-REPRODUCED] 教学负对照：last-wins 回调覆盖先前拒绝，并非宣称旧版存在该实现')
        print('[FIX-APPLIED] 实际 ToolGuards 在首个拒绝处终止，授权仍由执行边界强制检查')
        print(f'[VERIFY] unauthorized_calls: {before} -> {after}')
        suite=unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeContracts)
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        print('[TAKEAWAY] 负对照包括权限拒绝、回调异常、验收过期和未知评测格式。')
        return 0 if result.wasSuccessful() else 1

if __name__=='__main__':raise SystemExit(main())
