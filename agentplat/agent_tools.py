"""编码 agent 的工具集：文件读写、搜索、命令执行、任务收尾。

两个设计要点
------------
1. **Schema 就是给模型的说明书**。工具的 `description` 写得好不好，直接决定
   模型会不会用对 —— 这是 lab-15 实测过的结论（描述质量决定选择正确率）。
   所以每个工具都写清"什么时候用/什么时候不要用/参数什么含义"。

2. **所有文件与命令操作都经过 `Workspace`**，工具本身不做路径拼接。
   安全边界只有一处，就不会有漏网的路径。

工具格式是 **OpenAI function calling** 的 JSON Schema，这样真实模型能原生调用，
不需要靠 prompt 里写"请输出 JSON 表示你要调什么"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .workspace import Workspace, WorkspaceError


@dataclass
class AgentTool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., str]
    destructive: bool = False   # 会改文件/跑命令
    terminal: bool = False      # 调用后结束任务（finish）
    network: bool = False

    def schema(self) -> dict:
        """转成 OpenAI tools 数组里的一项。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _obj(props: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def build_agent_tools(ws: Workspace) -> dict[str, AgentTool]:
    """构造工具表。全部绑定到同一个 workspace 实例。"""

    def list_dir(path: str = ".", pattern: str = "*") -> str:
        return ws.list_dir(path, pattern)

    def read_file(path: str, start_line: int = 1, max_lines: int = 400) -> str:
        return ws.read_file(path, start_line, max_lines)

    def grep(pattern: str, path: str = ".", glob: str = "*") -> str:
        return ws.grep(pattern, path, glob)

    def write_file(path: str, content: str) -> str:
        return ws.write_file(path, content)

    def append_file(path: str, content: str) -> str:
        return ws.append_file(path, content)

    def edit_file(path: str, old_text: str, new_text: str,
                  replace_all: bool = False) -> str:
        return ws.edit_file(path, old_text, new_text, replace_all)

    def delete_file(path: str) -> str:
        return ws.delete_file(path)

    def run_shell(command: str, timeout_s: float = 30.0, cwd: str = '.') -> str:
        return ws.run(command, timeout_s, cwd)

    def finish(summary: str, files_changed: str = "") -> str:
        """终止任务。真正的"完成"由循环在收到这个调用时判定。"""
        return f"完成申请已提交，是否完成以宿主验收结果为准。\n总结：{summary}\n改动文件：{files_changed or '（见工作区 diff）'}"

    tools = [
        AgentTool('read_chunk', '按字节分页回取大文件或 spill；next_offset 用于下一页。',
                  _obj({'path': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
                        'max_bytes': {'type': 'integer', 'minimum': 1, 'maximum': 200000}}, ['path']),
                  lambda path, offset=0, max_bytes=8000: ws.read_chunk(path, offset, max_bytes)),
        AgentTool(
            name="list_dir",
            description=(
                "列出工作区某个目录下的文件和子目录（含大小标记）。"
                "**开始任何任务前先用它了解项目结构**，不要凭空猜文件名。"
            ),
            parameters=_obj({
                "path": {"type": "string", "description": "相对工作区的目录路径，默认 '.'"},
                "pattern": {"type": "string", "description": "文件名通配，如 '*.py'"},
            }, []),
            fn=list_dir,
        ),
        AgentTool(
            name="read_file",
            description=(
                "读取工作区内一个文本文件（带行号，便于引用）。"
                "大文件用 start_line/max_lines 分段读。"
                "**改文件之前必须先读**，否则 edit_file 会因匹配不到而失败。"
            ),
            parameters=_obj({
                "path": {"type": "string", "description": "相对工作区的文件路径"},
                "start_line": {"type": "integer", "minimum": 1, "description": "起始行，默认 1"},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000,
                              "description": "最多读多少行，默认 400"},
            }, ["path"]),
            fn=read_file,
        ),
        AgentTool(
            name="grep",
            description=(
                "用正则在工作区里搜索内容，返回 `文件:行号: 内容`。"
                "**定位代码位置的首选工具** —— 比逐个读文件快得多。"
            ),
            parameters=_obj({
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "搜索范围目录，默认 '.'"},
                "glob": {"type": "string", "description": "只搜匹配的文件名，如 '*.py'"},
            }, ["pattern"]),
            fn=grep,
        ),
        AgentTool(
            name="write_file",
            description=(
                "写入（覆盖）一个文件，不存在则创建。"
                "**只在新建文件或整体重写时用**；改动已有文件请用 edit_file，"
                "否则会丢掉你没读到的部分。"
            ),
            parameters=_obj({
                "path": {"type": "string", "description": "相对工作区的文件路径"},
                "content": {"type": "string", "description": "完整文件内容"},
            }, ["path", "content"]),
            fn=write_file,
            destructive=True,
        ),
        AgentTool(
            name="append_file",
            description=(
                "向文件末尾追加内容（不存在则创建）。"
                "**写长文件时必须用它分块写** —— 一次性 write_file 传整个大文件会"
                "撞上输出长度上限被截断，导致参数不完整而失败。"
                "推荐做法：write_file 写第一块，然后 append_file 逐块补齐。"
            ),
            parameters=_obj({
                "path": {"type": "string", "description": "相对工作区的文件路径"},
                "content": {"type": "string", "description": "要追加的内容（建议每块 < 200 行）"},
            }, ["path", "content"]),
            fn=append_file,
            destructive=True,
        ),
        AgentTool(
            name="edit_file",
            description=(
                "在已有文件里做**精确字符串替换**（改代码的首选方式）。"
                "old_text 必须与文件内容**完全一致**（含缩进和换行）。"
                "若同一段文本出现多次，要给出更长的上下文让它唯一，"
                "或设 replace_all=true。"
            ),
            parameters=_obj({
                "path": {"type": "string", "description": "相对工作区的文件路径"},
                "old_text": {"type": "string", "description": "要被替换的原文（需完全匹配）"},
                "new_text": {"type": "string", "description": "替换成的新文本"},
                "replace_all": {"type": "boolean", "description": "是否替换全部出现，默认 false"},
            }, ["path", "old_text", "new_text"]),
            fn=edit_file,
            destructive=True,
        ),
        AgentTool(
            name="delete_file",
            description="删除工作区里的单个文件（不能删目录）。不确定时不要用。",
            parameters=_obj({
                "path": {"type": "string", "description": "要删除的文件路径"},
            }, ["path"]),
            fn=delete_file,
            destructive=True,
        ),
        AgentTool(
            name="run_shell",
            description=(
                "在工作区目录下执行命令（有白名单与安全检查，超时会强制终止）。"
                "**这是验证代码是否真的能跑的唯一手段**：写完代码要跑测试/运行脚本，"
                "不要只凭肉眼判断正确性。"
            ),
            parameters=_obj({
                "command": {"type": "string", "description": "要执行的命令"},
                "cwd": {"type": "string", "description": "工作目录；多文件夹使用 @别名/相对路径。单条沙箱命令只授权该文件夹。默认主目录。"},
                "timeout_s": {"type": "number", "minimum": 1, "maximum": 300,
                              "description": "超时秒数，默认 30"},
            }, ["command"]),
            fn=run_shell,
            destructive=True,
        ),
        AgentTool(
            name="finish",
            description=(
                "任务完成时调用，给出总结。**不要用自然语言说「完成了」来结束**，"
                "必须调用这个工具，否则循环会继续。"
            ),
            parameters=_obj({
                "summary": {"type": "string", "description": "做了什么、结果如何"},
                "files_changed": {"type": "string", "description": "改了哪些文件"},
            }, ["summary"]),
            fn=finish,
            terminal=True,
        ),
    ]
    return {t.name: t for t in tools}


def schemas(tools: dict[str, AgentTool]) -> list[dict]:
    return [t.schema() for t in tools.values()]
