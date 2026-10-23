"""Steering（运行中引导）测试夹具：手工装配最小 Runtime + 脚本化假模型 + API 客户端。

不走 ``Runtime.create``（那会加载全量工具/子代理/迁移检查），只装配引导机制依赖的
最小平面：Database/Repos、注册 noop 与 slow 工具的注册表、ExecutionService、
HITL（总开关关闭 = 全部免审）、FakeModel（脚本化响应并**记录每次请求体**）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from ctfagent.agent.hitl import HITLManager
from ctfagent.agents.markdown import AgentRegistry
from ctfagent.app import create_app
from ctfagent.config import AppConfig
from ctfagent.events import Event, EventType, QueueEventBus
from ctfagent.models.types import ModelResponse, ToolCall, ToolOutcome
from ctfagent.runtime import Runtime
from ctfagent.storage.db import Database
from ctfagent.storage.repos import Repos
from ctfagent.tools.context import ToolContext
from ctfagent.tools.execution_service import ExecutionService
from ctfagent.tools.registry import ToolDefinition, ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 假模型
# ---------------------------------------------------------------------------
class _FakeChannel:
    max_total_tokens = 120_000
    max_completion_tokens = 16_384


class FakeModel:
    """脚本化模型：按序消费 steps；step 可以是 ModelResponse 或 ``(model) -> ModelResponse`` 协程函数。

    每次调用都把完整请求体（``[m.to_openai() for m in messages]``）记进 ``requests``，
    供"注入内容出现在后续模型请求上下文"的断言使用。
    """

    def __init__(self, steps: list[Any] | None = None) -> None:
        self.steps: list[Any] = list(steps or [])
        self.requests: list[list[dict[str, Any]]] = []
        self.calls = 0
        self.channel = _FakeChannel()

    def push(self, step: Any) -> None:
        self.steps.append(step)

    async def chat(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
        *,
        on_delta: Any = None,
        on_reasoning: Any = None,
    ) -> ModelResponse:
        self.requests.append([m.to_openai() for m in messages])
        self.calls += 1
        if not self.steps:
            raise AssertionError(f"FakeModel 步骤已耗尽（收到第 {self.calls} 次调用）")
        step = self.steps.pop(0)
        if isinstance(step, ModelResponse):
            return step
        return await step(self)

    async def aclose(self) -> None:
        return None


def text_step(content: str) -> ModelResponse:
    """纯文本响应（无工具调用）→ 在启用 finalizer=false 的测试里结束运行。"""
    return ModelResponse(content=content, finish_reason="stop")


def tool_call_step(call_id: str = "call_1", name: str = "noop") -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments={})],
        finish_reason="tool_calls",
    )


# ---------------------------------------------------------------------------
# Runtime 装配
# ---------------------------------------------------------------------------
async def build_runtime(tmp_path: Path, model: FakeModel) -> Runtime:
    config = AppConfig(db_path=str(tmp_path / "ctfagent.db"), project_root=REPO_ROOT)
    config = config.model_copy(
        update={
            "agent": config.agent.model_copy(
                update={"finalizer_enabled": False, "max_iterations": 50, "max_history_messages": 120}
            ),
            "hitl": config.hitl.model_copy(update={"enabled": False}),
            "multi_agent": config.multi_agent.model_copy(update={"enabled": True}),
        }
    )

    db = Database(config.database_path)
    await db.connect()
    repos = Repos(db)

    registry = ToolRegistry(expose_all=True)

    async def _noop(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome.ok("noop ok")

    registry.register(
        ToolDefinition(
            name="noop",
            short_description="测试用即时工具",
            description="立即返回 noop ok",
            handler=_noop,
            source="builtin",
        )
    )

    slow_release: asyncio.Event = asyncio.Event()

    async def _slow(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        # 一直等到取消信号或测试放行（供 hard 取消测试）
        try:
            await asyncio.wait_for(slow_release.wait(), timeout=30)
        except TimeoutError:
            return ToolOutcome.error("slow tool timeout")
        return ToolOutcome.ok("slow released")

    registry.register(
        ToolDefinition(
            name="slow_tool",
            short_description="测试用可挂起工具",
            description="等待测试放行或被取消",
            handler=_slow,
            source="builtin",
        )
    )

    execution_service = ExecutionService(config, repos=repos, registry=registry)
    hitl = HITLManager(config.hitl, approver=None, repos=repos)

    runtime = Runtime(
        config=config,
        db=db,
        repos=repos,
        registry=registry,
        execution_service=execution_service,
        model=model,  # type: ignore[arg-type]
        agents=AgentRegistry(),
        hitl=hitl,
    )
    runtime._fake_slow_release = slow_release  # type: ignore[attr-defined]  # 测试钩子
    return runtime


@pytest.fixture
async def runtime(tmp_path: Path):
    """带空脚本假模型的 Runtime（每个测试自行 push 步骤，避免步骤串扰）。"""
    model = FakeModel()
    rt = await build_runtime(tmp_path, model)
    try:
        yield rt
    finally:
        await rt.close()


@pytest.fixture
async def client(runtime: Runtime):
    """FastAPI 测试客户端（手工注入 runtime，不触发 lifespan）。"""
    app = create_app(runtime.config)
    app.state.runtime = runtime
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------
async def wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError("等待条件超时")


async def wait_task_done(runtime: Runtime, conversation_id: str, timeout: float = 10.0) -> None:
    """等待会话任务结束（含 watcher 清理会话登记）。"""
    await wait_until(lambda: not runtime.steering.is_running(conversation_id), timeout=timeout)
    # watcher 在任务协程结束后才发布 task_done，多让出一拍
    await asyncio.sleep(0.05)


def hub_events(runtime: Runtime, conversation_id: str) -> list[tuple[int, str, dict[str, Any]]]:
    """读取会话事件枢纽的回放缓冲（白盒便捷断言用）。"""
    hub = runtime.steering.hub(conversation_id)
    return list(hub._buffer)


def events_of_type(runtime: Runtime, conversation_id: str, event_type: str) -> list[dict[str, Any]]:
    return [data for _, etype, data in hub_events(runtime, conversation_id) if etype == event_type]


__all__ = [
    "FakeModel",
    "REPO_ROOT",
    "build_runtime",
    "client",
    "events_of_type",
    "hub_events",
    "runtime",
    "text_step",
    "tool_call_step",
    "wait_task_done",
    "wait_until",
    "Event",
    "EventType",
    "QueueEventBus",
]
