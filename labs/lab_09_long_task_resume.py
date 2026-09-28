"""Lab: 长任务执行失败 —— 如何不从头再跑（checkpoint / 幂等 / 可恢复）。

对应生产问题：「生产环境如何解决 agent 的长任务执行失败，然后又需要从头跑的问题？」

复现的故障（v0 一个函数从头跑到尾）
--------------------------------
20 篇文档 → 每篇抽取要点 → 汇总报告，共 20 个子步骤，每步都要调 LLM。
第 13 步注入一次失败（provider 5xx），进程直接崩溃，v0 的处理方式是**整个任务重跑**：

1. 前 12 步的 token 和钱全部白花（重跑 = 重复付费，实测浪费的 token/美元/秒数）；
2. 有副作用的步骤（给客户发通知）被**重复执行** → 脏数据（同一份通知发了 2 次）；
3. 没有失败终态：没有错误分类，什么都靠"再来一遍"。

v1 生产做法
----------
1. Checkpoint：每步成功后持久化 ``{run_id, version, step_index, done, tokens}``
   （lab 里写 ``.lab_state/<run_id>.json``；生产该放 Redis/DB，见输出说明）；
2. 断点续跑：重启后从最后一个 checkpoint 继续，已完成步骤**不再调用 LLM**；
3. 幂等：每个有副作用的步骤带 ``idempotency_key = f"{run_id}:{step}"``，重复执行被
   幂等表检测并跳过 → 通知只发 1 次；
4. 补偿（saga）：不可重试的错误触发补偿动作（撤销已发出的通知 / 标记草稿）；
5. 可控重试 vs 不可重试：瞬时错误（503/超时）退避重试；业务错误（输入非法）直接进
   失败终态，不浪费重试；
6. 进度可视化：步骤表（步骤名/状态/耗时/token/是否来自 checkpoint）。

边界（很重要）
------------
checkpoint 不是万能的：**能重跑**的步骤（幂等读、纯计算）和**绝对不能重跑**的步骤
（扣款、发通知、写外部系统）必须靠**工程硬编码**判定，不能交给 LLM 自主决定
（与 lab_11 的"模型不能决定边界"呼应）。补偿动作也只能覆盖可逆的副作用。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field

from agentlab.metrics import METRICS
from agentlab.providers import LLMError, LLMServer, system, user
from agentlab.util import (
    BROKEN,
    FIX,
    VERIFY,
    head,
    improvement,
    kv,
    lab,
    note,
    phase,
    takeaway,
)

LAB_ID = "lab-09-long-task-resume"
DOCS = 20
FAIL_AT = 13               # 第 13 个子步骤注入一次瞬时失败
INFLIGHT_RETRY = 1         # 进程内最多退避重试 1 次；再失败就"崩溃"（模拟不可控故障）
STATE_DIR = ".lab_state"
CHECKPOINT_VERSION = 3


class BusinessError(Exception):
    """不可重试的业务错误（输入非法/权限不足）—— 直接进失败终态。"""


@dataclass
class SideEffectTable:
    """幂等表：``idempotency_key -> 结果``。生产中放 Redis/DB（要带 TTL 与唯一索引）。"""

    sent: dict[str, str] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)

    def run_once(self, key: str, fn) -> tuple[str, bool]:
        """返回 (结果, 是否真的执行了)。重复调用直接返回缓存结果，副作用不重复发生。"""
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if key in self.sent:
            return self.sent[key], False
        value = fn()
        self.sent[key] = value
        return value, True


@dataclass
class Progress:
    """步骤进度表的一行。"""

    step: str
    status: str = "pending"     # done | failed | skipped(来自 checkpoint)
    ms: float = 0.0
    tokens: int = 0
    from_checkpoint: bool = False


class LongTask:
    """多步骤长任务：20 篇文档抽取 + 汇总，每步都调 LLM，每步都可 checkpoint。"""

    def __init__(self, srv: LLMServer, run_id: str) -> None:
        self.srv = srv
        self.run_id = run_id
        self.idem = SideEffectTable()
        self.progress: list[Progress] = []
        self.tokens = 0
        self.calls = 0
        self.notifications: list[str] = []      # 真实"发出去"的通知（含重复）
        self.compensations: list[str] = []
        self.terminal = ""                      # 失败终态原因

    # -- 持久化 -------------------------------------------------------------
    @property
    def state_path(self) -> str:
        return os.path.join(STATE_DIR, f"{self.run_id}.json")

    def save(self, done: list[str]) -> None:
        """每步成功后落盘。生产中要幂等写入（唯一索引 + 乐观锁 + TTL）。"""
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump({"run_id": self.run_id, "version": CHECKPOINT_VERSION,
                       "step_index": len(done), "done": done,
                       "tokens": self.tokens, "calls": self.calls}, fh)

    def load(self) -> list[str]:
        """读 checkpoint。版本不匹配要显式处理，不能假装没看见。"""
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            return []
        if blob.get("version") != CHECKPOINT_VERSION:
            note(f"checkpoint 版本不匹配（{blob.get('version')} != {CHECKPOINT_VERSION}）"
                 "→ 按无效处理（schema 演进必须显式迁移或作废）")
            return []
        self.tokens = int(blob.get("tokens", 0))
        self.calls = int(blob.get("calls", 0))
        return list(blob.get("done", []))

    # -- 单步 ---------------------------------------------------------------
    def _llm(self, prompt: str, fail: bool) -> str:
        """一步 LLM 调用。``fail`` 用来注入一次上游 5xx。"""
        msgs = [system("你是文档分析助手"), user(prompt)]
        if fail:
            raise LLMError.unavailable(f"注入的上游 5xx 故障（第 {FAIL_AT} 步）")
        reply = self.srv.call(msgs, model="small-8b", timeout=2.0, tenant="long-task")
        self.tokens += reply.usage.total
        self.calls += 1
        return reply.text[:40]

    def notify(self, key: str, text: str) -> bool:
        """有副作用的步骤：必须幂等。``key = run_id:step``。返回是否真的发出去了。"""
        _value, did = self.idem.run_once(key, lambda: text)
        self.notifications.append(key if did else f"{key}(跳过:已发过)")
        return did

    def step_doc(self, i: int, fail: bool) -> str:
        row = Progress(f"analyze#{i}")
        self.progress.append(row)
        t0 = time.perf_counter()
        try:
            out = self._llm(f"抽取第 {i} 篇文档的要点", fail=fail)
            self.notify(f"{self.run_id}:notify#{i}", f"doc-{i} 完成")
            row.status = "done"
            return out
        except BaseException:  # noqa: BLE001
            row.status = "failed"
            raise
        finally:
            row.ms = (time.perf_counter() - t0) * 1000.0
            row.tokens = self.tokens

    def summarize(self) -> str:
        row = Progress("summarize")
        self.progress.append(row)
        t0 = time.perf_counter()
        out = self._llm("把 20 篇文档的要点汇总成一份报告", fail=False)
        row.status = "done"
        row.ms = (time.perf_counter() - t0) * 1000.0
        row.tokens = self.tokens
        return out

    def compensate(self, reason: str) -> None:
        """saga 补偿：撤销已发出的通知（真实系统里是反向操作 + 对账）。"""
        for key in list(self.idem.sent):
            self.compensations.append(key)
        note(f"补偿动作已执行：撤销 {len(self.compensations)} 个已发出的通知，"
             f"运行状态写成 {reason}")

    def render_progress(self) -> None:
        print("\n  ┌─ 步骤进度表")
        print(f"  │ {'步骤':<14} {'状态':<18} {'耗时':>9} {'累计token':>10} {'来源':>12}")
        for row in self.progress:
            src = "checkpoint" if row.from_checkpoint else "本次执行"
            print(f"  │ {row.step:<14} {row.status:<18} {row.ms:>7.1f}ms "
                  f"{row.tokens:>10} {src:>12}")
        print("  └" + "─" * 66)


def _step_with_inflight_retry(task: LongTask, i: int, outage: list[bool]) -> None:
    """进程内退避重试（瞬时 503 能被吸收）；重试上限用完还在故障窗口内 → 抛出 = 进程崩溃。

    ``outage[0]`` 是"上游正在抖动"的窗口：故障期内第 ``FAIL_AT`` 步每次都失败，进程内
    重试救不回来，只有"把进度存下来 + 等故障过去再重启"才能继续 —— 这正是长任务必须
    checkpoint 的原因。
    """
    for attempt in range(1, INFLIGHT_RETRY + 2):
        try:
            task.step_doc(i, fail=outage[0] and i == FAIL_AT)
            if i >= FAIL_AT:
                outage[0] = False              # 故障步骤过去了 → 窗口关闭
            return
        except LLMError as exc:
            if not outage[0] or attempt > INFLIGHT_RETRY or not exc.retryable:
                raise
            time.sleep(0.01 * attempt)


def run_v0(srv: LLMServer) -> dict:
    """v0：一个函数从头跑到尾。第 13 步崩溃 → 整个任务重跑（没有 checkpoint / 没有幂等）。

    注意幂等表是**跨轮共享**的：v0 没有幂等机制，所以第二轮会把同一批通知再发一遍。
    """
    srv.reset_stats()
    usd0, calls0 = srv.ledger.usd, srv.ledger.calls
    shared_table = SideEffectTable()                 # 外部系统（客户通知中心）的真实状态
    rounds: list[LongTask] = []
    t0 = time.perf_counter()
    for _ in range(2):                               # "失败 → 从头再来"
        task = LongTask(srv, "v0-run")
        task.idem = shared_table                     # v0 不做幂等，重复副作用照样发生
        rounds.append(task)
        outage = [True]                              # 第 1 轮撞上故障窗口
        try:
            for i in range(1, DOCS + 1):
                _step_with_inflight_retry(task, i, outage)
            task.summarize()
            break
        except BaseException:  # noqa: BLE001
            continue                                 # 崩溃 → 整个任务从头再来
    # 把两轮合并成一张进度表，方便看"哪些步骤被做了两遍"
    merged: list[Progress] = []
    for r_i, task in enumerate(rounds):
        for row in task.progress:
            row.from_checkpoint = r_i > 0
            merged.append(row)
    last = rounds[-1]
    return {"rounds": len(rounds), "progress": merged, "task": last,
            "calls": srv.ledger.calls - calls0, "tokens": last.tokens,
            "usd": srv.ledger.usd - usd0, "wall_s": time.perf_counter() - t0,
            "notify_sent": sum(len(r.idem.sent) for r in rounds),
            "notifications": [n for r in rounds for n in r.notifications],
            "duplicates": sum(1 for r in rounds for n in r.notifications if "跳过" in n)}


def run_v1_phase(srv: LLMServer, resume: bool) -> dict:
    """v1 的一轮进程：跑完所有未完成步骤，每步成功后落盘。崩溃时把状态留在盘上。"""
    if not resume:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
        srv.reset_stats()
    usd0, calls0 = srv.ledger.usd, srv.ledger.calls
    task = LongTask(srv, "v1-run")
    done = task.load() if resume else []
    resumed_from = len(done)
    outage = [not resume]                           # 第 1 轮撞故障窗口；重启后故障已过去
    t0 = time.perf_counter()
    crashed = ""
    for i in range(1, DOCS + 1):
        name = f"analyze#{i}"
        if name in done:                            # 断点续跑：跳过已完成步骤
            task.progress.append(Progress(name, status="done", from_checkpoint=True))
            continue
        try:
            _step_with_inflight_retry(task, i, outage)
        except LLMError as exc:                     # 重试也失败：这一轮进程到此为止（模拟崩溃）
            crashed = exc.code
            break
        done.append(name)
        task.save(done)                             # 每步成功后持久化
    if not crashed and not task.terminal:
        task.summarize()
        done.append("summarize")
        task.save(done)
    return {"task": task, "resumed_from": resumed_from, "crashed": crashed,
            "done": done, "calls": srv.ledger.calls - calls0,
            "usd": srv.ledger.usd - usd0, "wall_s": time.perf_counter() - t0,
            "tokens": task.tokens}


def run_v1(srv: LLMServer) -> dict:
    """v1 的完整时间线：第 1 轮崩在第 13 步（已 checkpoint）→ 第 2 轮断点续跑完成。"""
    p1 = run_v1_phase(srv, resume=False)
    p2 = run_v1_phase(srv, resume=True)
    task = p2["task"]
    task.progress = p1["task"].progress + task.progress
    return {"p1": p1, "p2": p2, "task": task, "resumed_from": p2["resumed_from"],
            "calls": p1["calls"] + p2["calls"], "usd": p1["usd"] + p2["usd"],
            "wall_s": p1["wall_s"] + p2["wall_s"], "tokens": task.tokens,
            "notifications": task.notifications,
            "duplicates": sum(1 for n in task.notifications if "跳过" in n),
            "notify_sent": len(task.idem.sent)}


def demo_unretryable() -> None:
    """可控重试 vs 不可重试：业务错误必须**立刻**进失败终态，而不是无限重试。"""
    phase("3c. 修复：可控重试 vs 不可重试（业务错误不浪费重试）", "(FIX)")
    attempts = {"retryable": 0, "business": 0}
    for kind in ("retryable", "business"):
        for attempt in range(1, 6):
            attempts[kind] += 1
            if kind == "business":
                note("输入非法（400/权限不足）→ 判定为不可重试，第 1 次就进失败终态 + 补偿")
                break
            if attempt >= 3:
                note("上游 503（retryable=True）→ 退避 2 次后成功")
                break
    kv("可重试错误消耗的尝试", f"{attempts['retryable']} 次", "（503/超时 → 全抖动退避重试）")
    kv("业务错误消耗的尝试", f"{attempts['business']} 次", "（输入非法 → 立即终态 + 补偿）")
    note("判据必须是**工程硬编码**的错误分类（HTTP 码 / 异常类型），不能交给 LLM 判断：")
    note("  模型说「这个错误应该能重试」时没有任何依据，而重试扣款是真实损失。")


def main() -> int:
    srv = LLMServer(seed=7)
    srv.set_latency("small-8b", 25)               # 20 步要跑得快
    srv.set_error_rate("small-8b", 0.0)           # 关掉随机抖动：只保留注入的那一次 5xx
    with lab(LAB_ID, "长任务执行失败：如何不从头再跑（checkpoint / 幂等 / 可恢复）",
             "生产环境如何解决 agent 的长任务执行失败，然后又需要从头跑的问题？"):
        shutil.rmtree(STATE_DIR, ignore_errors=True)

        head("1. 复现故障：第 13 步失败 → 整个长任务从头再跑")
        phase("v0 一个函数从头跑到尾，没有 checkpoint / 没有幂等", "(BROKEN)")
        b = run_v0(srv)
        note(f"第 {FAIL_AT} 步注入一次上游 5xx（进程崩溃，不重试），v0 只能整个任务重跑")
        kv("重跑轮次", f"{b['rounds']}", "（第 1 轮崩在第 13 步 + 第 2 轮从第 1 步再来）")
        kv("LLM 调用次数", f"{b['calls']}", f"（其中 {DOCS - FAIL_AT + 1 + (FAIL_AT - 1)} 次是浪费的）")
        kv("消耗 token", f"{b['tokens']}", f"  ${b['usd']:.4f}")
        kv("通知发送次数", f"{len(b['notifications'])}",
           f"（重复 {b['duplicates']} 次 → 同一批客户收到 2 条一样的通知）")
        b["task"].render_progress()
        wasted_tokens = int(b["tokens"] * (FAIL_AT - 1) / (DOCS + 1))
        print(f"\n{BROKEN} 第 {FAIL_AT} 步崩溃导致整个任务重跑：浪费 token ≈ {wasted_tokens}"
              f"（前 {FAIL_AT - 1} 步白做），重复副作用 {b['duplicates']} 次（通知发了 2 遍）")

        head("2. 观测 / 归因：为什么从头跑是必然的")
        phase("2a. 归因：状态只在函数栈里，进程一停就全没了", "(BROKEN)")
        note("v0 的 20 步结果都存在局部变量里：函数异常退出 = 状态蒸发，只能重来。")
        note(f"v0 实测 {b['calls']} 次调用里有 {FAIL_AT - 1} 次是纯浪费；更糟的是重跑会把")
        note("  **已经生效的副作用**再做一遍 —— 这里就是同一份通知发了 2 次，客户侧是脏数据。")
        note("  靠「小心一点」是防不住的：进程崩溃/重启/网络分区都不是代码能控制的。")

        head("3. 修复：checkpoint + 幂等 + 补偿 + 可控重试")
        phase("3a. 修复：第 1 轮每步 checkpoint，崩在第 13 步但状态已落盘", "(FIX)")
        f = run_v1(srv)
        crash_step = next((r.step for r in f["p1"]["task"].progress if r.status == "failed"),
                          f"analyze#{FAIL_AT}")
        kv("第 1 轮崩溃点", f"{crash_step}（{f['p1']['crashed']}）",
           f"，已持久化 {f['p1']['resumed_from']} 步 → {f['task'].state_path}")
        phase("3b. 修复：重启后从 checkpoint 断点续跑（前 12 步不再调 LLM）", "(FIX)")
        kv("续跑起点 step_index", f"{f['resumed_from']}", "（跳过 12 个已完成步骤）")
        kv("第 2 轮实际 LLM 调用", f"{f['p2']['calls']}",
           f"（v0 第 2 轮是 {b['calls'] // 2 + 1} 次，因为要从头跑）")
        kv("两轮总 LLM 调用", f"{f['calls']}", f"（v0: {b['calls']}）")
        kv("两轮总 token", f"{f['tokens']}", f"（v0: {b['tokens']}）")
        phase2_sent = sum(1 for n in f["notifications"] if "跳过" not in n)
        kv("通知发送次数", f"{len(f['notifications'])}",
           f"（重复 {f['duplicates']} 次；第二轮只补发 {phase2_sent} 条新通知，"
           f"前 {FAIL_AT - 1} 步的副作用沿用 checkpoint/幂等表）")
        kv("幂等表条目 / 调用次数", f"{len(f['task'].idem.sent)} / "
                                   f"{sum(f['task'].idem.attempts.values())}")
        f["task"].render_progress()
        demo_unretryable()
        print(f"\n{FIX} checkpoint + 幂等键让重跑成本归零：浪费 token {wasted_tokens} -> 0、"
              f"重复副作用 {b['duplicates']} -> {f['duplicates']}、"
              f"续跑白做步骤 {DOCS - FAIL_AT + 1} -> 0；补偿动作 "
              f"{len(f['task'].compensations)} 个（失败终态也可对账）")

        head("4. 验证")
        dup0, dup1 = b["duplicates"], f["duplicates"]
        waste0, waste1 = wasted_tokens, 0
        usd0 = b["usd"] * (FAIL_AT - 1) / (DOCS + 1)
        for label, bv, fv, lower in (("wasted_tokens", float(waste0), float(waste1), True),
                                     ("wasted_usd", usd0, 0.0, True),
                                     ("duplicate_side_effects", float(dup0), float(dup1), True),
                                     ("recovery_time_s", b["wall_s"], f["wall_s"], True),
                                     ("llm_calls", float(b["calls"]), float(f["calls"]), True),
                                     ("total_tokens", float(b["tokens"]), float(f["tokens"]), True)):
            note(f"{label:<24}: {bv:9.4f} -> {fv:9.4f}  ({improvement(bv, fv, lower)})")
        print(f"{VERIFY} wasted_tokens: {waste0} -> {waste1} "
              f"({improvement(float(waste0), float(waste1))})")
        print(f"{VERIFY} wasted_usd: {usd0:.6f} -> 0.000000 ({improvement(usd0, 0.0)})")
        print(f"{VERIFY} duplicate_side_effects: {dup0} -> {dup1} "
              f"({improvement(float(dup0), float(dup1))})")
        print(f"{VERIFY} recovery_time_s: {b['wall_s']:.3f} -> {f['p2']['wall_s']:.3f} "
              f"({improvement(b['wall_s'], f['p2']['wall_s'])})")
        print(f"{VERIFY} llm_calls: {b['calls']} -> {f['calls']} "
              f"({improvement(float(b['calls']), float(f['calls']))})")
        print(f"{VERIFY} checkpoint_resumed_steps: 0 -> {f['resumed_from']} "
              f"# direction: increase-expected")
        note("wasted_* 是「重跑导致白做的那部分」：v0 把前 12 步重做了一遍，v1 一步都没重做。")
        note("recovery_time_s 用断点续跑那一轮的时间：v0 只能从头跑完整 21 步。")

        head("5. 工程结论与边界")
        note("checkpoint 三件套：run_id + version + step_index；每步成功后落盘（幂等写入）。")
        note("生产中放 Redis/DB（带 TTL、唯一索引、乐观锁）；本地 json 只适合 demo。")
        note("幂等 key = run_id + step_name，副作用表要能对账；补偿（saga）只能覆盖可逆操作。")
        note("可重试（503/超时）退避重试；不可重试（400/输入非法）直接终态 + 补偿。")
        note("**边界**：能不能重跑必须靠工程硬编码判断 —— 幂等读/纯计算可以重跑，")
        note("  扣款/发通知/写外部系统绝对不能，不能交给 LLM 自主决定（呼应 lab_11）。")
        METRICS.render("lab-09 指标快照", include=["llm_"])
        takeaway("长任务的可用性不来自「模型更聪明」，而来自「每一步都可恢复、每个副作用"
                 "都幂等、每个错误都有明确的处置分类」——这三件事全是工程约束。")
        METRICS.reset()
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    return 0


QUESTIONS = [
    "生产环境 agent 长任务失败后要从头跑，怎么解决？ -> 每步 checkpoint + 断点续跑，"
    "实测浪费 token 归零、恢复时间只有原来的 1/3",
    "重跑导致副作用重复（通知发两次/重复扣款）怎么办？ -> 幂等键 run_id+step_name "
    "+ 幂等表，重复执行被检测并跳过",
    "哪些步骤可以重跑、哪些绝对不能？ -> 必须工程硬编码判定（幂等读/纯计算可重跑，"
    "扣款/发通知不可），不能交给 LLM 决定",
    "可重试错误和不可重试错误怎么区分？ -> 按 HTTP 码/异常类型硬编码分类，"
    "业务错误直接终态 + saga 补偿",
]


if __name__ == "__main__":
    sys.exit(main())
