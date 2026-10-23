"""配置、工具、子代理、健康检查与运行期信息路由。"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ..config import (
    ChannelConfig,
    api_key_placeholder_channels,
    changed_config_sections,
    config_section_map,
    describe_secret,
    merge_config_text,
    secret_problem,
    set_config,
    writable_config_path,
)
from ..constants import COMPLIANCE_STATEMENT, __version__
from ..models.client import build_model_client
from ..models.types import ChatMessage
from .routes_agent import get_runtime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["config"])


@router.get("/api/health", summary="健康检查")
async def health(request: Request) -> dict[str, Any]:
    runtime = getattr(request.app.state, "runtime", None)
    return {
        "status": "ok" if runtime is not None else "starting",
        "version": __version__,
        "compliance": COMPLIANCE_STATEMENT.strip(),
    }


@router.get("/api/info", summary="运行期信息（工具统计/子代理/加载错误）")
async def info(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    return runtime.info()


@router.get("/api/config", summary="读取配置（api_key 明文）")
async def read_config(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {
        "config": runtime.config.public_dict(),
        # 让界面能显示"当前配置来自哪个文件 / 首次保存会写到哪"
        "configPath": str(runtime.config.config_path or ""),
        "writableConfigPath": str(writable_config_path(runtime.config)),
        # 文件里仍是 ${VAR} 的 api_key 通道（本版本不再从环境变量读取，需要在界面上重填）
        "apiKeyPlaceholderChannels": api_key_placeholder_channels(runtime.config),
    }


@router.put("/api/config", summary="热更新部分配置字段")
async def update_config(
    request: Request, persist: bool = Query(default=False, description="是否写回 config.yaml")
) -> dict[str, Any]:
    runtime = get_runtime(request)
    try:
        patch = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"请求体不是合法 JSON：{exc}") from exc
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    patch = patch.get("config", patch) if isinstance(patch.get("config"), dict) else patch

    before = runtime.config
    try:
        updated = before.apply_update(patch)
    except Exception as exc:  # noqa: BLE001 - 校验失败要明确告知
        raise HTTPException(status_code=422, detail=f"配置校验失败：{exc}") from exc

    runtime.apply_config(updated)
    set_config(updated)

    persisted_path: str | None = None
    changed_sections: list[str] = []
    if persist:
        target = writable_config_path(updated)
        changed_sections = changed_config_sections(before, updated)
        # 磁盘上还有 ${VAR} 写法的密钥：这次保存顺手改写 ai 段（密钥只认用户填写的内容）
        if api_key_placeholder_channels(path=target) and "ai" not in changed_sections:
            changed_sections.append("ai")
        try:
            _write_config_with_backup(updated, target, sections=changed_sections)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"写回配置文件失败：{exc}") from exc
        # 首次保存可能新建了 config.yaml：让运行期与后续保存都指向真实落盘位置
        if updated.config_path is None or Path(updated.config_path) != target:
            updated.config_path = target
            runtime.apply_config(updated)
            set_config(updated)
        persisted_path = str(target)
        logger.info("配置已写回 %s（变更 section：%s）", target, changed_sections or "无（未重写）")

    return {
        "ok": True,
        "config": updated.public_dict(),
        "persisted": bool(persist),
        "persistedPath": persisted_path,
        "changedSections": changed_sections,
    }


def _write_config_with_backup(
    config: Any, target: Path | None = None, sections: list[str] | None = None
) -> None:
    """写回配置前先备份（``config.yaml.bak``），写坏了可以立刻回滚。

    ``sections`` 非空时只把这些顶层 section 合并进既有文件，**保留其它部分的注释**；
    为 ``None``（例如没有既有文件、或调用方要求整份写出）时写全量。
    没有任何 section 变更时不重写文件，避免无意义地丢注释。密钥（api_key）一律按内存中的
    真实值写出——用户填什么就写什么，不再保留 ``${VAR}`` 占位符。
    """
    target = Path(target) if target is not None else config.config_path
    assert target is not None
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.is_file() else None
    if sections is not None:
        if not sections and existing is not None:
            return  # 配置没变：不碰用户的文件
        data = config_section_map(config)
        payload: Any = {key: data[key] for key in sections if key in data} if existing is not None else data
        text = merge_config_text(existing, payload)
    else:
        text = config.to_yaml()
    if existing is not None:
        backup = target.with_name(target.name + ".bak")
        try:
            shutil.copy2(target, backup)
        except OSError:  # pragma: no cover - 备份失败不阻断保存，但要留日志
            logger.warning("备份配置文件失败：%s", backup, exc_info=True)
    target.write_text(text, encoding="utf-8")


@router.post("/api/config/verify", summary="真实探测各 LLM 通道（密钥 / model / base_url）")
async def verify_channels(request: Request) -> dict[str, Any]:
    """逐通道发一次最小真实请求，把服务商的原始报错直接摊开。

    "到底哪一环错了"（密钥无效？model 名不对？base_url 写错？）由服务商回答，
    比在日志里翻 401/404 快得多。请求体可选：``{"channels": ["main"]}`` 只测指定通道。
    """
    runtime = get_runtime(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 空 body 是合法的
        payload = {}
    wanted = payload.get("channels") if isinstance(payload, dict) else None
    if wanted is not None and not isinstance(wanted, list):
        raise HTTPException(status_code=400, detail='channels 必须是数组，例如 {"channels": ["main"]}')

    results: list[dict[str, Any]] = []
    for key, channel in runtime.config.ai.channels.items():
        if wanted and key not in wanted:
            continue
        results.append(await _probe_channel(key, channel))
    return {"ok": all(item["ok"] for item in results), "results": results}


async def _probe_channel(key: str, channel: ChannelConfig) -> dict[str, Any]:
    """对单个通道做一次 1-token 真实调用。"""
    base: dict[str, Any] = {
        "id": key,
        "name": channel.name,
        "baseUrl": channel.base_url,
        "model": channel.model,
        "apiKey": {
            "description": describe_secret(channel.api_key),
            "placeholder": secret_problem(channel.api_key, channel.base_url) is not None,
        },
    }
    problem = secret_problem(channel.api_key, channel.base_url)
    if problem:
        if problem.startswith("未配置"):
            hint = (
                "该通道没有 api_key：远端端点必须填真实密钥（本地 vLLM/Ollama 才免鉴权）。"
                "也可以把 base_url 指向本机推理服务。"
            )
        else:
            hint = "在 Web 界面「配置」里填写真实密钥（明文，直接粘贴服务商控制台里的 key）。"
        return {**base, "ok": False, "error": f"api_key {problem}", "hint": hint}

    probe = channel.model_copy(
        update={
            "max_completion_tokens": 1,
            "temperature": 0.0,
            "supports_tools": False,
            "max_retries": 1,
            "timeout_seconds": min(float(channel.timeout_seconds or 30), 20.0),
        }
    )
    client = build_model_client(probe)
    started = time.perf_counter()
    try:
        await client.chat([ChatMessage.user("ping")])
    except Exception as exc:  # noqa: BLE001 - 报错原文就是结论
        return {
            **base,
            "ok": False,
            "error": str(exc)[:500],
            "hint": _probe_hint(exc),
            "elapsedMs": round((time.perf_counter() - started) * 1000),
        }
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不影响结论
            logger.debug("探测后关闭客户端失败：%s", key, exc_info=True)
    return {
        **base,
        "ok": True,
        "elapsedMs": round((time.perf_counter() - started) * 1000),
    }


def _probe_hint(exc: BaseException) -> str:
    """把服务商报错翻译成"下一步该改哪个字段"。"""
    text = str(exc).lower()
    if any(k in text for k in ("401", "authentication", "invalid api key", "unauthorized", "api key")):
        return "密钥无效/过期，或不是该服务商签发的密钥：去服务商控制台重新签发并粘贴。"
    if any(k in text for k in ("model_not_found", "does not exist", "no access", "404")):
        return (
            "base_url 与 model 不匹配：DashScope 兼容端点不提供 deepseek-chat（用 qwen-*/deepseek-v3），"
            "只有 https://api.deepseek.com/v1 才用 deepseek-chat。"
        )
    if any(k in text for k in ("timeout", "timed out", "connect", "name or service")):
        return "网络不可达或 base_url 写错：确认能直连该域名（必要时配代理）。"
    if any(k in text for k in ("insufficient", "quota", "balance", "欠费", "余额")):
        return "账户额度不足/欠费：充值或换通道。"
    return "按 error 原文对照服务商文档排查。"


@router.get("/api/config/raw", summary="导出当前配置 YAML（含密钥，仅本机调试用）")
async def raw_config(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {"yaml": runtime.config.to_yaml()}


@router.get("/api/tools", summary="列出工具注册表")
async def list_tools(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {"tools": runtime.registry.public_list(), "summary": runtime.registry.summary()}


@router.get("/api/agents", summary="列出子代理定义")
async def list_agents(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {"agents": runtime.agents.public_list(), "errors": runtime.agents.errors}


@router.get("/api/mcp", summary="MCP 桥接状态")
async def mcp_status(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    if runtime.mcp is None:
        return {"enabled": False, "registered": [], "errors": []}
    return {
        "enabled": runtime.config.mcp.enabled,
        "registered": runtime.mcp.registered,
        "errors": runtime.mcp.errors,
    }


__all__ = ["router"]
