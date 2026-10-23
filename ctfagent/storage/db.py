"""SQLite schema 与迁移（stdlib sqlite3 + asyncio.to_thread，开启 WAL）。

数据库文件默认 ``data/ctfagent.db``。所有访问都经过 :class:`Database`，
内部用单个连接 + ``asyncio.Lock`` 串行化，避免多协程并发写导致的 ``database is locked``。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: SQL 迁移脚本目录（v2 起迁移以独立 .sql 文件形式落盘，便于评审与回滚）
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

SCHEMA_VERSION = 10


def load_migration_sql(version: int, *, rollback: bool = False) -> str:
    """读取 ``migrations/`` 下的迁移脚本。

    ``rollback=True`` 时读取 ``00X_xxx_rollback.sql``。文件缺失时抛出明确错误
    （而不是静默跳过迁移——静默跳过会让运行期出现"表不存在"的诡异故障）。
    """
    candidates = sorted(MIGRATIONS_DIR.glob(f"{version:03d}_*.sql"))
    if rollback:
        candidates = [p for p in candidates if p.stem.endswith("_rollback")]
    else:
        candidates = [p for p in candidates if not p.stem.endswith("_rollback")]
    if not candidates:
        raise FileNotFoundError(
            f"缺少迁移 {version:03d} 的 SQL 脚本（{'回滚' if rollback else '升级'}），"
            f"请检查 {MIGRATIONS_DIR} 是否完整（pip 安装时需 package-data 包含 migrations/*.sql）"
        )
    return candidates[0].read_text(encoding="utf-8")


MIGRATIONS: dict[int, str] = {
    1: """
-- 会话
CREATE TABLE IF NOT EXISTS conversations (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    challenge_id  TEXT,
    agent_id      TEXT NOT NULL DEFAULT 'ctf-agent',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC);

-- 消息
CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    tool_calls      TEXT NOT NULL DEFAULT '[]',
    tool_call_id    TEXT,
    name            TEXT,
    reasoning       TEXT,
    created_at      REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_conv_seq ON messages(conversation_id, seq);

