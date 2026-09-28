"""检查落盘配置与类默认值的差异（配置迁移问题的诊断）。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentplat.llmconfig import CONFIG_PATH, LLMConfig  # noqa: E402

print(f"配置文件: {CONFIG_PATH}")
print(f"存在: {CONFIG_PATH.exists()}")
print()

if CONFIG_PATH.exists():
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    defaults = LLMConfig()
    print(f"{'字段':<22}{'落盘值':<14}{'当前默认':<14}说明")
    print("-" * 70)
    for k in sorted(set(raw) | {"max_tokens", "json_mode", "reasoning_effort"}):
        if k == "api_key":
            continue
        stored = raw.get(k, "—")
        dflt = getattr(defaults, k, "—")
        note = ""
        if k in raw and stored != dflt and not isinstance(dflt, dict):
            note = "← 落盘值覆盖了默认值"
        print(f"{k:<22}{str(stored):<14}{str(dflt):<14}{note}")

    print()
    loaded = LLMConfig.load()
    print(f"实际生效: max_tokens={loaded.max_tokens} "
          f"(期望 {defaults.max_tokens})  "
          f"{'❌ 被落盘配置覆盖' if loaded.max_tokens != defaults.max_tokens else '✅'}")
else:
    print("没有落盘配置，全部走默认值。")
