"""M1 租约化回归测试（同步风格：不依赖 pytest-asyncio）。

覆盖：
1. 租约过期 → StopSolver（新主判据）
2. 租约未过期 → 不杀
3. 无租约 + 超软超时 + 无产出 → 旧启发式仍然生效（兼容）
4. 租约过期但有产出且在软超时内 → 宽限不杀
5. repos 层：续约写入心跳 / 过期列表 / release_claim 退回 pending
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ctfagent.config import AppConfig
from ctfagent.orchestrator.policy import ChallengeSnapshot, Policy, SolverView, StopSolver
from ctfagent.state.clock import iso_after
from ctfagent.storage.db import Database
from ctfagent.storage.repos import Repos

REPO_ROOT = Path(__file__).resolve().parents[1]


def _policy(**solver_overrides) -> Policy:
    config = AppConfig(db_path=":memory:", project_root=REPO_ROOT)
    if solver_overrides:
        config = config.model_copy(
            update={"solver": config.solver.model_copy(update=solver_overrides)}
        )
    return Policy(config)


def _snapshot(solvers: list[SolverView]) -> ChallengeSnapshot:
    return ChallengeSnapshot(challenge_id="ch_test", solvers=solvers)


def _solver(**kw) -> SolverView:
    base = dict(id="sr_1", task_type="explore", status="running", live=False)
    base.update(kw)
    return SolverView(**base)


# ---------------------------------------------------------------------------
# Policy 判定
# ---------------------------------------------------------------------------
def test_expired_lease_returns_stop() -> None:
    action = _policy()._stale_solver(_snapshot([_solver(claim_expires_at=iso_after(-1))]))
    assert isinstance(action, StopSolver)
    assert "租约过期" in action.why


def test_fresh_lease_is_not_killed() -> None:
    action = _policy()._stale_solver(_snapshot([_solver(claim_expires_at=iso_after(5))]))
    assert action is None


def test_expired_lease_with_progress_gets_grace() -> None:
    action = _policy(soft_timeout_minutes=20)._stale_solver(
        _snapshot([
            _solver(
                claim_expires_at=iso_after(-1),
                has_progress=True,
                elapsed_seconds=60.0,
            )
        ])
    )
    assert action is None


def test_no_lease_falls_back_to_legacy_heuristic() -> None:
    # 无租约 + 超软超时（默认 20min）+ 无产出 → 旧启发式仍然杀
    action = _policy()._stale_solver(
        _snapshot([_solver(claim_expires_at=None, elapsed_seconds=21 * 60.0)])
    )
    assert isinstance(action, StopSolver)
    assert "僵尸" in action.why


def test_no_lease_within_soft_timeout_not_killed() -> None:
    action = _policy()._stale_solver(
        _snapshot([_solver(claim_expires_at=None, elapsed_seconds=60.0)])
    )
    assert action is None


def test_live_solver_never_killed() -> None:
    action = _policy()._stale_solver(
        _snapshot([_solver(claim_expires_at=iso_after(-30), live=True)])
    )
    assert action is None


# ---------------------------------------------------------------------------
# repos 层
# ---------------------------------------------------------------------------
def test_renew_and_expire_roundtrip(tmp_path: Path) -> None:
    async def _inner() -> None:
        db = Database(tmp_path / "lease.db")
        await db.connect()
        try:
            repos = Repos(db)
            run = await repos.solver_runs.create(
                challenge_id="ch_test", task_type="explore", idea_id="idea_1"
            )
            # 初始：无心跳 → 不视为过期（旧数据兼容）
            assert await repos.solver_runs.list_expired_leases("ch_test") == []

            # 续约后：TTL 有效期内不过期
            await repos.solver_runs.renew_lease(run["id"], ttl_seconds=180)
            assert await repos.solver_runs.list_expired_leases("ch_test") == []

            # 心跳拨到 5 分钟前 + TTL 60s → 过期
            await db.execute(
                "UPDATE solver_runs SET heartbeat_at=?, lease_ttl_seconds=60 WHERE id=?",
                (iso_after(-5), run["id"]),
            )
            expired = await repos.solver_runs.list_expired_leases("ch_test")
            assert [row["id"] for row in expired] == [run["id"]]
            assert expired[0]["leaseTtlSeconds"] == 60
        finally:
            await db.close()

    asyncio.run(_inner())


def test_release_claim_resets_idea_to_pending(tmp_path: Path) -> None:
    async def _inner() -> None:
        db = Database(tmp_path / "release.db")
        await db.connect()
        try:
            repos = Repos(db)
            idea = await repos.ideas.create(challenge_id="ch_test", text="测试方向", created_by="sys")
            await repos.ideas.update_status(
                idea["id"], "testing", claimed_by="sr_1", claim_expires_at=iso_after(3)
            )
            claimed = await repos.ideas.get(idea["id"])
            assert claimed["status"] == "testing"
            assert claimed["claimExpiresAt"]

            await repos.ideas.release_claim(idea["id"])
            released = await repos.ideas.get(idea["id"])
            assert released["status"] == "pending"
            assert released["claimedBy"] is None
            assert released["claimExpiresAt"] is None
        finally:
            await db.close()

    asyncio.run(_inner())
