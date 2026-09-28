"""Capstone 长任务：checkpoint / 幂等 / 重试分级 / 补偿（saga）。

对应 lab-09。核心工程结论：

* **可恢复**的前提是"每一步执行前先把状态落盘"，而不是"失败了再想办法"。
* **幂等**必须由工程保证（幂等键），不能问模型"这个操作是不是重复的"。
* **哪些步骤可以重跑、哪些绝对不能**，是硬编码的步骤属性：``retryable`` /
  ``side_effect`` / ``compensable``。把这三个属性显式声明出来，是长任务能
  安全恢复的全部秘密。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from agentlab.metrics import METRICS


class StepFailure(Exception):
    def __init__(self, step: str, code: str, message: str, retryable: bool = True):
        super().__init__(f"[{code}] step={step}: {message}")
        self.step = step
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class Step:
    name: str
    fn: Callable[["TaskRun"], str]
    side_effect: bool = False  # 有副作用 → 必须幂等
    retryable: bool = True  # 瞬时错误可否重试（业务错误要设 False）
    compensable: bool = False  # 需要注册补偿动作
    compensate: Callable[["TaskRun"], str] | None = None
    est_tokens: int = 400


@dataclass
class TaskRun:
    run_id: str
    task: str
    steps: list[dict] = field(default_factory=list)
    completed: int = 0
    tokens_used: int = 0
    usd_used: float = 0.0
    notifications_sent: int = 0
    compensations_run: int = 0
    version: int = 1
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    last_error: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


class CheckpointStore:
    """把运行状态落盘。生产上用 Redis/DB；这里用文件 + 原子替换。

    三个必须有的字段：``version``（结构升级用）、``completed``（断点位置）、
    ``run_id``（幂等与审计的主键）。少了任何一个，恢复都会出错。
    """

    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        self.writes = 0
        self.reads = 0

    def _path(self, run_id: str) -> str:
        return os.path.join(self.dir, f"{run_id}.json")

    def save(self, run: TaskRun) -> None:
        with self._lock:
            tmp = self._path(run.run_id) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(run.to_json())
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path(run.run_id))  # 原子替换，避免写坏
            self.writes += 1

    def load(self, run_id: str) -> TaskRun | None:
        path = self._path(run_id)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.reads += 1
        run = TaskRun(data["run_id"], data["task"])
        run.steps = data.get("steps", [])
        run.completed = data.get("completed", 0)
        run.tokens_used = data.get("tokens_used", 0)
        run.usd_used = data.get("usd_used", 0.0)
        run.notifications_sent = data.get("notifications_sent", 0)
        run.compensations_run = data.get("compensations_run", 0)
        run.version = data.get("version", 1)
        run.status = data.get("status", "running")
        run.last_error = data.get("last_error", "")
        return run

    def clear(self, run_id: str) -> None:
        with self._lock:
            path = self._path(run_id)
            if os.path.exists(path):
                os.remove(path)


class IdempotencyTable:
    """幂等表：``key -> 结果``。有副作用的步骤执行前先查，执行后立刻写。

    关键顺序：**先写"意图"再执行**（或者用唯一约束）才能防住"执行成功但记录
    失败"导致的重复；这里教学化地采用"执行成功后立即记录"，并说明生产上要用
    ``INSERT ... ON CONFLICT`` 之类的原子语义。
    """

    def __init__(self) -> None:
        self._applied: dict[str, str] = {}
        self._lock = threading.Lock()
        self.skipped = 0

    def already_done(self, key: str) -> str | None:
        with self._lock:
            v = self._applied.get(key)
            if v is not None:
                self.skipped += 1
            return v

    def mark(self, key: str, result: str) -> None:
        with self._lock:
            self._applied[key] = result


@dataclass
class RunStats:
    runs: int = 0
    wasted_tokens: int = 0
    wasted_usd: float = 0.0
    duplicate_side_effects: int = 0
    resumed_from: int = 0
    recovered: int = 0
    failed_permanently: int = 0
    compensations: int = 0


class LongTaskRunner:
    """可恢复的长任务执行器。

    ``checkpoint=True`` 时每步落盘；``checkpoint=False`` 时就是"从头跑"的反面教材。
    """

    def __init__(
        self,
        store: CheckpointStore,
        idem: IdempotencyTable,
        usd_per_1k_tokens: float = 0.0006,
        checkpoint: bool = True,
        max_attempts: int = 3,
    ) -> None:
        self.store = store
        self.idem = idem
        self.usd_per_1k = usd_per_1k_tokens
        self.checkpoint = checkpoint
        self.max_attempts = max_attempts
        self.stats = RunStats()
        self.m_wasted = METRICS.counter("longtask_wasted_tokens_total", "重跑浪费的 token")
        self.m_dup = METRICS.counter("longtask_duplicate_side_effects_total", "重复副作用")
        self.m_resume = METRICS.counter("longtask_resumed_total", "断点续跑次数")

    # -- 单个步骤 -----------------------------------------------------------
    def _run_step(self, run: TaskRun, step: Step, idx: int) -> str:
        idem_key = f"{run.run_id}:{step.name}"
        if step.side_effect:
            prev = self.idem.already_done(idem_key)
            if prev is not None:
                run.steps.append(self._step_record(step, idx, prev, "skipped-idempotent"))
                return prev

        attempt = 0
        while True:
            attempt += 1
            # 每一步都消耗 token（长任务的成本就是这样累积的）
            run.tokens_used += step.est_tokens
            run.usd_used += step.est_tokens / 1000 * self.usd_per_1k
            try:
                out = step.fn(run)
            except StepFailure as exc:
                if not exc.retryable or attempt >= self.max_attempts:
                    raise
                time.sleep(0.0)  # 退避（教学环境不加真实延迟）
                continue
            if step.side_effect:
                self.idem.mark(idem_key, out)
                if step.name == "notify":
                    run.notifications_sent += 1
            run.steps.append(self._step_record(step, idx, out, "ok"))
            return out

    @staticmethod
    def _step_record(step: Step, idx: int, out: str, status: str) -> dict:
        return {
            "idx": idx,
            "name": step.name,
            "status": status,
            "out": out[:80],
            "side_effect": step.side_effect,
            "at": round(time.time(), 3),
        }

    # -- 整任务 -------------------------------------------------------------
    def run(self, run_id: str, task: str, steps: list[Step], fail_at: int | None = None) -> TaskRun:
        run = self.store.load(run_id) if self.checkpoint else None
        if run is None:
            run = TaskRun(run_id=run_id, task=task)
        else:
            if run.completed > 0:
                self.stats.resumed_from = run.completed
                self.m_resume.inc()
        self.stats.runs += 1

        start_idx = run.completed
        for idx in range(start_idx, len(steps)):
            step = steps[idx]
            try:
                self._run_step(run, step, idx)
            except StepFailure as exc:
                run.last_error = str(exc)
                if self.checkpoint:
                    run.status = "failed"
                    self.store.save(run)
                self._compensate(run, steps, idx)
                run.status = "failed"
                self.stats.failed_permanently += 1
                raise
            run.completed = idx + 1
            if self.checkpoint:
                self.store.save(run)

        run.status = "done"
        if self.checkpoint:
            self.store.save(run)
        return run

    def resume(self, run_id: str, steps: list[Step], fail_at: int | None = None) -> TaskRun:
        """从 checkpoint 继续；只跑没跑过的步骤。"""
        prev = self.store.load(run_id)
        if prev is None:
            raise KeyError(f"没有 {run_id} 的 checkpoint")
        before_tokens = prev.tokens_used
        run = self.run(run_id, prev.task, steps, fail_at)
        # 第二次调用 run() 时，失败前的重复消耗就是"浪费"
        run.tokens_used = before_tokens
        self.stats.recovered += 1
        return run

    def _compensate(self, run: TaskRun, steps: list[Step], failed_idx: int) -> None:
        """saga 补偿：按**逆序**回滚已完成的可补偿步骤。"""
        for idx in range(failed_idx - 1, -1, -1):
            step = steps[idx]
            if step.compensable and step.compensate is not None:
                try:
                    step.compensate(run)
                    run.compensations_run += 1
                    self.stats.compensations += 1
                except Exception:  # noqa: BLE001 - 补偿失败要单独告警，但不掩盖原始错误
                    pass

    # -- 报价 ---------------------------------------------------------------
    def account_waste(self, tokens: int) -> None:
        self.stats.wasted_tokens += tokens
        self.stats.wasted_usd += tokens / 1000 * self.usd_per_1k
        self.m_wasted.inc(tokens)

    def account_duplicate_effect(self, n: int = 1) -> None:
        self.stats.duplicate_side_effects += n
        self.m_dup.inc(n)


def build_pipeline_steps(server=None, n_docs: int = 20, fail_at: int | None = None) -> list[Step]:
    """构造一条真实的长任务流水线：抽取 → 摘要 → 计算 → 落库 → 通知。"""

    def make_extract(i: int) -> Callable[[TaskRun], str]:
        def _fn(run: TaskRun) -> str:
            if fail_at is not None and i == fail_at:
                raise StepFailure(f"extract_{i}", "503", "上游抖动（模拟）", retryable=True)
            return f"extract_{i}_ok"
        return _fn

    def summarize(run: TaskRun) -> str:
        return f"summary_of_{n_docs}_docs"

    def compute(run: TaskRun) -> str:
        return "total=42"

    def write_db(run: TaskRun) -> str:
        return "persisted"

    def notify(run: TaskRun) -> str:
        # 有副作用：靠幂等表保证只发一次
        return f"notified:{run.notifications_sent + 1}"

    def undo_write(run: TaskRun) -> str:
        return "rolled_back_write"

    steps: list[Step] = []
    for i in range(n_docs):
        steps.append(Step(f"extract_{i}", make_extract(i), est_tokens=380))
    steps.append(Step("summarize", summarize, est_tokens=900))
    steps.append(Step("compute", compute, est_tokens=120))
    steps.append(
        Step("write_db", write_db, side_effect=True, compensable=True,
             compensate=undo_write, est_tokens=200)
    )
    steps.append(Step("notify", notify, side_effect=True, retryable=False, est_tokens=150))
    return steps
