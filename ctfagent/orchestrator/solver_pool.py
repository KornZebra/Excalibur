"""Solver 生命周期包装器 + 双阶段 conclude（失败即产出）。

设计要点（对应升级提示词 §4.1）
--------------------------------
1. **双阶段**：第一阶段是正常 ReAct 执行；无论结束原因（完成 / 超时 / 取消 / 异常），
   第二阶段都会在**同一会话**追加一次收尾调用，强制模型收尾 Idea 并沉淀一条 Memory。
2. **失败即产出**：收尾若仍无产出，系统兜底自动写一条 ``failure`` Memory 并把认领的
   Idea 置为 ``failed``——"走不通"本身也是可复用产出，绝不允许任务结束时黑板为空。
3. **v1 零侵入**：只有"账本工具可用 + ``state_v2.enabled`` + ``solver.two_phase_conclude``"
   三个条件同时满足才启用包装；否则 roles 模式的 ``task`` 派发链路完全走 v1 原路径。
4. **超时判定是纯函数**：:func:`evaluate_solver_timeouts` 无 IO、无时钟，便于穷举单测。

软/硬超时
---------
- 软超时（``solver.soft_timeout_minutes``）：超过且**期间无新 Memory** → 先发一次效率提醒
  （交给 ``reminder_hook``，M4 由 Observer 转发），再宽限 ``solver.grace_minutes``；
  仍无进展才请求停止。
- 硬超时（``solver.hard_timeout_minutes``）：无条件请求停止。

"请求停止"是**协作式**的：设置 v1 循环已经在检查的 ``cancel_event``，让循环自己收尾，
因此第二阶段 conclude 仍能在同一次运行中正常执行（不依赖 asyncio 强杀）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from ..config import AppConfig
from ..events import EventType
from ..observer.detector import Observation, ObserverDetector
from ..observer.judge import ObserverJudge
from ..state.ideas import ALLOWED_TRANSITIONS, IdeaService, IdeaStatus, coerce_status
from ..state.memory import MemoryService
from ..state.renderer import RenderInputs, render_delta, render_worker_context

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from ..agent.loop import AgentRunner, RunResult
    from ..storage.repos import Repos

logger = logging.getLogger(__name__)


def delta_due(
    steps_since: int,
    elapsed_seconds: float,
    cfg_steps: int,
    cfg_seconds: float,
) -> bool:
    """M3 双闸门：步数达标**或**时间达标即触发黑板增量刷新；cfg_steps<=0 关闭。"""
    if cfg_steps <= 0:
        return False
    return steps_since >= cfg_steps or elapsed_seconds >= cfg_seconds

#: 账本工具名（Solver 工具集包含其中任意一个才启用 conclude）
LEDGER_TOOLS: tuple[str, ...] = ("idea_upsert", "idea_transition", "memory_write", "blackboard_query")

#: 收尾阶段额外允许的只读工具（让模型能在收尾前快速复核）
CONCLUDE_EXTRA_TOOLS: tuple[str, ...] = ("read_file", "grep", "bash_readonly")

CONCLUDE_INSTRUCTION = """\
【收尾阶段】本次任务即将结束（原因：{reason}）。

现在只做两件事，不要开展新方向：
1. 用 idea_transition 收尾你认领的 Idea：走通 → verified；没走通 → failed 并在
   evidence_summary 里写清**已排除的路径**（这是给后续 Solver 的关键情报）。
2. 用 memory_write 沉淀一条本次任务**唯一确定**的结论：成功 → fact；受阻 → failure。

