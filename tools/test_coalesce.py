"""回归测试：singleflight 合并 + 属性名冲突。

覆盖两件事：
1. **原崩溃**：子类用 `_inflight` 覆盖父类的并发计数字典，导致
   `sum(self._inflight.values())` 抛
   `TypeError: unsupported operand type(s) for +: 'int' and '_PendingCall'`。
2. **合并效果**：并发的相同请求只应产生 1 次真实上游调用，
   并且失败时不能退化成 N 次重发。

用**假客户端**替换真实网络调用，所以这个测试不发任何请求、不花钱、可离线跑。
"""

from __future__ import annotations

import os
import sys
import threading
import time

# 允许 `python tools/xxx.py` 直接跑：把仓库根加入 sys.path。
# （Python 只在 `python -m` 时才把 cwd 放进 sys.path；直接跑脚本文件时
#   放进去的是脚本所在目录，也就是 tools/，所以 import agentlab 会失败。）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentlab.providers import LLMError, user  # noqa: E402
from agentplat.guard import CostGuard  # noqa: E402
from agentplat.llm import RealLLMServer  # noqa: E402
from agentplat.llmconfig import LLMConfig, ModelRouting  # noqa: E402


class FakeClient:
    """假装是 OpenAI 客户端，记录被调用次数。"""

    def __init__(self, fail: bool = False, delay: float = 0.15):
        self.calls = 0
        self.fail = fail
        self.delay = delay
        self._lock = threading.Lock()

    def complete(self, model, messages, timeout_s):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)  # 制造重叠窗口
        if self.fail:
            raise LLMError("503", "伪造的上游故障", 0.0, retryable=True)
        from agentlab.providers import Usage
        return f"回答-{self.calls}", Usage(100, 20, 0)


def make_server(fail: bool = False, guard=None) -> tuple[RealLLMServer, FakeClient]:
    cfg = LLMConfig(
        provider="real", base_url="https://example.invalid/v1", api_key="sk-fake",
        model="fake-model", routing=ModelRouting("fake-model", "fake-model", "fake-model"),
        timeout_s=5.0, offline_mock_fallback=False, max_parallel=8,
    )
    srv = RealLLMServer(cfg, guard=guard)
    fake = FakeClient(fail=fail)
    srv.client = fake
    return srv, fake


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  {detail}" if detail else ""))
    return ok


def main() -> int:
    print("=" * 76)
    print("  singleflight 合并 / 属性名冲突 回归测试（不出网、不花钱）")
    print("=" * 76)
    passed = True

    # ---------- 1. 并发计数没有被破坏（原崩溃点）----------
    print("\n[1] 父类并发计数（原崩溃点）")
    srv, fake = make_server()
    try:
        # 这一步在修复前会抛 TypeError
        inflight_snapshot = dict(srv._inflight)
        passed &= check("`_inflight` 仍是 dict[str,int]", 
                        all(isinstance(v, int) for v in inflight_snapshot.values()),
                        f"值 = {inflight_snapshot}")
        passed &= check("`_coalesce` 是独立属性",
                        hasattr(srv, "_coalesce") and srv._coalesce is not srv._inflight)
        reply = srv.call([user("你好")], model="mid-32b", timeout=8.0)
        passed &= check("call() 正常返回", reply.ok if hasattr(reply, "ok") else bool(reply.text),
                        f"text={reply.text!r}")
        passed &= check("请求结束后并发计数归零",
                        sum(srv._inflight.values()) == 0, f"{srv._inflight}")
    except TypeError as exc:
        passed &= check("未复现 TypeError", False, str(exc))
    except Exception as exc:  # noqa: BLE001
        passed &= check("call() 未抛异常", False, f"{type(exc).__name__}: {exc}")

    # ---------- 2. 合并：8 个并发相同请求 → 1 次上游调用 ----------
    print("\n[2] 并发相同请求应被合并")
    srv2, fake2 = make_server()
    results: list = []

    def worker():
        try:
            results.append(srv2.call([user("同一个问题")], model="mid-32b", timeout=10.0))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok_count = sum(1 for r in results if not isinstance(r, Exception))
    passed &= check("8 个并发请求都拿到结果", ok_count == 8, f"成功 {ok_count}/8")
    passed &= check("上游只被调用 1 次", fake2.calls == 1,
                    f"实际 {fake2.calls} 次（修复前应是 8 次）")
    passed &= check("统计到合并数", srv2.coalesced == 7, f"coalesced={srv2.coalesced}")
    texts = {getattr(r, "text", "") for r in results if not isinstance(r, Exception)}
    passed &= check("所有等待者拿到同一份结果", len(texts) == 1, f"{texts}")

    # ---------- 3. 领头失败：等待者不各自重发 ----------
    print("\n[3] 领头失败时，等待者不应各自重发")
    srv3, fake3 = make_server(fail=True)
    errs: list = []

    def worker_fail():
        try:
            srv3.call([user("会失败的问题")], model="mid-32b", timeout=10.0)
            errs.append("unexpected-success")
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=worker_fail) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    passed &= check("6 个请求都失败（未被兜底伪装成功）",
                    len(errs) == 6 and all(isinstance(e, Exception) for e in errs))
    passed &= check("上游仍只被调用 1 次（没有 1+N 放大）", fake3.calls == 1,
                    f"实际 {fake3.calls} 次")
    codes = {getattr(e, "code", type(e).__name__) for e in errs}
    passed &= check("等待者拿到的是同一个错误码", codes == {"503"}, f"{codes}")

    # ---------- 4. 护栏在真实路径上生效 ----------
    print("\n[4] 成本护栏拦截")
    g = CostGuard(max_calls=2, max_usd=99.0)
    srv4, fake4 = make_server(guard=g)
    for i in range(3):
        try:
            srv4.call([user(f"问题{i}")], model="mid-32b", timeout=8.0)
            print(f"    第 {i+1} 次: 放行")
        except Exception as exc:  # noqa: BLE001
            print(f"    第 {i+1} 次: 拦下 -> {str(exc)[:58]}")
    passed &= check("超过调用上限后被拦下", fake4.calls == 2, f"上游被调用 {fake4.calls} 次")

    print("\n" + "=" * 76)
    print("  结论：" + ("全部通过 ✅" if passed else "存在失败项 ❌"))
    print("=" * 76)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
