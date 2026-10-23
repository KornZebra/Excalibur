"""历史摘要与用户意图账本（ledger）。

M3 上下文治理的第三层：
- 第一层：单条工具结果截断落盘（guard.py）
- 第二层：历史消息 reduction（旧轮工具输出替换为占位符，本模块 ``reduce_history``）
- 第三层：token 预算超窗时激进压缩（丢最旧轮 + 工具输出再截断 + 意图账本注入）

意图账本保证压缩后代理仍记得"用户到底要什么"，避免压缩丢目标。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..models.client import ModelClient
from ..models.types import ChatMessage

logger = logging.getLogger(__name__)

#: 单条工具输出在 reduction 后保留的最大字符数
REDUCED_TOOL_CHARS = 400
#: 触发 reduction 的单条工具输出长度
REDUCTION_THRESHOLD = 2_000


@dataclass
class IntentEntry:
    """一条用户意图/约束。"""

    text: str
    source: str = "user"
    kind: str = "intent"
    at: float = 0.0


@dataclass
class IntentLedger:
    """用户意图账本：压缩历史时用于重建"目标不丢失"的锚点。"""

    entries: list[IntentEntry] = field(default_factory=list)
    max_entries: int = 12

    def record(self, text: str, *, source: str = "user", kind: str = "intent") -> None:
        text = (text or "").strip()
        if not text:
            return
        for entry in self.entries:
            if entry.text == text:
                return
        self.entries.append(IntentEntry(text=text[:800], source=source, kind=kind))
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries :]

    def extract_from_messages(self, messages: list[ChatMessage]) -> None:
        """从历史消息中抽取用户消息与关键约束。"""
        for message in messages:
            if message.role == "user":
                self.record(message.content, source="user")
            elif message.role == "system" and "题目元数据" in message.content:
                for line in message.content.splitlines():
                    if line.startswith("- 目标：") or line.startswith("- flag 格式："):
                        self.record(line.strip(), source="challenge", kind="constraint")

    def render(self) -> str:
        if not self.entries:
            return ""
        lines = ["【用户意图账本（压缩后仍必须遵守）】"]
        for entry in self.entries:
            prefix = "约束" if entry.kind == "constraint" else "意图"
            lines.append(f"- [{prefix}] {entry.text}")
        return "\n".join(lines)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [{"text": e.text, "source": e.source, "kind": e.kind} for e in self.entries]


# ---------------------------------------------------------------------------
# 历史摘要
# ---------------------------------------------------------------------------
SUMMARY_PROMPT = """请把下面的 CTF 解题对话历史压缩成结构化摘要，用于后续继续解题。要求：

1. 保留：题目目标与范围、已确认事实（含命令与关键输出）、已排除的假设及原因、当前正在验证的假设、未完成的下一步。
2. 丢弃：寒暄、重复解释、已失效的中间输出。
3. 用中文输出，分节：## 目标与范围 / ## 已确认事实(证据) / ## 已排除的假设 / ## 当前进展 / ## 下一步。
4. 不超过 {max_chars} 字符，不要编造未出现过的信息。

对话历史：
{history}
"""


def render_history(messages: list[ChatMessage], *, max_chars: int = 24_000) -> str:
    """把消息列表渲染成可读文本（用于摘要）。"""
    parts: list[str] = []
    for message in messages:
        if message.role == "system":
            continue
        label = {"user": "用户", "assistant": "助手", "tool": f"工具[{message.name or ''}]"}.get(
            message.role, message.role
        )
        content = (message.content or "")[:4_000]
        if message.tool_calls:
            calls = ", ".join(f"{tc.name}({tc.raw_arguments or tc.arguments})" for tc in message.tool_calls)
            content = f"{content}\n[调用工具] {calls}"
        parts.append(f"--- {label} ---\n{content}")
    text = "\n\n".join(parts)
    return text[-max_chars:]


async def summarize_history(
    model: ModelClient | None,
    messages: list[ChatMessage],
    *,
    max_chars: int = 4_000,
    ledger: IntentLedger | None = None,
) -> str:
    """用模型把历史压缩成摘要；模型不可用时退化为规则摘要。"""
    if not messages:
        return ""
    if model is None:
        return fallback_summary(messages, ledger=ledger)
    prompt = SUMMARY_PROMPT.format(max_chars=max_chars, history=render_history(messages))
    try:
        response = await model.chat([ChatMessage.user(prompt)], tools=None)
        text = (response.content or "").strip()
        if text:
            return text[: max_chars * 2]
    except Exception as exc:  # noqa: BLE001 - 摘要失败不影响主流程
        logger.warning("历史摘要失败，退化为规则摘要：%s", exc)
    return fallback_summary(messages, ledger=ledger)


def fallback_summary(messages: list[ChatMessage], *, ledger: IntentLedger | None = None) -> str:
    """不依赖模型的规则摘要：抽取用户消息 + 黑板写入 + 工具名统计。"""
    lines: list[str] = ["## 目标与范围"]
    if ledger and ledger.entries:
        lines += [f"- {e.text[:200]}" for e in ledger.entries[-5:]]
    else:
        lines += [f"- {m.content[:200]}" for m in messages if m.role == "user"][-3:] or ["- (未提取到)"]

    lines.append("\n## 已确认事实(证据)")
    blackboard_calls: list[str] = []
    tool_names: list[str] = []
    for message in messages:
        for call in message.tool_calls:
            tool_names.append(call.name)
            if call.name == "blackboard_upsert":
                key = call.arguments.get("key") or "?"
                summary = call.arguments.get("summary") or ""
                blackboard_calls.append(f"- {key}: {summary[:200]}")
    lines += blackboard_calls[-12:] or ["- (历史中无黑板写入记录)"]

    lines.append("\n## 已排除的假设")
    lines.append("- (规则摘要无法判定，请结合黑板中的 failed 记录)")

    lines.append("\n## 当前进展")
    from collections import Counter

    counter = Counter(tool_names)
    lines.append("- 已调用工具：" + (", ".join(f"{k}×{v}" for k, v in counter.most_common(10)) or "无"))

    lines.append("\n## 下一步")
    lines.append("- 依据未完成的黑板条目继续验证；不要重复已失败的命令。")
    return "\n".join(lines)


def condense_whitespace(text: str) -> str:
    """压缩多余空白，减少 token 占用。"""
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text or ""))


__all__ = [
    "REDUCED_TOOL_CHARS",
    "REDUCTION_THRESHOLD",
    "IntentEntry",
    "IntentLedger",
    "condense_whitespace",
    "fallback_summary",
    "render_history",
    "summarize_history",
]
