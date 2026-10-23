"""运行期装配（Runtime）：把配置、存储、工具平面、模型、子代理与运行句柄组装起来。

Web 服务与 CLI 共用本模块，避免两套装配逻辑漂移。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .agent.audit_agent import AuditAgentReviewer
from .agent.finalizer import Finalizer
from .agent.hitl import HITLManager
from .agent.loop import AgentRunner, RunResult
from .agents.markdown import AgentRegistry
from .agents.orchestrator import SubAgentOrchestrator, apply_orchestration_mode
from .attachments import build_attachment_manifest
from .config import AppConfig, HITLConfig
from .constants import __version__
from .events import EventType, QueueEventBus
from .models.resilience import ResilientModelClient
from .models.types import ToolOutcome
from .orchestrator import session as challenge_session
from .orchestrator.dispatcher import Dispatcher
from .orchestrator.solver_pool import LEDGER_TOOLS, SolverPool
from .orchestrator.termination import check_target_scope
from .steering import SteeringCoordinator
from .storage.db import Database
from .storage.repos import Repos, new_id
from .tools.builtin import register_builtins
from .tools.context import ToolContext
from .tools.execution_service import ExecutionService
from .tools.guard import soft_failure
from .tools.mcp_bridge import MCPBridge
from .tools.registry import ToolDefinition, ToolRegistry
from .tools.yaml_loader import load_yaml_tools

logger = logging.getLogger(__name__)


@dataclass
class RunHandle:
    """一次 Agent 运行的句柄（供 SSE 消费与取消）。"""

    run_id: str
    bus: QueueEventBus
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    conversation_id: str = ""
    agent_id: str = "ctf-agent"
    orchestration: str = "single"
    started_at: float = field(default_factory=time.time)
    result: RunResult | None = None
    orchestrator: Any = None


class Runtime:
    """应用运行期容器。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        db: Database,
        repos: Repos,
        registry: ToolRegistry,
        execution_service: ExecutionService,
        model: ResilientModelClient,
        agents: AgentRegistry,
        hitl: HITLManager,
        tool_load_errors: list[str] | None = None,
        mcp: MCPBridge | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.repos = repos
        self.registry = registry
        self.execution_service = execution_service
        self.model = model
        self.agents = agents
        self.hitl = hitl
        self.mcp = mcp
        self.tool_load_errors = tool_load_errors or []
        self.runs: dict[str, RunHandle] = {}
        # v2：挑战会话（blackboard 模式）——挑战 id → 会话句柄
        self.challenge_sessions: dict[str, challenge_session.ChallengeSession] = {}
        # 运行中引导（Steering）：按会话管理运行中任务、FIFO 队列与会话级事件枢纽
        self.steering = SteeringCoordinator(self)
        self._judge_model: ResilientModelClient | None = None
        # 作战图 AI 推荐战法用的轻量客户端（懒创建，与判官通道相互独立）
        self._board_model: ResilientModelClient | None = None
        self.started_at = time.time()

    # ------------------------------------------------------------------
    # 装配
    # ------------------------------------------------------------------
    @classmethod
    async def create(cls, config: AppConfig, *, approver: Any = None) -> Runtime:
        db = Database(config.database_path)
        await db.connect()
        repos = Repos(db)

        registry = ToolRegistry(expose_all=not config.agent.tool_search_enabled)
        register_builtins(registry, config)

        tools_dir = config.project_root / "tools"
        yaml_tools, errors = load_yaml_tools(tools_dir, project_root=config.project_root)
        registry.register_many(yaml_tools)
        for message in errors:
            logger.warning("工具配方加载问题：%s", message)

        execution_service = ExecutionService(config, repos=repos, registry=registry)
        registry.register_many(execution_service.meta_tools())

        agents = AgentRegistry()
        agents.load_directory(config.project_root / "agents")
        custom = getattr(config.agent, "custom_agents", None) or {}
        if custom:
            agents.merge(custom)

        model = ResilientModelClient(
            config.ai.ordered_channels(),
            emit=None,  # 运行期由 Runtime.broadcast 转发
        )

        hitl = HITLManager(_effective_hitl_config(config), approver=approver, repos=repos)

        runtime = cls(
            config=config,
            db=db,
            repos=repos,
            registry=registry,
            execution_service=execution_service,
            model=model,
            agents=agents,
            hitl=hitl,
            tool_load_errors=errors,
        )
        model.emit = runtime.broadcast

        # U6b：审计器通道在每次审计时惰性解析（闭包读 runtime.config，热更新后自动跟随）
        runtime.hitl.auditor = AuditAgentReviewer(
            config.hitl.audit_agent,
            channels_provider=lambda: runtime.config.ai.ordered_channels(),
        )

        # 多代理：注册 task 工具（handler 从 ctx.services 取本次运行的 orchestrator）
        if config.multi_agent.enabled:
            runtime._register_task_tool()

        if config.mcp.enabled:
            runtime.mcp = MCPBridge(config.mcp)
            registered = await runtime.mcp.start(registry)
            if registered:
                logger.info("已注册 %d 个 MCP 工具", len(registered))

        if config.knowledge.enabled:
            try:
                from .knowledge.retriever import register_knowledge_tools

                register_knowledge_tools(registry, config)
            except Exception as exc:  # noqa: BLE001 - 知识库失败不影响主流程
                logger.warning("知识库工具注册失败：%s", exc)

        logger.info(
            "Runtime 就绪：%s 个工具（可用 %s）｜%s 个子代理｜数据库 %s",
            registry.summary()["total"],
            registry.summary()["available"],
            len(agents),
            config.database_path,
        )
        return runtime

    def _register_task_tool(self) -> None:
        """注册主代理专用的 ``task`` 工具（真正的 orchestrator 在每次运行时注入）。"""

        async def _task(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
            orchestrator: SubAgentOrchestrator | None = ctx.service("orchestrator")
            if orchestrator is None:
                return ToolOutcome.error(
                    soft_failure(
                        "多代理编排器未就绪（可能在子代理中被调用）。",
                        tool="task",
                        hint="task 仅主代理可用，且需 multi_agent.enabled=true。",
                    )
                )
            return await orchestrator.run_task(args, ctx)

        agent_ids = [a.id for a in self.agents.sub_agents()]
        self.registry.register(
            ToolDefinition(
                name="task",
                short_description="派发专项子代理（每个子代理只看到你传入的 description）",
                description=(
                    "把一个明确的子目标派发给专项子代理并在同一轮拿到结构化交付物。description 必须包含："
                    "题目上下文（题目名/分类/描述要点/已确认信息/已排除假设）、本轮唯一子目标与成功标准、"
                    "附件的 workspace 绝对路径、禁止事项。互不依赖的子目标应在同一条回复里并行多次调用。"
                    "禁止嵌套派单（子代理无权调用 task）。\n"
                    f"可用子代理 id：{', '.join(agent_ids) or '（未加载到）'}"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "agent": {
                            "type": "string",
                            "description": "子代理 id",
                            **({"enum": agent_ids} if agent_ids else {}),
                        },
                        "description": {
                            "type": "string",
                            "description": "完整交接包（上下文 + 唯一子目标与成功标准 + 附件路径 + 禁止事项）",
                        },
                        "expected_deliverable": {"type": "string", "description": "期望交付物形态"},
                        "max_iterations": {"type": "integer", "description": "本子代理迭代上限"},
                    },
                    "required": ["agent", "description"],
                },
                handler=_task,
                source="builtin",
                category="multi_agent",
                always_expose=True,
            )
        )

    def apply_config(self, new_config: AppConfig) -> None:
        """热更新配置：部分字段生效（模型通道、执行治理、HITL、黑板、知识库）。"""
        old_model = self.model
        self.config = new_config
        self.execution_service.config = new_config
        self.hitl.config = _effective_hitl_config(new_config)
        if self.hitl.auditor is not None:
            self.hitl.auditor.refresh(new_config.hitl.audit_agent)
        self.registry.expose_all = not new_config.agent.tool_search_enabled
        self.model = ResilientModelClient(new_config.ai.ordered_channels())
        self.model.emit = self.broadcast
        asyncio.create_task(old_model.aclose())
        logger.info("配置已热更新（通道顺序：%s）", [c.name for c in new_config.ai.ordered_channels()])

    async def close(self) -> None:
        for session in list(self.challenge_sessions.values()):
            session.request_stop("runtime_close")
            if session.task is not None and not session.task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(session.task), timeout=30)
                except TimeoutError:
                    session.task.cancel()
                except Exception:  # noqa: BLE001 - 关闭阶段尽力而为
                    logger.debug("挑战会话收尾失败：%s", session.challenge_id, exc_info=True)
        if self._judge_model is not None:
            await self._judge_model.aclose()
            self._judge_model = None
        for handle in list(self.runs.values()):
            handle.cancel_event.set()
        await asyncio.gather(
            *(h.task for h in self.runs.values() if h.task and not h.task.done()),
            return_exceptions=True,
        )
        if self.mcp is not None:
            await self.mcp.stop()
        # 先收尾所有工具执行与落库任务，再关数据库（避免 sqlite3 并发关闭崩溃）
        await self.execution_service.shutdown()
        await self.model.aclose()
        await self.db.close()

    # ------------------------------------------------------------------
    # 事件广播
    # ------------------------------------------------------------------
    async def broadcast(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """把事件广播给所有活跃运行（供模型重试/切换通道等全局事件使用）。"""
        for handle in list(self.runs.values()):
            try:
                await handle.bus.emit(event_type, data or {})
            except Exception:  # noqa: BLE001
                logger.debug("广播事件失败", exc_info=True)

    # ------------------------------------------------------------------
    # 运行管理
    # ------------------------------------------------------------------
    def runner_factory(self, model: ResilientModelClient | None = None) -> AgentRunner:
        """AgentRunner 工厂；``model`` 非空时使用专用客户端（会话级模型选择）。"""
        return AgentRunner(
            config=self.config,
            model=model or self.model,
            registry=self.registry,
            execution_service=self.execution_service,
            repos=self.repos,
            hitl=self.hitl,
            finalizer=Finalizer(self.config),
        )

    def start_run(
        self,
        message: str,
        *,
        conversation_id: str | None = None,
        challenge_id: str | None = None,
        orchestration: str | None = None,
        multi: bool = False,
        agent_id: str | None = None,
        system_prompt: str | None = None,
        attachment_ids: list[str] | None = None,
        steering: Any = None,
        pre_persisted_user_message_id: str | None = None,
        model_channel: str | None = None,
    ) -> RunHandle:
        """创建一次后台运行并返回句柄（调用方消费 ``handle.bus.queue``）。

        ``attachment_ids``（U2a）非空时，本轮消息会被追加一段附件清单块；为空时行为与改造前完全一致。
        ``steering`` / ``pre_persisted_user_message_id``（运行中引导）：由统一消息入口
        ``POST /api/conversations/{id}/messages`` 传入，见 :mod:`ctfagent.steering`。
        ``model_channel``（模型选择）：会话在 composer 里选定的模型通道 key。指定后本次运行
        （含多代理子代理）只使用该通道的专用客户端——用户显式选择的模型不参与跨模型 failover；
       缺省或通道不存在时回落既有默认通道链，行为与改造前完全一致。
        """
        # 会话级模型选择：构建只含被选通道的专用客户端（子代理共用同一工厂）
        chosen: ResilientModelClient | None = None
        if model_channel:
            channel = self.config.ai.channels.get(model_channel)
            if channel is None:
                logger.warning("start_run: model_channel=%r 不存在，回落默认通道链", model_channel)
            else:
                chosen = ResilientModelClient([channel], emit=None)
        factory = self.runner_factory if chosen is None else (lambda: self.runner_factory(chosen))
        mode = orchestration or self.config.multi_agent.default_orchestration
        use_multi = multi and self.config.multi_agent.enabled

        if use_multi:
            orchestrator_def = self.agents.get("ctf-orchestrator") or self.agents.get("orchestrator")
            agent_id = agent_id or (orchestrator_def.id if orchestrator_def else "ctf-agent")
            agent_prompt = (
                apply_orchestration_mode(orchestrator_def.system_prompt, mode) if orchestrator_def else None
            )
            tool_names = None
        else:
            agent_id = agent_id or "ctf-agent"
            agent_prompt = None
            tool_names = None

        run_id = new_id("run_")
        bus = QueueEventBus(run_id=run_id, agent_id=agent_id)
        handle = RunHandle(
            run_id=run_id,
            bus=bus,
            conversation_id=conversation_id or "",
            agent_id=agent_id,
            orchestration="multi:" + mode if use_multi else "single",
        )
        self.runs[run_id] = handle

        services: dict[str, Any] = {}
        if use_multi:
            orchestrator = SubAgentOrchestrator(
                config=self.config,
                agents=self.agents,
                runner_factory=factory,
                parent_registry=self.registry,
                emit=bus.emitter(run_id=run_id, agent_id=agent_id),
                cancel_event=handle.cancel_event,
            )
            services["orchestrator"] = orchestrator
            handle.orchestrator = orchestrator

        runner = factory()
        handle.task = asyncio.create_task(
            self._execute(
                runner,
                handle,
                message,
                conversation_id=conversation_id,
                challenge_id=challenge_id,
                agent_id=agent_id,
                agent_prompt=agent_prompt,
                system_prompt=system_prompt,
                tool_names=tool_names,
                services=services,
                attachment_ids=list(attachment_ids or []),
                steering=steering,
                pre_persisted_user_message_id=pre_persisted_user_message_id,
            ),
            name=f"run:{run_id}",
        )
        return handle

    async def attach_manifest(self, message: str, attachment_ids: list[str]) -> str:
        """把附件清单块追加到消息尾部（U2a）。无附件时原样返回。"""
        if not attachment_ids:
            return message
        rows = await self.repos.attachments.list_by_ids(list(attachment_ids))
        block = build_attachment_manifest(rows)
        if not block:
            logger.warning("附件清单为空（ids=%s），按无附件处理", attachment_ids)
            return message
        return f"{message}\n\n{block}"

    async def _execute(
        self,
        runner: AgentRunner,
        handle: RunHandle,
        message: str,
        attachment_ids: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        _attachments = list(attachment_ids or [])
        try:
            message = await self.attach_manifest(message, _attachments)
            result = await runner.run(
                message,
                run_id=handle.run_id,
                emit=handle.bus.emitter(run_id=handle.run_id, agent_id=handle.agent_id),
                cancel_event=handle.cancel_event,
                attachment_ids=_attachments,
                **kwargs,
            )
            handle.result = result
            handle.conversation_id = result.conversation_id
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，保证 SSE 一定能收到 error/done
            logger.exception("运行 %s 失败", handle.run_id)
            await handle.bus.emit(EventType.ERROR, {"message": f"{type(exc).__name__}: {exc}"})
            await handle.bus.emit(EventType.DONE, {"runId": handle.run_id, "status": "error"})
        finally:
            handle.bus.close()

    # ------------------------------------------------------------------
    # v2 挑战会话（blackboard 模式）
    # ------------------------------------------------------------------
    def get_challenge_session(self, challenge_id: str) -> challenge_session.ChallengeSession | None:
        """取挑战会话句柄（可能已结束但仍在内存中，便于查询最终状态）。"""
        return self.challenge_sessions.get(challenge_id)

    def challenge_sessions_info(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.challenge_sessions.values()]

    async def start_challenge_run(
        self,
        challenge_id: str,
        *,
        time_budget_minutes: int | None = None,
        interval_seconds: float | None = None,
        tactic: str | None = None,
    ) -> challenge_session.ChallengeSession:
        """启动一次自主解题会话（Dispatcher 控制回路 + Solver 池 + SSE 总线）。

        合规前置（fail-closed）：题目必须存在且目标范围校验通过，否则抛 ``ValueError``
        （路由层转 400/404）。blackboard 模式要求 ``multi_agent.mode=blackboard``。
        ``tactic`` 为作战图连线带来的战法 ID（见 ``ctfagent.tactics``），只影响
        Worker 的提示词与工具集，不改变调度决策。
        """
        # 战法编排已移除；tactic 参数保留仅为旧客户端兼容，恒被忽略
        _ = tactic
        if self.config.multi_agent.mode != "blackboard":
            raise ValueError(
                "自主解题需要 multi_agent.mode=blackboard（当前为 "
                f"{self.config.multi_agent.mode}）；roles 模式请走 /api/multi-agent/stream"
            )
        if not self.config.state_v2.enabled:
            raise ValueError("state_v2.enabled=false：v2 挑战账本已关闭，无法启动自主解题")

        challenge = await self.repos.challenges.get(challenge_id)
        if challenge is None:
            raise ValueError(f"未知题目：{challenge_id}")
        scope = check_target_scope(challenge)
        if not scope.allowed:
            logger.warning("拒绝启动挑战 %s：%s", challenge_id, scope.reason)
            raise ValueError(f"目标范围校验未通过：{scope.reason}")

        existing = self.challenge_sessions.get(challenge_id)
        if existing is not None and existing.running:
            raise ValueError(f"挑战 {challenge_id} 已在运行中（run={existing.bus.run_id}）")

        if time_budget_minutes is None:
            previous = await self.repos.challenge_sessions.get(challenge_id)
            time_budget_minutes = int(
                (previous or {}).get("timeBudgetMinutes")
                or self.config.termination.default_time_budget_minutes
            )

        run_id = new_id("chalrun_")
        bus = QueueEventBus(run_id=run_id, agent_id="dispatcher")
        pool = SolverPool(
            config=self.config,
            repos=self.repos,
            runner_factory=self.runner_factory,
            emit=bus.emitter(run_id=run_id, agent_id="worker"),
            judge_model=self._observer_judge_model(),
        )
        dispatcher = Dispatcher(
            config=self.config,
            repos=self.repos,
            pool=pool,
            agents=self.agents,
            registry=self.registry,
            execution_service=self.execution_service,
            emit=bus.emitter(run_id=run_id, agent_id="dispatcher"),
            interval_seconds=interval_seconds,
        )
        session = await challenge_session.start_session(
            challenge_id=challenge_id,
            repos=self.repos,
            pool=pool,
            dispatcher=dispatcher,
            bus=bus,
            time_budget_minutes=int(time_budget_minutes),
            tactic_id="",
            tactic_name="",
        )
        self.challenge_sessions[challenge_id] = session
        logger.info(
            "挑战会话启动：%s（run=%s，预算 %s 分钟）",
            challenge_id,
            run_id,
            time_budget_minutes,
        )
        return session

    def board_model(self) -> Any:
        """作战图 AI 推荐用的模型客户端（未配置通道 ⇒ None，调用方降级为规则推荐）。

        与 Observer 判官通道**相互独立**：判官可能被单独关闭，而战法推荐仍应可用；
        反之判官开着也不影响推荐走主通道。
        """
        if self._board_model is not None:
            return self._board_model
        channels = self.config.ai.ordered_channels()
        if not channels:
            return None
        try:
            self._board_model = ResilientModelClient(channels, emit=None)
        except Exception:  # noqa: BLE001 - 构造失败也要能降级
            logger.warning("作战图 AI 推荐客户端构造失败，将使用规则推荐", exc_info=True)
            return None
        return self._board_model

    def _observer_judge_model(self) -> Any:
        """Observer 判官用的小模型通道（未配置/未启用 ⇒ None，判官透明放行）。

        ``observer.judge.model_channel`` 支持按通道名或模型名匹配；匹配不到时打日志并关闭
        判官（fail-open：提醒照发，绝不因为判官缺配置而卡住解题）。
        """
        judge_cfg = self.config.observer.judge
        if not (self.config.observer.enabled and judge_cfg.enabled):
            return None
        if self._judge_model is not None:
            return self._judge_model
        wanted = (judge_cfg.model_channel or "").strip()
        channels = [
            channel
            for channel in self.config.ai.ordered_channels()
            if wanted and (channel.name == wanted or channel.model == wanted)
        ]
        if not channels:
            logger.warning("observer.judge.model_channel=%r 未匹配到任何通道，判官关闭", wanted)
            return None
        self._judge_model = ResilientModelClient(channels, emit=None)
        logger.info("Observer 判官已启用：通道 %s", channels[0].name)
        return self._judge_model

    def stop_challenge_run(self, challenge_id: str, reason: str = "human_stop") -> bool:
        """人工停止挑战（各 Solver 完成 conclude 后归档）。"""
        session = self.challenge_sessions.get(challenge_id)
        if session is None or not session.running:
            return False
        session.request_stop(reason)
        return True

    async def challenge_snapshot(self, challenge_id: str) -> dict[str, Any]:
        """挑战快照：账本 + Solver + 本次会话运行态（供 ``GET /state`` 与前端状态板）。"""
        session = self.challenge_sessions.get(challenge_id)
        if session is not None:
            snapshot = await session.dispatcher.snapshot(challenge_id)
            payload = snapshot.to_dict()
        else:
            # 进程重启后仍可查看历史会话状态（无实时 Solver 池）
            dispatcher = Dispatcher(
                config=self.config,
                repos=self.repos,
                pool=SolverPool(
                    config=self.config,
                    repos=self.repos,
                    runner_factory=self.runner_factory,
                ),
                agents=self.agents,
                registry=self.registry,
                execution_service=self.execution_service,
            )
            snapshot = await dispatcher.snapshot(challenge_id)
            payload = snapshot.to_dict()
        payload["ideas"] = await self.repos.ideas.list(challenge_id, limit=200)
        payload["memories"] = await self.repos.memories.list(challenge_id, limit=100)
        payload["solverRuns"] = await self.repos.solver_runs.list(challenge_id, limit=100)
        payload["flags"] = await self.repos.flags.list_by_challenge(challenge_id, limit=20)
        payload["session"] = await self.repos.challenge_sessions.get(challenge_id)
        payload["live"] = session.to_dict() if session is not None else None
        return payload

    async def add_challenge_hint(
        self, challenge_id: str, text: str, *, author: str = "human"
    ) -> dict[str, Any]:
        """人工提示（Hint 原语）：写入 ``hint`` Memory，并广播 ``hint_added`` 事件。

        只有本方法（人类/系统通道）能写 ``hint``；模型侧 `memory_write` 工具无权伪造。
        """
        clean = " ".join((text or "").split())
        if not clean:
            raise ValueError("提示内容不能为空")
        from .state.memory import MemoryService

        memory = await MemoryService(self.repos).write(
            challenge_id,
            "hint",
            clean,
            body=f"author={author}",
            source="human",
            allow_privileged_source=True,
        )
        session = self.challenge_sessions.get(challenge_id)
        if session is not None:
            await session.bus.emit(
                EventType.HINT_ADDED,
                {
                    "challengeId": challenge_id,
                    "memoryId": memory["id"],
                    "summary": memory["summary"],
                    "author": author,
                },
            )
        # 把提示推给正在跑的 Solver（下一轮渲染块会带上 hint）
        for run in session.pool.active() if session is not None else []:
            await run.add_reminder(f"[人类提示] {clean}", source="human")
        return memory

    async def submit_challenge_flag(
        self, challenge_id: str, value: str, *, source: str = "human"
    ) -> dict[str, Any]:
        """人工提交/确认 flag（触发终止评估器下一轮判定 SUCCESS）。"""
        flag = (value or "").strip()
        if not flag:
            raise ValueError("flag 不能为空")
        challenge = await self.repos.challenges.get(challenge_id)
        pattern = str((challenge or {}).get("flagPattern") or "")
        validated = True
        if pattern:
            import re as _re

            try:
                validated = bool(_re.search(pattern, flag))
            except _re.error:
                validated = True
        record = await self.repos.flags.add(
            value=flag,
            challenge_id=challenge_id,
            fmt=pattern,
            validated=validated,
            source=source,
        )
        session = self.challenge_sessions.get(challenge_id)
        if session is not None:
            await session.bus.emit(
                EventType.PROGRESS,
                {
                    "stage": "flag_submitted",
                    "message": f"人工提交 flag（validated={validated}）",
                    "challengeId": challenge_id,
                    "value": flag,
                },
            )
        return record

    def get_run(self, run_id: str) -> RunHandle | None:
        return self.runs.get(run_id)

    def cancel_run(self, run_id: str) -> bool:
        handle = self.runs.get(run_id)
        if handle is None:
            return False
        handle.cancel_event.set()
        # 取消该运行下所有仍在跑的工具进程
        for record in self.execution_service.active_records():
            if record.run_id == run_id:
                self.execution_service.cancel(record.id)
        return True

    def cancel_running_tools(self, run_id: str) -> int:
        """只取消该运行下进行中的工具执行（**不**置 cancel_event、不结束任务）。

        「中断并继续」用：正在跑的长工具被掐断后以错误结果返回，
        AgentRunner 在中断边界注入用户消息并续跑。
        """
        cancelled = 0
        for record in self.execution_service.active_records():
            if record.run_id == run_id:
                self.execution_service.cancel(record.id)
                cancelled += 1
        return cancelled

    def active_runs(self) -> list[dict[str, Any]]:
        return [
            {
                "runId": h.run_id,
                "conversationId": h.conversation_id,
                "agentId": h.agent_id,
                "orchestration": h.orchestration,
                "elapsedMs": int((time.time() - h.started_at) * 1000),
                "status": "running" if (h.task and not h.task.done()) else "finished",
            }
            for h in self.runs.values()
        ]

    def cleanup_runs(self, keep: int = 50) -> int:
        finished = [h for h in self.runs.values() if h.task and h.task.done()]
        finished.sort(key=lambda h: h.started_at)
        removed = 0
        for handle in finished[:-keep] if keep > 0 else finished:
            self.runs.pop(handle.run_id, None)
            removed += 1
        return removed

    # ------------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "projectRoot": str(self.config.project_root),
            "workspaceRoot": str(self.config.workspace_root),
            "database": str(self.config.database_path),
            "tools": self.registry.summary(),
            "agents": self.agents.ids(),
            "toolLoadErrors": self.tool_load_errors,
            "agentLoadErrors": self.agents.errors,
            "mcp": {"enabled": self.config.mcp.enabled, "tools": len(self.mcp.registered) if self.mcp else 0},
            "uptimeSeconds": int(time.time() - self.started_at),
        }


__all__ = ["RunHandle", "Runtime"]


def _effective_hitl_config(config: AppConfig) -> HITLConfig:
    """v2 开关打开时，把账本工具并入免审白名单。

    账本工具（idea_upsert / idea_transition / memory_write / blackboard_query）只写挑战账本，
    不含命令执行面。若仍按"不在白名单→人工审批"处理，blackboard 模式会卡在审批上；
    关闭 ``state_v2.enabled`` 时本函数原样返回 v1 配置（行为完全一致）。
    """
    if not config.state_v2.enabled:
        return config.hitl
    missing = [name for name in LEDGER_TOOLS if name not in config.hitl.tool_whitelist]
    if not missing:
        return config.hitl
    return config.hitl.model_copy(update={"tool_whitelist": [*config.hitl.tool_whitelist, *missing]})
