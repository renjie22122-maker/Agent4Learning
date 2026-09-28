"""Agent 工作区：文件读写与命令执行的**唯一入口**，也是安全边界。

为什么要有这一层
----------------
给它 shell 和写文件权限之后，风险是真实的 —— 一个幻觉就可能写错路径、
删错目录、或者把命令跑到仓库外面。所以所有文件操作**必须**经过这里，
而不是让工具各自去调 `open()`：

* **路径必须落在工作区内**。用 `resolve()` 解出真实路径（消掉 `..` 和符号链接）
  再判断前缀，而不是检查字符串里有没有 `..` —— 后者能被各种编码绕过。
* **命令在工作区里执行**，并且挡掉明显的破坏性写法。
* 所有操作留**审计记录**（谁在什么时候改了什么），这是 lab-16 的结论。

注意：这不是"完美的沙箱"。OS 隔离由 native AppContainer 或 docker 后端提供。这一层的定位是
**防手滑，不防恶意** —— 让 agent 在正常情况下不可能跑到工作区外面去。
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import signal
import sys
import tempfile
import json
import uuid
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

#: 默认工作区：仓库根下的 workspace/。**不要**把 agent 指向仓库根，
#: 否则它可能改坏自己的源码（而且这个项目里源码就是教具）。
DEFAULT_WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"

#: 单文件读写上限。防止 agent 一次读进一个巨大的二进制把上下文撑爆。
MAX_READ_BYTES = 200_000

#: 明显具破坏性/越界的命令模式。刻意保守：宁可多拦，让用户手动执行。
FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+(-[a-zA-Z]*\s+)*/(\s|$)", "禁止删除根目录"),
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+[~$]", "禁止递归删除家目录/变量展开路径"),
    (r"\bmkfs(\.\w+)?\b", "禁止格式化文件系统"),
    (r"\bdd\s+.*of=/dev/", "禁止直接写块设备"),
    (r":\(\)\s*\{.*\};:", "禁止 fork 炸弹"),
    (r"\bshutdown\b|\breboot\b", "禁止关机/重启"),
    (r"\bchmod\s+-R\s+777\s+/(\s|$)", "禁止对根目录递归改权限"),
    (r"\b(curl|wget)\b.*\|\s*(ba)?sh", "禁止「下载后直接执行」"),
    (r"\bgit\s+push\b", "禁止推送远端（演示环境不需要）"),
    (r"\b(npm|pnpm|yarn)\s+publish\b|\bpip\s+install\b.*--user", "禁止发布/改用户环境"),
    (r">\s*/dev/sd", "禁止写裸设备"),
    (r"\bdel\s+/[sfq]\b|\bformat\s+[a-zA-Z]:", "禁止 Windows 破坏性命令"),
)

#: 允许执行的命令白名单（第一个词）。比黑名单安全得多。
#: 只放"开发日常需要"的：跑测试、跑 Python、看目录、git 只读。
ALLOWED_COMMANDS = {
    "python", "python3", "py", "pytest", "unittest",
    "ls", "dir", "cat", "type", "head", "tail", "wc", "find", "findstr", "grep",
    "echo", "pwd", "cd", "mkdir", "touch", "cp", "copy", "mv", "move",
    "git", "node", "npm", "npx", "tsc", "go", "cargo", "java", "javac",
    "sort", "uniq", "diff", "tree", "where", "which", "sed", "awk", "jq",
    "python.exe", "pip", "pip3", "ruff", "black", "mypy", "flake8",
}

#: git 子命令里允许的（只读 + 本地提交，不允许 push/remote 变更）
ALLOWED_GIT_SUBCOMMANDS = {
    "status", "diff", "log", "show", "branch", "add", "commit", "stash",
    "checkout", "restore", "rev-parse", "ls-files", "init",
}


class WorkspaceError(Exception):
    """越界或违规操作。**这类错误必须抛出，不能"降级处理"** ——
    安全边界一旦允许降级，就等于没有边界。
    """


def _split_segments(cmd: str) -> list[str]:
    """按 `&&` `||` `;` `|` 切分命令，**引号感知**。

    ## 为什么不能用 `re.split`

    早期实现是 `re.split(r"&&|\\|\\||;|\\|", cmd)`。它对下面这条命令是错的：

        python -c "import pygame; print(pygame.__version__)"

    正则会把**引号内**的分号也当成命令分隔符，切成两段，第二段首词变成
    `print` —— 于是一个完全合法的命令被白名单拒绝，报错还说
    "命令 'print(pygame.__version__)' 不在允许列表里"。

    实测后果不是"少支持一种写法"，而是**把 agent 卡死**：
    它想检查 pygame 装没装 → 被拒 → 原样重试 → 又被拒，
    最后撞上别的错误整个任务失败。

    ## 这个 bug 的毒性在哪

    命令行解析的失败有对称的两面：
      · **非法输入被放行** = 安全问题，有人会去查；
      · **合法输入被拒** = 只是"agent 变笨了"，没人查，只会觉得模型不行。
    正则切分同时在这两面出错，而只有一面会被发现。

    ## 实现

    手写状态机，跟踪单引号 / 双引号 / 反斜杠转义。
    不做完整的 shell 解析（那需要 `shlex` 的全部复杂性，而这里的目的是
    **安全校验**而不是执行）—— 只保证"不在引号内切分"这一件事。
    多切一段是安全的（会拒掉本该允许的），少切一段是不安全的（会放行尾巴），
    所以遇到看不懂的结构一律**保守处理**：当作没闭合的引号，剩下的不切。
    """
    out: list[str] = []
    cur: list[str] = []
    quote = ""          # "" | "'" | '"'
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"' and i + 1 < n:
                # 双引号内的反斜杠转义：连下一个字符一起吃掉，
                # 否则 `"\""` 这种会把引号提前闭合。
                cur.append(cmd[i + 1])
                i += 1
        elif ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == "\\" and i + 1 < n:
            cur.append(ch)
            cur.append(cmd[i + 1])
            i += 1
        elif ch in (";", "|", "&"):
            # `|` / `||` / `&&` 都算分隔符；单个 `&` 也按分隔符处理（保守）。
            nxt = cmd[i + 1] if i + 1 < n else ""
            if ch in ("|", "&") and nxt == ch:
                i += 1
            out.append("".join(cur))
            cur = []
        elif ch == ">" and not quote:
            # 重定向：`>` `>>` `2>` `2>&1` `&>` 都算**语法**，不是命令。
            # 早期版本把它留下的尾巴（`1`）当成了命令名，于是
            # `python -m pytest -q 2>&1 | findstr PASS` 被拒，
            # 理由是"命令 '1' 不在允许列表里" —— 模型根本看不出问题在哪。
            #
            # 这里只做**归一化**：吃掉重定向符号及其目标，不改变安全性 ——
            # 重定向本身不会执行别的程序（`> file` 只是写文件，
            # 而写文件已经被工作区边界管住了）。
            while i < n:
                c2 = cmd[i]
                if c2 == ">":
                    i += 1
                elif c2 == "&":
                    i += 1
                    # `&1` / `&2` 这种"合并到某个 fd"
                    while i < n and cmd[i].isdigit():
                        i += 1
                elif c2.isdigit() or c2 in "-":
                    # `2>` 的文件描述符前缀、`&-`（关闭 fd）
                    i += 1
                elif c2 == " ":
                    i += 1
                else:
                    break
            # 重定向目标是文件/设备名，跳过它（引号感知）
            while i < n and cmd[i] == " ":
                i += 1
            if i < n and cmd[i] in ("'", '"'):
                q = cmd[i]
                i += 1
                while i < n and cmd[i] != q:
                    i += 1
                i += 1
            else:
                while i < n and cmd[i] not in " \t;|&":
                    i += 1
            i -= 1                      # 外层还会 +1
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [s for s in out if s.strip()]


@dataclass
class AuditEntry:
    ts: float
    action: str
    target: str
    ok: bool
    detail: str = ""


@dataclass
class FileChange:
    path: str
    action: str  # create | modify | delete
    added: int = 0
    removed: int = 0
    ts: float = field(default_factory=time.time)


class Workspace:
    """工作区句柄。所有工具都通过它访问文件系统与 shell。"""

    def __init__(self, root: Path | str | None = None, allow_shell: bool = True):
        self.roots = {name: Path(path).resolve() for name, path in root.items()} if isinstance(root, dict) else {'main': Path(root or DEFAULT_WORKSPACE).resolve()}
        if not self.roots or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}', k) for k in self.roots):
            raise WorkspaceError('文件夹别名无效')
        folders = list(self.roots.values())
        if any(a.is_relative_to(b) or b.is_relative_to(a) for i,a in enumerate(folders) for b in folders[i+1:]):
            raise WorkspaceError('文件夹不能重复或相互包含')
        self.root = next(iter(self.roots.values()))
        self.scope = self.roots if list(self.roots) != ['main'] else self.root
        self.root.mkdir(parents=True, exist_ok=True)
        self.allow_shell = allow_shell
        from .processes import ProcessSupervisor
        self.processes = ProcessSupervisor()
        self.cancel_event = threading.Event()
        self.last_execution = None
        from .execution import execution_mode, native_network_policy
        self.execution_mode = execution_mode()
        self.native_network = native_network_policy()
        from .knowledge import database_root
        self.knowledge_root = database_root(self.root)
        self._lock = threading.RLock()
        self.audit: list[AuditEntry] = []
        self.changes: list[FileChange] = []
        self.commands_run = 0
        self.bytes_written = 0
        self.m_ops = None  # 由外部注入 METRICS counter，避免核心层依赖 UI

    # -- 路径安全 -----------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        """把相对路径解析成工作区内的绝对路径，越界即报错。

        **必须用 resolve() 解真实路径再判前缀**：只检查字符串里有没有 ".."
        很容易被绕过（拼接、符号链接、URL 编码、Windows 短路径名等）。
        """
        raw = (rel or "").strip().strip('"').strip("'")
        if not raw:
            raise WorkspaceError("路径为空")
        base = self.root
        if raw.startswith('@'):
            alias, _, raw = raw.replace('\\', '/')[1:].partition('/')
            if alias not in self.roots:
                raise WorkspaceError(f'未知文件夹：@{alias}')
            base = self.roots[alias]
            raw = raw or '.'
            if Path(raw).is_absolute():
                raise WorkspaceError('文件夹别名后必须是相对路径')
        p = Path(raw)
        candidate = (base / p).resolve() if not p.is_absolute() else p.resolve()
        if p.is_absolute():
            base = next((r for r in self.roots.values() if candidate.is_relative_to(r)), base)
        try:
            candidate.relative_to(base)
        except ValueError:
            raise WorkspaceError(
                f"路径越界：{raw} 解析到 {candidate}，不在工作区 {self.root} 内。"
                f"agent 只能在 {self.root} 下操作。"
            ) from None
        if '.agent-runtime' in [part.lower() for part in candidate.relative_to(base).parts]:
            raise WorkspaceError('宿主管理配置与知识库原件仅能通过专用授权工具访问')
        if candidate.is_file() and candidate.stat().st_nlink > 1:
            raise WorkspaceError('拒绝通过硬链接访问工作区文件')
        return candidate

    def rel(self, path: Path) -> str:
        for alias, root in self.roots.items():
            if path.is_relative_to(root):
                return ('' if root == self.root else '@' + alias + '/') + path.relative_to(root).as_posix()
        try:
            return str(path.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(path)

    def _audit(self, action: str, target: str, ok: bool, detail: str = "") -> None:
        with self._lock:
            self.audit.append(AuditEntry(time.time(), action, target, ok, detail))
            if len(self.audit) > 500:
                del self.audit[:100]
            if self.m_ops is not None:
                self.m_ops.inc()

    # -- 读 ----------------------------------------------------------------
    def list_dir(self, rel: str = ".", pattern: str = "*", limit: int = 200) -> str:
        target = self.resolve(rel)
        if not target.exists():
            raise WorkspaceError(f"目录不存在：{rel}")
        if target.is_file():
            return f"{self.rel(target)} 是文件，不是目录（用 read_file 读它）"
        entries: list[str] = []
        for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if child.name.startswith(".") and child.name not in (".gitignore",):
                continue
            if pattern != "*" and not fnmatch.fnmatch(child.name, pattern):
                continue
            size = ""
            if child.is_file():
                try:
                    size = f"  {child.stat().st_size:>8,}B"
                except OSError:
                    pass
            entries.append(f"{'[D]' if child.is_dir() else '[F]'} {child.name}{size}")
            if len(entries) >= limit:
                entries.append(f"…（超过 {limit} 项已截断）")
                break
        self._audit("list_dir", rel, True)
        head = f"{self.rel(target)}/  共 {len(entries)} 项"
        return head + "\n" + "\n".join(entries) if entries else head + "\n（空目录）"

    def read_file(self, rel: str, start_line: int = 1, max_lines: int = 400) -> str:
        target = self.resolve(rel)
        if not target.exists():
            raise WorkspaceError(f"文件不存在：{rel}（用 list_dir 看看目录里有什么）")
        if target.is_dir():
            raise WorkspaceError(f"{rel} 是目录，不是文件")
        start = max(1, int(start_line))
        count = max(1, min(2000, int(max_lines)))
        chunk = []
        used = 0
        more = False
        with target.open(encoding="utf-8", errors="replace") as f:
            for number in range(1, start):
                # readline(size) 避免单条超长行导致无界内存。
                line = f.readline(MAX_READ_BYTES + 1)
                while line and not line.endswith("\n") and len(line) > MAX_READ_BYTES:
                    line = f.readline(MAX_READ_BYTES + 1)
                if not line:
                    break
            for number in range(start, start + count):
                line = f.readline(MAX_READ_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_READ_BYTES:
                    raise WorkspaceError("单行过长，请用 read_chunk 按字节读取")
                if used + len(line.encode("utf-8")) > MAX_READ_BYTES:
                    more = True
                    break
                chunk.append(f"{number:>5}| {line.rstrip(chr(10)).rstrip(chr(13))}")
                used += len(line.encode("utf-8"))
            else:
                more = bool(f.read(1))
        self._audit("read_file", rel, True)
        footer = f"\n…继续读用 start_line={start + len(chunk)}" if more else ""
        return f"{self.rel(target)}（显示 {len(chunk)} 行）\n" + "\n".join(chunk) + footer

    def read_chunk(self, rel: str, offset: int = 0, max_bytes: int = 8000) -> str:
        target = self.resolve(rel)
        with target.open('rb') as f:
            f.seek(max(0, offset))
            data = f.read(max(1, min(max_bytes, MAX_READ_BYTES)))
            cursor = f.tell()
        return json.dumps(dict(path=self.rel(target), offset=offset, next_offset=cursor,
                               eof=cursor >= target.stat().st_size,
                               text=data.decode('utf-8', 'replace')), ensure_ascii=False)

    def grep(self, pattern: str, rel: str = ".", glob: str = "*", limit: int = 60) -> str:
        # 用户正则可在 C 引擎中灾难性回溯，必须隔离进程，不靠线程超时。
        target = self.resolve(rel)
        root = next(r for r in self.roots.values() if target.is_relative_to(r))
        request = json.dumps(dict(root=str(root), pattern=pattern, rel=target.relative_to(root).as_posix(), glob=glob, limit=limit))
        task_id = self.processes.start([sys.executable, '-m', 'agentplat.worker', request],
                                      Path(__file__).resolve().parents[1], timeout_s=5)
        try:
            while True:
                state = self.processes.wait(task_id, .1)
                if self.cancel_event.is_set():
                    state = self.processes.cancel(task_id)
                if state['status'] != 'running':
                    break
            if state['status'] != 'exited' or state['exit_code'] != 0:
                raise WorkspaceError('搜索超时、取消或失败：' + state['output'][-500:])
            result = json.loads(state['output'])
            if not result['ok']:
                raise WorkspaceError(result['error'])
            self._audit('grep', pattern, True)
            return result['text']
        finally:
            self.processes.release(task_id)

    def _grep_inline(self, pattern: str, rel: str = ".", glob: str = "*", limit: int = 60) -> str:
        target = self.resolve(rel)
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise WorkspaceError(f"正则不合法：{exc}") from None
        hits: list[str] = []
        files = [target] if target.is_file() else [
            p for p in target.rglob("*") if p.is_file()
        ]
        for f in files:
            if f.stat().st_size > MAX_READ_BYTES:
                continue
            if glob != "*" and not fnmatch.fnmatch(f.name, glob):
                continue
            if any(part in (".git", "__pycache__", "node_modules", ".lab_state", ".agent-runtime")
                   for part in f.parts):
                continue
            try:
                f = self.resolve(str(f))
                for i, ln in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(ln):
                        hits.append(f"{self.rel(f)}:{i}: {ln.strip()[:150]}")
                        if len(hits) >= limit:
                            break
            except (OSError, WorkspaceError):
                continue
            if len(hits) >= limit:
                break
        self._audit("grep", f"{rel} :: {pattern}", True)
        if not hits:
            return f"没有匹配 {pattern!r} 的内容（范围 {rel}, 文件名 {glob}）"
        return f"匹配 {len(hits)} 处：\n" + "\n".join(hits)

    def _write_target(self, rel):
        target = self.resolve(rel)
        root = next(r for r in self.roots.values() if target.is_relative_to(r))
        parts = target.relative_to(root).parts
        if any(p.lower() in {'.sources', '.sessions', '.agent-runtime', '.git', '.spill'} for p in parts):
            raise WorkspaceError('运行时记录和来源证据目录不允许通过文件编辑工具修改')
        return target

    # -- 写 ----------------------------------------------------------------
    def write_file(self, rel: str, content: str) -> str:
        target = self._write_target(rel)
        existed = target.exists()
        old = target.read_text(encoding="utf-8", errors="replace") if existed else ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        added = max(0, len(content.splitlines()) - len(old.splitlines())) if existed else len(content.splitlines())
        removed = max(0, len(old.splitlines()) - len(content.splitlines())) if existed else 0
        with self._lock:
            self.bytes_written += len(content.encode("utf-8"))
            self.changes.append(FileChange(self.rel(target),
                                           "modify" if existed else "create",
                                           added, removed))
        self._audit("write_file", rel, True, f"{len(content)} 字符")
        return (f"{'已更新' if existed else '已创建'} {self.rel(target)}"
                f"（{len(content.splitlines())} 行，{len(content.encode('utf-8')):,} 字节）")

    def edit_file(self, rel: str, old_text: str, new_text: str,
                  replace_all: bool = False) -> str:
        """精确字符串替换。

        为什么不做"按行号改"：行号会随每次编辑漂移，模型记的行号几乎必然过期，
        结果改错地方。**精确文本匹配**是唯一可靠的定位方式 —— 匹配不到就报错，
        让模型重新读文件，而不是猜。
        """
        target = self._write_target(rel)
        if not target.exists():
            raise WorkspaceError(f"文件不存在：{rel}")
        text = target.read_text(encoding="utf-8", errors="replace")
        if not old_text:
            raise WorkspaceError("old_text 不能为空")
        count = text.count(old_text)
        if count == 0:
            raise WorkspaceError(
                f"在 {rel} 里找不到要替换的文本。请先 read_file 确认原文"
                f"（注意缩进和换行要完全一致）。"
            )
        if count > 1 and not replace_all:
            raise WorkspaceError(
                f"要替换的文本在 {rel} 里出现了 {count} 次，无法确定改哪一处。"
                f"请提供更长的上下文让它唯一，或设 replace_all=true。"
            )
        updated = text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1)
        target.write_text(updated, encoding="utf-8")
        old_lines = text.splitlines()
        new_lines = updated.splitlines()
        with self._lock:
            self.bytes_written += len(updated.encode("utf-8"))
            self.changes.append(FileChange(self.rel(target), "modify",
                                           max(0, len(new_lines) - len(old_lines)),
                                           max(0, len(old_lines) - len(new_lines))))
        self._audit("edit_file", rel, True, f"替换 {count if replace_all else 1} 处")
        return (f"已修改 {self.rel(target)}：替换 {count if replace_all else 1} 处，"
                f"现在 {len(new_lines)} 行（原 {len(old_lines)} 行）")

    def delete_file(self, rel: str) -> str:
        target = self.resolve(rel)
        if not target.exists():
            raise WorkspaceError(f"文件不存在：{rel}")
        if target.is_dir():
            raise WorkspaceError("只能删文件；目录请手工处理（避免误删一整棵子树）")
        target.unlink()
        with self._lock:
            self.changes.append(FileChange(self.rel(target), "delete"))
        self._audit("delete_file", rel, True)
        return f"已删除 {self.rel(target)}"

    # -- 执行 --------------------------------------------------------------
    def check_command(self, cmd: str) -> None:
        """命令白名单 + 破坏性模式检查。**在真正执行之前**做。"""
        stripped = cmd.strip()
        if not stripped:
            raise WorkspaceError("命令为空")
        if getattr(self, 'trusted_host_commands', False):
            return  # Set only by the authenticated host permission selector.
        for pattern, reason in FORBIDDEN_PATTERNS:
            if re.search(pattern, stripped, re.IGNORECASE):
                raise WorkspaceError(f"命令被安全检查拦下：{reason}\n命令：{stripped[:200]}")
        # 取每个 && / || / ; / | 分段的首词做白名单校验，避免"合法命令 + 恶意尾巴"。
        #
        # ⚠ 切分**必须引号感知**。早期版本直接用 `re.split(r"&&|\|\||;|\|")`，
        # 于是 `python -c "import pygame; print(pygame.__version__)"` 会被切成
        # `python -c "import pygame` 和 ` print(pygame.__version__)"` 两段，
        # 第二段的首词变成 `print` —— 一个完全合法的命令被拒，报错还说
        # "命令 'print(...)' 不在允许列表里"。
        # 实测就是这么把 agent 卡死的：它想检查 pygame 装没装，结果被拦，
        # 于是反复重试同一件事，最后撞上协议错误整个任务失败。
        #
        # 这个 bug 的教训是：**解析命令行不能用正则切分**。引号、转义、嵌套
        # 都会让正则的假设失效，而失效的表现是"合法输入被拒"，
        # 比"非法输入被放行"更难发现（后者至少是安全问题，有人会去查）。
        for segment in _split_segments(stripped):
            seg = segment.strip()
            if not seg:
                continue
            first = seg.split()[0].lower().strip("'\"")
            first = os.path.basename(first)
            if first not in ALLOWED_COMMANDS:
                # ⚠ 报错必须让人（和模型）看出**问题出在哪一段**。
                # 只回一句"命令 '1' 不在允许列表里"是没用的：
                # 模型看不到自己写的是 `2>&1`，只会原样重试。
                # 实测这条比白名单本身更容易把人卡住。
                hint = ""
                if first.isdigit() or ">" in seg or first in ("2", "1"):
                    hint = (f"（看起来是**重定向语法**被当成了命令：`{seg.strip()[:40]}`。"
                            f"这个沙箱不做 shell 重定向，请把输出直接打出来，"
                            f"或用 `|` 接白名单里的过滤命令如 findstr。）")
                raise WorkspaceError(
                    f"命令 {first!r} 不在允许列表里。可用："
                    f"{', '.join(sorted(ALLOWED_COMMANDS))}{hint}"
                )
            if first == "git":
                parts = seg.split()
                if len(parts) > 1:
                    sub = parts[1].lower()
                    if sub.startswith("-"):
                        sub = parts[2].lower() if len(parts) > 2 else ""
                    if sub and sub not in ALLOWED_GIT_SUBCOMMANDS:
                        raise WorkspaceError(
                            f"git {sub} 不在允许列表里（只允许只读与本地提交）："
                            f"{', '.join(sorted(ALLOWED_GIT_SUBCOMMANDS))}"
                        )

    def append_file(self, rel: str, content: str) -> str:
        """向文件末尾追加内容（不存在则创建）。

        为什么 agent 需要这个：写一个大文件时，工具参数（含整个文件内容）
        会撞上 max_tokens 上限被截断，导致 JSON 解析失败。
        **分块写**是唯一可靠的解法 —— 先写骨架，再逐块追加。
        实测模型遇到截断会自己想到"分块写"，但必须有这个工具它才能做到。
        """
        target = self._write_target(rel)
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(content)
        lines = content.count("\n") + (0 if content.endswith("\n") else 1)
        with self._lock:
            self.bytes_written += len(content.encode("utf-8"))
            self.changes.append(FileChange(self.rel(target), "modify" if existed else "create",
                                           added=lines))
        self._audit("append_file", rel, True, f"+{lines} 行")
        total = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
        return (f"已追加到 {self.rel(target)}（+{lines} 行，现在共 {total} 行）")

    def execution_command(self, cmd: str):
        """local 为受信宿主模式；docker 为真正隔离模式，失败不回退宿主。"""
        if self.execution_mode == 'native':
            if os.name != 'nt':
                raise WorkspaceError('原生 Windows 沙箱不可用；不会回退本机')
            return cmd, False
        if self.execution_mode == 'local':
            return cmd, True
        if self.execution_mode != 'docker':
            raise WorkspaceError('命令执行已禁用，配置 local 或 docker 执行模式')
        docker = shutil.which('docker')
        if not docker:
            raise WorkspaceError('Docker 隔离执行器不可用；不会回退到宿主执行')
        image = os.environ.get('AGENTLAB_SANDBOX_IMAGE', 'python:3.11-slim')
        check = subprocess.run([docker, 'image', 'inspect', image],
                               capture_output=True, timeout=10)
        if check.returncode:
            raise WorkspaceError(f'沙箱镜像尚未准备：{image}，请管理员预先拉取')
        return [docker, 'run', '--name', 'agentlab-' + uuid.uuid4().hex,
                '--rm', '--pull=never', '--network=none',
                '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--pids-limit=128', '--memory=512m', '--cpus=1',
                '--tmpfs=/tmp:rw,noexec,nosuid,size=64m',
                '--mount', f'type=bind,source={self.root},target=/workspace',
                '--workdir=/workspace', image, 'sh', '-c', cmd], False

    def execution_cleanup(self, command):
        if self.execution_mode == 'docker' and isinstance(command, list):
            return [command[0], 'rm', '--force', command[command.index('--name') + 1]]
        return None

    def run(self, cmd: str, timeout_s: float = 30.0, cwd: str | None = None) -> str:
        """在工作区里执行命令，返回合并后的输出。

        真实终端是 agent 最重要的工具：**能跑测试才算真的会写代码**。
        """
        if not self.allow_shell:
            raise WorkspaceError("本工作区已禁用命令执行")
        self.check_command(cmd)
        workdir = self.resolve(cwd) if cwd else self.root
        selected_root = next(r for r in self.roots.values() if workdir.is_relative_to(r))
        if selected_root != self.root:
            # 每条命令只授予所选文件夹；不把两目录的共同祖先授权给沙箱。
            selected = Workspace(selected_root, allow_shell=self.allow_shell)
            selected.execution_mode, selected.native_network = self.execution_mode, self.native_network
            selected.trusted_host_commands = getattr(self, 'trusted_host_commands', False)
            selected.cancel_event, selected.processes = self.cancel_event, self.processes
            result = selected.run(cmd, timeout_s, str(workdir))
            self.last_execution = selected.last_execution
            self.commands_run += selected.commands_run
            self.audit.extend(selected.audit)
            return result
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        t0 = time.perf_counter()
        command, use_shell = self.execution_command(cmd)
        task_id = self.processes.start(command, workdir, timeout_s=max(1, timeout_s),
                                       shell=use_shell, env=env,
                                       cleanup_command=self.execution_cleanup(command),
                                       native_workspace=self.root if self.execution_mode == 'native' else None,
                                       native_network=self.native_network)
        try:
            while True:
                state = self.processes.wait(task_id, .1)
                if self.cancel_event.is_set():
                    state = self.processes.cancel(task_id)
                if state['status'] != 'running':
                    break
            self.last_execution = dict(state, command=cmd)
            out = state['output']
            code = state['exit_code']
        finally:
            self.processes.release(task_id)
        if state['status'] != 'exited':
            self._audit("run", cmd, False, state['status'])
            return (f"命令超时或中止（>{timeout_s:.0f}s，{state['status']}）：{cmd}\n"
                    f"部分输出：\n{out[-1500:]}")
        elapsed = (time.perf_counter() - t0) * 1000.0
        with self._lock:
            self.commands_run += 1
        self._audit("run", cmd, code == 0, f"exit={code} {elapsed:.0f}ms")
        body = out.strip()
        if len(body) > 6000:
            body = body[:3000] + f"\n…（输出 {len(out)} 字符，中间省略）…\n" + body[-2500:]
        return (f"$ {cmd}\n(工作目录 {self.rel(workdir)}，退出码 {code}，{elapsed:.0f}ms)\n"
                f"{body or '(无输出)'}")

    # -- 观测 --------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "root": str(self.root),
                "commands_run": self.commands_run,
                "bytes_written": self.bytes_written,
                "files_changed": len({c.path for c in self.changes}),
                "changes": [
                    {"path": c.path, "action": c.action,
                     "added": c.added, "removed": c.removed,
                     "ts": time.strftime("%H:%M:%S", time.localtime(c.ts))}
                    for c in self.changes[-40:]
                ],
                "audit": [
                    {"ts": time.strftime("%H:%M:%S", time.localtime(a.ts)),
                     "action": a.action, "target": a.target[:80],
                     "ok": a.ok, "detail": a.detail[:60]}
                    for a in self.audit[-40:]
                ],
            }

    def diff_summary(self) -> str:
        """本次会话改过哪些文件 —— 给人看的收尾摘要。"""
        with self._lock:
            if not self.changes:
                return "（本次没有修改任何文件）"
            by_file: dict[str, FileChange] = {}
            for c in self.changes:
                prev = by_file.get(c.path)
                if prev is None:
                    by_file[c.path] = c
                else:
                    prev.added += c.added
                    prev.removed += c.removed
                    if c.action == "create":
                        prev.action = "create"
            lines = [f"  {c.action:<7} {c.path}" for c in by_file.values()]
            return "\n".join(lines)

    def reset(self) -> str:
        """清空工作区（谨慎使用，仅限工作区内）。"""
        with self._lock:
            for child in self.root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            self.changes.clear()
            self.audit.clear()
        return f"已清空工作区 {self.root}"
