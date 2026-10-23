"""上下文三层治理。

1. **单条工具结果截断落盘**（``tools/guard.py``，发生在工具返回时）；
2. **历史消息 reduction**：超过阈值后把旧轮的工具输出替换为占位符（:func:`reduce_history`）；
3. **token 预算超窗时激进压缩**：丢最旧轮 + 工具输出再截断 + 注入意图账本（:func:`compress_messages`），
   由 Agent 循环在收到 :class:`~ctfagent.models.resilience.ContextOverflowError` 后调用并重试。

所有裁剪都以"块（block）"为单位：助手消息 + 其后的工具结果消息必须同生共死，
否则会破坏 ``assistant.tool_calls`` 与 ``role=tool`` 消息的配对关系，导致接口报错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..models.types import ChatMessage
from .summarizer import REDUCED_TOOL_CHARS, REDUCTION_THRESHOLD, IntentLedger, condense_whitespace


# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：ASCII 约 4 字符/token，CJK 约 1 字符/token。"""
    if not text:
        return 0
    ascii_count = 0
    wide_count = 0
    for ch in text:
        if ord(ch) < 128:
            ascii_count += 1
        else:
            wide_count += 1
    return int(ascii_count / 4 + wide_count * 1.05) + 1


def estimate_message_tokens(message: ChatMessage) -> int:
    """单条消息的 token 估算（含工具调用与固定开销）。"""
    overhead = 6
    total = overhead + estimate_tokens(message.text_for_estimate())
    if message.role == "tool":
        total += 4
    return total


