"""竞赛平台适配层（M7）——把「能力规格」变成真实调用。

设计：
- 统一入口 ``PlatformAdapter.call(capability, **kwargs)``，返回 ``NormalizedResult``；
- 三种 transport：``rest``（通用 REST，spec 驱动）、``mcp``（MCP 工具，可选依赖）、
  ``generic_rest``（与 rest 同实现，语义上表示"完全靠 spec"）；
- **错误分类**是策略引擎的输入：7 类 error_kind 决定重试/暂停/跳过；
- **令牌桶**统一限流，速率来自文档解析出的 constraints.rate_limit_per_sec。

安全：凭证由 ``resolve_secret(auth_ref)`` 从 config/环境变量读取，
本模块不持久化任何密钥，日志一律脱敏。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "NormalizedResult",
    "TokenBucket",
    "RestAdapter",
    "McpAdapter",
    "PlatformAdapter",
    "build_adapter",
    "classify_error",
    "resolve_secret",
    "evaluate_path",
]

#: 错误分类 → 策略动作（供 StrategyEngine 读取）
ERROR_ACTIONS = {
    "auth_error": "pause_competition",
    "rate_limited": "backoff",
    "env_not_running": "restart_env",
    "level_locked": "skip_exercise",
    "duplicate": "mark_solved",
    "invalid_flag": "record_and_cooldown",
    "platform_down": "backoff",
    "unsupported": "disable_capability",
    "spec_error": "disable_capability",
    "unknown": "backoff",
}


@dataclass
class NormalizedResult:
    ok: bool
    data: Any = None
    error_kind: str = "unknown"
    message: str = ""
    raw: Any = None
    retry_after: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "errorKind": self.error_kind,
            "message": self.message,
            "retryAfter": self.retry_after,
            "action": ERROR_ACTIONS.get(self.error_kind, "backoff"),
        }


# ---------------------------------------------------------------------------
# 令牌桶
# ---------------------------------------------------------------------------
class TokenBucket:
    """简单令牌桶：``rate`` 个/秒，容量 = rate（允许 1 秒突发）。"""

    def __init__(self, rate_per_sec: float = 1.0) -> None:
        self.rate = max(0.1, float(rate_per_sec))
        self.capacity = max(1.0, self.rate)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """取一个令牌；不足时等待并返回等待秒数。"""
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            need = (1.0 - self._tokens) / self.rate
            await asyncio.sleep(need)
            self._tokens = 0.0
            self._last = time.monotonic()
            return need


# ---------------------------------------------------------------------------
# 凭证解析
# ---------------------------------------------------------------------------
def resolve_secret(auth_ref: str, *, competition_id: str | None = None, runtime: Any = None) -> str:
    """解析密钥：优先进程内存（UI 提供的凭证）→ config: / env: / 裸值。"""
    if competition_id and runtime is not None:
        store = getattr(runtime, "_competition_secrets", None) or {}
        if store.get(competition_id):
            return str(store[competition_id])
    ref = (auth_ref or "").strip()
    if not ref:
        return ""
    if ref.startswith("env:"):
        return os.environ.get(ref[4:], "")
    if ref.startswith("config:"):
        try:
            from ..config import load_config

            cfg = load_config()
            node: Any = cfg
            for part in ref[7:].split("."):
                node = getattr(node, part, None)
                if node is None:
                    return ""
            return str(node)
        except Exception:  # noqa: BLE001 - 配置读取失败视为无密钥
            return ""
    return ref


# ---------------------------------------------------------------------------
# 字段路径求值（spec 里用 a.b / corpus[].id / !flag）
# ---------------------------------------------------------------------------
def evaluate_path(data: Any, path: str) -> Any:
    """按 spec 路径取值（递归实现）。

    支持：``a.b``、``items[].id``（展开列表后继续取子字段）、``!flag``（取反）。
    例：``corpus[].id`` 对 ``[{"corpus":[{"id":1001}]}]`` → ``[1001]``。
    """
    if not path:
        return data
    negate = path.startswith("!")
    if negate:
        path = path[1:]
    node = _walk_path(data, [p for p in path.split(".") if p])
    return (not bool(node)) if negate else node


def _walk_path(node: Any, parts: list[str]) -> Any:
    if not parts:
        return node
    head, rest = parts[0], parts[1:]
    if head.endswith("[]"):
        key = head[:-2]
        seq: Any = None
        if isinstance(node, dict):
            seq = node.get(key)
        elif isinstance(node, list):
            collected: list[Any] = []
            for item in node:
                if not isinstance(item, dict) or key not in item:
                    continue
                val = item.get(key)
                if isinstance(val, list):
                    collected.extend(val)
                elif val is not None:
                    collected.append(val)
            seq = collected
        if not isinstance(seq, list):
            return None
        if not rest:
            return seq
        return [_walk_path(item, rest) for item in seq]
    if isinstance(node, dict):
        return _walk_path(node.get(head), rest)
    if isinstance(node, list):
        return [_walk_path(item.get(head) if isinstance(item, dict) else None, rest) for item in node]
    return None


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------
_AUTH_HINTS = ("无效的token", "token", "accesskey", "未授权", "unauthorized", "forbidden", "队伍已禁用")
_RATE_HINTS = ("频率", "rate limit", "too many", "429", "每秒最多")
_ENV_HINTS = ("实例未运行", "未运行", "not running", "实例不存在")
_LOCK_HINTS = ("尚未解锁", "关卡", "locked", "未开放")
_DUP_HINTS = ("已提交", "重复", "duplicate", "already")
_DOWN_HINTS = ("timeout", "超时", "502", "503", "504", "connection")


def classify_error(
    status_code: int | None,
    body_text: str = "",
    *,
    code: str | None = None,
    verdict_field: str | None = None,
) -> str:
    """把平台响应映射到 7 类 error_kind（顺序敏感：先鉴权，再限流…）。"""
    text = (body_text or "").lower()
    if status_code in (401, 403):
        return "auth_error"
    if status_code == 429:
        return "rate_limited"
    if status_code is not None and status_code >= 500:
        return "platform_down"
    if code is not None and code not in ("00000", "0", ""):
        if any(h in text for h in _AUTH_HINTS):
            return "auth_error"
        if any(h in text for h in _RATE_HINTS):
            return "rate_limited"
        if any(h in text for h in _ENV_HINTS):
            return "env_not_running"
        if any(h in text for h in _LOCK_HINTS):
            return "level_locked"
        if any(h in text for h in _DUP_HINTS):
            return "duplicate"
        if any(h in text for h in _DOWN_HINTS):
            return "platform_down"
        return "unknown"
    if any(h in text for h in _AUTH_HINTS) and "token" in text:
        return "auth_error"
    if any(h in text for h in _RATE_HINTS):
        return "rate_limited"
    if any(h in text for h in _ENV_HINTS):
        return "env_not_running"
    if any(h in text for h in _LOCK_HINTS):
        return "level_locked"
    return "unknown"


# ---------------------------------------------------------------------------
# REST 适配器（spec 驱动）
# ---------------------------------------------------------------------------
class RestAdapter:
    """通用 REST 适配器：完全由 capability.spec 驱动，不含平台特有路径。"""

    def __init__(
        self,
        *,
        base_url: str,
        auth_kind: str,
        auth_header: str,
        secret: str,
        capabilities: dict[str, dict[str, Any]],
        rate_limit_per_sec: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_kind = auth_kind
        self.auth_header = auth_header
        self.secret = secret
        self.capabilities = capabilities
        self.bucket = TokenBucket(rate_limit_per_sec)
        self._client = client
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if not self.secret:
            return headers
        if self.auth_kind == "bearer":
            # 凭证自带 Bearer 前缀时不重复添加（文档整行粘贴的情况）
            value = (self.secret if self.secret[:7].lower() == "bearer "
                     else f"Bearer {self.secret}")
            headers[self.auth_header or "Authorization"] = value
        else:
            headers[self.auth_header or "Authorization"] = self.secret
        return headers

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def call(self, capability: str, **kwargs: Any) -> NormalizedResult:
        cap = self.capabilities.get(capability)
        if not cap:
            return NormalizedResult(False, error_kind="unsupported", message=f"未注册能力：{capability}")
        spec = cap.get("spec") or {}
        path = str(spec.get("path") or "")
        if not path:
            return NormalizedResult(False, error_kind="spec_error", message=f"{capability} 缺少 path")
        await self.bucket.acquire()

        # 参数映射：spec.params / spec.body 里 {逻辑名: 实际字段名}
        query: dict[str, Any] = {}
        for logical, actual in (spec.get("params") or {}).items():
            if logical in kwargs and kwargs[logical] is not None:
                query[str(actual)] = kwargs[logical]
        body: dict[str, Any] = {}
        for logical, actual in (spec.get("body") or {}).items():
            if logical in kwargs and kwargs[logical] is not None:
                body[str(actual)] = kwargs[logical]
        # 未声明 body 映射时，flag 按通用约定放进请求体（提交类接口）
        if not body and kwargs.get("flag") is not None:
            body["flag"] = str(kwargs["flag"])
        method = str(spec.get("method") or "GET").upper()

        # 路径模板：{attempt_id}/{id}/{key} 等占位符按 kwargs / spec.params 填充；
        # 语义兜底：attempt_id/id/exercise_key/key/challenge_id 都接受 exercise_key
        if "{" in path:
            def _fill(match: "re.Match[str]") -> str:
                key = match.group(1)
                if kwargs.get(key) is not None:
                    return str(kwargs[key])
                mapped = (spec.get("params") or {}).get(key)
                if mapped and kwargs.get(mapped) is not None:
                    return str(kwargs[mapped])
                if key in ("attempt_id", "id", "exercise_key", "key", "challenge_id") \
                        and kwargs.get("exercise_key") is not None:
                    return str(kwargs["exercise_key"])
                return match.group(0)
            path = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", _fill, path)

        if path.startswith("http"):
            url = path
        else:
            url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
            # base_url 末段与路径首段重复时去重——仅限末段为 "api" 的常见模式
            # （NSSCTF 文档写 /api/x 而 base_url 已含 /api，否则拼出 /api/api/ 404）。
            # 不加宽条件：DASCTF 等 base 末段是 agent/v1 等，避免误伤真实路径。
            base_last = self.base_url.rstrip("/").rsplit("/", 1)[-1].lower()
            rest = path.lstrip("/")
            first = rest.split("/", 1)[0].lower() if rest else ""
            if base_last == "api" and first == "api" and "/" in rest:
                url = f"{self.base_url.rstrip('/')}/" + rest.split("/", 1)[1]
        client = await self._ensure_client()
        try:
            resp = await client.request(
                method, url, params=query or None, json=body or None, headers=self._headers()
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常统一归平台不可用
            return NormalizedResult(False, error_kind="platform_down", message=f"{type(exc).__name__}: {exc}")

        text = resp.text or ""
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": text[:2000]}

        code = str(payload.get("code")) if isinstance(payload, dict) and "code" in payload else None
        message = str(payload.get("message") or "") if isinstance(payload, dict) else ""

        # 平台成功码：DASCTF 用 00000，NSSCTF 等用 200（HTTP 风格）
        if code is not None and code not in ("00000", "0", "", "200"):
            kind = classify_error(resp.status_code, f"{message} {text[:400]}", code=code)
            return NormalizedResult(False, error_kind=kind, message=message or f"code={code}", raw=payload)

        if resp.status_code >= 400:
            kind = classify_error(resp.status_code, text[:400])
            return NormalizedResult(False, error_kind=kind, message=message or f"HTTP {resp.status_code}", raw=payload)

        # 响应数据：优先取 spec.response.path（如 data）
        # 响应解包：优先 spec.response.path；否则按统一响应约定剥掉 data 包裹层
        data_path = str((spec.get("response") or {}).get("path") or "")
        if data_path:
            data = evaluate_path(payload, data_path)
        elif isinstance(payload, dict) and "data" in payload:
            data = payload["data"]
        else:
            data = payload
        if data is None and isinstance(payload, dict) and "data" in payload:
            data = payload["data"]

        # 提交类：按 spec.verdict.field 判正确性
        verdict_spec = spec.get("verdict") or {}
        if verdict_spec and isinstance(data, dict):
            field_name = str(verdict_spec.get("field") or "")
            success = verdict_spec.get("success", True)
            if field_name and field_name in data and data[field_name] != success:
                return NormalizedResult(
                    False, data=data, error_kind="invalid_flag", message=str(data.get("message") or "答案不正确"),
                    raw=payload,
                )
        return NormalizedResult(True, data=data, raw=payload)


# ---------------------------------------------------------------------------
# MCP 适配器（可选依赖）
# ---------------------------------------------------------------------------
class McpAdapter:
    """MCP（streamable-http）适配器。

    依赖可选包 ``mcp``；未安装时所有调用返回 ``unsupported``，
    由策略引擎把对应能力标记为不可用（不影响 REST 平台）。
    """

    def __init__(
        self,
        *,
        url: str,
        secret: str,
        capabilities: dict[str, dict[str, Any]],
        rate_limit_per_sec: float = 1.0,
    ) -> None:
        self.url = url
        self.secret = secret
        self.capabilities = capabilities
        self.bucket = TokenBucket(rate_limit_per_sec)

    async def call(self, capability: str, **kwargs: Any) -> NormalizedResult:
        cap = self.capabilities.get(capability)
        if not cap:
            return NormalizedResult(False, error_kind="unsupported", message=f"未注册能力：{capability}")
        spec = cap.get("spec") or {}
        tool = str(spec.get("tool") or "")
        if not tool:
            return NormalizedResult(False, error_kind="spec_error", message=f"{capability} 缺少 tool")
        try:
            from mcp import ClientSession  # type: ignore
            from mcp.client.streamable_http import streamablehttp_client  # type: ignore
        except Exception:  # noqa: BLE001 - 依赖缺失
            return NormalizedResult(
                False, error_kind="unsupported",
                message="未安装 mcp 依赖：pip install 'ctf-agent[mcp]'",
            )
        await self.bucket.acquire()
        args = {logical: kwargs[logical] for logical in (spec.get("args") or {}) if logical in kwargs}
        headers = {"Authorization": f"Bearer {self.secret}"} if self.secret else {}
        try:
            async with streamablehttp_client(self.url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, args)
        except Exception as exc:  # noqa: BLE001
            return NormalizedResult(False, error_kind="platform_down", message=f"{type(exc).__name__}: {exc}")
        text = str(getattr(result, "content", "") or "")
        payload: Any = text
        try:
            payload = json.loads(text)
        except ValueError:
            pass
        ok = not bool(getattr(result, "isError", False))
        if not ok:
            return NormalizedResult(False, error_kind=classify_error(None, text), message=text[:400], raw=payload)
        return NormalizedResult(True, data=payload, raw=payload)

    async def close(self) -> None:  # 无持久连接
        return None


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
PlatformAdapter = RestAdapter | McpAdapter


def build_adapter(
    competition: dict[str, Any],
    capabilities: list[dict[str, Any]],
    *,
    constraints: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
    only_enabled: bool = True,
    runtime: Any = None,
) -> PlatformAdapter:
    """按竞赛配置与能力注册表构建适配器。

    ``only_enabled=True`` 时只装载已启用能力（生产路径）；
    连接测试可传 False 以试调未启用能力。
    """
    # 兼容两种传入形态：能力清单（list[dict]）或黑板驱动的 {name: cap} 字典
    cap_list = (list(capabilities.values()) if isinstance(capabilities, dict)
                else list(capabilities or []))
    caps = {c["name"]: c for c in cap_list if (c.get("enabled") or not only_enabled)}
    rate = float((constraints or {}).get("rate_limit_per_sec") or 1.0)
    secret = resolve_secret(
        str(competition.get("authRef") or ""),
        competition_id=str(competition.get("id") or "") or None,
        runtime=runtime,
    )
    kind = str(competition.get("platformKind") or "generic_rest")
    transports = {c.get("transport") for c in caps.values()}
    if kind == "tencent_mcp" or transports == {"mcp"}:
        return McpAdapter(
            url=str(competition.get("baseUrl") or ""),
            secret=secret,
            capabilities=caps,
            rate_limit_per_sec=rate,
        )
    return RestAdapter(
        base_url=str(competition.get("baseUrl") or ""),
        auth_kind=str(competition.get("authKind") or "header"),
        auth_header=str(competition.get("authHeader") or ""),
        secret=secret,
        capabilities=caps,
        rate_limit_per_sec=rate,
        client=client,
    )
