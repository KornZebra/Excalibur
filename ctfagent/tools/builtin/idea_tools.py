"""v2 账本内置工具：``idea_upsert`` / ``idea_transition`` / ``memory_write`` / ``blackboard_query``。

这四个工具是"状态即协调"的落点：Idea（方向假设）与 Memory（已确认事实）分离，
让外置 Dispatcher 能仅凭账本做出调度决策，而模型只负责执行被派发的方向。

设计约束（与 §3.3 对应）：
- ``idea_transition`` 走状态机校验（``pending→testing→{verified|failed|skipped}``，``testing`` 需先认领）；
- 转 ``verified`` / ``failed`` 时由状态机**自动联动**写一条 ``fact`` / ``failure`` Memory，防漏记；
- ``failure`` / ``hint`` 的 ``source`` 不允许 agent 伪造（``hint`` 只能由人类/系统通道写入）；
- 全部为纯本地 DB 写入（无子进程、无网络），因此可安全加入 HITL 免审白名单。
"""

from __future__ import annotations

import logging
from typing import Any

from ...models.types import ToolOutcome
from ...state.ideas import IdeaService, IdeaTransitionError
from ...state.memory import MemoryService, MemoryWriteError
from ..context import ToolContext
from ..guard import soft_failure, wrap_exception

logger = logging.getLogger(__name__)


def _challenge_id(ctx: ToolContext) -> str:
    return str(ctx.challenge_id or "").strip()


def _missing_challenge(tool: str) -> ToolOutcome:
    return ToolOutcome.error(
        soft_failure(
            "当前运行没有题目上下文（challenge_id），无法使用挑战级账本。",
            tool=tool,
            hint="账本用于挑战会话；单轮问答请改用 blackboard_upsert（对话级黑板）。",
        )
    )