def estimate_messages_tokens(messages: list[ChatMessage]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------
@dataclass
class ContextPolicy:
    """上下文治理参数。"""

    max_history_messages: int = 120
    keep_recent_messages: int = 40
    reduce_tool_chars: int = REDUCED_TOOL_CHARS
    max_total_tokens: int = 120_000
    reserve_completion_tokens: int = 16_384
    #: 压缩目标 = 可用预算 × ratio
    compression_ratio: float = 0.6
    #: 压缩时保留的工具输出字符数
    compressed_tool_chars: int = 600

    @property
    def prompt_budget(self) -> int:
        return max(2_000, self.max_total_tokens - self.reserve_completion_tokens)

    def target_tokens(self) -> int:
        return int(self.prompt_budget * self.compression_ratio)


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def build_context(
    *,
    system_prompt: str,
    history: list[ChatMessage],
    user_message: str | None = None,
    policy: ContextPolicy | None = None,
) -> list[ChatMessage]:
    """组装发送给模型的消息列表：system + （reduction 后的）历史 + 当前用户消息。"""
    policy = policy or ContextPolicy()
    reduced = reduce_history(history, policy=policy)
    messages: list[ChatMessage] = [ChatMessage.system(system_prompt)]
    messages.extend(m for m in reduced if m.role != "system")
    if user_message is not None:
        messages.append(ChatMessage.user(user_message))
    return messages


# ---------------------------------------------------------------------------
# 第二层：reduction
# ---------------------------------------------------------------------------
def reduce_history(messages: list[ChatMessage], *, policy: ContextPolicy | None = None) -> list[ChatMessage]:
    """历史消息 reduction：保留最近若干轮，把更早的臃肿工具输出替换为占位符。"""
    policy = policy or ContextPolicy()
    if not messages:
        return []
    total = len(messages)
    if total <= policy.max_history_messages:
        return list(messages)

    keep_from = max(0, total - policy.keep_recent_messages)
    result: list[ChatMessage] = []
    for index, message in enumerate(messages):
        if index >= keep_from:
            result.append(message)
            continue
        if message.role == "tool" and len(message.content or "") > REDUCTION_THRESHOLD:
            placeholder = (
                f"[历史工具输出已压缩：原 {len(message.content)} 字符。"
                f"如需该信息请重新执行工具或用 blackboard_get 取回已落黑板的事实]"
            )
            replaced = message.model_copy(update={"content": placeholder, "reduced": True})
            result.append(replaced)
        else:
            result.append(message)
    return result


# ---------------------------------------------------------------------------
# 第三层：激进压缩
# ---------------------------------------------------------------------------
def _into_blocks(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
    """把消息序列切成可安全整体丢弃的块。"""
    blocks: list[list[ChatMessage]] = []
    current: list[ChatMessage] = []
    for message in messages:
        if message.role == "system":
            if current:
                blocks.append(current)
                current = []
            blocks.append([message])
            continue
        if message.role == "assistant":
            if current:
                blocks.append(current)
            current = [message]
            continue
        if message.role == "tool" and current and current[-1].role == "assistant":
            current.append(message)
            continue
        if current:
            blocks.append(current)
            current = []
        blocks.append([message])
    if current:
        blocks.append(current)
    return blocks


def compress_messages(
    messages: list[ChatMessage],
    *,
    target_tokens: int,
    policy: ContextPolicy | None = None,
    ledger: IntentLedger | None = None,
    summary: str | None = None,
    note: str = "",
) -> list[ChatMessage]:
    """激进压缩：丢最旧块 + 工具输出再截断 + 注入摘要/意图账本。

    保证：系统提示词始终保留；最后一条用户消息始终保留；assistant/tool 配对不被破坏。
    """
    policy = policy or ContextPolicy()
    if not messages:
        return []
    blocks = _into_blocks(messages)
    systems = [b[0] for b in blocks if b[0].role == "system"]
    rest = [b for b in blocks if b[0].role != "system"]

    # 从后往前保留块，直到达到目标预算
    kept: list[list[ChatMessage]] = []
    used = estimate_messages_tokens(systems)
    for block in reversed(rest):
        block_tokens = estimate_messages_tokens(block)
        if kept and used + block_tokens > target_tokens:
            break
        kept.append(block)
        used += block_tokens
    kept.reverse()

    # 至少保留最后一条用户消息
    last_user_block = next((b for b in reversed(rest) if any(m.role == "user" for m in b)), None)
    if last_user_block is not None and last_user_block not in kept:
        kept.append(last_user_block)
        used += estimate_messages_tokens(last_user_block)

    # 工具输出再截断
    compressed: list[ChatMessage] = []
    for block in kept:
        for message in block:
            if message.role == "tool" and len(message.content or "") > policy.compressed_tool_chars:
                head = (message.content or "")[: policy.compressed_tool_chars]
                compressed.append(
                    message.model_copy(
                        update={
                            "content": f"{head}\n…[压缩：原 {len(message.content)} 字符，如需请重新执行该工具]",
                            "reduced": True,
                        }
                    )
                )
            else:
                compressed.append(message)

    header_parts: list[str] = ["【上下文已压缩】较早的对话轮次已被移除以腾出上下文空间。"]
    if note:
        header_parts.append(note)
    if summary:
        header_parts.append("## 之前进展摘要\n" + condense_whitespace(summary))
    if ledger is not None and ledger.entries:
        header_parts.append(ledger.render())
    header_parts.append(
        "请依据以上摘要与黑板索引继续解题；不要重复已失败的命令，必要时用 blackboard_list 查看已确认事实。"
    )

    result: list[ChatMessage] = []
    for index, system in enumerate(systems):
        if index == 0:
            result.append(
                system.model_copy(update={"content": system.content + "\n\n" + "\n\n".join(header_parts)})
            )
        else:
            result.append(system)
    result.extend(compressed)
    return result


def fits_budget(messages: list[ChatMessage], *, policy: ContextPolicy) -> bool:
    """判断消息列表是否在提示预算内。"""
    return estimate_messages_tokens(messages) <= policy.prompt_budget


def context_stats(messages: list[ChatMessage]) -> dict[str, Any]:
    """上下文统计（事件与日志用）。"""
    roles: dict[str, int] = {}
    for message in messages:
        roles[message.role] = roles.get(message.role, 0) + 1
    return {
        "messages": len(messages),
        "estimatedTokens": estimate_messages_tokens(messages),
        "roles": roles,
        "reducedMessages": sum(1 for m in messages if m.reduced),
    }


def strip_reasoning(messages: list[ChatMessage]) -> list[ChatMessage]:
    """移除历史中的 reasoning 内容（部分通道不接受回填 reasoning）。"""
    return [m.model_copy(update={"reasoning": None}) if m.reasoning else m for m in messages]


def extract_flag_candidates(text: str, pattern: str) -> list[str]:
    """从文本中提取 flag 候选（供 finalizer 判断是否已拿到 flag）。"""
    try:
        regex = re.compile(pattern)
    except re.error:
        return []
    found: list[str] = []
    for match in regex.finditer(text or ""):
        value = match.group(0)
        if value not in found:
            found.append(value)
    return found


__all__ = [
    "ContextPolicy",
    "build_context",
    "compress_messages",
    "context_stats",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_tokens",
    "extract_flag_candidates",
    "fits_budget",
    "reduce_history",
    "strip_reasoning",
]
