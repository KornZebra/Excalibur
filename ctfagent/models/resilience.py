"""模型调用弹性层：指数退避重试、通道级 failover、上下文超限识别。

调用方（Agent 循环）的契约：
- 上下文超限 → 抛 :class:`ContextOverflowError`，由 ``agent.context`` 压缩历史后重试；
- 其余可重试错误 → 本层内部消化（退避 + 换通道）；
- 全部通道失败 → 抛 :class:`ModelCallError`，循环把它转成对模型/用户友好的错误事件。
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..config import ChannelConfig, secret_problem
from .client import ModelClient, build_model_client
from .types import ChatMessage, ModelResponse

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None] | None]

#: 上下文超限的典型错误特征（各家 SDK / 网关措辞不同）
CONTEXT_OVERFLOW_PATTERNS: tuple[str, ...] = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "reduce the length",
    "too many tokens",
    "token limit",
    "context window",
    "prompt is too long",
    "input is too long",
    "maximum number of tokens",
)

#: 可重试的瞬时错误特征
RETRYABLE_PATTERNS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "rate limit",
    "429",
    "500",
    "502",
    "503",
    "504",
    "overloaded",
    "connection",
    "temporarily",
    "server error",
    "internal error",
    "eof occurred",
    "read timeout",
    "service unavailable",
)


class ModelCallError(RuntimeError):
    """所有通道与重试都失败。"""


class ContextOverflowError(RuntimeError):
    """请求超出模型上下文窗口，需要压缩历史后重试。"""


def is_context_overflow_error(exc: BaseException) -> bool:
    """判断异常是否为上下文超限。"""
    if isinstance(exc, ContextOverflowError):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    if "context" in text and ("length" in text or "window" in text or "tokens" in text):
        return True
    return any(p in text for p in CONTEXT_OVERFLOW_PATTERNS)


def is_retryable_error(exc: BaseException) -> bool:
    """判断异常是否值得退避重试。"""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(p in text for p in RETRYABLE_PATTERNS)


@dataclass
class RetryPolicy:
    """指数退避策略。"""

    max_retries: int = 3
    base_delay: float = 1.5
    max_delay: float = 30.0
    jitter: float = 0.3

    def delay_for(self, attempt: int) -> float:
        raw = min(self.base_delay * (2 ** max(0, attempt - 1)), self.max_delay)
        return raw * (1 + random.uniform(-self.jitter, self.jitter))


class ResilientModelClient(ModelClient):
    """多通道 + 退避重试的模型客户端包装。

    对外暴露与 :class:`ModelClient` 相同的 ``chat()`` 协议，因此 Agent 循环可以无感替换。
    """

    def __init__(
        self,
        channels: list[ChannelConfig],
        *,
        policy: RetryPolicy | None = None,
        emit: EmitFn | None = None,
        client_factory: Callable[[ChannelConfig], ModelClient] = build_model_client,
    ) -> None:
        if not channels:
            raise ValueError("至少需要一个 LLM 通道")
        self.channels = channels
        self.policy = policy or RetryPolicy(max_retries=channels[0].max_retries or 3)
        self.emit = emit
        self._factory = client_factory
        self._clients: dict[str, ModelClient] = {}
        self.channel = channels[0]
        #: 上次成功使用的通道名（便于前端展示）
        self.last_channel: str = channels[0].name

    # ------------------------------------------------------------------
    def _client_for(self, channel: ChannelConfig) -> ModelClient:
        key = channel.name or channel.model
        if key not in self._clients:
            self._clients[key] = self._factory(channel)
        return self._clients[key]

    async def _notify(self, event: str, payload: dict[str, Any]) -> None:
        if not self.emit:
            return
        try:
            result = self.emit(event, payload)
            if result is not None:
                await result
        except Exception:  # noqa: BLE001 - 事件发射失败不影响主流程
            logger.debug("事件发射失败：%s", event, exc_info=True)

    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        *,
        on_delta: Callable[[str], Any] | None = None,
        on_reasoning: Callable[[str], Any] | None = None,
    ) -> ModelResponse:
        last_error: BaseException | None = None
        total_channels = len(self.channels)
        skipped: list[str] = []

        for ch_index, channel in enumerate(self.channels):
            problem = secret_problem(channel.api_key, channel.base_url)
            if problem:
                # 绝不把星号占位符/明显无效的密钥发到服务商：那只会换来一个看不懂的 401。
                skipped.append(f"{channel.name}（api_key {problem}）")
                logger.error(
                    "通道 %s 已跳过：api_key %s。请在 Web 界面「配置」里填写真实密钥",
                    channel.name,
                    problem,
                )
                await self._notify(
                    "progress",
                    {
                        "stage": "channel_skipped",
                        "message": f"通道 {channel.name} 已跳过：api_key {problem}",
                        "channel": channel.name,
                    },
                )
                continue
            client = self._client_for(channel)
            retries = max(1, min(self.policy.max_retries, max(channel.max_retries, 1)))
            for attempt in range(1, retries + 1):
                try:
                    response = await client.chat(
                        messages, tools, on_delta=on_delta, on_reasoning=on_reasoning
                    )
                    self.last_channel = channel.name
                    self.channel = channel
                    return response
                except Exception as exc:  # noqa: BLE001 - 按类型分流
                    last_error = exc
                    if is_context_overflow_error(exc) and not isinstance(exc, ContextOverflowError):
                        await self._notify(
                            "progress",
                            {
                                "stage": "context_overflow",
                                "message": f"通道 {channel.name} 报告上下文超限，将压缩历史后重试",
                            },
                        )
                        raise ContextOverflowError(str(exc)) from exc

                    retryable = is_retryable_error(exc)
                    has_next_attempt = attempt < retries
                    has_next_channel = ch_index < total_channels - 1
                    logger.warning(
                        "模型调用失败（通道=%s 第%d/%d次）：%s", channel.name, attempt, retries, exc
                    )
                    if not retryable or (not has_next_attempt and not has_next_channel):
                        break
                    if has_next_attempt:
                        delay = self.policy.delay_for(attempt)
                        await self._notify(
                            "progress",
                            {
                                "stage": "retry",
                                "message": f"调用失败，{delay:.1f}s 后重试（通道 {channel.name}）",
                                "attempt": attempt,
                                "channel": channel.name,
                            },
                        )
                        await asyncio.sleep(delay)
                        continue
                    break  # 换通道
            if ch_index < total_channels - 1:
                next_channel = self.channels[ch_index + 1]
                await self._notify(
                    "progress",
                    {
                        "stage": "failover",
                        "message": f"通道 {channel.name} 不可用，切换到 {next_channel.name}",
                        "channel": next_channel.name,
                    },
                )

        if skipped and last_error is None:
            raise ModelCallError(
                "没有可用的 LLM 通道，已跳过：" + "；".join(skipped) + "。请填写真实密钥后重试。"
            )
        message = f"所有 LLM 通道均调用失败：{last_error}"
        if skipped:
            message += "（另有通道被跳过：" + "；".join(skipped) + "）"
        raise ModelCallError(message) from last_error

    async def aclose(self) -> None:
        for client in self._clients.values():
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass

    @classmethod
    def from_channels(cls, channels: list[ChannelConfig], **kwargs: Any) -> ResilientModelClient:
        return cls(channels, **kwargs)


__all__ = [
    "CONTEXT_OVERFLOW_PATTERNS",
    "ContextOverflowError",
    "ModelCallError",
    "ResilientModelClient",
    "RetryPolicy",
    "is_context_overflow_error",
    "is_retryable_error",
]
