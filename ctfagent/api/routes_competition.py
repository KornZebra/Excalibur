"""竞赛自动化模式 API（M5）：竞赛 CRUD / 全局黑板 / 能力注册 / 题目池 / 提交记录。

设计原则：
- 所有自动化能力默认关闭（capabilities.enabled=0），必须经连接测试 + 人工确认；
- 黑板只存「引用」不存明文凭证（auth_ref 指向 config/环境变量）；
- 竞赛级黑板与题目级账本分离，接口按 competition_id 组织。
"""

from __future__ import annotations

import json
import re
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..runtime import Runtime
from .routes_agent import get_runtime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["competition"])


class CreateCompetitionRequest(BaseModel):
    """创建竞赛：只需名称。

    base_url / platform_kind / 鉴权方式都由**文档解析**得出后回填——
    用户不该在还没看到文档时先猜这些参数。
    """

    name: str = Field(min_length=1, max_length=120)
    base_url: str = ""
    platform_kind: Literal["dasctf", "tencent_mcp", "generic_rest"] = "generic_rest"
    auth_kind: Literal["header", "bearer"] = "header"
    auth_header: str = ""
    auth_ref: str = ""
    strategy: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


class UpdateCompetitionRequest(BaseModel):
    status: Literal["idle", "syncing", "running", "paused", "error"] | None = None
    strategy: dict[str, Any] | None = None
    base_url: str | None = None
    auth_header: str | None = None
    auth_ref: str | None = None


class AddBoardFactRequest(BaseModel):
    kind: Literal["rule", "protocol", "constraint", "credential", "feedback", "notice", "strategy"] = "notice"
    summary: str = Field(min_length=1)
    body: str = ""
    ref: str | None = None


def _rt(request: Request) -> Runtime:
    return get_runtime(request)


_DOC_TOKEN_PATTERNS = [
    re.compile(r"\b(nss_agent_[A-Za-z0-9]{8,})"),
    re.compile(r"\b(ak_live_[A-Za-z0-9]{8,})"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{12,})"),
    re.compile(r"Bearer\s+([A-Za-z0-9_.\-]{12,})", re.I),
]


def _extract_doc_token(text: str) -> str:
    """从文档原文提取平台 token（占位符 xxx/... 不算）。"""
    for pattern in _DOC_TOKEN_PATTERNS:
        for match in pattern.finditer(text or ""):
            token = match.group(1)
            low = token.lower()
            if "xxx" in low or low.endswith("...") or low.endswith("***"):
                continue
            return token
    return ""


async def _require_competition(runtime: Runtime, competition_id: str) -> dict[str, Any]:
    comp = await runtime.repos.competitions.get(competition_id)
    if comp is None:
        raise HTTPException(404, "竞赛不存在")
    return comp


# ---------------------------------------------------------------------------
# 竞赛 CRUD
# ---------------------------------------------------------------------------
@router.get("/api/competitions", summary="列出竞赛")
async def list_competitions(request: Request) -> dict[str, Any]:
    runtime = _rt(request)
    items = await runtime.repos.competitions.list()
    return {"competitions": items}


@router.post("/api/competitions", summary="创建竞赛（平台接入配置）")
async def create_competition(payload: CreateCompetitionRequest, request: Request) -> dict[str, Any]:
    runtime = _rt(request)
    comp = await runtime.repos.competitions.create(
        name=payload.name,
        base_url=payload.base_url,
        platform_kind=payload.platform_kind,
        auth_kind=payload.auth_kind,
        auth_header=payload.auth_header,
        auth_ref=payload.auth_ref,
        strategy=payload.strategy,
        meta=payload.meta,
    )
    await runtime.repos.competitions.add_fact(
        comp["id"],
        kind="notice",
        summary=f"竞赛创建：{comp['name']}（{comp['platformKind']}）",
        body=f"base_url={comp['baseUrl']}\nauth={comp['authKind']}:{comp['authHeader'] or '-'}\n"
             f"auth_ref={comp['authRef'] or '-'}",
        source="human",
    )
    return comp


