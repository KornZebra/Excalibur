"""M2 因果边回归测试（同步风格）。

覆盖：边表幂等 / 批量来源查询 / 反向查询 / 渲染因果前缀 / memory 产出指针。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ctfagent.state.renderer import RenderInputs, _idea_line, _memory_line, render_worker_context
from ctfagent.storage.db import Database
from ctfagent.storage.repos import Repos

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_edge_roundtrip(tmp_path: Path) -> None:
    async def _inner() -> None:
        db = Database(tmp_path / "edges.db")
        await db.connect()
        try:
            repos = Repos(db)
            m1 = await repos.memories.append(challenge_id="ch1", kind="fact", summary="发现登录页")
            m2 = await repos.memories.append(challenge_id="ch1", kind="evidence", summary="存在 sql 报错")
            idea = await repos.ideas.create(challenge_id="ch1", text="尝试 SQL 注入", created_by="sys")

            await repos.ideas.add_sources(idea["id"], "ch1", [m1["id"], m2["id"]])
            # 幂等：重复写入不报错、不重复计数
            await repos.ideas.add_sources(idea["id"], "ch1", [m1["id"]])

            sources = await repos.ideas.sources_of("ch1", [idea["id"]])
            assert sources[idea["id"]] == [m1["id"], m2["id"]]
            assert await repos.ideas.ideas_from(m2["id"]) == [idea["id"]]

            # 删除 Idea 时级联删除边
            await repos.ideas.delete(idea["id"])
            assert await repos.ideas.sources_of("ch1", [idea["id"]]) == {}
        finally:
            await db.close()

    asyncio.run(_inner())


def test_memory_from_idea_pointer(tmp_path: Path) -> None:
    async def _inner() -> None:
        db = Database(tmp_path / "pointer.db")
        await db.connect()
        try:
            repos = Repos(db)
            idea = await repos.ideas.create(challenge_id="ch1", text="方向A", created_by="sys")
            memory = await repos.memories.append(
                challenge_id="ch1", kind="fact", summary="拿到了 shell", from_idea_id=idea["id"]
            )
            assert memory["fromIdeaId"] == idea["id"]
            fetched = await repos.memories.get(memory["id"])
            assert fetched["fromIdeaId"] == idea["id"]
        finally:
            await db.close()

    asyncio.run(_inner())


def test_idea_line_with_sources() -> None:
    idea = {"id": "i1", "text": "尝试 SQL 注入", "status": "pending"}
    text, prio = _idea_line(idea, own_idea_id=None, own_run=None, sources=["f003", "f009"])
    assert "← f003+f009" in text
    assert prio == 0

    plain, _ = _idea_line(idea, own_idea_id=None, own_run=None, sources=None)
    assert "←" not in plain


def test_memory_line_with_from_idea() -> None:
    line, _ = _memory_line({"id": "f1", "kind": "fact", "summary": "拿到 shell", "fromIdeaId": "i7"})
    assert "(from i7)" in line

    plain, _ = _memory_line({"id": "f2", "kind": "fact", "summary": "无来源"})
    assert "(from" not in plain


def test_render_worker_context_includes_edges() -> None:
    challenge = {"id": "ch1", "name": "测试题", "category": "web", "target": "10.0.0.1"}
    block = render_worker_context(
        challenge,
        {"id": "sr1", "taskType": "explore", "ideaId": "i1", "ideaText": "尝试注入"},
        inputs=RenderInputs(
            ideas=[{"id": "i1", "text": "尝试注入", "status": "testing"}],
            memories=[{"id": "f1", "kind": "fact", "summary": "有报错", "fromIdeaId": "i0"}],
            idea_sources={"i1": ["f1"]},
            own_idea_id="i1",
            own_solver_run_id="sr1",
        ),
    )
    assert "← f1" in block
    assert "(from i0)" in block
