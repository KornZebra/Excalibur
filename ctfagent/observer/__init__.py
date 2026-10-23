"""v2 旁路纠偏（Observer）：只提醒、不决策、不改任务。

* :mod:`ctfagent.observer.detector` —— 三条检测规则（D1 原地打转 / D2 无进展 / D3 重复踩坑）+ 限频
* :mod:`ctfagent.observer.judge` —— 可选小模型判官（成本换精度，默认关闭）

提醒的落地动作在 :class:`~ctfagent.orchestrator.solver_pool.SolverRun`：
SSE 发 ``efficiency_reminder``，并把文本注入该 Solver 下一轮的「[Observer 提醒]」段。
"""

from __future__ import annotations

from .detector import (
    D1_REPEAT_TOOL,
    D2_NO_PROGRESS,
    D3_REPEAT_FAILURE,
    Observation,
    ObserverDetector,
    fingerprint,
    normalize_arguments,
    normalize_text,
    similarity,
)
from .judge import ObserverJudge

__all__ = [
    "D1_REPEAT_TOOL",
    "D2_NO_PROGRESS",
    "D3_REPEAT_FAILURE",
    "Observation",
    "ObserverDetector",
    "ObserverJudge",
    "fingerprint",
    "normalize_arguments",
    "normalize_text",
    "similarity",
]
