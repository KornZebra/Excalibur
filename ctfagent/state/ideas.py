"""Idea 状态机：``pending → testing → {verified | failed | skipped}``。

Idea 表示"接下来该往哪走"的假设，是 Dispatcher 的**调度依据**；与表示"已确认事实"的
Memory 严格分离。本模块只负责状态机的合法性与联动（转 verified/failed 时自动写一条
Memory），持久化交给 :class:`~ctfagent.storage.repos.IdeaRepo`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .clock import now_iso
from .memory import MemoryKind, MemoryService

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from ..storage.repos import Repos

__all__ = [
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATUSES",
    "IdeaService",
    "IdeaStatus",
    "IdeaTransitionError",
    "TransitionResult",
    "coerce_status",
]


class IdeaStatus(StrEnum):
    """Idea 生命周期状态。"""

    PENDING = "pending"
    TESTING = "testing"
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"


#: 终态：不再接受任何转换
TERMINAL_STATUSES: frozenset[IdeaStatus] = frozenset(
    {IdeaStatus.VERIFIED, IdeaStatus.FAILED, IdeaStatus.SKIPPED}
)

#: 合法转换表（单测按此真值表逐项断言）
ALLOWED_TRANSITIONS: dict[IdeaStatus, frozenset[IdeaStatus]] = {
    # 未验证的方向可以直接跳过（例如发现前提不成立），但只能由 pending 直接跳过
    IdeaStatus.PENDING: frozenset({IdeaStatus.TESTING, IdeaStatus.SKIPPED}),
    IdeaStatus.TESTING: frozenset({IdeaStatus.VERIFIED, IdeaStatus.FAILED, IdeaStatus.SKIPPED}),
    IdeaStatus.VERIFIED: frozenset(),
    IdeaStatus.FAILED: frozenset(),
    IdeaStatus.SKIPPED: frozenset(),
}

#: 转换到该状态时必须已有认领者（solver_run）
REQUIRES_CLAIM: frozenset[IdeaStatus] = frozenset({IdeaStatus.TESTING})

#: 转换到该状态时自动补写一条 Memory（联动，防漏记）
AUTO_MEMORY_KIND: dict[IdeaStatus, MemoryKind] = {
    IdeaStatus.VERIFIED: MemoryKind.FACT,
    IdeaStatus.FAILED: MemoryKind.FAILURE,
}


class IdeaTransitionError(ValueError):
    """非法状态转换。"""


def coerce_status(value: str | IdeaStatus) -> IdeaStatus:
    """把字符串转成 :class:`IdeaStatus`；非法值抛出 :class:`IdeaTransitionError`。"""
    if isinstance(value, IdeaStatus):
        return value
    text = str(value or "").strip().lower()
    try:
        return IdeaStatus(text)
    except ValueError as exc:
        allowed = "/".join(s.value for s in IdeaStatus)
        raise IdeaTransitionError(f"未知的 idea 状态：{value!r}（可选：{allowed}）") from exc


@dataclass
class TransitionResult:
    """一次状态转换的结果（含联动写入的 Memory）。"""

    idea: dict[str, Any]
    from_status: IdeaStatus
    to_status: IdeaStatus
    memory: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


class IdeaService:
    """Idea 状态机服务（业务规则唯一入口）。"""

    def __init__(self, repos: Repos) -> None:
        self.repos = repos
        self.memories = MemoryService(repos)

    # ------------------------------------------------------------------
    async def upsert(
        self,
        challenge_id: str,
        text: str,
        *,
        idea_id: str | None = None,
        created_by: str = "agent",
    ) -> tuple[dict[str, Any], bool]:
        """声明一个新方向；同文本去重。返回 ``(idea, created)``。"""
        clean = " ".join((text or "").split())
        if not clean:
            raise IdeaTransitionError("idea 文本不能为空（必须是一句可验证的假设）")
        if idea_id:
            existing = await self.repos.ideas.get(idea_id)
            if existing is None:
                raise IdeaTransitionError(f"idea 不存在：{idea_id}")
            return existing, False
        found = await self.repos.ideas.find_by_text(challenge_id, clean)
        if found is not None:
            return found, False
        idea = await self.repos.ideas.create(
            challenge_id=challenge_id, text=clean, created_by=created_by or "agent"
        )
        return idea, True

    # ------------------------------------------------------------------
    async def transition(
        self,
        challenge_id: str,
        idea_id: str,
        status: str | IdeaStatus,
        *,
        evidence_summary: str = "",
        actor: str = "agent",
        solver_run_id: str | None = None,
        allow_privileged_source: bool = False,
    ) -> TransitionResult:
        """执行一次状态转换，并按需联动写入 Memory。"""
        target = coerce_status(status)
        idea = await self.repos.ideas.get(idea_id)
        if idea is None or idea.get("challengeId") != challenge_id:
            raise IdeaTransitionError(f"idea 不存在或不属于当前题目：{idea_id}")

        current = coerce_status(idea["status"])
        if current == target:
            # 幂等：同状态重复调用不算错误，但明确告知（避免模型误以为写入失败）
            return TransitionResult(idea=idea, from_status=current, to_status=target, notes=["状态未变化"])
        if target not in ALLOWED_TRANSITIONS[current]:
            allowed = (
                "/".join(sorted(s.value for s in ALLOWED_TRANSITIONS[current])) or "（终态，无可用转换）"
            )
            raise IdeaTransitionError(
                f"非法状态转换：{current.value} → {target.value}（当前状态允许：{allowed}）"
            )

        claimed_by = idea.get("claimedBy") or solver_run_id
        if target in REQUIRES_CLAIM and not claimed_by:
            raise IdeaTransitionError(
                "转换到 testing 前必须先认领该 Idea（缺少认领者 solver_run）。"
                "请由 Dispatcher 派发（blackboard 模式）或先由主代理认领。"
            )

        updated = await self.repos.ideas.update_status(
            idea_id, target.value, claimed_by=claimed_by, touch_transition=True
        )
        assert updated is not None
        result = TransitionResult(idea=updated, from_status=current, to_status=target)

        kind = AUTO_MEMORY_KIND.get(target)
        # 归因给"实际执行转换的运行"（有 run_id 用 run_id，否则退化到认领者）：
        # 这样收尾阶段写入的 Memory 也能被 SolverRun 统计到（失败即产出的判定依据）
        author_run = solver_run_id or claimed_by
        if kind is not None:
            summary = (evidence_summary or updated["text"]).strip()
            if target is IdeaStatus.FAILED and not evidence_summary.strip():
                summary = f"方向未走通：{updated['text']}"
            memory = await self.memories.write(
                challenge_id,
                kind,
                summary,
                body=(
                    f"idea_id={idea_id}\n"
                    f"状态：{current.value} → {target.value}\n"
                    f"假设：{updated['text']}\n"
                    f"证据/说明：{evidence_summary.strip() or '(未提供)'}"
                ),
                source="agent",
                solver_run_id=author_run,
                actor=actor,
                allow_privileged_source=allow_privileged_source,
            )
            result.memory = memory
            result.notes.append(f"已联动写入 {kind.value} Memory：{memory['summary']}")
        return result

    # ------------------------------------------------------------------
    async def open_ideas(self, challenge_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """未收敛的 Idea（pending + testing），供 Dispatcher/渲染器使用。"""
        return await self.repos.ideas.list(
            challenge_id, statuses=[IdeaStatus.PENDING.value, IdeaStatus.TESTING.value], limit=limit
        )

    async def counts(self, challenge_id: str) -> dict[str, int]:
        return await self.repos.ideas.count_by_status(challenge_id)

    async def age_seconds(self, idea: dict[str, Any]) -> float:
        """Idea 自最近一次状态变化以来的秒数。"""
        from .clock import seconds_since

        return seconds_since(idea.get("lastTransitionAt") or idea.get("createdAt"))

    def stamp(self) -> str:
        return now_iso()
