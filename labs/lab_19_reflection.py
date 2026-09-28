"""Lab: 反射机制 —— 让 agent 在**声明完成之前**回头核对证据与需求。

这个 lab 回答什么问题
--------------------
* 「agent 说"我做完了"，凭什么信它？」
* 「怎么让"完成"这个动作有门槛，而不是模型一句话就通过？」
* 「用户写了四条验收标准，模型只满足三条就收尾 —— 怎么在机制上拦住？」

复现什么故障
-----------
v0 是"无反射"的 agent：`finish` 工具一被调用就 `ok = True`，schema 里除
``summary`` 没有任何要求。于是三种失败全部发生，而且**都不报错**：

1. **零验证交付**：写完文件、一次都没跑，直接 finish。代码能不能跑没人知道。
2. **半成品交付**：改了实现、留下 TODO，finish 说"已完成"。
3. **漏需求交付**：任务里明写四条要求，只做了三条，finish 的总结里不提第四条。

关键点：这三种失败**不是模型坏**，是**机制缺**。同一个脚本化模型，
装上反射闸门之后全部自愈 —— 本 lab 用同一个模型对比，把这件事测出来。

生产正确做法
-----------
``ReflectionPolicy``（放行/拒绝 + **可执行的下一步**）+ 两个内置策略：

* ``EvidenceBeforeFinish`` —— 改过文件就必须有一次成功验证；
  判据是 **workspace 的真实审计记录**，不是模型自述（自述会形成循环论证：
  模型说自己改了、模型说自己验证过）。
* ``RequirementChecklist`` —— 从任务里抽出需求条目，逐条要求在总结里交代。

再加三条工程细节，缺一条都会把机制用坏：

* **拒绝要有上限**（``MAX_REJECTS``）：无上限的拒绝会把 agent 卡在
  "被拒 → 再试 → 又被拒"里，比放它过去更糟（钱照花，任务永不结束）。
* **拒绝理由必须是可执行的下一步**，不能是"再检查一下" —— 后者模型只会
  原样再调一次 finish。
* **验证失败要作废已有的绿**：否则模型可以"先跑绿一次 → 再改坏 → 收尾"。

工程结论
--------
"完成"必须是一个**有门槛的动作**，门槛由机制强制而不是 prompt 请求；
但反射只能**提高撒谎成本**，不能**杜绝撒谎** —— 独立验收（自己写用例打
交付物）仍然不可替代。本 lab 把这两层的分工也测出来。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentlab.providers import Usage  # noqa: E402
from agentlab.util import (  # noqa: E402
    BROKEN, FIX, TAKEAWAY, VERIFY, head, improvement, kv, lab, note, phase,
)
from agentplat.llmconfig import LLMConfig  # noqa: E402
from agentplat.loop import CodingAgent  # noqa: E402
from agentplat.reflection import (  # noqa: E402
    MAX_REJECTS, ReflectionRequest, RequirementChecklist, coverage,
    default_reflector, extract_requirements,
)
from agentplat.workspace import Workspace  # noqa: E402

LAB_ID = "lab-19-reflection"

#: 三条任务，每条都有明确的验收点。
TASKS = {
    "零验证交付": (
        "在工作区写一个 greet.py，函数 greet(name) 返回 'Hello, {name}!'。"
        "必须跑一次确认它能 import。",
        "写完一次都没跑就 finish",
    ),
    "半成品交付": (
        "在工作区写一个 calc.py，实现 add(a, b)。不要留下未实现的函数。",
        "留下 TODO 就说完成了",
    ),
    "漏需求交付": (
        "写一个 notes.py：\n"
        "1) 实现 add(text)；\n"
        "2) 写一个 README.md 说明用法；\n"
        "3) 必须跑一次 python -c 自测。",
        "任务写了三条，只做了一条",
    ),
}


# --------------------------------------------------------------------------
# 脚本化模型：**对上下文作出反应**，不是硬编码剧本
# --------------------------------------------------------------------------
class ScriptedAgentLLM:
    """读上下文决定下一步 —— 这样"有反射/无反射"的差异才是被同一模型产生的。

    如果写成"第 N 次调用返回 X"，那对比的就是两个不同剧本，结论没有意义。
    这个模型每条分支只依赖两件事：**刚才那个工具的结果**、以及
    **上下文里有没有出现反射的拒绝反馈**。

    ⚠ 这里有个设计陷阱，踩过三次，值得完整记下来：

    **陷阱**：把"要不要验证"挂在"有没有被拒"上（`if not saw_reject: 声称完成`）。
    看起来合理，实际会让每一轮都在 `n==1` 上声称完成 —— 因为
    "声称完成的文本"和"诚实的总结文本"被写成了同一句（都提到跑了命令），
    于是反射每次都正确地拒绝它（summary 说跑了、但证据是 false），
    而模型**永远拿不到执行验证的那一轮**。结果是 v1 被拒两次后放行，
    `verified` 仍是 False —— 机制在工作，但工作错了方向。

    **修法**：剧本按 `n` 单调推进，不依赖拒绝：
    `n==0` 写文件 → `n==1` 跑验证 → `n>=2` 诚实总结。
    这样 v0（无反射）会**跳过** `n>=2` 之前的检查直接收尾（在 n==1 声称完成），
    v1 被逼着走到 n>=2。**差异来自闸门，而不是来自两套剧本。**

    另外，判定"刚被拒了"只能用拒绝理由的独有字面量。曾经用 `run_shell`
    和 `没有交代` 判过 —— 而 tool 的 JSON Schema 一直躺在上下文里
    （里面就有 `run_shell`），于是 `saw_reject` 从第一轮起就是真。
    这是"拿上下文里的词做状态判断"的典型失败。
    """

    def __init__(self, task: str):
        self.task = task
        self.turn = 0
        self.saw_reject = False
        self.last_finish_reason = "tool_calls"
        self.calls: list[list] = []

    def complete_with_tools(self, model, messages, tools, timeout_s):
        self.calls.append(list(messages))
        self.turn += 1
        blob = "\n".join((getattr(m, "content", "") or "") for m in messages)
        if "这次完成声明被拒绝" in blob:
            self.saw_reject = True
        results = [m for m in messages if getattr(m, "role", "") == "tool"]
        n = len(results)

        # ---- 任务一：写 greet.py ----
        # ⚠ 注意 `n` 的实际取值：被拒一次之后 n 会**跳到 2**，而不是停在 1 ——
        # 因为被拒的那次 finish 也产生了一条 tool 结果（结果文本就是"任务结束…"），
        # 加上注入的拒绝说明（一条 user 消息）。所以剧本必须按
        # dump 出来的真实 n 写，不能凭直觉写 0/1/2。
        # 这个坑卡了三轮：反射看起来"完全没生效"，实际是剧本分支永远命中不到。
        if "greet.py" in self.task:
            if n == 0:
                return "", [_tc("w1", "write_file", {
                    "path": "greet.py",
                    "content": "def greet(name):\n    return f'Hello, {name}!'\n"})], _u()
            if n == 1:
                # **默认就犯病**：写完立刻声称完成，一次都没跑。
                # v0 会在这里通过；v1 的证据闸门会拒它。
                return "", [_tc("f1", "finish",
                                {"summary": "已写好 greet.py"})], _u()
            if n == 2:
                return "", [_tc("t1", "run_shell", {
                    "command": "python -c \"from greet import greet; "
                               "assert greet('x') == 'Hello, x!'; print('OK')\""})], _u()
            return "", [_tc("f2", "finish", {
                "summary": "已写好 greet.py。跑了 "
                           "`python -c \"from greet import greet; "
                           "assert greet('x')=='Hello, x!'; print('OK')\"`，"
                           "退出码 0，输出 OK —— 确认能 import 且行为正确。"})], _u()

        # ---- 任务二：写 calc.py，不留 TODO ----
        if "calc.py" in self.task:
            if n == 0:
                return "", [_tc("w2", "write_file", {
                    "path": "calc.py",
                    "content": "def add(a, b):\n    return a + b\n\n"
                               "def mul(a, b):\n    # TODO: 还没实现\n    pass\n"})], _u()
            if n == 1:
                return "", [_tc("f2", "finish",
                                {"summary": "已写好 calc.py"})], _u()
            if n == 2:
                return "", [_tc("e2", "write_file", {
                    "path": "calc.py",
                    "content": "def add(a, b):\n    return a + b\n\n"
                               "def mul(a, b):\n    return a * b\n"})], _u()
            if n == 3:
                return "", [_tc("t2", "run_shell", {
                    "command": "python -c \"from calc import add, mul; "
                               "assert add(2,3)==5 and mul(2,3)==6; print('OK')\""})], _u()
            return "", [_tc("f3", "finish", {
                "summary": "calc.py 已完成：add 和 mul 都已实现，没有留下未实现的"
                           "函数（原来的 TODO 已补全）。跑了 python -c 自测，"
                           "退出码 0，输出 OK。"})], _u()

        # ---- 任务三：notes.py + README，前两条都要做 ----
        if n == 0:
            return "", [_tc("w3", "write_file", {
                "path": "notes.py",
                "content": "def add(text):\n    return text\n"})], _u()
        if n == 1:
            # 只做了一条就收尾（v0 的故障：漏需求）
            return "", [_tc("f3", "finish",
                            {"summary": "已实现 add 函数"})], _u()
        if n == 2:
            return "", [_tc("w4", "write_file", {
                "path": "README.md",
                "content": "# notes.py\n\n用法：`from notes import add`\n"})], _u()
        if n == 3:
            return "", [_tc("t3", "run_shell", {
                "command": "python -c \"from notes import add; "
                           "assert add('a')=='a'; print('OK')\""})], _u()
        return "", [_tc("f4", "finish", {
            "summary": "三条都做了：1) notes.py 里实现 add(text)；"
                       "2) 写了 README.md 说明用法；"
                       "3) 跑了 python -c 自测，退出码 0 输出 OK。"})], _u()


def _u():
    return Usage(200, 40, 0)


def _tc(cid, name, args):
    return {"id": cid, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


# --------------------------------------------------------------------------
@dataclass
class Trial:
    task: str
    ok: bool
    verified: bool
    finished: bool
    rejects: int
    reflection: str
    files: list[str] = field(default_factory=list)
    requirement_hit: int = 0
    requirement_total: int = 0


def run_trial(task: str, *, reflection: bool, name: str) -> Trial:
    """跑一次任务。`reflection=False` 就是 v0（无反射闸门）。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td))
        ws.execution_mode = "local"  # 只执行本实验的固定夹具
        agent = CodingAgent(
            llm=ScriptedAgentLLM(task),
            cfg=LLMConfig(provider="mock", model="m", timeout_s=5.0),
            workspace=ws, guard=None,
            session_dir=Path(td) / ".sessions",
            hard_iterations=14,
            invariants=False,          # 本 lab 只测反射，不掺不变量
            reflection=reflection,
        )
        r = agent.run(task, model="m")
        files = sorted(p.name for p in Path(td).iterdir() if p.is_file())
        items = extract_requirements(task)
        hit, _missed = coverage(items, r.summary or "")
        return Trial(
            task=name, ok=r.ok,
            verified=any(s.kind == "tool" and getattr(s, "tool", "")
                         == "run_shell" for s in r.steps),
            finished=r.stopped_by.startswith("finish"),
            rejects=r.reflection_rejects,
            reflection=r.reflection or "（无策略介入）",
            files=files,
            requirement_hit=hit, requirement_total=len(items),
        )


