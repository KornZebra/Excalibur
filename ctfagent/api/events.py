"""事件类型定义与 SSE 序列化。

事件模型定义在 :mod:`ctfagent.events`（与存储解耦）；本模块只负责 HTTP/SSE 形态的转换。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..events import Event, EventBus, EventType, QueueEventBus
from ..textsafe import clean_json

try:  # pragma: no cover - 依赖可选
    from sse_starlette.sse import EventSourceResponse

    HAVE_SSE_STARLETTE = True
except ImportError:  # pragma: no cover
    EventSourceResponse = None  # type: ignore[misc,assignment]
    HAVE_SSE_STARLETTE = False

SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def event_to_payload(event: Event) -> str:
    """事件 → SSE data 行内容（JSON）。

    ``ensure_ascii=False`` 下，孤立代理字符会让 sse-starlette 的 UTF-8 编码失败并
    中断事件流，故序列化后统一清洗。
    """
    return clean_json(json.dumps(event.to_dict(), ensure_ascii=False, default=str))


def event_to_sse(event: Event) -> str:
    """事件 → 完整 SSE 帧（用于 StreamingResponse 回退路径）。"""
    return f"event: {event.type}\ndata: {event_to_payload(event)}\n\n"


async def iter_sse(bus: EventBus | QueueEventBus) -> AsyncIterator[Any]:
    """消费总线队列并产出 SSE 帧；收到 ``None`` 哨兵后结束。

    若安装了 sse-starlette 则产出 dict（由 EventSourceResponse 负责编码），
    否则产出已格式化的字符串帧。
    """
    queue = getattr(bus, "queue", None)
    if queue is None:  # pragma: no cover - 防御性分支
        return
    while True:
        event = await queue.get()
        if event is None:
            break
        if HAVE_SSE_STARLETTE:
            yield {"event": event.type, "data": event_to_payload(event)}
        else:
            yield event_to_sse(event)


def sse_response(bus: EventBus | QueueEventBus) -> Any:
    """把事件总线包装成 SSE 响应对象。"""
    if HAVE_SSE_STARLETTE:
        return EventSourceResponse(iter_sse(bus), ping=15, headers=SSE_HEADERS)
    from starlette.responses import StreamingResponse

    return StreamingResponse(iter_sse(bus), media_type="text/event-stream", headers=SSE_HEADERS)


__all__ = [
    "HAVE_SSE_STARLETTE",
    "SSE_HEADERS",
    "Event",
    "EventBus",
    "EventType",
    "QueueEventBus",
    "event_to_payload",
    "event_to_sse",
    "iter_sse",
    "sse_response",
]
