"""LLM 交互用到的数据类型：ChatMessage / ToolCall / ToolResult / Usage / ModelResponse。

统一使用 pydantic 模型，便于落库（model_dump）与 SSE 序列化（model_dump(mode="json")）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """模型发起的一次工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: 模型返回的原始参数字符串（JSON 解析失败时可回显给模型自查）
    raw_arguments: str = ""

    def to_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments or json.dumps(self.arguments, ensure_ascii=False),
            },
        }

    @classmethod
    def parse_arguments(cls, raw: str | None, fallback: Any = None) -> dict[str, Any]:
        """尽力把模型给的参数字符串解析成 dict；失败时返回 ``{"_raw": raw}``。"""
        if raw is None or raw == "":
            return dict(fallback) if isinstance(fallback, dict) else {}
        if isinstance(raw, dict):  # 部分 SDK/通道直接给 dict
            return raw
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"_raw": str(raw)}
        if isinstance(parsed, dict):
            return parsed
        return {"_value": parsed}


class ChatMessage(BaseModel):
    """一条对话消息（屏蔽各家 SDK 的格式差异）。"""

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    #: reasoning 模型（deepseek-reasoner 等）的思考内容
    reasoning: str | None = None
    #: 是否为上下文压缩后的占位消息
    reduced: bool = False
    created_at: float = Field(default_factory=time.time)

    # -- 构造快捷方式 -----------------------------------------------------
    @classmethod
    def system(cls, content: str) -> ChatMessage:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: list[ToolCall] | None = None) -> ChatMessage:
        return cls(role="assistant", content=content, tool_calls=tool_calls or [])

    @classmethod
    def tool(cls, tool_call_id: str, content: str, name: str | None = None) -> ChatMessage:
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)

    # -- 转换 -------------------------------------------------------------
    def to_openai(self) -> dict[str, Any]:
        if self.role == "tool":
            payload: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": self.tool_call_id or "",
                "content": self.content,
            }
            if self.name:
                payload["name"] = self.name
            return payload
        if self.role == "assistant" and self.tool_calls:
            payload = {
                "role": "assistant",
                "content": self.content or None,
                "tool_calls": [tc.to_openai() for tc in self.tool_calls],
            }
            return payload
        return {"role": self.role, "content": self.content}

    def text_for_estimate(self) -> str:
        """用于 token 估算的文本表示。"""
        parts = [self.content or ""]
        for tc in self.tool_calls:
            parts.append(tc.name)
            parts.append(tc.raw_arguments or json.dumps(tc.arguments, ensure_ascii=False))
        return "\n".join(parts)

    def to_db(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "toolCalls": [tc.model_dump() for tc in self.tool_calls],
            "toolCallId": self.tool_call_id,
            "name": self.name,
            "reasoning": self.reasoning,
        }


class Usage(BaseModel):
    """token 用量与模型调用次数。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0

    def add(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            model_calls=self.model_calls + other.model_calls,
        )

    def accumulate(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.model_calls += other.model_calls


class ModelResponse(BaseModel):
    """一次模型调用的结果。"""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str = ""
    model: str = ""
    #: 原始响应（调试/续跑用，落 trace 时截断）
    raw: dict[str, Any] | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_message(self) -> ChatMessage:
        return ChatMessage.assistant(self.content, self.tool_calls)


class ToolOutcome(BaseModel):
    """工具执行结果（统一收敛 YAML / 内置 / MCP 三类工具）。"""

    content: str = ""
    is_error: bool = False
    execution_id: str | None = None
    persisted_file: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def ok(cls, content: str, **extra: Any) -> ToolOutcome:
        return cls(content=content, is_error=False, extra=extra)

    @classmethod
    def error(cls, content: str, **extra: Any) -> ToolOutcome:
        return cls(content=content, is_error=True, extra=extra)


__all__ = [
    "ChatMessage",
    "ModelResponse",
    "Role",
    "ToolCall",
    "ToolOutcome",
    "Usage",
]
