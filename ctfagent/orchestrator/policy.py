"""调度策略：**全部确定性、零 LLM**，可穷举单测。

规则表（对应升级提示词 §5.3）::

    R1 有 pending Idea 且 running < max_concurrent_solvers 且距上次 spawn ≥ stability_window
       → SpawnExplore（认领：claim 该 Idea，status→testing）
    R2 无 pending/testing Idea 且 running==0 且已有 verified fact 但未拿到 flag
       → SpawnReason（输入=全量账本渲染，输出=新 Ideas 或宣布 Complete）
    R3 存在超过 soft_timeout 且无新 Memory 的 Solver → StopSolver（触发 conclude）
    R4 稳定性优先：仅因"出现了新 Idea"不停正在健康的 Solver；只在 R3/R5 条件下停
    R5 flag 已验证 / 时间预算耗尽 / 图静止 → 交给终止评估器（本模块不判终止）

R3 的边界说明：**进程内**在跑的 Solver 由 :class:`~ctfagent.orchestrator.solver_pool.SolverRun`
自带的看门狗执行软/硬超时（协作式取消 + conclude）；本模块的 R3 负责 **DB 里登记为 running
但已不在进程池中**的僵尸行（进程重启/异常退出留下的），把它们收掉并补一条 failure Memory，
保证账本里不会长期挂着"看起来还在跑"的假状态。
"""

from __future__ import annotations

from ..state.clock import seconds_since as _seconds_since

from dataclasses import dataclass, field
from typing import Any

from ..config import AppConfig

__all__ = [
    "Action",
    "ChallengeSnapshot",
    "IdeaView",
    "Policy",
    "SolverView",
    "SpawnBootstrap",
    "SpawnExplore",
    "SpawnReason",
    "StopSolver",
    "Wait",
]


# ---------------------------------------------------------------------------
# 快照视图（Dispatcher 从 DB/进程池装配，Policy 只读）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IdeaView:
    """一条 Idea 的调度视图。"""

    id: str
    text: str
    status: str
    claimed_by: str | None = None
    age_seconds: float = 0.0
    created_at: str = ""


@dataclass(frozen=True)
class SolverView:
    """一个 Solver 的调度视图。"""

    id: str
    task_type: str
    status: str
    idea_id: str | None = None
    elapsed_seconds: float = 0.0
    has_progress: bool = False
    #: 是否仍在当前进程的并发池中（False = 僵尸行）
    live: bool = True
    #: M1：认领租约到期时间（空 = 无租约，走旧 soft_timeout 启发式）
    claim_expires_at: str | None = None


