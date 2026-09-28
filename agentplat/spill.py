"""工具结果 spill 策略：超大结果不进上下文，只留预览 + 可回取的落盘 locator。

为什么必须有这一层
------------------
编码 agent 的上下文是被**工具结果**撑爆的，不是被用户输入撑爆的：

* `run_shell` 跑一次 pytest，输出可能是几万字符的失败堆栈；
* `read_file` 读一个 400 行的文件就是上万字符；
* `grep` 在大仓库里一次几百行命中。

本项目实测：一次"写 quicksort + 跑测试"的任务，agent 循环里 31 轮共消耗
**输入 token 188,425** —— 绝大部分是反复回灌的工具输出。而模型每一步都要
把这些**重发一遍**，所以成本是随轮数**平方级**增长的。

对策（DSH `dsh-spill-policy` 的做法，`maxInlineBytes` 默认 50000）：
超过阈值的纯文本结果 → 写进工作区文件，上下文里只放
**首尾预览 + 一个 locator**，模型需要细节时自己 `read_file` 去取。

这样做的两个收益：
1. **上下文有界**：单条工具结果对 prompt 的贡献被封顶，不再随输出大小失控；
2. **信息不丢**：全文落盘可回取，模型不是"看不到"，而是"按需再看"。

注意这不是"截断"。截断会真的丢信息，spill 只是把它挪出热路径 ——
这是两者最重要的区别，也是这个策略敢用在生产的原因。
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

#: 超过这个字节数的工具结果就走 spill。
#: 取值参考 DSH 的 `maxInlineBytes: 50000`（UTF-8 字节）。
#: 但本项目把默认值调小到 8000 —— 理由：真实 key 下 token 是花钱的，
#: 而且实测单条结果超过 ~2k token 后对模型决策的边际价值迅速下降。
DEFAULT_MAX_INLINE_BYTES = 8_000

#: 预览保留的首尾字节数（各留一半）。
DEFAULT_PREVIEW_BYTES = 2_400

#: spill 文件的存放子目录（在工作区内，模型可以自己 read_file 取回）。
SPILL_DIRNAME = ".spill"


@dataclass
class SpillRecord:
    """一次 spill 的账。用于统计"省了多少上下文"。"""

    tool: str
    original_bytes: int
    inline_bytes: int
    path: str
    ts: float = field(default_factory=time.time)

    @property
    def saved_bytes(self) -> int:
        return max(0, self.original_bytes - self.inline_bytes)


@dataclass
class SpillPolicy:
    """把超大工具结果挪出上下文，只留预览 + locator。"""

    workspace: Path
    max_inline_bytes: int = DEFAULT_MAX_INLINE_BYTES
    preview_bytes: int = DEFAULT_PREVIEW_BYTES
    enabled: bool = True

    spilled: list[SpillRecord] = field(default_factory=list)
    total_original: int = 0
    total_inline: int = 0
    reuses: int = 0

    def __post_init__(self) -> None:
        self.dir = Path(self.workspace) / SPILL_DIRNAME

    # ------------------------------------------------------------------
    @property
    def saved_bytes(self) -> int:
        return max(0, self.total_original - self.total_inline)

    @property
    def saved_ratio(self) -> float:
        return self.saved_bytes / self.total_original if self.total_original else 0.0

    # ------------------------------------------------------------------
    def apply(self, tool: str, result: str) -> str:
        """对一次工具结果应用策略：够小就原样返回，够大就 spill。"""
        raw = result or ""
        n = len(raw.encode("utf-8"))
        self.total_original += n
        if not self.enabled or n <= self.max_inline_bytes:
            self.total_inline += n
            return result

        # 落盘：用工具名 + 内容哈希做文件名，**同内容重复 spill 会复用**，
        # 避免 agent 反复读同一个大文件时把工作区塞满。
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
        name = f"{tool}-{digest}.txt"
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.dir / name
        reused = target.exists()
        if not reused:
            target.write_bytes(raw.encode("utf-8"))

        head_n = self.preview_bytes // 2
        tail_n = self.preview_bytes - head_n
        head = raw.encode("utf-8")[:head_n].decode("utf-8", "ignore")
        tail = raw.encode("utf-8")[-tail_n:].decode("utf-8", "ignore") if tail_n and n > head_n + tail_n else ""
        omitted = len(raw) - len(head) - len(tail)
        rel = f"{SPILL_DIRNAME}/{name}"

        inline = (
            f"{head}\n"
            f"\n…〔此处省略 {omitted:,} 字符 —— 完整结果已落盘，"
            f"不要凭预览猜内容〕…\n\n"
            f"{tail}\n"
            f"\n[spill] 完整输出共 {n:,} 字节，已保存到工作区文件 `{rel}`。"
            f"需要其中任何细节时用 read_file(path=\"{rel}\", start_line=N) 分段读取；超长单行可用 read_chunk(path, offset, max_bytes)。"
        )
        inline_bytes = len(inline.encode("utf-8"))
        self.total_inline += inline_bytes
        # 同内容重复 spill 只记一条（文件已复用）——否则统计会把同一份输出
        # 重复计入"触发次数"，看起来像省了更多，其实是虚高。
        if not any(r.path == rel for r in self.spilled):
            self.spilled.append(SpillRecord(tool, n, inline_bytes, rel))
        self.reuses += 1 if reused else 0
        return inline

    # ------------------------------------------------------------------
    def summary(self) -> str:
        if not self.spilled:
            return "（本次没有触发 spill）"
        return (f"{len(self.spilled)} 次 spill，"
                f"上下文少装 {self.saved_bytes:,} 字节（-{self.saved_ratio:.1%}）")

    def render(self) -> None:
        from agentlab.util import kv, note, phase

        phase("工具结果 spill 策略", f"(阈值 {self.max_inline_bytes:,} 字节)")
        kv("工具结果总量", f"{self.total_original:,} 字节")
        kv("进入上下文", f"{self.total_inline:,} 字节")
        kv("省下", f"{self.saved_bytes:,} 字节（{self.saved_ratio:.1%}）")
        kv("触发次数", f"{len(self.spilled)}" + (f"（复用落盘文件 {self.reuses} 次）" if self.reuses else ""))
        if self.spilled:
            note("落盘文件（模型可按需回取）：")
            for rec in self.spilled[-8:]:
                note(f"    {rec.path}  ({rec.original_bytes:,}B → {rec.inline_bytes:,}B)")
