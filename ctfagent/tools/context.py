"""工具执行上下文：把配置、仓储、执行服务、工作区与事件发射器打包给工具处理器。

工具处理器签名统一为 ``async def handler(args: dict, ctx: ToolContext) -> ToolOutcome``，
这样内置工具、YAML 配方工具与 MCP 工具可以收敛到同一个工具平面。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import AppConfig
from ..scope import fact_scope
from ..storage.repos import Repos

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from .execution_service import ExecutionService
    from .registry import ToolRegistry

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class ToolContext:
    """一次工具调用所需的全部环境信息。"""

    config: AppConfig
    repos: Repos
    registry: ToolRegistry
    execution_service: ExecutionService
    conversation_id: str
    run_id: str = ""
    agent_id: str = "ctf-agent"
    challenge_id: str | None = None
    #: U4：项目（比赛）维度。默认 None（不带项目 ⇒ 行为与升级前完全一致）；
    #: 由 runtime 之外的调用方（如项目 API / 测试）注入，用于黑板项目级过滤。
    project_id: str | None = None
    #: U6a：会话级审查模式覆盖（human | audit_agent | none；None = 跟随全局 default_reviewer）
    hitl_reviewer: str | None = None
    #: 黑板（facts）共享 scope：子代理派单时指向父会话，实现"事实共享"（None = 用本会话）
    blackboard_conversation_id: str | None = None
    #: U6b：本轮用户消息原文（审计 payload 的 userMessage 字段，用于范围判断）
    user_message: str = ""
    workspace: Path | None = None
    emit: EmitFn | None = None
    cancel_event: asyncio.Event | None = None
    #: 工具被拒绝/超时时由 HITL 注入的标记，便于子代理感知
    services: dict[str, Any] = field(default_factory=dict)

    def resolved_workspace(self) -> Path:
        """当前运行的工作区（惰性创建）。

        U17 会话隔离：绑定题目 → 题目隔离区；未绑定 → **会话私有** ``work`` 目录。
        旧行为把所有未绑定会话指向 ``workspace/challenge/_default``，导致新会话读到
        其它题目的附件与旧 writeup（含已解出的 flag），出现「串题」。
        """
        if self.workspace is not None:
            return self.workspace
        path = self.config.run_workspace(self.challenge_id, self.conversation_id)
        self.workspace = path
        return path

    @property
    def facts_conversation_id(self) -> str:
        """黑板归属的**原始会话 ID**（子代理共享父会话）：反查项目等场景使用。"""
        return self.blackboard_conversation_id or self.conversation_id

    @property
    def blackboard_scope(self) -> str:
        """黑板作用域键（U18）：绑定题目 → ``challenge:<id>``，否则 ``conversation:<cid>``。

        同一题目的所有会话（控制台 Solver、升级自会话的对话、子代理）共享同一作用域，
        不同题目之间完全隔离。
        """
        return fact_scope(self.challenge_id, self.facts_conversation_id)

    async def notify(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        if self.emit:
            await self.emit(event_type, data or {})

    def child(self, **overrides: Any) -> ToolContext:
        """派生一个子上下文（多代理派单时改变 agent_id / workspace / 工具集）。"""
        return replace(self, **overrides)

    def service(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)


__all__ = ["EmitFn", "ToolContext"]