@dataclass(frozen=True)
class ChallengeSnapshot:
    """控制回路每轮看到的完整状态（不可变，便于单测构造成字面量）。"""

    challenge_id: str
    target: str = ""
    ideas: list[IdeaView] = field(default_factory=list)
    solvers: list[SolverView] = field(default_factory=list)
    running_count: int = 0
    #: 已**派发**次数（含在跑的；用于"是否已开局/推进"判断）
    bootstraps_done: int = 0
    explores_done: int = 0
    reasons_done: int = 0
    #: 最近一次派发的任务类型（用于阻止 reason→reason 空转）
    last_task_type: str | None = None
    #: M4：上次成功 reason 时的图指纹 (memories 数, hints 数, 是否有 open ideas)
    reason_fingerprint: tuple[int, int, bool] | None = None
    facts_count: int = 0
    failures_count: int = 0
    hints_count: int = 0
    memories_count: int = 0
    flag_verified: bool = False
    elapsed_seconds: float = 0.0
    time_budget_seconds: float = 0.0
    since_last_spawn_seconds: float | None = None
    stop_requested: bool = False
    scope_ok: bool = True
    scope_reason: str = ""

    # ---- 派生视图 ----
    @property
    def pending_ideas(self) -> list[IdeaView]:
        return [i for i in self.ideas if i.status == "pending"]

    @property
    def testing_ideas(self) -> list[IdeaView]:
        return [i for i in self.ideas if i.status == "testing"]

    @property
    def open_ideas(self) -> list[IdeaView]:
        return [i for i in self.ideas if i.status in {"pending", "testing"}]

    @property
    def has_verified_facts(self) -> bool:
        return self.facts_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "challengeId": self.challenge_id,
            "target": self.target,
            "ideas": [
                {
                    "id": i.id,
                    "text": i.text,
                    "status": i.status,
                    "claimedBy": i.claimed_by,
                    "ageSeconds": round(i.age_seconds, 1),
                }
                for i in self.ideas
            ],
            "solvers": [
                {
                    "id": s.id,
                    "taskType": s.task_type,
                    "status": s.status,
                    "ideaId": s.idea_id,
                    "elapsedSeconds": round(s.elapsed_seconds, 1),
                    "hasProgress": s.has_progress,
                    "live": s.live,
                }
                for s in self.solvers
            ],
            "runningCount": self.running_count,
            "bootstrapsDone": self.bootstraps_done,
            "exploresDone": self.explores_done,
            "reasonsDone": self.reasons_done,
            "lastTaskType": self.last_task_type,
            "factsCount": self.facts_count,
            "failuresCount": self.failures_count,
            "hintsCount": self.hints_count,
            "flagVerified": self.flag_verified,
            "elapsedSeconds": round(self.elapsed_seconds, 1),
            "timeBudgetSeconds": round(self.time_budget_seconds, 1),
            "stopRequested": self.stop_requested,
            "scopeOk": self.scope_ok,
            "scopeReason": self.scope_reason,
        }


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SpawnBootstrap:
    """派发一次 bootstrap（开局侦察 + 产出初始 Idea）。"""

    reason: str = ""


@dataclass(frozen=True)
class SpawnExplore:
    """派发一次 explore（认领指定 pending Idea）。"""

    idea_id: str = ""
    idea_text: str = ""
    reason: str = ""


@dataclass(frozen=True)
class SpawnReason:
    """派发一次 reason（对全量账本做归纳，产出新 Idea 或宣布收敛）。"""

    reason: str = ""


@dataclass(frozen=True)
class StopSolver:
    """停止一个 Solver（触发其 conclude）。"""

    solver_run_id: str = ""
    why: str = ""


@dataclass(frozen=True)
class Wait:
    """本轮不动作。"""

    reason: str = ""


Action = SpawnBootstrap | SpawnExplore | SpawnReason | StopSolver | Wait


