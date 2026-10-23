"""v2 挑战会话 API：创建 / 启动 / 停止 / 快照 / flag / hint。

对应升级提示词 §5.5。全部为**新增**路由（v1 的 ``/api/agent/*``、``/api/multi-agent/*``
保持不变），因此关闭 v2 开关时前端与既有调用方不受影响。

注意：题目**列表**沿用 v1 的 ``GET /api/challenges``（routes_data.py），本模块不重复注册，
避免同一路径被两处定义（会出现注册顺序决定行为的隐患）。

合规：``POST /run`` 会做目标范围校验（``check_target_scope``，fail-closed），
校验不通过直接 400，绝不派发任何 Solver。
"""

from __future__ import annotations

import logging
import yaml
import mimetypes
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

from .. import attachments as att
from ..challenges_admin import (
    MAX_BATCH,
    DeleteConflict,
    batch_delete_challenges,
    cascade_delete_challenge,
)
from ..runtime import Runtime
from ..scope import fact_scope
from ..state.memory import MemoryService
from .events import sse_response

logger = logging.getLogger(__name__)
router = APIRouter(tags=["challenge"])


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="运行时尚未就绪")
    return runtime


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------
class ChallengeCreateRequest(BaseModel):
    """创建题目（登记到 v1 的 challenges 表 + 初始化挑战会话行）。

    携带 ``conversation_id`` 时为**从会话升级为题目**：绑定会话、迁移附件到题目
    工作区、并把会话已沉淀的黑板事实与最近结论播种到题目账本——控制台接手后
    不必从零重复劳动。
    """

    name: str = Field(..., description="题目名称")
    description: str = Field(default="", description="题目描述（含提示与约束）")
    category: str = Field(default="misc", description="分类：web/pwn/crypto/reverse/forensics/misc")
    target: str = Field(default="", description="靶机 IP:Port 或 URL（缺失则按纯附件题处理）")
    flag_format: str = Field(default="", description="flag 格式正则，如 flag\\{.*\\} 或 \\w+\\{.*\\}")
    attachments: list[str] = Field(default_factory=list, description="附件路径（workspace 内）")
    points: int = Field(default=0, description="分值")
    time_budget_minutes: int | None = Field(default=None, description="自主解题时间预算（分钟）")
    challenge_id: str | None = Field(default=None, description="可选：指定题目 ID（幂等更新）")
    project_id: str | None = Field(
        default=None,
        description="归属项目 ID（可选）；缺省时从升级来源会话继承，写入 meta.projectId",
    )
    meta: dict[str, Any] = Field(default_factory=dict, description="附加元数据（如 target_authorized）")
    conversation_id: str | None = Field(
        default=None, description="可选：从该会话升级——绑定会话并迁移附件与已有结论"
    )


class ChallengeFlagRequest(BaseModel):
    """人工提交/确认 flag。"""

    flag: str = Field(..., description="flag 值")


class ChallengeHintRequest(BaseModel):
    """人工提示（Hint 原语：人在回路从"闸门"扩展出"导师"）。"""

    text: str = Field(..., description="提示内容，会以 hint Memory 注入所有后续 Solver")
    author: str = Field(default="human", description="提示来源标识")


class ChallengeRunRequest(BaseModel):
    """启动自主解题。"""

    time_budget_minutes: int | None = Field(default=None, description="覆盖时间预算（分钟）")
    interval_seconds: float | None = Field(default=None, description="覆盖控制回路轮询间隔（调试用）")
    tactic: str | None = Field(
        default=None,
        description="战法 ID（作战图连线带来，如 web-blackbox / web-whitebox；空 = 不干预）",
    )


class OkResponse(BaseModel):
    ok: bool = True
    challengeId: str = ""
    detail: str = ""


class ChallengeBatchDeleteRequest(BaseModel):
    """批量删除题目请求（对齐会话批量删除 U2b）。"""

    ids: list[str] = Field(default_factory=list, description=f"题目 ID 列表，单批最多 {MAX_BATCH} 个")
    confirm: bool = Field(default=False, description="必须显式确认为 true，避免误删")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.post("/api/challenges", summary="创建/更新题目（可从现有会话升级）")
