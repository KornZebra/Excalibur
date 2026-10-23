"""数据库迁移运维 CLI：``status`` / ``up`` / ``down``。

v2 起迁移脚本以 ``migrations/00X_*.sql`` 落盘，本模块提供不启动服务即可执行的
升级/回滚入口，并在任何写操作前**自动备份**数据库文件。

用法::

    python -m ctfagent.storage.migrate status
    python -m ctfagent.storage.migrate up                 # 升级到最新（默认先备份）
    python -m ctfagent.storage.migrate up --to 2
    python -m ctfagent.storage.migrate down --to 1        # 回滚（执行 00X_*_rollback.sql）
    python -m ctfagent.storage.migrate up --db data/ctfagent.db --no-backup

注意：SQLite 的 DDL 在同一连接上是非事务性的（``executescript`` 会隐式提交），
因此回滚依赖备份文件而非事务回滚——这也是默认强制备份的原因。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from ..config import load_config
from .db import MIGRATIONS, MIGRATIONS_DIR, ROLLBACK_MIGRATIONS, SCHEMA_VERSION

META_TABLE = "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(META_TABLE)
    conn.commit()
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    return int(row[0]) if row else 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(version),),
    )
    conn.commit()


def backup(db_path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = db_path.with_name(f"{db_path.name}.bak.{stamp}")
    shutil.copy2(db_path, target)
    return target


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def cmd_status(db_path: Path) -> int:
    conn = _connect(db_path)
    try:
        version = current_version(conn)
        tables = sorted(_existing_tables(conn))
        print(f"数据库：{db_path}")
        print(f"当前 schema 版本：{version}（代码支持到 {SCHEMA_VERSION}）")
        pending = [v for v in sorted(MIGRATIONS) if v > version]
        print(f"待应用迁移：{pending or '（无）'}")
        for probe in ("ideas", "memories", "solver_runs", "challenge_sessions"):
            mark = "✓" if probe in tables else "—"
            print(f"  {mark} {probe}")
        print(f"表总数：{len(tables)}")
        return 0
    finally:
        conn.close()


def cmd_up(db_path: Path, target: int, *, do_backup: bool) -> int:
    conn = _connect(db_path)
    try:
        version = current_version(conn)
        todo = [v for v in sorted(MIGRATIONS) if version < v <= target]
        if not todo:
            print(f"已是最新（版本 {version}），无需升级。")
            return 0
        if do_backup and db_path.exists():
            saved = backup(db_path)
            print(f"已备份：{saved}")
        for v in todo:
            print(f"应用迁移 {v:03d} …")
            conn.executescript(MIGRATIONS[v])
            _set_version(conn, v)
        print(f"升级完成：{version} → {current_version(conn)}")
        return 0
    finally:
        conn.close()


def cmd_down(db_path: Path, target: int, *, do_backup: bool) -> int:
    conn = _connect(db_path)
    try:
        version = current_version(conn)
        todo = [v for v in sorted(ROLLBACK_MIGRATIONS, reverse=True) if target < v <= version]
        if not todo:
            print(f"当前版本 {version} 无需回滚到 {target}。")
            return 0
        if do_backup and db_path.exists():
            saved = backup(db_path)
            print(f"已备份：{saved}")
        for v in todo:
            print(f"回滚迁移 {v:03d} …")
            conn.executescript(ROLLBACK_MIGRATIONS[v])
            _set_version(conn, v - 1)
        print(f"回滚完成：{version} → {current_version(conn)}")
        return 0
    finally:
        conn.close()


def _resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db).expanduser().resolve()
    config = load_config(args.config)
    return config.database_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ctfagent.storage.migrate",
        description="CTF-Agent 数据库迁移工具（升级/回滚/查看状态）",
    )
    parser.add_argument(
        "action", choices=["status", "up", "down"], nargs="?", default="status", help="操作（默认 status）"
    )
    parser.add_argument(
        "--to", type=int, default=None, help=f"目标版本（默认 up 到 {SCHEMA_VERSION}，down 到 1）"
    )
    parser.add_argument("--db", default=None, help="数据库文件路径（默认取配置中的 db_path）")
    parser.add_argument("--config", "-c", default=None, help="配置文件路径（用于解析 db_path）")
    parser.add_argument("--no-backup", action="store_true", help="跳过自动备份（不推荐）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = _resolve_db_path(args)
    if args.action == "status":
        return cmd_status(db_path)
    if args.action == "up":
        return cmd_up(db_path, args.to or SCHEMA_VERSION, do_backup=not args.no_backup)
    target = 1 if args.to is None else args.to
    return cmd_down(db_path, target, do_backup=not args.no_backup)


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())


__all__ = [
    "MIGRATIONS_DIR",
    "backup",
    "build_parser",
    "cmd_down",
    "cmd_status",
    "cmd_up",
    "current_version",
    "main",
]