def main() -> int:
    with lab(LAB_ID, "反射机制：让「完成」成为一个有门槛的动作",
             "agent 说「我做完了」，凭什么信它？"):

        phase("1. 复现故障", "(v0：finish 一被调用就通过，schema 里只要求 summary)")

        v0 = [run_trial(t, reflection=False, name=k)
              for k, (t, _why) in TASKS.items()]
        for k, (task, why) in TASKS.items():
            tr = next(x for x in v0 if x.task == k)
            print(f"{BROKEN} {k}：{why} → agent 自评 "
                  f"{'成功' if tr.ok else '未完成'}，实际跑过验证="
                  f"{'是' if tr.verified else '**否**'}，"
                  f"需求交代 {tr.requirement_hit}/{tr.requirement_total}")

        zero_verified = sum(1 for x in v0 if x.ok and not x.verified)
        miss_req = sum(1 for x in v0 if x.requirement_hit < x.requirement_total)
        head("v0 的三个数字")
        kv("自评成功但零验证的任务数", f"{zero_verified} / {len(v0)}")
        kv("需求没交代全的任务数", f"{miss_req} / {len(v0)}")
        kv("finish 被拒次数", "0（没有任何机制会拒绝）")
        note("注意：这三个失败**都不报错**。agent 输出「已完成」，"
             "res.ok=True，界面显示绿色 —— 而代码可能根本跑不起来。")

        phase("2. 装上反射闸门", "(同一个脚本化模型，只多了一层 ReflectionPolicy)")
        v1 = [run_trial(t, reflection=True, name=k)
              for k, (t, _why) in TASKS.items()]
        for k, _why in TASKS.items():
            tr = next(x for x in v1 if x.task == k)
            print(f"{FIX} {k}：跑过验证={'是' if tr.verified else '否'}，"
                  f"需求交代 {tr.requirement_hit}/{tr.requirement_total}，"
                  f"finish 被拒 {tr.rejects} 次 → 最终 "
                  f"{'接受' if tr.ok else '未完成'}"
                  f"（{tr.reflection}）")

        zero_verified1 = sum(1 for x in v1 if x.ok and not x.verified)
        miss_req1 = sum(1 for x in v1 if x.requirement_hit < x.requirement_total)
        rejects = sum(x.rejects for x in v1)

        phase("3. 量出效果", "(同一个模型、同一批任务，只差一层反射)")
        # ⚠ 格式必须严格是 `[VERIFY] name: before -> after` ——
        # verify.py 的正则要求**半角冒号 + 空格 + 数字**。
        # 用全角「：」或把数字包在句子里都会让验收器解析不到这条断言，
        # 结果是"lab 通过但零条断言"（静默失效，比失败更难发现）。
        imp1 = improvement(zero_verified, zero_verified1,
                           lower_is_better=True, choose_better=True)
        print(f"{VERIFY} 零验证就交付的任务数: {zero_verified} -> "
              f"{zero_verified1} （{imp1}）")
        imp2 = improvement(miss_req, miss_req1,
                           lower_is_better=True, choose_better=True)
        print(f"{VERIFY} 需求没交代全的任务数: {miss_req} -> {miss_req1} （{imp2}）")
        # 被拒次数**上升**才是对的：说明门槛在工作。
        # direction: increase-expected
        print(f"{VERIFY} finish 被拒次数: 0 -> {rejects} "
              f"# direction: increase-expected")
        note("被拒次数上升是**期望结果**：门槛在工作。零拒绝只有两种可能 —— "
             "要么模型一次就做对了（实测很少），要么闸门没生效。")

        phase("4. 机制的边界", "(反射只能提高撒谎成本，不能杜绝撒谎)")
        items = extract_requirements(TASKS["漏需求交付"][0])
        head("需求抽取：规则式，抽不出来的就不检查")
        for r in items:
            kv(f"  第 {r.index} 条", r.text)
        plain = ("写一个工具函数，注意性能和可读性的平衡，"
                 "尽量优雅一点，别写得太啰嗦")
        kv("对一段没有硬性句式的任务抽出几条", str(len(extract_requirements(plain))))
        note("抽出 0 条 = **这条任务不会被需求清单检查**。"
             "报告里必须把这件事说出来，否则「没报问题」会被读成「全都满足了」"
             "—— 这正是本项目反复出现的同一类错误：没报错 ≠ 没检查。")

        head("关键词命中的局限：")
        fake = "我没有写 README.md"
        hit, missed = coverage(items, fake)
        kv("总结里写「我没有写 README.md」", f"判定为已交代 {hit} 条")
        note("命中靠的是词在不在，不是语义。所以这一层**不能当验收** —— "
             "真正的验收在 tools/evaluate_delivery.py：自己写用例去打交付物。")

        head("拒绝次数有上限，但上限不能转换成成功")
        box = RequirementChecklist()
        stuck = ReflectionRequest(
            task=TASKS["漏需求交付"][0], summary="做完了",
            verified=True, files_touched=["notes.py"], rejects=0)
        n_deny = 0
        for i in range(MAX_REJECTS + 3):
            stuck.rejects = i
            if not box.check(stuck).allow:
                n_deny += 1
        kv("同一份不达标交付，连续问若干次", f"{n_deny} 次均未接受；执行循环达到上限后返回未验证")
        kv("上限常量", f"MAX_REJECTS = {MAX_REJECTS}")
        note("无上限的拒绝 = agent 卡在「被拒→再试→又被拒」，"
             "钱照花而任务永不结束。达到上限后停止并返回「未验证」，"
             "不能把未满足说成完成。")

        head("验证失败要作废已有的绿：")
        v_bad = default_reflector().review(ReflectionRequest(
            task="写 x.py 并跑通", summary="写好了，跑过测试",
            verified=False, failed_verifies=1, files_touched=["x.py"]))
        kv("跑绿过一次、之后又改坏（verified 被作废）", "拒绝" if not v_bad.allow
           else "放行")
        note("否则模型可以「先跑绿一次 → 再改坏 → 收尾」，"
             "而它交出来的代码从没被验证过。")

        phase("5. 结论")
        kv("v0 零验证交付", f"{zero_verified}/{len(v0)} 个任务")
        kv("v1 零验证交付", f"{zero_verified1}/{len(v1)} 个任务")
        kv("v0 漏需求交付", f"{miss_req}/{len(v0)} 个任务")
        kv("v1 漏需求交付", f"{miss_req1}/{len(v1)} 个任务")
        kv("反射层数", "2（证据闸门 + 需求清单）")
        kv("本 lab 用时", f"{time.time() % 1:.0f}s（纯本地，零模型调用）")

        print(TAKEAWAY, "「完成」必须是机制强制的门槛，不是 prompt 里的请求："
                        "没有证据不许收尾、有需求必须逐条交代；"
                        "但反射只提高撒谎成本，独立验收仍然不可替代。")
    return 0


QUESTIONS = [
    "agent 说完成了凭什么信？ -> 不信：把 finish 变成有门槛的动作，"
    "没有验证证据就拒绝，并回灌可执行的下一步",
    "拒绝之后怎么让模型真的去改？ -> 拒绝理由必须是可执行的下一步"
    "（跑哪条命令、贴什么输出），不能是「再检查一下」",
    "为什么要给拒绝设上限？ -> 无上限的拒绝会让 agent 卡在"
    "「被拒→再试→又被拒」，由执行循环有界停止并记为未验证，不能放行为成功",
    "为什么验证失败要作废已有的绿？ -> 否则可以「先跑绿→再改坏→收尾」，"
    "交出的代码从没被验证过",
    "反射能替代验收吗？ -> 不能。需求清单靠关键词命中，"
    "连「我没有写 README」都会判为已交代；真正的验收是自己写用例打交付物",
]

if __name__ == "__main__":
    sys.exit(main())
