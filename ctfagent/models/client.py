"""ModelClient 抽象与 OpenAI 兼容实现。

设计说明（保留框架对接留白）：
- :class:`ModelClient` 是唯一被 Agent 循环依赖的协议。若要接入 pydantic-ai / LangGraph，
  只需实现同名 ``chat()`` 协议并让 :class:`ctfagent.agent.loop.AgentRunner` 接收该对象即可，
  循环本身不依赖 openai SDK 类型。
- 流式回调 ``on_delta`` / ``on_reasoning`` 用于 SSE 事件发射（response_delta / thinking_stream_delta）。
"""

from __future__ import annotations

import abc
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from ..config import ChannelConfig
from ..textsafe import clean_text, clean_value
from .types import ChatMessage, ModelResponse, ToolCall, Usage

logger = logging.getLogger(__name__)

DeltaCallback = Callable[[str], Awaitable[None] | None]

# ---------------------------------------------------------------------------
# 退化重复早期中止（对抗 "Let me ..." 式循环叙述）
#
# 模型偶尔在正文里陷入同几句话的高频复现，一路烧满 max_completion_tokens：
# 流结束时 finish_reason=length、tool_calls 从未发出，上层若不识别就会把截断
# 文本当"纯文本回复"反复续跑。这里在流式过程中周期性检查尾部窗口，一旦命中
# 重复模式立即中止（finish_reason=degenerate_repetition），把止损点从
# "整次生成长度"提前到"重复刚成形"。
#
# 判定用**行去重比例**而非字符 n-gram：循环叙述的特征是整个窗口几乎只有同样
# 几行在轮转（实测样本 unique/total ≈ 0.07），而正常代码/汇编每行含不同地址
# 与操作数（≈ 1.0）、hex/base64 无行结构——行比例几乎不误伤合法内容。
# ---------------------------------------------------------------------------
REPEAT_CHECK_INTERVAL = 2048  # 每累计多少正文字符检查一次
REPEAT_WINDOW_CHARS = 4096  # 检查的尾部窗口大小
REPEAT_MIN_LINES = 24  # 窗口内非空行达到该数量才参与判定
REPEAT_UNIQUE_RATIO = 0.2  # 去重行数 / 总行数 ≤ 该比例判定为退化重复


def _looks_degenerate(text: str) -> bool:
    """判断生成文本是否已陷入退化重复（尾部窗口内只剩同样几行在轮转）。"""
    window = text[-REPEAT_WINDOW_CHARS:]
    lines = [line.strip() for line in window.splitlines() if line.strip()]
    if len(lines) < REPEAT_MIN_LINES:
        return False
    return len(set(lines)) / len(lines) <= REPEAT_UNIQUE_RATIO


class ModelClient(abc.ABC):
    """模型客户端协议。"""

    channel: ChannelConfig

    @abc.abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        *,
        on_delta: DeltaCallback | None = None,
        on_reasoning: DeltaCallback | None = None,
    ) -> ModelResponse:
        """发起一次对话补全，返回 :class:`ModelResponse`。"""

    async def aclose(self) -> None:  # pragma: no cover - 默认无资源
        return None