@router.get("/api/competitions/{competition_id}", summary="竞赛详情")
async def get_competition(competition_id: str, request: Request) -> dict[str, Any]:
    runtime = _rt(request)
    comp = await _require_competition(runtime, competition_id)
    counts = await runtime.repos.competitions.count_facts(competition_id)
    return {"competition": comp, "factCounts": counts}


@router.patch("/api/competitions/{competition_id}", summary="更新竞赛（状态/策略/接入）")
async def update_competition(
    competition_id: str, payload: UpdateCompetitionRequest, request: Request
) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    try:
        updated = await runtime.repos.competitions.update(
            competition_id,
            status=payload.status,
            strategy=payload.strategy,
            base_url=payload.base_url,
            auth_header=payload.auth_header,
            auth_ref=payload.auth_ref,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return updated or {}


@router.delete("/api/competitions/{competition_id}", summary="删除竞赛（含黑板与题目池）")
async def delete_competition(competition_id: str, request: Request) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    await runtime.repos.competitions.delete(competition_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 竞赛全局黑板
# ---------------------------------------------------------------------------
@router.get("/api/competitions/{competition_id}/board", summary="竞赛全局黑板（按 kind 分组）")
async def get_board(competition_id: str, request: Request, limit: int = 200) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    facts = await runtime.repos.competitions.list_facts(competition_id, limit=limit)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        grouped.setdefault(str(fact["kind"]), []).append(fact)
    return {"facts": facts, "byKind": grouped, "counts": await runtime.repos.competitions.count_facts(competition_id)}


@router.post("/api/competitions/{competition_id}/board", summary="人工追加黑板事实（hint/规则修正）")
async def add_board_fact(
    competition_id: str, payload: AddBoardFactRequest, request: Request
) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    return await runtime.repos.competitions.add_fact(
        competition_id,
        kind=payload.kind,
        summary=payload.summary,
        body=payload.body,
        source="human",
        ref=payload.ref,
    )


# ---------------------------------------------------------------------------
# 能力注册
# ---------------------------------------------------------------------------
@router.get("/api/competitions/{competition_id}/capabilities", summary="能力清单（文档解析产物）")
async def list_capabilities(competition_id: str, request: Request) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    items = await runtime.repos.competitions.list_capabilities(competition_id)
    return {"capabilities": items, "enabled": sum(1 for i in items if i["enabled"])}


@router.post(
    "/api/competitions/{competition_id}/capabilities/{name}/enable",
    summary="启用/停用某项能力（默认关闭，需人工确认）",
)
async def enable_capability(
    competition_id: str, name: str, request: Request, enabled: bool = True
) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    got = await runtime.repos.competitions.set_capability_enabled(competition_id, name, enabled)
    if got is None:
        raise HTTPException(404, f"能力不存在：{name}")
    return got


# ---------------------------------------------------------------------------
# 题目池 / 提交 / 状态
# ---------------------------------------------------------------------------
@router.get("/api/competitions/{competition_id}/exercises", summary="平台题目池")
async def list_exercises(competition_id: str, request: Request) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    return {"exercises": await runtime.repos.competitions.list_exercises(competition_id)}


@router.post(
    "/api/competitions/{competition_id}/pin/{exercise_key}",
    summary="指定题目（置顶优先，pinned=false 取消）",
)
async def pin_exercise(
    competition_id: str, exercise_key: str, request: Request, pinned: bool = True
) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    key = f"pinned:{exercise_key}"
    if pinned:
        await runtime.repos.competitions.set_state(competition_id, key, True)
    else:
        await runtime.repos.competitions.delete_state(competition_id, key)
    return {"exerciseKey": exercise_key, "pinned": pinned}


@router.get("/api/competitions/{competition_id}/submissions", summary="提交记录")
async def list_submissions(competition_id: str, request: Request, limit: int = 100) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    items = await runtime.repos.competitions.list_submissions(competition_id, limit=limit)
    return {"submissions": items, "counts": await runtime.repos.competitions.count_verdicts(competition_id)}


@router.get("/api/competitions/{competition_id}/status", summary="竞赛运行状态汇总")
async def competition_status(competition_id: str, request: Request) -> dict[str, Any]:
    runtime = _rt(request)
    comp = await _require_competition(runtime, competition_id)
    repo = runtime.repos.competitions
    exercises = await repo.list_exercises(competition_id)
    caps = await repo.list_capabilities(competition_id)
    return {
        "competition": comp,
        "exercises": {
            "total": len(exercises),
            "open": sum(1 for e in exercises if e["status"] == "open"),
            "running": sum(1 for e in exercises if e["status"] == "running"),
            "solved": sum(1 for e in exercises if e["status"] == "solved"),
            "locked": sum(1 for e in exercises if e["status"] == "locked"),
            "flagsGot": sum(e["flagsGot"] for e in exercises),
            "flagsTotal": sum(e["flagsTotal"] for e in exercises),
        },
        "capabilities": {"total": len(caps), "enabled": sum(1 for c in caps if c["enabled"])},
        "submissions": await repo.count_verdicts(competition_id),
        "state": await repo.list_state(competition_id),
        # N2：指挥台可视化所需（限流/实例/自动派发）
        "runtime": {
            "rateLimitPerSec": float((comp.get("strategy") or {}).get("rateLimitPerSec") or 1),
            "maxInstances": int((comp.get("strategy") or {}).get("maxInstances") or 3),
            "runningInstances": sum(1 for e in exercises if e["status"] == "running"),
            "autoSolve": bool((comp.get("strategy") or {}).get("autoSolve")),
            "hintEnabled": bool((comp.get("strategy") or {}).get("hintEnabled")),
        },
    }


# ---------------------------------------------------------------------------
# M6：文档解析（官方文档 → 能力规格 → 黑板）
# ---------------------------------------------------------------------------
class IngestDocRequest(BaseModel):
    text: str = Field(min_length=20, description="官方文档 Markdown 原文")
    apply: bool = Field(default=False, description="校验通过后是否直接落库（否则仅预览）")
    use_ai: bool = Field(default=True, description="是否启用 AI 分析增强（失败不影响规则抽取）")


@router.post("/api/competitions/{competition_id}/ingest-doc", summary="解析官方文档并沉淀到竞赛黑板")
async def ingest_doc(
    competition_id: str, payload: IngestDocRequest, request: Request
) -> dict[str, Any]:
    """解析 → 校验 → （可选）落库。

    ``apply=false`` 时只预览（便于人工核对路径）；落库后能力仍为关闭状态，
    需连接测试通过并显式启用。
    """
    from ..competition.doc_parser import extract_facts, persist_facts

    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    facts, notes = await extract_facts(
        payload.text,
        getattr(runtime, "model", None),
        use_ai=payload.use_ai,
    )
    protocol_count = sum(1 for f in facts if f.get("kind") == "protocol")
    result: dict[str, Any] = {
        "facts": facts,
        "notes": notes,
        "protocolCount": protocol_count,
        "errors": [] if facts else ["文档未产出任何事实：请确认内容非空且包含接口/规则描述"],
        "valid": bool(facts),
        "applied": False,
    }
    if payload.apply and facts:
        result["persisted"] = await persist_facts(runtime.repos.competitions, competition_id, facts)
        # 从文档事实里推断接入参数并回填（用户创建时不填这些）
        from ..competition.doc_parser import (
            _extract_auth as _auth,
            _extract_base_url as _base,
        )

        updates: dict[str, Any] = {}
        base_url = _base(payload.text)
        auth_kind, auth_header = _auth(payload.text)
        if base_url:
            updates["base_url"] = base_url
        if auth_header:
            updates["auth_header"] = auth_header
        if auth_kind:
            updates["auth_kind"] = auth_kind
        if updates:
            await runtime.repos.competitions.update(competition_id, **updates)
        # 凭证自动提取：文档里的 token 直接进进程内存（不落库），免去手动粘贴
        token = _extract_doc_token(payload.text)
        if token:
            store = getattr(runtime, "_competition_secrets", None)
            if store is None:
                store = {}
                setattr(runtime, "_competition_secrets", store)
            store[competition_id] = token
            await runtime.repos.competitions.add_fact(
                competition_id, kind="credential",
                summary="凭证已从文档自动提取（进程内存态，不落库）",
                body=f"auth_header={auth_header or 'Authorization'}\n长度={len(token)}\n"
                     "（明文仅存于进程内存，重启或重新应用文档后自动恢复）",
                source="doc", ref="credential:doc",
            )
            result["credential"] = "auto"
        result["applied"] = True
        result["backfilled"] = {
            "baseUrl": base_url,
            "authKind": auth_kind,
            "authHeader": auth_header,
        }
    return result


# ---------------------------------------------------------------------------
# M7：连接测试（只读能力 dry-run，通过后自动启用）
# ---------------------------------------------------------------------------
async def _constraints_of(runtime: Runtime, competition_id: str) -> dict[str, Any]:
    """从竞赛黑板的 constraint fact 读取平台约束（限流/实例/扣分）。"""
    facts = await runtime.repos.competitions.list_facts(competition_id, kinds=["constraint"], limit=1)
    if not facts:
        return {}
    try:
        return json.loads(facts[0].get("body") or "{}")
    except ValueError:
        return {}


@router.post("/api/competitions/{competition_id}/test", summary="连接测试（只读接口 dry-run）")
async def test_competition(competition_id: str, request: Request) -> dict[str, Any]:
    """对黑板中的**只读（GET）**接口发起真实调用，验证链路与凭证。

    设计（S3 黑板驱动）：
    - 能力来自黑板 protocol 事实，而非已废弃的 capabilities 表；
    - **只探测 GET 接口**：POST /next 会误领题、POST /submit 会误提交 flag，
      连接测试绝不触发任何写操作；
    - 逐个调用并按 error_kind 返回，便于区分鉴权 / 限流 / 协议解析问题。
    """
    from ..competition.adapters import build_adapter

    runtime = _rt(request)
    comp = await _require_competition(runtime, competition_id)
    engine = _engine(runtime, competition_id)
    facts = await runtime.repos.competitions.list_facts(competition_id, limit=500)
    caps = engine._caps_from_board(facts)
    if not caps:
        raise HTTPException(
            400, "黑板里还没有接口事实：请先在「文档解析」页把官方文档存入黑板"
        )
    constraints = await _constraints_of(runtime, competition_id)
    adapter = build_adapter(comp, caps, constraints=constraints, only_enabled=False, runtime=runtime)
    results: dict[str, Any] = {}
    skipped: list[str] = []
    try:
        for name, cap in caps.items():
            spec = cap.get("spec") or {}
            method = str(spec.get("method") or "GET").upper()
            path_tpl = str(spec.get("path") or "")
            if method != "GET":
                skipped.append(name)  # 写接口：连接测试不触发
                continue
            if "{" in path_tpl:
                skipped.append(name)  # 带路径参数的接口无法无水槽调用
                continue
            if re.search(r"/\d+(?=/|$)", path_tpl):
                skipped.append(name)  # 文档示例里的字面 ID 路径（/attempt/123/）非真接口
                continue
            res = await adapter.call(name)
            results[name] = res.to_dict()
    finally:
        await adapter.close()
    ok_count = sum(1 for r in results.values() if r.get("ok"))
    return {
        "results": results,
        "skipped": skipped,
        "capabilities": len(caps),
        "tested": len(results),
        "enabled": ok_count,
        "ok": ok_count,
    }


# ---------------------------------------------------------------------------
# M8：策略引擎（启动/暂停/同步）
# ---------------------------------------------------------------------------
def _engine(runtime: Runtime, competition_id: str, *, interval_seconds: float = 10.0):
    """取（或创建）竞赛引擎实例，挂在 runtime 上以复用后台任务。"""
    from ..competition.engine import CompetitionEngine

    store = getattr(runtime, "_competition_engines", None)
    if store is None:
        store = {}
        setattr(runtime, "_competition_engines", store)
    engine = store.get(competition_id)
    if engine is None:
        engine = CompetitionEngine(runtime, competition_id, interval_seconds=interval_seconds)
        store[competition_id] = engine
    return engine


@router.post("/api/competitions/{competition_id}/sync", summary="同步规则/题目/公告到黑板")
async def sync_competition(competition_id: str, request: Request) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    engine = _engine(runtime, competition_id)
    try:
        return await engine.sync()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/competitions/{competition_id}/start", summary="启动自动化（策略引擎主循环）")
async def start_competition(
    competition_id: str, request: Request, interval_seconds: float = 10.0
) -> dict[str, Any]:
    runtime = _rt(request)
    comp = await _require_competition(runtime, competition_id)
    counts = await runtime.repos.competitions.count_facts(competition_id)
    if not (counts.get("protocol") or 0) and not (counts.get("rule") or 0):
        raise HTTPException(
            400, "黑板里还没有接口/规则事实：请先在「文档解析」页把官方文档存入黑板"
        )
    engine = _engine(runtime, competition_id, interval_seconds=interval_seconds)
    result = await engine.start()
    result["competition"] = comp["id"]
    return result


@router.post("/api/competitions/{competition_id}/pause", summary="暂停自动化")
async def pause_competition(competition_id: str, request: Request) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    return await _engine(runtime, competition_id).pause()


@router.post("/api/competitions/{competition_id}/tick", summary="手动执行一轮（调试用）")
async def tick_competition(competition_id: str, request: Request) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    engine = _engine(runtime, competition_id)
    return await engine.tick()


# ---------------------------------------------------------------------------
# N2：SSE 决策日志 / 手动提交 / 复盘导出 / 能力 spec 编辑
# ---------------------------------------------------------------------------
@router.get("/api/competitions/{competition_id}/logs", summary="策略引擎决策日志（SSE）")
async def stream_logs(competition_id: str, request: Request) -> Any:
    """历史回放 + 实时推送 + 15s 心跳。

    每条事件为 JSON：``{id, at, phase, level, message}``；
    phase ∈ SYNC/SCHEDULE/PREPARE_ENV/DISPATCH/SUBMIT/FEEDBACK/ERROR/SYSTEM。
    """
    import asyncio

    from fastapi.responses import StreamingResponse

    from ..competition.logbus import LOG_BUS

    runtime = _rt(request)
    await _require_competition(runtime, competition_id)

    async def gen():
        for entry in LOG_BUS.history(competition_id, 200):
            yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
        queue = LOG_BUS.subscribe(competition_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
        finally:
            LOG_BUS.unsubscribe(competition_id, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ManualSubmitRequest(BaseModel):
    exercise_key: str = Field(min_length=1)
    flag: str = Field(min_length=1, max_length=256)


@router.post("/api/competitions/{competition_id}/submit", summary="手动兜底提交 flag")
async def manual_submit(
    competition_id: str, payload: ManualSubmitRequest, request: Request
) -> dict[str, Any]:
    """人工提交：跳过策略引擎，但仍走去重与平台判定，结果写入提交记录与黑板。"""
    from ..competition.adapters import build_adapter
    from ..competition.logbus import LOG_BUS
    from ..competition.strategy import DEFAULT_STRATEGY

    runtime = _rt(request)
    comp = await _require_competition(runtime, competition_id)
    repo = runtime.repos.competitions
    exercise = await repo.get_exercise(competition_id, payload.exercise_key)
    if exercise is None:
        raise HTTPException(404, "题目不存在于该竞赛的题目池")
    existing = await repo.list_submissions(competition_id)
    if any(s["flag"] == payload.flag and s["exerciseKey"] == payload.exercise_key
           for s in existing):
        raise HTTPException(400, "该 flag 已提交过（去重拦截）")

    caps = await repo.list_capabilities(competition_id)
    constraints: dict[str, Any] = {}
    facts = await repo.list_facts(competition_id, kinds=["constraint"], limit=1)
    if facts:
        try:
            constraints = json.loads(facts[0].get("body") or "{}")
        except ValueError:
            constraints = {}
    adapter = build_adapter(comp, caps, constraints=constraints, only_enabled=False, runtime=runtime)
    strategy = {**DEFAULT_STRATEGY, **(comp.get("strategy") or {})}
    try:
        LOG_BUS.publish(
            competition_id, "SUBMIT",
            f"人工提交：{exercise['name'] or payload.exercise_key}（{len(payload.flag)} 字符）",
        )
        res = await adapter.call("submit_flag", exercise_key=payload.exercise_key, flag=payload.flag)
    finally:
        await adapter.close()

    verdict = "correct" if res.ok else ("invalid" if res.error_kind == "invalid_flag" else "error")
    await repo.add_submission(
        competition_id, exercise_key=payload.exercise_key, flag=payload.flag,
        verdict=verdict, platform_msg=res.message,
    )
    if res.ok:
        got = exercise["flagsGot"] + 1
        total = exercise["flagsTotal"]
        await repo.upsert_exercise(
            competition_id, exercise_key=payload.exercise_key, name=exercise["name"],
            category=exercise["category"], score=exercise["score"],
            difficulty=exercise["difficulty"],
            status="solved" if got >= total else "running",
            flags_got=got, flags_total=total, env=exercise["env"],
        )
        LOG_BUS.publish(
            competition_id, "FEEDBACK",
            f"人工提交成功：{payload.exercise_key}（{got}/{total}）", level="success",
        )
    else:
        LOG_BUS.publish(
            competition_id, "FEEDBACK",
            f"人工提交失败：{payload.exercise_key} — {res.message[:80]}", level="warn",
        )
    return {
        "ok": res.ok,
        "verdict": verdict,
        "message": res.message,
        "strategyApplied": bool(strategy.get("flagRegex")),
    }


@router.get("/api/competitions/{competition_id}/export", summary="导出复盘时间线")
async def export_competition(competition_id: str, request: Request, format: str = "timeline"):
    """timeline：人类可读事件流；yaml：结构化快照。"""
    from fastapi.responses import Response

    runtime = _rt(request)
    comp = await _require_competition(runtime, competition_id)
    repo = runtime.repos.competitions
    facts = await repo.list_facts(competition_id, limit=500)
    exercises = await repo.list_exercises(competition_id)
    subs = await repo.list_submissions(competition_id, limit=500)

    if format == "yaml":
        payload = {
            "competition": {k: comp[k] for k in ("id", "name", "platformKind", "baseUrl", "status")},
            "facts": [{k: f[k] for k in ("id", "kind", "summary", "source", "createdAt")} for f in facts],
            "exercises": exercises,
            "submissions": [{k: s[k] for k in ("exerciseKey", "verdict", "createdAt")} for s in subs],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(content=text, media_type="application/json")

    events: list[tuple[str, str]] = [(
        "",
        f"COMPETITION {comp['id']} ({comp['name']})\n"
        f"  platform: {comp['platformKind']}\n  base_url: {comp['baseUrl']}\n"
        f"  status: {comp['status']}",
    )]
    for f in facts:
        events.append((str(f.get("createdAt") or ""),
                       f"BOARD [{f['kind']}] {f['summary']}\n  source: {f['source']}"))
    for e in exercises:
        events.append((str(e.get("lastSync") or ""),
                       f"EXERCISE {e['exerciseKey']} {e['name']} [{e['status']}] "
                       f"score={e['score']} flags={e['flagsGot']}/{e['flagsTotal']} "
                       f"attempts={e['attempts']}"))
    for s in subs:
        events.append((str(s.get("createdAt") or ""),
                       f"SUBMIT {s['exerciseKey']} → {s['verdict']}  {s['platformMsg'][:120]}"))
    events.sort(key=lambda x: x[0])
    text = "\n\n".join(block for _, block in events) + "\n"
    return Response(content=text, media_type="text/plain; charset=utf-8")


class UpdateCapabilityRequest(BaseModel):
    spec: dict[str, Any]
    confidence: Literal["high", "medium", "low"] | None = None


@router.patch(
    "/api/competitions/{competition_id}/capabilities/{name}",
    summary="编辑能力 spec（修复解析错误）",
)
async def update_capability(
    competition_id: str, name: str, payload: UpdateCapabilityRequest, request: Request
) -> dict[str, Any]:
    runtime = _rt(request)
    await _require_competition(runtime, competition_id)
    repo = runtime.repos.competitions
    cap = await repo.get_capability(competition_id, name)
    if cap is None:
        raise HTTPException(404, f"能力不存在：{name}")
    spec = payload.spec or {}
    if cap["transport"] == "mcp":
        if not str(spec.get("tool") or "").strip():
            raise HTTPException(400, "MCP 能力必须提供 tool 字段")
    else:
        if not str(spec.get("path") or "").strip():
            raise HTTPException(400, "REST 能力必须提供 path 字段")
    updated = await repo.upsert_capability(
        competition_id, name=name, transport=cap["transport"],
        spec=spec, confidence=payload.confidence or cap["confidence"],
    )
    await repo.add_fact(
        competition_id, kind="protocol",
        summary=f"能力 {name} 的 spec 已被人工修正",
        body=json.dumps(spec, ensure_ascii=False, indent=2), source="human",
        ref=f"capability:{name}",
    )
    return updated


# ---------------------------------------------------------------------------
# P1：凭证提供（内存态，永不落库/不上屏）
# ---------------------------------------------------------------------------
class CredentialRequest(BaseModel):
    value: str = Field(min_length=1, max_length=512, description="平台 AccessKey / Token 明文")


@router.post("/api/competitions/{competition_id}/credential", summary="提供平台凭证（仅存进程内存）")
async def provide_credential(
    competition_id: str, payload: CredentialRequest, request: Request
) -> dict[str, Any]:
    """把凭证存进进程内存（重启失效），只回显脱敏形态。

    设计：凭证**不写数据库、不写配置文件、不进 prompt**；黑板只记「已提供」这一事实。
    """
    from ..competition.logbus import LOG_BUS

    runtime = _rt(request)
    comp = await _require_competition(runtime, competition_id)
    store = getattr(runtime, "_competition_secrets", None)
    if store is None:
        store = {}
        setattr(runtime, "_competition_secrets", store)
    store[competition_id] = payload.value
    value = payload.value
    masked = (value[:4] + "****" + value[-2:]) if len(value) > 10 else "****"
    await runtime.repos.competitions.add_fact(
        competition_id, kind="credential",
        summary="凭证已提供（进程内存态，不落库）",
        body=f"auth_header={comp.get('authHeader') or '-'}\n已提供长度={len(value)}\n"
             "（明文仅存于进程内存，重启需重新提供）",
        source="human", ref="credential:memory",
    )
    LOG_BUS.publish(competition_id, "SYSTEM", "凭证已更新（内存态，已脱敏）", level="success")
    return {"ok": True, "masked": masked, "authHeader": comp.get("authHeader") or ""}
