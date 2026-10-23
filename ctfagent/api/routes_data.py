"""数据与治理路由：会话/消息/黑板/flag/题目/文件下载/HITL 审批/执行记录。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..agent.hitl import HITLDecision
from ..scope import fact_scope
from ..conversations_admin import (
    MAX_BATCH,
    DeleteConflict,
    batch_delete_conversations,
    cascade_delete_conversation,
)
from .routes_agent import get_runtime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["data"])

MAX_TITLE_LENGTH = 200


# ---------------------------------------------------------------------------
# 会话 / 消息
# ---------------------------------------------------------------------------
@router.get("/api/conversations", summary="列出会话")
async def list_conversations(request: Request, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {"conversations": await runtime.repos.conversations.list(limit=limit, offset=offset)}


@router.post("/api/conversations", summary="新建会话")
async def create_conversation(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    body = await _json_body(request)
    conversation = await runtime.repos.conversations.create(
        title=str(body.get("title") or "新会话"),
        challenge_id=body.get("challenge_id") or body.get("challengeId"),
    )
    return {"conversation": conversation}


@router.get("/api/conversations/{conversation_id}", summary="会话详情")
async def get_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    conversation = await runtime.repos.conversations.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{conversation_id}")
    return {"conversation": conversation}


class ConversationRenameRequest(BaseModel):
    """会话重命名请求（U2c）。"""

    title: str = Field(default="", description="新标题；去除首尾空白后不能为空，最长 200 字符")


@router.patch("/api/conversations/{conversation_id}", summary="重命名会话（U2c）")
async def rename_conversation(
    conversation_id: str,
    payload: ConversationRenameRequest,
    request: Request,
) -> dict[str, Any]:
    """重命名会话：只改标题，不动消息/附件/运行记录；会话不存在返回 404。"""
    runtime = get_runtime(request)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title 不能为空")
    if len(title) > MAX_TITLE_LENGTH:
        raise HTTPException(status_code=422, detail=f"title 最长 {MAX_TITLE_LENGTH} 字符")
    conversation = await runtime.repos.conversations.rename(conversation_id, title)
    if conversation is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{conversation_id}")
    logger.info("已重命名会话 %s：%r", conversation_id, title)
    return {"conversation": conversation}


# ---------------------------------------------------------------------------
# U6a 多模式审查：会话级 reviewer 覆盖 + 裁决历史
# ---------------------------------------------------------------------------
VALID_REVIEWERS = ("human", "audit_agent", "none")


class ConversationHitlRequest(BaseModel):
    """会话级审查模式覆盖请求（U6a）。"""

    #: human | audit_agent | none；null 表示清除覆盖（跟随全局 default_reviewer）
    reviewer: Literal["human", "audit_agent", "none"] | None = Field(default=None)


def _hitl_settings_payload(runtime: Any, conversation: dict[str, Any]) -> dict[str, Any]:
    """组装 GET /api/conversations/{id}/hitl 的响应。"""
    hitl_cfg = runtime.config.hitl
    session_value = conversation.get("hitlReviewer")
    inherited = session_value not in VALID_REVIEWERS
    return {
        "reviewer": session_value if not inherited else hitl_cfg.default_reviewer,
        "defaultReviewer": hitl_cfg.default_reviewer,
        "whitelist": list(hitl_cfg.tool_whitelist),
        "inherited": inherited,
    }


@router.get("/api/conversations/{conversation_id}/hitl", summary="查看会话审查模式（U6a）")
async def get_conversation_hitl(conversation_id: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    conversation = await runtime.repos.conversations.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{conversation_id}")
    return _hitl_settings_payload(runtime, conversation)


@router.patch("/api/conversations/{conversation_id}/hitl", summary="设置会话审查模式（U6a）")
async def set_conversation_hitl(
    conversation_id: str,
    payload: ConversationHitlRequest,
    request: Request,
) -> dict[str, Any]:
    """会话级审查模式覆盖，对**后续**工具调用立即生效；reviewer=null 恢复跟随全局。"""
    runtime = get_runtime(request)
    conversation = await runtime.repos.conversations.set_reviewer(
        conversation_id, payload.reviewer
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{conversation_id}")
    logger.info("会话 %s 审查模式 → %s", conversation_id, payload.reviewer or "(跟随全局)")
    return _hitl_settings_payload(runtime, conversation)


@router.get("/api/hitl/decisions", summary="审查裁决历史（U6a）")
async def list_hitl_decisions(
    request: Request,
    conversation_id: str | None = None,
    reviewer: str | None = Query(default=None, description="human | audit_agent | policy | whitelist"),
    decision: str | None = Query(default=None, description="approve | reject"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = 0,
) -> dict[str, Any]:
    runtime = get_runtime(request)
    decisions = await runtime.repos.hitl_decisions.list(
        conversation_id=conversation_id or None,
        reviewer=reviewer or None,
        decision=decision or None,
        limit=limit,
        offset=offset,
    )
    return {"decisions": decisions}


class BatchDeleteRequest(BaseModel):
    """批量删除请求（U2b）。"""

    ids: list[str] = Field(default_factory=list, description=f"会话 ID 列表，单批最多 {MAX_BATCH} 个")
    confirm: bool = Field(default=False, description="必须显式确认为 true，避免误删")


@router.delete("/api/conversations/{conversation_id}", summary="删除会话（级联清理，U2b）")
async def delete_conversation(
    conversation_id: str,
    request: Request,
    confirm: bool = Query(default=False, description="必须显式确认为 true"),
) -> dict[str, Any]:
    """删除会话：取消运行中任务 → 级联清理所有关联表 → 删除消息/附件与磁盘目录。

    需要 ``?confirm=true``（避免任何"静默数据丢失"）；运行中且取消失败时返回 409。
    """
    runtime = get_runtime(request)
    if not confirm:
        raise HTTPException(status_code=400, detail="删除会话需要显式确认：请带 ?confirm=true")
    try:
        info = await cascade_delete_conversation(
            conversation_id,
            repos=runtime.repos,
            config=runtime.config,
            runtime=runtime,
        )
    except DeleteConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if info["missing"]:
        raise HTTPException(status_code=404, detail=f"会话不存在：{conversation_id}")
    return {"ok": True, **info}


@router.post("/api/conversations/batch-delete", summary=f"批量删除会话（单批最多 {MAX_BATCH} 个，U2b）")
async def batch_delete(payload: BatchDeleteRequest, request: Request) -> dict[str, Any]:
    """批量删除：逐条独立处理（部分失败不影响其它），逐条返回结果。"""
    runtime = get_runtime(request)
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="批量删除需要显式确认：请求体带上 confirm=true")
    try:
        return await batch_delete_conversations(
            payload.ids,
            repos=runtime.repos,
            config=runtime.config,
            runtime=runtime,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/conversations/{conversation_id}/messages", summary="列出会话消息")
async def list_messages(
    conversation_id: str, request: Request, limit: int = 500, offset: int = 0
) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {"messages": await runtime.repos.messages.list(conversation_id, limit=limit, offset=offset)}


# ---------------------------------------------------------------------------
# 黑板
# ---------------------------------------------------------------------------
async def _resolve_fact_scope(
    runtime: Any, conversation_id: str, challenge_id: str | None = None
) -> str:
    """把 API 的 ``conversation_id`` 解析成黑板作用域。

    前端仍按会话查询（零改动）：会话若已绑定题目，黑板实际归属题目作用域
    （同一题目的多个会话共享一个黑板）。
    """
    if challenge_id:
        return fact_scope(challenge_id, conversation_id)
    conversation = await runtime.repos.conversations.get(conversation_id)
    bound = (conversation or {}).get("challengeId")
    return fact_scope(bound, conversation_id)


@router.get("/api/facts", summary="列出/检索黑板事实")
async def list_facts(
    request: Request,
    conversation_id: str = Query(...),
    challenge_id: str | None = None,
    query: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    runtime = get_runtime(request)
    scope = await _resolve_fact_scope(runtime, conversation_id, challenge_id)
    if query:
        facts = await runtime.repos.facts.search(scope, query, limit=limit)
    else:
        facts = await runtime.repos.facts.list(scope, limit=limit)
    return {"facts": facts, "scope": scope}


@router.get("/api/facts/{key}", summary="按 key 取黑板事实")
async def get_fact(
    key: str, request: Request, conversation_id: str = Query(...), challenge_id: str | None = None
) -> dict[str, Any]:
    runtime = get_runtime(request)
    scope = await _resolve_fact_scope(runtime, conversation_id, challenge_id)
    fact = await runtime.repos.facts.get(scope, key)
    if fact is None:
        raise HTTPException(status_code=404, detail=f"黑板不存在 key={key}")
    return {"fact": fact}


@router.put("/api/facts/{key}", summary="写入/更新黑板事实")
async def upsert_fact(key: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    body = await _json_body(request)
    conversation_id = str(body.get("conversationId") or body.get("conversation_id") or "")
    if not conversation_id:
        raise HTTPException(status_code=400, detail="缺少 conversationId")
    tags = body.get("tags") or []
    scope = await _resolve_fact_scope(runtime, conversation_id, body.get("challengeId"))
    fact = await runtime.repos.facts.upsert(
        scope,
        key,
        summary=str(body.get("summary") or ""),
        body=str(body.get("body") or ""),
        tags=[str(t) for t in tags] if isinstance(tags, list) else str(tags).split(","),
        conversation_id=conversation_id,
    )
    return {"fact": fact}


@router.delete("/api/facts/{key}", summary="删除黑板事实")
async def delete_fact(
    key: str, request: Request, conversation_id: str = Query(...), challenge_id: str | None = None
) -> dict[str, Any]:
    runtime = get_runtime(request)
    scope = await _resolve_fact_scope(runtime, conversation_id, challenge_id)
    await runtime.repos.facts.delete(scope, key)
    return {"ok": True}


# ---------------------------------------------------------------------------
# flag / 题目
# ---------------------------------------------------------------------------
@router.get("/api/flags", summary="列出 flag 记录")
async def list_flags(
    request: Request, conversation_id: str | None = None,
    challenge_id: str | None = None, limit: int = 100
) -> dict[str, Any]:
    runtime = get_runtime(request)
    # 题目维度优先：flag 常落在 explore 子会话，从题目跟踪进来时当前会话
    # （主会话）按会话查会是空——按 challenge 聚合全部子会话的 flag
    if challenge_id:
        return {"flags": await runtime.repos.flags.list_by_challenge(challenge_id, limit=limit)}
    return {"flags": await runtime.repos.flags.list(conversation_id=conversation_id, limit=limit)}


@router.post("/api/flags", summary="人工提交 flag")
async def submit_flag(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    body = await _json_body(request)
    value = str(body.get("value") or body.get("flag") or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="缺少 value")
    conversation_id = body.get("conversationId") or body.get("conversation_id")
    record = await runtime.repos.flags.add(
        value=value,
        conversation_id=conversation_id,
        challenge_id=body.get("challengeId") or body.get("challenge_id"),
        fmt=value.split("{", 1)[0] if "{" in value else "",
        validated=bool(body.get("validated", True)),
        source="user",
        meta={"note": body.get("note") or ""},
    )
    return {"flag": record}


@router.get("/api/challenges", summary="列出题目元数据（含解题状态）")
async def list_challenges(request: Request, limit: int = 100) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {"challenges": await runtime.repos.challenges.list_with_status(limit=limit)}


@router.put("/api/challenges/{challenge_id}", summary="登记/更新题目元数据")
async def upsert_challenge(challenge_id: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    body = await _json_body(request)
    body["id"] = challenge_id
    record = await runtime.repos.challenges.upsert(body)
    return {"challenge": record}


# ---------------------------------------------------------------------------
# 执行记录 / 轨迹
# ---------------------------------------------------------------------------
@router.get("/api/executions", summary="列出工具执行记录")
async def list_executions(
    request: Request,
    conversation_id: str | None = None,
    run_id: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {
        "executions": await runtime.repos.executions.list(
            conversation_id=conversation_id, run_id=run_id, limit=limit
        )
    }


@router.get("/api/executions/live", summary="列出内存中的执行状态（含运行中）")
async def live_executions(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {"executions": [r.summary(include_output=False) for r in runtime.execution_service.all_records()]}


@router.get("/api/traces", summary="列出 agent trace")
async def list_traces(request: Request, run_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {"traces": await runtime.repos.traces.list(run_id=run_id, limit=limit)}


# ---------------------------------------------------------------------------
# 文件下载（严格限制在 workspace 内）
# ---------------------------------------------------------------------------
@router.get("/api/files", summary="下载/查看工作区文件（含落盘输出）")
async def get_file(request: Request, path: str = Query(..., description="workspace 内的相对路径")) -> Any:
    runtime = get_runtime(request)
    target = resolve_workspace_file(runtime.config, path)
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


def resolve_workspace_file(config: Any, path: str) -> Path:
    """把请求路径解析为工作区内的真实文件（严格限制在 workspace 内）。

    兼容两种写法（与 ``read_file`` 的路径语义一致）：
    - ``_outputs/x.log``          → 相对**工作区根**
    - ``workspace/_outputs/x.log`` → 相对**项目根**（前端 fileUrl 的约定形式）
    仅当解析结果落在 ``config.workspace_root`` 之内才允许下载。
    """
    raw = (path or "").strip().lstrip("/")
    if not raw:
        raise HTTPException(status_code=400, detail="缺少 path 参数")
    workspace_root = config.workspace_root.resolve()
    for candidate in (config.workspace_root / raw, config.project_root / raw):
        try:
            target = candidate.resolve()
        except OSError:  # pragma: no cover - 极少见
            continue
        if not _is_within(target, workspace_root):
            continue
        if target.is_file():
            return target
    # 区分"越权"与"不存在"，便于排查
    probe = (config.workspace_root / raw).resolve()
    if not _is_within(probe, workspace_root):
        raise HTTPException(status_code=403, detail="只允许访问 workspace 内的文件")
    raise HTTPException(status_code=404, detail=f"文件不存在：{raw}")


# ---------------------------------------------------------------------------
# HITL 审批
# ---------------------------------------------------------------------------
class HitlDecisionRequest(BaseModel):
    decision: Literal["approve", "reject", "pending"] = Field(default="approve")
    reason: str = ""
    arguments: dict[str, Any] | None = None


@router.get("/api/hitl/pending", summary="列出待审批请求")
async def hitl_pending(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {"requests": [r.to_event() for r in runtime.hitl.pending_requests()]}


@router.post("/api/hitl/{request_id}", summary="回填审批结论（批准/拒绝/改参批准）")
async def hitl_decide(request: Request, request_id: str) -> dict[str, Any]:
    runtime = get_runtime(request)
    body = HitlDecisionRequest(**await _json_body(request))
    if body.decision == "pending":
        req = runtime.hitl.get_request(request_id)
        if req is None:
            raise HTTPException(status_code=404, detail=f"未知审批请求：{request_id}")
        return {"ok": False, "pending": True, "request": req.to_event()}

    approved = body.decision == "approve"
    decision = HITLDecision(
        approved=approved,
        reason=body.reason or ("人工批准" if approved else "人工拒绝"),
        arguments=body.arguments if approved and body.arguments else None,
        modified=bool(approved and body.arguments),
    )
    ok = runtime.hitl.resolve(request_id, decision)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"审批请求 {request_id} 不存在或已被处理（可能已超时自动拒绝）",
        )
    return {"ok": True, "approved": approved, "modified": decision.modified}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 允许空 body
        return {}
    return body if isinstance(body, dict) else {}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = ["router"]