class OpenAICompatibleClient(ModelClient):
    """基于 openai 官方 SDK 的 ``openai_compatible`` 通道实现。

    兼容 DeepSeek / Qwen(DashScope 兼容模式) / GLM / 自建 vLLM / Ollama 等端点，
    并对 reasoning 参数做了适配（``reasoning_effort`` + ``extra_body`` 透传 + ``reasoning_content`` 增量解析）。
    """

    def __init__(self, channel: ChannelConfig) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise RuntimeError(
                "缺少 openai 依赖：请先执行 `pip install -e .` 或 `pip install openai>=1.40`"
            ) from exc
        self.channel = channel
        self._client = AsyncOpenAI(
            base_url=channel.base_url or None,
            # 本地 vLLM/Ollama 常常不需要 key，给一个占位值避免 SDK 直接报错
            api_key=channel.api_key or "not-needed",
            timeout=channel.timeout_seconds,
            max_retries=0,  # 重试统一由 resilience 层负责
            default_headers=channel.extra_headers or None,
        )
        self._stream_options_supported = True

    # ------------------------------------------------------------------
    def _build_kwargs(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        chan = self.channel
        kwargs: dict[str, Any] = {
            "model": chan.model,
            # 最后一道闸门：历史消息可能来自工具结果或磁盘文件名，含无法编码为
            # UTF-8 的孤立代理字符时，httpx 序列化请求体会抛
            # ``UnicodeEncodeError: surrogates not allowed``，整轮解题直接失败。
            "messages": clean_value([m.to_openai() for m in messages]),
            "max_tokens": chan.max_completion_tokens,
            "stream": True,
        }
        if self._stream_options_supported:
            kwargs["stream_options"] = {"include_usage": True}
        if chan.temperature is not None:
            kwargs["temperature"] = chan.temperature
        if chan.reasoning_effort:
            kwargs["reasoning_effort"] = chan.reasoning_effort
        if chan.extra_body:
            kwargs["extra_body"] = dict(chan.extra_body)
        if tools and chan.supports_tools:
            kwargs["tools"] = clean_value(tools)
            kwargs["tool_choice"] = "auto"
        return kwargs

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        *,
        on_delta: DeltaCallback | None = None,
        on_reasoning: DeltaCallback | None = None,
    ) -> ModelResponse:
        kwargs = self._build_kwargs(messages, tools)
        try:
            return await self._chat_stream(kwargs, messages, on_delta, on_reasoning)
        except Exception as exc:  # noqa: BLE001 - 需要按错误类型降级重试
            text = str(exc).lower()
            # 部分端点不支持 stream_options，去掉后重试一次
            if self._stream_options_supported and ("stream_options" in text or "include_usage" in text):
                logger.debug("通道 %s 不支持 stream_options，降级重试", self.channel.name)
                self._stream_options_supported = False
                kwargs = self._build_kwargs(messages, tools)
                return await self._chat_stream(kwargs, messages, on_delta, on_reasoning)
            raise

    async def _chat_stream(
        self,
        kwargs: dict[str, Any],
        messages: list[ChatMessage],
        on_delta: DeltaCallback | None,
        on_reasoning: DeltaCallback | None,
    ) -> ModelResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_buf: dict[int, dict[str, Any]] = {}
        usage: Usage | None = None
        finish_reason = ""
        content_len = 0
        last_repeat_check = 0

        stream = await self._client.chat.completions.create(**kwargs)
        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = Usage(
                        prompt_tokens=chunk.usage.prompt_tokens or 0,
                        completion_tokens=chunk.usage.completion_tokens or 0,
                        total_tokens=chunk.usage.total_tokens or 0,
                        model_calls=0,
                    )
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason

                # -- reasoning 增量（DeepSeek / Qwen / GLM 字段名不同） --------
                reasoning_piece = (
                    getattr(delta, "reasoning_content", None)
                    or getattr(delta, "reasoning", None)
                    or getattr(delta, "thinking", None)
                )
                if reasoning_piece:
                    # 上游可能返回孤立代理字符（JSON \udXXX 转义），清洗后再累计/外发
                    text = clean_text(str(reasoning_piece))
                    reasoning_parts.append(text)
                    if on_reasoning:
                        await _maybe_await(on_reasoning(text))

                # -- 正文增量 ------------------------------------------------
                piece = getattr(delta, "content", None)
                if piece:
                    piece = clean_text(piece)
                    content_parts.append(piece)
                    content_len += len(piece)
                    if on_delta:
                        await _maybe_await(on_delta(piece))
                    # 退化重复早期中止：命中即止损，不再烧剩余的生成长度预算
                    if content_len - last_repeat_check >= REPEAT_CHECK_INTERVAL:
                        last_repeat_check = content_len
                        if _looks_degenerate("".join(content_parts)):
                            logger.warning(
                                "通道 %s 检测到退化重复叙述，提前中止本次生成（已生成 %d 字符）",
                                self.channel.name,
                                content_len,
                            )
                            finish_reason = "degenerate_repetition"
                            break

                # -- tool_calls 增量 -----------------------------------------
                for tc in getattr(delta, "tool_calls", None) or []:
                    idx = getattr(tc, "index", None)
                    if idx is None:
                        idx = len(tool_buf)
                    slot = tool_buf.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] += fn.name
                        if getattr(fn, "arguments", None):
                            slot["arguments"] += fn.arguments
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:  # noqa: BLE001 - 关闭流失败不影响结果回传
                    pass

        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_buf):
            slot = tool_buf[idx]
            if not slot.get("name"):
                continue
            raw = slot.get("arguments") or ""
            tool_calls.append(
                ToolCall(
                    id=slot.get("id") or f"call_{idx}_{abs(hash(raw)) % 10**8}",
                    name=slot["name"],
                    arguments=ToolCall.parse_arguments(raw),
                    raw_arguments=raw,
                )
            )

        usage = usage or Usage()
        usage.model_calls = 1
        return ModelResponse(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            model=self.channel.model,
            raw={"message_count": len(messages), "tool_call_count": len(tool_calls)},
        )

    async def describe_image(self, image_b64: str, mime: str, prompt: str) -> str:
        """视觉旁路：单轮多模态调用（供内置工具 ``analyze_image`` 使用）。"""
        payload: list[dict[str, Any]] = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
        ]
        message = cast(Any, {"role": "user", "content": payload})
        resp = await self._client.chat.completions.create(
            model=self.channel.model,
            messages=cast(Any, [message]),
            max_tokens=min(self.channel.max_completion_tokens, 2048),
        )
        return resp.choices[0].message.content or ""

    async def aclose(self) -> None:
        try:
            await self._client.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
            pass


async def _maybe_await(value: Awaitable[None] | None) -> None:
    if value is not None:
        await value


def build_model_client(channel: ChannelConfig) -> ModelClient:
    """按通道 ``provider`` 构造客户端；未知 provider 一律按 openai_compatible 处理。"""
    if channel.provider in {"openai_compatible", "openai", "deepseek", "qwen", "glm", "vllm", "ollama"}:
        return OpenAICompatibleClient(channel)
    logger.warning("未知 provider=%s，按 openai_compatible 处理", channel.provider)
    return OpenAICompatibleClient(channel)


__all__ = [
    "DeltaCallback",
    "ModelClient",
    "OpenAICompatibleClient",
    "build_model_client",
]
