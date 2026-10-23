"""项目（比赛）维度 API（U4 后端）。

纯增量：全部为新增路由，不修改任何既有接口。项目通过 ``conversations.project_id``
单列建立"0 或 1 个项目"的归属；会话本身的消息/附件/运行语义完全不变。

设计要点：

- 删除项目有两种模式：``detach``（默认，只清会话归属，不删会话）与 ``delete``
  （先解除归属，再对每个会话复用 U2b 的 :func:`cascade_delete_conversation` 级联删除）；
  两种模式都必须 ``?confirm=true``，绝不做静默数据丢失。
- ``delete`` 模式下运行中且取消失败的会话会被保留并逐条报告（``conflict=true``）。
- ``summary`` 与 ``report`` 全部基于真实 SQL 聚合，**不调用 LLM**。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..conversations_admin import DeleteConflict, cascade_delete_conversation
from .routes_agent import get_runtime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["projects"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class ProjectCreateRequest(BaseModel):
    """创建项目。"""

    name: str = Field(default="", description="项目（比赛）名称，必填且唯一")
    description: str = Field(default="", description="描述")
    status: str = Field(default="active", description="状态：active|paused|archived")
    meta: dict[str, Any] = Field(default_factory=dict, description="附加元数据")


class ProjectUpdateRequest(BaseModel):
    """更新项目（只改显式传入的字段）。"""

    name: str | None = Field(default=None, description="新名称（可选）")
    description: str | None = Field(default=None, description="新描述（可选）")
    status: str | None = Field(default=None, description="新状态（可选）")


class ProjectAssignRequest(BaseModel):
    """把会话归属到项目 / 从项目解除归属。"""

    conversationIds: list[str] = Field(default_factory=list, description="会话 ID 列表")
    action: Literal["assign", "unassign"] = Field(default="assign", description="assign|unassign")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("/api/projects", summary="列出项目（含会话数）")
async def list_projects(request: Request, limit: int = 100, status: str | None = None) -> dict[str, Any]:
    runtime = get_runtime(request)
    projects = await runtime.repos.projects.list(limit=limit, status=status)
    return {"projects": projects}


@router.post("/api/projects", summary="创建项目（空名 422 / 重名 409）")
async def create_project(payload: ProjectCreateRequest, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name 不能为空")
    if await runtime.repos.projects.get_by_name(name) is not None:
        raise HTTPException(status_code=409, detail=f"项目名称已存在：{name}")
    project = await runtime.repos.projects.create(
        name=name,
        description=payload.description,
        status=payload.status or "active",
        meta=payload.meta,
    )
    logger.info("已创建项目 %s（%s）", project["id"], name)
    return {"project": project}


@router.get("/api/projects/{project_id}", summary="项目详情")
async def get_project(project_id: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    project = await runtime.repos.projects.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    return {"project": project}


@router.put("/api/projects/{project_id}", summary="更新项目（重名 409）")
async def update_project(project_id: str, payload: ProjectUpdateRequest, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    existing = await runtime.repos.projects.get(project_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")

    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="name 不能为空")
        duplicate = await runtime.repos.projects.get_by_name(name)
        if duplicate is not None and duplicate["id"] != project_id:
            raise HTTPException(status_code=409, detail=f"项目名称已存在：{name}")
        fields["name"] = name

    updated = await runtime.repos.projects.update(project_id, **fields)
    if updated is None:  # pragma: no cover - 已在上方判空
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    logger.info("已更新项目 %s：%s", project_id, sorted(fields))
    return {"project": updated}


@router.delete("/api/projects/{project_id}", summary="删除项目（detach 保留会话 / delete 级联删除）")
async def delete_project(
    project_id: str,
    request: Request,
    mode: Literal["detach", "delete"] = Query(
        default="detach", description="detach=只解除归属（默认）；delete=同时级联删除会话"
    ),
    confirm: bool = Query(default=False, description="必须显式确认为 true，避免误删"),
) -> dict[str, Any]:
    """删除项目。缺 ``confirm`` → 400（防静默数据丢失）。"""
    if not confirm:
        raise HTTPException(status_code=400, detail="删除项目需要显式确认：请带 ?confirm=true")
    runtime = get_runtime(request)
    project = await runtime.repos.projects.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")

    conversation_ids = await runtime.repos.projects.conversation_ids(project_id)
    detached = await runtime.repos.conversations.clear_project(project_id)

    results: list[dict[str, Any]] = []
    if mode == "delete":
        for conversation_id in conversation_ids:
            try:
                info = await cascade_delete_conversation(
                    conversation_id,
                    repos=runtime.repos,
                    config=runtime.config,
                    runtime=runtime,
                )
                results.append({"id": conversation_id, "ok": not info["missing"], **info})
            except DeleteConflict as exc:
                logger.warning("项目 %s 删除会话冲突：%s", project_id, conversation_id)
                results.append({"id": conversation_id, "ok": False, "error": str(exc), "conflict": True})
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                logger.exception("项目 %s 级联删除会话失败 id=%s", project_id, conversation_id)
                results.append({"id": conversation_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    await runtime.repos.projects.delete(project_id)
    deleted = sum(1 for r in results if r["ok"])
    logger.info(
        "已删除项目 %s（mode=%s，解除归属 %d 个会话，删除 %d 个会话）",
        project_id,
        mode,
        detached,
        deleted,
    )
    return {
        "ok": True,
        "mode": mode,
        "projectId": project_id,
        "detached": detached,
        "deleted": deleted,
        "failed": len(results) - deleted,
        "results": results,
        "projectDeleted": True,
    }


@router.post("/api/projects/{project_id}/assign", summary="分配/解除会话归属（0 或 1 个项目）")
async def assign_conversations(
    project_id: str, payload: ProjectAssignRequest, request: Request
) -> dict[str, Any]:
    runtime = get_runtime(request)
    project = await runtime.repos.projects.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    if not payload.conversationIds:
        raise HTTPException(status_code=400, detail="conversationIds 不能为空")

    action = payload.action
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for conversation_id in payload.conversationIds:
        cid = str(conversation_id or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        if action == "assign":
            conversation = await runtime.repos.conversations.set_project(cid, project_id)
        else:
            conversation = await runtime.repos.conversations.set_project(cid, None)
        if conversation is None:
            results.append({"id": cid, "ok": False, "error": "会话不存在"})
            continue
        results.append({"id": cid, "ok": True, "projectId": conversation.get("projectId")})

    ok_count = sum(1 for r in results if r["ok"])
    assigned = ok_count if action == "assign" else 0
    unassigned = ok_count if action == "unassign" else 0
    logger.info("项目 %s %s 会话：成功 %d / %d", project_id, action, ok_count, len(results))
    return {
        "ok": all(r["ok"] for r in results),
        "projectId": project_id,
        "action": action,
        "assigned": assigned,
        "unassigned": unassigned,
        "results": results,
    }


@router.get("/api/projects/{project_id}/summary", summary="项目黑板聚合（跨全部会话，只读）")
async def project_summary(project_id: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    summary = await runtime.repos.projects.summary(project_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    return summary


@router.post("/api/projects/{project_id}/report", summary="生成项目级 Markdown 汇总报告（不调用 LLM）")
async def project_report(project_id: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    summary = await runtime.repos.projects.summary(project_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")

    markdown = render_project_report(summary)
    # 秒级时间戳 + 毫秒后缀，避免同一秒内连续生成互相覆盖
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"
    safe_id = re.sub(r"[^A-Za-z0-9_.\-]", "_", project_id)
    target = runtime.config.outputs_dir() / f"project-{safe_id}-{stamp}.md"
    target.write_text(markdown, encoding="utf-8")
    logger.info("已生成项目报告 %s（%d 字节）", target, len(markdown.encode("utf-8")))
    return {
        "ok": True,
        "path": str(target),
        "relativePath": f"_outputs/{target.name}",
        "bytes": len(markdown.encode("utf-8")),
        "project": {"id": summary["project"]["id"], "name": summary["project"]["name"]},
    }


@router.get("/api/projects/{project_id}/files", summary="列出该项目已生成报告文件")
async def project_files(project_id: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    project = await runtime.repos.projects.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    safe_id = re.sub(r"[^A-Za-z0-9_.\-]", "_", project_id)
    outputs = runtime.config.outputs_dir()
    files: list[dict[str, Any]] = []
    for path in sorted(outputs.glob(f"project-{safe_id}-*.md")):
        try:
            stat = path.stat()
        except OSError:  # pragma: no cover - 极少数竞态
            continue
        files.append(
            {
                "name": path.name,
                "path": str(path),
                "relativePath": f"_outputs/{path.name}",
                "bytes": stat.st_size,
                "modifiedAt": int(stat.st_mtime * 1000),
            }
        )
    return {"projectId": project_id, "files": files}


# ---------------------------------------------------------------------------
# 报告渲染（纯聚合）
# ---------------------------------------------------------------------------
def render_project_report(summary: dict[str, Any]) -> str:
    """把 :meth:`ProjectRepo.summary` 的聚合结果渲染为 Markdown（纯函数，无 LLM）。"""
    project = summary["project"]
    conversations = summary["conversations"]
    flags = summary["flags"]
    challenges = summary["challenges"]
    facts = summary["facts"]
    attachments = summary["attachments"]
    stats = summary["stats"]

    lines: list[str] = [f"# 项目汇总报告：{project.get('name') or project.get('id')}", ""]
    lines.append("> 由 CTF-Agent 自动生成 · 纯聚合（未调用 LLM）· 仅用于 CTF 竞赛与授权靶场")
    lines.append("")

    lines += ["## 项目信息", ""]
    lines += [
        f"- 项目 ID：`{project.get('id')}`",
        f"- 名称：{project.get('name')}",
        f"- 状态：{project.get('status')}",
        f"- 描述：{project.get('description') or '(无)'}",
        f"- 创建时间：{project.get('createdAt') or '-'}",
        f"- 更新时间：{project.get('updatedAt') or '-'}",
        "",
    ]

    lines += ["## 会话列表", ""]
    if conversations:
        for row in conversations:
            title = row.get("title") or "(未命名)"
            lines.append(
                f"- `{row.get('id')}` {title}（消息 {row.get('messageCount', 0)} 条，"
                f"flag {row.get('flagCount', 0)} 个，challenge={row.get('challengeId') or '-'}）"
            )
    else:
        lines.append("- （该项目下暂无会话）")
    lines.append("")

    lines += ["## flag 列表", ""]
    if flags:
        for row in flags:
            lines.append(f"- `{row.get('value')}`（会话 `{row.get('conversationId') or '-'}`）")
    else:
        lines.append("- （暂无 flag 记录）")
    lines.append("")

    lines += ["## 挑战列表", ""]
    if challenges:
        for row in challenges:
            lines.append(
                f"- `{row.get('challengeId')}` {row.get('title')}（状态 {row.get('status') or '-'}，"
                f"会话 {row.get('conversationCount', 0)} 个）"
            )
    else:
        lines.append("- （暂无关联题目）")
    lines.append("")

    lines += ["## 黑板事实（按 key 分组）", ""]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        grouped.setdefault(str(fact.get("key") or ""), []).append(fact)
    if grouped:
        for key, entries in grouped.items():
            lines.append(f"### {key}")
            lines.append("")
            for entry in entries:
                lines.append(
                    f"- {entry.get('summary') or '(无 summary)'}"
                    f"（会话 `{entry.get('conversationId') or '-'}`）"
                )
            lines.append("")
    else:
        lines += ["- （黑板为空）", ""]

    lines += ["## 附件清单", ""]
    if attachments:
        for row in attachments:
            lines.append(
                f"- `{row.get('filename')}`（{row.get('size', 0)} 字节，"
                f"kind={row.get('kind') or '-'}，会话 `{row.get('conversationId') or '-'}`）"
            )
    else:
        lines.append("- （暂无附件）")
    lines.append("")

    lines += ["## 统计", ""]
    lines += [
        f"- 会话数：{stats.get('conversationCount', 0)}",
        f"- 消息数：{stats.get('messageCount', 0)}",
        f"- flag 数：{stats.get('flagCount', 0)}",
        f"- 黑板事实数：{stats.get('factCount', 0)}",
        f"- 附件数：{stats.get('attachmentCount', 0)}",
        f"- 挑战数：{stats.get('challengeCount', 0)}",
        "",
    ]
    return "\n".join(lines)


__all__ = ["render_project_report", "router"]
