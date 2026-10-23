"""竞赛自动化模式（Competition Mode）—— M5 数据层。

设计：竞赛级全局黑板与题目级账本（memories/ideas）严格分离。
- ``competition_facts``：规则/协议/约束/反馈，跨题共享，append-only。
- ``capabilities``：文档解析产物；``enabled`` 默认 0，需连接测试通过后人工开启。
- ``platform_exercises``：平台题目 → 本地 challenge 的映射与状态。
- ``submissions``：提交去重与退避依据（唯一索引）。
- ``strategy_state``：令牌桶/退避/冷却/置顶等运行态（key-value）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..state.clock import now_iso
from ..storage.db import Database

__all__ = ["CompetitionRepo"]

COMPETITION_STATUSES = {"idle", "syncing", "running", "paused", "error"}
FACT_KINDS = {"rule", "protocol", "constraint", "credential", "feedback", "notice", "strategy"}
EXERCISE_STATUSES = {"locked", "open", "running", "solved", "failed"}
SUBMISSION_VERDICTS = {"correct", "wrong", "duplicate", "invalid", "rate_limited", "error"}


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:20]


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _normalize_competition(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "platformKind": row.get("platform_kind") or "generic_rest",
        "baseUrl": row.get("base_url") or "",
        "authKind": row.get("auth_kind") or "header",
        "authHeader": row.get("auth_header") or "",
        "authRef": row.get("auth_ref") or "",
        "status": row.get("status") or "idle",
        "strategy": _loads(row.get("strategy"), {}),
        "meta": _loads(row.get("meta"), {}),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
    }


def _normalize_fact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "competitionId": row["competition_id"],
        "kind": row["kind"],
        "summary": row.get("summary") or "",
        "body": row.get("body") or "",
        "source": row.get("source") or "doc",
        "ref": row.get("ref"),
        "createdAt": row.get("created_at"),
    }


def _normalize_capability(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"],
        "transport": row.get("transport") or "rest",
        "spec": _loads(row.get("spec"), {}),
        "confidence": row.get("confidence") or "medium",
        "enabled": bool(row.get("enabled")),
        "lastTestAt": row.get("last_test_at"),
        "lastTestOk": None if row.get("last_test_ok") is None else bool(row.get("last_test_ok")),
    }


def _normalize_exercise(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "exerciseKey": row["exercise_key"],
        "name": row.get("name") or "",
        "category": row.get("category") or "",
        "score": float(row.get("score") or 0),
        "difficulty": row.get("difficulty") or "",
        "status": row.get("status") or "open",
        "challengeId": row.get("challenge_id"),
        "env": _loads(row.get("env"), {}),
        "flagsGot": int(row.get("flags_got") or 0),
        "flagsTotal": int(row.get("flags_total") or 1),
        "attempts": int(row.get("attempts") or 0),
        "lastSync": row.get("last_sync"),
    }


def _mask_flag(value: str) -> str:
    """提交记录上屏用的脱敏 flag（保留前后少量字符，中间打码）。"""
    text = str(value or "")
    if len(text) <= 8:
        return text[:2] + "****"
    return text[:5] + "****" + text[-3:]


def _normalize_submission(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "exerciseKey": row["exercise_key"],
        "flag": row["flag"],
        # 前端只展示脱敏版：页面永不出现完整 flag
        "flagMasked": _mask_flag(row["flag"]),
        "verdict": row["verdict"],
        "platformMsg": row.get("platform_msg") or "",
        "createdAt": row.get("created_at"),
    }


class CompetitionRepo:
    """竞赛数据仓储（M5）。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 竞赛本体
    # ------------------------------------------------------------------
    async def create(
        self,
        *,
        name: str,
        base_url: str,
        platform_kind: str = "generic_rest",
        auth_kind: str = "header",
        auth_header: str = "",
        auth_ref: str = "",
        strategy: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        new = _new_id("comp_")
        now = now_iso()
        await self.db.execute(
            "INSERT INTO competitions(id, name, platform_kind, base_url, auth_kind, auth_header, "
            "auth_ref, status, strategy, meta, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new, name, platform_kind, base_url, auth_kind, auth_header, auth_ref,
                "idle", json.dumps(strategy or {}, ensure_ascii=False),
                json.dumps(meta or {}, ensure_ascii=False), now, now,
            ),
        )
        created = await self.get(new)
        assert created is not None
        return created

    async def get(self, competition_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM competitions WHERE id=?", (competition_id,))
        return _normalize_competition(row) if row else None

    async def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM competitions ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [_normalize_competition(r) for r in rows]

    async def update(
        self,
        competition_id: str,
        *,
        status: str | None = None,
        strategy: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        base_url: str | None = None,
        auth_header: str | None = None,
        auth_kind: str | None = None,
        auth_ref: str | None = None,
    ) -> dict[str, Any] | None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            if status not in COMPETITION_STATUSES:
                raise ValueError(f"非法状态：{status}")
            sets.append("status=?")
            params.append(status)
        if strategy is not None:
            sets.append("strategy=?")
            params.append(json.dumps(strategy, ensure_ascii=False))
        if meta is not None:
            sets.append("meta=?")
            params.append(json.dumps(meta, ensure_ascii=False))
        if base_url is not None:
            sets.append("base_url=?")
            params.append(base_url)
        if auth_header is not None:
            sets.append("auth_header=?")
            params.append(auth_header)
        if auth_kind is not None:
            sets.append("auth_kind=?")
            params.append(auth_kind)
        if auth_ref is not None:
            sets.append("auth_ref=?")
            params.append(auth_ref)
        if not sets:
            return await self.get(competition_id)
        sets.append("updated_at=?")
        params.append(now_iso())
        params.append(competition_id)
        await self.db.execute(f"UPDATE competitions SET {', '.join(sets)} WHERE id=?", tuple(params))
        return await self.get(competition_id)

    async def delete(self, competition_id: str) -> None:
        for table in ("competition_facts", "capabilities", "platform_exercises",
                      "submissions", "strategy_state"):
            await self.db.execute(f"DELETE FROM {table} WHERE competition_id=?", (competition_id,))
        await self.db.execute("DELETE FROM competitions WHERE id=?", (competition_id,))

    # ------------------------------------------------------------------
    # 竞赛全局黑板
    # ------------------------------------------------------------------
    async def add_fact(
        self,
        competition_id: str,
        *,
        kind: str,
        summary: str,
        body: str = "",
        source: str = "doc",
        ref: str | None = None,
    ) -> dict[str, Any]:
        if kind not in FACT_KINDS:
            raise ValueError(f"非法 fact kind：{kind}")
        new = _new_id("cfact_")
        await self.db.execute(
            "INSERT INTO competition_facts(id, competition_id, kind, summary, body, source, ref, "
            "created_at) VALUES(?,?,?,?,?,?,?,?)",
            (new, competition_id, kind, summary, body, source, ref, now_iso()),
        )
        row = await self.db.fetchone("SELECT * FROM competition_facts WHERE id=?", (new,))
        assert row is not None
        return _normalize_fact(row)

    async def list_facts(
        self,
        competition_id: str,
        *,
        kinds: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [competition_id]
        clause = ""
        if kinds:
            clause = f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        rows = await self.db.fetchall(
            f"SELECT * FROM competition_facts WHERE competition_id=?{clause} "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*params, limit),
        )
        return [_normalize_fact(r) for r in rows]

    async def count_facts(self, competition_id: str) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT kind, COUNT(*) AS n FROM competition_facts WHERE competition_id=? GROUP BY kind",
            (competition_id,),
        )
        return {str(r["kind"]): int(r["n"]) for r in rows}

    # ------------------------------------------------------------------
    # 能力注册（文档解析产物）
    # ------------------------------------------------------------------
    async def upsert_capability(
        self,
        competition_id: str,
        *,
        name: str,
        transport: str,
        spec: dict[str, Any],
        confidence: str = "medium",
    ) -> dict[str, Any]:
        await self.db.execute(
            "INSERT INTO capabilities(competition_id, name, transport, spec, confidence, enabled) "
            "VALUES(?,?,?,?,?,0) ON CONFLICT(competition_id, name) DO UPDATE SET "
            "transport=excluded.transport, spec=excluded.spec, confidence=excluded.confidence",
            (competition_id, name, transport, json.dumps(spec, ensure_ascii=False), confidence),
        )
        got = await self.get_capability(competition_id, name)
        assert got is not None
        return got

    async def get_capability(self, competition_id: str, name: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM capabilities WHERE competition_id=? AND name=?", (competition_id, name)
        )
        return _normalize_capability(row) if row else None

    async def list_capabilities(self, competition_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM capabilities WHERE competition_id=? ORDER BY name", (competition_id,)
        )
        return [_normalize_capability(r) for r in rows]

    async def set_capability_enabled(
        self, competition_id: str, name: str, enabled: bool
    ) -> dict[str, Any] | None:
        await self.db.execute(
            "UPDATE capabilities SET enabled=? WHERE competition_id=? AND name=?",
            (1 if enabled else 0, competition_id, name),
        )
        return await self.get_capability(competition_id, name)

    async def record_capability_test(
        self, competition_id: str, name: str, *, ok: bool
    ) -> None:
        await self.db.execute(
            "UPDATE capabilities SET last_test_at=?, last_test_ok=? "
            "WHERE competition_id=? AND name=?",
            (now_iso(), 1 if ok else 0, competition_id, name),
        )

    # ------------------------------------------------------------------
    # 平台题目池
    # ------------------------------------------------------------------
    async def upsert_exercise(
        self,
        competition_id: str,
        *,
        exercise_key: str,
        name: str = "",
        category: str = "",
        score: float = 0,
        difficulty: str = "",
        status: str = "open",
        flags_got: int = 0,
        flags_total: int = 1,
        env: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in EXERCISE_STATUSES:
            raise ValueError(f"非法题目状态：{status}")
        now = now_iso()
        await self.db.execute(
            "INSERT INTO platform_exercises(competition_id, exercise_key, name, category, score, "
            "difficulty, status, env, flags_got, flags_total, attempts, last_sync) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,0,?) ON CONFLICT(competition_id, exercise_key) DO UPDATE SET "
            "name=excluded.name, category=excluded.category, score=excluded.score, "
            "difficulty=excluded.difficulty, status=excluded.status, env=excluded.env, "
            "flags_got=excluded.flags_got, flags_total=excluded.flags_total, "
            "last_sync=excluded.last_sync",
            (competition_id, exercise_key, name, category, float(score), difficulty, status,
             json.dumps(env or {}, ensure_ascii=False), int(flags_got), int(flags_total), now),
        )
        got = await self.get_exercise(competition_id, exercise_key)
        assert got is not None
        return got

    async def get_exercise(self, competition_id: str, exercise_key: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM platform_exercises WHERE competition_id=? AND exercise_key=?",
            (competition_id, exercise_key),
        )
        return _normalize_exercise(row) if row else None

    async def list_exercises(
        self, competition_id: str, *, statuses: list[str] | None = None
    ) -> list[dict[str, Any]]:
        params: list[Any] = [competition_id]
        clause = ""
        if statuses:
            clause = f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        rows = await self.db.fetchall(
            f"SELECT * FROM platform_exercises WHERE competition_id=?{clause} "
            "ORDER BY score DESC, exercise_key",
            tuple(params),
        )
        return [_normalize_exercise(r) for r in rows]

    async def bind_challenge(
        self, competition_id: str, exercise_key: str, challenge_id: str
    ) -> None:
        await self.db.execute(
            "UPDATE platform_exercises SET challenge_id=? WHERE competition_id=? AND exercise_key=?",
            (challenge_id, competition_id, exercise_key),
        )

    async def bump_attempt(self, competition_id: str, exercise_key: str) -> int:
        await self.db.execute(
            "UPDATE platform_exercises SET attempts=attempts+1 "
            "WHERE competition_id=? AND exercise_key=?",
            (competition_id, exercise_key),
        )
        row = await self.db.fetchone(
            "SELECT attempts FROM platform_exercises WHERE competition_id=? AND exercise_key=?",
            (competition_id, exercise_key),
        )
        return int((row or {}).get("attempts") or 0)

    # ------------------------------------------------------------------
    # 提交记录
    # ------------------------------------------------------------------
    async def add_submission(
        self,
        competition_id: str,
        *,
        exercise_key: str,
        flag: str,
        verdict: str,
        platform_msg: str = "",
    ) -> dict[str, Any] | None:
        """写入提交记录；同 (竞赛, 题目, flag) 已存在时返回 None（幂等去重）。"""
        if verdict not in SUBMISSION_VERDICTS:
            raise ValueError(f"非法 verdict：{verdict}")
        new = _new_id("sub_")
        try:
            await self.db.execute(
                "INSERT INTO submissions(id, competition_id, exercise_key, flag, verdict, "
                "platform_msg, created_at) VALUES(?,?,?,?,?,?,?)",
                (new, competition_id, exercise_key, flag, verdict, platform_msg, now_iso()),
            )
        except Exception:
            return None
        row = await self.db.fetchone("SELECT * FROM submissions WHERE id=?", (new,))
        return _normalize_submission(row) if row else None

    async def list_submissions(
        self, competition_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM submissions WHERE competition_id=? ORDER BY created_at DESC, rowid DESC "
            "LIMIT ?",
            (competition_id, limit),
        )
        return [_normalize_submission(r) for r in rows]

    async def count_verdicts(self, competition_id: str) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT verdict, COUNT(*) AS n FROM submissions WHERE competition_id=? GROUP BY verdict",
            (competition_id,),
        )
        return {str(r["verdict"]): int(r["n"]) for r in rows}

    # ------------------------------------------------------------------
    # 策略运行态（key-value）
    # ------------------------------------------------------------------
    async def get_state(self, competition_id: str, key: str) -> Any:
        row = await self.db.fetchone(
            "SELECT value FROM strategy_state WHERE competition_id=? AND key=?",
            (competition_id, key),
        )
        if not row:
            return None
        return _loads(row["value"], row["value"])

    async def set_state(self, competition_id: str, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT INTO strategy_state(competition_id, key, value, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(competition_id, key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (competition_id, key, json.dumps(value, ensure_ascii=False), now_iso()),
        )

    async def delete_state(self, competition_id: str, key: str) -> None:
        await self.db.execute(
            "DELETE FROM strategy_state WHERE competition_id=? AND key=?", (competition_id, key)
        )

    async def list_state(self, competition_id: str) -> dict[str, Any]:
        rows = await self.db.fetchall(
            "SELECT key, value FROM strategy_state WHERE competition_id=?", (competition_id,)
        )
        return {str(r["key"]): _loads(r["value"], r["value"]) for r in rows}
