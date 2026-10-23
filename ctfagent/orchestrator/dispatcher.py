"""外置 Dispatcher：规则驱动的 asyncio 控制回路（**唯一控制面**）。

循环形态（对应升级提示词 §5.2）::

    while True:
        state = await self.snapshot(challenge_id)     # ideas / memories / solver_runs / 时间
        verdict = self.termination.evaluate(state)    # 结束条件完全外置
        if verdict: await self.finish(...); return verdict
        action = self.policy.decide(state)            # 纯规则，零 LLM
        await self.execute(state, action)             # 派发/停止/等待
        await asyncio.sleep(self.interval_seconds)

设计要点：

* **控制面与模型面分离**：模型只做 Worker，调度决策完全由确定性代码做出，
  每一轮决策都会通过 ``dispatcher_decision`` 事件以及账本留痕（可审计、可回放）。
* **合规边界优先**：``snapshot`` 每次都做目标范围校验；未通过则终止评估器直接
  ``TERMINATE_ABORTED``，绝不派发。
* **失败即产出**：僵尸 Solver（进程重启遗留的 running 行）也会被补一条 failure Memory，
  账本里不允许出现"看起来还在跑"的假状态。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..config import AppConfig
from ..events import EventType
from ..state.clock import seconds_since
from ..state.ideas import IdeaService
from ..state.memory import MemoryService
from ..storage.repos import new_id
from .policy import (
    ChallengeSnapshot,
    IdeaView,
    Policy,
    SolverView,
    SpawnBootstrap,
    SpawnExplore,
    SpawnReason,
    StopSolver,
    Wait,
)
from .solver_pool import SolverPool, SolverSpec
from .termination import TerminationEvaluator, TerminationVerdict, check_target_scope

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from ..agents.markdown import AgentDefinition, AgentRegistry
    from ..storage.repos import Repos

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

#: 从 v1 专项子代理提示词中抽取"专业方法"段落（复用方法段，不引入调度职责）
_METHOD_HEADING = re.compile(r"^#{2,3}\s*.*(方法|方法论|思路|checklist|Checklist).*$", re.MULTILINE)

#: 各类任务的交接包模板（短交接 + 状态块由渲染器提供其余上下文）
HANDOFF_TEMPLATES: dict[str, str] = {
    "bootstrap": (
        "【bootstrap】对题目做开局侦察，只做三件事：\n"
        "1. 确认目标可达性与技术栈/入口点（记录可复现命令）；\n"
        "2. 用 idea_upsert 声明 2~4 条**可验证**的方向假设（每条写清「如何验证」）；\n"
        "3. 用 memory_write 记录已确认的事实（fact）。\n"
        "不要尝试一次性解题；你的产出是「账本开局」。"
    ),
    "explore": (
        "【explore】验证下面这一条方向（只做这一条）：\n"
        "  Idea：{idea_text}\n"
        "先 idea_transition(status=testing) 认领，然后动手验证；结束时按收尾纪律收尾。"
    ),
    "reason": (
        "【reason】对当前账本做一次归纳推理，不要重复执行已有命令：\n"
        "1. 读完全部 Idea 与 Memory（含 failure/hint）；\n"
        "2. 若存在尚未被利用的事实组合，用 idea_upsert 产出 1~3 条新的可验证方向；\n"
        "3. 若所有方向确实已收敛，**不要**再声明新 Idea，直接在回复中输出 ## 结论 说明收敛原因。\n"
        "本任务不强制产生新 Idea，但必须写一条 memory_write（fact 或 failure）总结判断依据。"
    ),
}


class Dispatcher:
    """规则驱动的调度回路（无 LLM 参与决策）。"""

    def __init__(
        self,
        *,
        config: AppConfig,
        repos: Repos,
        pool: SolverPool,
        agents: AgentRegistry | None = None,
        registry: Any = None,
        execution_service: Any = None,
        emit: EmitFn | None = None,
        policy: Policy | None = None,
        termination: TerminationEvaluator | None = None,
        interval_seconds: float | None = None,
        worker_agent_id: str = "worker",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.repos = repos
        self.pool = pool
        self.agents = agents
        self.registry = registry
        self.execution_service = execution_service
        self.emit = emit
        self.policy = policy or Policy(config)
        self.termination = termination or TerminationEvaluator(config)
        #: 本轮运行采用的战法（None = 不干预）；只影响 Worker 提示词与工具集，
        #: 不参与调度决策（控制面仍然完全确定性）。
        # M4：每 challenge 上次成功 reason 时的图指纹（内存态；重启后自动重建基线）
        self._reason_fp: dict[str, tuple[int, int, bool]] = {}
        self.interval_seconds = (
            float(interval_seconds)
            if interval_seconds is not None
            else max(0.05, float(config.dispatcher.interval_seconds))
        )
        self.worker_agent_id = worker_agent_id
        self.clock = clock
        self.ticks = 0
        self.verdict: TerminationVerdict | None = None
        self.actions: list[str] = []
        self._last_spawn_at: float | None = None
        self._stop_requested = False
        self._stop_reason = ""
        self._wake = asyncio.Event()
        self._started_wall = self.clock()

    # ------------------------------------------------------------------
    # 对外控制
    # ------------------------------------------------------------------
    def request_stop(self, reason: str = "human_stop") -> None:
        """请求停止挑战（终止评估器下一轮会返回 STOPPED 并归档）。

        同时唤醒控制回路：轮询间隔是可中断的，人工停止不必等满一个 interval。
        """
        self._stop_requested = True
        self._stop_reason = self._stop_reason or reason
        self._wake.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    # ------------------------------------------------------------------
    # 主回路
    # ------------------------------------------------------------------
    async def run(self, challenge_id: str) -> TerminationVerdict:
        """运行控制回路直到出现终止结论。"""
        logger.info("Dispatcher 启动：challenge=%s，interval=%.2fs", challenge_id, self.interval_seconds)
        while True:
            snapshot = await self.snapshot(challenge_id)
            verdict = self.termination.evaluate(snapshot)
            if verdict is not None:
                await self.finish(challenge_id, verdict, snapshot)
                return verdict
            action = self.policy.decide(snapshot)
            self.ticks += 1
            await self.execute(snapshot, action)
            # 可中断的轮询间隔：request_stop（人工停止）会立即唤醒回路评估 STOPPED，
            # 而不必等满一个 interval 才响应
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_seconds)
                self._wake.clear()
            except TimeoutError:
                pass

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    async def snapshot(self, challenge_id: str) -> ChallengeSnapshot:
        # M1：惰性清扫过期租约（读到才清，无需后台定时器）
        await self._sweep_expired_leases(challenge_id)
        challenge = await self.repos.challenges.get(challenge_id)
        scope = check_target_scope(challenge)
        session = await self.repos.challenge_sessions.get(challenge_id)

        ideas_raw = await self.repos.ideas.list(challenge_id, limit=200)
        ideas = [
            IdeaView(
                id=str(i["id"]),
                text=str(i["text"]),
                status=str(i["status"]),
                claimed_by=i.get("claimedBy"),
                age_seconds=seconds_since(i.get("lastTransitionAt") or i.get("createdAt")),
                created_at=str(i.get("createdAt") or ""),
            )
            for i in ideas_raw
        ]

        live_runs = {run.solver_run_id: run for run in self.pool.active() if run.solver_run_id}
        rows = await self.repos.solver_runs.list(challenge_id, limit=200)
        solvers: list[SolverView] = []
        for row in rows:
            run = live_runs.get(str(row["id"]))
            if run is not None:
                elapsed = run.elapsed_seconds
                has_progress = (await self.repos.memories.count_for_solver(str(row["id"]))) > 0
                solvers.append(
                    SolverView(
                        id=str(row["id"]),
                        task_type=str(row["taskType"]),
                        status="running",
                        idea_id=row.get("ideaId"),
                        elapsed_seconds=elapsed,
                        has_progress=has_progress,
                        live=True,
                        claim_expires_at=row.get("claimExpiresAt"),
                    )
                )
                continue
            solvers.append(
                SolverView(
                    id=str(row["id"]),
                    task_type=str(row["taskType"]),
                    status=str(row["status"]),
                    idea_id=row.get("ideaId"),
                    elapsed_seconds=_elapsed_between(row.get("startedAt"), row.get("endedAt")),
                    has_progress=(await self.repos.memories.count_for_solver(str(row["id"]))) > 0,
                    live=False,
                    claim_expires_at=row.get("claimExpiresAt"),
                )
            )

        seen = {str(r["id"]) for r in rows}
        for run in live_runs.values():
            if run.solver_run_id in seen:
                continue
            solvers.append(
                SolverView(
                    id=run.solver_run_id,
                    task_type=str(run.spec.task_type),
                    status="running",
                    idea_id=run.spec.idea_id,
                    elapsed_seconds=run.elapsed_seconds,
                    has_progress=False,
                    live=True,
                )
            )

        memory_counts = await self.repos.memories.count_by_kind(challenge_id)
        task_counts = await self.repos.solver_runs.count_by_task_type(challenge_id, finished_only=False)
        flags = await self.repos.flags.list_by_challenge(challenge_id, limit=50)
        elapsed = seconds_since(session.get("startedAt")) if session else (self.clock() - self._started_wall)
        budget_minutes = float((session or {}).get("timeBudgetMinutes") or 0)

        return ChallengeSnapshot(
            challenge_id=challenge_id,
            reason_fingerprint=self._reason_fp.get(challenge_id),
            target=str((challenge or {}).get("target") or ""),
            ideas=ideas,
            solvers=solvers,
            running_count=len(live_runs),
            bootstraps_done=int(task_counts.get("bootstrap", 0)),
            explores_done=int(task_counts.get("explore", 0)),
            reasons_done=int(task_counts.get("reason", 0)),
            last_task_type=(str(rows[0]["taskType"]) if rows else None),
            facts_count=int(memory_counts.get("fact", 0)),
            failures_count=int(memory_counts.get("failure", 0)),
            hints_count=int(memory_counts.get("hint", 0)),
            memories_count=sum(memory_counts.values()),
            flag_verified=any(bool(f.get("validated")) for f in flags),
            elapsed_seconds=elapsed,
            time_budget_seconds=budget_minutes * 60,
            since_last_spawn_seconds=(
                None if self._last_spawn_at is None else max(0.0, time.monotonic() - self._last_spawn_at)
            ),
            stop_requested=self._stop_requested,
            scope_ok=scope.allowed,
            scope_reason=scope.reason,
        )

    # ------------------------------------------------------------------
    # 动作执行
    # ------------------------------------------------------------------
    async def _sweep_expired_leases(self, challenge_id: str) -> None:
        """M1：把心跳过期的 running/concluding 行标记为孤儿，并释放对应 Idea 认领。"""
        try:
            expired = await self.repos.solver_runs.list_expired_leases(challenge_id)
        except Exception:  # noqa: BLE001 - 清扫失败不阻断调度
            logger.debug("租约清扫失败", exc_info=True)
            return
        if not expired:
            return
        live_ids = {run.solver_run_id for run in self.pool.active() if run.solver_run_id}
        for row in expired:
            solver_id = str(row["id"])
            if solver_id in live_ids:
                continue  # 进程内仍在跑（极端时钟抖动）：不杀
            try:
                await self.repos.solver_runs.finish(
                    solver_id, status="timeout", outcome="orphaned_lease", concluded=True,
                )
                idea_id = row.get("ideaId")
                if idea_id:
                    await self.repos.ideas.release_claim(str(idea_id))
                logger.info("租约过期回收 solver=%s idea=%s", solver_id, idea_id)
            except Exception:  # noqa: BLE001
                logger.warning("租约回收失败 solver=%s", solver_id, exc_info=True)

    async def execute(self, snapshot: ChallengeSnapshot, action: Any) -> None:
        """执行一个调度动作，并发出 ``dispatcher_decision`` 事件。"""
        label = type(action).__name__
        detail: dict[str, Any] = {"action": label, "reason": getattr(action, "reason", "")}
        if isinstance(action, SpawnExplore):
            detail.update({"ideaId": action.idea_id, "ideaText": action.idea_text})
            await self._spawn_explore(snapshot, action)
        elif isinstance(action, SpawnBootstrap):
            await self._spawn(snapshot, task_type="bootstrap", idea=None)
        elif isinstance(action, SpawnReason):
            await self._spawn(snapshot, task_type="reason", idea=None)
            # M4：仅在派发成功后记录指纹（失败不更新 → 下轮仍可重试）
            self._reason_fp[snapshot.challenge_id] = (
                snapshot.memories_count,
                snapshot.hints_count,
                bool(snapshot.open_ideas),
            )
        elif isinstance(action, StopSolver):
            detail.update({"solverRunId": action.solver_run_id, "why": action.why})
            await self._stop_solver(snapshot, action)
        elif isinstance(action, Wait):
            detail["waitReason"] = action.reason
        else:  # pragma: no cover - 防御性分支
            logger.warning("未知调度动作：%r", action)

        self.actions.append(f"{label}:{detail.get('reason', '')}")
        await self._emit(
            EventType.DISPATCHER_DECISION,
            {
                **detail,
                "challengeId": snapshot.challenge_id,
                "tick": self.ticks,
                "runningCount": snapshot.running_count,
                "openIdeas": len(snapshot.open_ideas),
                "pool": self.pool.stats(),
                # 控制面全程可审计：把决策依据的关键计数一并发出
                "snapshotSummary": {
                    "ideas": len(snapshot.ideas),
                    "pendingIdeas": len(snapshot.pending_ideas),
                    "testingIdeas": len(snapshot.testing_ideas),
                    "solvers": len(snapshot.solvers),
                    "running": snapshot.running_count,
                    "facts": snapshot.facts_count,
                    "failures": snapshot.failures_count,
                    "hints": snapshot.hints_count,
                    "bootstraps": snapshot.bootstraps_done,
                    "explores": snapshot.explores_done,
                    "reasons": snapshot.reasons_done,
                    "flagVerified": snapshot.flag_verified,
                    "elapsedSeconds": round(snapshot.elapsed_seconds, 1),
                    "scopeOk": snapshot.scope_ok,
                },
            },
        )

    async def _spawn_explore(self, snapshot: ChallengeSnapshot, action: SpawnExplore) -> None:
        """认领 Idea 并派发 explore（认领失败则跳过本轮）。"""
        solver_run_id = new_id("solver_")
        idea = await self.repos.ideas.get(action.idea_id)
        if idea is None or idea["status"] != "pending":
            logger.info("Idea %s 已不可认领（%s），跳过本轮派发", action.idea_id, (idea or {}).get("status"))
            return
        await self.repos.ideas.update_status(action.idea_id, "testing", claimed_by=solver_run_id)
        await self._spawn(
            snapshot,
            task_type="explore",
            idea={"id": action.idea_id, "text": action.idea_text or str(idea["text"])},
            solver_run_id=solver_run_id,
        )

    async def _spawn(
        self,
        snapshot: ChallengeSnapshot,
        *,
        task_type: str,
        idea: dict[str, Any] | None,
        solver_run_id: str | None = None,
    ) -> None:
        if not self.pool.has_capacity():
            logger.info("并发已满，放弃本次派发（%s）", task_type)
            return
        challenge = await self.repos.challenges.get(snapshot.challenge_id)
        prompt, tool_names, agent_id = self._worker_setup(challenge)
        idea_text = str((idea or {}).get("text") or "")
        handoff = HANDOFF_TEMPLATES.get(task_type, HANDOFF_TEMPLATES["explore"]).format(idea_text=idea_text)

        spec = SolverSpec(
            challenge_id=snapshot.challenge_id,
            task_type=task_type,
            idea_id=(idea or {}).get("id"),
            idea_text=idea_text,
            handoff=handoff,
            agent_id=agent_id,
            agent_prompt=prompt,
            tool_names=tool_names,
            tactic_id="",
            run_id=solver_run_id or new_id("solver_"),
            conversation_title=(
                f"[{task_type}]"
                + f" {challenge.get('name') if challenge else snapshot.challenge_id}"
            ),
        )
        self.pool.start(spec)
        self._last_spawn_at = time.monotonic()
        logger.info("Dispatcher 派发 %s（solver=%s，idea=%s）", task_type, spec.run_id, spec.idea_id)

    async def _stop_solver(self, snapshot: ChallengeSnapshot, action: StopSolver) -> None:
        """停止 Solver：进程内的走协作式取消；僵尸行直接归档并补 failure Memory。"""
        for run in self.pool.active():
            if run.solver_run_id == action.solver_run_id:
                run.request_stop(action.why)
                return
        row = await self.repos.solver_runs.get(action.solver_run_id)
        if row is None or row["status"] not in {"running", "concluding"}:
            return
        await self.repos.solver_runs.finish(
            action.solver_run_id, status="timeout", outcome="orphaned_solver", concluded=True
        )
        await MemoryService(self.repos).write(
            snapshot.challenge_id,
            "failure",
            f"僵尸 Solver 已归档（{action.why}）",
            body=(
                f"solver_run={action.solver_run_id}\n"
                f"task_type={row.get('taskType')}\n"
                f"idea_id={row.get('ideaId')}\n"
                "该 Solver 已不在进程池中（进程重启或异常退出），由 Dispatcher 归档。"
            ),
            source="system",
            solver_run_id=action.solver_run_id,
            allow_privileged_source=True,
        )
        logger.warning("已归档僵尸 Solver %s", action.solver_run_id)

    # ------------------------------------------------------------------
    # Worker 提示词与工具集
    # ------------------------------------------------------------------
    def _worker_setup(
        self,
        challenge: dict[str, Any] | None,
    ) -> tuple[str | None, list[str] | None, str]:
        """返回 ``(system_prompt, tool_names, agent_id)``。

        * 基础提示词来自 ``agents/worker.md``（剥离调度职责）；
        * 若题目分类命中 v1 专项子代理，则**追加其方法段**（复用方法、不引入调度职责）；
        * 工具集 = Worker 工具 ∪ 专项工具（都必须在注册表里存在）。
        """
        if self.agents is None:
            prompt, tools, agent_id = None, [], self.worker_agent_id
        else:
            worker = self.agents.get(self.worker_agent_id)
            specialist = self._specialist_for(challenge)
            if worker is None and specialist is None:
                prompt, tools, agent_id = None, [], self.worker_agent_id
            else:
                agent_id = worker.id if worker else self.worker_agent_id
                prompt = worker.system_prompt if worker else None
                tools = list(worker.tools or []) if worker else []
                if specialist is not None and specialist.id != agent_id:
                    method = _method_section(specialist.system_prompt)
                    if method:
                        header = f"\n\n## 专项方法（复用 {specialist.id} 子代理的方法段）\n"
                        prompt = (prompt or "") + header + method
                    for tool in specialist.tools or []:
                        if tool not in tools:
                            tools.append(tool)

        return prompt, (tools or None), agent_id

    def _specialist_for(self, challenge: dict[str, Any] | None) -> AgentDefinition | None:
        if self.agents is None or not challenge:
            return None
        category = str(challenge.get("category") or "").strip().lower()
        if not category:
            return None
        candidate = self.agents.get(category)
        if candidate is None:
            # 允许"仅该分类可用"的宽松匹配（例如 category=web2 → web）
            for agent in self.agents.workers():
                if agent.id in {"worker"}:
                    continue
                if category.startswith(agent.id):
                    return agent
            return None
        return candidate

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    async def finish(
        self,
        challenge_id: str,
        verdict: TerminationVerdict,
        snapshot: ChallengeSnapshot | None = None,
    ) -> None:
        """归档挑战：停 Solver → 收敛 Idea → 生成 writeup → 落库 → 发事件。"""
        self.verdict = verdict
        snapshot = snapshot or await self.snapshot(challenge_id)
        reason = self.termination.describe(verdict, snapshot)
        logger.info("挑战 %s 终止：%s（%s）", challenge_id, verdict.value, reason)

        # 1) 停止所有 Solver（进程内的会各自 conclude）
        await self.pool.cancel_all(verdict.value)
        results = self.pool.results()
        concluded = sum(1 for r in results if r.concluded)

        # 2) 未派发的 pending Idea 全部 skipped（账本保持一致）
        skipped = 0
        ideas = await self.repos.ideas.list(challenge_id, statuses=["pending", "testing"], limit=200)
        for idea in ideas:
            try:
                await IdeaService(self.repos).transition(
                    challenge_id,
                    str(idea["id"]),
                    "skipped",
                    evidence_summary=f"挑战结束（{verdict.value}）：该方向未被验证",
                    actor="system",
                )
                skipped += 1
            except Exception:  # noqa: BLE001 - 归档失败不能中断收尾
                logger.debug("结束归档时跳过 Idea %s 失败", idea.get("id"), exc_info=True)

        # 3) 账本留痕（系统来源）
        report_path = await self._writeup(challenge_id)
        memories = await MemoryService(self.repos).write(
            challenge_id,
            "note",
            f"挑战结束：{verdict.value}",
            body=(
                f"verdict={verdict.value}\nreason={reason}\n"
                f"已用={snapshot.elapsed_seconds / 60:.1f} 分钟（预算 {snapshot.time_budget_seconds / 60:.0f} 分钟）\n"
                f"账本：ideas={len(snapshot.ideas)}(open={len(snapshot.open_ideas)}) "
                f"facts={snapshot.facts_count} failures={snapshot.failures_count} hints={snapshot.hints_count}\n"
                f"Solver：派发 {len(snapshot.solvers)}，conclude 产出 {concluded}\n"
                f"未验证方向已归档 {skipped} 条\n"
                f"writeup={report_path or '（未生成）'}"
            ),
            source="system",
            allow_privileged_source=True,
        )

        # 4) 会话状态归档
        session = await self.repos.challenge_sessions.upsert(
            challenge_id,
            status="terminated",
            verdict=verdict.value,
            reason=reason,
            ended_at=_now_iso(),
            spawned_solvers=len(snapshot.solvers),
            meta={
                "ticks": self.ticks,
                "actions": self.actions[-50:],
                "reportPath": report_path,
                "elapsedSeconds": round(snapshot.elapsed_seconds, 1),
            },
        )

        # 5) 事件
        await self._emit(
            EventType.CHALLENGE_TERMINATED,
            {
                "challengeId": challenge_id,
                "verdict": verdict.value,
                "reason": reason,
                "elapsedSeconds": round(snapshot.elapsed_seconds, 1),
                "facts": snapshot.facts_count,
                "solverRuns": len(snapshot.solvers),
                "concludedSolvers": concluded,
                "skippedIdeas": skipped,
                "reportPath": report_path,
                "noteMemoryId": memories["id"],
                "session": session,
            },
        )

    async def _writeup(self, challenge_id: str) -> str | None:
        """尽力生成 writeup（复用 v1 的 ``report_generate`` 工具，不改其实现）。"""
        if self.registry is None or self.execution_service is None:
            return None
        last = next((r for r in reversed(self.pool.results()) if r.conversation_id), None)
        if last is None or not last.conversation_id:
            return None
        definition = self.registry.get("report_generate")
        if definition is None:
            return None
        from ..tools.context import ToolContext

        ctx = ToolContext(
            config=self.config,
            repos=self.repos,
            registry=self.registry,
            execution_service=self.execution_service,
            conversation_id=last.conversation_id,
            run_id=f"{last.solver_run_id}_report",
            agent_id="dispatcher",
            challenge_id=challenge_id,
        )
        try:
            outcome = await definition.handler({"write_to_file": True}, ctx)
        except Exception:  # noqa: BLE001 - writeup 失败不影响归档
            logger.exception("生成 writeup 失败（challenge=%s）", challenge_id)
            return None
        if getattr(outcome, "is_error", False):
            logger.info("writeup 未生成：%s", str(getattr(outcome, "content", ""))[:200])
            return None
        content = str(getattr(outcome, "content", "") or "")
        for token in content.replace("`", " ").split():
            index = token.find("workspace/")
            if index >= 0:
                return token[index:].strip(".,;:()")
        return None

    # ------------------------------------------------------------------
    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self.emit is None:
            return
        try:
            await self.emit(event_type, data)
        except Exception:  # noqa: BLE001 - 事件失败不能中断控制回路
            logger.debug("Dispatcher 事件发送失败：%s", event_type, exc_info=True)


# ---------------------------------------------------------------------------
def _method_section(prompt: str) -> str:
    """抽取提示词中的"专业方法"段落（到下一个同级标题为止）。"""
    match = _METHOD_HEADING.search(prompt or "")
    if match is None:
        return ""
    rest = prompt[match.start() :]
    lines = rest.split("\n")
    heading_level = len(lines[0]) - len(lines[0].lstrip("#"))
    body = [lines[0]]
    for line in lines[1:]:
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if level <= heading_level:
                break
        body.append(line)
    text = "\n".join(body).strip()
    return text[:4000]


def _elapsed_between(started_at: str | None, ended_at: str | None) -> float:
    if not started_at:
        return 0.0
    if ended_at:
        from ..state.clock import parse_iso

        start, end = parse_iso(started_at), parse_iso(ended_at)
        if start and end:
            return max(0.0, (end - start).total_seconds())
    return seconds_since(started_at)


def _now_iso() -> str:
    from ..state.clock import now_iso

    return now_iso()


__all__ = ["HANDOFF_TEMPLATES", "Dispatcher"]
