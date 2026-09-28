"""中止正在跑的 agent 任务，并确认它真的停了。"""

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8800"


def fetch(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


st, body = fetch("/agent")
if st != 200:
    print("页面取不到:", body[:200])
    sys.exit(1)

# 判断是否有任务在跑
running = 'class="v warn">running' in body or "running</div>" in body
print("有任务在跑:", running)

if running:
    try:
        urllib.request.urlopen(BASE + "/agent/stop", timeout=20)
    except Exception:  # noqa: BLE001
        pass
    print("已发中止请求，等待停在步骤边界…")
    for i in range(20):
        time.sleep(1.5)
        st, body = fetch("/agent")
        if "running</div>" not in body:
            print(f"  第 {i + 1} 次检查：已停止")
            break
    else:
        print("  仍未停止（可能在等一次模型调用返回）")
else:
    print("没有正在跑的任务")

# 提取状态摘要
for pat in (r"状态</div><div class=\"v[^\"]*\">([^<]+)",
            r"已运行</div><div class=\"v[^\"]*\">([^<]+)"):
    m = re.search(pat, body)
    if m:
        print("  ", m.group(1).strip())
m = re.search(r"(\d+)\s*步", body)
if m:
    print("   步数:", m.group(1))
