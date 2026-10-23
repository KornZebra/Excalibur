"""题目级隔离（U18）的回归测试：黑板按 challenge 作用域隔离。

历史问题：``facts`` 以 ``conversation_id`` 为主键 —— 同一题目的多个 Solver 会话各写各的
黑板，控制台看不到彼此的结论；会话升级为题目后，历史事实仍留在会话作用域里「消失」。
若直接按会话读取，又会出现「同项目不同题目互相看到对方结论」的串题风险。

修复：引入统一作用域键 ``scope``（见 :mod:`ctfagent.scope`）：
``challenge:<id>`` 同题共享、跨题隔离；``conversation:<cid>`` 仅属于该会话。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ctfagent.scope import fact_scope, parse_scope
from ctfagent.storage.db import Database
from ctfagent.storage.repos import Repos


# ---------------------------------------------------------------------------
# 作用域键
# ---------------------------------------------------------------------------
def test_fact_scope_prefers_challenge() -> None:
    assert fact_scope("chal_a", "conv_x") == "challenge:chal_a"
    assert fact_scope(None, "conv_x") == "conversation:conv_x"
    assert fact_scope("  ", "conv_x") == "conversation:conv_x"
    assert fact_scope(None, "") == "conversation:_anon"


def test_parse_scope_roundtrip() -> None:
    assert parse_scope("challenge:chal_a") == ("challenge", "chal_a")
    assert parse_scope("conversation:conv_x") == ("conversation", "conv_x")
    # 兼容裸 conversation_id
    assert parse_scope("conv_legacy") == ("conversation", "conv_legacy")


# ---------------------------------------------------------------------------
# 仓储层隔离
# ---------------------------------------------------------------------------
def _run(tmp_path: Path, body) -> None:
    async def main() -> None:
        db = Database(str(tmp_path / "facts.db"))
        await db.connect()
        try:
            await body(Repos(db), db)
        finally:
            await db.close()

    asyncio.run(main())


def test_challenge_blackboards_are_isolated(tmp_path: Path) -> None:
    async def body(repos: Repos, db: Database) -> None:
        scope_a = fact_scope("chal_a", "conv_1")
        scope_b = fact_scope("chal_b", "conv_2")
        await repos.facts.upsert(scope_a, "web.sqli", summary="A 题注入点")
        await repos.facts.upsert(scope_b, "pwn.heap", summary="B 题堆布局")

        keys_a = [f["key"] for f in await repos.facts.list(scope_a)]
        keys_b = [f["key"] for f in await repos.facts.list(scope_b)]
        assert keys_a == ["web.sqli"]
        assert keys_b == ["pwn.heap"]
        assert await repos.facts.get(scope_a, "pwn.heap") is None
        assert await repos.facts.search(scope_b, "堆") != []
        assert await repos.facts.search(scope_a, "堆") == []

    _run(tmp_path, body)


def test_same_challenge_shares_blackboard_across_conversations(tmp_path: Path) -> None:
    """同一题目的两个会话（控制台 Solver / 升级会话）必须看到同一块黑板。"""

    async def body(repos: Repos, db: Database) -> None:
        solver_a = fact_scope("chal_a", "conv_solver_1")
        solver_b = fact_scope("chal_a", "conv_solver_2")
        assert solver_a == solver_b  # 作用域只看题目
        await repos.facts.upsert(solver_a, "crypto.key", summary="密钥在 0x401210")
        fact = await repos.facts.get(solver_b, "crypto.key")
        assert fact is not None and "0x401210" in fact["summary"]

    _run(tmp_path, body)


def test_conversation_blackboard_isolated_between_conversations(tmp_path: Path) -> None:
    async def body(repos: Repos, db: Database) -> None:
        await repos.facts.upsert(fact_scope(None, "conv_1"), "note.a", summary="会话1")
        await repos.facts.upsert(fact_scope(None, "conv_2"), "note.b", summary="会话2")
        assert [f["key"] for f in await repos.facts.list(fact_scope(None, "conv_1"))] == ["note.a"]
        assert [f["key"] for f in await repos.facts.list(fact_scope(None, "conv_2"))] == ["note.b"]

    _run(tmp_path, body)


def test_rescope_moves_conversation_facts_into_challenge(tmp_path: Path) -> None:
    """会话升级为题目：会话期事实并入题目黑板，原作用域清空。"""

    async def body(repos: Repos, db: Database) -> None:
        conv_scope = fact_scope(None, "conv_1")
        chal_scope = fact_scope("chal_new", "conv_1")
        await repos.facts.upsert(conv_scope, "a.one", summary="来自会话")
        await repos.facts.upsert(conv_scope, "a.two", summary="来自会话 2")
        await repos.facts.upsert(chal_scope, "a.one", summary="题目侧已有更新")  # 较新，应保留

        moved = await repos.facts.rescope(conv_scope, chal_scope)
        assert moved >= 1
        assert await repos.facts.list(conv_scope) == []
        keys = {f["key"]: f["summary"] for f in await repos.facts.list(chal_scope)}
        assert keys["a.two"] == "来自会话 2"
        assert keys["a.one"] == "题目侧已有更新"  # 冲突时较新者胜出

    _run(tmp_path, body)


def test_created_at_is_seconds_not_milliseconds(tmp_path: Path) -> None:
    """回归：旧 upsert 把毫秒时间戳写回秒字段，导致 created_at 放大 1000 倍。"""

    async def body(repos: Repos, db: Database) -> None:
        scope = fact_scope("chal_a", "conv_1")
        first = await repos.facts.upsert(scope, "k", summary="v1")
        created = first["createdAt"]
        second = await repos.facts.upsert(scope, "k", summary="v2")
        assert second["created"] is False
        # createdAt 是毫秒；两次写入后不应出现 1000 倍漂移
        assert abs(second["createdAt"] - created) < 5_000

    _run(tmp_path, body)


def test_schema_has_scope_primary_key(tmp_path: Path) -> None:
    async def body(repos: Repos, db: Database) -> None:
        columns = [r["name"] for r in await db.fetchall("PRAGMA table_info(facts)")]
        assert "scope" in columns and "challenge_id" in columns
        row = await db.fetchone(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='facts'"
        )
        assert "PRIMARY KEY (scope, key)" in (row["sql"] or "")

    _run(tmp_path, body)


# ---------------------------------------------------------------------------
# 工具处理器 → 作用域（集成）
# ---------------------------------------------------------------------------
def test_blackboard_tool_writes_into_challenge_scope(tmp_path: Path) -> None:
    """``blackboard_upsert`` 在绑定题目的会话里必须写进题目作用域。"""

    async def body(repos: Repos, db: Database) -> None:
        from ctfagent.config import AppConfig
        from ctfagent.tools.builtin.blackboard import blackboard_get, blackboard_upsert
        from ctfagent.tools.context import ToolContext

        config = AppConfig()
        config.project_root = tmp_path
        ctx = ToolContext(
            config=config,
            repos=repos,
            registry=None,  # type: ignore[arg-type]
            execution_service=None,  # type: ignore[arg-type]
            conversation_id="conv_solver",
            challenge_id="chal_a",
        )
        assert ctx.blackboard_scope == "challenge:chal_a"

        out = await blackboard_upsert({"key": "pwn.heap", "summary": "堆布局确认"}, ctx)
        assert out.is_error is False

        # 题目作用域可见
        assert await repos.facts.get("challenge:chal_a", "pwn.heap") is not None
        # 会话作用域不可见（跨维度不串）
        assert await repos.facts.list("conversation:conv_solver") == []
        # 另一个题目不可见
        assert await repos.facts.get("challenge:chal_b", "pwn.heap") is None

        # 同题另一会话（如控制台 Solver）能读到
        other = ToolContext(
            config=config,
            repos=repos,
            registry=None,  # type: ignore[arg-type]
            execution_service=None,  # type: ignore[arg-type]
            conversation_id="conv_console",
            challenge_id="chal_a",
        )
        got = await blackboard_get({"key": "pwn.heap"}, other)
        assert got.is_error is False and "堆布局确认" in got.content

    _run(tmp_path, body)
