"""运行中引导（Steering）API：统一消息入口 / 状态查询 / 会话事件流 / 取消。

- ``POST /api/conversations/{id}/messages``：统一入口。会话无运行中任务 → 持久化
  消息并启动任务（status=running）；有 → 持久化（queued）并入队（status=queued）。
  前端无需判断该发哪个接口，由后端按会话状态分流。
- ``GET /api/conversations/{id}/state``：任务状态 + 轮次 + 队列积压（刷新恢复）。
- ``GET /api/conversations/{id}/events``：会话级 SSE 事件流，支持 Last-Event-ID
  断点补发；聚合运行任务的全部事件与 Steering 控制事件。
- ``POST /api/conversations/{id}/cancel``：取消（graceful 跑完当前轮 / hard 立即中止）。

与既有 per-run SSE（``POST /api/agent/stream``）的关系：后者保持不变（CLI/兼容用），
新前端统一走本模块的会话级事件流。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..models.types import ChatMessage
from ..runtime import Runtime
from ..steering import SteerItem
from ..textsafe import clean_json
from .events import SSE_HEADERS

logger = logging.getLogger(__name__)
router = APIRouter(tags=["steering"])

#: 单条消息最大长度（超长返回 400）
MAX_MESSAGE_CHARS = 32_000

try:  # pragma: no cover - 依赖可选（与 api/events.py 保持一致）
    from sse_starlette.sse import EventSourceResponse

    HAVE_SSE_STARLETTE = True
except ImportError:  # pragma: no cover
    EventSourceResponse = None  # type: ignore[misc,assignment]
    HAVE_SSE_STARLETTE = False


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="运行时尚未就绪")
    return runtime


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class ConversationMessageRequest(BaseModel):
    """统一消息入口请求体。

    除 ``text`` 外的字段只在"启动任务"分支生效（排队消息忽略它们——沿用任务
    启动时的配置）。
    """

    text: str = Field(..., description="用户消息内容（空白返回 400）")
    mode: Literal["single", "multi"] | None = Field(
        default=None, description="single | multi；缺省时 multi_agent.enabled=true 视为 multi"
    )
    orchestration: Literal["deep", "plan_execute"] | None = Field(
        default=None, description="编排模式（multi 时有效），默认取配置"
    )
    challenge_id: str | None = Field(
        default=None, alias="challengeId", description="题目 ID；缺省用会话绑定的题目"
    )
    attachment_ids: list[str] = Field(
        default_factory=list, alias="attachmentIds", description="附件 ID（仅启动任务时生效）"
    )
    interrupt: bool | None = Field(
        default=None,
        description="运行中发送时是否强制中断当前轮并注入本消息（缺省 = 中断并继续）",
    )
    model: str | None = Field(
        default=None, description="本次任务使用的模型通道 key；缺省用会话绑定的模型"
    )

    model_config = {"populate_by_name": True}


class CancelTaskRequest(BaseModel):
    mode: Literal["hard", "graceful"] = Field(default="hard", description="hard 立即中止 / graceful 跑完当前轮")


# ---------------------------------------------------------------------------
# 统一消息入口
# ---------------------------------------------------------------------------
@router.post("/api/conversations/{conversation_id}/messages", summary="发送消息（运行中自动入队引导）")
async def send_conversation_message(
    conversation_id: str, payload: ConversationMessageRequest, request: Request
) -> dict[str, Any]:
    runtime = get_runtime(request)
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="消息不能为空（空白字符也不允许）")
    if len(text) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=400, detail=f"消息超长（最多 {MAX_MESSAGE_CHARS} 字符）")

    coordinator = runtime.steering
    # 会话锁串行化"发送 vs 任务刚结束"竞态：要么入队到旧任务，要么启动新任务，
    # 不存在既不入队也不触发任务的黑洞。
    async with coordinator.lock(conversation_id):
        conversation = await runtime.repos.conversations.get(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail=f"未知会话：{conversation_id}")

        if coordinator.is_running(conversation_id):
            session = coordinator.session(conversation_id)
            # ---- 引导分支：立即落库（queued）并入 FIFO 队列，不打断当前轮 ----
            saved = await runtime.repos.messages.append(
                conversation_id, ChatMessage.user(text), delivery_status="queued"
            )
            item = SteerItem(id=saved["id"], text=text)
            queue_length = await coordinator.enqueue(conversation_id, item)
            # 中断并继续（缺省开启）：掐断当前推理/工具执行，主循环安全点注入本消息后续跑。
            # payload.interrupt=False 时保持旧行为（排队等当前轮自然结束）。
            interrupt = True if payload.interrupt is None else bool(payload.interrupt)
            if interrupt:
                coordinator.request_interrupt(conversation_id)
                runtime.cancel_running_tools(session.run_id)
                session.hub.publish(
                    "steer_interrupt", {"id": saved["id"], "runId": session.run_id}
                )
            return {
                "status": "queued",
                "messageId": saved["id"],
                "taskId": session.run_id if session else "",
                "queueLength": queue_length,
                "interrupt": interrupt,
            }

        # ---- 空闲分支：持久化消息并启动新任务（消息即任务首轮输入） ----
        saved = await runtime.repos.messages.append(
            conversation_id, ChatMessage.user(text), delivery_status="delivered"
        )
        mode = payload.mode or ("multi" if runtime.config.multi_agent.enabled else "single")
        challenge_id = payload.challenge_id or conversation.get("challengeId") or None
        # 模型选择：一次性入参 > 会话绑定（meta.modelChannel）> 全局默认通道链
        model_channel = payload.model or (conversation.get("meta") or {}).get("modelChannel")
        handle = runtime.start_run(
            text,
            conversation_id=conversation_id,
            challenge_id=challenge_id,
            orchestration=payload.orchestration,
            multi=mode == "multi" and runtime.config.multi_agent.enabled,
            attachment_ids=payload.attachment_ids,
            steering=coordinator,
            pre_persisted_user_message_id=saved["id"],
            model_channel=model_channel,
        )
        session = coordinator.register(conversation_id, handle)
        coordinator.watch(session, runtime.repos)
        return {"status": "running", "messageId": saved["id"], "taskId": handle.run_id}


# ---------------------------------------------------------------------------
# 状态查询（刷新恢复）
# ---------------------------------------------------------------------------
@router.get("/api/conversations/{conversation_id}/state", summary="会话任务状态与引导队列")
async def conversation_state(conversation_id: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    coordinator = runtime.steering
    async with coordinator.lock(conversation_id):
        conversation = await runtime.repos.conversations.get(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail=f"未知会话：{conversation_id}")
        running = coordinator.is_running(conversation_id)
        session = coordinator.session(conversation_id)
        # 队列真相源在 messages 表（入队即落库 /drain 即置 delivered），刷新恢复不依赖内存
        queued = await runtime.repos.messages.list_queued(conversation_id)
    return {
        "taskStatus": "running" if running else "idle",
        "round": session.round if (running and session) else 0,
        "runId": session.run_id if (running and session) else None,
        "queuedMessages": queued,
        # 事件游标：前端恢复运行中视图时以此为续订点（seq > lastEventId），
        # 避免切回会话时把整个回放缓冲（可达 2000 条）重放进时间线。
        "lastEventId": session.hub.current_seq if (running and session) else 0,
    }


# ---------------------------------------------------------------------------
# 会话级事件流（SSE + Last-Event-ID 补发）
# ---------------------------------------------------------------------------
@router.get("/api/conversations/{conversation_id}/events", summary="会话事件流（SSE，支持断线补发）")
async def conversation_events(
    conversation_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    last_event_id_query: str | None = Query(default=None, alias="last_event_id"),
) -> Any:
    runtime = get_runtime(request)
    conversation = await runtime.repos.conversations.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail=f"未知会话：{conversation_id}")

    raw_last = last_event_id or last_event_id_query or "0"
    try:
        last_seq = max(0, int(raw_last))
    except ValueError:
        last_seq = 0

    hub = runtime.steering.hub(conversation_id)
    backlog, queue = hub.subscribe(last_seq=last_seq)

    async def _stream() -> Any:
        try:
            for seq, event_type, data in backlog:
                yield _frame(seq, event_type, data)
            while True:
                item = await queue.get()
                if item is None:  # pragma: no cover - 枢纽当前不投递哨兵
                    break
                seq, event_type, data = item
                if seq <= last_seq:
                    continue
                yield _frame(seq, event_type, data)
        finally:
            hub.unsubscribe(queue)

    if HAVE_SSE_STARLETTE:
        return EventSourceResponse(_stream(), ping=15, headers=SSE_HEADERS)
    from starlette.responses import StreamingResponse

    return StreamingResponse(
        _stream(), media_type="text/event-stream", headers=dict(SSE_HEADERS)
    )


def _frame(seq: int, event_type: str, data: dict[str, Any]) -> dict[str, str]:
    """构造 SSE 帧。

    ``ensure_ascii=False`` 让中文直出，但**孤立代理字符无法编码为 UTF-8**，
    sse-starlette 在写流时会抛 ``UnicodeEncodeError: surrogates not allowed``
    并中断整条事件流。因此序列化后再兜底清洗一次（覆盖 ``default=str`` 分支）。
    """
    body = json.dumps(data, ensure_ascii=False, default=str)
    return {
        "id": str(seq),
        "event": event_type,
        "data": clean_json(body),
    }


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------
@router.post("/api/conversations/{conversation_id}/cancel", summary="取消会话任务（graceful / hard）")
async def cancel_conversation_task(
    conversation_id: str, payload: CancelTaskRequest, request: Request
) -> dict[str, Any]:
    runtime = get_runtime(request)
    coordinator = runtime.steering
    async with coordinator.lock(conversation_id):
        conversation = await runtime.repos.conversations.get(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail=f"未知会话：{conversation_id}")
        ok = coordinator.cancel(conversation_id, mode=payload.mode)
    if not ok:
        raise HTTPException(status_code=409, detail="当前会话没有运行中的任务")
    # 排队消息保留在队列/库中（保持 queued），由用户决定重发或继续——绝不静默丢弃
    return {"ok": True, "mode": payload.mode}


__all__ = ["router"]