-- 工具执行记录
CREATE TABLE IF NOT EXISTS tool_executions (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT,
    challenge_id    TEXT,
    run_id          TEXT,
    agent_id        TEXT NOT NULL DEFAULT 'ctf-agent',
    tool_name       TEXT NOT NULL,
    arguments       TEXT NOT NULL DEFAULT '{}',
    command         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'queued',
    exit_code       INTEGER,
    output          TEXT NOT NULL DEFAULT '',
    error           TEXT,
    persisted_file  TEXT,
    is_error        INTEGER NOT NULL DEFAULT 0,
    started_at      REAL NOT NULL,
    finished_at     REAL,
    duration_ms     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_exec_conv ON tool_executions(conversation_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_exec_run ON tool_executions(run_id);

-- 黑板记忆（项目/会话级 facts）
CREATE TABLE IF NOT EXISTS facts (
    conversation_id TEXT NOT NULL,
    key             TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (conversation_id, key)
);
CREATE INDEX IF NOT EXISTS idx_facts_updated ON facts(conversation_id, updated_at DESC);

-- 题目元数据
CREATE TABLE IF NOT EXISTS challenges (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT 'misc',
    description   TEXT NOT NULL DEFAULT '',
    points        INTEGER NOT NULL DEFAULT 0,
    attachments   TEXT NOT NULL DEFAULT '[]',
    flag_pattern  TEXT NOT NULL DEFAULT '',
    target        TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    meta          TEXT NOT NULL DEFAULT '{}'
);

-- flag 记录
CREATE TABLE IF NOT EXISTS flags (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT,
    challenge_id    TEXT,
    value           TEXT NOT NULL,
    format          TEXT NOT NULL DEFAULT '',
    validated       INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT '',
    submitted_at    REAL NOT NULL,
    meta            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_flags_conv ON flags(conversation_id, submitted_at DESC);

-- Agent 轨迹（JSONL 文件索引）
CREATE TABLE IF NOT EXISTS agent_traces (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    conversation_id TEXT,
    agent_id        TEXT NOT NULL DEFAULT 'ctf-agent',
    trace_path      TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    meta            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_traces_run ON agent_traces(run_id);

-- 运行记录（用于续跑与前端恢复）
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    agent_id        TEXT NOT NULL DEFAULT 'ctf-agent',
    orchestration   TEXT NOT NULL DEFAULT 'single',
    status          TEXT NOT NULL DEFAULT 'running',
    iterations      INTEGER NOT NULL DEFAULT 0,
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    model_calls     INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      REAL NOT NULL,
    finished_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_conv ON runs(conversation_id, started_at DESC);

-- 挂起的 HITL 审批（进程重启后可查询历史）
CREATE TABLE IF NOT EXISTS hitl_requests (
    id              TEXT PRIMARY KEY,
    run_id          TEXT,
    conversation_id TEXT,
    tool_call_id    TEXT,
    tool_name       TEXT NOT NULL,
    arguments       TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'pending',
    reason          TEXT,
    requested_at    REAL NOT NULL,
    resolved_at     REAL
);
""",
    # v2：blackboard v2（挑战级账本）。SQL 落在 migrations/ 下，便于评审与回滚。
    2: load_migration_sql(2),
    # v3：U2a 对话附件元数据（纯增量：只新增表/索引）
    3: load_migration_sql(3),
    # v4：U4 项目（比赛）维度（纯增量：新增 projects 表 + conversations.project_id 可空列）
    4: load_migration_sql(4),
    # v5：U6a 多模式审查（新增 hitl_decisions 表 + conversations.hitl_reviewer 可空列）
    5: load_migration_sql(5),
    # v6：运行中引导（Steering）—— messages.delivery_status 投递状态（纯增量列 + 索引）
    6: load_migration_sql(6),
    # v7：黑板按题目隔离 —— facts 引入 scope 主键（challenge:<id> / conversation:<cid>）
    7: load_migration_sql(7),
    # v8：作战图 —— 题目 × 战法 的显式编排快照（solve_boards）
    8: load_migration_sql(8),
    # v9：M1 租约化（solver_runs 心跳）+ M2 因果边（idea_sources / memories.from_idea_id）
    9: load_migration_sql(9),
    # v10：竞赛自动化模式（竞赛级全局黑板 / 能力注册 / 题目池 / 提交 / 策略状态）
    10: load_migration_sql(10),
}

#: 各版本的回滚脚本（供 ctfagent.storage.migrate down 使用）
ROLLBACK_MIGRATIONS: dict[int, str] = {
    2: load_migration_sql(2, rollback=True),
    3: load_migration_sql(3, rollback=True),
    4: load_migration_sql(4, rollback=True),
    5: load_migration_sql(5, rollback=True),
    6: load_migration_sql(6, rollback=True),
    7: load_migration_sql(7, rollback=True),
    8: load_migration_sql(8, rollback=True),
    9: load_migration_sql(9, rollback=True),
    10: load_migration_sql(10, rollback=True),
}


class Database:
    """异步 SQLite 封装（单连接 + 锁 + 线程池）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._closed

    def _require(self) -> sqlite3.Connection:
        """取出当前连接；关闭后给出可读错误而不是让 C 层崩溃。"""
        conn = self._conn
        if conn is None or self._closed:
            raise RuntimeError("数据库已关闭，无法继续读写（请检查是否在 Runtime.close() 之后还有后台任务）")
        return conn

    async def connect(self) -> None:
        if self._conn is not None and not self._closed:
            return
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await asyncio.to_thread(self._open)
        self._conn = conn
        await self._migrate()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    async def close(self) -> None:
        """关闭连接。

        关键：必须与读写操作互斥（同一把锁）。否则一条正在工作线程里执行的
        ``conn.execute`` 会被另一线程的 ``conn.close`` 释放底层句柄，导致解释器段错误。
        """
        if self._conn is None:
            return
        async with self._lock:
            conn, self._conn = self._conn, None
            self._closed = True
            if conn is not None:
                await asyncio.to_thread(conn.close)

    # ------------------------------------------------------------------
    async def _migrate(self) -> None:
        assert self._conn is not None
        await self._exec_script(
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = await self.fetchone("SELECT value FROM schema_meta WHERE key='version'")
        current = int(row["value"]) if row else 0
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            logger.info("应用数据库迁移 v%s", version)
            await self._exec_script(MIGRATIONS[version])
            await self.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(version),),
            )
        if current == 0:
            await self.execute(
                "INSERT INTO schema_meta(key, value) VALUES('created_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(time.time()),),
            )

    # ------------------------------------------------------------------
    async def _exec_script(self, script: str) -> None:
        conn = self._require()

        def _run() -> None:
            conn.executescript(script)
            conn.commit()

        async with self._lock:
            await asyncio.to_thread(_run)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        conn = self._require()

        def _run() -> None:
            conn.execute(sql, tuple(params))
            conn.commit()

        async with self._lock:
            await asyncio.to_thread(_run)

    async def execute_transaction(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> None:
        """在单个事务里执行多条语句：任一条失败则整体回滚（U2b 级联删除用）。"""
        conn = self._require()
        batch = [(sql, tuple(params)) for sql, params in statements]

        def _run() -> None:
            try:
                conn.execute("BEGIN IMMEDIATE")
                for sql, params in batch:
                    conn.execute(sql, params)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        async with self._lock:
            await asyncio.to_thread(_run)

    async def conversation_child_tables(self) -> list[str]:
        """返回所有带 ``conversation_id`` 列的表名（级联删除时动态覆盖，避免漏表）。"""
        rows = await self.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
        tables: list[str] = []
        for row in rows:
            name = str(row.get("name") or "")
            if not name or name.startswith("sqlite_"):
                continue
            cols = await self.fetchall(f"PRAGMA table_info({name})")
            if any(str(c.get("name")) == "conversation_id" for c in cols):
                tables.append(name)
        return tables

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        conn = self._require()
        rows = [tuple(p) for p in seq]

        def _run() -> None:
            conn.executemany(sql, rows)
            conn.commit()

        async with self._lock:
            await asyncio.to_thread(_run)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = await self.fetchall(sql, params, limit=1)
        return rows[0] if rows else None

    async def fetchall(
        self, sql: str, params: Sequence[Any] = (), *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        conn = self._require()

        def _run() -> list[dict[str, Any]]:
            cur = conn.execute(sql, tuple(params))
            rows = cur.fetchmany(limit) if limit else cur.fetchall()
            return [dict(r) for r in rows]

        async with self._lock:
            return await asyncio.to_thread(_run)


__all__ = [
    "MIGRATIONS",
    "MIGRATIONS_DIR",
    "ROLLBACK_MIGRATIONS",
    "SCHEMA_VERSION",
    "Database",
    "load_migration_sql",
]
