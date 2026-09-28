"""端到端验证：用真实 LLM 跑一个**能在纯命令行下自测**的任务。

为什么不用俄罗斯方块：那需要 pygame，而沙箱的白名单里没有 pip ——
agent 能写代码但**没法运行验证**，"证据闭环"就闭合不了。
这里换成纯标准库、能用 `python -c` 自测的任务，正好也验证了刚修的
引号感知命令切分（旧实现在这类命令上会误拒并把 agent 卡死）。

花的是真钱（DeepSeek）。默认上限 $0.25。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("AGENTLAB_DEMO_BASE", "http://127.0.0.1:8800")
ROOT = Path(__file__).resolve().parent.parent

TASK = (
    "在工作区里写一个纯标准库的模块 `rle.py`，实现游程编码的两个函数："
    "`encode(text)` 把字符串压成 [(字符, 次数), ...]，"
    "`decode(pairs)` 还原成原字符串。要求：\n"
    "1) 连续相同字符要合并；2) 空字符串返回空列表；\n"
    "3) 必须能用 `python -c` 直接跑自测（import rle; assert ...），"
    "不要依赖 pytest；\n"
    "4) 写完必须自己跑一次自测，把命令和输出贴出来。"
)


def get(path: str, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(url, timeout=600) as r:
        return r.status, r.read().decode("utf-8", "replace"), r.geturl()


def main() -> int:
    print("=" * 78)
    print("  提交任务（真实 LLM，会花钱）")
    print("=" * 78)
    print(f"  {TASK[:120]}...")
    st, body, url = get("/agent/run", task=TASK, max_iters=20, max_usd=0.25)
    if "error=" in url:
        print("  提交失败：",
              urllib.parse.unquote(url.split("error=")[1]))
        return 1
    print("  已提交")

    print("\n" + "=" * 78)
    print("  等它跑完（最多 8 分钟）")
    print("=" * 78)
    t0 = time.time()
    last = -1
    while time.time() - t0 < 480:
        time.sleep(6)
        st, body, _ = get("/agent")
        st2, snap, _ = get("/api/snapshot")
        try:
            d = json.loads(snap)
        except Exception:
            d = {}
        it = body.count("class=\"ln tool\"")
        if it != last:
            print(f"    [{time.time()-t0:5.0f}s] 工具调用约 {it} 次")
            last = it
        if "已提交" in body or "执行中" not in body:
            # 状态标签：执行中 / 已完成 / 失败 / 已停止
            for tag in ("已完成", "失败", "已停止"):
                if tag in body:
                    print(f"    [{time.time()-t0:5.0f}s] 状态：{tag}")
                    break
            else:
                continue
            break

    print("\n" + "=" * 78)
    print("  独立验证：不听 agent 的总结，自己跑")
    print("=" * 78)
    f = ROOT / "workspace" / "rle.py"
    if not f.exists():
        print(f"  ❌ 交付物不存在：{f}")
        print("     （agent 说自己成功也不算 —— 文件都没有就是没交付）")
        return 1
    print(f"  ✅ 文件存在：{f}（{f.stat().st_size} 字节）")

    sys.path.insert(0, str(f.parent))
    checks = []
    try:
        import importlib
        rle = importlib.import_module("rle")
        cases = [
            ("aaabbc -> [('a',3),('b',2),('c',1)]",
             lambda: rle.encode("aaabbc") == [("a", 3), ("b", 2), ("c", 1)]),
            ("空串 -> []", lambda: rle.encode("") == []),
            ("单字符", lambda: rle.encode("x") == [("x", 1)]),
            ("全同", lambda: rle.encode("aaaa") == [("a", 4)]),
            ("无重复", lambda: rle.encode("abc") == [("a", 1), ("b", 1), ("c", 1)]),
            ("往返：encode->decode",
             lambda: rle.decode(rle.encode("aaabbc")) == "aaabbc"),
            ("往返：空串", lambda: rle.decode(rle.encode("")) == ""),
            ("往返：长随机（边界）",
             lambda: rle.decode(rle.encode("aab" * 500)) == "aab" * 500),
        ]
        for name, fn in cases:
            try:
                checks.append((name, bool(fn()), ""))
            except Exception as exc:  # noqa: BLE001
                checks.append((name, False, f"{type(exc).__name__}: {exc}"))
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 连 import 都失败：{type(exc).__name__}: {exc}")
        return 1

    passed = 0
    for name, good, err in checks:
        print(f"  {'✅' if good else '❌'} {name}" + (f"  {err}" if err else ""))
        passed += bool(good)
    print(f"\n  独立用例：{passed}/{len(checks)} 通过")
    print("  这些用例是**我写的**，不是 agent 写的 —— "
          "它自己那套测试覆盖不到的地方，只有这里能发现。")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