如果没有确定结论，也必须写一条 failure，写明"试了什么、结果如何、为什么放弃"。
"""

#: 结束原因 → 人类可读的收尾说明
REASON_TEXT: dict[str, str] = {
    "done": "任务正常结束",
    "hard_timeout": "已达硬超时上限",
    "soft_timeout_no_progress": "软超时且长时间无新产出",
    "cancelled": "被请求停止",
    "error": "运行期异常",
    # 来自 v1 循环的结束状态（见 AgentRunner 的 status 取值）
    "runner_error": "运行期异常",
    "runner_max_iterations": "已达迭代上限",
}

#: 兜底 Memory 的总结模板（失败即产出：绝不允许空手而归）
FALLBACK_SUMMARY: dict[str, str] = {
    "done": "任务结束但未沉淀结论：{idea}",
    "hard_timeout": "任务硬超时未产出结论：{idea}",
    "soft_timeout_no_progress": "任务超时未产出结论：{idea}",
    "cancelled": "任务被中止未产出结论：{idea}",
    "error": "任务异常结束未产出结论：{idea}",
    "runner_error": "任务异常结束未产出结论：{idea}",
    "runner_max_iterations": "任务迭代耗尽未产出结论：{idea}",
}

#: v1 循环状态 → Solver 状态（保持 solver_runs.status 的取值域稳定）
STATUS_MAP: dict[str, str] = {
    "completed": "done",
    "done": "done",
    "failed": "failed",
    "error": "failed",
    "cancelled": "cancelled",
    "max_iterations": "timeout",
    "timeout": "timeout",
}


def normalize_status(status: str, outcome: str = "") -> str:
    """把循环状态/结束原因归一为 ``done|failed|cancelled|timeout``。"""
    if outcome in {"hard_timeout", "soft_timeout_no_progress"}:
        return "timeout"
    return STATUS_MAP.get(status, "done")


# ---------------------------------------------------------------------------
# 超时判定（纯函数）
# ---------------------------------------------------------------------------
def _reason_text(outcome: str) -> str:
    """结束原因 → 人类可读文本（未知原因回落到通用描述，绝不抛 KeyError）。"""
    return REASON_TEXT.get(outcome) or REASON_TEXT["done"]


@dataclass(frozen=True)
class TimeoutDecision:
    """超时判定结果：``action`` ∈ ``continue`` / ``remind`` / ``stop``。"""

    action: str
    reason: str = ""

    @property
    def should_stop(self) -> bool:
        return self.action == "stop"

    @property
    def should_remind(self) -> bool:
        return self.action == "remind"


CONTINUE = TimeoutDecision("continue")


def evaluate_solver_timeouts(
    *,
    elapsed: float,
    soft: float,
    hard: float,
    grace: float,
    has_progress: bool,
    reminder_at: float | None,
    progress_since_reminder: bool,
) -> TimeoutDecision:
    """判定一个 Solver 该继续、该提醒、还是该停止（单位：秒）。

    ``soft`` / ``hard`` / ``grace`` 为 0 表示"不启用该判定"；``reminder_at`` 为提醒时刻的
    ``elapsed`` 值（None 表示尚未提醒过）。
    """
    if hard > 0 and elapsed >= hard:
        return TimeoutDecision("stop", "hard_timeout")
    if soft <= 0:
        return CONTINUE
    if reminder_at is not None:
        if progress_since_reminder:
            return CONTINUE
        if elapsed >= reminder_at + grace:
            return TimeoutDecision("stop", "soft_timeout_no_progress")
        return CONTINUE
    if elapsed >= soft and not has_progress:
        return TimeoutDecision("remind", "soft_timeout_no_progress")
    return CONTINUE


def should_wrap(*, config: AppConfig, tool_names: list[str] | None) -> bool:
    """是否用本包装器替代裸 ``runner.run``（三个条件缺一不可）。"""
    if not (config.state_v2.enabled and config.solver.two_phase_conclude):
        return False
    if not tool_names:
        return False
    return any(name in LEDGER_TOOLS for name in tool_names)


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------
@dataclass
class ConcludeOutcome:
    """第二阶段（收尾）的结果。"""

    attempted: bool = False
    produced: bool = False
    fallback_used: bool = False
    memory_id: str | None = None
    idea_finalized: str | None = None
    skipped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "produced": self.produced,
            "fallbackUsed": self.fallback_used,
            "memoryId": self.memory_id,
            "ideaFinalized": self.idea_finalized,
            "skippedReason": self.skipped_reason,
        }


@dataclass
class SolverSpec:
    """一次 Solver 派发的输入。"""

    challenge_id: str
    task_type: str = "explore"
    idea_id: str | None = None
    idea_text: str = ""
    handoff: str = ""
    agent_id: str = "ctf-agent"
    agent_prompt: str | None = None
    system_prompt: str | None = None
    tool_names: list[str] | None = None
    max_iterations: int | None = None
    conversation_id: str | None = None
    conversation_title: str | None = None
    parent_conversation_id: str | None = None
    #: 随会话继承的审查模式（human | audit_agent | none；None = 跟随全局 default_reviewer）
    hitl_reviewer: str | None = None
    #: 黑板（facts）共享 scope：来自 run_task 的派单指向父会话；Dispatcher 直派的为 None
    blackboard_conversation_id: str | None = None
    #: 本轮战法 ID（作战图连线来源；空 = default）
    tactic_id: str = ""
    run_id: str | None = None
    services: dict[str, Any] | None = None

    def describe(self) -> str:
        parts = [f"task_type={self.task_type}"]
        if self.idea_id:
            parts.append(f"idea={self.idea_id}")
        if self.idea_text:
            parts.append(f"idea_text={self.idea_text[:80]}")
        return " | ".join(parts)


@dataclass
class SolverResult:
    """一次 Solver 运行的最终产物。"""

    solver_run_id: str
    challenge_id: str
    task_type: str
    status: str = "done"
    outcome: str = "done"
    concluded: bool = False
    idea_id: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    content: str = ""
    flags: list[str] = field(default_factory=list)
    iterations: int = 0
    tool_calls: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    reminders: list[str] = field(default_factory=list)
    conclude: ConcludeOutcome | None = None
    run_result: RunResult | None = None

    @property
    def solved(self) -> bool:
        return bool(self.flags)

    @property
    def succeeded(self) -> bool:
        return self.status == "done" and not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "solverRunId": self.solver_run_id,
            "challengeId": self.challenge_id,
            "taskType": self.task_type,
            "status": self.status,
            "outcome": self.outcome,
            "concluded": self.concluded,
            "ideaId": self.idea_id,
            "conversationId": self.conversation_id,
            "runId": self.run_id,
            "iterations": self.iterations,
            "toolCalls": self.tool_calls,
            "durationSeconds": round(self.duration_seconds, 2),
            "flags": self.flags,
            "error": self.error,
            "reminders": list(self.reminders),
            "conclude": self.conclude.to_dict() if self.conclude else None,
        }


ReminderHook = Callable[[str, dict[str, Any]], Awaitable[None]]
EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


# ---------------------------------------------------------------------------
# SolverRun
# ---------------------------------------------------------------------------
class SolverRun:
    """包装一次 Solver 执行（复用 v1 的 ReAct 循环 + 会话）。"""

    def __init__(
        self,
        *,
        spec: SolverSpec,
        config: AppConfig,
        repos: Repos,
        runner: AgentRunner,
        emit: EmitFn | None = None,
        cancel_event: asyncio.Event | None = None,
        reminder_hook: ReminderHook | None = None,
        watch_interval_seconds: float = 15.0,
        judge_model: Any = None,
    ) -> None:
        self.spec = spec
        self.config = config
        self.repos = repos
        self.runner = runner
        self.emit = emit
        self.cancel_event = cancel_event or asyncio.Event()
        self.reminder_hook = reminder_hook
        self.watch_interval = max(0.01, watch_interval_seconds)
        # v2-M4：Observer 只提醒不决策；state_v2 关闭时完全不启用（v1 行为不变）
        self.observer_enabled = bool(config.observer.enabled and config.state_v2.enabled)
        self.observer = ObserverDetector(config.observer) if self.observer_enabled else None
        # M3：运行中黑板增量刷新（0 步 = 关闭）
        self.blackboard_refresh_enabled = int(getattr(config.solver, "blackboard_refresh_steps", 0) or 0) > 0
        self._bb_steps_since = 0
        self._bb_last_at = 0.0
        from ..state.clock import now_iso as _now_iso

        self._bb_since = _now_iso()
        self.judge = ObserverJudge(config.observer.judge, model=judge_model)
        self._seen_hint_ids: set[str] = set()

        self.solver_run_id: str = spec.run_id or ""
        self.conversation_id: str | None = spec.conversation_id
        self.run_id: str | None = spec.run_id
        #: 收尾阶段的 run_id（runs.id 是主键，不能与主阶段复用；账本归因时一并统计）
        self.conclude_run_id: str = ""
        self.run_result: RunResult | None = None
        self.reminders: list[str] = []
        self.record: dict[str, Any] | None = None

        self._status = "done"
        self._outcome = "done"
        self._error: str | None = None
        self._stop_reason: str = ""
        self._started_monotonic = 0.0
        self._last_output = ""
        self._conclude_outcome: ConcludeOutcome | None = None

    # ------------------------------------------------------------------
    # 对外控制
    # ------------------------------------------------------------------
    def request_stop(self, reason: str = "cancelled") -> None:
        """请求停止（协作式：置位 v1 循环已在检查的 cancel_event）。"""
        self._stop_reason = self._stop_reason or reason
        self.cancel_event.set()

    @property
    def stop_requested(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def elapsed_seconds(self) -> float:
        """已运行时长（秒）；未开始时为 0。"""
        if not self._started_monotonic:
            return 0.0
        return max(0.0, time.monotonic() - self._started_monotonic)

    async def add_reminder(self, text: str, *, source: str = "system") -> None:
        """记录并转发一条效率提醒（M4 由 Observer 调用）。"""
        clean = " ".join((text or "").split())
        if not clean:
            return
        self.reminders.append(clean)
        if self.reminder_hook is not None:
            try:
                await self.reminder_hook(clean, {"solverRunId": self.solver_run_id, "source": source})
            except Exception:  # noqa: BLE001 - 提醒失败不影响 Solver 执行
                logger.debug("reminder_hook 调用失败", exc_info=True)

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self.emit is None:
            return
        try:
            await self.emit(event_type, data)
        except Exception:  # noqa: BLE001 - SSE 失败不能中断解题
            logger.debug("SolverRun 事件发送失败：%s", event_type, exc_info=True)

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def run(self) -> SolverResult:
        self._started_monotonic = time.monotonic()
        record = await self.repos.solver_runs.create(
            challenge_id=self.spec.challenge_id,
            task_type=self.spec.task_type,
            idea_id=self.spec.idea_id,
            conversation_id=self.spec.conversation_id,
            solver_run_id=self.spec.run_id,
        )
        self.record = record
        self.solver_run_id = str(record["id"])
        # 账本写入按 solver_run_id 归因：主阶段用 solver_run_id，收尾阶段用其派生 id
        self.run_id = self.solver_run_id
        self.conclude_run_id = f"{self.solver_run_id}_conclude"
        await self._emit(
            EventType.SOLVER_SPAWNED,
            {
                "solverRunId": self.solver_run_id,
                "challengeId": self.spec.challenge_id,
                "taskType": self.spec.task_type,
                "ideaId": self.spec.idea_id,
                "ideaText": self.spec.idea_text,
                "agentId": self.spec.agent_id,
            },
        )

        # M1：先写一次心跳，避免启动窗口内无租约（TTL 内必有续约）
        await self._renew_lease()
        watchdog = asyncio.create_task(self._watchdog())
        try:
            await self._execute()
        except asyncio.CancelledError:
            self._status = "cancelled"
            self._outcome = self._stop_reason or "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - 任何异常都要走收尾（失败即产出）
            logger.exception("Solver %s 运行失败", self.solver_run_id)
            self._status = "failed"
            self._outcome = "error"
            self._error = f"{type(exc).__name__}: {exc}"
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            await self._safe_conclude()
            await self._finalize()

        return self._build_result()

    async def _execute(self) -> None:
        state_block = await self._build_state_block()
        await self._emit(
            EventType.PROGRESS,
            {
                "stage": "solver_start",
                "message": f"Solver 开始：{self.spec.describe()}",
                "solverRunId": self.solver_run_id,
            },
        )
        result = await self.runner.run(
            self.spec.handoff or f"执行任务：{self.spec.idea_text or self.spec.task_type}",
            conversation_id=self.spec.conversation_id,
            challenge_id=self.spec.challenge_id,
            agent_id=self.spec.agent_id,
            agent_prompt=self.spec.agent_prompt,
            system_prompt=self.spec.system_prompt,
            tool_names=self.spec.tool_names,
            max_iterations=self.spec.max_iterations,
            emit=self._observe_emit,
            cancel_event=self.cancel_event,
            is_sub_agent=True,
            new_conversation=self.spec.conversation_id is None,
            conversation_title=self.spec.conversation_title,
            parent_conversation_id=self.spec.parent_conversation_id,
            hitl_reviewer=self.spec.hitl_reviewer,
            blackboard_conversation_id=self.spec.blackboard_conversation_id,
            run_id=self.run_id,
            services=self.spec.services,
            state_block=state_block,
            dynamic_context=(
                self._dynamic_context
                if (self.observer_enabled or self.blackboard_refresh_enabled)
                else None
            ),
        )
        self.run_result = result
        self.conversation_id = result.conversation_id
        self._last_output = result.content or ""
        if result.status == "cancelled":
            self._status = "cancelled"
            self._outcome = self._stop_reason or "cancelled"
        elif result.status == "completed":
            self._status = "done"
            self._outcome = "done"
        else:
            # v1 循环把模型异常/迭代耗尽收敛成 status=error|max_iterations；
            # 这里保留原始状态作为 outcome，同时把错误信息透出到 SolverResult
            self._status = result.status or "done"
            self._outcome = f"runner_{result.status}"
            self._error = result.error or self._error

    # ------------------------------------------------------------------
    # 第二阶段：收尾
    # ------------------------------------------------------------------
    @property
    def ledger_available(self) -> bool:
        return should_wrap(config=self.config, tool_names=self.spec.tool_names)

    async def _safe_conclude(self) -> None:
        """收尾绝不允许抛异常（否则会掩盖原始结束原因）。"""
        try:
            self._conclude_outcome = await self._conclude()
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - 收尾失败也必须完成状态落库
            logger.exception("Solver %s 收尾阶段失败", self.solver_run_id)
            self._conclude_outcome = ConcludeOutcome(skipped_reason="收尾阶段异常，已跳过")

    async def _conclude(self) -> ConcludeOutcome:
        cfg = self.config
        if not cfg.solver.two_phase_conclude:
            return ConcludeOutcome(skipped_reason="solver.two_phase_conclude 已关闭")
        if not cfg.state_v2.enabled:
            return ConcludeOutcome(skipped_reason="state_v2.enabled 已关闭")
        if not self.ledger_available:
            return ConcludeOutcome(
                skipped_reason="Solver 工具集不含账本工具（roles 模式默认跳过，v1 行为不变）"
            )
        if self.record is None:
            return ConcludeOutcome(skipped_reason="solver_run 尚未落库")

        reason = self._outcome
        await self.repos.solver_runs.mark_concluding(self.solver_run_id)
        await self._emit(
            EventType.PROGRESS,
            {
                "stage": "solver_conclude",
                "message": f"进入收尾阶段（{REASON_TEXT.get(reason, reason)}）",
                "solverRunId": self.solver_run_id,
            },
        )

        before = await self._ledger_memory_count()
        instruction = CONCLUDE_INSTRUCTION.format(reason=_reason_text(reason))
        if self.reminders:
            instruction += "\n【本轮收到的效率提醒】\n" + "\n".join(f"- {r}" for r in self.reminders)

        try:
            conclude_run = await self.runner.run(
                instruction,
                conversation_id=self.conversation_id,
                challenge_id=self.spec.challenge_id,
                agent_id=self.spec.agent_id,
                agent_prompt=self.spec.agent_prompt,
                system_prompt=self.spec.system_prompt,
                tool_names=self._conclude_tools(),
                max_iterations=max(1, cfg.solver.max_conclude_iterations),
                emit=self._observe_emit,
                cancel_event=asyncio.Event(),  # 收尾阶段不受停止信号影响
                is_sub_agent=True,
                new_conversation=self.conversation_id is None,
                conversation_title=self.spec.conversation_title or "收尾阶段",
                parent_conversation_id=self.spec.parent_conversation_id,
                hitl_reviewer=self.spec.hitl_reviewer,
                blackboard_conversation_id=self.spec.blackboard_conversation_id,
                run_id=self.conclude_run_id,
                services=self.spec.services,
                state_block=await self._build_state_block(),
            )
            self.conversation_id = conclude_run.conversation_id
            if conclude_run.content:
                self._last_output = conclude_run.content
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 收尾调用失败 → 直接走系统兜底
            logger.warning("Solver %s 收尾调用失败：%s", self.solver_run_id, exc)
            return await self._fallback_conclude(reason, note=f"收尾调用失败：{exc}")

        after = await self._ledger_memory_count()
        if after > before:
            return ConcludeOutcome(attempted=True, produced=True)
        return await self._fallback_conclude(reason, note="")

    async def _ledger_memory_count(self) -> int:
        """主阶段 + 收尾阶段累计沉淀的 Memory 条数。"""
        main = await self.repos.memories.count_for_solver(self.solver_run_id)
        conclude = await self.repos.memories.count_for_solver(self.conclude_run_id)
        return main + conclude

    def _conclude_tools(self) -> list[str]:
        """收尾工具集：账本工具 + 少量只读工具（受限子集，避免收尾阶段跑偏）。"""
        names = set(self.spec.tool_names or [])
        extra = [t for t in CONCLUDE_EXTRA_TOOLS if t in names or t in {"read_file", "grep"}]
        ordered = [t for t in LEDGER_TOOLS if t in names]
        for tool in extra:
            if tool not in ordered:
                ordered.append(tool)
        # 注册表里必须有该工具，否则会被工具装配层忽略；这里只做保守过滤
        return ordered or list(LEDGER_TOOLS)

    async def _fallback_conclude(self, reason: str, *, note: str) -> ConcludeOutcome:
        """系统兜底：写入 failure Memory，并把认领的 Idea 置为 failed。"""
        outcome = ConcludeOutcome(attempted=True, produced=False, fallback_used=True)
        idea_label = self.spec.idea_text[:80] or self.spec.idea_id or "(未声明方向)"
        summary = FALLBACK_SUMMARY.get(reason, FALLBACK_SUMMARY["done"]).format(idea=idea_label)
        body = "\n".join(
            [
                f"task_type={self.spec.task_type}",
                f"status={self._status}",
                f"outcome={reason}",
                f"agent={self.spec.agent_id}",
                f"工具调用数={self.run_result.tool_calls if self.run_result else 0}",
                f"迭代数={self.run_result.iterations if self.run_result else 0}",
                f"备注={note or '(无)'}",
                "最后输出（截断）：",
                (self._last_output or "(无输出)")[:800],
            ]
        )
        try:
            memory = await MemoryService(self.repos).write(
                self.spec.challenge_id,
                "failure",
                summary,
                body=body,
                source="agent",
                solver_run_id=self.solver_run_id,
                actor="system",
            )
            outcome.memory_id = memory["id"]
        except Exception:  # noqa: BLE001 - 兜底写入失败也不能抛
            logger.exception("Solver %s 兜底 Memory 写入失败", self.solver_run_id)

        outcome.idea_finalized = await self._force_fail_idea(reason)
        await self._emit(
            EventType.SOLVER_CONCLUDED,
            {
                "solverRunId": self.solver_run_id,
                "challengeId": self.spec.challenge_id,
                "outcome": reason,
                "concluded": True,
                "fallbackUsed": True,
                "memoryId": outcome.memory_id,
                "ideaId": self.spec.idea_id,
            },
        )
        return outcome

    async def _force_fail_idea(self, reason: str) -> str | None:
        """把本 Solver 认领且仍未收敛的 Idea 置为 failed（失败即产出）。"""
        if not self.spec.idea_id:
            return None
        idea = await self.repos.ideas.get(self.spec.idea_id)
        if idea is None:
            return None
        current = coerce_status(idea["status"])
        if IdeaStatus.FAILED not in ALLOWED_TRANSITIONS[current]:
            return None
        try:
            result = await IdeaService(self.repos).transition(
                self.spec.challenge_id,
                self.spec.idea_id,
                "failed",
                evidence_summary=f"系统兜底：Solver 未产出结论（{_reason_text(reason)}）",
                actor="system",
                solver_run_id=self.solver_run_id,
            )
            return result.to_status.value
        except Exception:  # noqa: BLE001
            logger.debug("兜底置 failed 失败（Idea %s）", self.spec.idea_id, exc_info=True)
            return None

    # ------------------------------------------------------------------
    async def _finalize(self) -> None:
        status = normalize_status(self._status, self._outcome)
        self._status = status  # 让 SolverResult 与落库状态一致
        concluded = bool(
            self._conclude_outcome
            and (self._conclude_outcome.produced or self._conclude_outcome.fallback_used)
        )
        if self.observer is not None:
            self.observer.forget(self.solver_run_id)
        try:
            await self.repos.solver_runs.finish(
                self.solver_run_id,
                status=status,
                outcome=self._outcome,
                concluded=concluded,
                error=self._error,
                conversation_id=self.conversation_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Solver %s 状态落库失败", self.solver_run_id)
        await self._emit(
            EventType.SOLVER_CONCLUDED,
            {
                "solverRunId": self.solver_run_id,
                "challengeId": self.spec.challenge_id,
                "status": status,
                "outcome": self._outcome,
                "concluded": concluded,
                "ideaId": self.spec.idea_id,
                "durationSeconds": round(time.monotonic() - self._started_monotonic, 2),
                "flags": list(self.run_result.flags) if self.run_result else [],
            },
        )

    def _build_result(self) -> SolverResult:
        result = SolverResult(
            solver_run_id=self.solver_run_id,
            challenge_id=self.spec.challenge_id,
            task_type=self.spec.task_type,
            status=self._status,
            outcome=self._outcome,
            concluded=bool(
                self._conclude_outcome
                and (self._conclude_outcome.produced or self._conclude_outcome.fallback_used)
            ),
            idea_id=self.spec.idea_id,
            conversation_id=self.conversation_id,
            run_id=self.run_id,
            content=self._last_output,
            flags=list(self.run_result.flags) if self.run_result else [],
            iterations=self.run_result.iterations if self.run_result else 0,
            tool_calls=self.run_result.tool_calls if self.run_result else 0,
            duration_seconds=time.monotonic() - self._started_monotonic,
            error=self._error,
            reminders=list(self.reminders),
            conclude=self._conclude_outcome,
            run_result=self.run_result,
        )
        return result

    # ------------------------------------------------------------------
    # 看门狗（软/硬超时）
    # ------------------------------------------------------------------
    async def _renew_lease(self) -> None:
        """M1：续约（solver_runs.heartbeat_at + ideas.claim_expires_at）。

        失败只记 debug，不中断解题——宁可让 policy 用宽限判定，也不要因续约异常杀任务。
        """
        cfg = self.config.solver
        ttl = int(getattr(cfg, "lease_ttl_seconds", 0) or 0)
        if ttl <= 0:
            return
        try:
            await self.repos.solver_runs.renew_lease(self.solver_run_id, ttl_seconds=ttl)
            if self.spec.idea_id:
                from ..state.clock import iso_after

                await self.repos.ideas.update_status(
                    self.spec.idea_id,
                    "testing",
                    claimed_by=self.solver_run_id,
                    touch_transition=False,
                    claim_expires_at=iso_after(ttl / 60.0),
                )
        except Exception:  # noqa: BLE001
            logger.debug("Solver %s 租约续约失败", self.solver_run_id, exc_info=True)

    async def _watchdog(self) -> None:
        soft = self.config.solver.soft_timeout_minutes * 60
        hard = self.config.solver.hard_timeout_minutes * 60
        grace = self.config.solver.grace_minutes * 60
        reminder_at: float | None = None
        reminder_count = 0
        while True:
            await asyncio.sleep(self.watch_interval)
            # M1：每轮续约（watch_interval 默认 15s，TTL 180s → 12 次机会）
            await self._renew_lease()
            elapsed = time.monotonic() - self._started_monotonic
            try:
                count = await self.repos.memories.count_for_solver(self.solver_run_id)
            except Exception:  # noqa: BLE001 - 查询失败按"有进展"处理（宁可漏杀不可误杀）
                logger.debug("Solver 进度查询失败", exc_info=True)
                count = max(reminder_count, 1)
            has_progress = count > 0
            progress_since_reminder = count > reminder_count
            # M4-Observer D2：账本长时间无产出 → 旁路提醒（与超时看门狗互不替代）
            if self.observer is not None:
                try:
                    stall = self.observer.check_stall(self.solver_run_id, produced_count=count)
                    if stall is not None:
                        await self._observer_remind(stall)
                except Exception:  # noqa: BLE001
                    logger.debug("Observer D2 检测失败", exc_info=True)
            decision = evaluate_solver_timeouts(
                elapsed=elapsed,
                soft=soft,
                hard=hard,
                grace=grace,
                has_progress=has_progress,
                reminder_at=reminder_at,
                progress_since_reminder=progress_since_reminder,
            )
            if decision.should_remind:
                reminder_at = elapsed
                reminder_count = count
                await self.add_reminder(
                    f"已运行 {int(elapsed // 60)} 分钟仍无新产出：请立刻收窄方向，"
                    "或把已排除的路径写进黑板后结束本轮。",
                    source="watchdog",
                )
                continue
            if decision.should_stop:
                logger.info("Solver %s 触发超时停止：%s", self.solver_run_id, decision.reason)
                self.request_stop(decision.reason)
                return

    # ------------------------------------------------------------------
    # Observer（旁路纠偏：只提醒，不改变任务）
    # ------------------------------------------------------------------
    async def _observe_emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """包装 emit：在不改变 v1 事件流的前提下旁听工具调用/失败。"""
        payload = data or {}
        if self.observer is not None:
            try:
                observation = self._inspect_event(event_type, payload)
                if observation is None and event_type == EventType.TOOL_RESULT and payload.get("isError"):
                    observation = await self._check_repeat_failure(
                        str(payload.get("name") or ""), str(payload.get("result") or "")
                    )
                if observation is not None:
                    await self._observer_remind(observation)
            except Exception:  # noqa: BLE001 - 观察者异常绝不能影响解题
                logger.debug("Observer 检测失败", exc_info=True)
        await self._emit(event_type, payload)

    def _inspect_event(self, event_type: str, payload: dict[str, Any]) -> Observation | None:
        """把事件喂给检测器（D1 看 tool_call，D3 看失败的 tool_result）。"""
        assert self.observer is not None
        if event_type == EventType.TOOL_CALL:
            return self.observer.note_tool_call(
                self.solver_run_id,
                str(payload.get("name") or ""),
                payload.get("arguments") or {},
            )
        if event_type == EventType.TOOL_RESULT and payload.get("isError"):
            return None  # D3 需要账本里的 failure 文本，异步取（见 _check_repeat_failure）
        return None

    async def _check_repeat_failure(self, tool: str, text: str) -> Observation | None:
        """D3：失败输出与账本里已排除的路径比对。"""
        if self.observer is None:
            return None
        failures = await MemoryService(self.repos).failures(self.spec.challenge_id, limit=20)
        return self.observer.note_tool_result(self.solver_run_id, tool=tool, text=text, failures=failures)

    async def _observer_remind(self, observation: Observation) -> None:
        """限频 → （可选判官）→ 注入提醒 + 发 efficiency_reminder 事件。"""
        if self.observer is None:
            return
        if not self.observer.allow(self.solver_run_id):
            logger.debug("Observer 提醒被限频丢弃：%s", observation.rule)
            return
        if self.judge.enabled:
            confirmed = await self.judge.confirm(observation, task=self.spec.idea_text or self.spec.task_type)
            if not confirmed:
                return
        self.observer.mark_reminded(self.solver_run_id)
        await self.add_reminder(f"[Observer/{observation.rule}] {observation.reason}", source="observer")
        await self._emit(
            EventType.EFFICIENCY_REMINDER,
            {
                **(observation.to_dict()),
                "challengeId": self.spec.challenge_id,
                "reason": observation.reason,
            },
        )

    async def _blackboard_delta(self) -> str:
        """M3：按步数/时间双闸门，返回"他人新增事实"的增量摘要。

        只报**别人**的产出；措辞允许忽略，避免打断当前思路。
        """
        cfg = self.config.solver
        steps = int(getattr(cfg, "blackboard_refresh_steps", 0) or 0)
        if steps <= 0:
            return ""
        interval = float(getattr(cfg, "blackboard_refresh_seconds", 90) or 0)
        self._bb_steps_since += 1
        now = time.monotonic()
        if not delta_due(self._bb_steps_since, now - self._bb_last_at, steps, interval):
            return ""
        self._bb_steps_since = 0
        self._bb_last_at = now
        try:
            fresh = await self.repos.memories.list_since(
                self.spec.challenge_id,
                since_iso=self._bb_since,
                exclude_solver_run_id=self.solver_run_id,
                limit=3,
            )
        except Exception:  # noqa: BLE001 - 增量查询失败不中断解题
            logger.debug("黑板增量查询失败", exc_info=True)
            return ""
        if not fresh:
            return ""
        newest = str(fresh[0].get("createdAt") or "")
        if newest:
            self._bb_since = newest
        return (
            "【黑板更新（其他 Solver 的新事实，可参考也可忽略）】\n"
            + render_delta(fresh, limit=3)
        )

    async def _dynamic_context(self) -> str:
        """逐轮动态上下文：新提醒 + 运行中插入的 hint（只注入一次，只提醒不改任务）。"""
        lines: list[str] = []
        if self.reminders:
            lines.extend(f"- {text}" for text in dict.fromkeys(self.reminders))
            self.reminders.clear()
        if self.config.state_v2.enabled:
            try:
                hints = await MemoryService(self.repos).recent(
                    self.spec.challenge_id, limit=10, kinds=["hint"]
                )
            except Exception:  # noqa: BLE001 - 账本查询失败不中断解题
                logger.debug("hint 查询失败", exc_info=True)
                hints = []
            for hint in hints:
                memory_id = str(hint.get("id") or "")
                if not memory_id or memory_id in self._seen_hint_ids:
                    continue
                self._seen_hint_ids.add(memory_id)
                lines.append(f"- [人类提示] {hint.get('summary') or ''}")
        blocks: list[str] = []
        if lines:
            blocks.append("【Observer / 人类提醒（只提醒，不改变你被派发的任务）】\n" + "\n".join(lines))
        # M3：黑板增量（其他 Solver 的新事实）
        delta = await self._blackboard_delta()
        if delta:
            blocks.append(delta)
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # 渲染式上下文
    # ------------------------------------------------------------------
    async def _build_state_block(self) -> str:
        renderer_cfg = self.config.state_v2.renderer
        if not (self.config.state_v2.enabled and renderer_cfg.enabled):
            return ""
        challenge = await self.repos.challenges.get(self.spec.challenge_id)
        open_ideas = await self.repos.ideas.list(
            self.spec.challenge_id, statuses=["testing", "pending"], limit=renderer_cfg.idea_top
        )
        closed = await self.repos.ideas.list(
            self.spec.challenge_id,
            statuses=["verified", "failed", "skipped"],
            limit=renderer_cfg.idea_top,
        )
        ideas = (open_ideas + closed)[: renderer_cfg.idea_top]
        memories = await MemoryService(self.repos).recent(
            self.spec.challenge_id, limit=renderer_cfg.memory_top
        )
        session = await self.repos.challenge_sessions.get(self.spec.challenge_id)
        elapsed_minutes: float | None = None
        remaining: float | None = None
        if session and session.get("startedAt"):
            from ..state.clock import seconds_since

            elapsed_minutes = seconds_since(session["startedAt"]) / 60
            budget = float(session.get("timeBudgetMinutes") or 0)
            if budget > 0:
                remaining = max(0.0, budget - elapsed_minutes)
        return render_worker_context(
            challenge,
            {
                "id": self.solver_run_id,
                "taskType": self.spec.task_type,
                "ideaId": self.spec.idea_id,
                "ideaText": self.spec.idea_text,
                "handoff": self.spec.handoff,
            },
            budget_tokens=renderer_cfg.budget_tokens,
            idea_top=renderer_cfg.idea_top,
            memory_top=renderer_cfg.memory_top,
            handoff_max_chars=renderer_cfg.handoff_max_chars,
            inputs=RenderInputs(
                ideas=ideas,
                memories=memories,
                idea_sources=await self.repos.ideas.sources_of(
                    self.spec.challenge_id, [str(i.get("id") or "") for i in ideas]
                ),
                own_idea_id=self.spec.idea_id,
                own_solver_run_id=self.solver_run_id,
                observer_reminders=list(self.reminders),
                elapsed_minutes=elapsed_minutes,
                time_remaining_minutes=remaining,
            ),
        )


# ---------------------------------------------------------------------------
# SolverPool
# ---------------------------------------------------------------------------
class PoolFullError(RuntimeError):
    """并发已达上限（由 Dispatcher 负责在派发前检查）。"""


class SolverPool:
    """Solver 并发池：负责并发上限、后台任务登记与统一收尾。

    并发上限由本池**强制**（与模型行为无关），Dispatcher 只做"要不要派发"的决策。
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        repos: Repos,
        runner_factory: Callable[[], AgentRunner],
        emit: EmitFn | None = None,
        reminder_hook: ReminderHook | None = None,
        max_concurrent: int | None = None,
        watch_interval_seconds: float = 15.0,
        judge_model: Any = None,
    ) -> None:
        self.config = config
        self.repos = repos
        self.runner_factory = runner_factory
        self.emit = emit
        self.reminder_hook = reminder_hook
        #: M4：Observer 判官用的小模型通道（None ⇒ 判官关闭，提醒直接放行）
        self.judge_model = judge_model
        self.max_concurrent = max(1, max_concurrent or config.dispatcher.max_concurrent_solvers)
        self.watch_interval = watch_interval_seconds
        self._runs: dict[str, SolverRun] = {}
        self._tasks: dict[str, asyncio.Task[SolverResult]] = {}
        self._results: dict[str, SolverResult] = {}
        self._seq = 0

    # ------------------------------------------------------------------
    def has_capacity(self) -> bool:
        return len(self._tasks) < self.max_concurrent

    @property
    def capacity(self) -> int:
        return max(0, self.max_concurrent - len(self._tasks))

    def active(self) -> list[SolverRun]:
        return list(self._runs.values())

    def stats(self) -> dict[str, int]:
        return {
            "active": len(self._tasks),
            "capacity": self.capacity,
            "maxConcurrent": self.max_concurrent,
            "finished": len(self._results),
        }

    def results(self) -> list[SolverResult]:
        return list(self._results.values())

    # ------------------------------------------------------------------
    def _launch(
        self,
        spec: SolverSpec,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[SolverRun, asyncio.Task[SolverResult]]:
        if not self.has_capacity():
            raise PoolFullError(
                f"并发已达上限 {self.max_concurrent}（active={len(self._tasks)}）；先等待 Solver 收敛。"
            )
        run = SolverRun(
            spec=spec,
            config=self.config,
            repos=self.repos,
            runner=self.runner_factory(),
            emit=self.emit,
            cancel_event=cancel_event,
            reminder_hook=self.reminder_hook,
            watch_interval_seconds=self.watch_interval,
            judge_model=self.judge_model,
        )
        self._seq += 1
        key = f"slot{self._seq}"
        task: asyncio.Task[SolverResult] = asyncio.create_task(
            run.run(), name=f"solver:{spec.task_type}:{spec.idea_id or '-'}"
        )
        self._runs[key] = run
        self._tasks[key] = task
        task.add_done_callback(partial(self._collect, key, run))
        return run, task

    def _collect(self, key: str, run: SolverRun, task: asyncio.Task[SolverResult]) -> None:
        """任务结束回调：登记结果并释放并发额度（绝不抛异常）。"""
        self._tasks.pop(key, None)
        self._runs.pop(key, None)
        try:
            result = task.result()
        except asyncio.CancelledError:
            logger.info("Solver 任务被取消：%s", run.solver_run_id)
            return
        except Exception:  # noqa: BLE001
            logger.exception("Solver 任务异常：%s", run.solver_run_id)
            return
        if result.solver_run_id:
            self._results[result.solver_run_id] = result

    def start(
        self,
        spec: SolverSpec,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> SolverRun:
        """创建并**后台**启动一个 Solver（达到上限时抛 :class:`PoolFullError`）。"""
        run, _ = self._launch(spec, cancel_event=cancel_event)
        return run

    async def run_solver(self, spec: SolverSpec) -> SolverResult:
        """前台运行一个 Solver（等待其完成，含收尾阶段）。"""
        _, task = self._launch(spec)
        return await asyncio.shield(task)

    async def wait_all(self) -> list[SolverResult]:
        """等待全部在跑的 Solver 结束（返回本轮收集到的结果）。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        return self.results()

    async def cancel_all(
        self,
        reason: str = "challenge_stopped",
        *,
        grace_seconds: float = 5.0,
        force: bool = True,
    ) -> None:
        """停止全部 Solver。

        先请求协作式停止，``force=True`` 时再取消其 asyncio 任务：协作式停止只能等
        当前模型调用/工具执行自然返回（最长可达数百秒），人工停止必须尽快生效。
        取消后给 ``grace_seconds`` 宽限让 finally 里的状态落库与 SOLVER_CONCLUDED
        事件完成，仍未结束则二次取消并等待归档。
        """
        for run in self.active():
            run.request_stop(reason)
        if force:
            for task in list(self._tasks.values()):
                if not task.done():
                    task.cancel()
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while self._tasks and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if self._tasks:
            # 二次取消：打断收尾中仍阻塞的 await，随后等待归档完成
            for task in list(self._tasks.values()):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)


__all__ = [
    "CONCLUDE_EXTRA_TOOLS",
    "CONCLUDE_INSTRUCTION",
    "LEDGER_TOOLS",
    "ConcludeOutcome",
    "PoolFullError",
    "SolverPool",
    "SolverResult",
    "SolverRun",
    "SolverSpec",
    "TimeoutDecision",
    "evaluate_solver_timeouts",
    "should_wrap",
]
