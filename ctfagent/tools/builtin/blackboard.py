"""黑板记忆内置工具：blackboard_upsert / blackboard_get / blackboard_list / blackboard_search。

设计宪法第 8 条：提示词只注入 ``key + summary`` 索引，模型按 key 取 body；
系统提示词强制"每确认一条新认知立即落黑板再继续"。
"""

from __future__ import annotations

from typing import Any

from ...models.types import ToolOutcome
from ..context import ToolContext
from ..guard import soft_failure


async def blackboard_upsert(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    key = str(args.get("key") or "").strip()
    if not key:
        return ToolOutcome.error(
            soft_failure(
                "缺少 key 参数（建议用稳定短键，如 web.sqli.param、pwn.libc.version）",
                tool="blackboard_upsert",
            )
        )
    summary = str(args.get("summary") or "").strip()
    body = str(args.get("body") or "").strip()
    tags_raw = args.get("tags") or []
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    else:
        tags = [str(t) for t in tags_raw]
    if not summary and not body:
        return ToolOutcome.error(
            soft_failure(
                "summary 与 body 至少提供一个（summary 一行写清「什么+在哪+如何验证」）",
                tool="blackboard_upsert",
            )
        )
    fact = await ctx.repos.facts.upsert(
        ctx.blackboard_scope,
        key,
        summary=summary,
        body=body,
        tags=tags,
        conversation_id=ctx.facts_conversation_id,
        challenge_id=ctx.challenge_id,
    )
    verb = "新建" if fact.get("created") else "更新"
    await ctx.notify(
        "progress",
        {"stage": "blackboard", "message": f"黑板{verb}：{key}", "key": key, "tool": "blackboard_upsert"},
    )
    return ToolOutcome.ok(
        f"已{verb}黑板条目 `{key}`。\n- summary: {summary or '(空)'}\n- tags: {', '.join(tags) or '(无)'}"
        f"\n- body 长度: {len(body)} 字符（模型可用 blackboard_get 取回）"
        "\n继续下一步前，请确保本条目已包含可复现证据（命令+关键输出）。"
    )


async def blackboard_get(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    key = str(args.get("key") or "").strip()
    if not key:
        return ToolOutcome.error(soft_failure("缺少 key 参数", tool="blackboard_get"))
    fact = await ctx.repos.facts.get(ctx.blackboard_scope, key)
    if fact is None:
        listed = await ctx.repos.facts.list(ctx.blackboard_scope, limit=30)
        keys = ", ".join(f["key"] for f in listed) or "(黑板为空)"
        return ToolOutcome.error(
            soft_failure(
                f"黑板中不存在 key={key}。",
                tool="blackboard_get",
                hint=f"现有条目：{keys}",
            )
        )
    body = fact["body"] or "(无 body)"
    return ToolOutcome.ok(
        f"# blackboard[{fact['key']}]\n- summary: {fact['summary'] or '(空)'}"
        f"\n- tags: {', '.join(fact['tags']) or '(无)'}\n\n{body}"
    )


async def blackboard_list(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    limit = int(args.get("limit") or 60)
    facts = await ctx.repos.facts.list(ctx.blackboard_scope, limit=limit)
    if not facts:
        return ToolOutcome.ok(
            "黑板为空。每确认一条新认知（端口/版本/漏洞/偏移/flag 片段）都应立即 blackboard_upsert。"
        )
    lines = [f"- {f['key']}: {f['summary'] or '(无 summary)'}" for f in facts]
    return ToolOutcome.ok(f"黑板共 {len(facts)} 条：\n" + "\n".join(lines))


async def blackboard_search(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolOutcome.error(soft_failure("缺少 query 参数", tool="blackboard_search"))
    limit = int(args.get("limit") or ctx.config.blackboard.search_limit)
    facts = await ctx.repos.facts.search(ctx.blackboard_scope, query, limit=limit)
    if not facts:
        return ToolOutcome.ok(f"黑板中没有匹配「{query}」的条目。可先用 blackboard_list 看全量 key。")
    chunks = [
        f"## {f['key']}\n- summary: {f['summary'] or '(空)'}\n- body: {(f['body'] or '')[:400]}"
        for f in facts
    ]
    return ToolOutcome.ok(f"匹配「{query}」共 {len(facts)} 条：\n\n" + "\n\n".join(chunks))


__all__ = ["blackboard_get", "blackboard_list", "blackboard_search", "blackboard_upsert"]
