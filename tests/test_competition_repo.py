"""M5 竞赛数据层回归测试（同步风格）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ctfagent.storage.db import Database
from ctfagent.storage.repos import Repos


def test_competition_crud_and_board(tmp_path: Path) -> None:
    async def _inner() -> None:
        db = Database(tmp_path / "comp.db")
        await db.connect()
        try:
            repos = Repos(db)
            comp = await repos.competitions.create(
                name="测试赛", base_url="https://example.com/api", platform_kind="dasctf",
                auth_kind="header", auth_header="X-Agent-AccessKey", auth_ref="config:competition.key",
            )
            assert comp["status"] == "idle"
            assert comp["platformKind"] == "dasctf"

            fact = await repos.competitions.add_fact(
                comp["id"], kind="rule", summary="每队最多 3 实例", source="doc"
            )
            assert fact["kind"] == "rule"
            counts = await repos.competitions.count_facts(comp["id"])
            assert counts["rule"] == 1

            # 能力默认关闭
            cap = await repos.competitions.upsert_capability(
                comp["id"], name="submit_flag", transport="rest",
                spec={"method": "POST", "path": "/answer"}, confidence="high",
            )
            assert cap["enabled"] is False
            await repos.competitions.set_capability_enabled(comp["id"], "submit_flag", True)
            caps = await repos.competitions.list_capabilities(comp["id"])
            assert caps[0]["enabled"] is True

            # 状态更新校验
            await repos.competitions.update(comp["id"], status="running")
            assert (await repos.competitions.get(comp["id"]))["status"] == "running"
            try:
                await repos.competitions.update(comp["id"], status="bogus")
                raise AssertionError("应拒绝非法状态")
            except ValueError:
                pass
        finally:
            await db.close()

    asyncio.run(_inner())


def test_exercise_and_submission_dedupe(tmp_path: Path) -> None:
    async def _inner() -> None:
        db = Database(tmp_path / "comp2.db")
        await db.connect()
        try:
            repos = Repos(db)
            comp = await repos.competitions.create(name="赛", base_url="https://x")
            ex = await repos.competitions.upsert_exercise(
                comp["id"], exercise_key="1001", name="easy-web", score=100,
                difficulty="EASY", flags_total=2,
            )
            assert ex["flagsTotal"] == 2
            await repos.competitions.bind_challenge(comp["id"], "1001", "chal_x")
            assert (await repos.competitions.get_exercise(comp["id"], "1001"))["challengeId"] == "chal_x"

            first = await repos.competitions.add_submission(
                comp["id"], exercise_key="1001", flag="flag{a}", verdict="wrong"
            )
            assert first is not None
            # 唯一索引去重：同 flag 第二次返回 None
            again = await repos.competitions.add_submission(
                comp["id"], exercise_key="1001", flag="flag{a}", verdict="wrong"
            )
            assert again is None
            counts = await repos.competitions.count_verdicts(comp["id"])
            assert counts["wrong"] == 1

            attempts = await repos.competitions.bump_attempt(comp["id"], "1001")
            assert attempts == 1

            # 策略状态
            await repos.competitions.set_state(comp["id"], "pinned:1001", True)
            assert await repos.competitions.get_state(comp["id"], "pinned:1001") is True
            await repos.competitions.delete_state(comp["id"], "pinned:1001")
            assert await repos.competitions.get_state(comp["id"], "pinned:1001") is None
        finally:
            await db.close()

    asyncio.run(_inner())


def test_delete_cascades(tmp_path: Path) -> None:
    async def _inner() -> None:
        db = Database(tmp_path / "comp3.db")
        await db.connect()
        try:
            repos = Repos(db)
            comp = await repos.competitions.create(name="删", base_url="https://y")
            await repos.competitions.add_fact(comp["id"], kind="notice", summary="n")
            await repos.competitions.upsert_exercise(comp["id"], exercise_key="1")
            await repos.competitions.delete(comp["id"])
            assert await repos.competitions.get(comp["id"]) is None
            assert await repos.competitions.list_facts(comp["id"]) == []
            assert await repos.competitions.list_exercises(comp["id"]) == []
        finally:
            await db.close()

    asyncio.run(_inner())
