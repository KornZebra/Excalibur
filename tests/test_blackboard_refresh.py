"""M3 运行中黑板增量刷新回归测试（同步风格）。

覆盖：增量查询过滤 / 渲染格式 / 双闸门节流（步数、时间、关闭）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ctfagent.orchestrator.solver_pool import delta_due
from ctfagent.state.clock import iso_after
from ctfagent.state.renderer import render_delta
from ctfagent.storage.db import Database
from ctfagent.storage.repos import Repos


def test_list_since_excludes_self_and_old(tmp_path: Path) -> None:
    async def _inner() -> None:
        db = Database(tmp_path / "delta.db")
        await db.connect()
        try:
            repos = Repos(db)
            old = await repos.memories.append(
                challenge_id="ch1", kind="fact", summary="旧事实", solver_run_id="sr_other"
            )
            await db.execute(
                "UPDATE memories SET created_at=? WHERE id=?", (iso_after(-30), old["id"])
            )
            await repos.memories.append(
                challenge_id="ch1", kind="fact", summary="我自己的产出", solver_run_id="sr_me"
            )
            other = await repos.memories.append(
                challenge_id="ch1", kind="failure", summary="别人踩的坑", solver_run_id="sr_other"
            )

            fresh = await repos.memories.list_since(
                "ch1", since_iso=iso_after(-5), exclude_solver_run_id="sr_me", limit=3
            )
            assert [m["id"] for m in fresh] == [other["id"]]
        finally:
            await db.close()

    asyncio.run(_inner())


def test_render_delta_format_and_limit() -> None:
    memories = [
        {"id": "f21", "kind": "fact", "summary": "发现 /backup.zip 可下载"},
        {"id": "f22", "kind": "failure", "summary": "sqlmap 被 WAF 拦截", "fromIdeaId": "i7"},
        {"id": "f23", "kind": "evidence", "summary": "目录扫描发现 /api/v2/debug"},
        {"id": "f24", "kind": "note", "summary": "第四条不应出现"},
    ]
    text = render_delta(memories, limit=3)
    lines = text.splitlines()
    assert len(lines) == 3
    assert "f21 [事实]" in lines[0]
    assert "f22 [此路不通] ← i7" in lines[1]
    assert "f24" not in text


def test_delta_due_gates() -> None:
    assert delta_due(1, 10.0, 8, 90) is False
    assert delta_due(8, 10.0, 8, 90) is True
    assert delta_due(1, 90.0, 8, 90) is True
    assert delta_due(100, 999.0, 0, 90) is False
