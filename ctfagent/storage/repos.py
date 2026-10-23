"""仓储层：conversations / messages / tool_executions / facts / challenges / flags / traces / runs。

所有方法都是 async，内部复用 :class:`~ctfagent.storage.db.Database` 的串行化执行。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ..models.types import ChatMessage, ToolCall, Usage
from ..scope import parse_scope
from ..state.clock import now_iso as _now_iso
from ..state.clock import seconds_since as _seconds_since
from ..textsafe import clean_text, clean_value
from .db import Database


def new_id(prefix: str = "") -> str:
    """生成短 ID（带可选前缀）。"""
    raw = uuid.uuid4().hex[:20]
    return f"{prefix}{raw}" if prefix else raw


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


class ConversationRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        *,
        conversation_id: str | None = None,
        title: str = "",
        challenge_id: str | None = None,
        agent_id: str = "ctf-agent",
        meta: dict[str, Any] | None = None,
        project_id: str | None = None,
        hitl_reviewer: str | None = None,
    ) -> dict[str, Any]:
        cid = conversation_id or new_id("conv_")
        now = time.time()
        await self.db.execute(
            "INSERT INTO conversations(id, title, challenge_id, agent_id, created_at, updated_at, meta, "
            "project_id, hitl_reviewer) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
            (
                cid,
                title or "未命名会话",
                challenge_id,
                agent_id,
                now,
                now,
                _dumps(meta or {}),
                project_id,
                hitl_reviewer,
            ),
        )
        row = await self.get(cid)
        assert row is not None
        return row

    async def get(self, conversation_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM conversations WHERE id=?", (conversation_id,))
        return _normalize_conversation(row) if row else None

    async def get_or_create(
        self, conversation_id: str | None, *, project_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if conversation_id:
            existing = await self.get(conversation_id)
            if existing:
                return existing
        return await self.create(conversation_id=conversation_id, project_id=project_id, **kwargs)

    async def list(
        self, limit: int = 100, offset: int = 0, *, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        if project_id:
            rows = await self.db.fetchall(
                "SELECT c.*, "
                "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count "
                "FROM conversations c WHERE c.project_id=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (project_id, limit, offset),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT c.*, "
                "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count "
                "FROM conversations c ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return [_normalize_conversation(r) for r in rows]

    async def set_project(self, conversation_id: str, project_id: str | None) -> dict[str, Any] | None:
        """设置会话的项目归属（``None`` 表示解除归属）；会话不存在时返回 None。"""
        existing = await self.get(conversation_id)
        if existing is None:
            return None
        await self.db.execute(
            "UPDATE conversations SET project_id=?, updated_at=? WHERE id=?",
            (project_id, time.time(), conversation_id),
        )
        return await self.get(conversation_id)

    async def clear_project(self, project_id: str) -> int:
        """把归属于某项目的全部会话置空（U4 项目删除的 detach 模式）；返回受影响行数。"""
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM conversations WHERE project_id=?", (project_id,)
        )
        await self.db.execute("UPDATE conversations SET project_id=NULL WHERE project_id=?", (project_id,))
        return int((row or {}).get("n") or 0)

    async def rename(self, conversation_id: str, title: str) -> dict[str, Any] | None:
        """重命名会话（U2c）；会话不存在时返回 None。"""
        existing = await self.get(conversation_id)
        if existing is None:
            return None
        await self.db.execute(
            "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
            (title, time.time(), conversation_id),
        )
        return await self.get(conversation_id)

    async def set_reviewer(self, conversation_id: str, reviewer: str | None) -> dict[str, Any] | None:
        """设置会话级审查模式（U6a）；``None`` 表示清除覆盖（跟随全局）。会话不存在时返回 None。"""
        existing = await self.get(conversation_id)
        if existing is None:
            return None
        await self.db.execute(
            "UPDATE conversations SET hitl_reviewer=? WHERE id=?",
            (reviewer, conversation_id),
        )
        return await self.get(conversation_id)

    async def set_challenge(self, conversation_id: str, challenge_id: str | None) -> dict[str, Any] | None:
        """绑定/解绑会话与题目；绑定后该会话的解题在题目工作区与上下文中进行。"""
        existing = await self.get(conversation_id)
        if existing is None:
            return None
        await self.db.execute(
            "UPDATE conversations SET challenge_id=?, updated_at=? WHERE id=?",
            (challenge_id, time.time(), conversation_id),
        )
        return await self.get(conversation_id)

    async def set_model(self, conversation_id: str, channel: str | None) -> dict[str, Any] | None:
        """设置会话使用的模型通道（写入 ``meta.modelChannel``；None = 跟随全局默认）。

        模型选择是会话级耐久状态：composer 的模型下拉写这里，运行时会话据此构建
        专用模型客户端（只含被选通道，不跨模型 failover）。深合并 meta，不动其他键。
        """
        existing = await self.get(conversation_id)
        if existing is None:
            return None
        meta = dict(existing.get("meta") or {})
        if channel:
            meta["modelChannel"] = channel
        else:
            meta.pop("modelChannel", None)
        await self.db.execute(
            "UPDATE conversations SET meta=?, updated_at=? WHERE id=?",
            (_dumps(meta), time.time(), conversation_id),
        )
        return await self.get(conversation_id)

    async def touch(self, conversation_id: str, *, title: str | None = None) -> None:
        if title:
            await self.db.execute(
                "UPDATE conversations SET updated_at=?, title=? WHERE id=?",
                (time.time(), title, conversation_id),
            )
        else:
            await self.db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (time.time(), conversation_id)
            )

    async def delete(self, conversation_id: str) -> None:
        await self.db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
        await self.db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))


class MessageRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def append(
        self,
        conversation_id: str,
        message: ChatMessage,
        *,
        seq: int | None = None,
        delivery_status: str = "delivered",
    ) -> dict[str, Any]:
        if seq is None:
            row = await self.db.fetchone(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM messages WHERE conversation_id=?",
                (conversation_id,),
            )
            seq = int(row["s"]) + 1 if row else 1
        mid = new_id("msg_")
        payload = message.to_db()
        # 落库前清洗孤立代理字符：sqlite3 绑定 str 时同样要求可编码为 UTF-8，
        # 否则会抛 UnicodeEncodeError，导致工具结果/回复无法持久化。
        payload["content"] = clean_text(payload["content"] or "")
        if payload.get("reasoning"):
            payload["reasoning"] = clean_text(payload["reasoning"])
        if payload.get("toolCalls"):
            payload["toolCalls"] = clean_value(payload["toolCalls"])
        await self.db.execute(
            "INSERT INTO messages(id, conversation_id, seq, role, content, tool_calls, tool_call_id, "
            "name, reasoning, created_at, delivery_status) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mid,
                conversation_id,
                seq,
                payload["role"],
                payload["content"],
                _dumps(payload["toolCalls"]),
                payload["toolCallId"],
                payload["name"],
                payload["reasoning"],
                message.created_at,
                delivery_status,
            ),
        )
        return {
            "id": mid,
            "seq": seq,
            "conversationId": conversation_id,
            "deliveryStatus": delivery_status,
            **payload,
        }

    async def last_assistant_content(self, conversation_id: str, limit: int = 200) -> str:
        """取会话最近一条 assistant 消息的正文（会话升级播种用）；没有则返回空串。"""
        rows = await self.db.fetchall(
            "SELECT role, content FROM messages WHERE conversation_id=? AND role='assistant' "
            "ORDER BY seq DESC LIMIT ?",
            (conversation_id, max(1, int(limit))),
        )
        for row in rows:
            content = (row.get("content") or "").strip()
            if content:
                return content
        return ""

    async def to_chat_messages(
        self, conversation_id: str, limit: int = 200, *, exclude_queued: bool = False
    ) -> list[ChatMessage]:
        """读取最近 ``limit`` 条消息并还原为 :class:`ChatMessage`。

        - ``exclude_queued=True``（Steering 运行加载历史时）：跳过 ``delivery_status='queued'``
          的引导消息——它们尚未在安全点注入，不属于本轮模型上下文；
        - 相邻的 user 消息合并为一条（空行分隔）：运行中引导把一次安全点的积压合并成
          **一条** user 消息发给模型，但落库仍是逐条行（保留气泡粒度）；历史还原时按同样
          规则合并，保证重载后的上下文与运行时一致，且不产生连续多条 user 消息。
        """
        rows = await self.db.fetchall(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY seq DESC LIMIT ?",
            (conversation_id, limit),
        )
        messages: list[ChatMessage] = []
        for row in reversed(rows):
            data = _normalize_message(row)
            if exclude_queued and data.get("deliveryStatus") == "queued":
                continue
            if data["role"] == "system":
                messages.append(ChatMessage.system(data["content"]))
                continue
            if data["role"] == "user":
                text = data["content"]
                if messages and messages[-1].role == "user":
                    # 相邻 user 行合并：与安全点注入的合并格式（空行分隔）保持一致
                    messages[-1] = ChatMessage.user(messages[-1].content + "\n\n" + text)
                    continue
                messages.append(ChatMessage.user(text))
                continue
            if data["role"] == "tool":
                messages.append(
                    ChatMessage.tool(data.get("toolCallId") or "", data["content"], data.get("name"))
                )
                continue
            tool_calls = [
                ToolCall(
                    id=tc.get("id") or "",
                    name=tc.get("name") or "",
                    arguments=tc.get("arguments") or {},
                    raw_arguments=tc.get("raw_arguments") or "",
                )
                for tc in (data.get("toolCalls") or [])
            ]
            messages.append(ChatMessage.assistant(data["content"], tool_calls))
        return messages

    async def mark_delivered(self, message_ids: list[str]) -> None:
        """把引导消息标记为已投递（``queued → delivered``）。空列表直接返回。"""
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        await self.db.execute(
            f"UPDATE messages SET delivery_status='delivered' "
            f"WHERE id IN ({placeholders}) AND delivery_status='queued'",
            list(message_ids),
        )

    async def list_queued(self, conversation_id: str) -> list[dict[str, Any]]:
        """按入队顺序返回仍处于 queued 状态的引导消息（``/state`` 刷新恢复用）。"""
        rows = await self.db.fetchall(
            "SELECT id, content, created_at FROM messages "
            "WHERE conversation_id=? AND delivery_status='queued' ORDER BY seq ASC",
            (conversation_id,),
        )
        return [
            {"id": r["id"], "text": r["content"] or "", "enqueuedAt": r["created_at"] * 1000}
            for r in rows
        ]

    async def count(self, conversation_id: str) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS c FROM messages WHERE conversation_id=?", (conversation_id,)
        )
        return int(row["c"]) if row else 0

    async def list(self, conversation_id: str, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY seq ASC LIMIT ? OFFSET ?",
            (conversation_id, limit, offset),
        )
        return [_normalize_message(r) for r in rows]


class ExecutionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def save(self, record: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO tool_executions(id, conversation_id, challenge_id, run_id, agent_id, tool_name, "
            "arguments, command, status, exit_code, output, error, persisted_file, is_error, started_at, "
            "finished_at, duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, exit_code=excluded.exit_code, "
            "output=excluded.output, error=excluded.error, persisted_file=excluded.persisted_file, "
            "is_error=excluded.is_error, finished_at=excluded.finished_at, duration_ms=excluded.duration_ms",
            (
                record["id"],
                record.get("conversation_id"),
                record.get("challenge_id"),
                record.get("run_id"),
                record.get("agent_id", "ctf-agent"),
                record.get("tool_name", ""),
                _dumps(record.get("arguments", {})),
                record.get("command", ""),
                record.get("status", "running"),
                record.get("exit_code"),
                (record.get("output") or "")[:200_000],
                record.get("error"),
                record.get("persisted_file"),
                1 if record.get("is_error") else 0,
                record.get("started_at", time.time()),
                record.get("finished_at"),
                record.get("duration_ms"),
            ),
        )

    async def get(self, execution_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM tool_executions WHERE id=?", (execution_id,))
        return _normalize_execution(row) if row else None

    async def list(
        self, *, conversation_id: str | None = None, run_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.db.fetchall(
            f"SELECT * FROM tool_executions {where} ORDER BY started_at DESC LIMIT ?",
            (*params, limit),
        )
        return [_normalize_execution(r) for r in rows]


class FactRepo:
    """黑板记忆仓储：upsert/get/list/search。

    U18：所有方法以 ``scope`` 为唯一归属键（见 :mod:`ctfagent.scope`）：
    ``challenge:<id>`` 表示题目隔离单元，``conversation:<cid>`` 表示普通会话。
    同一题目的所有会话共享一个黑板，不同题目之间完全隔离。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(
        self,
        scope: str,
        key: str,
        *,
        summary: str = "",
        body: str = "",
        tags: list[str] | None = None,
        conversation_id: str = "",
        challenge_id: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        existing = await self.get(scope, key)
        # createdAt 是毫秒时间戳（对外契约），落库需要秒
        created = (existing["createdAt"] / 1000.0) if existing else now
        kind, ident = parse_scope(scope)
        audit_conversation = conversation_id or (ident if kind == "conversation" else "")
        audit_challenge = challenge_id if challenge_id is not None else (ident if kind == "challenge" else None)
        await self.db.execute(
            "INSERT INTO facts(scope, key, summary, body, tags, conversation_id, challenge_id, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope, key) DO UPDATE SET "
            "summary=excluded.summary, body=excluded.body, tags=excluded.tags, "
            "conversation_id=excluded.conversation_id, updated_at=excluded.updated_at",
            (
                scope,
                key,
                summary,
                body,
                ",".join(tags or []),
                audit_conversation,
                audit_challenge,
                created,
                now,
            ),
        )
        fact = await self.get(scope, key)
        assert fact is not None
        fact["created"] = existing is None
        return fact

    async def get(self, scope: str, key: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM facts WHERE scope=? AND key=?", (scope, key))
        return _normalize_fact(row) if row else None

    async def search(self, scope: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        like = f"%{query}%"
        rows = await self.db.fetchall(
            "SELECT * FROM facts WHERE scope=? AND "
            "(key LIKE ? OR summary LIKE ? OR body LIKE ? OR tags LIKE ?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (scope, like, like, like, like, limit),
        )
        return [_normalize_fact(r) for r in rows]

    async def index(self, scope: str, limit: int = 60) -> list[dict[str, str]]:
        """只返回 key+summary 索引，用于注入系统提示词。"""
        rows = await self.db.fetchall(
            "SELECT key, summary, updated_at FROM facts WHERE scope=? "
            "ORDER BY updated_at DESC LIMIT ?",
            (scope, limit),
        )
        return [{"key": r["key"], "summary": r["summary"] or ""} for r in rows]

    async def delete(self, scope: str, key: str) -> None:
        await self.db.execute("DELETE FROM facts WHERE scope=? AND key=?", (scope, key))

    async def list(self, scope: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM facts WHERE scope=? ORDER BY updated_at DESC LIMIT ?",
            (scope, limit),
        )
        return [_normalize_fact(r) for r in rows]

    async def count(self, scope: str) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM facts WHERE scope=?", (scope,))
        return int(row["n"]) if row else 0

    async def rescope(self, source: str, target: str) -> int:
        """把 ``source`` 作用域的事实并入 ``target``（会话升级为题目时用）。

        冲突（同 key）按 ``updated_at`` 较新者胜出；返回迁移的条目数。
        """
        rows = await self.db.fetchall("SELECT * FROM facts WHERE scope=?", (source,))
        moved = 0
        for row in rows:
            existing = await self.db.fetchone(
                "SELECT updated_at FROM facts WHERE scope=? AND key=?", (target, row["key"])
            )
            if existing is not None and float(existing["updated_at"]) > float(row["updated_at"]):
                continue
            await self.db.execute(
                "INSERT INTO facts(scope, key, summary, body, tags, conversation_id, challenge_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope, key) DO UPDATE SET "
                "summary=excluded.summary, body=excluded.body, tags=excluded.tags, "
                "conversation_id=excluded.conversation_id, challenge_id=excluded.challenge_id, "
                "updated_at=excluded.updated_at",
                (
                    target,
                    row["key"],
                    row["summary"],
                    row["body"],
                    row["tags"],
                    row["conversation_id"],
                    row["challenge_id"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
            moved += 1
        await self.db.execute("DELETE FROM facts WHERE scope=?", (source,))
        return moved


class ChallengeRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(self, data: dict[str, Any]) -> dict[str, Any]:
        cid = data.get("id") or new_id("chal_")
        now = time.time()
        existing = await self.get(cid)
        await self.db.execute(
            "INSERT INTO challenges(id, name, category, description, points, attachments, flag_pattern, "
            "target, created_at, updated_at, meta) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, category=excluded.category, "
            "description=excluded.description, points=excluded.points, attachments=excluded.attachments, "
            "flag_pattern=excluded.flag_pattern, target=excluded.target, updated_at=excluded.updated_at, "
            "meta=excluded.meta",
            (
                cid,
                data.get("name", ""),
                data.get("category", "misc"),
                data.get("description", ""),
                int(data.get("points", 0) or 0),
                _dumps(data.get("attachments", [])),
                data.get("flag_pattern", ""),
                data.get("target", ""),
                existing["createdAt"] if existing else now,
                now,
                _dumps(data.get("meta", {})),
            ),
        )
        row = await self.get(cid)
        assert row is not None
        return row

    async def get(self, challenge_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM challenges WHERE id=?", (challenge_id,))
        return _normalize_challenge(row) if row else None

    async def list(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM challenges ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [_normalize_challenge(r) for r in rows]

    async def set_project(self, challenge_id: str, project_id: str | None) -> dict[str, Any] | None:
        """设置题目归属项目（写入 ``meta.projectId``；空值表示取消归属）。

        题目表没有 ``project_id`` 列，归属信息统一放在 ``meta`` 里，看板与题目模块
        都按该字段识别归属——这样不必为一个可选字段做表结构迁移。仅改 meta，
        不会像 ``upsert`` 那样覆盖名称/描述/附件等字段。
        """
        existing = await self.get(challenge_id)
        if existing is None:
            return None
        meta = dict(existing.get("meta") or {})
        pid = (project_id or "").strip()
        if pid:
            meta["projectId"] = pid
        else:
            meta.pop("projectId", None)
        await self.db.execute(
            "UPDATE challenges SET meta=?, updated_at=? WHERE id=?",
            (_dumps(meta), time.time(), challenge_id),
        )
        return await self.get(challenge_id)

    async def list_with_status(self, limit: int = 100) -> list[dict[str, Any]]:
        """列出题目并附带解题状态（会话状态 + 已提交 flag 数）。

        状态取自 ``challenge_sessions``（挑战控制台的运行会话）与 ``flags``；
        flag 计数同时覆盖**直接挂在题目上**与**挂在题目绑定会话上**两种情况，
        一次查询取回，避免前端为每道题各请求一次 ``/state`` 快照。
        """
        rows = await self.db.fetchall(
            "SELECT c.*, COALESCE(cs.status, '') AS session_status, "
            "COALESCE(cs.verdict, '') AS session_verdict, "
            "COALESCE(cs.reason, '') AS session_reason, "
            "cs.started_at AS session_started_at, cs.ended_at AS session_ended_at, "
            "(SELECT COUNT(*) FROM flags f WHERE f.challenge_id = c.id "
            "   OR (f.challenge_id IS NULL AND f.conversation_id IS NOT NULL "
            "       AND f.conversation_id IN (SELECT cv.id FROM conversations cv "
            "                                 WHERE cv.challenge_id = c.id))) AS flag_count "
            "FROM challenges c "
            "LEFT JOIN challenge_sessions cs ON cs.challenge_id = c.id "
            "ORDER BY c.updated_at DESC LIMIT ?",
            (limit,),
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _normalize_challenge(row)
            verdict = row.get("session_verdict") or ""
            item["status"] = row.get("session_status") or ""
            item["verdict"] = verdict
            item["statusReason"] = row.get("session_reason") or ""
            item["startedAt"] = row.get("session_started_at")
            item["endedAt"] = row.get("session_ended_at")
            item["flagCount"] = int(row.get("flag_count") or 0)
            # 已解答：控制台判定成功，或该题目（含其绑定会话）下已有 flag 记录
            item["solved"] = verdict == "TERMINATE_SUCCESS" or item["flagCount"] > 0
            items.append(item)
        return items


class FlagRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self,
        *,
        value: str,
        conversation_id: str | None = None,
        challenge_id: str | None = None,
        fmt: str = "",
        validated: bool = False,
        source: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fid = new_id("flag_")
        await self.db.execute(
            "INSERT INTO flags(id, conversation_id, challenge_id, value, format, validated, source, "
            "submitted_at, meta) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                fid,
                conversation_id,
                challenge_id,
                value,
                fmt,
                1 if validated else 0,
                source,
                time.time(),
                _dumps(meta or {}),
            ),
        )
        row = await self.db.fetchone("SELECT * FROM flags WHERE id=?", (fid,))
        assert row is not None
        return _normalize_flag(row)

    async def list_by_challenge(self, challenge_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """按题目取 flag 记录（v2 挑战级终止判定用；不影响 v1 的按会话查询）。"""
        rows = await self.db.fetchall(
            "SELECT * FROM flags WHERE challenge_id=? ORDER BY submitted_at DESC LIMIT ?",
            (challenge_id, limit),
        )
        return [_normalize_flag(r) for r in rows]

    async def list(self, *, conversation_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if conversation_id:
            rows = await self.db.fetchall(
                "SELECT * FROM flags WHERE conversation_id=? ORDER BY submitted_at DESC LIMIT ?",
                (conversation_id, limit),
            )
        else:
            rows = await self.db.fetchall("SELECT * FROM flags ORDER BY submitted_at DESC LIMIT ?", (limit,))
        return [_normalize_flag(r) for r in rows]

    async def exists(self, conversation_id: str, value: str) -> bool:
        row = await self.db.fetchone(
            "SELECT id FROM flags WHERE conversation_id=? AND value=?", (conversation_id, value)
        )
        return row is not None


class TraceRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def save(
        self,
        *,
        run_id: str,
        conversation_id: str | None,
        agent_id: str,
        trace_path: str,
        summary: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO agent_traces(id, run_id, conversation_id, agent_id, trace_path, summary, "
            "created_at, meta) VALUES(?,?,?,?,?,?,?,?)",
            (
                new_id("trace_"),
                run_id,
                conversation_id,
                agent_id,
                trace_path,
                summary,
                time.time(),
                _dumps(meta or {}),
            ),
        )

    async def list(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if run_id:
            rows = await self.db.fetchall(
                "SELECT * FROM agent_traces WHERE run_id=? ORDER BY created_at DESC LIMIT ?",
                (run_id, limit),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM agent_traces ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in rows]


class RunRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        *,
        run_id: str | None = None,
        conversation_id: str,
        agent_id: str = "ctf-agent",
        orchestration: str = "single",
    ) -> dict[str, Any]:
        rid = run_id or new_id("run_")
        await self.db.execute(
            "INSERT INTO runs(id, conversation_id, agent_id, orchestration, status, started_at) "
            "VALUES(?,?,?,?,?,?)",
            (rid, conversation_id, agent_id, orchestration, "running", time.time()),
        )
        row = await self.get(rid)
        assert row is not None
        return row

    async def finish(
        self,
        run_id: str,
        *,
        status: str,
        iterations: int = 0,
        usage: Usage | None = None,
        error: str | None = None,
    ) -> None:
        await self.db.execute(
            "UPDATE runs SET status=?, iterations=?, prompt_tokens=?, completion_tokens=?, model_calls=?, "
            "error=?, finished_at=? WHERE id=?",
            (
                status,
                iterations,
                usage.prompt_tokens if usage else 0,
                usage.completion_tokens if usage else 0,
                usage.model_calls if usage else 0,
                error,
                time.time(),
                run_id,
            ),
        )

    async def get(self, run_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        return dict(row) if row else None

    async def list(self, conversation_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if conversation_id:
            rows = await self.db.fetchall(
                "SELECT * FROM runs WHERE conversation_id=? ORDER BY started_at DESC LIMIT ?",
                (conversation_id, limit),
            )
        else:
            rows = await self.db.fetchall("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


class HitlDecisionRepo:
    """审查裁决留痕（U6a，迁移 005 新增表）。

    reviewer 四种取值：``human``（人工）/ ``audit_agent``（Agent 快速审计）/
    ``policy``（reviewer=none 的策略放行）/ ``whitelist``（白名单免审）。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def save(
        self,
        *,
        conversation_id: str,
        tool_name: str,
        reviewer: str,
        decision: str,
        tool_call_id: str = "",
        run_id: str = "",
        arguments: dict[str, Any] | None = None,
        mode: str = "",
        comment: str = "",
        edited_arguments: dict[str, Any] | None = None,
        elapsed_ms: int | None = None,
    ) -> dict[str, Any]:
        record_id = new_id("hitld_")
        created_at = _now_iso()
        await self.db.execute(
            "INSERT INTO hitl_decisions(id, conversation_id, run_id, tool_call_id, tool_name, "
            "arguments_json, reviewer, mode, decision, comment, edited_arguments_json, "
            "elapsed_ms, execution_id, execution_result_digest, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record_id,
                conversation_id,
                run_id,
                tool_call_id,
                tool_name,
                _dumps(arguments or {}),
                reviewer,
                mode,
                decision,
                comment or "",
                _dumps(edited_arguments) if edited_arguments is not None else None,
                elapsed_ms,
                None,
                None,
                created_at,
            ),
        )
        record = await self.get(record_id)
        assert record is not None
        return record

    async def get(self, record_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM hitl_decisions WHERE id=?", (record_id,))
        return _normalize_hitl_decision(row) if row else None

    async def attach_execution(self, record_id: str, execution_id: str, result_digest: str) -> None:
        """闭环回填（U6b 蓝本第 7 条）：放行的调用在执行完成后回写结果摘要（截断 1KB）。"""
        await self.db.execute(
            "UPDATE hitl_decisions SET execution_id=?, execution_result_digest=? WHERE id=?",
            (execution_id, (result_digest or "")[:1024], record_id),
        )

    async def list(
        self,
        *,
        conversation_id: str | None = None,
        reviewer: str | None = None,
        decision: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        if reviewer:
            clauses.append("reviewer=?")
            params.append(reviewer)
        if decision:
            clauses.append("decision=?")
            params.append(decision)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        rows = await self.db.fetchall(
            f"SELECT * FROM hitl_decisions {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            tuple(params),
        )
        return [_normalize_hitl_decision(r) for r in rows]


def _normalize_hitl_decision(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversationId": row.get("conversation_id"),
        "runId": row.get("run_id"),
        "toolCallId": row.get("tool_call_id"),
        "toolName": row.get("tool_name"),
        "arguments": _loads(row.get("arguments_json"), {}),
        "reviewer": row.get("reviewer"),
        "mode": row.get("mode"),
        "decision": row.get("decision"),
        "comment": row.get("comment") or "",
        "editedArguments": _loads(row.get("edited_arguments_json"), None),
        "elapsedMs": row.get("elapsed_ms"),
        "executionId": row.get("execution_id"),
        "executionResultDigest": row.get("execution_result_digest"),
        "createdAt": row.get("created_at"),
    }


class Repos:
    """仓储门面。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.conversations = ConversationRepo(db)
        self.messages = MessageRepo(db)
        self.executions = ExecutionRepo(db)
        self.facts = FactRepo(db)
        self.challenges = ChallengeRepo(db)
        self.flags = FlagRepo(db)
        self.traces = TraceRepo(db)
        self.runs = RunRepo(db)
        # U2a：对话附件（迁移 003 新增表）
        self.attachments = AttachmentRepo(db)
        # U4：项目（比赛）维度（迁移 004 新增表 + conversations.project_id）
        self.projects = ProjectRepo(db)
        # v2：挑战级账本（blackboard v2，迁移 002 新增表）
        self.ideas = IdeaRepo(db)
        self.memories = MemoryRepo(db)
        self.solver_runs = SolverRunRepo(db)
        self.challenge_sessions = ChallengeSessionRepo(db)
        # U6a：多模式审查裁决留痕（迁移 005 新增表）
        self.hitl_decisions = HitlDecisionRepo(db)
        # v10：竞赛自动化模式（迁移 010 新增表）—— 竞赛级全局黑板/能力/题目池/提交/策略状态
        from ..competition.repo import CompetitionRepo

        self.competitions = CompetitionRepo(db)
        # v8：作战图（迁移 008 新增表）—— 题目 × 战法 的显式编排


class IdeaRepo:
    """挑战级 Idea（方向假设）仓储。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        *,
        challenge_id: str,
        text: str,
        idea_id: str | None = None,
        created_by: str = "agent",
    ) -> dict[str, Any]:
        now = _now_iso()
        new = idea_id or new_id("idea_")
        await self.db.execute(
            "INSERT INTO ideas(id, challenge_id, text, status, created_by, claimed_by, "
            "created_at, updated_at, last_transition_at, meta) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (new, challenge_id, text, "pending", created_by or "agent", None, now, now, None, "{}"),
        )
        created = await self.get(new)
        assert created is not None
        return created

    async def get(self, idea_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM ideas WHERE id=?", (idea_id,))
        return _normalize_idea(row) if row else None

    async def find_by_text(self, challenge_id: str, text: str) -> dict[str, Any] | None:
        """同文本去重（避免模型换个说法反复声明同一条方向）。"""
        row = await self.db.fetchone(
            "SELECT * FROM ideas WHERE challenge_id=? AND text=? ORDER BY created_at LIMIT 1",
            (challenge_id, text),
        )
        return _normalize_idea(row) if row else None

    async def update_status(
        self,
        idea_id: str,
        status: str,
        *,
        claimed_by: str | None = None,
        touch_transition: bool = True,
        claim_expires_at: str | None = None,
    ) -> dict[str, Any] | None:
        now = _now_iso()
        if claimed_by is not None:
            await self.db.execute(
                "UPDATE ideas SET status=?, claimed_by=COALESCE(claimed_by, ?), updated_at=?, "
                "last_transition_at=?, claim_expires_at=COALESCE(?, claim_expires_at) WHERE id=?",
                (status, claimed_by, now, now if touch_transition else None,
                 claim_expires_at, idea_id),
            )
        else:
            await self.db.execute(
                "UPDATE ideas SET status=?, updated_at=?, last_transition_at=? WHERE id=?",
                (status, now, now if touch_transition else None, idea_id),
            )
        return await self.get(idea_id)

    async def list_many(
        self,
        challenge_ids: list[str],
        *,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """跨多个挑战取 Idea（U4：项目维度黑板查询，纯只读扩展）。"""
        if not challenge_ids:
            return []
        placeholders = ",".join("?" for _ in challenge_ids)
        params: list[Any] = list(challenge_ids)
        clause = ""
        if statuses:
            clause = f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        rows = await self.db.fetchall(
            f"SELECT * FROM ideas WHERE challenge_id IN ({placeholders}){clause} "
            "ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (*params, limit),
        )
        return [_normalize_idea(r) for r in rows]

    async def list(
        self,
        challenge_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [challenge_id]
        clause = ""
        if statuses:
            clause = f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        rows = await self.db.fetchall(
            f"SELECT * FROM ideas WHERE challenge_id=?{clause} ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (*params, limit),
        )
        return [_normalize_idea(r) for r in rows]

    async def count_by_status(self, challenge_id: str) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM ideas WHERE challenge_id=? GROUP BY status", (challenge_id,)
        )
        return {str(r["status"]): int(r["n"]) for r in rows}

    async def release_claim(self, idea_id: str) -> None:
        """M1 租约回收：把认领过期的 testing Idea 退回 pending。

        这是**技术性回退**，不是业务状态转换，因此直接写库、绕过状态机
        （``testing → pending`` 不在 ALLOWED_TRANSITIONS 里）。
        """
        now = _now_iso()
        await self.db.execute(
            "UPDATE ideas SET status='pending', claimed_by=NULL, claim_expires_at=NULL, "
            "updated_at=?, last_transition_at=? WHERE id=? AND status='testing'",
            (now, now, idea_id),
        )

    # ---- M2 因果边 ----
    async def add_sources(self, idea_id: str, challenge_id: str, memory_ids: list[str]) -> None:
        """记录「该方向从哪些事实推来」（幂等：重复写入忽略）。"""
        if not memory_ids:
            return
        now = _now_iso()
        for memory_id in memory_ids:
            await self.db.execute(
                "INSERT OR IGNORE INTO idea_sources(idea_id, memory_id, challenge_id, created_at) "
                "VALUES(?,?,?,?)",
                (idea_id, memory_id, challenge_id, now),
            )

    async def sources_of(self, challenge_id: str, idea_ids: list[str]) -> dict[str, list[str]]:
        """批量取多条 Idea 的来源事实（渲染用，避免 N+1 查询）。"""
        if not idea_ids:
            return {}
        placeholders = ",".join("?" for _ in idea_ids)
        rows = await self.db.fetchall(
            f"SELECT idea_id, memory_id FROM idea_sources "
            f"WHERE challenge_id=? AND idea_id IN ({placeholders}) ORDER BY rowid",
            (challenge_id, *idea_ids),
        )
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(str(row["idea_id"]), []).append(str(row["memory_id"]))
        return out

    async def ideas_from(self, memory_id: str) -> list[str]:
        """反向：哪些 Idea 引用了这条事实。"""
        rows = await self.db.fetchall(
            "SELECT idea_id FROM idea_sources WHERE memory_id=? ORDER BY rowid", (memory_id,)
        )
        return [str(row["idea_id"]) for row in rows]

    async def delete(self, idea_id: str) -> None:
        await self.db.execute("DELETE FROM idea_sources WHERE idea_id=?", (idea_id,))
        await self.db.execute("DELETE FROM ideas WHERE id=?", (idea_id,))


class MemoryRepo:
    """挑战级 Memory（追加式事实记录）仓储。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def append(
        self,
        *,
        challenge_id: str,
        kind: str,
        summary: str,
        body: str = "",
        source: str = "agent",
        solver_run_id: str | None = None,
        from_idea_id: str | None = None,
    ) -> dict[str, Any]:
        new = new_id("mem_")
        await self.db.execute(
            "INSERT INTO memories(id, challenge_id, kind, summary, body, source, solver_run_id, "
            "created_at, from_idea_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (new, challenge_id, kind, summary, body, source, solver_run_id, _now_iso(), from_idea_id),
        )
        created = await self.get(new)
        assert created is not None
        return created

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM memories WHERE id=?", (memory_id,))
        return _normalize_memory(row) if row else None

    async def list_since(
        self,
        challenge_id: str,
        *,
        since_iso: str,
        exclude_solver_run_id: str | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """M3：取 ``since_iso`` 之后新增的 Memory（默认排除自己写的），按时间倒序。"""
        sql = "SELECT * FROM memories WHERE challenge_id=? AND created_at > ?"
        params: list[Any] = [challenge_id, since_iso]
        if exclude_solver_run_id:
            sql += " AND (solver_run_id IS NULL OR solver_run_id != ?)"
            params.append(exclude_solver_run_id)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = await self.db.fetchall(sql, tuple(params))
        return [_normalize_memory(r) for r in rows]

    async def recent_for_solver(self, solver_run_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM memories WHERE solver_run_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (solver_run_id, limit),
        )
        return [_normalize_memory(r) for r in rows]

    async def list_many(
        self,
        challenge_ids: list[str],
        *,
        kinds: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """跨多个挑战取 Memory（U4：项目维度黑板查询，纯只读扩展）。"""
        if not challenge_ids:
            return []
        placeholders = ",".join("?" for _ in challenge_ids)
        params: list[Any] = list(challenge_ids)
        clause = ""
        if kinds:
            clause = f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        rows = await self.db.fetchall(
            f"SELECT * FROM memories WHERE challenge_id IN ({placeholders}){clause} "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*params, limit),
        )
        return [_normalize_memory(r) for r in rows]

    async def list(
        self,
        challenge_id: str,
        *,
        kinds: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [challenge_id]
        clause = ""
        if kinds:
            clause = f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        rows = await self.db.fetchall(
            f"SELECT * FROM memories WHERE challenge_id=?{clause} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*params, limit),
        )
        return [_normalize_memory(r) for r in rows]

    async def count_by_kind(self, challenge_id: str) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT kind, COUNT(*) AS n FROM memories WHERE challenge_id=? GROUP BY kind", (challenge_id,)
        )
        return {str(r["kind"]): int(r["n"]) for r in rows}

    async def latest_created_at(self, challenge_id: str) -> str | None:
        """最近一条 Memory 的时间（软超时"无新 Memory"判定的依据）。"""
        row = await self.db.fetchone(
            "SELECT created_at FROM memories WHERE challenge_id=? ORDER BY created_at DESC LIMIT 1",
            (challenge_id,),
        )
        return str(row["created_at"]) if row else None

    async def count_for_solver(self, solver_run_id: str) -> int:
        """该 Solver 已沉淀的 Memory 条数（"失败即产出"的验收依据）。"""
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM memories WHERE solver_run_id=?", (solver_run_id,)
        )
        return int(row["n"]) if row else 0

    async def latest_for_solver(self, solver_run_id: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT created_at FROM memories WHERE solver_run_id=? ORDER BY created_at DESC LIMIT 1",
            (solver_run_id,),
        )
        return str(row["created_at"]) if row else None


class SolverRunRepo:
    """Solver 生命周期仓储（running → concluding → done/timeout/failed/cancelled）。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        *,
        challenge_id: str,
        task_type: str,
        idea_id: str | None = None,
        conversation_id: str | None = None,
        solver_run_id: str | None = None,
    ) -> dict[str, Any]:
        new = solver_run_id or new_id("solver_")
        await self.db.execute(
            "INSERT INTO solver_runs(id, challenge_id, conversation_id, task_type, idea_id, status, "
            "started_at, ended_at, concluded, outcome, error, meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new,
                challenge_id,
                conversation_id,
                task_type,
                idea_id,
                "running",
                _now_iso(),
                None,
                0,
                None,
                None,
                "{}",
            ),
        )
        created = await self.get(new)
        assert created is not None
        return created

    async def get(self, solver_run_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM solver_runs WHERE id=?", (solver_run_id,))
        return _normalize_solver_run(row) if row else None

    async def finish(
        self,
        solver_run_id: str,
        *,
        status: str,
        outcome: str | None = None,
        concluded: bool | None = None,
        error: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any] | None:
        sets = ["status=?", "outcome=?", "ended_at=?"]
        params: list[Any] = [status, outcome, _now_iso()]
        if concluded is not None:
            sets.append("concluded=?")
            params.append(1 if concluded else 0)
        if error is not None:
            sets.append("error=?")
            params.append(error)
        if conversation_id is not None:
            sets.append("conversation_id=?")
            params.append(conversation_id)
        params.append(solver_run_id)
        await self.db.execute(f"UPDATE solver_runs SET {', '.join(sets)} WHERE id=?", tuple(params))
        return await self.get(solver_run_id)

    async def mark_concluding(self, solver_run_id: str) -> None:
        await self.db.execute("UPDATE solver_runs SET status='concluding' WHERE id=?", (solver_run_id,))

    # ---- M1 租约 ----
    async def renew_lease(self, solver_run_id: str, *, ttl_seconds: int) -> None:
        """续约：写 heartbeat_at（过期判定交给 list_expired_leases，惰性清理）。"""
        await self.db.execute(
            "UPDATE solver_runs SET heartbeat_at=?, lease_ttl_seconds=? WHERE id=?",
            (_now_iso(), max(0, int(ttl_seconds)), solver_run_id),
        )

    async def list_expired_leases(self, challenge_id: str | None = None) -> list[dict[str, Any]]:
        """列出心跳已过期的 running/concluding 行（只认有心跳的；无心跳的走旧启发式）。"""
        sql = (
            "SELECT * FROM solver_runs WHERE status IN ('running','concluding') "
            "AND heartbeat_at IS NOT NULL AND heartbeat_at != ''"
        )
        params: tuple[Any, ...] = ()
        if challenge_id:
            sql += " AND challenge_id=?"
            params = (challenge_id,)
        rows = await self.db.fetchall(sql, params)
        expired: list[dict[str, Any]] = []
        for raw in rows:
            row = _normalize_solver_run(raw)
            ttl = int(row.get("leaseTtlSeconds") or 0)
            if ttl <= 0:
                continue
            if _seconds_since(row.get("heartbeatAt")) > ttl:
                expired.append(row)
        return expired

    async def list(
        self,
        challenge_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [challenge_id]
        clause = ""
        if statuses:
            clause = f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        rows = await self.db.fetchall(
            f"SELECT * FROM solver_runs WHERE challenge_id=?{clause} ORDER BY started_at DESC LIMIT ?",
            (*params, limit),
        )
        return [_normalize_solver_run(r) for r in rows]

    async def counts(self, challenge_id: str) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM solver_runs WHERE challenge_id=? GROUP BY status",
            (challenge_id,),
        )
        return {str(r["status"]): int(r["n"]) for r in rows}

    async def count_by_task_type(self, challenge_id: str, *, finished_only: bool = True) -> dict[str, int]:
        """按任务类型统计已结束（或全部）的 Solver 次数（Dispatcher 判断"是否做过 bootstrap"）。"""
        clause = " AND ended_at IS NOT NULL" if finished_only else ""
        rows = await self.db.fetchall(
            f"SELECT task_type, COUNT(*) AS n FROM solver_runs WHERE challenge_id=?{clause} "
            "GROUP BY task_type",
            (challenge_id,),
        )
        return {str(r["task_type"]): int(r["n"]) for r in rows}


class ChallengeSessionRepo:
    """挑战会话元状态仓储（v2 新表，v1 challenges 表零改动）。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(
        self,
        challenge_id: str,
        *,
        status: str | None = None,
        time_budget_minutes: int | None = None,
        verdict: str | None = None,
        reason: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        spawned_solvers: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = await self.get(challenge_id)
        if existing is None:
            await self.db.execute(
                "INSERT INTO challenge_sessions(challenge_id, status, time_budget_minutes, started_at, "
                "ended_at, verdict, reason, spawned_solvers, meta) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    challenge_id,
                    status or "idle",
                    int(time_budget_minutes or 120),
                    started_at,
                    ended_at,
                    verdict,
                    reason,
                    int(spawned_solvers or 0),
                    _dumps(meta or {}),
                ),
            )
        else:
            sets: list[str] = []
            params: list[Any] = []
            for column, value in (
                ("status", status),
                ("time_budget_minutes", time_budget_minutes),
                ("verdict", verdict),
                ("reason", reason),
                ("started_at", started_at),
                ("ended_at", ended_at),
                ("spawned_solvers", spawned_solvers),
            ):
                if value is not None:
                    sets.append(f"{column}=?")
                    params.append(value)
            if meta is not None:
                sets.append("meta=?")
                params.append(_dumps({**existing.get("meta", {}), **meta}))
            if sets:
                params.append(challenge_id)
                await self.db.execute(
                    f"UPDATE challenge_sessions SET {', '.join(sets)} WHERE challenge_id=?", tuple(params)
                )
        result = await self.get(challenge_id)
        assert result is not None
        return result

    async def get(self, challenge_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM challenge_sessions WHERE challenge_id=?", (challenge_id,))
        return _normalize_challenge_session(row) if row else None

    async def list(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM challenge_sessions ORDER BY COALESCE(started_at, '') DESC LIMIT ?", (limit,)
        )
        return [_normalize_challenge_session(r) for r in rows]


# ---------------------------------------------------------------------------
# 行 → 对外 JSON 的规范化
# ---------------------------------------------------------------------------
def _normalize_conversation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "challengeId": row.get("challenge_id"),
        "agentId": row.get("agent_id"),
        "projectId": row.get("project_id"),
        "hitlReviewer": row.get("hitl_reviewer"),
        "createdAt": row["created_at"] * 1000,
        "updatedAt": row["updated_at"] * 1000,
        "messageCount": row.get("message_count", 0),
        "meta": _loads(row.get("meta"), {}),
    }


def _normalize_message(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversationId": row["conversation_id"],
        "seq": row["seq"],
        "role": row["role"],
        "content": row["content"] or "",
        "toolCalls": _loads(row.get("tool_calls"), []),
        "toolCallId": row.get("tool_call_id"),
        "name": row.get("name"),
        "reasoning": row.get("reasoning"),
        "createdAt": row["created_at"] * 1000,
        "deliveryStatus": row.get("delivery_status") or "delivered",
    }


def _normalize_execution(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversationId": row.get("conversation_id"),
        "challengeId": row.get("challenge_id"),
        "runId": row.get("run_id"),
        "agentId": row.get("agent_id"),
        "toolName": row.get("tool_name"),
        "arguments": _loads(row.get("arguments"), {}),
        "command": row.get("command"),
        "status": row.get("status"),
        "exitCode": row.get("exit_code"),
        "output": row.get("output"),
        "error": row.get("error"),
        "persistedFile": row.get("persisted_file"),
        "isError": bool(row.get("is_error")),
        "startedAt": (row.get("started_at") or 0) * 1000,
        "finishedAt": (row["finished_at"] * 1000) if row.get("finished_at") else None,
        "durationMs": row.get("duration_ms"),
    }


def _normalize_fact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": row.get("scope") or "",
        "challengeId": row.get("challenge_id"),
        "conversationId": row.get("conversation_id") or "",
        "key": row["key"],
        "summary": row["summary"] or "",
        "body": row["body"] or "",
        "tags": [t for t in (row.get("tags") or "").split(",") if t],
        "createdAt": row["created_at"] * 1000,
        "updatedAt": row["updated_at"] * 1000,
    }


def _normalize_nodes(nodes: Any) -> str:
    """校验并序列化作战图节点（只保留 challenge / tactic 两类，同源节点去重）。"""
    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in nodes if isinstance(nodes, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        ref = str(item.get("ref") or "").strip()
        if kind not in {"challenge", "tactic"} or not ref:
            continue
        key = (kind, ref)
        if key in seen:
            continue
        seen.add(key)
        node_id = str(item.get("id") or "").strip() or f"{kind}:{ref}"
        cleaned.append({"id": node_id, "kind": kind, "ref": ref})
    return _dumps(cleaned)


def _normalize_edges(edges: Any) -> str:
    """校验并序列化连线：challenge → tactic；去重且保持顺序（顺序即执行顺序）。"""
    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in edges if isinstance(edges, list) else []:
        if not isinstance(item, dict):
            continue
        challenge = str(item.get("challenge") or "").strip()
        tactic = str(item.get("tactic") or "").strip() or "default"
        if not challenge:
            continue
        key = (challenge, tactic)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({"challenge": challenge, "tactic": tactic})
    return _dumps(cleaned)


def _normalize_challenge(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "description": row["description"],
        "points": row["points"],
        "attachments": _loads(row.get("attachments"), []),
        "flagPattern": row.get("flag_pattern") or "",
        "target": row.get("target") or "",
        "createdAt": row["created_at"] * 1000,
        "updatedAt": row["updated_at"] * 1000,
        "meta": _loads(row.get("meta"), {}),
    }


def _normalize_flag(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversationId": row.get("conversation_id"),
        "challengeId": row.get("challenge_id"),
        "value": row["value"],
        "format": row.get("format") or "",
        "validated": bool(row.get("validated")),
        "source": row.get("source") or "",
        "submittedAt": row["submitted_at"] * 1000,
        "meta": _loads(row.get("meta"), {}),
    }


def _normalize_idea(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "challengeId": row["challenge_id"],
        "text": row["text"],
        "status": row["status"],
        "createdBy": row.get("created_by"),
        "claimedBy": row.get("claimed_by"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
        "lastTransitionAt": row.get("last_transition_at"),
        "meta": _loads(row.get("meta"), {}),
        # M1：认领租约到期时间（空 = 无租约，退回旧启发式）
        "claimExpiresAt": row.get("claim_expires_at"),
    }


def _normalize_memory(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "challengeId": row["challenge_id"],
        "kind": row["kind"],
        "summary": row["summary"],
        "body": row.get("body") or "",
        "source": row.get("source") or "agent",
        "solverRunId": row.get("solver_run_id"),
        "createdAt": row.get("created_at"),
        # M2：产出该事实的 Idea（因果边的反向指针）
        "fromIdeaId": row.get("from_idea_id"),
    }


def _normalize_solver_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "challengeId": row["challenge_id"],
        "conversationId": row.get("conversation_id"),
        "taskType": row["task_type"],
        "ideaId": row.get("idea_id"),
        "status": row["status"],
        "startedAt": row.get("started_at"),
        "endedAt": row.get("ended_at"),
        "concluded": bool(row.get("concluded")),
        "outcome": row.get("outcome"),
        "error": row.get("error"),
        "meta": _loads(row.get("meta"), {}),
        # M1：租约字段（heartbeatAt 为最后一次续约时间，leaseTtlSeconds 为 TTL）
        "heartbeatAt": row.get("heartbeat_at"),
        "leaseTtlSeconds": int(row.get("lease_ttl_seconds") or 0),
    }


def _normalize_challenge_session(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "challengeId": row["challenge_id"],
        "status": row["status"],
        "timeBudgetMinutes": int(row.get("time_budget_minutes") or 0),
        "startedAt": row.get("started_at"),
        "endedAt": row.get("ended_at"),
        "verdict": row.get("verdict"),
        "reason": row.get("reason"),
        "spawnedSolvers": int(row.get("spawned_solvers") or 0),
        "meta": _loads(row.get("meta"), {}),
    }


class AttachmentRepo:
    """对话附件元数据仓储（U2a；附件本体在 workspace 磁盘上）。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        *,
        conversation_id: str,
        filename: str,
        stored_path: str,
        size: int,
        mime: str = "",
        sha256: str = "",
        kind: str = "",
        message_id: str | None = None,
        attachment_id: str | None = None,
    ) -> dict[str, Any]:
        new = attachment_id or new_id("att_")
        await self.db.execute(
            "INSERT INTO attachments(id, conversation_id, message_id, filename, stored_path, size, "
            "mime, sha256, kind, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                new,
                conversation_id,
                message_id,
                filename,
                stored_path,
                int(size),
                mime or "",
                sha256 or "",
                kind or "",
                _now_iso(),
            ),
        )
        row = await self.get(new)
        assert row is not None
        return row

    async def get(self, attachment_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM attachments WHERE id=?", (attachment_id,))
        return _normalize_attachment(row) if row else None

    async def list_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """按传入顺序返回（清单块需要稳定顺序）。"""
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = await self.db.fetchall(f"SELECT * FROM attachments WHERE id IN ({placeholders})", tuple(ids))
        by_id = {row["id"]: _normalize_attachment(row) for row in rows}
        return [by_id[i] for i in ids if i in by_id]

    async def count(self, conversation_id: str) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM attachments WHERE conversation_id=?", (conversation_id,)
        )
        return int((row or {}).get("n") or 0)

    async def link(self, message_id: str, ids: list[str]) -> int:
        """把附件关联到某条消息（U2a：用户消息落库后回填）。"""
        if not ids or not message_id:
            return 0
        placeholders = ",".join("?" for _ in ids)
        await self.db.execute(
            f"UPDATE attachments SET message_id=? WHERE id IN ({placeholders})",
            (message_id, *ids),
        )
        return len(ids)

    async def delete(self, attachment_id: str) -> dict[str, Any] | None:
        row = await self.get(attachment_id)
        if row is None:
            return None
        await self.db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
        return row

    async def delete_by_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = await self.list(conversation_id)
        await self.db.execute("DELETE FROM attachments WHERE conversation_id=?", (conversation_id,))
        return rows

    async def list(self, conversation_id: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM attachments WHERE conversation_id=? ORDER BY created_at ASC, id ASC LIMIT ?",
            (conversation_id, int(limit)),
        )
        return [_normalize_attachment(r) for r in rows]


def _normalize_attachment(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "conversationId": row.get("conversation_id"),
        "messageId": row.get("message_id"),
        "filename": row.get("filename"),
        "storedPath": row.get("stored_path"),
        "size": int(row.get("size") or 0),
        "mime": row.get("mime") or "",
        "sha256": row.get("sha256") or "",
        "kind": row.get("kind") or "",
        "createdAt": row.get("created_at"),
    }


class ProjectRepo:
    """项目（比赛）维度仓储（U4；迁移 004 新增 ``projects`` 表 + ``conversations.project_id``）。

    一个会话通过单列 ``project_id`` 归属项目，因此天然满足"0 或 1 个项目"的约束；
    本仓储只做读写，不引入任何对既有会话/消息路径的行为改变。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        *,
        name: str,
        description: str = "",
        status: str = "active",
        meta: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        pid = project_id or new_id("proj_")
        now = _now_iso()
        await self.db.execute(
            "INSERT INTO projects(id, name, description, status, created_at, updated_at, meta) "
            "VALUES(?,?,?,?,?,?,?)",
            (pid, name, description or "", status or "active", now, now, _dumps(meta or {})),
        )
        created = await self.get(pid)
        assert created is not None
        return created

    async def get(self, project_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT p.*, (SELECT COUNT(*) FROM conversations c WHERE c.project_id=p.id) "
            "AS conversation_count FROM projects p WHERE p.id=?",
            (project_id,),
        )
        return _normalize_project(row) if row else None

    async def get_by_name(self, name: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT p.*, (SELECT COUNT(*) FROM conversations c WHERE c.project_id=p.id) "
            "AS conversation_count FROM projects p WHERE p.name=? ORDER BY p.created_at LIMIT 1",
            (name,),
        )
        return _normalize_project(row) if row else None

    async def update(self, project_id: str, **fields: Any) -> dict[str, Any] | None:
        """更新项目字段（``name`` / ``description`` / ``status`` / ``meta``）；不存在返回 None。"""
        existing = await self.get(project_id)
        if existing is None:
            return None
        sets: list[str] = []
        params: list[Any] = []
        if fields.get("name") is not None:
            sets.append("name=?")
            params.append(str(fields["name"]))
        if fields.get("description") is not None:
            sets.append("description=?")
            params.append(str(fields["description"]))
        if fields.get("status") is not None:
            sets.append("status=?")
            params.append(str(fields["status"]))
        if fields.get("meta") is not None:
            sets.append("meta=?")
            params.append(_dumps(fields["meta"]))
        sets.append("updated_at=?")
        params.append(_now_iso())
        params.append(project_id)
        await self.db.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", tuple(params))
        return await self.get(project_id)

    async def delete(self, project_id: str) -> None:
        await self.db.execute("DELETE FROM projects WHERE id=?", (project_id,))

    async def challenge_ids(self, project_id: str) -> list[str]:
        """项目内全部会话引用到的 challenge_id（去重，供黑板项目过滤）。"""
        rows = await self.db.fetchall(
            "SELECT DISTINCT challenge_id FROM conversations WHERE project_id=? "
            "AND challenge_id IS NOT NULL AND challenge_id != '' ORDER BY challenge_id",
            (project_id,),
        )
        return [str(r["challenge_id"]) for r in rows]

    async def conversation_ids(self, project_id: str) -> list[str]:
        """项目内全部会话 id（按更新时间倒序，供项目删除的 delete 模式逐条级联）。"""
        rows = await self.db.fetchall(
            "SELECT id FROM conversations WHERE project_id=? ORDER BY updated_at DESC",
            (project_id,),
        )
        return [str(r["id"]) for r in rows]

    async def summary(self, project_id: str) -> dict[str, Any] | None:
        """项目级黑板聚合（跨该项目全部会话，只读真实 SQL）；项目不存在返回 None。"""
        project = await self.get(project_id)
        if project is None:
            return None
        conversations = await self.db.fetchall(
            "SELECT c.id, c.title, c.updated_at, c.challenge_id, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count, "
            "(SELECT COUNT(*) FROM flags f WHERE f.conversation_id=c.id) AS flag_count "
            "FROM conversations c WHERE c.project_id=? ORDER BY c.updated_at DESC",
            (project_id,),
        )
        flags = await self.db.fetchall(
            "SELECT f.value, f.conversation_id, f.submitted_at FROM flags f "
            "JOIN conversations c ON c.id=f.conversation_id WHERE c.project_id=? "
            "ORDER BY f.submitted_at DESC",
            (project_id,),
        )
        challenges = await self.db.fetchall(
            "SELECT c.challenge_id AS challenge_id, COALESCE(ch.name, c.challenge_id) AS title, "
            "COALESCE(cs.status, '') AS status, COUNT(DISTINCT c.id) AS conversation_count "
            "FROM conversations c "
            "LEFT JOIN challenges ch ON ch.id=c.challenge_id "
            "LEFT JOIN challenge_sessions cs ON cs.challenge_id=c.challenge_id "
            "WHERE c.project_id=? AND c.challenge_id IS NOT NULL AND c.challenge_id != '' "
            "GROUP BY c.challenge_id ORDER BY conversation_count DESC, c.challenge_id ASC",
            (project_id,),
        )
        facts = await self.db.fetchall(
            "SELECT f.key, f.summary, f.conversation_id, f.updated_at, f.tags FROM facts f "
            "JOIN conversations c ON c.id=f.conversation_id WHERE c.project_id=? "
            "ORDER BY f.updated_at DESC",
            (project_id,),
        )
        attachments = await self.db.fetchall(
            "SELECT a.id, a.filename, a.size, a.kind, a.conversation_id FROM attachments a "
            "JOIN conversations c ON c.id=a.conversation_id WHERE c.project_id=? "
            "ORDER BY a.created_at ASC, a.id ASC",
            (project_id,),
        )

        conversation_rows = [
            {
                "id": row["id"],
                "title": row["title"],
                "updatedAt": row["updated_at"] * 1000,
                "messageCount": int(row.get("message_count") or 0),
                "flagCount": int(row.get("flag_count") or 0),
                "challengeId": row.get("challenge_id"),
            }
            for row in conversations
        ]
        flag_rows = [
            {
                "value": row["value"],
                "conversationId": row.get("conversation_id"),
                "createdAt": row["submitted_at"] * 1000,
            }
            for row in flags
        ]
        challenge_rows = [
            {
                "challengeId": row["challenge_id"],
                "title": row.get("title") or row["challenge_id"],
                "status": row.get("status") or "",
                "conversationCount": int(row.get("conversation_count") or 0),
            }
            for row in challenges
        ]
        fact_rows = [
            {
                "key": row["key"],
                "summary": row.get("summary") or "",
                "conversationId": row.get("conversation_id"),
                "updatedAt": row["updated_at"] * 1000,
                "tags": [t for t in (row.get("tags") or "").split(",") if t],
            }
            for row in facts
        ]
        attachment_rows = [
            {
                "id": row["id"],
                "filename": row.get("filename"),
                "size": int(row.get("size") or 0),
                "kind": row.get("kind") or "",
                "conversationId": row.get("conversation_id"),
            }
            for row in attachments
        ]
        return {
            "project": project,
            "conversations": conversation_rows,
            "flags": flag_rows,
            "challenges": challenge_rows,
            "facts": fact_rows,
            "attachments": attachment_rows,
            "stats": {
                "conversationCount": len(conversation_rows),
                "messageCount": sum(r["messageCount"] for r in conversation_rows),
                "flagCount": len(flag_rows),
                "factCount": len(fact_rows),
                "attachmentCount": len(attachment_rows),
                "challengeCount": len(challenge_rows),
            },
        }

    async def list(self, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        """列出项目（按更新时间倒序）；``status`` 非空时按状态过滤。"""
        base = (
            "SELECT p.*, (SELECT COUNT(*) FROM conversations c WHERE c.project_id=p.id) "
            "AS conversation_count FROM projects p"
        )
        if status:
            rows = await self.db.fetchall(
                f"{base} WHERE p.status=? ORDER BY p.updated_at DESC LIMIT ?", (status, limit)
            )
        else:
            rows = await self.db.fetchall(f"{base} ORDER BY p.updated_at DESC LIMIT ?", (limit,))
        return [_normalize_project(r) for r in rows]


def _normalize_project(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row.get("description") or "",
        "status": row.get("status") or "active",
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
        "meta": _loads(row.get("meta"), {}),
        "conversationCount": int(row.get("conversation_count") or 0),
    }


__all__ = [
    "AttachmentRepo",
    "ChallengeRepo",
    "ChallengeSessionRepo",
    "ConversationRepo",
    "ExecutionRepo",
    "FactRepo",
    "FlagRepo",
    "IdeaRepo",
    "MemoryRepo",
    "MessageRepo",
    "ProjectRepo",
    "Repos",
    "RunRepo",
    "SolverRunRepo",
    "TraceRepo",
    "new_id",
]
