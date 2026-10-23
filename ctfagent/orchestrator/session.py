"""挑战会话：把"控制回路 + Solver 池 + SSE 总线 + 会话元状态"绑成一个可管理的对象。

一次挑战会话（``POST /api/challenges/{id}/run``）的生命周期::

    start_challenge_run  →  ChallengeSession（bus + pool + dispatcher）
        dispatcher.run()  →  轮询快照 → 策略决策 → 派发/停止  … 直到 verdict
        finish()          →  停 Solver（各自 conclude）→ writeup → 归档 → SSE terminated
        session.task 结束 →  bus.close()（SSE 流自然结束）

会话状态落 ``challenge_sessions`` 表（迁移 002 新增，v1 的 ``challenges`` 表零改动），
进程内的运行态（总线/池/回路任务）只存在于 Runtime。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..events import EventType, QueueEventBus
from ..state.clock import now_iso
from .dispatcher import Dispatcher
from .solver_pool import SolverPool
from .termination import TerminationVerdict

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from ..storage.repos import Repos

logger = logging.getLogger(__name__)

#: 会话状态
SESSION_RUNNING = "running"
SESSION_TERMINATED = "terminated"
SESSION_ABORTED = "aborted"


@dataclass
class ChallengeSession:
    """一次自主解题会话的进程内句柄。"""

    challenge_id: str
    bus: QueueEventBus
    dispatcher: Dispatcher
    pool: SolverPool
    repos: Repos
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = field(default_factory=time.time)
    task: asyncio.Task[Any] | None = None
    verdict: TerminationVerdict | None = None
    state: str = SESSION_RUNNING
    reason: str = ""
    error: str | None = None
    #: 本轮采用的战法（作战图连线带来；空 = 不干预）
    tactic_id: str = ""
    tactic_name: str = ""

    # ------------------------------------------------------------------
    def request_stop(self, reason: str = "human_stop") -> None:
        """人工停止（Dispatcher 下一轮以 STOPPED 收尾，各 Solver 各自 conclude）。"""
        self.cancel_event.set()
        self.dispatcher.request_stop(reason)

    @property
    def running(self) -> bool:
        return self.state == SESSION_RUNNING and bool(self.task and not self.task.done())

    def stats(self) -> dict[str, Any]:
        return {
            "challengeId": self.challenge_id,
            "state": self.state,
            "verdict": self.verdict.value if self.verdict else None,
            "reason": self.reason,
            "runId": self.bus.run_id,
            "tacticId": self.tactic_id,
            "tacticName": self.tactic_name,
            "elapsedSeconds": round(time.time() - self.started_at, 1),
            "ticks": self.dispatcher.ticks,
            "solvers": self.pool.stats(),
            "actions": self.dispatcher.actions[-10:],
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.stats()


async def start_session(
    *,
    challenge_id: str,
    repos: Repos,
    pool: SolverPool,
    dispatcher: Dispatcher,
    bus: QueueEventBus,
    time_budget_minutes: int,
    tactic_id: str = "",
    tactic_name: str = "",
) -> ChallengeSession:
    """登记会话元状态并启动控制回路（返回句柄，调用方消费 ``bus.queue``）。"""
    await repos.challenge_sessions.upsert(
        challenge_id,
        status=SESSION_RUNNING,
        time_budget_minutes=time_budget_minutes,
        started_at=now_iso(),
        ended_at=None,
        verdict=None,
        reason=None,
    )
    session = ChallengeSession(
        challenge_id=challenge_id,
        bus=bus,
        dispatcher=dispatcher,
        pool=pool,
        repos=repos,
        tactic_id=tactic_id,
        tactic_name=tactic_name,
    )
    await bus.emit(
        EventType.PROGRESS,
        {
            "stage": "challenge_start",
            "message": (
                f"挑战会话启动（预算 {time_budget_minutes} 分钟，调度由 Dispatcher 负责"
                + (f"，战法 {tactic_name}" if tactic_name else "")
                + "）"
            ),
            "challengeId": challenge_id,
            "timeBudgetMinutes": time_budget_minutes,
            "tacticId": tactic_id,
            "tacticName": tactic_name,
        },
    )
    session.task = asyncio.create_task(_drive(session), name=f"challenge:{challenge_id}")
    return session


async def _drive(session: ChallengeSession) -> None:
    """控制回路驱动协程：保证无论如何都能收到 terminated/done 并关闭 SSE。"""
    try:
        verdict = await session.dispatcher.run(session.challenge_id)
        session.verdict = verdict
        session.state = SESSION_ABORTED if verdict is TerminationVerdict.ABORTED else SESSION_TERMINATED
        session.reason = session.dispatcher.termination.describe(verdict)
    except asyncio.CancelledError:
        session.state = SESSION_ABORTED
        session.reason = "会话任务被取消"
        raise
    except Exception as exc:  # noqa: BLE001 - 控制回路异常必须落到会话状态与 SSE
        logger.exception("挑战会话 %s 控制回路异常", session.challenge_id)
        session.error = f"{type(exc).__name__}: {exc}"
        session.state = SESSION_ABORTED
        session.reason = session.error
        await session.repos.challenge_sessions.upsert(
            session.challenge_id,
            status=SESSION_ABORTED,
            verdict=TerminationVerdict.ABORTED.value,
            reason=session.error,
            ended_at=now_iso(),
        )
        await session.bus.emit(EventType.ERROR, {"message": session.error})
    finally:
        await session.bus.emit(
            EventType.DONE,
            {
                "challengeId": session.challenge_id,
                "verdict": session.verdict.value if session.verdict else None,
                "state": session.state,
                "reason": session.reason,
                "elapsedSeconds": round(time.time() - session.started_at, 1),
                "solvers": session.pool.stats(),
            },
        )
        session.bus.close()


__all__ = [
    "SESSION_ABORTED",
    "SESSION_RUNNING",
    "SESSION_TERMINATED",
    "ChallengeSession",
    "start_session",
]
