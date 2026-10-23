"""竞赛运行日志总线（N1）——给指挥台提供 SSE 流式决策日志。

设计：
- 纯内存、按 competition_id 分桶（环形缓冲 500 条），不落库（日志是观测面，不是事实）；
- ``publish`` 同步调用（引擎内部零 await），订阅者用 ``asyncio.Queue`` 接收；
- 历史回放 + 实时推送由 API 层组合（先 history 再 queue）；
- 每条日志带 ``phase``（SYNC/SCHEDULE/PREPARE_ENV/DISPATCH/SUBMIT/FEEDBACK/ERROR）
  与 ``level``，消息里必须写清**决策理由**（便于复盘与评审）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

__all__ = ["CompetitionLogBus", "LOG_BUS"]

MAX_HISTORY = 500


class CompetitionLogBus:
    """竞赛日志总线（单进程内存实现）。"""

    def __init__(self, limit: int = MAX_HISTORY) -> None:
        self._limit = limit
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._subs: dict[str, set[asyncio.Queue]] = {}

    # ------------------------------------------------------------------
    def publish(
        self,
        competition_id: str,
        phase: str,
        message: str,
        *,
        level: str = "info",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写入一条日志并推给所有订阅者（同步，不阻塞引擎）。"""
        entry: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phase": phase,
            "level": level,
            "message": message,
        }
        if extra:
            entry["extra"] = extra
        bucket = self._history.setdefault(competition_id, deque(maxlen=self._limit))
        bucket.append(entry)
        for queue in list(self._subs.get(competition_id, ())):
            try:
                queue.put_nowait(entry)
            except asyncio.QueueFull:  # pragma: no cover - 订阅者积压时丢弃旧日志
                pass
        return entry

    def history(self, competition_id: str, limit: int = 200) -> list[dict[str, Any]]:
        bucket = self._history.get(competition_id)
        if not bucket:
            return []
        return list(bucket)[-max(1, int(limit)):]

    def clear(self, competition_id: str) -> None:
        self._history.pop(competition_id, None)

    # ------------------------------------------------------------------
    def subscribe(self, competition_id: str, *, maxsize: int = 256) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.setdefault(competition_id, set()).add(queue)
        return queue

    def unsubscribe(self, competition_id: str, queue: asyncio.Queue) -> None:
        bucket = self._subs.get(competition_id)
        if bucket is None:
            return
        bucket.discard(queue)
        if not bucket:
            self._subs.pop(competition_id, None)


#: 全局单例（单进程部署；多实例场景需换成外部总线）
LOG_BUS = CompetitionLogBus()
