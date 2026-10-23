"""Observer 检测器：**旁路纠偏，只提醒不决策**（升级提示词 §6.1）。

三条规则（滑动窗口，每 Solver 独立）::

    D1 同一 (tool, 归一化参数) 连续 ≥ repeat_tool_threshold 次      → 原地打转
    D2 运行 no_progress_minutes 分钟无任何 memory_write/idea_transition → 无进展漂移
    D3 与已有 failure Memory 文本相似度 ≥ failure_similarity_threshold → 重复踩坑

本模块是**纯函数 + 内存窗口**：不碰 IO、不碰时钟（``now`` 可注入）、不发事件。
触发后的动作（SSE ``efficiency_reminder`` + 注入下一轮渲染块）由
:class:`~ctfagent.orchestrator.solver_pool.SolverRun` 完成——Observer 永远不改变任务、
不停止 Solver、不写账本，只把一句话塞进「[Observer 提醒]」段。

限频：``cooldown_per_solver_minutes`` 定义冷却窗长度，``max_reminders_per_solver``
定义该窗口内最多提醒次数（默认 30 分钟内最多 2 次）；超限的 Observation 直接丢弃。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from ..config import ObserverConfig

__all__ = [
    "D1_REPEAT_TOOL",
    "D2_NO_PROGRESS",
    "D3_REPEAT_FAILURE",
    "Observation",
    "ObserverDetector",
    "fingerprint",
    "normalize_arguments",
    "normalize_text",
    "similarity",
]

D1_REPEAT_TOOL = "D1"
D2_NO_PROGRESS = "D2"
D3_REPEAT_FAILURE = "D3"

#: 归一化参数时忽略的易变字段（每次都不同，不应算作"同一条命令"）
VOLATILE_KEYS = frozenset({"timestamp", "ts", "nonce", "request_id", "requestId", "call_id", "id"})

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff{}:=/.\-]+")
_HEX = re.compile(r"\b[0-9a-fA-F]{16,}\b")


def normalize_text(text: str) -> str:
    """归一化文本：小写、压空白、去标点、抹掉长十六进制串。"""
    clean = _HEX.sub(" hex ", str(text or "").lower())
    clean = _WS.sub(" ", clean)
    clean = _PUNCT.sub(" ", clean)
    return clean.strip()


def normalize_arguments(arguments: dict[str, Any] | None) -> str:
    """归一化工具参数：排序键、丢弃易变字段、值截断（用于"同一条命令"判定）。"""
    if not arguments:
        return ""
    parts: list[str] = []
    for key in sorted(arguments):
        if key in VOLATILE_KEYS:
            continue
        value = arguments[key]
        if isinstance(value, (dict, list)):
            value = repr(value)
        text = _WS.sub(" ", str(value)).strip()
        if len(text) > 120:
            text = text[:120] + "…"
        parts.append(f"{key}={text}")
    return "&".join(parts)


def fingerprint(tool: str, arguments: dict[str, Any] | None) -> str:
    """``(tool, 归一化参数)`` 的短指纹。"""
    raw = f"{tool or ''}?{normalize_arguments(arguments)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]  # noqa: S324 - 非安全用途


def similarity(left: str, right: str) -> float:
    """归一化后的文本相似度（0~1，``difflib`` 实现，无第三方依赖）。"""
    a, b = normalize_text(left), normalize_text(right)
    if len(a) < 8 or len(b) < 8:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass(frozen=True)
class Observation:
    """一条检测结论（是否真的提醒由调用方 + 限频器决定）。"""

    solver_run_id: str
    rule: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "solverRunId": self.solver_run_id,
            "rule": self.rule,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class _Window:
    """单个 Solver 的滑动窗口状态。"""

    last_fingerprint: str = ""
    repeat_count: int = 0
    first_seen_at: float = 0.0
    last_progress_at: float = 0.0
    produced_count: int = 0
    prompted: set[str] = field(default_factory=set)
    reminder_times: list[float] = field(default_factory=list)


class ObserverDetector:
    """三条规则的纯逻辑实现（线程/协程内共享，非跨进程）。"""

    def __init__(self, config: ObserverConfig) -> None:
        self.config = config
        self._windows: dict[str, _Window] = {}

    # ------------------------------------------------------------------
    # 窗口管理
    # ------------------------------------------------------------------
    def _window(self, solver_run_id: str, *, now: float | None = None) -> _Window:
        stamp = time.monotonic() if now is None else now
        window = self._windows.get(solver_run_id)
        if window is None:
            window = _Window(first_seen_at=stamp, last_progress_at=stamp)
            self._windows[solver_run_id] = window
        return window

    def forget(self, solver_run_id: str) -> None:
        """Solver 结束后清理窗口（避免内存泄漏）。"""
        self._windows.pop(solver_run_id, None)

    # ------------------------------------------------------------------
    # D1：原地打转
    # ------------------------------------------------------------------
    def note_tool_call(
        self,
        solver_run_id: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        now: float | None = None,
    ) -> Observation | None:
        """记录一次工具调用；连续重复达阈值则给出 D1。"""
        window = self._window(solver_run_id, now=now)
        current = fingerprint(tool, arguments)
        if current == window.last_fingerprint:
            window.repeat_count += 1
        else:
            window.last_fingerprint = current
            window.repeat_count = 1
        threshold = max(2, int(self.config.repeat_tool_threshold))
        if window.repeat_count < threshold:
            return None
        reason = (
            f"连续 {window.repeat_count} 次执行同一条命令（{tool}），很可能在原地打转："
            "请改变参数/策略，或把这条路径写进账本（failure）后换方向。"
        )
        # 同一指纹只在达到阈值那一刻提醒一次，之后每多一次重复不再重复生成
        if current in window.prompted:
            return None
        window.prompted.add(current)
        return Observation(
            solver_run_id=solver_run_id,
            rule=D1_REPEAT_TOOL,
            reason=reason,
            detail=f"tool={tool} args={normalize_arguments(arguments)[:120]}",
        )

    # ------------------------------------------------------------------
    # D2：无进展漂移
    # ------------------------------------------------------------------
    def note_progress(self, solver_run_id: str, *, produced_count: int = 0, now: float | None = None) -> None:
        """账本有新产出（memory_write / idea_transition）→ 刷新进度时间。"""
        stamp = time.monotonic() if now is None else now
        window = self._window(solver_run_id, now=stamp)
        window.last_progress_at = stamp
        window.produced_count = max(window.produced_count, int(produced_count))

    def check_stall(
        self, solver_run_id: str, *, produced_count: int, now: float | None = None
    ) -> Observation | None:
        """无账本产出超过 ``no_progress_minutes`` → D2。"""
        stamp = time.monotonic() if now is None else now
        window = self._window(solver_run_id, now=stamp)
        if int(produced_count) > window.produced_count:
            self.note_progress(solver_run_id, produced_count=int(produced_count), now=stamp)
            return None
        minutes = max(0, int(self.config.no_progress_minutes))
        if minutes <= 0:
            return None
        idle_seconds = stamp - window.last_progress_at
        if idle_seconds < minutes * 60:
            return None
        return Observation(
            solver_run_id=solver_run_id,
            rule=D2_NO_PROGRESS,
            reason=(
                f"已连续 {int(idle_seconds // 60)} 分钟没有任何账本产出（memory_write/idea_transition）："
                "请立即把当前确定的事实或排除的路径落账本，不要继续空转。"
            ),
            detail=f"idleSeconds={int(idle_seconds)}",
        )

    # ------------------------------------------------------------------
    # D3：重复踩坑
    # ------------------------------------------------------------------
    def note_tool_result(
        self,
        solver_run_id: str,
        *,
        tool: str,
        text: str,
        failures: list[dict[str, Any]],
        now: float | None = None,
    ) -> Observation | None:
        """工具失败输出与已有 failure Memory 高度相似 → D3。"""
        self._window(solver_run_id, now=now)
        threshold = float(self.config.failure_similarity_threshold)
        probe = normalize_text(f"{tool} {text}")
        if len(probe) < 12:
            return None
        best: tuple[float, str] = (0.0, "")
        for failure in failures or []:
            summary = str(failure.get("summary") or "")
            score = similarity(probe, summary)
            if score > best[0]:
                best = (score, summary)
        if best[0] < threshold:
            return None
        return Observation(
            solver_run_id=solver_run_id,
            rule=D3_REPEAT_FAILURE,
            reason=(
                f"本次失败与账本中已排除的路径高度相似（{best[0]:.2f}）："
                f"「{best[1][:80]}」。这条坑已经踩过，请换方向或收尾。"
            ),
            detail=f"similarity={best[0]:.2f} tool={tool}",
        )

    # ------------------------------------------------------------------
    # 限频
    # ------------------------------------------------------------------
    def allow(self, solver_run_id: str, *, now: float | None = None) -> bool:
        """冷却窗内提醒次数是否未超限。"""
        stamp = time.monotonic() if now is None else now
        window = self._window(solver_run_id, now=stamp)
        cooldown = max(0.0, float(self.config.cooldown_per_solver_minutes) * 60)
        recent = [t for t in window.reminder_times if stamp - t < cooldown]
        window.reminder_times = recent
        limit = max(0, int(self.config.max_reminders_per_solver))
        return len(recent) < limit

    def mark_reminded(self, solver_run_id: str, *, now: float | None = None) -> None:
        stamp = time.monotonic() if now is None else now
        self._window(solver_run_id, now=stamp).reminder_times.append(stamp)

    def reminder_count(self, solver_run_id: str, *, now: float | None = None) -> int:
        stamp = time.monotonic() if now is None else now
        window = self._window(solver_run_id, now=stamp)
        cooldown = max(0.0, float(self.config.cooldown_per_solver_minutes) * 60)
        return len([t for t in window.reminder_times if stamp - t < cooldown])
