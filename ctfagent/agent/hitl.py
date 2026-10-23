"""HITL 治理：工具调用前拦截（白名单 / 三模式审查分发 / 超时自动拒绝）。

设计宪法第 7 条 + U6 多模式审查：
- 工具调用前拦截（统一拦截层，不散落进执行器）；
- 白名单（只读/低危类）免审；
- reviewer 三模式：``human``（人工逐条确认，v1 既有路径）/ ``audit_agent``（独立审计
  小模型秒级裁决）/ ``none``（完全访问，信任所有命令）；会话级覆盖 > 全局默认；
- ``none`` 只免**审查**，不免**治理**：硬超时、无输出终止、workspace 隔离照常生效；
- 超时**自动拒绝**（fail-closed）；拒绝理由作为文本反馈给模型，让循环继续迭代。

危险命令模式命中时，即使工具在白名单内也强制走审批（none 模式下由 policy 放行）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ..config import HITLConfig
from ..constants import HITL_REJECTED_PREFIX, TOOL_ERROR_PREFIX
from ..models.types import ToolOutcome
from ..storage.repos import new_id
from ..tools.guard import detect_dangerous_command, is_readonly_command

if TYPE_CHECKING:  # pragma: no cover
    from ..storage.repos import Repos
    from ..tools.context import ToolContext
    from ..tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

#: U6 reviewer 取值（全局默认与会话覆盖共用）
REVIEWERS = ("human", "audit_agent", "none")


@dataclass
class HITLRequest:
    """一次待审批的工具调用。"""

    id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    command: str = ""
    reason: str = ""
    run_id: str = ""
    conversation_id: str = ""
    agent_id: str = "ctf-agent"
    timeout_seconds: int = 120
    created_at: float = field(default_factory=time.time)

    @property
    def timeout_at_ms(self) -> int:
        return int((self.created_at + self.timeout_seconds) * 1000)

    def to_event(self) -> dict[str, Any]:
        return {
            "requestId": self.id,
            "toolCallId": self.tool_call_id,
            "name": self.tool_name,
            "arguments": self.arguments,
            "command": self.command,
            "reason": self.reason,
            "agent": self.agent_id,
            "timeoutAt": self.timeout_at_ms,
            "timeoutSeconds": self.timeout_seconds,
            "reviewer": "human",  # U6c：走人工队列的请求固定 human（审计/策略裁决不发此事件）
        }


@dataclass
class HITLDecision:
    """审批结论。"""

    approved: bool = True
    reason: str = ""
    arguments: dict[str, Any] | None = None
    modified: bool = False
    auto: bool = False
    #: U6a：本条裁决在 hitl_decisions 表中的记录 id（闭环回填执行结果用）
    decision_id: str = ""
    #: U6a：裁决耗时（毫秒，从拦截开始算；落库与前端卡片展示用）
    elapsed_ms: int = 0
    #: U6c：审计改参放行时的原始参数（前端 diff 展示用；人工改参不填）
    original_arguments: dict[str, Any] | None = None

    @property
    def rejected(self) -> bool:
        return not self.approved

    def feedback(self) -> str:
        """拒绝时反馈给模型的文本。

        软失败协议要求所有工具错误都以 ``[TOOL_ERROR]`` 开头（模型据此判定"可继续、需换路"），
        因此这里在 HITL 专用前缀之前再叠加统一的错误前缀。
        """
        base = f"{TOOL_ERROR_PREFIX} {HITL_REJECTED_PREFIX} 工具调用未获批准：{self.reason or '用户拒绝'}"
        base += (
            "\n请不要重复同一调用。可选做法：换用只读工具（bash_readonly/read_file/grep）获取等价信息、"
            "缩小命令影响范围后重试，或向用户说明该步骤的必要性。"
        )
        return base


class Approver(Protocol):
    """审批人协议（CLI / Web / 测试可各自实现）。"""

    async def __call__(self, request: HITLRequest) -> HITLDecision: ...


class CLIApprover:
    """CLI 交互式审批。"""

    def __init__(self, *, prompt_prefix: str = "[HITL]") -> None:
        self.prompt_prefix = prompt_prefix

    async def __call__(self, request: HITLRequest) -> HITLDecision:
        question = (
            f"\n{self.prompt_prefix} 需要批准工具调用：\n"
            f"  工具：{request.tool_name}\n"
            f"  命令/参数：{request.command or request.arguments}\n"
            f"  原因：{request.reason or '(白名单外工具)'}\n"
            f"批准执行？[y=批准 / n=拒绝 / e=改参批准] "
        )
        answer = (await asyncio.to_thread(input, question)).strip().lower()
        if answer in {"y", "yes", "1"}:
            return HITLDecision(approved=True, reason="CLI 批准")
        if answer in {"e", "edit"}:
            raw = (await asyncio.to_thread(input, "请输入新的参数 JSON：")).strip()
            import json

            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                return HITLDecision(approved=False, reason="改参解析失败，视为拒绝")
            return HITLDecision(approved=True, reason="CLI 改参批准", arguments=arguments, modified=True)
        return HITLDecision(approved=False, reason="CLI 拒绝")


class AutoApprover:
    """自动批准（仅用于本地靶场调试 / hitl.mode=off）。"""

    def __init__(self, reason: str = "hitl 已关闭，自动批准") -> None:
        self.reason = reason

    async def __call__(self, request: HITLRequest) -> HITLDecision:
        return HITLDecision(approved=True, reason=self.reason, auto=True)


ApproverFn = Callable[[HITLRequest], Awaitable[HITLDecision]]


class HITLManager:
    """审批管理器：判定是否需要审批、三路分发（human / audit_agent / none）、
    等待结论、超时拒绝、供 Web 端回填决定，并把每条裁决落库（U6a）。

    **回归锚点**：reviewer=``human``（v1 默认迁移后的等价配置）下的人工审批路径
    与改造前逐字节行为一致。
    """

    def __init__(
        self,
        config: HITLConfig,
        *,
        approver: ApproverFn | None = None,
        repos: Repos | None = None,
        auditor: Any = None,
    ) -> None:
        self.config = config
        self.approver = approver
        self.repos = repos
        self.auditor = auditor
        self._pending: dict[str, asyncio.Future[HITLDecision]] = {}
        self._requests: dict[str, HITLRequest] = {}
        self._history: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    def should_review(self, tool: ToolDefinition, arguments: dict[str, Any]) -> tuple[bool, str]:
        """判断是否需要审批（决定"是否升级到 reviewer"，"谁来审"由 reviewer 决定）。

        v1 的 ``hitl.mode``（off/all/whitelist）已弃用：off 的语义由
        ``default_reviewer=none`` 承接，all/whitelist 统一映射为 human；
        ``hitl.enabled`` 仍是总开关（false 时全部免审，行为与 v1 一致）。
        """
        if not self.config.enabled:
            return False, ""
        command = str(arguments.get("command") or arguments.get("code") or "")
        danger = detect_dangerous_command(command) if command else None
        if danger:
            return True, f"命中高危命令模式（{danger}）"
        gate = self.config.readonly_gate
        whitelist = set(self.config.tool_whitelist)
        if tool.name in whitelist:
            readonly_ok = (
                not gate.enabled
                or is_readonly_command(command, extra_allowlist=gate.extra_allowlist)
            )
            if tool.name == "bash_readonly":
                # 只读外壳仍需检查命令本身：命中非只读操作时不走免审
                if readonly_ok:
                    return False, "白名单只读命令免审" if gate.enabled else "只读校验已关闭，白名单免审"
                return True, f"bash_readonly 的命令未通过只读校验，需人工确认：{command[:80]}"
            if not command or readonly_ok:
                return False, "白名单免审"
            return True, f"白名单工具 {tool.name} 的参数包含非只读操作，需人工确认"
        if tool.dangerous:
            return True, "工具被标记为危险操作"
        return True, "该工具不在免审白名单，需人工确认"

    # ------------------------------------------------------------------
    async def intercept(
        self,
        *,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        tool_call_id: str,
        ctx: ToolContext,
        command: str = "",
    ) -> HITLDecision:
        """拦截一次工具调用：三路分发（U6a），返回审批结论。"""
        started = time.monotonic()
        need, reason = self.should_review(tool, arguments)
        if not need:
            # 既有豁免逻辑不变（含 bash_readonly 只读校验 / enabled=false 总开关）
            reviewer = "policy" if not self.config.enabled else "whitelist"
            comment = reason or ("hitl.enabled=false：全部免审" if reviewer == "policy" else "白名单免审")
            decision = await self._record_and_notify(
                ctx,
                tool=tool,
                arguments=arguments,
                tool_call_id=tool_call_id,
                command=command,
                decision=HITLDecision(approved=True, reason=reason, auto=True),
                reviewer=reviewer,
                comment=comment,
                elapsed_ms=0,
            )
            return decision

        reviewer = await self._effective_reviewer(ctx)
        if reviewer == "none":
            # 完全访问：只免审查，不免治理（执行治理在 execution_service 照常生效）
            decision = await self._record_and_notify(
                ctx,
                tool=tool,
                arguments=arguments,
                tool_call_id=tool_call_id,
                command=command,
                decision=HITLDecision(
                    approved=True, reason="完全访问模式（reviewer=none）免审放行", auto=True
                ),
                reviewer="policy",
                comment="完全访问模式",
                elapsed_ms=0,
            )
            return decision
        if reviewer == "audit_agent":
            audit_decision = await self._audit_review(
                tool, arguments, tool_call_id, ctx, command, reason, started
            )
            if audit_decision is not None:
                return audit_decision
            # on_failure=fallback_human（或审计器未装配）→ 转既有的人工队列
            return await self._human_review(
                tool, arguments, tool_call_id, ctx, command, reason, started
            )
        return await self._human_review(tool, arguments, tool_call_id, ctx, command, reason, started)

    # ------------------------------------------------------------------
    async def _effective_reviewer(self, ctx: ToolContext) -> str:
        """会话级覆盖 > 全局默认（U6a §2.2）。

        会话值每次拦截时从库中**现读**：前端承诺"切换审查模式对后续工具调用立即生效"，
        而 run 启动时的快照（``ctx.hitl_reviewer``）无法覆盖长跑 run 中途的切换；
        现读失败时退回快照，再退回全局默认。
        """
        session_snapshot = getattr(ctx, "hitl_reviewer", None)
        conversation_id = getattr(ctx, "conversation_id", "")
        repos = getattr(ctx, "repos", None)
        if conversation_id and repos is not None:
            try:
                conversation = await repos.conversations.get(conversation_id)
                live_value = (conversation or {}).get("hitlReviewer")
                if live_value in REVIEWERS:
                    return str(live_value)
            except Exception:  # noqa: BLE001 - 现读失败不阻断拦截，退回快照
                logger.debug("现读会话审查模式失败，退回 run 启动快照", exc_info=True)
        if session_snapshot in REVIEWERS:
            return str(session_snapshot)
        return self.config.default_reviewer if self.config.default_reviewer in REVIEWERS else "human"

    # ------------------------------------------------------------------
    async def _human_review(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        tool_call_id: str,
        ctx: ToolContext,
        command: str,
        reason: str,
        started: float,
    ) -> HITLDecision:
        """v1 既有的人工审批路径（回归基线：与改造前逐字节一致，仅新增落库）。"""
        request = HITLRequest(
            id=new_id("hitl_"),
            tool_call_id=tool_call_id,
            tool_name=tool.name,
            arguments=dict(arguments or {}),
            command=command,
            reason=reason,
            run_id=ctx.run_id,
            conversation_id=ctx.conversation_id,
            agent_id=ctx.agent_id,
            timeout_seconds=int(self.config.timeout_seconds),
        )
        self._requests[request.id] = request
        await ctx.notify("hitl_request", request.to_event())
        logger.info("等待 HITL 审批：%s(%s) 原因=%s", tool.name, command[:120], reason)

        decision = await self._await_decision(request)
        decision.elapsed_ms = int((time.monotonic() - started) * 1000)
        await self._record(
            ctx,
            tool=tool,
            arguments=arguments,
            tool_call_id=tool_call_id,
            command=command,
            decision=decision,
            reviewer="human",
            comment=decision.reason,
            mode=await self._effective_reviewer(ctx),
            elapsed_ms=decision.elapsed_ms,
        )
        self._history.append(
            {
                "requestId": request.id,
                "toolName": tool.name,
                "approved": decision.approved,
                "reason": decision.reason,
                "modified": decision.modified,
                "at": time.time(),
            }
        )
        await ctx.notify(
            "hitl_resolved",
            {
                "requestId": request.id,
                "toolCallId": tool_call_id,
                "approved": decision.approved,
                "reason": decision.reason,
            },
        )
        self._pending.pop(request.id, None)
        return decision

    # ------------------------------------------------------------------
    async def _audit_review(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        tool_call_id: str,
        ctx: ToolContext,
        command: str,
        reason: str,
        started: float,
    ) -> HITLDecision | None:
        """U6b：Agent 快速审查。审计器未装配或 on_failure=fallback_human 时返回 None。"""
        if self.auditor is None:
            logger.warning("reviewer=audit_agent 但审计器未装配，转人工审批（fail-closed）")
            return None
        decision = await self.auditor.review(
            tool=tool,
            arguments=arguments,
            tool_call_id=tool_call_id,
            ctx=ctx,
            command=command,
            reason=reason,
            mode_snapshot=await self._effective_reviewer(ctx),
        )
        if decision is None:  # fallback_human
            return None
        if decision.elapsed_ms <= 0:
            decision.elapsed_ms = int((time.monotonic() - started) * 1000)
        self._history.append(
            {
                "toolName": tool.name,
                "approved": decision.approved,
                "reason": decision.reason,
                "modified": decision.modified,
                "decidedBy": "audit_agent",
                "at": time.time(),
            }
        )
        return decision

    # ------------------------------------------------------------------
    async def _record_and_notify(
        self,
        ctx: ToolContext,
        *,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        tool_call_id: str,
        command: str,
        decision: HITLDecision,
        reviewer: str,
        comment: str,
        elapsed_ms: int,
    ) -> HITLDecision:
        """whitelist / policy 放行：落库 + 发 hitl_auto_decision（灰字徽标）。"""
        mode = await self._effective_reviewer(ctx)
        decision.elapsed_ms = elapsed_ms
        await self._record(
            ctx,
            tool=tool,
            arguments=arguments,
            tool_call_id=tool_call_id,
            command=command,
            decision=decision,
            reviewer=reviewer,
            comment=comment,
            mode=mode,
            elapsed_ms=elapsed_ms,
        )
        await ctx.notify(
            "hitl_auto_decision",
            {
                "toolCallId": tool_call_id,
                "toolName": tool.name,
                "decision": "approve",
                "comment": comment,
                "decidedBy": reviewer,
                "elapsedMs": elapsed_ms,
                "edited": False,
            },
        )
        return decision

    # ------------------------------------------------------------------
    async def _record(
        self,
        ctx: ToolContext,
        *,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        tool_call_id: str,
        command: str,
        decision: HITLDecision,
        reviewer: str,
        comment: str,
        mode: str,
        elapsed_ms: int | None,
        edited_arguments: dict[str, Any] | None = None,
    ) -> None:
        """裁决落库（U6a §2.4）。落库失败绝不影响解题主流程。"""
        if self.repos is None:
            return
        try:
            record = await self.repos.hitl_decisions.save(
                conversation_id=ctx.conversation_id,
                tool_name=tool.name,
                reviewer=reviewer,
                decision="approve" if decision.approved else "reject",
                tool_call_id=tool_call_id,
                run_id=ctx.run_id,
                arguments=dict(arguments or {}),
                mode=mode,
                comment=comment,
                edited_arguments=edited_arguments,
                elapsed_ms=elapsed_ms,
            )
            decision.decision_id = record["id"]
        except Exception:  # noqa: BLE001 - 留痕失败不阻断解题
            logger.warning("审查裁决落库失败（不影响执行）", exc_info=True)

    # ------------------------------------------------------------------
    async def attach_execution(self, decision_id: str, execution_id: str | None, result_digest: str) -> None:
        """闭环回填（U6b §3.4）：放行的调用在执行完成后把结果摘要合并进审查记录。"""
        if not decision_id or self.repos is None or not execution_id:
            return
        try:
            await self.repos.hitl_decisions.attach_execution(decision_id, execution_id, result_digest)
        except Exception:  # noqa: BLE001 - 回填失败不影响主流程
            logger.debug("审查闭环回填失败 decision=%s", decision_id, exc_info=True)

    # ------------------------------------------------------------------
    async def _await_decision(self, request: HITLRequest) -> HITLDecision:
        timeout = max(1, request.timeout_seconds)
        try:
            if self.approver is not None:
                return await asyncio.wait_for(self.approver(request), timeout=timeout)
            loop = asyncio.get_running_loop()
            future: asyncio.Future[HITLDecision] = loop.create_future()
            self._pending[request.id] = future
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError:
            return HITLDecision(
                approved=False,
                reason=f"审批超时（{timeout}s 内无响应）自动拒绝",
                auto=True,
            )
        except asyncio.CancelledError:
            return HITLDecision(approved=False, reason="运行被取消，工具调用一并取消", auto=True)
        except Exception as exc:  # noqa: BLE001 - 审批通道异常必须 fail-closed
            logger.warning("审批通道异常，按拒绝处理：%s", exc)
            return HITLDecision(
                approved=False, reason=f"审批通道异常（{type(exc).__name__}），按拒绝处理", auto=True
            )

    # ------------------------------------------------------------------
    # Web 端回填
    # ------------------------------------------------------------------
    def resolve(self, request_id: str, decision: HITLDecision) -> bool:
        """由 Web API 回填审批结论。"""
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    def pending_requests(self) -> list[HITLRequest]:
        return [self._requests[r] for r in self._pending if r in self._requests]

    def get_request(self, request_id: str) -> HITLRequest | None:
        return self._requests.get(request_id)

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)


def rejected_outcome(decision: HITLDecision, tool_name: str) -> ToolOutcome:
    """把拒绝结论转成回填给模型的工具结果。"""
    return ToolOutcome(
        content=decision.feedback(), is_error=True, extra={"hitl": "rejected", "tool": tool_name}
    )


__all__ = [
    "Approver",
    "ApproverFn",
    "AutoApprover",
    "CLIApprover",
    "HITLDecision",
    "HITLManager",
    "HITLRequest",
    "rejected_outcome",
]
