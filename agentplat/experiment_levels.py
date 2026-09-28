"""实验：推理等级 / 模型档位的成本-质量-延迟权衡。

回答的问题
----------
**"小模型便宜但弱、大模型准但贵，怎么平衡？"** 以及
**"提高推理等级到底值不值？"**

这个实验用**同一批任务**跑多个"推理等级"，每个等级测四项：
准确率、每任务成本、P50/P95 延迟、输出 token 数。然后给出**帕累托前沿**
（哪些等级是"没有被别的等级全面碾压"的）。

推理等级怎么定义
----------------
一个"等级"= 模型档位 + 推理参数 + 自一致性采样次数：

    等级              模型        推理参数            采样   说明
    L0 最省           small       reasoning 关        1     能答就答
    L1 小模型+推理     small       reasoning_effort   1     同模型多想一会儿
    L2 中模型         mid         reasoning 关        1     换更大模型
    L3 中模型+自一致   mid         -                   3     多采样投票
    L4 大模型         large       reasoning high      1     最贵最准

**注意成本是乘法放大的**：自一致性采样 3 次 = 3 倍成本；推理等级提高 =
输出 token 变多 = 更贵。所以"更准"从来不是免费的，这个实验就是把账算清楚。

安全与速度
----------
* 真实 LLM 下会走**成本护栏**（`agentplat/guard.py`），默认上限 $1.0 / 400 次调用。
* 支持 ``--dry-run``：不发真实请求，先看调用量与估算花费。
* 支持 ``--tasks N`` 控制样本量，先用小样本试。
* 支持 ``--budget-usd`` 覆盖上限。

用法::

    python -m agentplat.experiment_levels --dry-run          # 先干跑看花费
    python -m agentplat.experiment_levels --tasks 8          # 小样本真跑
    python -m agentplat.experiment_levels --tasks 20 --budget-usd 0.5
    python -m agentplat.experiment_levels --levels L0,L2,L4  # 只跑指定等级
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field

from agentlab.providers import ChatMessage
from agentlab.util import (
    FIX,
    VERIFY,
    Stats,
    force_utf8,
    head,
    improvement,
    kv,
    note,
    phase,
    rule,
    takeaway,
)

from .guard import CostGuard, estimate_run
from .llm import OpenAIChatClient
from .llmconfig import LLMConfig

# --------------------------------------------------------------------------
# 带标准答案的任务集
# --------------------------------------------------------------------------


@dataclass
class Task:
    q: str
    expect: str          # 标准答案（用于自动判分）
    kind: str = "fact"   # fact | math | reason
    weight: float = 1.0


#: 任务集刻意混入三类，因为"推理等级"对不同类型的影响完全不同：
#: 事实题几乎不受推理等级影响，数学/多步推理受影响最大。
TASKS: tuple[Task, ...] = (
    Task("一个字节等于多少位？只回答数字。", "8", "fact"),
    Task("HTTP 状态码 429 表示什么？只回答四个字以内的原因。", "限流", "fact"),
    Task("缓存穿透和缓存击穿的区别是什么？用一句话回答。", "不存在", "fact"),
    Task("一个服务 p50 延迟 200ms，并发 10，理论吞吐约多少 QPS？只回答数字。", "50", "math"),
    Task("上游错误率 20%，重试 2 次，最终失败率约多少百分比？只回答数字（保留一位小数）。", "0.8", "math"),
    Task("3 个租户各发 10 个请求，其中一个租户占了 70% 流量，另外两个各占多少？只回答两个数字。", "15", "math"),
    Task("某接口 QPS 1000，超时率 0.1%，每天大约多少次超时？只回答数字。", "86400", "math"),
    Task("如果 P95 是 2 秒而 P50 是 200 毫秒，最可能的原因是什么？用一句话回答。", "慢调用", "reason"),
    Task("为什么熔断器要统计慢调用而不只统计错误？用一句话回答。", "慢", "reason"),
    Task("两个租户共用一个连接池，一个租户突然打满会怎样？用一句话回答。", "影响", "reason"),
    Task("为什么重试会放大故障？用一句话回答。", "流量", "reason"),
    Task("缓存命中率从 0 提升到 60%，上游调用量大约降到原来的百分之多少？只回答数字。", "40", "math"),
)


@dataclass
class Level:
    """一个"推理等级"：模型档位 + 参数 + 采样次数。"""

    key: str
    label: str
    tier: str                 # small-8b / mid-32b / large-400b
    effort: str = ""          # reasoning_effort，空=不发
    samples: int = 1          # 自一致性采样次数（>1 则投票）
    note: str = ""


LEVELS: tuple[Level, ...] = (
    Level("L0", "小模型·直接答", "small-8b", "", 1, "最省，能答就答"),
    Level("L1", "小模型·开推理", "small-8b", "high", 1, "同模型多想一会儿"),
    Level("L2", "中模型·直接答", "mid-32b", "", 1, "换更大的模型"),
    Level("L3", "中模型·自一致×3", "mid-32b", "", 3, "多采样投票，成本×3"),
    Level("L4", "大模型·高推理", "large-400b", "high", 1, "最贵最准"),
)


# --------------------------------------------------------------------------
# 判分（确定性、可复现）
# --------------------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"[\s，。、；：,.!?！？\"'`]", "", s or "").lower()


def grade(task: Task, answer: str) -> float:
    """0~1 分。**判分必须确定性**，不能靠另一个模型来打分。

    否则"评估器"本身成了变量，实验结论就没法复现了 —— 这是评估类实验的
    第一条纪律：**打分器必须是确定性的代码。**
    """
    if not answer:
        return 0.0
    a = _norm(answer)
    exp = _norm(task.expect)
    if task.kind in ("math", "fact"):
        # 数值/关键词命中即得分
        return 1.0 if exp in a else 0.0
    # 判断题：命中关键词即可（宽松匹配，因为措辞千变万化）
    return 1.0 if exp in a else 0.0


def majority(answers: list[str]) -> str:
    """自一致性投票：取出现次数最多的答案（归一化后比较）。"""
    if not answers:
        return ""
    counts: dict[str, tuple[int, str]] = {}
    for a in answers:
        k = _norm(a)
        n, orig = counts.get(k, (0, a))
        counts[k] = (n + 1, orig)
    return max(counts.values(), key=lambda kv_: kv_[0])[1]


# --------------------------------------------------------------------------
# 主实验
# --------------------------------------------------------------------------


@dataclass
class LevelResult:
    level: Level
    n: int = 0
    correct: float = 0.0
    by_kind: dict[str, float] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    errors: int = 0
    answers: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0

    @property
    def usd_per_task(self) -> float:
        return self.usd / self.n if self.n else 0.0

    @property
    def stats(self) -> Stats:
        return Stats(self.latencies)

    @property
    def tokens_out_per_task(self) -> float:
        return self.tokens_out / self.n if self.n else 0.0


def run_level(
    lvl: Level,
    tasks: list[Task],
    cfg: LLMConfig,
    guard: CostGuard,
) -> LevelResult:
    """跑一个等级的全部任务。"""
    res = LevelResult(level=lvl)
    # 每个等级单独一个客户端，方便带上不同的 reasoning_effort
    level_cfg = LLMConfig(**{**cfg.__dict__})
    level_cfg.reasoning_effort = lvl.effort
    # 自一致性需要多样性，否则 3 次采样得到同一个答案，投票毫无意义
    level_cfg.temperature = 0.7 if lvl.samples > 1 else cfg.temperature
    client = OpenAIChatClient(level_cfg)
    model = cfg.tier_map().get(lvl.tier, cfg.model)
    if not model:
        raise SystemExit(
            f"档位 {lvl.tier} 没有可用的模型名。请在 /settings 页填写"
            f"（至少填 mid 档，其余档会回退到 mid）。"
        )

    kind_hits: dict[str, list[float]] = {}
    for task in tasks:
        t0 = time.perf_counter()
        answers: list[str] = []
        for _ in range(lvl.samples):
            msgs = [ChatMessage("user", task.q)]
            try:
                if guard.dry_run:
                    # ★ 干跑必须**真的不出网**。
                    # 实测踩过：只在 guard 里标记 dry_run 是不够的 —— 实验直接
                    # 调用 client.complete()，绕过了带 dry_run 分支的 _serve()，
                    # 结果"干跑"真的打出了网络请求（而且因为模型名回退有问题
                    # 报了一堆 400）。安全机制必须校验**确实没有出网**，
                    # 而不是"我设了个标志位所以应该没出网"。
                    res.calls += 1
                    answers.append("[dry-run]")
                    res.tokens_in += 90
                    res.tokens_out += 220
                    res.usd += guard.record(
                        90, 220, cfg.price_in_per_m, cfg.price_out_per_m, tag=lvl.key
                    )
                    continue
                if lvl.samples > 1:
                    # 自一致性需要多样性，否则多次采样得到同一答案，投票毫无意义
                    msgs = [
                        ChatMessage("system", f"这是第 {len(answers) + 1} 次独立作答。"),
                        ChatMessage("user", task.q),
                    ]
                text, usage = client.complete(model, msgs, cfg.timeout_s)
                answers.append(text)
                res.calls += 1
                res.tokens_in += usage.in_tokens
                res.tokens_out += usage.out_tokens
                res.usd += guard.record(
                    usage.in_tokens, usage.out_tokens,
                    cfg.price_in_per_m, cfg.price_out_per_m, tag=lvl.key,
                )
            except Exception as exc:  # noqa: BLE001
                res.errors += 1
                res.calls += 1
                if res.errors <= 2:  # 最多提示两条，避免刷屏
                    note(f"    任务失败（计入错误率）：{type(exc).__name__}: {str(exc)[:90]}")
                break
        else:
            # 只有采样全部成功才判分
            final = majority(answers) if lvl.samples > 1 else (answers[0] if answers else "")
            score = grade(task, final)
            res.correct += score
            kind_hits.setdefault(task.kind, []).append(score)
            res.answers.append(final[:60])
            res.latencies.append((time.perf_counter() - t0) * 1000.0)
            res.n += 1
            continue
        # for-else 未执行（有异常）→ 计一次失败但不计分
        res.latencies.append((time.perf_counter() - t0) * 1000.0)

    res.by_kind = {k: sum(v) / len(v) for k, v in kind_hits.items()}
    return res


def pareto(results: list[LevelResult]) -> list[LevelResult]:
    """帕累托前沿：没有被任何其他等级在"更便宜且更准"两个维度上同时碾压的等级。"""
    front = []
    for r in results:
        dominated = False
        for o in results:
            if o is r:
                continue
            cheaper = o.usd_per_task <= r.usd_per_task
            more_accurate = o.accuracy >= r.accuracy
            strictly_better = (
                o.usd_per_task < r.usd_per_task or o.accuracy > r.accuracy
            )
            if cheaper and more_accurate and strictly_better:
                dominated = True
                break
        if not dominated:
            front.append(r)
    return sorted(front, key=lambda r: r.usd_per_task)


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="推理等级 / 模型档位的成本-质量权衡实验")
    ap.add_argument("--tasks", type=int, default=len(TASKS), help="任务数（先用小样本试）")
    ap.add_argument("--levels", default="", help="只跑指定等级，逗号分隔，如 L0,L2,L4")
    ap.add_argument("--budget-usd", type=float, default=None, help="覆盖花费上限")
    ap.add_argument("--max-calls", type=int, default=None, help="覆盖调用次数上限")
    ap.add_argument("--dry-run", action="store_true", help="不发真实请求，只看估算")
    ap.add_argument("--json", default="", help="把结果另存为 JSON")
    args = ap.parse_args(argv)

    cfg = LLMConfig.load()
    if not cfg.is_real:
        print(rule("="))
        print("  这个实验需要真实 LLM：它测的是真实模型的准确率/延迟/成本。")
        print("  当前后端是内置模拟器（回答是模板文本，准确率没有意义）。")
        print(rule("="))
        print("\n配置方式（任选其一）：")
        print("  ① 在面板里配： python -m agentplat.demo  然后打开 /settings 填 key")
        print("  ② 环境变量：   set AGENTLAB_LLM_KEY=sk-xxx")
        print("                 set AGENTLAB_LLM_BASE=https://api.deepseek.com")
        print("                 set AGENTLAB_LLM_MODEL=deepseek-chat")
        print("  ③ 先用 --dry-run 看流程： python -m agentplat.experiment_levels --dry-run")
        if not args.dry_run:
            return 2

    # 护栏上限来自**平台配置**（PlatformConfig），不是 LLM 连接配置。
    # 两者职责不同：PlatformConfig 管"能花多少钱/多少次"，LLMConfig 管"连哪儿、用哪个模型"。
    from .config import PlatformConfig

    pcfg = PlatformConfig.from_env()
    dry = args.dry_run or pcfg.dry_run
    guard = CostGuard(
        max_usd=args.budget_usd if args.budget_usd is not None else pcfg.max_usd_per_run,
        max_calls=args.max_calls if args.max_calls is not None else pcfg.max_llm_calls_per_run,
        dry_run=dry,
    )

    tasks = list(TASKS[: max(1, min(args.tasks, len(TASKS)))])
    levels = LEVELS
    if args.levels:
        want = {s.strip().upper() for s in args.levels.split(",")}
        levels = tuple(l for l in LEVELS if l.key in want) or LEVELS

    total_samples = sum(l.samples for l in levels)
    est = estimate_run(
        n_requests=len(tasks) * total_samples,
        prompt_tokens=90, out_tokens=220,
        price_in_per_m=cfg.price_in_per_m, price_out_per_m=cfg.price_out_per_m,
        llm_calls_per_request=1.0,
    )

    print(rule("="))
    print("  实验：推理等级 / 模型档位的成本-质量-延迟权衡")
    print(rule("="))
    kv("后端", "干跑（不发请求）" if dry else "真实 LLM")
    kv("端点", cfg.chat_url() or "（未配置）")
    kv("模型映射", f"small={cfg.model_or('small') or '—'} mid={cfg.model_or('mid') or '—'} "
                  f"large={cfg.model_or('large') or '—'}")
    kv("任务数", len(tasks))
    kv("推理等级", "、".join(f"{l.key}" for l in levels) + f"（共 {len(levels)} 个）")
    kv("采样总数", total_samples)
    note("")

    head("1. 跑之前先算钱（干跑估算）")
    for k, v in est.items():
        kv(k, v)
    usd_cap = "不设限" if guard.max_usd is None else f"${guard.max_usd:.4f}"
    call_cap = "不设限" if guard.max_calls is None else f"{guard.max_calls} 次"
    kv("护栏上限", f"花费 {usd_cap} / 调用数 {call_cap}")
    # 上限可选（None = 不设限）之后，比较也要分支 ——
    # `est > None` 在 Python 3 里直接 TypeError，实验根本跑不起来。
    if guard.max_usd is not None and est["est_usd"] > guard.max_usd:
        note(f"⚠ 估算花费 ${est['est_usd']:.4f} 超过上限 ${guard.max_usd:.4f}，")
        note("  实验会在触达上限时被拦下。可用 --budget-usd 调高，或 --tasks 减少样本。")
    note("")
    note("成本是**乘法放大**的：自一致性 ×3 就是 3 倍成本；提高推理等级会让")
    note("输出 token 变多，也是更贵。所以「更准」从来不是免费的。")

    head("2. 逐个推理等级跑同一批任务")
    results: list[LevelResult] = []
    for lvl in levels:
        phase(f"{lvl.key} {lvl.label}", f"({lvl.note})")
        if guard.tripped():
            note("护栏已触达上限，跳过剩余等级（这是保护，不是故障）。")
            break
        t0 = time.perf_counter()
        r = run_level(lvl, tasks, cfg, guard)
        results.append(r)
        print(f"    完成 {r.n}/{len(tasks)} 题，用时 {time.perf_counter() - t0:.1f}s，"
              f"花费 ${r.usd:.6f}")
        kv("准确率", f"{r.accuracy:.1%}", f"({r.correct:.0f}/{r.n})")
        kv("每任务成本", f"${r.usd_per_task:.6f}")
        kv("延迟 P50/P95", f"{r.stats.p50:.0f} / {r.stats.p95:.0f}", "ms")
        kv("输出 tokens/题", f"{r.tokens_out_per_task:.0f}")
        if r.errors:
            kv("错误数", r.errors)
        if r.by_kind:
            note("按题型： " + "  ".join(f"{k}={v:.0%}" for k, v in sorted(r.by_kind.items())))

    if not results:
        print("\n没有任何等级跑完 —— 检查护栏上限或网络。")
        return 1

    head("3. 对比表")
    print(f"    {'等级':<4}{'说明':<20}{'准确率':>8}{'$/任务':>11}"
          f"{'P50':>9}{'P95':>9}{'out tok':>9}{'相对成本':>10}")
    print("    " + "-" * 84)
    base = results[0]
    for r in results:
        rel = r.usd_per_task / base.usd_per_task if base.usd_per_task else 0
        print(f"    {r.level.key:<4}{r.level.label:<20}{r.accuracy:>7.0%}"
              f"{r.usd_per_task:>11.6f}{r.stats.p50:>8.0f}ms{r.stats.p95:>8.0f}ms"
              f"{r.tokens_out_per_task:>9.0f}{rel:>9.1f}×")

    head("4. 帕累托前沿：哪些等级没有被全面碾压")
    front = pareto(results)
    note("「被碾压」= 存在另一个等级同时**更便宜且更准**。")
    note("")
    for r in front:
        note(f"  ✓ {r.level.key} {r.level.label:<20} 准确率 {r.accuracy:.0%}  "
             f"${r.usd_per_task:.6f}/任务")
    dominated = [r for r in results if r not in front]
    if dominated:
        note("")
        note("以下等级被前沿上的某个等级全面碾压，**不应选它们**：")
        for r in dominated:
            better = [o for o in results
                      if o is not r and o.usd_per_task <= r.usd_per_task
                      and o.accuracy >= r.accuracy
                      and (o.usd_per_task < r.usd_per_task or o.accuracy > r.accuracy)]
            tag = f"（被 {better[0].level.key} 碾压）" if better else ""
            note(f"  ✗ {r.level.key} {r.level.label:<20} {tag}")

    # ---- 关键断言：相对 L0 的收益 ----
    head("5. 验证")
    if len(results) >= 2:
        first, last = results[0], results[-1]
        print(f"{VERIFY} accuracy: {first.accuracy:.3f} -> {last.accuracy:.3f} "
              f"({improvement(first.accuracy, last.accuracy, lower_is_better=False)})")
        print(f"{VERIFY} usd_per_task: {first.usd_per_task:.6f} -> {last.usd_per_task:.6f} "
              f"({improvement(first.usd_per_task, last.usd_per_task)})")
        print(f"{VERIFY} p50_latency_ms: {first.stats.p50:.0f} -> {last.stats.p50:.0f} "
              f"({improvement(first.stats.p50, last.stats.p50)})")
        best = max(results, key=lambda r: r.accuracy)
        if base.usd_per_task and best.usd_per_task:
            print(f"{VERIFY} cost_multiple_for_best_accuracy: 1.0 -> "
                  f"{best.usd_per_task / base.usd_per_task:.2f} "
                  f"# direction: increase-expected （准确率从 "
                  f"{base.accuracy:.0%} 提到 {best.accuracy:.0%} 的代价）")

    head("6. 工程结论")
    note("1) **准确率不是免费的**：从最省等级到最准等级，成本倍数与准确率增益一起看，")
    note("   才能判断「多花的钱买到了多少正确率」。")
    note("2) **大模型不一定赢**：如果中模型准确率已接近大模型，大模型就是被碾压的选项。")
    note("3) **自一致性只对推理题有用**：事实题多采样投票是纯浪费（3 倍成本 0 收益）。")
    note("4) **推理等级对题型不敏感**：事实题提高推理等级几乎没有收益，")
    note("   多步推理题才有。所以生产上应该**按题型路由**，而不是全局调高推理等级。")
    note("5) 真实场景还要考虑：错误答案的**下游代价**（答错比答慢贵得多时，应该选准的）。")
    takeaway(
        "推理等级的取舍 = 用确定的成本换不确定的准确率；"
        "先测出帕累托前沿，再按题型路由，最后才谈「全局调高」。"
    )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({
                "backend": "dry-run" if dry else "real",
                "model": cfg.chat_url(),
                "tasks": len(tasks),
                "guard": {"spent_usd": guard.spent_usd, "calls": guard.calls,
                          "tripped": guard.tripped()},
                "levels": [{
                    "key": r.level.key, "label": r.level.label, "tier": r.level.tier,
                    "effort": r.level.effort, "samples": r.level.samples,
                    "n": r.n, "accuracy": r.accuracy,
                    "usd_per_task": r.usd_per_task,
                    "p50_ms": r.stats.p50, "p95_ms": r.stats.p95,
                    "out_tokens_per_task": r.tokens_out_per_task,
                    "by_kind": r.by_kind, "errors": r.errors,
                } for r in results],
                "pareto": [r.level.key for r in front],
            }, f, ensure_ascii=False, indent=2)
        note("")
        note(f"结果已写入 {args.json}")

    note("")
    guard.render()
    return 0


if __name__ == "__main__":
    sys.exit(main())
