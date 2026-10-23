"""题目删除：单条 + 批量，带运行中保护与事务级联（对齐 U2b 会话删除）。

- 运行中保护：删除前先请求停止该题目的挑战会话，并等待其真正归档（默认 8s）；
  停不下来则**拒绝删除**并返回明确原因，绝不"边跑边删"。
- 级联清理：所有带 ``challenge_id`` 列的表（动态发现，不写死表名）在一个事务里删除；
  ``conversations`` 表例外——**只解绑不删除**（会话是用户的独立资产，题目删除后回到未绑定态）；
  最后清理磁盘目录 ``workspace/challenge/{id}/``。
- 批量删除：单批上限 100，逐条返回结果（部分失败不影响其它）。

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

#: 批量删除单批上限（与会话批量删除一致）
MAX_BATCH = 100
#: 停止挑战会话后的等待上限（秒）
STOP_TIMEOUT = 8.0


class DeleteConflict(RuntimeError):
    """挑战会话仍在运行且无法停止，拒绝删除。"""


async def stop_challenge_session(
    runtime: Any, challenge_id: str, *, timeout: float = STOP_TIMEOUT
) -> bool:
    """停止该题目的运行中挑战会话并等待其归档；返回是否发生了停止动作。

    超时仍未停止时抛 :class:`DeleteConflict`（调用方转 409）。
    """
    session = runtime.challenge_sessions.get(challenge_id) if hasattr(runtime, "challenge_sessions") else None
    if session is None or not getattr(session, "running", False):
        return False
    runtime.stop_challenge_run(challenge_id, reason="challenge_delete")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = runtime.challenge_sessions.get(challenge_id)
        if session is None or not getattr(session, "running", False):
            logger.info("挑战会话 %s 已停止", challenge_id)
            return True
        await asyncio.sleep(0.1)
    raise DeleteConflict(
        f"题目 {challenge_id} 的挑战会话仍在运行，已请求停止但未在 {timeout:.0f}s 内归档，"
        "为安全起见未删除；请稍后在控制台点「停止」并等待各 Solver 收尾后重试。"
    )


async def cascade_delete_challenge(
    challenge_id: str,
    *,
    repos: Repos,
    config: AppConfig,
    runtime: Any | None = None,
) -> dict[str, Any]:
    """删除题目及其账本/Solver/flag 等关联数据；绑定它的会话只解绑不删除。

    返回 ``{challengeId, missing, stopped, unboundConversations, deletedRows, removedDir}``。
    """
    existing = await repos.challenges.get(challenge_id)
    if existing is None:
        return {
            "challengeId": challenge_id,
            "missing": True,
            "stopped": False,
            "unboundConversations": 0,
            "deletedRows": 0,
            "removedDir": False,
        }

    stopped = False
    if runtime is not None:
        stopped = await stop_challenge_session(runtime, challenge_id)

    statements: list[tuple[str, tuple[Any, ...]]] = []
    tables = await _challenge_child_tables(repos.db)
    for table in tables:
        if table == "conversations":
            # 会话是独立资产：只解绑，不级联删除
            statements.append(
                ("UPDATE conversations SET challenge_id=NULL WHERE challenge_id=?", (challenge_id,))
            )
        else:
            statements.append((f"DELETE FROM {table} WHERE challenge_id=?", (challenge_id,)))
    statements.append(("DELETE FROM challenges WHERE id=?", (challenge_id,)))
    unbound = _count_unbound(statements)
    await repos.db.execute_transaction(statements)

    removed_dir = _remove_challenge_dir(config, challenge_id)
    logger.info(
        "题目已删除 id=%s 表=%s 解绑会话=%d 目录删除=%s 停止会话=%s",
        challenge_id,
        tables,
        unbound,
        removed_dir,
        stopped,
    )
    return {
        "challengeId": challenge_id,
        "missing": False,
        "stopped": stopped,
        "unboundConversations": unbound,
        "deletedRows": len(statements) - 1,  # 不含题目行本身
        "removedDir": removed_dir,
    }


async def batch_delete_challenges(
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
        raise ValueError(f"单批最多删除 {MAX_BATCH} 个题目（收到 {len(ids)} 个）")
    seen: set[str] = set()
    unique: list[str] = []
    for challenge_id in ids:
        if not challenge_id or challenge_id in seen:
            continue
        seen.add(challenge_id)
        unique.append(challenge_id)
    results: list[dict[str, Any]] = []
    for challenge_id in unique:
        try:
            info = await cascade_delete_challenge(
                challenge_id, repos=repos, config=config, runtime=runtime
            )
            results.append({"id": challenge_id, "ok": not info["missing"], **info})
        except DeleteConflict as exc:
            results.append({"id": challenge_id, "ok": False, "error": str(exc), "conflict": True})
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            logger.exception("批量删除题目失败 id=%s", challenge_id)
            results.append({"id": challenge_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    deleted = sum(1 for r in results if r["ok"])
    return {
        "ok": all(r["ok"] for r in results),
        "deleted": deleted,
        "failed": len(results) - deleted,
        "results": results,
    }


async def _challenge_child_tables(db: Database) -> list[str]:
    """返回所有带 ``challenge_id`` 列的表名（动态发现，覆盖 ideas/memories/flags 等）。"""
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    tables: list[str] = []
    for row in rows:
        name = str(row.get("name") or "")
        if not name or name.startswith("sqlite_"):
            continue
        cols = await db.fetchall(f"PRAGMA table_info({name})")
        if any(str(c.get("name")) == "challenge_id" for c in cols):
            tables.append(name)
    return tables


def _count_unbound(statements: list[tuple[str, tuple[Any, ...]]]) -> int:
    """统计事务里的解绑语句数量（用于返回值展示）。"""
    return sum(1 for sql, _ in statements if sql.startswith("UPDATE conversations"))


def _remove_challenge_dir(config: AppConfig, challenge_id: str) -> bool:
    """删除题目工作区目录（含迁移进去的附件与解题产物）。"""
    directory = Path(config.challenge_workspace(challenge_id))
    try:
        if directory.is_dir():
            shutil.rmtree(directory)
            return True
    except OSError:  # noqa: BLE001 - 目录删不掉也不能回滚元数据删除
        logger.warning("题目工作区删除失败：%s", directory, exc_info=True)
    return False


__all__ = [
    "MAX_BATCH",
    "STOP_TIMEOUT",
    "DeleteConflict",
    "batch_delete_challenges",
    "cascade_delete_challenge",
    "stop_challenge_session",
]
