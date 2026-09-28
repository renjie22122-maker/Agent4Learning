"""agentlab — 生产级 Agent 工程学习项目用的零依赖核心 harness。

设计原则
--------
1. 零第三方依赖：只用 Python 3.11+ 标准库，``git clone`` 后立刻能跑。
2. 每个 lab 都是**可执行**的：先复现故障，再给观测手段，再修复，再验证。
3. 所有外部依赖（LLM、向量库、队列）都是**可注入的模拟实现**，可以精确控制
   延迟分布、错误率、并发上限、成本、内存占用——这是能在本地讲清"生产问题"
   的前提。
4. 核心契约（本模块 + ``providers`` + ``store`` + ``orchestration``）冻结，
   lab 只允许依赖这层公开 API。
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
