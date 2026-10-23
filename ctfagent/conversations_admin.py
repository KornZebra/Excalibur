"""对话删除（U2b）：单条 + 批量，带运行中保护与事务级联。

- 运行中保护：删除前先取消该会话正在跑的任务，并等待其真正停下（默认 8s）；
  停不下来则**拒绝删除**并返回明确原因，绝不"边跑边删"。
- 级联清理：所有带 ``conversation_id`` 列的表（动态发现，不写死表名）在一个事务里删除，
  再删会话行，最后清理磁盘目录 ``workspace/conversations/{id}/``（含附件）。
- 批量删除：单批上限 100，逐条返回结果（部分失败不影响其它），供前端展示。

两种删除都需要显式确认：接口层要求 ``?confirm=true`` 或请求体 ``{"confirm": true}``。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from .config import AppConfig
from .storage.db import Database
from .storage.repos import Repos

logger = logging.getLogger(__name__)

#: 批量删除单批上限（提示词要求）
MAX_BATCH = 100
#: 取消运行后的等待上限（秒）
CANCEL_TIMEOUT = 8.0


class DeleteConflict(RuntimeError):
    """会话正在运行且无法取消，拒绝删除。"""


async def stop_conversation_runs(
    runtime: Any, conversation_id: str, *, timeout: float = CANCEL_TIMEOUT
) -> list[str]:
    """取消该会话的运行中任务并等待其停止；返回被取消的 run_id 列表。

    超时仍未停止时抛 :class:`DeleteConflict`（调用方转 409）。
    """
    cancelled: list[str] = []
    for run in runtime.active_runs():
        if run.get("conversationId") != conversation_id or run.get("status") != "running":
            continue
        run_id = str(run.get("runId") or "")
        if run_id and runtime.cancel_run(run_id):
            cancelled.append(run_id)
    if not cancelled:
        return []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        still = [
            r
            for r in runtime.active_runs()
            if r.get("conversationId") == conversation_id and r.get("status") == "running"
        ]
        if not still:
            logger.info("会话 %s 的运行已取消：%s", conversation_id, cancelled)
            return cancelled
        await asyncio.sleep(0.1)
    raise DeleteConflict(
        f"会话 {conversation_id} 仍有运行中的任务（{cancelled}），已请求取消但未在 {timeout:.0f}s 内停止，"
        "为安全起见未删除；请稍后重试或在界面上点「停止」。"
    )


async def cascade_delete_conversation(
    conversation_id: str,
    *,
    repos: Repos,
    config: AppConfig,
    runtime: Any | None = None,
) -> dict[str, Any]:
    """删除会话及其全部关联数据与磁盘目录（U2b 核心）。

    返回 ``{conversationId, cancelledRuns, deletedRows, deletedAttachments, removedDir, missing}``。
    """
    existing = await repos.conversations.get(conversation_id)
    if existing is None:
        return {
            "conversationId": conversation_id,
            "missing": True,
            "cancelledRuns": [],
            "deletedRows": 0,
            "deletedAttachments": 0,
            "removedDir": False,
        }

    cancelled: list[str] = []
    if runtime is not None:
        cancelled = await stop_conversation_runs(runtime, conversation_id)

    attachments = await repos.attachments.list(conversation_id)
    tables = await repos.db.conversation_child_tables()
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (f"DELETE FROM {table} WHERE conversation_id=?", (conversation_id,))
        for table in tables
        if table != "conversations"
    ]
    statements.append(("DELETE FROM conversations WHERE id=?", (conversation_id,)))
    await repos.db.execute_transaction(statements)

    removed_dir = _remove_conversation_dir(config, conversation_id, attachments)
    logger.info(
        "会话已删除 id=%s 表=%s 附件=%d 目录删除=%s 取消运行=%s",
        conversation_id,
        tables,
        len(attachments),
        removed_dir,
        cancelled,
    )
    return {
        "conversationId": conversation_id,
        "missing": False,
        "cancelledRuns": cancelled,
        "deletedRows": len(statements) - 1,  # 不含会话行本身
        "deletedAttachments": len(attachments),
        "removedDir": removed_dir,
    }


def _remove_conversation_dir(
    config: AppConfig, conversation_id: str, attachments: list[dict[str, Any]]
) -> bool:
    """删除会话目录（含附件文件）。目录不存在时退化为逐个删附件文件。"""
    removed = False
    directory = config.conversation_dir(conversation_id)
    try:
        if directory.is_dir():
            shutil.rmtree(directory)
            removed = True
    except OSError:  # noqa: BLE001 - 目录删不掉也不能回滚元数据删除
        logger.warning("会话目录删除失败：%s", directory, exc_info=True)
    for row in attachments:
        path = Path(row.get("storedPath") or "")
        try:
            if path.is_file():
                path.unlink()
                removed = True
        except OSError:  # noqa: BLE001
            logger.warning("附件文件删除失败：%s", path, exc_info=True)
    return removed


async def batch_delete_conversations(
    ids: list[str],
    *,
    repos: Repos,
    config: AppConfig,
    runtime: Any | None = None,
    db: Database | None = None,
) -> dict[str, Any]:
    """批量删除（上限 :data:`MAX_BATCH`）：逐条独立处理，返回每条结果。"""
    if not ids:
        raise ValueError("ids 不能为空")
    if len(ids) > MAX_BATCH:
        raise ValueError(f"单批最多删除 {MAX_BATCH} 个会话（收到 {len(ids)} 个）")
    seen: set[str] = set()
    unique: list[str] = []
    for conversation_id in ids:
        if not conversation_id or conversation_id in seen:
            continue
        seen.add(conversation_id)
        unique.append(conversation_id)
    results: list[dict[str, Any]] = []
    for conversation_id in unique:
        try:
            info = await cascade_delete_conversation(
                conversation_id, repos=repos, config=config, runtime=runtime
            )
            results.append({"id": conversation_id, "ok": not info["missing"], **info})
        except DeleteConflict as exc:
            results.append({"id": conversation_id, "ok": False, "error": str(exc), "conflict": True})
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            logger.exception("批量删除失败 id=%s", conversation_id)
            results.append({"id": conversation_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    deleted = sum(1 for r in results if r["ok"])
    return {
        "ok": all(r["ok"] for r in results),
        "deleted": deleted,
        "failed": len(results) - deleted,
        "results": results,
    }


__all__ = [
    "CANCEL_TIMEOUT",
    "MAX_BATCH",
    "DeleteConflict",
    "batch_delete_conversations",
    "cascade_delete_conversation",
    "stop_conversation_runs",
]
