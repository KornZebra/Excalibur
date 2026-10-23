"""数据看板统计 API（U14 / U19 项目维度）。

单接口 ``GET /api/stats/dashboard`` 一次性返回看板所需的全部聚合指标，
避免前端为每张卡片各发一次请求：

- ``scope``            当前统计范围（全局 / 指定项目）
- ``totals``           总量：题目 / 已解答 / 成功率 / FLAG / 会话 / 消息 / 项目 / 事实 / 工具调用 / token
- ``live``             进行中：运行中的会话任务数、挑战会话数、活跃 Solver 数
- ``status``           题目状态分布：已解答 / 解题中 / 未解答
- ``trend``            近 N 天趋势：FLAG、新建会话、工具调用
- ``categories``       题目类型分布（含已解答数）
- ``projects``         项目（比赛）概览（全量，供前端范围选择器使用）
- ``tools``            工具调用 Top（含失败数）
- ``recentFlags``      最近捕获的 FLAG
- ``recentChallenges`` 最近更新的题目（含状态）

统计口径：**已捕获 flag 即视为已解答**，因此「解题数 = 已捕获 FLAG 数」、
「解题成功率 = 解题数 / 题目总数」；题目维度的归属状态（`verdict` / 绑定会话 flag）
另存于 ``status`` 备查。

项目维度（U19）
---------------
``project_id`` 为空 = 全局总览。指定项目时：

- 会话 / 消息 / flag / 工具调用 / 黑板 / run 按**会话所属项目**过滤；
- 题目按「有会话绑定到该题目且该会话属于本项目」过滤（题目表本身没有 project_id 列）；
- ``ideas`` / ``memories`` 按题目集合过滤；
- ``projects`` 仍返回全量（前端需要它渲染范围选择器）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ..runtime import Runtime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stats"])


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="运行时尚未就绪")
    return runtime


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d")


def _build_trend(
    now: float,
    days: int,
    flag_ts: list[float],
    conv_ts: list[float],
    tool_ts: list[float],
) -> list[dict[str, Any]]:
    """按本地日期分桶，输出**连续** N 天（含空天），便于前端直接画柱。"""
    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    today = datetime.fromtimestamp(now).date()
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        key = day.strftime("%m-%d")
        order.append(key)
        buckets[key] = {"date": key, "flags": 0, "conversations": 0, "toolCalls": 0}

    def bump(values: list[float], field: str) -> None:
        for ts in values:
            bucket = buckets.get(_day_key(ts))
            if bucket is not None:
                bucket[field] += 1

    bump(flag_ts, "flags")
    bump(conv_ts, "conversations")
    bump(tool_ts, "toolCalls")
    return [buckets[key] for key in order]


def _conv_scope(alias: str, project_id: str | None) -> tuple[str, tuple[Any, ...]]:
    """按「所属会话的项目」过滤（用于 flags / messages / tool_executions / facts / runs）。"""
    if not project_id:
        return "", ()
    return (
        f" AND {alias}.conversation_id IN (SELECT id FROM conversations WHERE project_id = ?)",
        (project_id,),
    )


def _in_clause(column: str, values: list[str], params: list[Any]) -> str:
    """生成 ``AND column IN (?,?,…)``；空集合时返回恒假条件。"""
    if not values:
        return " AND 1 = 0"
    params.extend(values)
    return f" AND {column} IN ({','.join('?' for _ in values)})"


@router.get("/api/stats/dashboard", summary="数据看板统计（一次性聚合，支持项目维度）")
async def dashboard_stats(
    request: Request,
    days: int = Query(default=14, ge=7, le=90, description="趋势窗口天数"),
    project_id: str | None = Query(default=None, description="按项目统计；为空则全局总览"),
) -> dict[str, Any]:
    runtime = get_runtime(request)
    db = runtime.repos.db
    repos = runtime.repos

    now = time.time()
    since = now - days * 86400.0
    project_id = (project_id or "").strip() or None

    async def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
        row = await db.fetchone(sql, params)
        return int((row or {}).get("n") or 0)

    # ----------------------------- 项目范围 -----------------------------
    project_name = ""
    project_challenge_ids: list[str] = []
    project_conversation_ids: set[str] = set()
    if project_id:
        project = await repos.projects.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"未知项目：{project_id}")
        project_name = project.get("name") or project_id
        rows = await db.fetchall(
            "SELECT DISTINCT challenge_id FROM conversations "
            "WHERE project_id = ? AND challenge_id IS NOT NULL AND challenge_id != ''",
            (project_id,),
        )
        conv_rows = await db.fetchall("SELECT id FROM conversations WHERE project_id = ?", (project_id,))
        project_conversation_ids = {r["id"] for r in conv_rows}

        # 题目归属本项目的三条证据（取并集）：
        # ① 有会话绑定该题目且会话属于本项目（conversations.challenge_id）；
        # ② challenges.meta.projectId 显式标注；
        # ③ 题目附件来自本项目的会话（路径含 conversations/<cid>，兼容历史数据）。
        # 题目表本身没有 project_id 列，所以只能按证据反推。
        challenge_ids: set[str] = {r["challenge_id"] for r in rows}
        all_rows = await db.fetchall("SELECT id, attachments, meta FROM challenges")
        for row in all_rows:
            cid = row["id"]
            if cid in challenge_ids:
                continue
            try:
                meta = json.loads(row.get("meta") or "{}")
            except (TypeError, ValueError):
                meta = {}
            if isinstance(meta, dict) and meta.get("projectId") == project_id:
                challenge_ids.add(cid)
                continue
            try:
                attachments = json.loads(row.get("attachments") or "[]")
            except (TypeError, ValueError):
                attachments = []
            for item in attachments if isinstance(attachments, list) else []:
                found = re.search(r"conversations/([A-Za-z0-9_\-]+)", str(item))
                if found and found.group(1) in project_conversation_ids:
                    challenge_ids.add(cid)
                    break
        project_challenge_ids = sorted(challenge_ids)

    # ----------------------------- 题目 / 解答 -----------------------------
    # 两个口径并存：
    #   · 解题数 = 已捕获 FLAG 数（产品既定口径：拿到 flag 即视为一次解答）；
    #   · 解题成功率 = **有 flag 的题目数 / 题目总数**（题目维度）。
    # 后者分子分母同量纲，不会被「一题多 flag」顶到 100% 以上。
    challenges = await repos.challenges.list_with_status(limit=500)
    if project_id:
        allowed = set(project_challenge_ids)
        challenges = [c for c in challenges if c.get("id") in allowed]
    total_challenges = len(challenges)
    running_list = [c for c in challenges if (c.get("status") or "") == "running"]
    attempted = sum(
        1 for c in challenges if (c.get("status") or "") or c.get("flagCount") or c.get("solved")
    )
    solved_by_challenge = sum(1 for c in challenges if c.get("solved"))

    # ------------------------------- 总量 -------------------------------
    flag_scope, flag_params = _conv_scope("f", project_id)
    msg_scope, msg_params = _conv_scope("m", project_id)
    tool_scope, tool_params = _conv_scope("t", project_id)
    fact_scope, fact_params = _conv_scope("fa", project_id)
    run_scope, run_params = _conv_scope("r", project_id)

    flags_total = await scalar(f"SELECT COUNT(*) AS n FROM flags f WHERE 1=1{flag_scope}", flag_params)
    if project_id:
        conversations_total = len(project_conversation_ids)
    else:
        conversations_total = await scalar("SELECT COUNT(*) AS n FROM conversations")
    messages_total = await scalar(f"SELECT COUNT(*) AS n FROM messages m WHERE 1=1{msg_scope}", msg_params)
    projects_total = 1 if project_id else await scalar("SELECT COUNT(*) AS n FROM projects")
    facts_total = await scalar(f"SELECT COUNT(*) AS n FROM facts fa WHERE 1=1{fact_scope}", fact_params)
    tool_calls = await scalar(f"SELECT COUNT(*) AS n FROM tool_executions t WHERE 1=1{tool_scope}", tool_params)
    tool_failures = await scalar(
        f"SELECT COUNT(*) AS n FROM tool_executions t WHERE t.is_error = 1{tool_scope}", tool_params
    )

    idea_params: list[Any] = []
    mem_params: list[Any] = []
    idea_filter = _in_clause("challenge_id", project_challenge_ids, idea_params) if project_id else ""
    mem_filter = _in_clause("challenge_id", project_challenge_ids, mem_params) if project_id else ""
    ideas_total = await scalar(f"SELECT COUNT(*) AS n FROM ideas WHERE 1=1{idea_filter}", tuple(idea_params))
    memories_total = await scalar(f"SELECT COUNT(*) AS n FROM memories WHERE 1=1{mem_filter}", tuple(mem_params))

    token_row = await db.fetchone(
        "SELECT COALESCE(SUM(r.prompt_tokens),0) AS p, COALESCE(SUM(r.completion_tokens),0) AS c, "
        f"COALESCE(SUM(r.model_calls),0) AS m FROM runs r WHERE 1=1{run_scope}",
        run_params,
    ) or {}

    # ------------------------------- 进行中 -------------------------------
    try:
        active_ids = list(runtime.steering.active_conversations())
        if project_id:
            active_ids = [cid for cid in active_ids if cid in project_conversation_ids]
        running_conversations = len(active_ids)
    except Exception:  # noqa: BLE001 - 运行时未就绪时不阻断看板
        running_conversations = 0
    try:
        sessions = runtime.challenge_sessions_info()
        if project_id:
            allowed = set(project_challenge_ids)
            sessions = [s for s in sessions if (s.get("challengeId") or s.get("challenge_id")) in allowed]
        # 注意：ChallengeSession.stats() 的字段名是 state（running/terminated/aborted），
        # 旧实现误用了 status，导致「进行中题目」恒为 0。
        running_challenges = sum(1 for s in sessions if (s.get("state") or "") == "running")
    except Exception:  # noqa: BLE001
        running_challenges = 0
    solver_params: list[Any] = []
    solver_filter = _in_clause("challenge_id", project_challenge_ids, solver_params) if project_id else ""
    active_solvers = await scalar(
        f"SELECT COUNT(*) AS n FROM solver_runs WHERE status = 'running'{solver_filter}", tuple(solver_params)
    )

    # -------------------------------- 趋势 --------------------------------
    flag_rows = await db.fetchall(
        f"SELECT f.submitted_at AS ts FROM flags f WHERE f.submitted_at >= ?{flag_scope} "
        "ORDER BY f.submitted_at DESC",
        (since, *flag_params),
    )
    conv_rows = await db.fetchall(
        "SELECT created_at AS ts FROM conversations WHERE created_at >= ?"
        + (" AND project_id = ?" if project_id else ""),
        (since, project_id) if project_id else (since,),
    )
    tool_rows = await db.fetchall(
        f"SELECT t.started_at AS ts FROM tool_executions t WHERE t.started_at >= ?{tool_scope}",
        (since, *tool_params),
    )
    trend = _build_trend(
        now,
        days,
        [float(r["ts"]) for r in flag_rows],
        [float(r["ts"]) for r in conv_rows],
        [float(r["ts"]) for r in tool_rows],
    )

    # ------------------------------ 类型分布 ------------------------------
    category_map: dict[str, dict[str, Any]] = {}
    for c in challenges:
        key = (c.get("category") or "misc") or "misc"
        item = category_map.setdefault(key, {"key": key, "total": 0, "solved": 0})
        item["total"] += 1
        if c.get("solved"):
            item["solved"] += 1
    categories = sorted(category_map.values(), key=lambda x: (-x["total"], x["key"]))

    # ------------------------------ 项目概览 ------------------------------
    # 始终返回全量（前端范围选择器要用），卡片只展示前 8 个
    project_rows = await db.fetchall(
        "SELECT p.id AS id, p.name AS name, p.status AS status, "
        "(SELECT COUNT(*) FROM conversations c WHERE c.project_id = p.id) AS conversations, "
        "(SELECT COUNT(*) FROM flags f JOIN conversations c2 ON c2.id = f.conversation_id "
        " WHERE c2.project_id = p.id) AS flags "
        "FROM projects p ORDER BY p.updated_at DESC LIMIT 50"
    )
    projects = [
        {
            "id": r["id"],
            "name": r.get("name") or r["id"],
            "status": r.get("status") or "",
            "conversations": int(r.get("conversations") or 0),
            "flags": int(r.get("flags") or 0),
            "current": r["id"] == project_id,
        }
        for r in project_rows
    ]

    # ------------------------------ 工具 Top ------------------------------
    tool_rows2 = await db.fetchall(
        "SELECT t.tool_name AS name, COUNT(*) AS calls, COALESCE(SUM(t.is_error),0) AS failures "
        f"FROM tool_executions t WHERE 1=1{tool_scope} "
        "GROUP BY t.tool_name ORDER BY calls DESC LIMIT 8",
        tool_params,
    )
    tools = [
        {
            "name": r["name"],
            "calls": int(r.get("calls") or 0),
            "failures": int(r.get("failures") or 0),
        }
        for r in tool_rows2
    ]

    # ------------------------------ 最近动态 ------------------------------
    recent_flag_rows = await db.fetchall(
        f"SELECT f.value, f.challenge_id, f.conversation_id, f.submitted_at FROM flags f "
        f"WHERE 1=1{flag_scope} ORDER BY f.submitted_at DESC LIMIT 6",
        flag_params,
    )
    recent_flags = [
        {
            "value": r.get("value") or "",
            "challengeId": r.get("challenge_id") or "",
            "conversationId": r.get("conversation_id") or "",
            "at": float(r.get("submitted_at") or 0) * 1000,
        }
        for r in recent_flag_rows
    ]
    recent_challenges = [
        {
            "id": c["id"],
            "name": c.get("name") or c["id"],
            "category": c.get("category") or "",
            "solved": bool(c.get("solved")),
            "status": c.get("status") or "",
            "flagCount": int(c.get("flagCount") or 0),
            "updatedAt": c.get("updatedAt") or 0,
        }
        for c in challenges[:6]
    ]

    flags_recent = await scalar(
        f"SELECT COUNT(*) AS n FROM flags f WHERE f.submitted_at >= ?{flag_scope}",
        (now - 7 * 86400.0, *flag_params),
    )
    # 捕获口径补充：有 flag 的会话数（历史上多为对话内解题，未绑定题目）
    flags_conversations = await scalar(
        f"SELECT COUNT(DISTINCT f.conversation_id) AS n FROM flags f "
        f"WHERE f.conversation_id IS NOT NULL{flag_scope}",
        flag_params,
    )

    # 解题数 = 已捕获 FLAG 数（「拿到 flag 即视为已解答」）；
    # 成功率按**题目维度**计算：有 flag 的题目数 / 题目总数。
    solved = flags_total
    # 题目为 0 时成功率无意义（返回 None，前端显示「—」而不是误导性的 0%）
    success_rate = (solved_by_challenge / total_challenges) if total_challenges else None

    return {
        "generatedAt": now * 1000,
        "windowDays": days,
        "scope": {
            "kind": "project" if project_id else "global",
            "projectId": project_id or "",
            "projectName": project_name,
        },
        "totals": {
            "challenges": total_challenges,
            "solved": solved,
            "solvedByChallenge": solved_by_challenge,
            "attempted": attempted,
            "successRate": round(success_rate, 4) if success_rate is not None else None,
            "flags": flags_total,
            "flagsRecent": flags_recent,
            "flagsConversations": flags_conversations,
            "conversations": conversations_total,
            "messages": messages_total,
            "projects": projects_total,
            "facts": facts_total,
            "ideas": ideas_total,
            "memories": memories_total,
            "toolCalls": tool_calls,
            "toolFailures": tool_failures,
            "promptTokens": int(token_row.get("p") or 0),
            "completionTokens": int(token_row.get("c") or 0),
            "modelCalls": int(token_row.get("m") or 0),
        },
        "live": {
            "runningConversations": running_conversations,
            "runningChallenges": running_challenges,
            "activeSolvers": active_solvers,
        },
        "status": {
            "solved": solved_by_challenge,
            "running": len(running_list),
            "unsolved": max(0, total_challenges - solved_by_challenge - len(running_list)),
        },
        "trend": trend,
        "categories": categories,
        "projects": projects,
        "tools": tools,
        "recentFlags": recent_flags,
        "recentChallenges": recent_challenges,
    }
