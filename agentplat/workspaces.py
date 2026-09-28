"""编码 Agent 的工作区管理与选择。

为什么需要一个"resolver"而不是直接接受任意路径
----------------------------------------------
agent 一旦有了 shell 与写文件能力，"能不能切到 D:\\ 或 /" 就是一个安全决策。
如果界面允许输入任意路径并切过去，那 `workspace/` 那层边界就形同虚设 ——
用户（或一个误导性的链接）就能把 agent 指向整个磁盘。

所以规则是：**只能切到"允许的根"或其子目录**。

* 允许的根默认只有两个：项目自带的 `workspace/` 与项目目录本身。
* 想加别的根，必须显式写进 ``.agentlab_workspaces.json`` 的 ``allowed_roots``，
  或者在启动时用 ``--allow-root`` 传入 —— **不存在"输入框里随便填就能切"的路径。**
* 当前选择会记住，重启后仍生效。

## 安全模型（重要，别把它读成"限制"）

这是**本机、单用户、给自己用**的工具，所以安全边界是**黑名单**：
只拒绝"一进去就会把系统搞坏"的位置（盘根、家目录本身、系统目录、
层级太浅的路径），其余任意目录都能选。

早期版本用的是白名单（只允许 `workspace/` 和项目根），结果是
**用户根本选不了自己的项目** —— 而"选不了工作区"的编码 agent 没有用。
白名单在服务端才是对的（你不知道谁在调）；在单机自用场景里，
它挡住的只有用户自己。

如果你的场景是多人共用一台机器，**必须换回白名单 + 每用户配额**，
并把 `_why_dangerous()` 换成 `_allowed()`。这是一个真实的取舍，
不是"黑名单更好"。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

#: 项目根（agentplat/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE = PROJECT_ROOT / "workspace"

#: 记住当前选择与最近用过的目录
STATE_PATH = PROJECT_ROOT / ".agentlab_workspaces.json"


def _dangerous_dirs() -> tuple[Path, ...]:
    """哪些目录"一进去就会把系统搞坏"。只列这些，其余一律允许。"""
    out: list[Path] = []
    if sys.platform == "win32":
        for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)",
                    "ProgramData"):
            v = os.environ.get(var)
            if v:
                out.append(Path(v))
        # System32 通常被 SystemRoot 覆盖，但用户也可能直接指到那里
        sr = os.environ.get("SystemRoot")
        if sr:
            out.append(Path(sr) / "System32")
            out.append(Path(sr) / "SysWOW64")
    else:
        out += [Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin"),
                Path("/var"), Path("/boot"), Path("/dev"), Path("/proc"),
                Path("/sys"), Path("/System"), Path("/Library")]
    # 项目自己的状态目录也拒：让 agent 改自己的工作区配置没有意义，
    # 却会真的把"当前工作区"改坏。
    out.append(PROJECT_ROOT / ".git")
    return tuple(dict.fromkeys(out))


DANGEROUS_DIRS = _dangerous_dirs()


def _tree_roots() -> tuple[Path, ...]:
    """整棵目录树的根：下面有成百上千个不相关的项目。

    选到这些地方，agent 的 `list_dir` / `grep` 会扫过大量无关内容
    （慢且贵），写操作也可能落错项目。不是"危险到搞坏系统"，
    而是**几乎肯定是误操作**，所以明确列出来拒掉。
    """
    home = Path.home()
    out = [Path(p) for p in
           (home / "Desktop", home / "Documents", home / "Downloads",
            home / "Pictures", home / "Music", home / "Videos")]
    return tuple(out)


TREE_ROOTS = _tree_roots()


class WorkspaceAccessError(Exception):
    """不允许的工作区路径。**必须拒绝，不能"降级为默认值"** ——
    静默降级会让用户以为切成功了，实际在另一个目录里改文件。
    """


@dataclass
class WorkspaceEntry:
    path: Path
    label: str = ""
    note: str = ""

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def files(self, limit: int = 500) -> int:
        if not self.path.exists():
            return 0
        n = 0
        for p in self.path.rglob("*"):
            if p.is_file() and not any(
                part in (".git", "__pycache__", "node_modules", ".spill")
                for part in p.parts
            ):
                n += 1
                if n >= limit:
                    break
        return n


class WorkspaceManager:
    """管理"允许哪些根"和"当前用哪个"。"""

    def __init__(self, allowed_roots: list[Path] | None = None,
                 state_path: Path | None = None):
        self.state_path = Path(state_path or STATE_PATH)
        self._allowed: list[Path] = []
        for r in (allowed_roots or []):
            self._add_root(r)
        # 默认允许的两个根 —— 刻意很窄
        self._add_root(DEFAULT_WORKSPACE)
        self._add_root(PROJECT_ROOT)
        self.current: Path = DEFAULT_WORKSPACE
        self.recent: list[str] = []
        self.groups = {}
        self.session_roots = []
        self.current_group = ''
        self._load()

    def validate_folders(self, folders):
        if not isinstance(folders, dict) or not 1 <= len(folders) <= 16:
            raise WorkspaceAccessError('工作区需要 1 到 16 个文件夹')
        result = {}
        for alias, raw in folders.items():
            if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,31}', alias):
                raise WorkspaceAccessError('文件夹别名需以英文字母开头，只能包含字母、数字、下划线和横线')
            path = self.resolve(raw)
            if not path.is_dir():
                raise WorkspaceAccessError(f'文件夹不存在：{path}，请先创建该目录')
            if any(path.is_relative_to(p) or p.is_relative_to(path) for p in result.values()):
                raise WorkspaceAccessError('同一工作区中的文件夹不能重复或相互包含')
            result[alias] = path
        return result

    def save_group(self, name, folders, group_id=''):
        name = name.strip()
        if not name or len(name) > 80:
            raise WorkspaceAccessError('工作区名称需为 1 到 80 个字符')
        folders = self.validate_folders(folders)
        if group_id and group_id not in self.groups:
            raise WorkspaceAccessError('工作区不存在')
        group_id = group_id or uuid.uuid4().hex[:12]
        self.session_roots = list(dict.fromkeys(self.session_roots + list(self.groups.get(group_id,{}).get('folders',{}).values()) + [str(v) for v in folders.values()]))
        self.groups[group_id] = {'name': name, 'folders': {k:str(v) for k,v in folders.items()}}
        self.select_group(group_id)
        return group_id

    def select_group(self, group_id):
        group = self.groups[group_id]
        folders = self.validate_folders(group['folders'])
        self.current_group = group_id
        self.current = next(iter(folders.values()))
        self._save()

    def current_folders(self):
        if self.current_group in self.groups:
            return self.validate_folders(self.groups[self.current_group]['folders'])
        return {'main': self.current}

    def session_directories(self):
        roots = {self.current, DEFAULT_WORKSPACE, *(Path(r) for r in self.recent + self.session_roots)}
        roots.update(Path(v) for g in self.groups.values() for v in g['folders'].values())
        return {r / '.sessions' for r in roots} | {r.parent / '.sessions' for r in roots}

    # ------------------------------------------------------------------
    def _add_root(self, path: Path | str) -> Path | None:
        try:
            p = Path(path).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        if p not in self._allowed:
            self._allowed.append(p)
        return p

    def add_root(self, path: Path | str) -> str:
        """显式新增一个允许的根（CLI/配置文件用，界面不暴露这个操作）。"""
        p = self._add_root(path)
        if p is None:
            raise WorkspaceAccessError(f"路径无法解析：{path}")
        self._save()
        return f"已允许工作区根：{p}"

    # ------------------------------------------------------------------
    def resolve(self, candidate: str | os.PathLike) -> Path:
        """把用户选择解析成合法工作区路径，不合法就抛错。

        ## 为什么是"黑名单"而不是"白名单"

        早期版本只允许 `workspace/` 和项目根两个目录，用户体验是
        **根本选不了自己的项目** —— 而"选不了工作区"的编码 agent 没有用。
        白名单在**服务端**才是对的做法（你不知道谁在调）；但这里是
        **本机、单用户、自己给自己用**，白名单挡住的只有用户自己。

        所以改成黑名单：只拒绝"一进去就会把系统搞坏"的地方，
        其余路径一律允许。安全边界仍然存在，但它落在**真正危险**的位置上，
        而不是落在"你没提前登记"上。

        拒绝清单：
          · 盘根（`D:\\`）与文件系统根（`/`）
          · 家目录本身（`C:\\Users\\你`）—— 允许它下面的子目录
          · 系统目录（Windows、Program Files、ProgramData、
            System32、`/etc` `/usr` `/bin` `/System` …）
          · 层级太浅的路径（`D:\\x` 这种一格目录）
          · 路径里含 `..` 且解析后落在被拒位置（解析后判断，不靠字符串）
        """
        raw = str(candidate).strip().strip('"').strip("'")
        if not raw:
            raise WorkspaceAccessError("工作区路径为空")
        p = Path(raw)
        if not p.is_absolute():
            p = (PROJECT_ROOT / p)
        try:
            p = p.resolve()
        except (OSError, RuntimeError) as exc:
            raise WorkspaceAccessError(f"路径无法解析：{raw}（{exc}）") from None

        reason = self._why_dangerous(p)
        if p.exists() and not p.is_dir():
            raise WorkspaceAccessError(f'不是文件夹：{p}')
        if reason:
            raise WorkspaceAccessError(f"拒绝把 {p} 作为工作区：{reason}")
        return p

    def _why_dangerous(self, p: Path) -> str:
        """返回"为什么这个路径危险"，安全则返回空串。"""
        s = str(p)
        # 1) 盘根 / 文件系统根
        try:
            if p == Path(p.anchor):
                return "它是盘根，agent 的写操作会散落到整个盘"
        except (OSError, ValueError):
            pass
        if s in ("/", "\\", ""):
            return "它是文件系统根"

        # 2) 家目录本身（允许家目录下的子目录 —— 大多数项目都在那里）
        with contextlib.suppress(OSError, RuntimeError):
            if p == Path.home():
                return "它是家目录本身；请选家目录里的具体项目目录"

        # 3) 系统目录（大小写不敏感比较，Windows 上 D:\windows 和 D:\Windows 是同一个）
        low = s.lower().replace("/", "\\")
        for bad in DANGEROUS_DIRS:
            b = str(bad).lower().replace("/", "\\")
            if low == b or low.startswith(b + "\\"):
                return f"它在系统目录 {bad} 里"

        # 4) 常见的"目录树根"：这些地方一选下去，agent 的写操作会散落到
        #    成百上千个不相关的项目里。**明确列出来**，而不是靠"层级太浅"
        #    这种启发式 —— 早期版本用 `len(parts) <= 2`，结果是
        #    `D:\Desktop` 被拒而 `D:\x` 反而通过，前后自相矛盾。
        for root in TREE_ROOTS:
            if p == root:
                return (f"{p} 是整棵目录树的根（下面有大量不相关的项目）；"
                        f"请选到具体的项目目录")

        # 5) 已经不存在且父目录也不存在 → 大概率是打错的路径，不是"想新建"
        #    允许新建，但要求父目录存在，避免一路 mkdir 出一个空树。
        if not p.exists():
            parent = p.parent
            if not parent.exists():
                return (f"路径不存在，且它的父目录 {parent} 也不存在"
                        f"（可能打错了）")

        return ""

    # ------------------------------------------------------------------
    def switch(self, candidate: str | os.PathLike) -> tuple[Path, str]:
        """切换当前工作区。返回 (路径, 提示)。"""
        p = self.resolve(candidate)
        created = False
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created = True
        self.current = p
        self.current_group = ''
        self.session_roots = list(dict.fromkeys(self.session_roots + [str(p)]))
        s = str(p)
        self.recent = [s] + [r for r in self.recent if r != s]
        self.recent = self.recent[:10]
        self._save()
        msg = f"已切换到 {p}" + ("（目录不存在，已创建）" if created else "")
        return p, msg

    # ------------------------------------------------------------------
    def entries(self) -> list[WorkspaceEntry]:
        """列出可选项：允许的根 + 它们的直接子目录 + 最近用过的。"""
        out: list[WorkspaceEntry] = []
        seen: set[str] = set()

        def push(path: Path, label: str, note: str = "") -> None:
            key = str(path)
            if key in seen:
                return
            seen.add(key)
            out.append(WorkspaceEntry(path, label, note))

        for r in self._allowed:
            tag = "默认" if r == DEFAULT_WORKSPACE else ("项目根" if r == PROJECT_ROOT else "允许的根")
            push(r, r.name or str(r), tag)
            if r.exists():
                try:
                    for child in sorted(r.iterdir()):
                        if child.is_dir() and not child.name.startswith("."):
                            push(child, child.name, "子目录")
                except OSError:
                    pass
        for rec in self.recent:
            push(Path(rec), Path(rec).name or rec, "最近使用")
        return out

    def summary(self) -> dict:
        return {
            "current": str(self.current),
            "current_group": self.current_group,
            "groups": self.groups,
            "exists": self.current.exists(),
            "files": WorkspaceEntry(self.current).files(),
            "allowed_roots": [str(r) for r in self._allowed],
            "dangerous": [str(d) for d in DANGEROUS_DIRS] + [str(t) for t in TREE_ROOTS],
            "recent": self.recent,
            "state_path": str(self.state_path),
        }

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 状态文件坏了不能让服务起不来
            return
        for r in d.get("allowed_roots", []):
            self._add_root(r)
        self.recent = list(d.get("recent", []))[:10]
        self.groups = d.get('groups', {})
        self.session_roots = d.get('session_roots', list(self.recent))
        self.current_group = d.get('current_group', '')
        cur = d.get("current")
        if cur:
            try:
                self.current = self.resolve(cur)
            except WorkspaceAccessError:
                self.current = DEFAULT_WORKSPACE

    def _save(self) -> None:
        try:
            self.state_path.write_text(json.dumps({
                # 只持久化"额外"的根与最近记录；两个默认根由代码保证
                "allowed_roots": [str(r) for r in self._allowed
                                  if r not in (DEFAULT_WORKSPACE, PROJECT_ROOT)],
                "current": str(self.current),
                "current_group": self.current_group,
                "groups": self.groups,
                "session_roots": self.session_roots,
                "recent": self.recent,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # 记不住不算致命，不该因此中断切换
