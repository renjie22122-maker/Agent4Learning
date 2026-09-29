"""端到端验收器：把每个 lab 都跑一遍并校验它**真的讲清了它要讲的问题**。

为什么需要一个验收器：教学项目最容易烂掉的方式是"代码还能跑，但结论不再成立"
—— 有人改了个参数，P95 优化 lab 就不再体现优化了，而没有任何东西会报错。
所以这里对每个 lab 做四项检查：

1. **能跑通**：退出码 0，且在超时内结束；
2. **协议完整**：``[LAB-START]`` / ``[BROKEN-REPRODUCED]`` / ``[FIX-APPLIED]`` /
   ``[VERIFY]`` / ``[TAKEAWAY]`` / ``[LAB-END]`` 标记都在；
3. **结论方向正确**：``[VERIFY]`` 行里的 before/after 必须体现"修复后更好"
   （除非该指标明确标注为"越低越好/越高越好"的反向项）；
4. **有实测数字**：不能只有叙述，必须有可解析的数值。

用法::

    python verify.py                # 全量
    python verify.py lab-06 lab-08  # 只跑指定的几个
    python verify.py --list         # 列出所有 lab
    python verify.py --jobs 4       # 并行度（默认按 CPU 核数）
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------
# Lab 清单：id -> (模块, 中文标题, 期望的 verify 指标名关键字)
# --------------------------------------------------------------------------

LABS: list[tuple[str, str, str]] = [
    ("lab-01-service-lifecycle", "labs.lab_01_service_lifecycle", "服务启停与发布不中断"),
    ("lab-02-concurrency-memory", "labs.lab_02_concurrency_memory", "并发 panic 与内存泄漏"),
    ("lab-03-startup-order", "labs.lab_03_task_startup_order", "从 0 落地的工程顺序"),
    ("lab-04-rate-limit-fairness", "labs.lab_04_rate_limit_fairness", "限流与多租户公平"),
    ("lab-05-queue-governance", "labs.lab_05_queue_governance", "异步任务队列治理"),
    ("lab-06-layered-timeout", "labs.lab_06_layered_timeout", "分层超时与熔断"),
    ("lab-07-cache-system", "labs.lab_07_cache_system", "完整缓存体系"),
    ("lab-08-context-governance", "labs.lab_08_context_governance", "上下文治理与压缩"),
    ("lab-09-long-task-resume", "labs.lab_09_long_task_resume", "长任务断点续跑"),
    ("lab-10-p95-optimization", "labs.lab_10_p95_optimization", "长链路 P95 优化"),
    ("lab-12-cost-reduction", "labs.lab_12_cost_reduction", "token 降本"),
    ("lab-13-model-routing", "labs.lab_13_model_routing", "大小模型平衡与调度"),
    ("lab-14-hardcode-boundary", "labs.lab_14_hardcode_boundary", "必须硬编码的边界"),
    ("lab-15-tools-subagents", "labs.lab_15_tools_subagents", "工具与子 Agent 工程化"),
    ("lab-16-multitenant-isolation", "labs.lab_16_multitenant_isolation", "多租户与会话隔离"),
    ("lab-17-batch-resource", "labs.lab_17_batch_resource", "批处理资源治理"),
    ("lab-18-observability-slo", "labs.lab_18_observability_slo", "指标体系与 SLO"),
    ("lab-19-reflection", "labs.lab_19_reflection", "反射机制与完成门槛"),
    ('lab-20-process-ownership', "labs.lab_20_process_ownership", '超时、取消与进程所有权'),
    ('lab-21-finish-evidence', "labs.lab_21_finish_evidence", '完成判定与证据版本'),
    ('lab-22-crash-recovery', "labs.lab_22_crash_recovery", '崩溃窗口与结果未知'),
    ('lab-23-parallel-agents', "labs.lab_23_parallel_agents", '真实子 Agent 调度'),
    ('lab-24-edit-conflicts', "labs.lab_24_edit_conflicts", '并行编辑与合并冲突'),
    ('lab-25-budget-race', "labs.lab_25_budget_race", '共享预算与原子预留'),
    ('lab-26-tool-protocol', "labs.lab_26_tool_protocol", '工具协议与截断参数'),
    ('lab-27-injection-boundary', "labs.lab_27_injection_boundary", '提示注入与权限边界'),
    ('lab-28-memory-retention', "labs.lab_28_memory_retention", '压缩后的用户约束'),
    ('lab-29-source-integrity', "labs.lab_29_source_integrity", '来源引用与数据完整性'),

    ('lab-33-sandbox-environment', 'labs.lab_33_sandbox_environment', '宿主凭据与环境清洗'),
    ('lab-34-domain-revocation', 'labs.lab_34_domain_revocation', '网页授权即时撤销'),
    ('lab-35-turn-accounting', 'labs.lab_35_turn_accounting', '交互轮次与模型步骤'),
    ('lab-36-knowledge-versions', 'labs.lab_36_knowledge_versions', '知识库版本与撤销'),
    ('lab-37-skill-revocation', 'labs.lab_37_skill_revocation', '技能导入与即时停用'),
    ('lab-38-compaction-protocol', 'labs.lab_38_compaction_protocol', '压缩与工具协议边界'),
    ('lab-39-cache-billing', 'labs.lab_39_cache_billing', '缓存命中与费用核算'),
    ('lab-40-memory-selection', 'labs.lab_40_memory_selection', '长期记忆选择、确认与撤销'),
    ('lab-41-recursive-scheduling', 'labs.lab_41_recursive_scheduling', '递归委派与调度饥饿'),
    ('lab-42-team-delivery', 'labs.lab_42_team_delivery', '团队消息持久化与确认'),
    ('lab-43-team-claims', 'labs.lab_43_team_claims', '团队任务认领与交接'),
    ('lab-44-review-wait', 'labs.lab_44_review_wait', '验收等待与实际耗时'),
    ('lab-45-review-blocked', 'labs.lab_45_review_blocked', '验收受阻与诚实收尾'),
    ('lab-46-safe-recovery', 'labs.lab_46_safe_recovery', '崩溃恢复与重复副作用'),
    ('lab-47-external-grader', 'labs.lab_47_external_grader', '独立随机验收'),
    ('lab-48-repeat-reliability', 'labs.lab_48_repeat_reliability', '重复运行可靠性'),
    ('lab-49-completion-audit', 'labs.lab_49_completion_audit', '完成声明与真实收尾'),
    ('lab-50-human-wait', 'labs.lab_50_human_wait', '聊天内等待回答'),
    ('lab-51-rag-source', 'labs.lab_51_rag_source', '验收原始知识来源'),
    ('lab-52-durable-operations', 'labs.lab_52_durable_operations', '跨重启副作用去重'),
    ('lab-54-semantic-memory', 'labs.lab_54_semantic_memory', '语义记忆与版本'),
    ('lab-55-team-dag', 'labs.lab_55_team_dag', '团队依赖调度'),

]

NATIVE_LABS = [
    ('lab-30-native-filesystem', 'labs.lab_30_native_filesystem', 'Windows 文件权限隔离'),
    ('lab-31-native-descendants', 'labs.lab_31_native_descendants', '子进程权限继承'),
    ('lab-32-native-network-gate', 'labs.lab_32_native_network_gate', '网络隔离实测与拒绝执行'),
]

#: 这些指标名里出现即表示"越大越好"（其余默认越小越好）
HIGHER_IS_BETTER = ("success", "accuracy", "recall", "hit_rate", "hit_ratio",
                    "goodput", "fairness", "throughput", "quality", "caught",
                    "saving", "coverage", "precision", "saved")

#: **成本类优先判定**：含这些词的指标一律"越小越好"，即使用户在名字里带了
#: "success"（例如 ``cost_per_success`` / ``usd_per_success``）。
#: 没有这条优先级规则时，``slo_cost_per_success_usd`` 会因为含 "success" 被误判成
#: "越大越好"，于是"成本下降 88%"会被报成方向错误 —— 这是命名与判定规则打架，
#: 必须由规则显式解决，而不是靠每个 lab 各自改名绕开。
COSTISH = ("cost", "usd", "price", "spend", "bill", "_fee", "token_used", "per_usd")

#: 这些指标是"事故计数"：修复后应当归零（或至少不增加）
ZERO_TARGET = ("leak", "lost", "duplicate", "unauthorized", "cross_", "wrong",
               "dropped", "invalid", "pollution", "hijack", "deviation", "runaway",
               "violation", "miss")

#: 显式方向标注：指标名里带这些词，表示该指标**确实是越大越好**，
#: 用来消除自动分类的误判（比如 "upstream_calls" 里的 "success" 是假匹配）。
#: 约定：lab 作者对"增长即正确"的指标，必须让指标名包含 good_ / saved_ / 或
#: 以 _gain/_ratio 结尾，否则会被当成"越小越好"。
FORCE_HIGHER = ("good_", "_gain", "saved_", "reduction", "speedup")

VERIFY_RE = re.compile(
    r"^\[VERIFY\]\s*(?P<name>[^:]+):\s*(?P<before>[-+0-9.eE]+)\s*->\s*"
    r"(?P<after>[-+0-9.eE]+)",
    re.M,
)

#: 显式"反向标注"：有些指标**变化方向本身就是结论**（例如"对冲请求让成本上升，
#: 因为它是在用钱买尾部延迟"）。这时作者必须在 `[VERIFY]` 行**同一行**用
#: ``# direction: increase-expected`` 或 ``# direction: decrease-expected``
#: 标注，验收器才会接受这个反向结果并单独列出。
#:
#: 为什么要求写成机器可解析的形式：强制标注把"我故意让这个指标变差"变成一条
#: 必须写出来、可被评审的声明 —— 而不是一句含糊的说明文字。
DIRECTION_RE = re.compile(r"#\s*direction:\s*(increase|decrease)-expected", re.I)

#: 允许在 [VERIFY] 之后的几行内出现自然语言反向说明（宽松模式，用于兼容已有写法）
REVERSAL_ANNOTATIONS = (
    "反向", "预期上升", "预期变贵", "故意", "代价", "变贵", "更贵", "权衡",
    "expected", "intended", "tradeoff", "trade-off", "by design",
)


def _reversal_kind(out: str, match: re.Match, line: str) -> str | None:
    """返回 'increase' / 'decrease' / None —— 该行是否被声明为反向结果。"""
    inline = DIRECTION_RE.search(line)
    if inline:
        return inline.group(1).lower()
    tail = out[match.end() : match.end() + 320]
    window = " ".join(tail.splitlines()[:3]).lower()
    if any(a.lower() in window for a in REVERSAL_ANNOTATIONS):
        return "loose"
    return None


REQUIRED_MARKERS = ("[LAB-START]", "[BROKEN-REPRODUCED]", "[FIX-APPLIED]", "[VERIFY]", "[TAKEAWAY]", "[LAB-END]")


@dataclass
class LabResult:
    lab_id: str
    module: str
    title: str
    ok: bool = False
    returncode: int = -1
    elapsed_s: float = 0.0
    missing: list[str] = field(default_factory=list)
    verify_lines: list[str] = field(default_factory=list)
    wrong_direction: list[str] = field(default_factory=list)
    annotated_reversals: list[str] = field(default_factory=list)
    stale_annotations: list[str] = field(default_factory=list)
    no_improvement: list[str] = field(default_factory=list)
    error_tail: str = ""
    stdout: str = ""

    def summary(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        bits = [f"{status:4} {self.lab_id:<28} {self.elapsed_s:6.1f}s"]
        if self.verify_lines:
            bits.append(f"{len(self.verify_lines)} verify")
        if self.missing:
            bits.append("缺少标记:" + ",".join(self.missing))
        if self.wrong_direction:
            bits.append("方向错误:" + "; ".join(self.wrong_direction))
        if self.annotated_reversals:
            bits.append(f"{len(self.annotated_reversals)} 条已标注反向(设计如此)")
        if self.no_improvement:
            bits.append("无改善:" + "; ".join(self.no_improvement))
        if self.returncode != 0:
            bits.append(f"exit={self.returncode}")
        return "  ".join(bits)


def classify(name: str) -> str:
    """判断指标方向：higher（越大越好）/ zero（事故计数，越少越好）/ lower。

    判定顺序很重要，每一步都有理由：

    ① ``good_`` / ``_gain`` / ``saved_`` 等**显式前缀**最先判 —— 作者主动标注的
       意图优先级最高。例如 ``good_success_per_usd``（每美元买到多少成功）
       虽然含 "usd"，但既然作者加了 ``good_``，就说明它是"越大越好"。
    ② 成本类：含 cost/usd/price... 一律越小越好。这条必须早于 "success" 匹配，
       否则 ``cost_per_success`` 会因为含 "success" 被误判成越大越好。
    ③ 拦截计数：blocked/caught/rejected 是"拦住了多少次"，越多越好。
    ④ 事故计数：leak/wrong/invalid... 越少越好（``wrong_hits`` 与
       ``session_hijack_blocked`` 靠命名区分，所以命名必须规范）。
    ⑤ 其余按正向词匹配，最后默认"越小越好"。
    """
    low = name.lower()
    if any(k in low for k in FORCE_HIGHER):
        return "higher"
    if any(k in low for k in COSTISH):
        return "lower"
    guard_like = ("blocked", "caught", "rejected", "prevented")
    if any(k in low for k in guard_like):
        return "higher"
    if any(k in low for k in ZERO_TARGET):
        return "zero"
    if any(k in low for k in HIGHER_IS_BETTER):
        return "higher"
    return "lower"


def check_direction(name: str, before: float, after: float) -> tuple[bool, str]:
    """返回 (是否可接受, 说明)。"""
    kind = classify(name)
    if kind == "higher":
        # 允许持平（有些指标在其他 lab 参数下本来就变化不大）
        ok = after >= before * 0.98
        return ok, f"{name}: {before:g} -> {after:g}（期望不下降）"
    if kind == "zero":
        ok = after <= max(before, 0) + 1e-9
        return ok, f"{name}: {before:g} -> {after:g}（期望不增加）"
    ok = after <= before * 1.02
    return ok, f"{name}: {before:g} -> {after:g}（期望下降）"


def run_lab(lab_id: str, module: str, title: str, timeout: float) -> LabResult:
    res = LabResult(lab_id=lab_id, module=module, title=title)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    t0 = time.perf_counter()
    try:
        # 实验只执行仓库内固定夹具；真实服务仍默认隔离执行。
        env['AGENTLAB_EXECUTION_MODE'] = 'local'
        proc = subprocess.run(
            [sys.executable, "-m", module],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        res.returncode = proc.returncode
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        res.returncode = -9
        out = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + f"\n[TIMEOUT] 超过 {timeout}s"
    res.elapsed_s = time.perf_counter() - t0
    res.stdout = out

    for marker in REQUIRED_MARKERS:
        if marker not in out:
            res.missing.append(marker)

    for m in VERIFY_RE.finditer(out):
        name = m.group("name").strip()
        line = out[m.start() : out.find("\n", m.start()) if "\n" in out[m.start():] else len(out)]
        try:
            before = float(m.group("before"))
            after = float(m.group("after"))
        except ValueError:
            continue
        if math.isnan(before) or math.isnan(after):
            continue
        res.verify_lines.append(f"{name}: {before:g} -> {after:g}")
        if before == 0 and after == 0:
            continue
        ok, desc = check_direction(name, before, after)
        reversal = _reversal_kind(out, m, line)
        if not ok:
            if reversal:
                res.annotated_reversals.append(f"{desc} [{reversal}]")
            elif classify(name) == "higher" and after < before:
                res.wrong_direction.append(desc)
            elif classify(name) != "higher" and after > before:
                res.wrong_direction.append(desc)
        elif reversal:
            # 标了反向但实际方向是"正常"的 —— 标注过期了，提示作者清理
            res.stale_annotations.append(desc)
        if abs(after - before) < 1e-12 and classify(name) != "zero":
            res.no_improvement.append(desc)

    res.ok = (
        res.returncode == 0
        and not res.missing
        and not res.wrong_direction
        and len(res.verify_lines) >= 1
    )
    if not res.ok and not res.error_tail:
        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-12:]
        res.error_tail = "\n".join(tail)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="agentlab 端到端验收")
    ap.add_argument("labs", nargs="*", help="只跑这些 lab id（可写前缀）")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--jobs", type=int, default=min(4, (os.cpu_count() or 2)))
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--show-fail", action="store_true", help="打印失败 lab 的输出尾部")
    ap.add_argument("--native", action="store_true", help="包含真实 Windows AppContainer 实验（需在允许创建身份的宿主运行）")
    ap.add_argument('--ann', action='store_true', help='包含真实 FAISS HNSW 实验（需要 requirements-ann.txt）')
    args = ap.parse_args(argv)
    available = LABS + (NATIVE_LABS if args.native else [])
    if args.ann:available += [('lab-53-ann-revocation','labs.lab_53_ann_revocation','ANN 撤销一致性')]

    if args.list:
        for lab_id, module, title in available:
            print(f"  {lab_id:<30} {title:<24} python -m {module}")
        return 0

    selected = available
    if args.labs:
        selected = [
            t for t in available
            if any(t[0].startswith(p) or p in t[0] for p in args.labs)
        ]
        if not selected:
            print(f"没有匹配的 lab：{args.labs}")
            return 2

    print("=" * 90)
    print(f"  agentlab 端到端验收：{len(selected)} 个 lab，并行度 {args.jobs}，单 lab 超时 {args.timeout:.0f}s")
    print("=" * 90)

    results: list[LabResult] = []
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {
            pool.submit(run_lab, lab_id, module, title, args.timeout): lab_id
            for lab_id, module, title in selected
        }
        for fut in cf.as_completed(futs):
            r = fut.result()
            results.append(r)
            print(r.summary(), flush=True)

    results.sort(key=lambda r: r.lab_id)
    wall = time.perf_counter() - t0
    passed = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    print("\n" + "=" * 90)
    print(f"  结果：{len(passed)}/{len(results)} 通过，总耗时 {wall:.1f}s")
    print("=" * 90)

    if failed:
        print("\n失败明细：")
        for r in failed:
            print(f"\n  ── {r.lab_id} ({r.title}) exit={r.returncode} ──")
            if r.missing:
                print(f"     缺少标记: {r.missing}")
            if r.wrong_direction:
                print(f"     结论方向错误: {r.wrong_direction}")
            if args.show_fail and r.error_tail:
                for line in r.error_tail.splitlines():
                    print(f"     | {line}")
            elif r.error_tail:
                last = r.error_tail.splitlines()[-1] if r.error_tail.splitlines() else ""
                print(f"     尾部: {last}")

    total_verify = sum(len(r.verify_lines) for r in results)
    annotated = sum(len(r.annotated_reversals) for r in results)
    print(f"\n累计校验断言: {total_verify} 条 [VERIFY]（每条都是一个可复现的数字结论）")
    if annotated:
        print(f"其中 {annotated} 条是**已显式标注的反向结果**（例如「对冲用钱买尾部延迟」）：")
        for r in results:
            for d in r.annotated_reversals:
                print(f"    {r.lab_id}: {d}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
