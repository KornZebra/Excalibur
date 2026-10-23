"""M7 平台适配层回归测试（httpx.MockTransport，完全离线）。"""

from __future__ import annotations

import asyncio
import json

import httpx

from ctfagent.competition.adapters import (
    ERROR_ACTIONS,
    McpAdapter,
    RestAdapter,
    TokenBucket,
    classify_error,
    evaluate_path,
)


def _caps() -> dict:
    return {
        "list_exercises": {
            "name": "list_exercises", "transport": "rest", "enabled": True,
            "spec": {"method": "GET", "path": "/ctf/exercise-list",
                     "response": {"path": "data", "item": {"key": "corpus[].id"}}},
        },
        "submit_flag": {
            "name": "submit_flag", "transport": "rest", "enabled": True,
            "spec": {"method": "POST", "path": "/answer-panel/answer",
                     "body": {"exercise_key": "exerciseId", "flag": "flag"},
                     "verdict": {"field": "isCorrect", "success": True}},
        },
        "get_exercise": {
            "name": "get_exercise", "transport": "rest", "enabled": True,
            "spec": {"method": "GET", "path": "/ctf/exercise",
                     "params": {"exercise_key": "exerciseId"}},
        },
    }


def _adapter(handler, *, rate: float = 100.0) -> RestAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RestAdapter(
        base_url="https://platform.test/api", auth_kind="header",
        auth_header="X-Agent-AccessKey", secret="ak_test",
        capabilities=_caps(), rate_limit_per_sec=rate, client=client,
    )


def test_list_exercises_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Agent-AccessKey"] == "ak_test"
        assert request.url.path.endswith("/ctf/exercise-list")
        return httpx.Response(200, json={"code": "00000", "message": "", "data": [
            {"id": 10, "name": "Web", "corpus": [{"id": 1001, "name": "easy-web"}]}
        ]})

    async def _inner() -> None:
        adapter = _adapter(handler)
        res = await adapter.call("list_exercises")
        assert res.ok is True
        assert isinstance(res.data, list) and res.data[0]["name"] == "Web"
        assert evaluate_path(res.data, "corpus[].id") == [1001]
        await adapter.close()

    asyncio.run(_inner())


def test_submit_verdict_false_is_invalid_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body == {"exerciseId": 1001, "flag": "flag{bad}"}
        return httpx.Response(200, json={"code": "00000", "data": {"isCorrect": False}})

    async def _inner() -> None:
        adapter = _adapter(handler)
        res = await adapter.call("submit_flag", exercise_key=1001, flag="flag{bad}")
        assert res.ok is False
        assert res.error_kind == "invalid_flag"
        assert ERROR_ACTIONS["invalid_flag"] == "record_and_cooldown"
        await adapter.close()

    asyncio.run(_inner())


def test_error_classification_matrix() -> None:
    assert classify_error(401, "") == "auth_error"
    assert classify_error(403, "") == "auth_error"
    assert classify_error(429, "") == "rate_limited"
    assert classify_error(503, "") == "platform_down"
    assert classify_error(200, "请求频率超出限制，每秒最多调用3次", code="99999") == "rate_limited"
    assert classify_error(200, "无效的Token或队伍已禁用", code="40100") == "auth_error"
    assert classify_error(200, "赛题实例未运行", code="50001") == "env_not_running"
    assert classify_error(200, "尚未解锁关卡2", code="50002") == "level_locked"
    assert classify_error(200, "该 flag 已提交过", code="50003") == "duplicate"


def test_params_mapping_and_code_failure() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"code": "40001", "message": "参数缺失", "data": {}})

    async def _inner() -> None:
        adapter = _adapter(handler)
        res = await adapter.call("get_exercise", exercise_key=1001)
        assert seen["query"] == {"exerciseId": "1001"}
        assert res.ok is False
        assert res.error_kind in {"unknown", "spec_error"} or res.error_kind == "unknown"
        await adapter.close()

    asyncio.run(_inner())


def test_token_bucket_limits_rate() -> None:
    async def _inner() -> None:
        bucket = TokenBucket(rate_per_sec=5.0)
        start = __import__("time").monotonic()
        for _ in range(12):  # 容量 5，12 次需等待约 1.4s
            await bucket.acquire()
        elapsed = __import__("time").monotonic() - start
        assert elapsed >= 1.0, f"限速未生效：{elapsed:.2f}s"

    asyncio.run(_inner())


def test_mcp_adapter_degrades_without_dependency() -> None:
    async def _inner() -> None:
        adapter = McpAdapter(
            url="http://platform.test/mcp", secret="t", rate_limit_per_sec=100,
            capabilities={"list_exercises": {"name": "list_exercises", "transport": "mcp",
                                             "enabled": True, "spec": {"tool": "list_challenges", "args": {}}}},
        )
        res = await adapter.call("list_exercises")
        # 未安装 mcp 依赖时优雅降级（若已安装则可能是 platform_down，二者都算"未崩溃"）
        assert res.ok is False
        assert res.error_kind in {"unsupported", "platform_down"}

    asyncio.run(_inner())


def test_unknown_capability() -> None:
    async def _inner() -> None:
        adapter = _adapter(lambda r: httpx.Response(200, json={"code": "00000"}))
        res = await adapter.call("build_env", exercise_key=1)
        assert res.ok is False
        assert res.error_kind == "unsupported"
        await adapter.close()

    asyncio.run(_inner())
