"""题目元数据内置工具：challenge_info。

支持 ``get``（默认）/``list``/``set``/``attachments`` 四种动作。
题目目标是唯一的行动范围依据：目标缺失时必须让模型向用户澄清。
"""

from __future__ import annotations

import os
from typing import Any

from ...models.types import ToolOutcome
from ..context import ToolContext
from ..guard import soft_failure
from .paths import display_path


async def _attachment_entries(ctx: ToolContext) -> list[str]:
    """本次运行可见的附件条目（U17：会话私有 work + 本会话 attachments）。"""
    workspace = ctx.resolved_workspace()
    roots = [workspace]
    conversation_dir = ctx.config.conversation_dir(ctx.conversation_id)
    if conversation_dir.resolve() != workspace.resolve() and conversation_dir.is_dir():
        roots.append(conversation_dir)

    skip = {"_outputs", "_scripts", "work"}
    seen: set[str] = set()
    entries: list[str] = []
    for root_dir in roots:
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = [d for d in dirs if d not in skip]
            for name in sorted(files):
                path = os.path.join(root, name)
                rel = os.path.relpath(path, ctx.config.project_root)
                if rel in seen:
                    continue
                seen.add(rel)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                entries.append(f"- {rel} ({size} B)")
    return entries


async def _attachment_listing(ctx: ToolContext) -> str:
    """列出本次运行可见的附件（排除内部产物目录）。

    U17：未绑定题目的会话，附件存放在 ``workspace/conversations/<cid>/attachments``，
    而工作区是会话私有的 ``.../work``；两者都要扫描，否则工具会误报「暂无附件」。
    """
    entries = await _attachment_entries(ctx)
    if not entries:
        return (
            "（本会话暂无附件：请在输入框用 📎 上传附件，"
            "或把文件放入 workspace/conversations/<会话ID>/attachments/ 后重试）"
        )
    return "\n".join(entries)


async def challenge_info(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    action = str(args.get("action") or "get").strip().lower()
    challenge_id = str(args.get("challenge_id") or ctx.challenge_id or "").strip()

    if action == "set":
        if not args.get("name") and not challenge_id:
            return ToolOutcome.error(
                soft_failure("action=set 时至少需要 name 或 challenge_id", tool="challenge_info")
            )
        record = await ctx.repos.challenges.upsert(
            {
                "id": challenge_id or None,
                "name": str(args.get("name") or challenge_id),
                "category": str(args.get("category") or "misc"),
                "description": str(args.get("description") or ""),
                "points": int(args.get("points") or 0),
                "attachments": list(args.get("attachments") or []),
                "flag_pattern": str(args.get("flag_pattern") or ""),
                "target": str(args.get("target") or ""),
            }
        )
        return ToolOutcome.ok(
            f"已登记题目：id={record['id']} name={record['name']} category={record['category']}"
            f" target={record['target'] or '(未填写)'}\n"
            "请把题目目标（target/附件）作为后续行动的唯一范围依据。"
        )

    if action == "list":
        challenges = await ctx.repos.challenges.list(limit=50)
        if not challenges:
            return ToolOutcome.ok(
                '当前没有登记的题目。可用 challenge_info(action="set", ...) 登记，'
                "或直接在会话中提供题目描述与附件路径。"
            )
        lines = [
            f"- {c['id']} | {c['name']} | {c['category']} | {c['points']}分 | target={c['target'] or '-'}"
            for c in challenges
        ]
        return ToolOutcome.ok("已登记题目：\n" + "\n".join(lines))

    if not challenge_id:
        # U17：未绑定题目的会话**不列举其它题目**（避免模型套用别的题），
        # 只要本会话有附件就按「纯附件题」正常解题。
        entries = await _attachment_entries(ctx)
        if entries:
            return ToolOutcome.ok(
                "# 未绑定题目的会话（按纯附件题处理）\n"
                f"- 工作区：{display_path(ctx.resolved_workspace(), ctx)}\n"
                "- 本会话附件：\n"
                + "\n".join(entries)
                + "\n\n请只针对上述附件解题（解压/逆向/分析都在本工作区内进行）；"
                "禁止引用 workspace/challenge/ 下其它题目的文件、旧 writeup 或历史 flag。"
                "若需要登记题目元数据，可调用 challenge_info(action=\"set\", ...)。"
            )
        hint = (
            "缺失信息清单（必须向用户澄清后才能行动）：\n"
            "1) 题目名称与分类（web/pwn/crypto/reverse/forensics/misc）\n"
            "2) 题目目标：靶机 IP:Port 或 URL，或“纯附件题”\n"
            "3) 附件文件（用 📎 上传，或告知本机路径）\n"
            "4) flag 格式（如 flag{...}）与分值\n"
            "5) 授权范围与时间窗口（仅限 CTF 竞赛环境 / 自建靶场 / 书面授权的目标）\n"
        )
        return ToolOutcome.error(
            soft_failure("未提供合法题目目标，无法确定行动范围。", tool="challenge_info", hint=hint)
        )

    challenge = await ctx.repos.challenges.get(challenge_id)
    attachments = await _attachment_listing(ctx)
    if challenge is None:
        return ToolOutcome.ok(
            f"题目 {challenge_id} 尚未登记元数据，但已确定题目工作区。\n"
            f"工作区附件：\n{attachments}\n\n"
            "建议先用 challenge_info(action=set) 补全题目描述，再开始侦察。"
        )
    if action == "attachments":
        return ToolOutcome.ok(f"# {challenge['name']} 附件清单\n{attachments}")
    body = (
        f"# 题目 {challenge['id']}\n"
        f"- 名称：{challenge['name']}\n"
        f"- 分类：{challenge['category']}\n"
        f"- 分值：{challenge['points']}\n"
        f"- 目标：{challenge['target'] or '(未填写——若为靶机题请要求用户补充)'}\n"
        f"- flag 格式：{challenge['flagPattern'] or '(未指定，默认 xxxx{...})'}\n"
        f"- 登记描述：\n{challenge['description'] or '(空)'}\n"
        f"\n## 工作区附件\n{attachments}\n"
        f"\n## 工作区路径\n{ctx.resolved_workspace()}\n"
        "\n行动范围仅限以上目标与附件；范围外的任何主机/域名都不得扫描或利用。"
    )
    return ToolOutcome.ok(body)


__all__ = ["challenge_info"]
