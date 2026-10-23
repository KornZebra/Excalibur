"""v2 控制内核：外置 Dispatcher + 终止评估器 + Solver 生命周期。

模块构成（对应 v2 升级提示词 §1.3 / §5）::

    policy.py     调度决策规则（纯函数，零 LLM，可单测）
    termination.py 外置结束条件评估器（三路 verdict）
    solver_pool.py Solver 生命周期包装 + 双阶段 conclude（失败即产出）
    dispatcher.py 规则驱动的 asyncio 控制回路（唯一控制面）
    session.py    挑战会话运行态（SSE 总线 + 控制回路任务 + 快照）

与 :mod:`ctfagent.agents.orchestrator` 的分工：后者是 v1 的 **roles 模式**派单器
（由主代理 LLM 通过 ``task`` 工具驱动，用于交互式对话）；本包是 v2 **blackboard 模式**
的控制面（无编排者 LLM，调度由确定性代码负责，模型只做 Worker）。
"""

from __future__ import annotations

__all__ = ["dispatcher", "policy", "session", "solver_pool", "termination"]
