"""结构化事件定义与进程内事件总线。

事件与存储解耦：Agent 循环只调用 ``emit(type, data)``，
前端（SSE）、CLI（stdout）、测试（断言用）分别订阅总线。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class EventType(StrEnum):
    """SSE 事件协议（见提示词 §8）。"""

    CONVERSATION = "conversation"
    MESSAGE_SAVED = "message_saved"
    PROGRESS = "progress"
    PLANNING = "planning"
    TODOS = "todos"
    THINKING_STREAM_DELTA = "thinking_stream_delta"
    RESPONSE_START = "response_start"
    RESPONSE_DELTA = "response_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SUB_AGENT_REPLY_STREAM_DELTA = "sub_agent_reply_stream_delta"
    USAGE_SUMMARY = "usage_summary"
    HITL_REQUEST = "hitl_request"
    HITL_RESOLVED = "hitl_resolved"
    ERROR = "error"
    CANCELLED = "cancelled"
    RESPONSE = "response"
    DONE = "done"

    # ---- v2（M2–M4）追加：只增不改，v1 事件语义完全不变 ----
    SOLVER_SPAWNED = "solver_spawned"
    SOLVER_CONCLUDED = "solver_concluded"
    IDEA_TRANSITION = "idea_transition"
    DISPATCHER_DECISION = "dispatcher_decision"
    EFFICIENCY_REMINDER = "efficiency_reminder"
    HINT_ADDED = "hint_added"
    CHALLENGE_TERMINATED = "challenge_terminated"

    # ---- U6：多模式审查。audit_agent / policy / whitelist 的自动裁决卡片 ----
    HITL_AUTO_DECISION = "hitl_auto_decision"

    # ---- 运行中引导（Steering）：会话级事件（经 ConversationHub 转发/回放） ----
    ROUND_STARTED = "round_started"
    MESSAGE_QUEUED = "message_queued"
    MESSAGE_DELIVERED = "message_delivered"
    TASK_DONE = "task_done"
    TASK_ERROR = "task_error"


@dataclass
class Event:
    """一条结构化事件。"""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    agent_id: str = "ctf-agent"
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.data)
        payload.setdefault("runId", self.run_id)
        payload.setdefault("agent", self.agent_id)
        payload["ts"] = int(self.ts * 1000)
        return payload

    def to_sse(self) -> str:
        """格式化为 SSE 帧（``event: x\ndata: {...}\n\n``）。"""
        import json

        from .textsafe import clean_json

        body = clean_json(json.dumps(self.to_dict(), ensure_ascii=False, default=str))
        return f"event: {self.type}\ndata: {body}\n\n"


EventHandler = Callable[[Event], "Awaitable[None] | None"]


class EventBus:
    """进程内事件总线：支持多订阅者与绑定 run/agent 的 emitter。"""

    def __init__(self, *, run_id: str = "", agent_id: str = "ctf-agent") -> None:
        self.run_id = run_id
        self.agent_id = agent_id
        self._handlers: list[EventHandler] = []
        self._history: list[Event] = []
        self.keep_history = False

    # ------------------------------------------------------------------
    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """订阅事件，返回取消订阅函数。"""
        self._handlers.append(handler)

        def _unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return _unsubscribe

    async def publish(self, event: Event) -> None:
        if self.keep_history:
            self._history.append(event)
        for handler in list(self._handlers):
            try:
                result = handler(event)
                if result is not None and hasattr(result, "__await__"):
                    await result
            except Exception:  # noqa: BLE001 - 订阅者异常不影响主流程
                logger.warning("事件订阅者处理 %s 失败", event.type, exc_info=True)

    async def emit(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """发射一条事件。"""
        await self.publish(
            Event(
                type=str(event_type),
                data=data or {},
                run_id=run_id if run_id is not None else self.run_id,
                agent_id=agent_id if agent_id is not None else self.agent_id,
            )
        )

    def emitter(
        self, *, run_id: str | None = None, agent_id: str | None = None
    ) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
        """返回绑定了 run/agent 的 ``emit(type, data)`` 可等待函数。"""

        async def _emit(event_type: str, data: dict[str, Any] | None = None) -> None:
            await self.emit(event_type, data, run_id=run_id, agent_id=agent_id)

        return _emit

    @property
    def history(self) -> list[Event]:
        return list(self._history)


class QueueEventBus(EventBus):
    """把事件投递到 ``asyncio.Queue`` 的总线（SSE 路由使用）。"""

    def __init__(self, *, run_id: str = "", agent_id: str = "ctf-agent", maxsize: int = 0) -> None:
        super().__init__(run_id=run_id, agent_id=agent_id)
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize)
        self.subscribe(self._enqueue)

    def _enqueue(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - 无界队列不会触发
            logger.warning("事件队列已满，丢弃事件 %s", event.type)

    def close(self) -> None:
        """投递结束哨兵，通知消费方退出。"""
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover
            pass


__all__ = ["Event", "EventBus", "EventHandler", "EventType", "QueueEventBus"]
