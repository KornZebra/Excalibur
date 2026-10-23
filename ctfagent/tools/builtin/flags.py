"""flag 记录与格式校验内置工具：flag_submit。

"发现疑似 flag 立即 flag_submit"——本工具会：
1. 按题目 flag 模式（或默认 ``xxxx{...}``）校验格式；
2. 记录到 ``flags`` 表并触发前端刷新；
3. 返回格式判定结果，提醒模型核对后再声明解题完成。
"""

from __future__ import annotations

import re
from typing import Any

from ...constants import DEFAULT_FLAG_PATTERN
from ...models.types import ToolOutcome
from ..context import ToolContext
from ..guard import soft_failure

#: 常见 flag 前缀（用于推断 format 字段）
_COMMON_PREFIXES = (
    "flag",
    "ctf",
    "ctfshow",
    "flag_is",
    "key",
    "picoctf",
    "hack",
    "hgame",
    "nssctf",
    "post",
    "dam",
)


def _extract_flags(text: str, pattern: str) -> list[str]:
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(DEFAULT_FLAG_PATTERN)
    found: list[str] = []
    for match in regex.finditer(text or ""):
        value = match.group(0) if match.groups() == () else (match.group(1) or match.group(0))
        if value not in found:
            found.append(value)
    return found


async def flag_submit(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    value = str(args.get("flag") or args.get("value") or "").strip()
    if not value:
        return ToolOutcome.error(
            soft_failure(
                "缺少 flag 参数。",
                tool="flag_submit",
                hint="把疑似 flag 原样传入（不要自行增删花括号）。",
            )
        )
    note = str(args.get("note") or "").strip()
    source = str(args.get("source") or "agent").strip()

    pattern = DEFAULT_FLAG_PATTERN
    challenge = None
    if ctx.challenge_id:
        challenge = await ctx.repos.challenges.get(ctx.challenge_id)
        if challenge and challenge.get("flagPattern"):
            pattern = str(challenge["flagPattern"])

    extracted = _extract_flags(value, pattern)
    validated = bool(extracted) or bool(re.fullmatch(pattern, value))
    fmt = ""
    if "{" in value and value.endswith("}"):
        fmt = value.split("{", 1)[0]

    record = await ctx.repos.flags.add(
        value=value,
        conversation_id=ctx.conversation_id,
        challenge_id=ctx.challenge_id,
        fmt=fmt,
        validated=validated,
        source=source,
        meta={"note": note, "pattern": pattern, "extracted": extracted},
    )
    await ctx.notify(
        "progress",
        {
            "stage": "flag",
            "message": f"记录 flag：{value[:80]}",
            "validated": validated,
            "tool": "flag_submit",
        },
    )

    if validated:
        body = (
            f"已记录 flag（格式校验通过）：`{value}`\n"
            f"- 前缀：{fmt or '(未识别)'}\n"
            f"- 校验模式：{pattern}\n"
            f"- 备注：{note or '(无)'}\n\n"
            "下一步：\n"
            "1) 用最简可复现命令再确认一次该 flag 确实由题目环境返回（证据链）；\n"
            "2) 把完整利用链 blackboard_upsert 落盘；\n"
            "3) 调用 report_generate 产出 writeup。"
        )
        if len(extracted) > 1:
            body += f"\n\n注意：本次文本中还识别到其他候选：{extracted}"
        return ToolOutcome.ok(body, flag=record["value"], validated=True)

    body = (
        f"{soft_failure('flag 格式未通过校验，但仍已记录以便回溯。', tool='flag_submit')}\n"
        f"- 提交值：`{value}`\n"
        f"- 期望模式：{pattern}\n"
        f"- 常见前缀：{', '.join(_COMMON_PREFIXES[:6])}…\n\n"
        "请核对：是否漏掉了花括号、是否把提示文字混入、是否只是 flag 片段（如只拿到前半段）。\n"
        '若确实是题目自定义格式，可先用 challenge_info(action=set, flag_pattern="你的正则") 更新题目元数据后重新提交。'
    )
    return ToolOutcome.error(body)


__all__ = ["flag_submit"]
