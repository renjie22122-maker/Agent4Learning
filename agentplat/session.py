"""会话事件日志 + 持久化 + 检查点屏障。

要解决的问题
------------
agent 跑到一半进程挂了，之前所有进度就没了 —— 用户重跑一次，钱和时间全部重付。
本项目实测：一次编码任务 19~45 轮、$0.07~$0.38，重跑一次就是再花一遍。

为什么是"事件日志"而不是"定期存快照"
------------------------------------
快照有歧义：崩在两次快照之间时，你不知道磁盘上的状态对应哪一步。
而**只追加的事件日志**没有这个歧义 —— 日志里有什么，就是确定发生过什么。
每一步（模型请求前 / 有副作用的工具前 / 下一步前）追加一条事件并 flush，
恢复时重放日志即可重建到崩溃前一刻。这就是 DSH `dsh-session-persistence-jsonl`
+ `dsh-session-checkpoint-policy` 的做法。

三个检查点屏障（顺序很重要）
----------------------------
1. **模型请求前** —— flush 完才允许发请求。否则崩溃后重放会重发一个
   日志里不存在的请求，计费对不上。
2. **有副作用的工具执行前** —— flush 完才允许写文件/跑命令。
   否则崩溃后会**重放副作用**（写一半的文件、跑两次的命令）。
3. **下一步前** —— flush 完才允许进入下一个 step。

第 2 条是这里最关键的设计：**持久化失败必须阻止副作用，而不是先干了再说。**
代价是每次写文件/跑命令多一次 fsync；收益是崩溃后不会出现"文件改了但
日志里没有"这种无法归因的状态。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: 事件类型。命名对齐 DSH 的会话事件语义（step/start、tool/call、…），
#: 因为"可回放"这件事需要事件有稳定语义，而不是随手打的日志。
EVENT_KINDS = (
    "session/created",
    "goal/created",
    "step/start",
    "assistant/message",
    "tool/call",
    "tool/result",
    "spill/created",
    "checkpoint/barrier",
    "step/end",
    "turn/stopping",
    "session/closed",
)


@dataclass
class Event:
    seq: int
    kind: str
    ts: float
    data: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"seq": self.seq, "kind": self.kind, "ts": round(self.ts, 6),
             "data": self.data},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, line: str) -> "Event":
        d = json.loads(line)
        return cls(seq=d["seq"], kind=d["kind"], ts=d.get("ts", 0.0),
                   data=d.get("data", {}))


class CheckpointError(RuntimeError):
    """持久化失败。**必须阻止后续副作用** —— 不允许"先干了再说"。"""


class SessionLog:
    """只追加的 JSONL 事件日志。损坏容忍：坏行跳过而不是整份作废。"""

    def __init__(self, path: Path, session_id: str | None = None,
                 fsync: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.fsync = fsync
        self._lock = threading.RLock()
        self._seq = 0
        self.events: list[Event] = []
        self.flushes = 0
        self.barriers = 0
        self.write_failures = 0
        #: 每次成功落盘后调用的观察者（用作运行时不变量的事件源）。
        #: 空列表时零开销 —— 不开不变量检查的场景不该为此付代价。
        self.observers: list[Any] = []

    # -- 写 ----------------------------------------------------------------
    def append(self, kind: str, **data: Any) -> Event:
        with self._lock:
            ev = Event(seq=self._seq + 1, kind=kind, ts=time.time(), data=data)
            self._write(ev)
            self._seq = ev.seq
            self.events.append(ev)
        # ★ 观察者放在**锁外、写盘之后**：
        #   · 放锁内会让检查代码持锁执行，一个慢检查就卡住整个会话写入；
        #   · 放写盘之前，检查的就是"打算记的事"而不是"已经记下的事" ——
        #     写入失败时日志与状态不一致，而检查却说没事。
        # 观察者抛异常不吞（吞掉的话不变量就静默失效了，比没有还糟），
        # 但也不在这里处理 —— 由调用方（注册表）决定是收集还是上抛。
        for obs in list(self.observers):
            obs(ev)
        return ev

    def project_run(self):
        from .run_projection import project
        with self._lock:
            return project(tuple(self.events))

    def _write(self, ev: Event) -> None:
        """追加一行并（可选）fsync。**失败必须抛，不能吞**。

        ⚠ 必须处理"文件结尾没有换行符"的情况。
        崩溃常常发生在写入一行的中途，于是文件末尾会留下一个**没有换行的半行**。
        此时若直接以 append 模式写入，新事件会被拼到那半行后面，
        结果是**两条事件粘成一行、两条都作废** —— 实测踩到过：

            {"seq": 3, "kind": "step/sta{"seq": 3, "kind": "step/end", ...

        这样丢的不是一行，而是"崩溃前那一行 + 崩溃后刚写的那一行"。
        代价极小的补救：写之前检查结尾是否有换行，没有就先补一个。
        """
        try:
            prefix = ""
            if self.path.exists() and self.path.stat().st_size > 0:
                with open(self.path, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        prefix = "\n"
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(prefix + ev.to_json() + "\n")
                if self.fsync:
                    f.flush()
                    os.fsync(f.fileno())
        except OSError as exc:
            self.write_failures += 1
            raise CheckpointError(
                f"会话日志写入失败（{self.path}）：{exc}。"
                f"**已阻止后续操作** —— 持久化不可靠时继续执行会导致崩溃后无法归因。"
            ) from None

    def flush(self, reason: str = "") -> None:
        """屏障：确保此前的写入都已落盘。

        单独提供（而不是只在 append 里 fsync）是为了让"屏障"成为**显式语义**：
        调用点一眼能看出"这里在保护副作用"。
        """
        with self._lock:
            if self.fsync:
                try:
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.flush()
                        os.fsync(f.fileno())
                except OSError as exc:
                    self.write_failures += 1
                    raise CheckpointError(f"checkpoint flush 失败：{exc}") from None
            self.flushes += 1
            self.barriers += 1
            self.append("checkpoint/barrier", reason=reason)

    # -- 读 ----------------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> tuple["SessionLog", int]:
        """恢复一份日志。返回 (日志, 跳过损坏行数)。"""
        path = Path(path)
        log = cls(path, fsync=False)
        skipped = 0
        if not path.exists():
            return log, 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = Event.from_json(line)
                except Exception:  # noqa: BLE001 - 半行写入是崩溃的常见后果
                    skipped += 1
                    continue
                log.events.append(ev)
                log._seq = max(log._seq, ev.seq)
        for ev in log.events:
            if ev.kind == "session/created":
                log.session_id = ev.data.get("session_id", log.session_id)
                break
        return log, skipped

    @classmethod
    def open(cls, path: Path, session_id: str | None = None,
             fsync: bool = True) -> tuple["SessionLog", int]:
        """**打开已有日志并继续追加**（恢复场景的正确入口）。

        为什么要单独提供：``SessionLog(path)`` 是"新建"，``_seq`` 从 0 开始，
        追加时会让 seq 从 1 重新计数，与文件里已有事件冲突 —— 实测就踩到了
        （一个已经写到 seq=2 的日志，重启后新事件又从 1 开始）。

        约定：**要续写一份已有日志，就用 ``open()``；要开新会话，才用构造函数。**
        """
        log, skipped = cls.load(path)     # load 是只读的，fsync 无意义
        log.fsync = fsync
        if session_id:
            log.session_id = session_id
        return log, skipped

    # -- 查询 --------------------------------------------------------------
    def of_kind(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]

    @property
    def last_seq(self) -> int:
        return self._seq

    def summary(self) -> dict:
        kinds: dict[str, int] = {}
        for e in self.events:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        return {"session_id": self.session_id, "events": len(self.events),
                "by_kind": kinds, "flushes": self.flushes,
                "write_failures": self.write_failures,
                "path": str(self.path)}


@dataclass
class ResumableState:
    """从日志重放出来的、可以继续跑的状态。"""

    session_id: str
    iterations_done: int = 0
    tool_calls_done: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    files_written: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    finished: bool = False
    last_step: int = 0
    skipped_lines: int = 0
    unknown_calls: list[dict] = field(default_factory=list)
    failed_calls: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)

    def render(self) -> None:
        from agentlab.util import kv, note, phase

        phase("断点续跑状态", f"(session {self.session_id})")
        kv("已完成轮数", self.iterations_done)
        kv("已完成工具调用", self.tool_calls_done)
        kv("累计 token", f"in {self.tokens_in:,} / out {self.tokens_out:,}")
        kv("已花费", f"${self.usd:.6f}")
        kv("已改文件", len(self.files_written))
        kv("已跑命令", len(self.commands_run))
        kv("是否已完成", self.finished)
        if self.skipped_lines:
            note(f"（日志里有 {self.skipped_lines} 行损坏被跳过 —— 崩溃时的半行写入）")


def replay(log: SessionLog, skipped: int = 0) -> ResumableState:
    """重放日志，重建可续跑状态。

    **重放必须是纯函数**：只读日志、不改任何外部状态。特别是不能"重放副作用" ——
    日志里的 ``tool/call`` 是记录，不是指令。恢复后要继续的是**剩下的工作**，
    而不是把已经做过的再做一遍。
    """
    st = ResumableState(session_id=log.session_id, skipped_lines=skipped)
    seen_steps: set[int] = set()
    pending = {}
    for ev in log.events:
        k, d = ev.kind, ev.data
        if k == "step/start":
            seen_steps.add(int(d.get("iteration", 0)))
        elif k == "assistant/message":
            st.tokens_in += int(d.get("in_tokens", 0) or 0)
            st.tokens_out += int(d.get("out_tokens", 0) or 0)
            st.usd += float(d.get("usd", 0.0) or 0.0)
        elif k == "tool/call":
            pending[d.get('call_id', f'legacy-{ev.seq}')] = d
        elif k == "tool/result":
            intent = pending.pop(d.get('call_id'), None)
            if intent is None:
                continue
            st.tool_calls_done += 1
            if not d.get('ok'):
                st.failed_calls.append(intent)
                continue
            if intent.get('destructive'):
                name = intent.get('tool')
                if name in ('write_file', 'edit_file', 'append_file', 'delete_file'):
                    path = intent.get('path')
                    if path and path not in st.files_written:
                        st.files_written.append(path)
                elif name == 'run_shell':
                    st.commands_run.append(intent.get('command') or intent.get('brief', ''))
        elif k == 'conversation/message':
            st.messages.append(d['message'])
        elif k == 'conversation/snapshot':
            st.messages = d.get('messages', [])
        elif k == 'followup/user':
            st.finished = False
        elif k == "session/closed":
            st.finished = bool(d.get("finished", False))
    st.unknown_calls = list(pending.values())
    st.iterations_done = max(seen_steps) if seen_steps else 0
    st.last_step = st.iterations_done
    return st


def find_latest_session(directory: Path, unfinished_only: bool = False) -> Path | None:
    """找出最近一次的会话日志。

    ``unfinished_only=True`` 时跳过已经标记完成的会话 —— "续跑"要找的是
    **被打断的那次**，而不是最近一次（最近的很可能已经跑完了，续跑它
    只会得到"已完成"并让用户以为什么都没发生）。
    """
    directory = Path(directory)
    if not directory.exists():
        return None
    logs = sorted(directory.glob("*.jsonl"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not unfinished_only:
        return logs[0] if logs else None
    for p in logs:
        try:
            log, skipped = SessionLog.load(p)
            st = replay(log, skipped)
        except Exception:  # noqa: BLE001 - 坏日志跳过，继续找下一个
            continue
        if not st.finished and st.iterations_done > 0:
            return p
    return None
