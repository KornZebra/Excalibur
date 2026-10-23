"""Agent / 多代理的 SSE 路由与运行管理。"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..runtime import Runtime
from .events import sse_response

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agent"])


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="运行时尚未就绪")
    return runtime


class AgentStreamRequest(BaseModel):
    """单代理流式请求。"""

    message: str = Field(..., description="题目描述或用户指令")
    conversation_id: str | None = Field(default=None, description="续跑时传入已有会话 ID")
    challenge_id: str | None = Field(default=None, description="题目 ID（决定隔离工作区）")
    system_prompt: str | None = Field(default=None, description="临时覆盖系统提示词（调试用）")
    attachment_ids: list[str] = Field(
        default_factory=list,
        alias="attachmentIds",
        description="U2a 附件 ID 列表：后端生成附件清单块追加到本轮消息（为空则行为与改造前一致）",
    )

    model_config = {"populate_by_name": True}


class MultiAgentStreamRequest(AgentStreamRequest):
    """多代理流式请求。"""

    orchestration: Literal["deep", "plan_execute"] | None = Field(
        default=None, description="编排模式，默认取配置 multi_agent.default_orchestration"
    )
    agent_id: str | None = Field(default=None, description="指定主代理 id（默认 ctf-orchestrator）")


class CancelResponse(BaseModel):
    ok: bool
    runId: str


@router.post("/api/agent/stream", summary="单代理解题（SSE 事件流）")
async def agent_stream(payload: AgentStreamRequest, request: Request) -> Any:
    runtime = get_runtime(request)
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="message 不能为空")
    handle = runtime.start_run(
        payload.message,
        conversation_id=payload.conversation_id,
        challenge_id=payload.challenge_id,
        system_prompt=payload.system_prompt,
        attachment_ids=payload.attachment_ids,
        multi=False,
    )
    return sse_response(handle.bus)


@router.post("/api/multi-agent/stream", summary="多代理编排解题（SSE 事件流）")
async def multi_agent_stream(payload: MultiAgentStreamRequest, request: Request) -> Any:
    runtime = get_runtime(request)
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="message 不能为空")
    if not runtime.config.multi_agent.enabled:
        raise HTTPException(status_code=400, detail="多代理模式未启用（multi_agent.enabled=false）")
    handle = runtime.start_run(
        payload.message,
        conversation_id=payload.conversation_id,
        challenge_id=payload.challenge_id,
        orchestration=payload.orchestration,
        agent_id=payload.agent_id,
        system_prompt=payload.system_prompt,
        attachment_ids=payload.attachment_ids,
        multi=True,
    )
    return sse_response(handle.bus)


@router.post("/api/runs/{run_id}/cancel", response_model=CancelResponse, summary="取消运行")
async def cancel_run(run_id: str, request: Request) -> CancelResponse:
    runtime = get_runtime(request)
    ok = runtime.cancel_run(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"未知 run_id：{run_id}")
    return CancelResponse(ok=True, runId=run_id)


@router.get("/api/runs", summary="列出运行中的会话")
async def list_runs(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {"runs": runtime.active_runs()}


@router.get("/api/runs/history", summary="列出历史运行记录")
async def run_history(
    request: Request, conversation_id: str | None = None, limit: int = 50
) -> dict[str, Any]:
    runtime = get_runtime(request)
    runs = await runtime.repos.runs.list(conversation_id=conversation_id, limit=limit)
    return {"runs": runs}


__all__ = ["get_runtime", "router"]
