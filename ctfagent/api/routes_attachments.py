"""对话附件路由（U2a）。

- ``POST   /api/conversations/{id}/attachments``   上传（multipart，可多文件）
- ``GET    /api/conversations/{id}/attachments``   列出会话附件（按消息分组由前端完成）
- ``GET    /api/attachments/{id}/download``        下载（强制 attachment + nosniff）
- ``DELETE /api/attachments/{id}``                 删除（同时删除磁盘文件）

校验顺序：开关 → 数量 → 体积 → 扩展名白名单 → 文件名消毒；**先全部校验再落盘**，
避免批量上传时出现"一半成功一半失败"的中间态。
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import attachments as att
from ..config import AppConfig
from ..storage.repos import Repos

logger = logging.getLogger(__name__)
router = APIRouter(tags=["attachments"])


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_repos(request: Request) -> Repos:
    """附件统一走运行时仓储（与其它数据路由一致）。"""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="运行时尚未就绪")
    return runtime.repos


class AttachmentItem(BaseModel):
    id: str
    conversationId: str
    messageId: str | None = None
    filename: str
    storedPath: str = ""
    size: int
    mime: str = ""
    sha256: str = ""
    kind: str = ""
    createdAt: str | None = None
    url: str = ""
    mimeWarning: str = ""


class UploadResponse(BaseModel):
    ok: bool
    attachments: list[AttachmentItem]
    rejected: list[dict[str, Any]] = []


class DeleteResponse(BaseModel):
    ok: bool
    id: str
    removedFile: bool = False


def _item(row: dict[str, Any]) -> AttachmentItem:
    return AttachmentItem(
        id=row["id"],
        conversationId=row.get("conversationId") or row.get("conversation_id") or "",
        messageId=row.get("messageId") or row.get("message_id"),
        filename=row.get("filename") or "",
        storedPath=row.get("storedPath") or row.get("stored_path") or "",
        size=int(row.get("size") or 0),
        mime=row.get("mime") or "",
        sha256=row.get("sha256") or "",
        kind=row.get("kind") or "",
        createdAt=row.get("createdAt") or row.get("created_at"),
        url=f"/api/attachments/{row['id']}/download",
        mimeWarning=att.mime_warning(row.get("filename") or "", row.get("mime") or ""),
    )


@router.post(
    "/api/conversations/{conversation_id}/attachments",
    response_model=UploadResponse,
    summary="上传会话附件",
)
async def upload_attachments(
    conversation_id: str,
    request: Request,
    files: list[UploadFile] = File(..., description="一个或多个文件"),
) -> UploadResponse:
    config = get_config(request)
    repos = get_repos(request)
    cfg = config.attachment
    if not cfg.enabled:
        raise HTTPException(status_code=400, detail="附件上传未启用（attachment.enabled=false）")
    if not files:
        raise HTTPException(status_code=400, detail="未收到文件")
    conversation = await repos.conversations.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail=f"未知会话：{conversation_id}")

    existing = await repos.attachments.count(conversation_id)
    try:
        att.check_count(existing, len(files), cfg)
    except att.AttachmentError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    # 1) 先全部读入 + 校验（不落盘），任何一项失败整批拒绝
    payloads: list[tuple[str, bytes, str, str]] = []  # (消毒名, 内容, mime, 原文件名)
    for upload in files:
        raw_name = upload.filename or "attachment"
        safe = att.sanitize_filename(raw_name)
        data = await upload.read()
        try:
            att.check_size(len(data), cfg)
            att.check_extension(safe, cfg)
        except att.AttachmentError as exc:
            exc.filename = exc.filename or safe
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        if not data:
            raise HTTPException(status_code=400, detail=f"文件 {safe} 为空")
        mime = upload.content_type or mimetypes.guess_type(safe)[0] or "application/octet-stream"
        payloads.append((safe, data, mime, raw_name))

    # 2) 落盘 + 写元数据
    directory = config.conversation_workspace(conversation_id)
    stored: list[AttachmentItem] = []
    for safe, data, mime, _raw in payloads:
        path = att.unique_path(directory, safe)
        path.write_bytes(data)
        try:
            row = await repos.attachments.create(
                conversation_id=conversation_id,
                filename=path.name,
                stored_path=str(path.resolve()),
                size=len(data),
                mime=mime,
                sha256=att.sha256_of(data),
                kind=att.kind_of(path.name),
            )
        except Exception:
            path.unlink(missing_ok=True)  # 元数据失败则不留孤儿文件
            raise
        stored.append(_item(row))
        logger.info(
            "附件已上传 conversation=%s file=%s size=%d kind=%s",
            conversation_id,
            path.name,
            len(data),
            att.kind_of(path.name),
        )
    return UploadResponse(ok=True, attachments=stored)


@router.get("/api/conversations/{conversation_id}/attachments", summary="列出会话附件")
async def list_attachments(conversation_id: str, request: Request) -> dict[str, Any]:
    repos = get_repos(request)
    rows = await repos.attachments.list(conversation_id)
    return {"attachments": [_item(r) for r in rows]}


@router.get("/api/attachments/{attachment_id}/download", summary="下载附件")
async def download_attachment(attachment_id: str, request: Request) -> Any:
    repos = get_repos(request)
    row = await repos.attachments.get(attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未知附件：{attachment_id}")
    path = Path(row.get("storedPath") or "")
    if not path.is_file():
        raise HTTPException(status_code=410, detail=f"附件文件已丢失：{row.get('filename')}")
    # 一律以附件形式下发，且禁止 MIME 嗅探：避免上传的 HTML/SVG 在本站 origin 内被渲染
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=row.get("filename") or path.name,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=60"},
    )


@router.delete("/api/attachments/{attachment_id}", response_model=DeleteResponse, summary="删除附件")
async def delete_attachment(attachment_id: str, request: Request) -> DeleteResponse:
    repos = get_repos(request)
    row = await repos.attachments.delete(attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未知附件：{attachment_id}")
    removed = False
    path = Path(row.get("storedPath") or "")
    try:
        if path.is_file():
            path.unlink()
            removed = True
    except OSError:  # noqa: BLE001 - 文件删不掉也不该阻塞元数据删除
        logger.warning("附件文件删除失败：%s", path, exc_info=True)
    return DeleteResponse(ok=True, id=attachment_id, removedFile=removed)


__all__ = ["get_config", "get_repos", "router"]
