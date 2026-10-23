"""Deep 式编排：``task`` 工具 + 子代理生命周期（禁止嵌套派单）。

设计宪法与提示词 §6.2 的落地：

- ``task`` 工具**只有主代理**可见/可用（子代理的工具列表强制剔除 ``task``，从机制上禁止嵌套派单）；
- 每个子代理跑在**独立会话**（消息独立落库，便于前端分组与事后审计）；
- 子代理继承同一个题目工作区与黑板（事实共享，避免重复劳动）；
- 子代理的流式回复通过 ``sub_agent_reply_stream_delta`` 事件转发给父级时间线；
- 交付格式校验：缺少 ``## 结论`` / ``## 证据`` 段落时在结果里追加校验提示。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..config import AppConfig
from ..events import EventType
from ..models.types import ToolOutcome
from ..orchestrator import solver_pool
from ..storage.repos import new_id
from ..tools.context import ToolContext
from ..tools.guard import soft_failure
from ..tools.registry import ToolDefinition
from .markdown import AgentDefinition, AgentRegistry

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

#: 永远不暴露给子代理的工具（禁止嵌套派单）
FORBIDDEN_SUB_AGENT_TOOLS: frozenset[str] = frozenset({"task"})

TASK_HANDOFF_TEMPLATE_MISSING = (
    "交接包不完整：description 必须包含【题目上下文】【本轮唯一子目标与成功标准】"
    "【附件 workspace 绝对路径】【禁止事项】四部分。"
)


@dataclass
class SubAgentRun:
    """一次子代理派单的结果摘要。"""

    agent_id: str
    agent_name: str
    conversation_id: str
    run_id: str
    content: str
    status: str
    iterations: int
    missing_sections: list[str]


#: 运行器工厂签名：``(config, model, registry, execution_service, repos, hitl) -> AgentRunner``
RunnerFactory = Callable[..., Any]


class SubAgentOrchestrator:
    """构造 ``task`` 工具并驱动子代理运行。"""

    def __init__(
        self,
        *,
        config: AppConfig,
        agents: AgentRegistry,
        runner_factory: RunnerFactory,
        parent_registry: Any,
        emit: EmitFn,
        cancel_event: asyncio.Event | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.config = config
        self.agents = agents
        self.runner_factory = runner_factory
        self.parent_registry = parent_registry
        self.emit = emit
        self.cancel_event = cancel_event or asyncio.Event()
        self.semaphore = semaphore or asyncio.Semaphore(max(1, config.multi_agent.max_concurrent_sub_agents))
        self.runs: list[SubAgentRun] = []

    # ------------------------------------------------------------------
    def available_agents(self) -> list[AgentDefinition]:
        """可派单的专项子代理。"""
        return self.agents.sub_agents()

    def make_task_tool(self) -> ToolDefinition:
        """构造主代理专用的 ``task`` 工具。"""
        agent_ids = [a.id for a in self.available_agents()]

        async def _task(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
            return await self.run_task(args, ctx)

        return ToolDefinition(
            name="task",
            short_description="派发专项子代理（每个子代理只看到你传入的 description，看不到父对话）",
            description=(
                "把一个明确的子目标派发给专项子代理并在同一轮里拿到结构化交付物。"
                "description 必须写全交接包：题目上下文（题目名/分类/描述要点/已确认信息/已排除假设）、"
                "本轮唯一子目标与成功标准、附件的 workspace 绝对路径、禁止事项。"
                "互不依赖的子目标应在同一条回复里并行多次调用 task。禁止嵌套派单。\n"
                f"可用子代理 id：{', '.join(agent_ids) or '（未加载到 subagent 定义）'}"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "子代理 id（recon/web/pwn/crypto/reverse/forensics/misc）",
                        "enum": agent_ids or None,
                    },
                    "description": {
                        "type": "string",
                        "description": "完整交接包：题目上下文 + 本轮唯一子目标与成功标准 + 附件 workspace 绝对路径 + 禁止事项",
                    },
                    "expected_deliverable": {
                        "type": "string",
                        "description": "期望交付物形态（如「可复现的利用命令与 flag 片段」）",
                    },
                    "max_iterations": {
                        "type": "integer",
                        "description": "本子代理迭代上限（默认取配置 sub_agent_max_iterations）",
                    },
                },
                "required": ["agent", "description"],
            },
            handler=_task,
            source="builtin",
            category="multi_agent",
            always_expose=True,
        )

    # ------------------------------------------------------------------
    async def run_task(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        """执行一次派单。"""
        if not self.config.multi_agent.enabled:
            return ToolOutcome.error(
                soft_failure(
                    "多代理模式未启用。", tool="task", hint="在 config.yaml 中设置 multi_agent.enabled=true。"
                )
            )

        requested = str(args.get("agent") or "").strip()
        agent = self.agents.get(requested)
        if agent is None:
            available = ", ".join(a.id for a in self.available_agents()) or "（无）"
            return ToolOutcome.error(
                soft_failure(
                    f"未知子代理：{requested or '(空)'}",
                    tool="task",
                    hint=f"可用子代理：{available}",
                )
            )
        if agent.id in {"ctf-orchestrator", "orchestrator"}:
            return ToolOutcome.error(
                soft_failure("不能把任务派给编排代理本身（禁止嵌套派单）。", tool="task")
            )

        description = str(args.get("description") or "").strip()
        if len(description) < 40:
            return ToolOutcome.error(
                soft_failure(TASK_HANDOFF_TEMPLATE_MISSING, tool="task", hint="补齐四部分后重新派单。")
            )

        expected = str(args.get("expected_deliverable") or "").strip()
        max_iterations = args.get("max_iterations")
        try:
            max_iter = int(max_iterations) if max_iterations not in (None, "") else None
        except (TypeError, ValueError):
            max_iter = None
        if max_iter is None:
            max_iter = agent.max_iterations or self.config.agent.sub_agent_max_iterations

        # 工具过滤：白名单 ∩ 已注册工具，并强制剔除 task（禁止嵌套）
        tool_names = agent.tool_filter()
        if tool_names is not None:
            tool_names = [n for n in tool_names if n not in FORBIDDEN_SUB_AGENT_TOOLS]
        else:
            tool_names = [
                t.name for t in self.parent_registry.all() if t.name not in FORBIDDEN_SUB_AGENT_TOOLS
            ]

        handoff = description
        if expected:
            handoff += f"\n\n【期望交付物】{expected}"
        handoff += (
            f"\n\n【工作区】{ctx.resolved_workspace()}"
            f"\n【会话】父会话 {ctx.conversation_id}；你拥有独立会话"
            f"\n【交付格式】必须包含 ## 结论 / ## 证据（命令+关键输出+置信度）/ ## 风险与不确定性 / ## 建议下一步"
        )
        if ctx.challenge_id:
            challenge = await ctx.repos.challenges.get(ctx.challenge_id)
            if challenge:
                handoff += (
                    f"\n【题目】{challenge['name']}（{challenge['category']}，{challenge['points']}分）"
                    f" target={challenge['target'] or '未填写'}"
                )

        sub_run_id = new_id("run_")
        agent_name = agent.display_name
        # 继承父会话审查模式：派发时现读库中最新值（ctx.hitl_reviewer 是 run 启动快照，
        # 可能已因运行中切换而过期）；读不到回退快照
        parent_reviewer = getattr(ctx, "hitl_reviewer", None)
        try:
            parent_conv = await ctx.repos.conversations.get(ctx.conversation_id)
            parent_reviewer = (parent_conv or {}).get("hitlReviewer") or parent_reviewer
        except Exception:  # noqa: BLE001 - 现读失败退回快照，不阻断派单
            logger.debug("派发子代理前现读父会话审查模式失败", exc_info=True)
        await ctx.notify(
            "progress",
            {
                "stage": "sub_agent_start",
                "message": f"派发子代理 {agent_name}（{agent.id}）",
                "agent": agent.id,
                "tool": "task",
            },
        )

        async def child_emit(event_type: str, data: dict[str, Any]) -> None:
            """把子代理事件转发到父时间线。"""
            payload = dict(data or {})
            if event_type == EventType.RESPONSE_DELTA:
                await self.emit(
                    EventType.SUB_AGENT_REPLY_STREAM_DELTA,
                    {
                        "agentId": agent.id,
                        "agentName": agent_name,
                        "delta": payload.get("delta", ""),
                        "kind": "response",
                    },
                )
                return
            if event_type == EventType.THINKING_STREAM_DELTA:
                await self.emit(
                    EventType.SUB_AGENT_REPLY_STREAM_DELTA,
                    {
                        "agentId": agent.id,
                        "agentName": agent_name,
                        "delta": payload.get("delta", ""),
                        "kind": "thinking",
                    },
                )
                return
            # 工具/进度/审批/用量等事件带上 agent 标识，前端据此分组
            payload["agent"] = agent.id
            payload["agentName"] = agent_name
            await self.emit(event_type, payload)

        try:
            async with self.semaphore:
                runner = self.runner_factory()
                # v2：账本工具可用时走 SolverRun 包装器（拿到"失败即产出"的收尾能力）；
                # 否则完全走 v1 原路径（不写 solver_runs、不做额外模型调用）。
                if ctx.challenge_id and solver_pool.should_wrap(config=self.config, tool_names=tool_names):
                    solver = solver_pool.SolverRun(
                        spec=solver_pool.SolverSpec(
                            challenge_id=ctx.challenge_id,
                            task_type="reason",
                            handoff=handoff,
                            agent_id=agent.id,
                            agent_prompt=agent.system_prompt,
                            tool_names=tool_names,
                            max_iterations=max_iter,
                            conversation_title=f"[{agent.id}] {description[:40]}",
                            parent_conversation_id=ctx.conversation_id,
                            hitl_reviewer=parent_reviewer,
                            # 黑板共享：子代理的事实读写都落到父会话的黑板
                            blackboard_conversation_id=ctx.conversation_id,
                            run_id=sub_run_id,
                            services=ctx.services,
                        ),
                        config=self.config,
                        repos=ctx.repos,
                        runner=runner,
                        emit=child_emit,
                        cancel_event=self.cancel_event,
                    )
                    solver_result = await solver.run()
                    if solver_result.run_result is None:
                        raise RuntimeError(solver_result.error or "SolverRun 未返回运行结果")
                    result = solver_result.run_result
                else:
                    result = await runner.run(
                        handoff,
                        conversation_id=None,
                        challenge_id=ctx.challenge_id,
                        agent_id=agent.id,
                        agent_prompt=agent.system_prompt,
                        tool_names=tool_names,
                        max_iterations=max_iter,
                        emit=child_emit,
                        cancel_event=self.cancel_event,
                        is_sub_agent=True,
                        new_conversation=True,
                        conversation_title=f"[{agent.id}] {description[:40]}",
                        parent_conversation_id=ctx.conversation_id,
                        # 审查模式随会话继承：子代理不继承会回退全局默认，
                        # 造成"父会话已切审查模式、子代理工具调用仍弹人工确认框"
                        hitl_reviewer=parent_reviewer,
                        # 黑板共享：子代理的事实读写都落到父会话的黑板
                        blackboard_conversation_id=ctx.conversation_id,
                        run_id=sub_run_id,
                    )
        except Exception as exc:  # noqa: BLE001 - 子代理失败不能让父代理崩
            logger.exception("子代理 %s 运行失败", agent.id)
            return ToolOutcome.error(
                soft_failure(
                    f"子代理 {agent_name} 运行失败：{type(exc).__name__}: {exc}",
                    tool="task",
                    hint="可缩小任务范围后重试，或亲自用工具完成该子目标。",
                )
            )

        missing = agent.validate_delivery(result.content)
        content = result.content
        if missing:
            content = (
                f"[交付格式校验] 子代理 {agent_name} 的交付缺少必需段落：{', '.join(missing)}。"
                "请在本轮汇总时补全，或派补充 task 要求其按格式重交。\n\n" + content
            )
        record = SubAgentRun(
            agent_id=agent.id,
            agent_name=agent_name,
            conversation_id=result.conversation_id,
            run_id=result.run_id,
            content=result.content,
            status=result.status,
            iterations=result.iterations,
            missing_sections=missing,
        )
        self.runs.append(record)
        await ctx.notify(
            "progress",
            {
                "stage": "sub_agent_done",
                "message": f"子代理 {agent_name} 结束（status={result.status}，{result.iterations} 步）",
                "agent": agent.id,
                "tool": "task",
            },
        )

        header = (
            f"# 子代理 {agent_name}（{agent.id}）交付\n"
            f"- 子会话：{result.conversation_id}\n"
            f"- 状态：{result.status}；迭代：{result.iterations}；工具调用：{result.tool_calls}\n"
            f"- 独立 trace：{result.trace_path or '(未生成)'}\n"
        )
        if result.flags:
            header += f"- 该子代理记录的 flag：{result.flags}\n"
        header += "\n---\n\n"
        return ToolOutcome.ok(
            header + content,
            subAgent=agent.id,
            subConversationId=result.conversation_id,
            missingSections=missing,
        )

    # ------------------------------------------------------------------
    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "agentId": r.agent_id,
                "agentName": r.agent_name,
                "conversationId": r.conversation_id,
                "runId": r.run_id,
                "status": r.status,
                "iterations": r.iterations,
                "missingSections": r.missing_sections,
            }
            for r in self.runs
        ]


# ---------------------------------------------------------------------------
# plan_execute 变体
# ---------------------------------------------------------------------------
PLAN_EXECUTE_INSTRUCTION = """

## 本轮编排模式：plan_execute（先计划后执行）
你必须严格分两阶段工作：
1. **计划阶段**：接到题目后，先用 write_todos 写出完整计划（每步写清"要验证什么假设、用什么工具、成功判据"），
   并在回复中给出「## 执行计划」小节。此阶段不要派发 task。
2. **执行阶段**：计划确定后再开始并行/串行派发 task，每完成一步更新 todo 状态。
若执行中计划需要调整，先更新 write_todos 并说明调整原因，再继续执行。
"""


def apply_orchestration_mode(prompt: str, mode: str) -> str:
    """按 orchestration 参数调整编排提示词。"""
    if mode == "plan_execute":
        return prompt.rstrip() + PLAN_EXECUTE_INSTRUCTION
    return prompt


__all__ = [
    "FORBIDDEN_SUB_AGENT_TOOLS",
    "PLAN_EXECUTE_INSTRUCTION",
    "SubAgentOrchestrator",
    "SubAgentRun",
    "apply_orchestration_mode",
]