async def idea_upsert(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """声明一个新方向（同文本去重）。"""
    challenge_id = _challenge_id(ctx)
    if not challenge_id:
        return _missing_challenge("idea_upsert")
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolOutcome.error(
            soft_failure(
                "缺少 text 参数：必须是一句**可验证**的假设/方向。",
                tool="idea_upsert",
                hint="例如「/login 的 u 参数存在字符串拼接注入，可用 ' OR 1=1-- 验证」。",
            )
        )
    idea_id = str(args.get("idea_id") or "").strip() or None
    # M2：因果边——该方向从哪些事实推来（可选；最多 8 条，不存在的忽略）
    raw_from = args.get("from") or []
    if isinstance(raw_from, str):
        raw_from = [raw_from]
    from_ids = [str(x).strip() for x in raw_from if str(x).strip()][:8]
    try:
        idea, created = await IdeaService(ctx.repos).upsert(
            challenge_id, text, idea_id=idea_id, created_by=ctx.agent_id or "agent"
        )
    except IdeaTransitionError as exc:
        return ToolOutcome.error(soft_failure(str(exc), tool="idea_upsert"))
    except Exception as exc:  # noqa: BLE001 - 软失败：账本写入失败不能中断循环
        return ToolOutcome.error(wrap_exception(exc, tool="idea_upsert"))

    # M2：写入因果边（不存在的来源忽略，不阻断创建）
    linked: list[str] = []
    missing: list[str] = []
    if from_ids:
        for memory_id in from_ids:
            try:
                if await ctx.repos.memories.get(memory_id):
                    linked.append(memory_id)
                else:
                    missing.append(memory_id)
            except Exception:  # noqa: BLE001 - 校验失败按缺失处理
                missing.append(memory_id)
        if linked:
            try:
                await ctx.repos.ideas.add_sources(idea["id"], challenge_id, linked)
            except Exception:  # noqa: BLE001 - 边写入失败不影响 Idea 本体
                logger.debug("idea_sources 写入失败", exc_info=True)

    verb = "已声明" if created else "已存在（去重命中）"
    await ctx.notify(
        "progress",
        {
            "stage": "idea",
            "message": f"{verb} Idea：{idea['text'][:60]}",
            "ideaId": idea["id"],
            "tool": "idea_upsert",
        },
    )
    lines = [
        f"{verb} Idea `{idea['id']}`（status={idea['status']}）。",
        f"- 假设：{idea['text']}",
    ]
    if linked:
        lines.append(f"- 来源事实：{' + '.join(linked)}")
    if missing:
        lines.append(f"- 注意：以下来源不存在，已忽略：{', '.join(missing)}")
    lines.append(
        "下一步：由 Dispatcher 认领后转 testing；若你已在本轮认领，直接 idea_transition(status=testing)。"
    )
    return ToolOutcome.ok("\n".join(lines))


async def idea_transition(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """推进 Idea 状态（verified/failed 时自动联动写 Memory）。"""
    challenge_id = _challenge_id(ctx)
    if not challenge_id:
        return _missing_challenge("idea_transition")
    idea_id = str(args.get("idea_id") or "").strip()
    status = str(args.get("status") or "").strip()
    evidence = str(args.get("evidence_summary") or "").strip()
    if not idea_id or not status:
        return ToolOutcome.error(
            soft_failure(
                "缺少 idea_id 或 status 参数",
                tool="idea_transition",
                hint="status ∈ pending/testing/verified/failed/skipped",
            )
        )
    if status.lower() in {"verified", "failed"} and not evidence:
        return ToolOutcome.error(
            soft_failure(
                f"转为 {status} 必须提供 evidence_summary（写清如何验证/为何走不通）。",
                tool="idea_transition",
                hint="verified 举例：`/admin 返回 flag{...}，与题目格式一致`；failed 举例：`/backup/ 返回 404，目录不存在`。",
            )
        )
    try:
        result = await IdeaService(ctx.repos).transition(
            challenge_id,
            idea_id,
            status,
            evidence_summary=evidence,
            actor=ctx.agent_id or "agent",
            solver_run_id=ctx.run_id or ctx.agent_id or None,
        )
    except IdeaTransitionError as exc:
        counts = await ctx.repos.ideas.count_by_status(challenge_id)
        return ToolOutcome.error(
            soft_failure(
                str(exc),
                tool="idea_transition",
                hint=f"当前账本状态统计：{counts}。可先 blackboard_query 查看所有 Idea。",
            )
        )
    except Exception as exc:  # noqa: BLE001
        return ToolOutcome.error(wrap_exception(exc, tool="idea_transition"))

    # v2-M4：把状态迁移也发成独立事件（前端状态板据此渲染 transition 历史）
    transition_payload = {
        "challengeId": challenge_id,
        "ideaId": idea_id,
        "from": result.from_status.value,
        "to": result.to_status.value,
        "by": ctx.run_id or ctx.agent_id or "agent",
        "text": result.idea.get("text", ""),
        "memoryId": (result.memory or {}).get("id"),
    }
    try:
        from ...events import EventType

        await ctx.notify(EventType.IDEA_TRANSITION, transition_payload)
    except Exception:  # noqa: BLE001 - 事件失败不影响状态迁移
        logger.debug("idea_transition 事件发送失败", exc_info=True)
    await ctx.notify(
        "progress",
        {
            "stage": "idea",
            "message": f"Idea {result.from_status.value} → {result.to_status.value}",
            "ideaId": idea_id,
            "tool": "idea_transition",
        },
    )
    lines = [
        f"Idea `{idea_id}` 状态：{result.from_status.value} → {result.to_status.value}。",
        f"- 假设：{result.idea['text']}",
    ]
    if result.memory is not None:
        lines.append(f"- 已自动联动写入 Memory[{result.memory['kind']}]：{result.memory['summary']}")
    elif result.notes:
        lines.extend(f"- {n}" for n in result.notes)
    if result.to_status.value == "testing":
        lines.append("请在本任务内完成验证，并在结束前 idea_transition 收尾（verified/failed）。")
    return ToolOutcome.ok("\n".join(lines))


async def memory_write(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """追加一条 Memory（不可改写历史条目）。"""
    challenge_id = _challenge_id(ctx)
    if not challenge_id:
        return _missing_challenge("memory_write")
    kind = str(args.get("kind") or "").strip()
    summary = str(args.get("summary") or "").strip()
    body = str(args.get("body") or "").strip()
    if not kind or not summary:
        return ToolOutcome.error(
            soft_failure(
                "缺少 kind 或 summary 参数。",
                tool="memory_write",
                hint="kind ∈ fact/evidence/failure/note（hint 只能由人类通道写入）；summary 一行写清「什么+在哪+如何验证」。",
            )
        )
    try:
        memory = await MemoryService(ctx.repos).write(
            challenge_id,
            kind,
            summary,
            body=body,
            # 模型永远以 agent 身份写入；human/system 是特权来源，必须走 API/系统通道
            source="agent",
            solver_run_id=ctx.run_id or None,
            actor=ctx.agent_id or "agent",
        )
    except MemoryWriteError as exc:
        return ToolOutcome.error(soft_failure(str(exc), tool="memory_write"))
    except Exception as exc:  # noqa: BLE001
        return ToolOutcome.error(wrap_exception(exc, tool="memory_write"))

    await ctx.notify(
        "progress",
        {
            "stage": "memory",
            "message": f"Memory[{memory['kind']}] {memory['summary'][:40]}",
            "tool": "memory_write",
        },
    )
    return ToolOutcome.ok(
        f"已追加 Memory `{memory['id']}`（kind={memory['kind']}, source={memory['source']}）。\n"
        f"- summary：{memory['summary']}\n"
        f"- body 长度：{len(memory['body'])} 字符（不占用上下文，需要时 blackboard_query 取回）"
    )


async def _resolve_project_id(explicit: str, ctx: ToolContext) -> str | None:
    """确定黑板查询的项目范围：显式参数 > ToolContext 注入 > 由当前会话反查。

    反查让"属于某项目的会话"默认即可跨项目检索；不属于任何项目的会话返回 ``None``，
    继续走原有单挑战路径，因此既有用例（无项目）的行为零改变。
    """
    if explicit:
        return explicit
    if ctx.project_id:
        return str(ctx.project_id)
    # 用黑板共享 scope 反查（子代理会话本身无项目归属，父会话可能有）
    conversation_id = str(ctx.facts_conversation_id or "").strip()
    if not conversation_id:
        return None
    try:
        conversation = await ctx.repos.conversations.get(conversation_id)
    except Exception:  # noqa: BLE001 - 反查失败按"无项目"处理，绝不能中断查询
        logger.debug("黑板项目反查失败 conversation=%s", conversation_id, exc_info=True)
        return None
    return (conversation or {}).get("projectId")


async def _project_blackboard(
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    project_id: str,
    challenge_id: str,
    kinds: list[str],
    statuses: list[str] | None,
    limit: int,
    include_body: bool,
) -> ToolOutcome:
    """跨项目全部会话（即其引用的全部 challenge）查询 Idea + Memory（只读）。"""
    project = await ctx.repos.projects.get(project_id)
    if project is None:
        return ToolOutcome.error(
            soft_failure(
                f"项目不存在：{project_id}",
                tool="blackboard_query",
                hint="可用 GET /api/projects 列出全部项目，确认 project_id。",
            )
        )
    challenge_ids = await ctx.repos.projects.challenge_ids(project_id)
    if challenge_id and challenge_id not in challenge_ids:
        challenge_ids = [challenge_id, *challenge_ids]
    ideas = await ctx.repos.ideas.list_many(challenge_ids, statuses=statuses, limit=limit)
    memories = await ctx.repos.memories.list_many(challenge_ids, kinds=kinds or None, limit=limit)
    if not ideas and not memories:
        return ToolOutcome.ok(
            f"项目 `{project_id}`（{project['name']}）的账本为空：其全部会话尚未沉淀 Idea/Memory。"
        )
    lines = [
        f"# 项目账本（project={project_id}「{project['name']}」，跨 {len(challenge_ids)} 个挑战）",
        "",
        f"## Ideas（{len(ideas)} 条）",
    ]
    if ideas:
        lines += [
            f"- `{i['id']}` [challenge={i.get('challengeId')}] [{i['status']}] {i['text']}"
            + (f"（认领者 {i['claimedBy']}）" if i.get("claimedBy") else "")
            for i in ideas
        ]
    else:
        lines.append(f"- （无匹配；status 过滤={args.get('status')}）")
    lines += ["", f"## Memories（{len(memories)} 条）"]
    if memories:
        for m in memories:
            lines.append(
                f"- `{m['id']}` [challenge={m.get('challengeId')}] [{m['kind']}/{m['source']}] {m['summary']}"
            )
            if include_body and m.get("body"):
                lines.append(f"  ```\n  {m['body'][:1500]}\n  ```")
    else:
        lines.append(f"- （无匹配；kinds 过滤={kinds}）")
    lines += ["", "提示：项目查询跨该项目全部会话；需要完整细节时加 include_body=true。"]
    return ToolOutcome.ok("\n".join(lines))


async def blackboard_query(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """结构化查询账本（Idea + Memory）。

    默认查当前挑战；传 ``project_id``（或当前会话已归属某项目）时跨该项目全部会话检索。
    """
    challenge_id = _challenge_id(ctx)
    explicit_project = str(args.get("project_id") or "").strip()
    project_id = await _resolve_project_id(explicit_project, ctx)
    if not challenge_id and not project_id:
        return _missing_challenge("blackboard_query")
    kinds_raw = args.get("kinds") or []
    if isinstance(kinds_raw, str):
        kinds = [k.strip() for k in kinds_raw.split(",") if k.strip()]
    else:
        kinds = [str(k).strip() for k in kinds_raw if str(k).strip()]
    status_raw = args.get("status")
    statuses = [str(status_raw).strip()] if status_raw else None
    limit = int(args.get("limit") or 20)
    include_body = bool(args.get("include_body") or False)

    if project_id:
        return await _project_blackboard(
            args,
            ctx,
            project_id=project_id,
            challenge_id=challenge_id,
            kinds=kinds,
            statuses=statuses,
            limit=limit,
            include_body=include_body,
        )

    ideas = await ctx.repos.ideas.list(challenge_id, statuses=statuses, limit=limit)
    memories = await MemoryService(ctx.repos).query(challenge_id, kinds=kinds or None, limit=limit)

    if not ideas and not memories:
        return ToolOutcome.ok(
            "账本为空。请先 idea_upsert 声明方向，并用 memory_write 记录已确认的事实/已排除的路径。"
        )
    lines = [f"# 挑战账本（challenge={challenge_id}）", "", f"## Ideas（{len(ideas)} 条）"]
    if ideas:
        lines += [
            f"- `{i['id']}` [{i['status']}] {i['text']}"
            + (f"（认领者 {i['claimedBy']}）" if i.get("claimedBy") else "")
            for i in ideas
        ]
    else:
        lines.append(f"- （无匹配；status 过滤={status_raw}）")
    lines += ["", f"## Memories（{len(memories)} 条）"]
    if memories:
        for m in memories:
            lines.append(f"- `{m['id']}` [{m['kind']}/{m['source']}] {m['summary']}")
            if include_body and m.get("body"):
                lines.append(f"  ```\n  {m['body'][:1500]}\n  ```")
    else:
        lines.append(f"- （无匹配；kinds 过滤={kinds}）")
    lines += ["", "提示：body 默认不返回；需要完整细节时加 include_body=true 或按 id 精查。"]
    return ToolOutcome.ok("\n".join(lines))


__all__ = ["blackboard_query", "idea_transition", "idea_upsert", "memory_write"]
