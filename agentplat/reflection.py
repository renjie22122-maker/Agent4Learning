"""反射（reflection）：让 agent **在声明完成之前**回头核对证据与需求。

## 为什么需要它 —— 两个真实的机制缺口

在补这一层之前，本项目的"反思"完全依赖两件事：

1. **循环的自然反馈**：工具结果回灌 → 模型看见 → 再决定。
2. **一道只管一半路的闸**：`loop.py` 里"模型没调工具、只想用文字收尾"时，
   必须 `self._verified` 为真才认。

问题在于**模型可以绕开第 2 条**：它直接调 `finish` 工具就行。
`finish` 的 schema 里 `required` 只有 `summary`，没有任何证据要求。
实测就是这么发生的：agent 写完代码、一次都没跑，直接 `finish` 说"已完成"，
而 `res.ok = True`。**"agent 说成功"与"交付物真的可用"是两件事**，
而这条路径上没有任何机制区分它们。

第二个缺口更隐蔽：**没有"对照原始需求复查"的动作**。
用户写四条验收标准，模型满足三条就收尾，`finish` 的总结里不会提第四条。
原始要求确实在上下文里（`compaction.py` 里专门修过"消息头逐字保留"），
但"逐条核对"这个**动作**不存在 —— 上下文里有信息 ≠ 有人去比过。

## 这一层做什么

`ReflectionPolicy` 是"放行 / 拒绝并说明"的接口。每个策略只回答一个问题：
**这次 finish 能不能接受**。拒绝时必须给出**可执行的下一步**，而不是
一句"再检查一下" —— 后者模型只会原样再调一次 finish。

内置两个策略：

- `EvidenceBeforeFinish` —— 有副作用的改动必须先有成功验证。
- `RequirementChecklist` —— 从任务里抽出的需求条目必须逐条交代。

## 它**不**做什么（诚实边界）

- **不能验证模型说的是真的。** `RequirementChecklist` 靠的是 finish 总结里
  **提到了**那条需求，不是真的验证了它。"提到"和"做到"之间有距离。
  真正独立的那一层在 `tools/evaluate_delivery.py`（自己写用例打交付物）。
  所以这一层是**提高撒谎成本**，不是**杜绝撒谎**。
- **不做语义理解。** 需求抽取是规则式的（编号列表 / 必须·不得·要求），
  自然语言里埋的隐含要求抽不出来。抽不出来的就不检查 —— 这一点必须写在
  报告里（`Requirement.checked`），否则"没报问题"会被误读成"全都满足了"。
- **不替代测试。** 它只是逼模型去跑测试，不是自己判断代码对不对。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, Sequence

#: 有副作用、需要证据的工具。改过这些就必须有成功验证。
SIDE_EFFECT_TOOLS = frozenset({
    "write_file", "edit_file", "append_file", "delete_file", "run_shell",
})

#: 验证类命令的特征。命中且退出码 0 才算"证据"。
VERIFY_HINTS = ("pytest", "unittest", "python ", "python3 ", "doctest",
                "assert", "black", "flake8", "cargo test", "go test")

#: 读起来像"硬性要求"的句式。只抽这些，不猜。
_REQ_PATTERNS = (
    re.compile(r"^\s*(?:[-*]|\d+[.、)]|\(\d+\))\s*(.+?)\s*$"),      # 列表项
    re.compile(r"[;；。]\s*(必须[^;；。]{2,120})"),                   # 必须…
    re.compile(r"[;；。]\s*(不得[^;；。]{2,120})"),                   # 不得…
    re.compile(r"[;；。]\s*(要求[^;；。]{2,120})"),                   # 要求…
    re.compile(r"[;；。]\s*(不要[^;；。]{2,120})"),                   # 不要…
)


@dataclass
class Requirement:
    """从任务描述里抽出来的一条需求。

    `checked` 表示这条需求**这一轮真的被检查了**。抽不出来的需求
    不会被检查，所以必须显式记下来 —— 否则"没有报问题"会被误读成
    "全满足了"。这是本项目里反复出现的同一类错误：
    **"没报错"和"没检查"在结果上一样**。
    """

    index: int
    text: str
    checked: bool = True
    #: 为什么没检查（例如"读起来像背景说明而不是要求"）
    unchecked_reason: str = ""


@dataclass
class ReflectionVerdict:
    """一次 finish 的放行结论。"""

    allow: bool
    #: 拒绝时给模型的**可执行**下一步。不能是"再检查一下"。
    instruction: str = ""
    #: 哪条策略做的判断（用于统计与日志）
    by: str = ""
    exhausted: bool = False

    @staticmethod
    def ok() -> "ReflectionVerdict":
        return ReflectionVerdict(True)


class ReflectionPolicy(Protocol):
    """finish 的准入检查。返回 `allow=False` 就拒绝这次完成声明。"""

    name: str

    def check(self, req: "ReflectionRequest") -> ReflectionVerdict: ...


@dataclass
class ReflectionRequest:
    """判断"能不能接受这次完成"所需要的全部事实。"""

    task: str
    summary: str
    files_changed: str = ""
    #: 是否成功跑过验证命令（退出码 0）
    verified: bool = False
    #: 这一轮改动过的工作区文件（来自 Workspace 的审计，不是模型自述）
    files_touched: list[str] = field(default_factory=list)
    #: 失败过的验证命令次数（"验证过但又改坏了"的信号）
    failed_verifies: int = 0
    #: 之前已经被拒绝过几次（避免无限拒绝）
    rejects: int = 0
    #: 需求清单（由 RequirementChecklist 自己抽，放在这里方便策略间共享）
    requirements: list[Requirement] = field(default_factory=list)
    #: 模型对每条需求的交代（finish 的 requirements_met 字段，可空）
    requirements_met: list[str] = field(default_factory=list)


#: 最多拒绝几次。**必须有上限** —— 无上限的拒绝会把 agent 卡在
#: "被拒 → 再试 → 又被拒"的循环里，比放它过去更糟（钱照花，任务永不结束）。
#: 上限之后停止为未验证，绝不把拒绝次数转换为成功。
MAX_REJECTS = 2


def extract_requirements(task: str) -> list[Requirement]:
    """从任务描述里抽出**可核对**的需求条目。

    刻意保守：只认编号/列表项和"必须/不得/要求/不要"这类硬性句式。
    宁可少抽（漏掉的会记成"未检查"），也不要把背景说明当成要求 ——
    误抽会让 agent 为一条根本不存在的要求反复折腾。

    多行任务里，列表项是最主要的来源（实测模型和用户都这么写要求）。
    """
    out: list[Requirement] = []
    seen: set[str] = set()
    for raw in (task or "").splitlines():
        line = raw.strip()
        if not line or len(line) < 4:
            continue
        text = ""
        for pat in _REQ_PATTERNS:
            m = pat.search(line)
            if m:
                text = m.group(1).strip()
                break
        if not text:
            continue
        # 去掉纯格式残留（如只有标点）
        text = text.strip("。.;；,， ")
        if len(text) < 4:
            continue
        key = re.sub(r"\s+", "", text)[:60]
        if key in seen:
            continue
        seen.add(key)
        req = Requirement(index=len(out) + 1, text=text[:160])
        # 没有具体标识的需求标记为**未检查**。
        # 这不是偷懒，是诚实：关键词核对对纯中文软性表述必然失败，
        # 假装能核对会制造一道过不去的墙（agent 怎么写总结都过不了）。
        if not _probes(req.text):
            req.checked = False
            req.unchecked_reason = "没有可核对的具体标识（文件名/命令/函数名）"
        out.append(req)
    return out


def _probes(text: str) -> list[str]:
    """从一条需求里挑出**可核对的具体标识**（文件名 / 命令 / 函数名）。

    ## 两次踩坑的经过，值得留着

    **第一次**：写成"取最长的两个 token"，结果：

        需求：必须跑一次 python -c 自测
        token：['python', '必须跑一次']      ← 「必须跑一次」是句式词
        总结：...跑了 python -c 自测，退出码 0...
        判定：没交代   ← 已经交代过了却被判没过

    **第二次**：加停用词表过滤句式词。但中文没有分词，正则把
    `必须跑一次确认它能` 整段切成一个 token，停用词表匹配不上，
    于是 probe 变成 `['import', '必须跑一次确认它能']` —— 仍然必失败。

    ## 结论（也是这一层真正的设计原则）

    **纯中文的软性表述无法用关键词可靠核对，所以不该假装能核对。**
    只有含**具体标识**的需求才是可机器核对的：
    文件名（`rle.py`、`README.md`）、命令（`python -c`、`pytest`）、
    函数名（`add(text)`）。这类需求撒谎成本高 —— 总结里必须写出那个名字。

    没有具体标识的需求（如"不要留下未实现的函数"）返回空列表，
    由 `extract_requirements` 标记为**未检查**。
    宁可少检查，也不要制造一道过不去的墙 ——
    后者会让 agent 无论怎么做都被拒，比不检查糟得多。
    """
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.\-]{1,}", text)
    keep = [t for t in tokens if t.lower() not in _SOFT_WORDS]
    if not keep:
        return []
    keep.sort(key=len, reverse=True)
    # 取最长的一个 + （若有）另一个不同前缀的，覆盖"文件名 + 命令"这类组合。
    out = [keep[0]]
    for t in keep[1:]:
        if len(out) >= 2:
            break
        if t.lower() not in out[0].lower():
            out.append(t)
    return out


#: 只表示语气、不指向任何具体东西的英文词。
_SOFT_WORDS = frozenset({
    "the", "and", "for", "with", "you", "your", "must", "should", "please",
    "code", "file", "files", "test", "tests",
})


def is_checkable(r: "Requirement") -> bool:
    """这条需求能不能被机器核对（有没有具体标识）。"""
    return bool(_probes(r.text))


def coverage(requirements: Sequence[Requirement],
             summary: str) -> tuple[int, list[Requirement]]:
    """总结里"交代"了多少条**可核对**的需求。返回 (已交代条数, 没交代的条目)。

    未被检查的需求（没有具体标识）**不计入分母** —— 这很重要：
    计进去的话，那些需求永远无法被满足，agent 会被无限拒绝。
    它们由 `extract_requirements` 打上 `checked=False` 标记，
    调用方应当把这个数字也报出来，而不是假装全都核对过了。

    ⚠ 这是**关键词命中**，不是语义判断。"写了 README"和
    "没有写 README"都可能命中 `README` 这个词。所以它只能当
    "提高撒谎成本"的手段，不能当验收。真正的验收在
    `tools/evaluate_delivery.py`（自己写用例打交付物）。
    """
    low = (summary or "").lower()
    missed: list[Requirement] = []
    hit = 0
    for r in requirements:
        if not r.checked:
            continue
        probes = _probes(r.text)
        if probes and all(p.lower() in low for p in probes):
            hit += 1
        else:
            missed.append(r)
    return hit, missed


class EvidenceBeforeFinish:
    """有副作用改动 → 必须有一次成功的验证，否则不接受完成声明。

    为什么这条是硬规则：编码任务里**"没跑过"就等于"不知道能不能跑"**。
    实测过的案例：agent 交了 23 个自测全绿的代码，而在随机数组上会挂死 ——
    那 23 个用例是它自己写的，覆盖不到的地方它不知道。
    "跑过一次"不能保证正确，但"一次都没跑"几乎一定不正确。
    """

    name = "证据闸门"

    def __init__(self, allow_readonly: bool = True):
        self.allow_readonly = allow_readonly

    def check(self, req: ReflectionRequest) -> ReflectionVerdict:
        # 只读任务不要求验证：用户问"这个项目是做什么的"，没有可验证的东西。
        # 判据是**工作区有没有被改过**，而不是模型怎么说。
        changed = [f for f in req.files_touched if f]
        if not changed and self.allow_readonly:
            return ReflectionVerdict(
                True, by=f"{self.name}（本轮没有改动文件，视为只读任务）")
        if req.verified:
            return ReflectionVerdict.ok()
        return ReflectionVerdict(
            False,
            by=self.name,
            instruction=(
                "**这次完成声明被拒绝：你改动了文件，但没有任何一次成功的验证。**\n"
                f"本轮改动过：{', '.join(changed[:6])}\n\n"
                "请现在做这件事（不要只是再调一次 finish）：\n"
                "1. 用 run_shell 跑一次能证明改动可用的命令"
                "（如 `python -m pytest -q`、`python your_script.py`、"
                "`python -c \"import your_module; assert ...\"`）；\n"
                "2. 确认输出里的**退出码为 0**；\n"
                "3. 如果失败，先修再跑，直到通过；\n"
                "4. 然后才调 finish，并在 summary 里**贴出你跑的命令和关键输出**。"
            ),
        )


class RequirementChecklist:
    """逐条核对原始需求，没交代的不接受完成声明。

    这一层补的是"**上下文里有信息 ≠ 有人去比过**"。
    原始要求一直在上下文里（压缩逻辑专门保住消息头），
    但"逐条核对"这个动作原先不存在。
    """

    name = "需求清单"

    def __init__(self, min_text_len: int = 4):
        self.min_text_len = min_text_len

    def check(self, req: ReflectionRequest) -> ReflectionVerdict:
        items = req.requirements or extract_requirements(req.task)
        if not items:
            return ReflectionVerdict(
                True, by=f"{self.name}（任务里没抽出可核对的需求条目）")
        hit, missed = coverage(items, req.summary)
        if not missed:
            return ReflectionVerdict.ok()
        lines = "\n".join(f"  {r.index}. {r.text}" for r in missed[:6])
        return ReflectionVerdict(
            False,
            by=self.name,
            instruction=(
                f"**这次完成声明被拒绝：有 {len(missed)} 条要求没有交代。**\n"
                f"下面是任务里明确写出的要求，你的 summary 里没有提到它们：\n"
                f"{lines}\n\n"
                "请逐条处理（**不要只是把话补进 summary**）：\n"
                "· 还没做的 → 现在做；\n"
                "· 已经做了的 → 在 summary 里指出**具体在哪个文件/哪条命令的输出里**；\n"
                "· 做不到的 → 明确说做不到以及原因，不要省略。"
            ),
        )


class Reflector:
    """把若干 `ReflectionPolicy` 串起来，按顺序问。"""

    def __init__(self, *policies: ReflectionPolicy):
        self.policies = [p for p in policies if p]

    @property
    def enabled(self) -> bool:
        return bool(self.policies)

    def review(self, req: ReflectionRequest) -> ReflectionVerdict:
        for p in self.policies:
            v = p.check(req)
            if not v.allow:
                v.exhausted = req.rejects >= MAX_REJECTS
                return v
        return ReflectionVerdict.ok()

    def names(self) -> list[str]:
        return [getattr(p, "name", type(p).__name__) for p in self.policies]


def default_reflector() -> Reflector:
    """本项目的默认反射配置：证据闸门 + 需求清单。"""
    return Reflector(EvidenceBeforeFinish(), RequirementChecklist())
