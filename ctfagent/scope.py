"""隔离作用域：把「黑板归属」收敛成单一字符串键。

设计
----
整个系统有两个隔离单元：

- **题目（challenge）**：``scope = "challenge:<challenge_id>"``
  同一题目的所有会话（控制台 Solver、升级自会话的对话、子代理）共享一个黑板，
  但**不同题目之间完全隔离**；
- **会话（conversation）**：``scope = "conversation:<conversation_id>"``
  未绑定题目的普通对话，黑板只属于该会话。

作用域键直接作为 ``facts`` 表主键的一部分，读写都只带一个参数，避免"传错维度"
（旧实现传 ``conversation_id``，同一题目的多个 Solver 各写各的黑板，控制台看不到）。
"""

from __future__ import annotations

__all__ = ["CHALLENGE_PREFIX", "CONVERSATION_PREFIX", "fact_scope", "parse_scope"]

CHALLENGE_PREFIX = "challenge:"
CONVERSATION_PREFIX = "conversation:"


def fact_scope(challenge_id: str | None, conversation_id: str | None) -> str:
    """解析黑板作用域：**绑定题目时题目优先**，否则回落到会话。"""
    challenge = (challenge_id or "").strip()
    if challenge:
        return f"{CHALLENGE_PREFIX}{challenge}"
    conversation = (conversation_id or "").strip() or "_anon"
    return f"{CONVERSATION_PREFIX}{conversation}"


def parse_scope(scope: str) -> tuple[str, str]:
    """把作用域键拆成 ``(kind, id)``；kind 为 ``challenge`` 或 ``conversation``。"""
    text = (scope or "").strip()
    if text.startswith(CHALLENGE_PREFIX):
        return "challenge", text[len(CHALLENGE_PREFIX) :]
    if text.startswith(CONVERSATION_PREFIX):
        return "conversation", text[len(CONVERSATION_PREFIX) :]
    # 兼容旧调用方：直接传裸 conversation_id
    return "conversation", text or "_anon"
