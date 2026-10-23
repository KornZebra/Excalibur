"""竞赛平台调用工具（S2）——让解题 Agent 能按黑板里的接口说明直接调平台。

设计：
- 自动带 base_url 与凭证（来自竞赛配置与「提供凭证」录入的值）；
- method/path/params/body 由模型按黑板里的协议事实自行构造——**不做能力槽位匹配**；
- 返回 HTTP 状态码 + 响应体（截断 4000 字符）；
- 所有调用写日志总线（SUBMIT/ERROR 级别由结果决定）。
"""

from __future__ import annotations

from typing import Any

import httpx

from ...models.types import ToolOutcome
from ...tools.context import ToolContext
from ...tools.guard import soft_failure
from ...tools.registry import ToolDefinition


async def platform_call(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """按黑板里的接口说明调用竞赛平台。"""
    method = str(args.get("method") or "").upper()
    path = str(args.get("path") or "").strip()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return ToolOutcome.error(soft_failure("method 必须是 GET/POST/PUT/PATCH/DELETE", tool="platform_call"))
    if not path.startswith("/"):
        return ToolOutcome.error(soft_failure("path 必须以 / 开头（相对竞赛 base_url）", tool="platform_call"))

    if not ctx.challenge_id:
        return ToolOutcome.error(soft_failure("当前运行没有题目上下文", tool="platform_call"))
    challenge = await ctx.repos.challenges.get(ctx.challenge_id)
    if challenge is None:
        return ToolOutcome.error(soft_failure("题目不存在", tool="platform_call"))
    meta = challenge.get("meta") or {}
    if isinstance(meta, str):
        import json as _json
        try:
            meta = _json.loads(meta)
        except ValueError:
            meta = {}
    competition_id = meta.get("competitionId")
    if not competition_id:
        return ToolOutcome.error(soft_failure(
            "该题目未关联竞赛项目（meta.competitionId 为空）", tool="platform_call",
            hint="竞赛题目由竞赛引擎自动创建；手动创建的题目请改用 curl 工具。",
        ))

    comp = await ctx.repos.competitions.get(str(competition_id))
    if comp is None:
        return ToolOutcome.error(soft_failure("竞赛配置不存在", tool="platform_call"))
    base_url = str(comp.get("baseUrl") or "").rstrip("/")
    if not base_url:
        return ToolOutcome.error(soft_failure("竞赛未配置 base_url（请先解析官方文档）", tool="platform_call"))

    from ...competition.adapters import resolve_secret_full

    secret = await resolve_secret_full(ctx.repos, str(competition_id), str(comp.get("authRef") or ""))
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if comp.get("authHeader") and secret:
        if comp.get("authKind") == "bearer":
            headers[comp["authHeader"]] = f"Bearer {secret}"
        else:
            headers[comp["authHeader"]] = secret

    params = args.get("params") or {}
    body = args.get("body") or {}
    url = base_url + path
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.request(method, url, params=params or None,
                                        json=body or None, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return ToolOutcome.error(f"平台调用失败：{type(exc).__name__}: {exc}")

    text = resp.text or ""
    return ToolOutcome.ok(f"HTTP {resp.status_code}\n{text[:4000]}")


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="platform_call",
            short_description="调用竞赛平台接口（自动带 base_url 与凭证）",
            description=(
                "调用当前竞赛平台 REST 接口。method/path/params/body 按竞赛全局黑板里的"
                "「协议」事实构造（base_url 与鉴权凭证自动附加，无需手拼）。"
                "返回 HTTP 状态码与响应体。仅用于已授权的比赛平台。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                               "description": "HTTP 方法"},
                    "path": {"type": "string", "description": "接口路径（以 / 开头，相对竞赛 base_url）"},
                    "params": {"type": "object", "description": "Query 参数（可选）"},
                    "body": {"type": "object", "description": "JSON 请求体（可选）"},
                },
                "required": ["method", "path"],
            },
            handler=platform_call,
            category="competition",
        )
    )
