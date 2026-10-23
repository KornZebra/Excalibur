"""Memory 类型学：``fact | evidence | failure | note | hint``。

Memory 是"已确认/已发生"的事实记录，**追加式不可变**（只能新增，不能改写历史条目），
这样调度回路才能把 failure 当作"此路不通"的可靠依据。

类型语义：
- ``fact``     已确认的事实（可复用的结论，例如"目标为 Flask 2.0.1 且 /login 存在注入"）
- ``evidence`` 证据要点（命令输出的关键片段，指向落盘日志）
- ``failure``  此路不通（已排除的路径，**防止重复踩坑**）
- ``note``     观察（尚未确认的线索）
- ``hint``     人类提示（人在回路从"闸门"扩展为"导师"，优先级最高）
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .clock import now_iso

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from ..storage.repos import Repos

__all__ = ["MEMORY_PRIORITY", "PRIVILEGED_SOURCES", "MemoryKind", "MemoryService", "MemoryWriteError"]


class MemoryKind(StrEnum):
    """Memory 类型。"""

    FACT = "fact"
    EVIDENCE = "evidence"
    FAILURE = "failure"
    NOTE = "note"
    HINT = "hint"


#: 渲染排序优先级（数值越大越靠前）：hint > failure > fact/evidence > note
MEMORY_PRIORITY: dict[str, int] = {
    MemoryKind.HINT.value: 4,
    MemoryKind.FAILURE.value: 3,
    MemoryKind.FACT.value: 2,
    MemoryKind.EVIDENCE.value: 2,
    MemoryKind.NOTE.value: 1,
}

#: 只有人类/系统通道能写入的来源（agent 不得伪造，避免污染 hint）
PRIVILEGED_SOURCES: frozenset[str] = frozenset({"human", "system"})


class MemoryWriteError(ValueError):
    """非法的 Memory 写入。"""


def coerce_kind(value: str | MemoryKind) -> MemoryKind:
    if isinstance(value, MemoryKind):
        return value
    text = str(value or "").strip().lower()
    try:
        return MemoryKind(text)
    except ValueError as exc:
        allowed = "/".join(k.value for k in MemoryKind)
        raise MemoryWriteError(f"未知的 memory 类型：{value!r}（可选：{allowed}）") from exc


class MemoryService:
    """Memory 写入与查询（追加式）。"""

    def __init__(self, repos: Repos) -> None:
        self.repos = repos

    async def write(
        self,
        challenge_id: str,
        kind: str | MemoryKind,
        summary: str,
        *,
        body: str = "",
        source: str = "agent",
        solver_run_id: str | None = None,
        actor: str = "agent",
        allow_privileged_source: bool = False,
    ) -> dict[str, Any]:
        """追加一条 Memory。

        ``hint`` 必须由人类/系统通道写入（``allow_privileged_source=True``），
        否则任何能调用工具的模型都能伪造"人类提示"，破坏人在回路的可信度。
        """
        resolved = coerce_kind(kind)
        clean_summary = " ".join((summary or "").split())
        if not clean_summary:
            raise MemoryWriteError("memory summary 不能为空（必须写清：什么 + 在哪 + 如何验证）")
        if len(clean_summary) > 500:
            clean_summary = clean_summary[:497] + "…"

        normalized_source = (source or "agent").strip().lower() or "agent"
        if resolved is MemoryKind.HINT and normalized_source not in PRIVILEGED_SOURCES:
            raise MemoryWriteError("hint 只能由人类/系统通道写入（agent 不得伪造人类提示）")
        if normalized_source in PRIVILEGED_SOURCES and not allow_privileged_source:
            raise MemoryWriteError(f"来源 {normalized_source} 属于特权通道，当前调用方无权写入")
        return await self.repos.memories.append(
            challenge_id=challenge_id,
            kind=resolved.value,
            summary=clean_summary,
            body=body or "",
            source=normalized_source,
            solver_run_id=solver_run_id,
        )

    # ------------------------------------------------------------------
    async def recent(
        self,
        challenge_id: str,
        *,
        limit: int = 6,
        kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """按渲染优先级取最近若干条（同 kind 内按时间倒序）。"""
        rows = await self.repos.memories.list(challenge_id, kinds=kinds, limit=max(limit * 4, limit))
        rows.sort(key=lambda m: (MEMORY_PRIORITY.get(m["kind"], 0), m.get("createdAt") or ""), reverse=True)
        return rows[:limit]

    async def query(
        self,
        challenge_id: str,
        *,
        kinds: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """结构化查询（供 blackboard_query 工具与 API 使用），按时间倒序。"""
        return await self.repos.memories.list(challenge_id, kinds=kinds, limit=limit)

    async def counts(self, challenge_id: str) -> dict[str, int]:
        return await self.repos.memories.count_by_kind(challenge_id)

    async def failures(self, challenge_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """已排除的路径（Observer 的 D3 重复踩坑检测依据）。"""
        return await self.repos.memories.list(challenge_id, kinds=[MemoryKind.FAILURE.value], limit=limit)

    def stamp(self) -> str:
        return now_iso()
