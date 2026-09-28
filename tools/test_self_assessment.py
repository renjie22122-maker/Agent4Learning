"""复核 docs/SELF-ASSESSMENT-20260928.md：引用是否真实、数字能否复算。

零依赖、确定性、退出码 0/1。命名成 ``test_*.py`` 是为了被
``tools/test_offline.py``（它会 glob ``tools/test_*.py``）自动带进离线回归套件。

做四件事：

1. **引用可解析**：正文里每个 `` `路径` `` / `` `路径:行号` `` 引用的文件必须存在，
   被引用的行号必须真实存在（防止写出"看起来有出处"的假引用）。
2. **数字可复算**：多 Agent 的 token 放大倍数直接从
   ``.diagnostics/eval-runtime5-*.json`` 重新算出来，不是从结论抄。
3. **关键发现可复现**：自审发现的记账缺陷、lab-15 的成本反向断言
   必须能在对应产物文件里找到原文。
4. **会话证据可复核**：本会话 workspace、对照会话的成功命令，
   都从 ``.sessions/*.jsonl`` 用 JSON 解析出来（不做字符串猜谜）。

关于 SKIP：隔离副本（``agentplat/isolation.py:8``）**不复制** ``.sessions`` 与
``.agent-runtime``。因此在没有 ``.sessions`` 的环境里，会话项检查打印 SKIP 而不是 FAIL，
其余检查照常执行；退出码只看 FAIL。这样同一份脚本在仓库根和隔离副本里都能跑。

用法：
    python -X utf8 tools/test_self_assessment.py
    python -m pytest -q tools/test_self_assessment.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "SELF-ASSESSMENT-20260928.md"
SESSION = "20260928-135004-3999"      # 写这份报告的会话
CONTRAST = "20260927-234118-73c5"     # 工作区选 workspace 子目录、命令可用的对照会话

BARE_DIRS = ("agentplat", "tools", "labs", "docs", "agentlab")
FILE_PREFIXES = ("agentplat/", "tools/", "labs/", "docs/", "agentlab/",
                 ".diagnostics/", ".sessions/", "verify.py", "README.md")
RAW_PREFIXES = (".agent-runtime/",)   # 宿主管理，不允许读，跳过

#: 报告里**故意**提到的"不存在的路径"：提到它们正是为了说明这类能力缺失，
#: 或者是为了记录"这条引用写错过、已修正"。校验方式相反 —— 必须确实不存在。
EXPECTED_ABSENT = ("AGENTS.md", "CLAUDE.md", "agentplat/tracing.py")

_ok: list[str] = []
_bad: list[str] = []
_skip: list[str] = []


def good(msg: str) -> None:
    _ok.append(msg)


def fail(msg: str) -> None:
    _bad.append(msg)


def skip(msg: str) -> None:
    _skip.append(msg)


def resolve(path: str) -> Path | None:
    direct = ROOT / path          # README.md / verify.py 这类就在仓库根
    if direct.is_file():
        return direct
    if "/" in path:
        return None
    for base in BARE_DIRS:
        candidate = ROOT / base / path
        if candidate.is_file():
            return candidate
    return None


def lines_of(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def check_citations(text: str) -> None:
    """每个行内代码引用都要能落到真实文件与真实行号。"""
    cited = resolved = line_hits = absent = absent_sessions = 0
    for span in sorted(set(re.findall(r"`([^`\n]+)`", text))):
        if "*" in span or "\\" in span:
            continue
        head = span.split(" ")[0]
        match = re.fullmatch(
            r"([\w./\-]+\.(?:py|md|json|jsonl|txt|cjs|ps1|html))"
            r"(?::(\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*))?", head)
        if not match:
            continue
        path, spec = match.group(1), match.group(2)
        if path.startswith(RAW_PREFIXES):
            continue
        if path.startswith(".sessions/") and not (ROOT / ".sessions").is_dir():
            # 隔离副本不复制 .sessions（isolation.py:8），这类引用在此处无从核对，
            # 属于"没素材"而不是"引用造假"。
            absent_sessions += 1
            continue
        if path in EXPECTED_ABSENT:
            absent += 1
            if resolve(path) is not None:
                fail(f'{path} 现在存在了，报告里"声明其不存在"的说法需要更新')
            continue
        if "/" in path and not path.startswith(FILE_PREFIXES):
            continue
        cited += 1
        target = resolve(path)
        if target is None:
            fail(f"引用不存在：{path}")
            continue
        resolved += 1
        if spec:
            total = lines_of(target)
            for part in spec.split(","):
                end = max(int(x) for x in part.split("-"))
                line_hits += 1
                if end > total:
                    fail(f"引用行号越界：{path}:{part}（文件只有 {total} 行）")
    good(f"引用可解析：{resolved}/{cited} 个文件引用命中，{line_hits} 个行号引用全部在文件范围内")
    good(f"并已反向验证 {absent} 处「报告声明不存在」的引用确实不存在"
         f"（{'、'.join(EXPECTED_ABSENT)}）")
    if absent_sessions:
        skip(f"{absent_sessions} 条 .sessions/* 引用在本环境无 .sessions 目录可比对（隔离副本不复制它）")


def check_numbers() -> None:
    """多 Agent 成本放大倍数从原始产物重算，不从结论抄。"""
    single = json.loads((ROOT / ".diagnostics" / "eval-runtime5-single.json").read_text(encoding="utf-8"))
    multi = json.loads((ROOT / ".diagnostics" / "eval-runtime5-multi.json").read_text(encoding="utf-8"))
    s_tok = sum(r["parent_tokens"] + r.get("child_tokens", 0) for r in single)
    m_tok = sum(r["parent_tokens"] + r.get("child_tokens", 0) for r in multi)
    ratio = m_tok / s_tok
    good(f"多 Agent 总 token {m_tok:,} / 单 Agent 总 token {s_tok:,} = {ratio:.2f} 倍")
    if ratio <= 3:
        fail(f"报告\"放大 5~7 倍\"的结论不成立：实测只有 {ratio:.2f} 倍")
    if ratio < 5 or ratio > 7:
        fail(f"报告写的是 5~7 倍，实测 {ratio:.2f} 倍，结论需要改口径")
    if not all(r.get("independently_passed") for r in single + multi):
        fail("存在未通过的任务，报告\"质量都通过\"不成立")
    if not all(r.get("cost_is_estimate") for r in multi):
        fail("multi 那份不再是估算口径，报告\"成本口径是 estimate\"需要更新")
    good(f"两组评测共 {len(single + multi)} 次运行全部 independently_passed=True")

    labs = (ROOT / ".diagnostics" / "runtime5-labs.txt").read_text(encoding="utf-8")
    if "subagent_cost_overhead: 98 -> 579" not in labs:
        fail("lab-15 反向断言 subagent_cost_overhead: 98 -> 579 未找到")
    else:
        good("lab-15 反向断言 subagent_cost_overhead: 98 -> 579 已核实")

    inv = (ROOT / ".diagnostics" / "runtime5-invariants.txt").read_text(encoding="utf-8")
    missing = [n for n in ("轮次回退", "轮次记账闭合", "20260928-124522-8e28.jsonl") if n not in inv]
    if missing:
        fail(f"自审发现未在 runtime5-invariants.txt 中找到：{missing}")
    else:
        good("自审发现的\"轮次回退\"与\"轮次记账闭合\"缺陷可复现")


def first_event(path: Path) -> dict:
    with path.open(encoding="utf-8", errors="replace") as fh:
        return json.loads(fh.readline())


def same(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def check_sessions() -> None:
    """本会话 workspace 与对照会话的成功命令，从日志 JSON 里读。

    ``.sessions`` 不在隔离副本里（``isolation.py:8`` 的 IGNORED），
    所以这里区分"检查失败"和"没素材可查"：后者打印 SKIP 而不是 FAIL。
    """
    sessions_dir = ROOT / ".sessions"
    if not sessions_dir.is_dir():
        skip(f"没有 {sessions_dir}（隔离副本不复制 .sessions），2 项会话证据检查本轮跳过")
        return
    mine = sessions_dir / f"{SESSION}.jsonl"
    if not mine.is_file():
        skip(f"没有本会话日志 {mine.name}，本会话证据检查跳过")
        return
    text = mine.read_text(encoding="utf-8", errors="replace")
    created = first_event(mine)
    workspace = created["data"]["workspace"]
    if same(workspace, ROOT):
        good(f"本会话 workspace = {workspace}（仓库根，A1 前提已核实）")
    else:
        fail(f"本会话 workspace 是 {workspace}，不等于仓库根，A1 的前提不成立")
    calls = text.count('"kind": "tool/call"')
    fails = text.count('"ok": false')
    good(f"本会话（仍在进行）已记录工具调用 {calls} 次、失败 {fails} 次")
    if calls < 56 or fails < 7:
        fail(f"报告写的 56/7 高于日志实际值 {calls}/{fails}（脚本按单调下界校验）")

    contrast = ROOT / ".sessions" / f"{CONTRAST}.jsonl"
    if not contrast.is_file():
        skip(f"没有对照会话日志 {contrast.name}，对照证据检查跳过")
        return
    other = first_event(contrast)
    o_ws = Path(other["data"]["workspace"])
    if o_ws.name.lower() == "workspace" and same(o_ws.parent, ROOT):
        good(f"对照会话 workspace = {o_ws}（仓库 workspace 子目录）")
    else:
        fail(f"对照会话 workspace 是 {o_ws}，不是仓库 workspace 子目录")
    ctext = contrast.read_text(encoding="utf-8", errors="replace")
    if '"tool": "run_shell", "ok": true' in ctext:
        good("对照会话确有成功的 run_shell（证明不是沙箱本身坏了）")
    else:
        fail("对照会话里找不到成功的 run_shell，A1 的对照证据不成立")


def check_sandbox_reason() -> None:
    """被引用的 107-109 行必须真的是那条拒绝逻辑。"""
    target = ROOT / "agentplat" / "windows_sandbox.py"
    window = "\n".join(target.read_text(encoding="utf-8").splitlines()[106:109])
    if "is_relative_to(workspace)" in window and "工作区不能包含沙箱运行时" in window:
        good("windows_sandbox.py:107-109 确实是\"工作区包含运行时\"的拒绝逻辑")
    else:
        fail("windows_sandbox.py:107-109 不是声称的拒绝逻辑，报告引用有误")


def run() -> int:
    _ok.clear()
    _bad.clear()
    _skip.clear()
    if not DOC.is_file():
        print("FAIL 报告不存在：docs/SELF-ASSESSMENT-20260928.md")
        return 1
    text = DOC.read_text(encoding="utf-8")
    good(f"报告存在：{DOC.stat().st_size:,} 字节 / {lines_of(DOC)} 行 / "
         f"{text.count(chr(10) + '## ')} 个一级小节")
    for needle in ("## 5. 改进清单", "P0-1", "P1-1", "P2-1", "[记忆推断]", "[已核实]"):
        if needle not in text:
            fail(f"报告缺少必需小节或标记：{needle}")
    good(f"证据强度标记：[已核实] {text.count('[已核实]')} 处、"
         f"[记忆推断] {text.count('[记忆推断]')} 处")

    check_citations(text)
    check_numbers()
    check_sessions()
    check_sandbox_reason()

    print("=" * 78)
    print("  docs/SELF-ASSESSMENT-20260928.md 引用与数字复核")
    print("=" * 78)
    for item in _ok:
        print(f"  PASS  {item}")
    for item in _skip:
        print(f"  SKIP  {item}")
    for item in _bad:
        print(f"  FAIL  {item}")
    print("-" * 78)
    if _bad:
        print(f"  结果：{len(_ok)} 项通过，{len(_skip)} 项跳过，{len(_bad)} 项失败")
        return 1
    tail = f"，{len(_skip)} 项因缺素材跳过" if _skip else ""
    print(f"  结果：{len(_ok)}/{len(_ok)} 项通过{tail}（引用真实、数字可复算）")
    return 0


def test_self_assessment_report() -> None:
    """pytest 入口：报告里的引用与数字必须自洽。"""
    code = run()
    assert code == 0, "报告引用或数字复核未通过：" + "；".join(_bad)


if __name__ == "__main__":
    sys.exit(run())
