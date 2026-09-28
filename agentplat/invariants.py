"""运行时不变量注册表 —— 对齐 DSH `@deepseek-ai/dsh-invariants`。

## 为什么需要"不变量"这一层

测试回答的是"我想到的那些情况对不对"；不变量回答的是"**不可能发生的事有没有发生**"。

两者不能互相替代：
  - 测试是**抽样**的。一个 45 轮的 agent 会话会走出 10^40 种可能的路径，
    你只能测其中几条。测不到的路径上出现状态损坏，测试永远发现不了。
  - 不变量是**全称**的。它挂在事件流上，每一个事件都过一遍。
    "每个 tool/call 都有配对的 tool/result" 这句话要么对所有事件成立，
    要么立刻报错 —— 不存在"我只检查了前三条"。

这在本项目里不是理论问题：真实的 agent 跑 45 轮产生了 300+ 个会话事件，
其中有 20 次 `finish_reason=length` 的截断。截断会让 assistant 的
`tool_calls` 变成半截 JSON —— 这正是"协议层面不可能发生的事"。
不变量就是把这类事故从"靠人翻日志发现"变成"跑到那一步就炸"。

## 与 DSH 的逐条对应

| DSH | 本项目的对应 |
| --- | --- |
| `InvariantError(packageName, message)`，稳定 `code="INVARIANT"` | 同名字段与语义：失败必须能**归因到某个包/模块** |
| `InvariantRegistry.register(packageName, installer)` | `register(owner, installer)` |
| 重复注册同名包 → 立刻报错 | 同（包名被永久保留，见下） |
| 包名即使被过滤器禁用也被保留 | `_names` 与 `_active` 分离 |
| `package_allowlist` / `package_blocklist`（正则） | 同，用 `re` |
| `enabled` 总开关 | 同 |
| 注册表**不导入任何产品代码** | 同：本模块只认识 `Reporter`，不认识 session/loop |
| 检查放在"伴生入口"里，不散落在产品代码 | `session_invariant.py` / `loop_invariant.py` / `compaction_invariant.py` |
| installer 失败 → 释放注册、回滚 | `install()` 失败时丢弃半成品注册 |
| 只检查"可观察的事件或可变数据关系" | 见三个伴生模块的 docstring |

## 与 DSH 的两处**有意分歧**（是取舍，不是更好）

1. **DSH 用 Cordis fiber 做生命周期与依赖注入，本模块用纯 Python 的
   `Registration` 对象 + 显式 `close()`。** 本项目零第三方依赖，
   没有 DI 容器；代价是"依赖没就绪就注册"这种错误要到运行时才暴露，
   而 DSH 的 `inject` 声明能在装配期就发现。
2. **DSH 的 installer 在插件加载时就运行；本模块的 `install()` 在
   `audit()` 时才调用。** 好处是"先装完再统一跑"更容易复现和开关，
   坏处是**注册得晚**：如果某个模块在 audit 之后才注册，
   它的检查就不会在本轮生效。`audit()` 因此对**快照**迭代，
   并在报告里写明本轮实际生效的模块清单 —— 不写清单的话，
   "没有报错"和"没有检查"看起来是一样的。

## 默认不飞异常，而是收集

DSH 的 reporter 直接 `throw`。本模块默认 `strict=False`：把违规**收集**起来，
在 `checkpoint()` / `audit()` 时统一抛出。

理由：agent 的循环很长，一次"call/result 不配对"往往后面还有 5 条别的
违规 —— 只报第一条会让你修 6 轮。收集起来一次给全，修起来快得多。
需要 fail-fast 的场合（比如 CI 里希望第一时间定位）就传 `strict=True`。
这一点也在 `Reporter.fail()` 的 docstring 里写清楚了，因为它决定了
"抛出的异常是第几条违规"。
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: 稳定的机器可读失败码（对齐 DSH 的 `code = "INVARIANT"`）。
INVARIANT_CODE = "INVARIANT"


class InvariantError(RuntimeError):
    """某个模块拥有的运行时不变量被违反。

    字段与 DSH 的 `InvariantError` 对齐：`code` 稳定、`owner` 指明归属。
    归属之所以重要：**注册表自己不知道是谁的契约坏了**，它只负责喊出来。
    如果错误信息里没有 owner，你就得从头翻是谁注册的这条检查。
    """

    code = INVARIANT_CODE

    def __init__(self, owner: str, message: str, violations: Sequence["Violation"] = ()):
        super().__init__(f'invariant violated by "{owner}": {message}')
        self.name = "InvariantError"
        self.owner = owner
        self.package_name = owner  # 兼容 DSH 的字段名
        self.violations = list(violations)


@dataclass(frozen=True)
class Violation:
    """一条被违反的契约。"""

    owner: str
    check: str
    message: str
    event_seq: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        where = f" @seq={self.event_seq}" if self.event_seq is not None else ""
        return f'  ✗ [{self.owner}] {self.check}{where}: {self.message}'

    def key(self) -> str:
        """去重键：同一个 (模块, 检查名) 只保留第一条最有用。

        为什么去重：一个坏掉的不变量往往在**每个**事件上都触发
        （比如"token 必须单调不减"在大历史里会连炸几百次）。
        不去重的话报告会被同一个问题刷屏，真正的新问题反而被淹掉。
        每类的**触发次数**仍然记在 `AuditReport.counts` 里 ——
        次数本身是信息（炸 1 次可能是抖动，炸 300 次是系统性问题）。
        """
        return f"{self.owner}::{self.check}"


class Reporter:
    """交给 installer 的句柄：既用来**报告违规**，也用来**挂钩子**。"""

    def __init__(self, registry: "InvariantRegistry", reg: "Registration"):
        self._registry = registry
        self._reg = reg

    # -- 报告 ---------------------------------------------------------------
    @property
    def registry(self) -> "InvariantRegistry":
        """这个 reporter 归属的注册表。

        伴生模块需要它来做**跨模块**的接线（比如把"读账本"的函数挂到
        某个具体注册表上）。暴露出注册表本身是刻意的：检查代码不该
        通过模块级全局变量互相找对方 —— 那样多个注册表并存时会串台。
        """
        return self._registry

    @property
    def strict(self) -> bool:
        return self._registry.strict

    def fail(self, message: str, *, check: str = "", event_seq: int | None = None,
             **detail: Any) -> None:
        """报告一次违规。

        - ``strict=True``：立刻抛 `InvariantError`（只带这一条）。
        - ``strict=False``（默认）：收集起来，等 `checkpoint()` / `audit()` 抛。
        """
        v = Violation(
            owner=self._reg.owner,
            check=check or self._reg.owner,
            message=message,
            event_seq=event_seq,
            detail=detail,
        )
        self._registry._record(v)
        if self.strict:
            raise InvariantError(v.owner, v.message, [v])

    # -- 挂钩子 -------------------------------------------------------------
    def on(self, kind: str, fn: Callable[[dict[str, Any]], None]) -> None:
        """挂一个检查到某种事件上。`fn` 收到事件的 ``data`` 字典。

        `fn` 抛出的**非** `InvariantError` 异常会被注册表包装成违规 ——
        检查代码自己写挂了，也必须算"不变量没守住"，
        否则一个 `KeyError` 会让检查静默失效，比没有检查更糟。
        """
        self._reg.hooks.setdefault(kind, []).append((self._reg.owner, fn))

    def on_snapshot(self, fn: Callable[[], None]) -> None:
        """挂一个"读当前状态"的检查（不挂在事件流上）。

        用于"可变数据关系"：比如 `guard.spent_usd` 必须等于会话日志里
        每次记账之和。这种关系不体现在单个事件上，只能读快照。
        """
        self._reg.snapshots.append((self._reg.owner, fn))

    def on_reset(self, fn: Callable[[], None]) -> None:
        """挂一个"清空累积状态"的回调，`audit()` 开始时调用。

        **不挂这个会得到一个很隐蔽的错误**：检查器持有的累积状态
        （"我见过哪些 seq / 哪些 call_id"）在多次 `audit()` 之间不会清空，
        于是第二次审计从第一次的残留状态继续 —— 报出来的违规指向
        上一次的事件，而你以为它在说这一次。

        本项目实测踩到：注册表被复用后，第二次审计凭空报出
        "日志里没有 session/created"，因为 `created` 计数器还停在上一次的值。
        """
        self._reg.resets.append((self._reg.owner, fn))


@dataclass
class Registration:
    """一次注册。`close()` 撤销它（对应 DSH 的 effect-scoped disposer）。"""

    owner: str
    active: bool
    hooks: dict[str, list[tuple[str, Callable[[dict[str, Any]], None]]]] = \
        field(default_factory=dict)
    snapshots: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    resets: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    #: 每条钩子被调用了多少次 —— 用于"检查到底跑了没有"。
    fired: dict[str, int] = field(default_factory=dict)
    closed: bool = False

    def close(self) -> None:
        self.closed = True
        self.hooks.clear()
        self.snapshots.clear()
        self.resets.clear()


@dataclass
class AuditReport:
    """一次 `audit()` 的结果。"""

    violations: list[Violation] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    checked_events: int = 0
    active_owners: list[str] = field(default_factory=list)
    disabled_owners: list[str] = field(default_factory=list)
    hooks_fired: dict[str, int] = field(default_factory=dict)
    #: 这份日志里出现过的 `kind` 的 **live** 计数（重放也走 dispatch，所以算在内）。
    kinds_seen: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def silent_checks(self) -> list[str]:
        """**应该**跑但一次都没跑到的钩子（`owner:kind` 形式）。

        判定标准：某个 `kind` 在这份日志里**确实出现过**，
        但注册在它上面的钩子计数是 0。这只可能是接线错了 ——
        事件名拼错、钩子没挂上、或者留着上个版本的事件名。

        这是最危险的一类问题：检查写好了、注册上了、也"没报错"，
        但因为**从未执行**，"没有违规"和"没有检查"在结果上完全一样。
        本项目真的踩过：`dispatch` 只传 data 不传 kind 时，
        每个钩子都退化成空转，而报告上只有一条被去重合并掉的
        "检查自身异常"。
        """
        out = []
        for owner, reg in self._regs.items():
            if not reg.active:
                continue
            for kind, fns in reg.hooks.items():
                if kind not in self.kinds_seen:
                    continue          # 这份日志没有这种事件，不算接线错
                for _o, fn in fns:
                    key = f"{kind}:{getattr(fn, '__name__', fn)}"
                    if reg.fired.get(key, 0) == 0:
                        out.append(f"{owner}:{kind}")
        return out

    @property
    def unexercised_checks(self) -> list[str]:
        """日志里没有对应事件、因此**这轮没被验证**的钩子。

        这不是错误，但必须**报出来**：否则"我检查了 8 类事件"
        和"我检查了 3 类事件、另外 5 类没有素材"看起来是一样的。
        """
        out = []
        for owner, reg in self._regs.items():
            if not reg.active:
                continue
            for kind in reg.hooks:
                if kind not in self.kinds_seen:
                    out.append(f"{owner}:{kind}")
        return out

    _regs: dict[str, Registration] = field(default_factory=dict, repr=False)

    def render(self) -> str:
        lines = [
            f"检查了 {self.checked_events} 个事件；生效模块 "
            f"{len(self.active_owners)} 个：{', '.join(self.active_owners) or '（无）'}",
        ]
        if self.disabled_owners:
            lines.append(f"被过滤器禁用：{', '.join(self.disabled_owners)}")
        if self.silent_checks:
            lines.append(f"⚠ 静默失效的钩子（事件出现过但钩子没跑）："
                         f"{', '.join(self.silent_checks)}")
        if self.unexercised_checks:
            lines.append(f"ℹ 这份日志没有对应事件、本轮未验证："
                         f"{', '.join(self.unexercised_checks)}")
        if not self.violations:
            lines.append("✅ 没有违反任何运行时不变量")
        else:
            for v in self.violations:
                lines.append(v.render())
            lines.append("  各类触发次数：")
            for k, n in sorted(self.counts.items()):
                lines.append(f"    {k}: {n} 次")
        return "\n".join(lines)


class InvariantRegistry:
    """模块自己拥有的运行时不变量注册表。

    **这个类不认识 session、loop、compaction 里的任何一个类型** ——
    它只认识 `Reporter`。检查属于拥有那份契约的模块，
    放在它们旁边的 `*_invariant.py` 里。这条边界是刻意的：
    注册表一旦开始 import 产品代码，它就会慢慢长成第二个产品。
    """

    def __init__(self, *, enabled: bool = True,
                 allowlist: Iterable[str] = (),
                 blocklist: Iterable[str] = (),
                 strict: bool = False):
        self.enabled = enabled
        self.strict = strict
        self.allowlist = _compile_patterns("allowlist", allowlist)
        self.blocklist = _compile_patterns("blocklist", blocklist)
        self._names: set[str] = set()      # 保留的名字（即使被禁用）
        self._active: dict[str, Registration] = {}
        self._violations: list[Violation] = []
        self._counts: dict[str, int] = {}
        self._lock = threading.RLock()

    # -- 选择 ---------------------------------------------------------------
    def selected(self, owner: str) -> bool:
        if not self.enabled:
            return False
        if self.allowlist and not any(p.search(owner) for p in self.allowlist):
            return False
        return not any(p.search(owner) for p in self.blocklist)

    # -- 注册 ---------------------------------------------------------------
    def register(self, owner: str, installer: Callable[[Reporter], None]) -> Registration:
        """注册一个模块的不变量。

        `installer` 会收到一个 `Reporter`，用它 `fail()` 报违规、`on()` 挂钩子。

        名字**先保留再判断启用**：即使这个模块被过滤器禁用，名字也已经被占用，
        所以两个模块永远不会静默地共用一个名字（对齐 DSH 的注册保留归属）。
        """
        if not owner or owner != owner.strip() or re.search(r"\s", owner):
            raise ValueError(f"invariants: owner 不能为空或含空白字符：{owner!r}")
        with self._lock:
            if owner in self._names:
                raise ValueError(f'invariants: 模块 "{owner}" 已经注册过了')
            self._names.add(owner)
            active = self.selected(owner)
            reg = Registration(owner=owner, active=active)
            reporter = Reporter(self, reg)
            try:
                installer(reporter)
            except Exception:
                # 半成品注册必须回滚：否则一个写坏的 installer 会留下一批
                # 只挂了一半的钩子，后面报出来的违规指向一个根本不完整的检查。
                reg.close()
                self._names.discard(owner)
                raise
            if active:
                self._active[owner] = reg
            return reg

    def unregister(self, owner: str) -> None:
        with self._lock:
            reg = self._active.pop(owner, None)
            if reg is not None:
                reg.close()

    # -- 报告 ---------------------------------------------------------------
    def _record(self, v: Violation) -> None:
        with self._lock:
            self._violations.append(v)
            self._counts[v.key()] = self._counts.get(v.key(), 0) + 1

    @property
    def violations(self) -> list[Violation]:
        with self._lock:
            return list(self._violations)

    def checkpoint(self) -> None:
        """如果已经有违规，现在抛出。**在关键边界调用它**。

        用在哪里：一步结束、一个副作用执行前、上下文压缩之后。
        位置选得好，就能让"状态已经坏了"在**坏消息传播开之前**被拦住 ——
        否则你会看到 20 轮之后才炸出来的、跟真正原因隔着十万八千里的报错。
        """
        with self._lock:
            if not self._violations:
                return
            vs = list(self._violations)
            self._violations.clear()
        first = vs[0]
        raise InvariantError(
            first.owner,
            f"{len(vs)} 条违规，第一条：{first.check}: {first.message}",
            vs,
        )

    # -- 分发 ---------------------------------------------------------------
    def dispatch(self, ev: Any) -> None:
        """把一个事件喂给所有挂了这个 kind 的检查。

        **钩子收到的永远是完整的 Event 对象**（有 `.kind` / `.data` / `.seq`）。
        只传 `data` 是个陷阱：钩子拿不到 `kind`，于是它得猜自己是"被哪个
        事件调用的"，而猜错的表现是**静默失效**（所有分支都不命中，
        报告上一切正常）。本项目真的踩了这条 —— 详见
        `session_invariant.on_event` 里的说明。

        调用点应该在**事件落盘之后**：不变量检查的是"已经发生的事"，
        而不是"打算发生的事"。放错位置的话，一条因为写入失败而根本没
        落盘的事件也会被检查，于是日志与实际状态不一致却查不出来。
        """
        if not self.enabled:
            return
        kind = getattr(ev, "kind", None)
        if kind is None:
            raise TypeError(
                "dispatch() 需要一个带 .kind 的事件对象（会话 Event）；"
                "只传 (kind, data) 会让钩子丢掉 kind")
        seq = getattr(ev, "seq", None)
        with self._lock:
            targets = [(reg, fns) for reg in self._active.values()
                       for fns in [reg.hooks.get(kind, [])] if fns]
        for reg, fns in targets:
            for owner, fn in list(fns):
                fname = getattr(fn, "__name__", str(fn))
                with self._lock:
                    reg.fired[f"{kind}:{fname}"] = reg.fired.get(f"{kind}:{fname}", 0) + 1
                try:
                    fn(ev)
                except InvariantError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # 检查自己崩了 = 不变量没守住。包装成违规而不是放它穿过：
                    # 放它穿过的话，一个 KeyError 会让这条检查从这一刻起静默失效。
                    self._record(Violation(
                        owner=owner, check=f"{kind}(检查自身异常)",
                        message=f"{type(exc).__name__}: {exc}", event_seq=seq,
                    ))

    # -- 审计 ---------------------------------------------------------------
    def audit(self, events: Iterable[Any] = (), *,
              clear: bool = True) -> AuditReport:
        """跑一遍全部检查：先重放事件，再跑快照检查。

        `events` 是会话事件（只需要有 `.kind` 和 `.data` / `.seq` 两个属性）。
        跑快照检查放在最后：它们看的是"所有事件都应用之后"的状态。
        """
        report = AuditReport()
        with self._lock:
            regs = dict(self._active)     # 快照：install 期间新注册的不算本轮
            report._regs = regs
            report.active_owners = sorted(regs)
            report.disabled_owners = sorted(self._names - set(regs))
        # 先清空各检查器的累积状态。**必须在喂事件之前**：
        # 否则第二次 audit 会带着上一次的残留状态，报出来的违规对不上
        # 这一次的事件 —— 这种错误极难定位，因为报错内容看起来完全合理。
        for reg in regs.values():
            for owner, fn in list(reg.resets):
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    self._record(Violation(
                        owner=owner, check="reset(检查自身异常)",
                        message=f"{type(exc).__name__}: {exc}",
                    ))
        n = 0
        kinds: dict[str, int] = {}
        for ev in events:
            kind = getattr(ev, "kind", None)
            if kind is None:
                raise TypeError("audit() 的 events 需要带 .kind 属性（会话 Event）")
            kinds[kind] = kinds.get(kind, 0) + 1
            self.dispatch(ev)
            n += 1
        report.checked_events = n
        report.kinds_seen = kinds

        for reg in regs.values():
            for owner, fn in list(reg.snapshots):
                try:
                    fn()
                except InvariantError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._record(Violation(
                        owner=owner, check="snapshot(检查自身异常)",
                        message=f"{type(exc).__name__}: {exc}",
                    ))

        with self._lock:
            report.violations = _dedup(self._violations)
            report.counts = dict(self._counts)
            report.hooks_fired = {
                f"{reg.owner}:{k}": v
                for reg in regs.values() for k, v in reg.fired.items()
            }
            if clear:
                self._violations.clear()
        return report

    def reset(self) -> None:
        """清空"看到的违规"（**不动已注册的检查**）。

        手工跑 `dispatch()`（不通过 `audit()`）的场景需要它：
        否则上一段日志攒下的违规会混进下一段的结果里。
        """
        with self._lock:
            self._violations.clear()
            self._counts.clear()


def _compile_patterns(field_name: str, values: Iterable[str]) -> list[re.Pattern[str]]:
    """编译名字过滤器。空串、首尾空白、重复、非法正则都立刻报错。

    对齐 DSH 的 `compilePatterns`：配置写错必须在**装配期**炸，
    而不是等到某次检查默默不生效。静默失效的过滤器比没有过滤器更危险 ——
    你会以为检查开着。
    """
    out: list[re.Pattern[str]] = []
    seen: set[str] = set()
    for v in values:
        if not v or v != v.strip():
            raise ValueError(
                f"invariants: {field_name} 的条目不能为空或带首尾空白：{v!r}")
        if v in seen:
            raise ValueError(f"invariants: {field_name} 里有重复的正则 {v!r}")
        seen.add(v)
        try:
            out.append(re.compile(v))
        except re.error as exc:
            raise ValueError(
                f"invariants: {field_name} 里的正则非法 {v!r}: {exc}") from exc
    return out


def _dedup(violations: Sequence[Violation]) -> list[Violation]:
    """同一 (模块, 检查) 只留第一条；顺序保持发现顺序。"""
    seen: set[str] = set()
    out: list[Violation] = []
    for v in violations:
        k = v.key()
        if k in seen:
            continue
        seen.add(k)
        out.append(v)
    return out


# --------------------------------------------------------------------------
def build_default_registry(**kwargs: Any) -> InvariantRegistry:
    """装配本项目全部伴生入口。

    这就是 DSH 里 `ctx.plugin(InvariantRegistry); ctx.plugin(SessionInvariant);`
    那两行在本项目的等价物。放在这里而不是让调用方自己拼：
    漏装一个伴生入口 = 一整套检查静默不存在，而报告上看起来一切正常。
    """
    from . import (compaction_invariant, loop_invariant,
                   session_invariant)

    reg = InvariantRegistry(**kwargs)
    for mod in (session_invariant, loop_invariant, compaction_invariant):
        mod.install(reg)
    return reg
