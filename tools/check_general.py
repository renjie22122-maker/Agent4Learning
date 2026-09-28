"""验证 agent 是**通用**的：能写代码，不再因为"知识库无资料"而拒答。

这是对一次真实能力缺陷的回归测试：早期 system prompt 写的是
"只依据给定资料回答，无依据就明确说明"，叠加默认注入检索片段，
agent 就退化成只会念文档的检索器 —— 连"写个俄罗斯方块"都拒答。

三种配置都要验证：
  A. 通用人格 + 不检索   ← 默认，应该正常答
  B. 通用人格 + RAG      ← 有资料时优先用资料，无资料也能答
  C. 严格检索人格        ← **故意**保留的拒答行为，合规场景需要
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("AGENTLAB_DEMO_BASE", "http://127.0.0.1:8800")


def ask(q: str, **kw) -> dict:
    params = {"q": q, "tenant": "alpha", "format": "json", **kw}
    url = BASE + "/ask?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=180) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", "replace"))


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"\n       {detail}" if detail else ""))
    return ok


def main() -> int:
    code = urllib.request.urlopen(f"{BASE}/readyz", timeout=10).status
    print(f"/readyz -> {code}\n")
    ok = True

    print("=" * 78)
    print("  A. 通用人格 + 不检索（默认）—— 应该正常回答")
    print("=" * 78)
    r = ask("用 Python 写一个俄罗斯方块的核心下落与消行逻辑")
    print(f"  模型={r.get('model')}  延迟={r.get('latency_ms')}ms  "
          f"上下文={r.get('context_tokens')} tokens")
    ans = r.get("answer") or r.get("error") or ""
    print(f"  回答：{ans[:300]}")
    refuses = any(k in ans for k in ("无法回答", "不提供", "未返回任何相关内容",
                                     "没有可依据的资料", "知识库检索未返回"))
    ok &= check("没有拒答", not refuses)
    ok &= check("确实给出了内容", r.get("ok") and len(ans) > 40,
                f"ok={r.get('ok')} len={len(ans)}")

    print("\n" + "=" * 78)
    print("  B. 通用人格 + RAG —— 有资料优先用，资料不足也能答")
    print("=" * 78)
    # 用一个**知识库里确实有**的问题来验证 RAG 生效；否则检索为空，
    # "上下文 token 变多"这个断言本来就不成立（不是 bug，是素材问题）。
    r2 = ask("缓存穿透和缓存击穿的区别是什么", rag="1")
    ans2 = r2.get("answer") or r2.get("error") or ""
    print(f"  上下文={r2.get('context_tokens')} tokens   缓存={r2.get('cache_layer') or '未命中'}")
    print(f"  回答：{ans2[:220]}")
    refuses2 = any(k in ans2 for k in ("无法回答", "不提供", "未返回任何相关内容"))
    ok &= check("RAG 模式下也没有拒答", not refuses2)
    ok &= check("RAG 确实把检索片段注入了上下文",
                (r2.get("context_tokens") or 0) > 0,
                f"context_tokens={r2.get('context_tokens')}")
    spans2 = {s["span"]: s for s in (r2.get("spans") or [])}
    ok &= check("span 里能看到 retrieve 阶段真的执行了",
                "retrieve" in spans2 and "skipped" not in spans2["retrieve"].get("attrs", {}),
                f"retrieve attrs={spans2.get('retrieve', {}).get('attrs')}")

    print("\n" + "=" * 78)
    print("  C. 严格检索人格 —— 拒答是**设计的**，合规场景需要")
    print("=" * 78)
    r3 = ask("制作一个俄罗斯方块游戏", persona="strict_rag")
    ans3 = r3.get("answer") or r3.get("error") or ""
    print(f"  回答：{ans3[:220]}")
    print("  说明：这个人格只依据资料、无依据即拒答，是有意保留的选项，")
    print("        适用于法务/财务口径、内部制度这类「答错代价极高」的场景。")

    print("\n" + "=" * 78)
    print("  结论：" + ("通用能力已恢复 ✅" if ok else "仍有问题 ❌"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