# ---------------------------------------------------------------------------
class Policy:
    """规则驱动的调度策略（无 IO、无随机、无时钟）。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    @property
    def max_concurrent(self) -> int:
        return max(1, int(self.config.dispatcher.max_concurrent_solvers))

    @property
    def stability_window(self) -> float:
        return max(0.0, float(self.config.dispatcher.stability_window_seconds))

    @property
    def soft_timeout(self) -> float:
        return max(0.0, float(self.config.solver.soft_timeout_minutes) * 60)

    def decide(self, snapshot: ChallengeSnapshot) -> Action:
        """按 R3 → R1 → bootstrap → R2 的优先级给出本轮动作。"""
        # R3：先收僵尸 Solver（安全优先，避免并发额度被无效占用）
        if stale := self._stale_solver(snapshot):
            return stale

        if snapshot.stop_requested:
            return Wait(reason="已收到停止请求，等待终止评估器收尾")

        if not snapshot.scope_ok:
            return Wait(reason=f"目标范围校验未通过：{snapshot.scope_reason}")

        capacity_ok = snapshot.running_count < self.max_concurrent
        stability_ok = self._stability_ok(snapshot)

        # R1：认领一条 pending Idea
        if snapshot.pending_ideas and capacity_ok and stability_ok:
            idea = self._pick_idea(snapshot.pending_ideas)
            return SpawnExplore(
                idea_id=idea.id,
                idea_text=idea.text,
                reason=f"R1 认领 pending Idea（已等待 {idea.age_seconds:.0f}s）",
            )

        # 开局：还没有任何 Idea、从未 bootstrap 过、且当前没有 Solver 在跑
        if (
            not snapshot.ideas
            and snapshot.bootstraps_done == 0
            and snapshot.running_count == 0
            and capacity_ok
            and stability_ok
        ):
            return SpawnBootstrap(reason="开局无账本：先做一次 bootstrap 侦察并产出初始 Idea")

        # R2：图静止但已有事实且未拿到 flag → 让 reason 归纳或宣布收敛
        # M4：用图指纹去重（memories 数 / hints 数 / 是否有 open ideas 三者之一变化才 reason），
        # 替代旧的 last_task_type 启发式；开关关闭时退回旧逻辑（可秒级回滚）。
        if self.config.dispatcher.reason_by_fingerprint:
            fingerprint = (snapshot.memories_count, snapshot.hints_count, bool(snapshot.open_ideas))
            reason_due = snapshot.reason_fingerprint != fingerprint
        else:
            reason_due = snapshot.last_task_type != "reason"
        if (
            not snapshot.open_ideas
            and snapshot.running_count == 0
            and snapshot.has_verified_facts
            and not snapshot.flag_verified
            and reason_due
            and capacity_ok
            and stability_ok
        ):
            return SpawnReason(reason="图态势已变化且无运行中 Solver：归纳账本，产出新方向或宣布收敛")

        return Wait(reason=self._wait_reason(snapshot, capacity_ok, stability_ok))

    # ------------------------------------------------------------------
    def _stability_ok(self, snapshot: ChallengeSnapshot) -> bool:
        """距上次 spawn 是否已达稳定窗（R4：稳定性优先）。"""
        since = snapshot.since_last_spawn_seconds
        return since is None or since >= self.stability_window

    def _pick_idea(self, pending: list[IdeaView]) -> IdeaView:
        """FIFO：最久未被认领的方向优先（防饿死）。"""
        return sorted(pending, key=lambda i: (i.created_at, i.id))[0]

    def _stale_solver(self, snapshot: ChallengeSnapshot) -> StopSolver | None:
        """R3：回收僵尸 Solver。

        M1 起**租约优先**：认领时写入 ``claim_expires_at``，Solver 看门狗每
        ``lease_renew_seconds`` 续约；过期即视为死亡（进程崩溃/挂起的可信信号）。
        无租约的旧数据退回原有 ``soft_timeout + has_progress`` 启发式，行为不变。
        """
        for solver in snapshot.solvers:
            if solver.live:
                continue  # 进程内的由 SolverRun 看门狗负责（M2）
            if solver.status not in {"running", "concluding"}:
                continue
            # ① 租约过期：最可信的死亡信号（宽限：过期但近期仍在产出则不杀）
            if solver.claim_expires_at:
                if _seconds_since(solver.claim_expires_at) <= 0:
                    continue  # 未过期
                if solver.has_progress and solver.elapsed_seconds < self.soft_timeout:
                    continue  # 仍有产出且未到软超时：给一轮宽限
                return StopSolver(
                    solver_run_id=solver.id,
                    why="R3 租约过期（心跳停止）",
                )
            # ② 无租约：旧启发式（soft_timeout + 无产出）
            if self.soft_timeout <= 0:
                continue
            if solver.elapsed_seconds < self.soft_timeout:
                continue
            if solver.has_progress:
                continue
            return StopSolver(
                solver_run_id=solver.id,
                why=f"R3 僵尸 Solver（不在进程池、超时 {solver.elapsed_seconds:.0f}s 且无产出）",
            )
        return None

    def _wait_reason(self, snapshot: ChallengeSnapshot, capacity_ok: bool, stability_ok: bool) -> str:
        if not capacity_ok:
            return f"并发已满（{snapshot.running_count}/{self.max_concurrent}），等待 Solver 收敛"
        if not stability_ok:
            return f"稳定性窗口未到（距上次派发 {snapshot.since_last_spawn_seconds or 0:.0f}s < {self.stability_window:.0f}s）"
        if snapshot.testing_ideas and not snapshot.pending_ideas:
            return "已有 Idea 正在被验证，等待其收敛"
        if not snapshot.ideas:
            return "等待 bootstrap 产出初始 Idea"
        return "无满足条件的动作，继续观察"
