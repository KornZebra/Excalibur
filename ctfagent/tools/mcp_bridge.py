"""外部 MCP 工具桥接（stdio / SSE / HTTP）。

设计宪法第 2/6 条的延伸：把外部 MCP 服务器暴露的工具也收敛到同一个工具平面，
工具名统一加前缀（默认 ``mcp_{server}_``）避免与内置/YAML 工具冲突。

依赖可选：未安装 ``mcp`` 包或服务器启动失败时**只记录警告并跳过**，
绝不影响主流程（软失败原则）。
"""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack
from typing import Any

from ..config import MCPConfig, MCPServerConfig
from ..models.types import ToolOutcome
from .context import ToolContext
from .guard import soft_failure
from .registry import ToolDefinition, ToolRegistry

logger = logging.getLogger(__name__)


class MCPBridgeError(RuntimeError):
    """MCP 桥接失败。"""


def _mcp_available() -> tuple[bool, str]:
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False, "未安装 mcp 依赖（pip install 'ctf-agent[mcp]'）"
    return True, ""


class MCPBridge:
    """管理多个 MCP 服务器连接与工具注册。"""

    def __init__(self, config: MCPConfig) -> None:
        self.config = config
        self._sessions: dict[str, Any] = {}
        self._stack = AsyncExitStack()
        self.registered: list[str] = []
        self.errors: list[str] = []

    # ------------------------------------------------------------------
    async def start(self, registry: ToolRegistry) -> list[str]:
        """连接全部服务器并注册其工具；返回已注册工具名。"""
        if not self.config.enabled:
            return []
        available, reason = _mcp_available()
        if not available:
            self.errors.append(reason)
            logger.warning("跳过 MCP：%s", reason)
            return []

        for server in self.config.servers:
            try:
                tools = await self._connect(server)
            except Exception as exc:  # noqa: BLE001 - 单个服务器失败不影响整体
                message = f"MCP 服务器 {server.name} 连接失败：{type(exc).__name__}: {exc}"
                logger.warning(message)
                self.errors.append(message)
                continue
            prefix = server.resolved_prefix()
            for tool in tools:
                name = f"{prefix}{tool['name']}"
                registry.register(
                    ToolDefinition(
                        name=name,
                        short_description=(tool.get("description") or tool["name"])[:80],
                        description=tool.get("description")
                        or f"MCP 工具 {tool['name']}（来自 {server.name}）",
                        parameters=tool.get("inputSchema") or {"type": "object", "properties": {}},
                        handler=self._make_handler(server.name, tool["name"]),
                        source="mcp",
                        category="mcp",
                        metadata={"server": server.name, "remoteName": tool["name"]},
                    )
                )
                self.registered.append(name)
        return self.registered

    # ------------------------------------------------------------------
    async def _connect(self, server: MCPServerConfig) -> list[dict[str, Any]]:
        """建立连接并返回远端工具列表。"""
        from mcp import ClientSession  # 延迟导入

        if server.transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(
                command=server.command,
                args=list(server.args),
                env={**dict(os.environ), **(server.env or {})},
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
        elif server.transport == "sse":
            from mcp.client.sse import sse_client

            read, write = await self._stack.enter_async_context(sse_client(server.url))
        else:  # http (streamable)
            from mcp.client.streamable_http import streamablehttp_client

            read, write, _ = await self._stack.enter_async_context(streamablehttp_client(server.url))

        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[server.name] = session
        response = await session.list_tools()
        tools: list[dict[str, Any]] = []
        for item in getattr(response, "tools", []) or []:
            tools.append(
                {
                    "name": getattr(item, "name", ""),
                    "description": getattr(item, "description", "") or "",
                    "inputSchema": getattr(item, "inputSchema", None) or {"type": "object", "properties": {}},
                }
            )
        logger.info("MCP 服务器 %s 提供 %d 个工具", server.name, len(tools))
        return tools

    def _make_handler(self, server_name: str, remote_name: str):
        async def _handler(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
            session = self._sessions.get(server_name)
            if session is None:
                return ToolOutcome.error(soft_failure(f"MCP 服务器 {server_name} 未连接。", tool=remote_name))
            try:
                result = await session.call_tool(remote_name, arguments=args or {})
            except Exception as exc:  # noqa: BLE001 - 转发失败软处理
                return ToolOutcome.error(
                    soft_failure(
                        f"MCP 调用失败：{type(exc).__name__}: {exc}",
                        tool=remote_name,
                        hint="检查外部 MCP 服务器状态，或改用内置工具完成同等操作。",
                    )
                )
            chunks: list[str] = []
            for item in getattr(result, "content", []) or []:
                text = getattr(item, "text", None)
                if text:
                    chunks.append(str(text))
                else:
                    chunks.append(str(item))
            body = "\n".join(chunks) or "(MCP 工具返回空内容)"
            is_error = bool(getattr(result, "isError", False))
            return ToolOutcome(content=body, is_error=is_error)

        return _handler

    # ------------------------------------------------------------------
    async def stop(self) -> None:
        try:
            await self._stack.aclose()
        except Exception:  # noqa: BLE001 - 关闭异常忽略
            logger.debug("关闭 MCP 连接时异常", exc_info=True)
        self._sessions.clear()


__all__ = ["MCPBridge", "MCPBridgeError"]