async def create_challenge(payload: ChallengeCreateRequest, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="name 不能为空")

    source_conversation = None
    if payload.conversation_id:
        source_conversation = await runtime.repos.conversations.get(payload.conversation_id)
        if source_conversation is None:
            raise HTTPException(status_code=404, detail=f"会话不存在：{payload.conversation_id}")

    # 归属项目：显式 project_id > meta.projectId > 升级来源会话的 project_id > 原题目已有归属
    # （题目表没有 project_id 列，统一写进 meta，看板按该字段识别归属）
    meta = dict(payload.meta or {})
    project_id = (payload.project_id or str(meta.get("projectId") or "")).strip()
    if not project_id and source_conversation is not None:
        project_id = str(source_conversation.get("projectId") or "").strip()
    if not project_id and payload.challenge_id:
        # 幂等更新时不丢已有归属（调用方通常只改名称/描述/附件）
        existing = await runtime.repos.challenges.get(payload.challenge_id)
        project_id = str(((existing or {}).get("meta") or {}).get("projectId") or "").strip()
    if project_id:
        if await runtime.repos.projects.get(project_id) is None:
            raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
        meta["projectId"] = project_id
    else:
        meta.pop("projectId", None)

    # 从会话升级时的默认值：附件/黑板播种都以该会话为源
    attachment_paths = list(payload.attachments)
    challenge = await runtime.repos.challenges.upsert(
        {
            "id": payload.challenge_id,
            "name": payload.name.strip(),
            "category": payload.category or "misc",
            "description": payload.description,
            "points": payload.points,
            "attachments": attachment_paths,
            "flag_pattern": payload.flag_format,
            "target": payload.target.strip(),
            "meta": meta,
        }
    )
    challenge_id = challenge["id"]
    session = await runtime.repos.challenge_sessions.upsert(
        challenge_id,
        time_budget_minutes=payload.time_budget_minutes
        or runtime.config.termination.default_time_budget_minutes,
    )

    upgraded: dict[str, Any] = {
        "conversationBound": False,
        "attachmentsMigrated": 0,
        "memoriesSeeded": 0,
    }
    if source_conversation is not None:
        conversation_id = source_conversation["id"]

        # 1) 绑定：该会话后续消息即运行在题目上下文（题目工作区/题目元数据/账本收尾）
        await runtime.repos.conversations.set_challenge(conversation_id, challenge_id)
        upgraded["conversationBound"] = True

        # 2) 迁移附件：把会话上传的附件复制进题目工作区并登记（Solver 可直接访问）
        workspace = runtime.config.challenge_workspace(challenge_id)
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            conversation_attachments = await runtime.repos.attachments.list(conversation_id)
        except Exception:  # noqa: BLE001 - 附件迁移失败不阻断升级
            conversation_attachments = []
        migrated: list[str] = []
        for att in conversation_attachments:
            src = att.get("storedPath") or ""
            name = att.get("filename") or "attachment.bin"
            if not src:
                continue
            src_path = Path(src)
            if not src_path.is_absolute():
                src_path = runtime.config.project_root / src_path
            try:
                if src_path.is_file():
                    dest = workspace / name
                    if not dest.exists() or dest.stat().st_size != src_path.stat().st_size:
                        shutil.copy2(src_path, dest)
                    migrated.append(str(dest))
                    upgraded["attachmentsMigrated"] += 1
            except OSError:
                logger.warning("附件迁移失败：%s → %s", src, workspace, exc_info=True)
        if migrated:
            merged = list(dict.fromkeys([*attachment_paths, *migrated]))
            challenge = await runtime.repos.challenges.upsert(
                {
                    "id": challenge_id,
                    "name": payload.name.strip(),
                    "category": payload.category or "misc",
                    "description": payload.description,
                    "points": payload.points,
                    "attachments": merged,
                    "flag_pattern": payload.flag_format,
                    "target": payload.target.strip(),
                    "meta": payload.meta,
                }
            )

        # 3) 播种题目账本：会话已确认的黑板事实 + 最近结论，控制台 Solver 接手即可见
        seeded = 0
        service = MemoryService(runtime.repos)
        source_scope = fact_scope(None, conversation_id)
        target_scope = fact_scope(challenge_id, conversation_id)
        try:
            conversation_facts = await runtime.repos.facts.list(source_scope, limit=60)
        except Exception:  # noqa: BLE001
            conversation_facts = []
        # U18：黑板作用域随会话一起升级 —— 会话期的事实并入题目黑板，
        # 否则控制台 Solver 接手时看不到这些结论（同题共享、跨题隔离）。
        if conversation_facts:
            try:
                moved = await runtime.repos.facts.rescope(source_scope, target_scope)
                logger.info("黑板作用域迁移：%s → %s（%d 条）", source_scope, target_scope, moved)
            except Exception:  # noqa: BLE001 - 迁移失败不阻断升级
                logger.warning("黑板作用域迁移失败：%s → %s", source_scope, target_scope, exc_info=True)
        for fact in conversation_facts:
            try:
                await service.write(
                    challenge_id,
                    "fact",
                    f"[来自会话] {fact.get('key')}: {fact.get('summary') or ''}",
                    body=fact.get("body") or "",
                    source="system",
                    actor="system",
                    allow_privileged_source=True,
                )
                seeded += 1
            except Exception:  # noqa: BLE001 - 单条播种失败不阻断
                logger.warning("黑板事实播种失败：%s", fact.get("key"), exc_info=True)
        last_conclusion = await runtime.repos.messages.last_assistant_content(conversation_id)
        seed_summary = (
            f"[会话升级] 本题目由会话 {conversation_id}（{source_conversation.get('title') or '未命名'}）"
            "升级而来；该会话已有解题过程与结论，避免从零重复侦察。"
        )
        if last_conclusion:
            seed_summary += f" 最近结论节选：{last_conclusion[:600]}"
        try:
            await service.write(
                challenge_id,
                "fact",
                seed_summary,
                body=(
                    f"source_conversation={conversation_id}\n"
                    f"message_count=见会话\n附件已迁移至题目工作区"
                ),
                source="system",
                actor="system",
                allow_privileged_source=True,
            )
            seeded += 1
        except Exception:  # noqa: BLE001
            logger.warning("会话升级摘要播种失败", exc_info=True)
        upgraded["memoriesSeeded"] = seeded

    return {"challenge": challenge, "session": session, "upgraded": upgraded}


