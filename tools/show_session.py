"""读会话日志，看 agent 到底做了什么、为什么 workspace 是空的。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentplat.session import SessionLog, replay  # noqa: E402

path = Path(".sessions/auto.jsonl")
log, skipped = SessionLog.load(path)
print(f"日志 {path}: {len(log.events)} 条事件, 跳过 {skipped}")
print(f"事件类型: {log.summary()['by_kind']}")
print()
print("时间线：")
for ev in log.events:
    d = ev.data
    if ev.kind == "tool/call":
        target = d.get("path") or d.get("command", "")
        print(f"  [{ev.seq:>3}] tool/call   {d.get('tool','?'):<12} {str(target)[:70]}")
    elif ev.kind == "tool/result":
        print(f"  [{ev.seq:>3}] tool/result {d.get('tool','?'):<12} ok={d.get('ok')} "
              f"{str(d.get('out',''))[:60]}")
    elif ev.kind == "checkpoint/barrier":
        print(f"  [{ev.seq:>3}] barrier     {d.get('reason','')}")
    elif ev.kind == "assistant/message":
        print(f"  [{ev.seq:>3}] assistant   in={d.get('in_tokens')} out={d.get('out_tokens')} "
              f"calls={d.get('tool_calls')} finish={d.get('finish_reason')}")
    elif ev.kind == "session/created":
        print(f"  [{ev.seq:>3}] created     task={str(d.get('task',''))[:60]}")
        print(f"              workspace={d.get('workspace')}")
    elif ev.kind == "session/closed":
        print(f"  [{ev.seq:>3}] closed      finished={d.get('finished')} "
              f"iters={d.get('iterations')} usd={d.get('usd')}")
    else:
        print(f"  [{ev.seq:>3}] {ev.kind:<12} {str(d)[:70]}")

print()
st = replay(log, skipped)
st.render()
print()
print("workspace 目录内容:", [p.name for p in Path("workspace").iterdir()] if Path("workspace").exists() else "目录不存在")