@router.post("/api/challenges/{challenge_id}/run", summary="启动自主解题（SSE 事件流）")
async def run_challenge(
    challenge_id: str,
    payload: ChallengeRunRequest | None = None,
    request: Request = None,  # type: ignore[assignment]
) -> Any:
    runtime = get_runtime(request)
    payload = payload or ChallengeRunRequest()
    try:
        session = await runtime.start_challenge_run(
            challenge_id,
            time_budget_minutes=payload.time_budget_minutes,
            interval_seconds=payload.interval_seconds,
            tactic=payload.tactic,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 404 if detail.startswith("未知题目") else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    return sse_response(session.bus)


@router.post("/api/challenges/{challenge_id}/stop", response_model=OkResponse, summary="人工停止挑战")
async def stop_challenge(challenge_id: str, request: Request, reason: str = "human_stop") -> OkResponse:
    runtime = get_runtime(request)
    if not runtime.stop_challenge_run(challenge_id, reason):
        raise HTTPException(status_code=404, detail=f"挑战 {challenge_id} 当前没有运行中的会话")
    return OkResponse(challengeId=challenge_id, detail="已请求停止，各 Solver 完成收尾后归档")


@router.get("/api/challenges/{challenge_id}/state", summary="挑战快照（账本 + Solver + 会话）")
async def challenge_state(challenge_id: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    challenge = await runtime.repos.challenges.get(challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail=f"未知题目：{challenge_id}")
    return await runtime.challenge_snapshot(challenge_id)


@router.post("/api/challenges/{challenge_id}/flag", summary="人工提交/确认 flag")
async def submit_flag(challenge_id: str, payload: ChallengeFlagRequest, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    challenge = await runtime.repos.challenges.get(challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail=f"未知题目：{challenge_id}")
    try:
        flag = await runtime.submit_challenge_flag(challenge_id, payload.flag)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"flag": flag, "challengeId": challenge_id}


@router.post("/api/challenges/{challenge_id}/hint", summary="人工提示（注入 hint Memory）")
async def add_hint(challenge_id: str, payload: ChallengeHintRequest, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    challenge = await runtime.repos.challenges.get(challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail=f"未知题目：{challenge_id}")
    try:
        memory = await runtime.add_challenge_hint(challenge_id, payload.text, author=payload.author)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"memory": memory, "challengeId": challenge_id}


@router.post(
    "/api/challenges/{challenge_id}/attachments",
    summary="上传题目附件（写入题目工作区并登记到题目元数据）",
)
async def upload_challenge_attachments(
    challenge_id: str,
    request: Request,
    files: list[UploadFile] = File(..., description="一个或多个文件"),
) -> dict[str, Any]:
    """题目模块创建题目时上传附件。

    与对话附件同一套校验（开关/数量/体积/扩展名白名单）与消毒规则，区别是落盘目录为
    **题目隔离工作区**（``config.challenge_workspace``），并把绝对路径登记进
    ``challenges.attachments``——Solver 与 ``challenge_info`` 可直接访问。
    """
    runtime = get_runtime(request)
    cfg = runtime.config.attachment
    if not cfg.enabled:
        raise HTTPException(status_code=400, detail="附件上传未启用（attachment.enabled=false）")
    if not files:
        raise HTTPException(status_code=400, detail="未收到文件")
    challenge = await runtime.repos.challenges.get(challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail=f"未知题目：{challenge_id}")

    existing = [str(p) for p in (challenge.get("attachments") or [])]
    try:
        att.check_count(len(existing), len(files), cfg)
    except att.AttachmentError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    # 1) 先全部读入 + 校验（不落盘），任何一项失败整批拒绝
    payloads: list[tuple[str, bytes, str]] = []
    for upload in files:
        safe = att.sanitize_filename(upload.filename or "attachment")
        data = await upload.read()
        try:
            att.check_size(len(data), cfg)
            att.check_extension(safe, cfg)
        except att.AttachmentError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        if not data:
            raise HTTPException(status_code=400, detail=f"文件 {safe} 为空")
        mime = upload.content_type or mimetypes.guess_type(safe)[0] or "application/octet-stream"
        payloads.append((safe, data, mime))

    # 2) 落盘到题目工作区
    workspace = runtime.config.challenge_workspace(challenge_id)
    stored: list[dict[str, Any]] = []
    for safe, data, mime in payloads:
        path = att.unique_path(workspace, safe)
        path.write_bytes(data)
        stored.append(
            {
                "filename": path.name,
                "path": str(path.resolve()),
                "size": len(data),
                "mime": mime,
                "kind": att.kind_of(path.name),
            }
        )

    # 3) 登记到题目元数据（合并去重，保留已有附件）
    merged = list(dict.fromkeys([*existing, *[item["path"] for item in stored]]))
    await runtime.repos.challenges.upsert(
        {
            "id": challenge_id,
            "name": challenge.get("name") or challenge_id,
            "category": challenge.get("category") or "misc",
            "description": challenge.get("description") or "",
            "points": challenge.get("points") or 0,
            "attachments": merged,
            "flag_pattern": challenge.get("flagPattern") or "",
            "target": challenge.get("target") or "",
            "meta": challenge.get("meta") or {},
        }
    )
    logger.info("题目附件已上传 challenge=%s files=%d", challenge_id, len(stored))
    return {"ok": True, "attachments": stored, "challengeId": challenge_id}


class ChallengeProjectRequest(BaseModel):
    """设置题目归属项目（null / 空串表示取消归属）。"""

    project_id: str | None = Field(default=None, description="目标项目 ID；留空表示取消归属")


@router.post("/api/challenges/{challenge_id}/project", summary="设置题目归属项目")
async def set_challenge_project(
    challenge_id: str, payload: ChallengeProjectRequest, request: Request
) -> dict[str, Any]:
    """只改 ``meta.projectId``，不影响名称/描述/附件等字段。

    历史题目（创建时没有项目归属）可通过该接口补登记，随后即计入对应项目的看板统计。
    """
    runtime = get_runtime(request)
    project_id = (payload.project_id or "").strip() or None
    if project_id and await runtime.repos.projects.get(project_id) is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    challenge = await runtime.repos.challenges.set_project(challenge_id, project_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail=f"题目不存在：{challenge_id}")
    logger.info("题目归属已更新 challenge=%s project=%s", challenge_id, project_id or "(none)")
    return {"challenge": challenge, "projectId": project_id or ""}


@router.delete("/api/challenges/{challenge_id}", summary="删除题目（级联清理账本/Solver/flag，解绑会话）")
async def delete_challenge(
    challenge_id: str,
    request: Request,
    confirm: bool = Query(default=False, description="必须显式确认为 true"),
) -> dict[str, Any]:
    """删除题目：停止运行中会话 → 级联清理账本/Solver/flag/执行记录 → 删除题目工作区目录。

    绑定的会话只**解绑**不删除（回到未绑定态）。需要 ``?confirm=true``；
    运行中且停止失败时返回 409。
    """
    runtime = get_runtime(request)
    if not confirm:
        raise HTTPException(status_code=400, detail="删除题目需要显式确认：请带 ?confirm=true")
    try:
        info = await cascade_delete_challenge(
            challenge_id,
            repos=runtime.repos,
            config=runtime.config,
            runtime=runtime,
        )
    except DeleteConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if info["missing"]:
        raise HTTPException(status_code=404, detail=f"题目不存在：{challenge_id}")
    return {"ok": True, **info}


@router.post("/api/challenges/batch-delete", summary=f"批量删除题目（单批最多 {MAX_BATCH} 个）")
async def batch_delete_challenges_route(
    payload: ChallengeBatchDeleteRequest,
    request: Request,
) -> dict[str, Any]:
    """批量删除题目：逐条独立处理（部分失败不影响其它），逐条返回结果。"""
    runtime = get_runtime(request)
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="批量删除需要显式确认：请求体带上 confirm=true")
    try:
        return await batch_delete_challenges(
            payload.ids,
            repos=runtime.repos,
            config=runtime.config,
            runtime=runtime,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


__all__ = ["router"]


# ---------------------------------------------------------------------------
# M2：事实-意图图导出（yaml / timeline）
# ---------------------------------------------------------------------------
def _graph_payload(challenge: dict[str, Any], memories: list[dict[str, Any]],
                   ideas: list[dict[str, Any]], sources: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "challenge": {
            "id": challenge.get("id"),
            "name": challenge.get("name"),
            "category": challenge.get("category"),
            "target": challenge.get("target"),
            "goal": challenge.get("description") or "",
        },
        "memories": [
            {
                "id": m.get("id"),
                "kind": m.get("kind"),
                "summary": m.get("summary"),
                "from_idea": m.get("fromIdeaId"),
                "created_at": m.get("createdAt"),
            }
            for m in memories
        ],
        "ideas": [
            {
                "id": i.get("id"),
                "text": i.get("text"),
                "status": i.get("status"),
                "from": sources.get(str(i.get("id")), []),
                "claimed_by": i.get("claimedBy"),
                "created_at": i.get("createdAt"),
            }
            for i in ideas
        ],
    }


def _graph_timeline(payload: dict[str, Any]) -> str:
    ch = payload["challenge"]
    events: list[tuple[str, str]] = []
    events.append((
        "",
        f"CHALLENGE {ch['id']} ({ch.get('name') or ''})\n"
        f"  category: {ch.get('category')}\n  target: {ch.get('target')}\n  goal: {ch.get('goal')}",
    ))
    for m in payload["memories"]:
        events.append((
            str(m.get("created_at") or ""),
            f"MEMORY {m['id']} [{m.get('kind')}]\n  {m.get('summary')}"
            + (f"\n  from: {m['from_idea']}" if m.get("from_idea") else ""),
        ))
    for i in payload["ideas"]:
        frm = ", ".join(i.get("from") or []) or "(无来源)"
        events.append((
            str(i.get("created_at") or ""),
            f"IDEA {i['id']} [{i.get('status')}] from {frm}\n  {i.get('text')}",
        ))
    events.sort(key=lambda e: e[0])
    return "\n\n".join(block for _, block in events) + "\n"


@router.get("/api/challenges/{challenge_id}/graph", summary="导出事实-意图图（yaml / timeline）")
async def export_challenge_graph(
    challenge_id: str,
    request: Request,
    format: str = Query("yaml", description="yaml | timeline"),
) -> Response:
    """把挑战级黑板导出为可审计的图（M2）。

    yaml 适合机器处理与跨会话迁移；timeline 适合复盘与写 WP 时引用。
    """
    if format not in ("yaml", "timeline"):
        raise HTTPException(400, "format 只支持 yaml / timeline")
    runtime = get_runtime(request)
    challenge = await runtime.repos.challenges.get(challenge_id)
    if challenge is None:
        raise HTTPException(404, "题目不存在")
    memories = await runtime.repos.memories.list(challenge_id, limit=500)
    ideas = await runtime.repos.ideas.list(challenge_id, limit=500)
    sources = await runtime.repos.ideas.sources_of(challenge_id, [str(i.get("id")) for i in ideas])
    payload = _graph_payload(challenge, memories, ideas, sources)
    if format == "timeline":
        text = _graph_timeline(payload)
    else:
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    return Response(content=text, media_type="text/plain; charset=utf-8")
