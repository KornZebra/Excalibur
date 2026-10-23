"""模型目录、会话级模型选择与模型通道管理路由。

对应 DSH（deepseek-harness）的两块能力：
- ``ui-model-selection``：composer 发送键旁的模型下拉（``GET /api/models``）
- ``ui-settings-models``：用户自建模型提供商（``POST/DELETE /api/ai/channels``）

模型即通道：本项目 ``ai.channels`` 的每个 key 就是一个可选项，composer 选中后
按会话持久化（``conversations.meta.modelChannel``），运行时会话据此构建**只含
被选通道**的专用客户端（不跨模型 failover——用户显式选定的模型不应被静默替换）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from ..config import ChannelConfig, set_config, writable_config_path
from ..runtime import Runtime
from .routes_agent import get_runtime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["models"])


def _channel_view(key: str, channel: ChannelConfig, *, default: bool) -> dict[str, Any]:
    """通道的目录投影（composer 下拉与模型管理的公共读模型）。"""
    return {
        "id": key,
        "name": channel.name or key,
        "model": channel.model,
        "provider": channel.provider,
        "baseUrl": channel.base_url,
        "hasKey": bool(channel.api_key),
        "isDefault": default,
    }


def _models_payload(runtime: Runtime) -> dict[str, Any]:
    ai = runtime.config.ai
    return {
        "models": [
            _channel_view(key, channel, default=(key == ai.default_channel))
            for key, channel in ai.channels.items()
        ],
        "defaultChannel": ai.default_channel,
    }


def _persist_ai(runtime: Runtime, ai: Any) -> dict[str, Any]:
    """热应用 + 全局注册 + 写盘（保留注释的 section 合并，失败不阻塞内存生效）。"""
    updated = runtime.config.model_copy(update={"ai": ai})
    runtime.apply_config(updated)
    set_config(updated)
    try:
        from .routes_config import _write_config_with_backup

        target = writable_config_path(updated)
        _write_config_with_backup(updated, target, sections=["ai"])
        if updated.config_path is None or str(updated.config_path) != str(target):
            updated.config_path = target
            runtime.apply_config(updated)
            set_config(updated)
    except Exception:  # noqa: BLE001 - 写盘失败不推翻内存里的生效配置
        logger.warning("模型配置写盘失败（内存已生效）", exc_info=True)
    return _models_payload(runtime)


@router.get("/api/models", summary="模型目录（composer 模型下拉数据源）")
async def list_models(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    return _models_payload(runtime)


class ConversationModelRequest(BaseModel):
    """会话级模型选择；``channel=None`` 表示跟随全局默认。"""

    channel: str | None = Field(default=None, description="模型通道 key；缺省/空 = 跟随默认")


@router.put("/api/conversations/{conversation_id}/model", summary="设置会话使用的模型")
async def set_conversation_model(
    conversation_id: str, payload: ConversationModelRequest, request: Request
) -> dict[str, Any]:
    runtime = get_runtime(request)
    channel = (payload.channel or "").strip() or None
    if channel is not None and channel not in runtime.config.ai.channels:
        raise HTTPException(status_code=422, detail=f"未知模型通道：{channel}")
    updated = await runtime.repos.conversations.set_model(conversation_id, channel)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"未知会话：{conversation_id}")
    return {"ok": True, "conversation": updated}


class ChannelUpsertRequest(BaseModel):
    """新增/编辑一个模型通道（DSH ProviderEditor 的写入口）。

    ``api_key`` 三态：``None`` = 不改动（编辑已存密钥的模型时不必重填）；
    字符串 = 设置（空串 = 清空）。其余字段仅在非空时覆盖，未传保持原值。
    """

    key: str = Field(..., min_length=1, max_length=64, description="通道 key（模型标识）")
    name: str = Field(default="", description="显示名")
    provider: str = Field(default="", description="提供方（openai_compatible/deepseek/...）")
    base_url: str = Field(default="", description="API 基础地址")
    api_key: str | None = Field(default=None, description="API 密钥（三态，见上）")
    model: str = Field(default="", description="模型 ID")
    temperature: float | None = None
    max_completion_tokens: int | None = None
    timeout_seconds: float | None = None
    reasoning_effort: str | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None


@router.post("/api/ai/channels", summary="新增或编辑模型通道（模型管理卡片）")
async def upsert_channel(payload: ChannelUpsertRequest, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    ai = runtime.config.ai
    existing = ai.channels.get(payload.key)
    if existing is None and not payload.model.strip():
        raise HTTPException(status_code=422, detail="新模型必须填写模型 ID")

    data: dict[str, Any] = {}
    if payload.name.strip():
        data["name"] = payload.name.strip()
    if payload.provider.strip():
        data["provider"] = payload.provider.strip()
    if payload.base_url.strip():
        data["base_url"] = payload.base_url.strip()
    if payload.model.strip():
        data["model"] = payload.model.strip()
    if payload.api_key is not None:
        data["api_key"] = payload.api_key
    if payload.temperature is not None:
        data["temperature"] = payload.temperature
    if payload.max_completion_tokens is not None:
        data["max_completion_tokens"] = payload.max_completion_tokens
    if payload.timeout_seconds is not None:
        data["timeout_seconds"] = payload.timeout_seconds
    if payload.reasoning_effort is not None:
        data["reasoning_effort"] = payload.reasoning_effort or None
    if payload.supports_tools is not None:
        data["supports_tools"] = payload.supports_tools
    if payload.supports_vision is not None:
        data["supports_vision"] = payload.supports_vision

    try:
        if existing is not None:
            channel = existing.model_copy(update=data)
        else:
            channel = ChannelConfig(**{"name": payload.name.strip() or payload.key, **data})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"通道参数校验失败：{exc}") from exc

    channels = dict(ai.channels)
    channels[payload.key] = channel
    # 首个通道自动成为默认（与 AIConfig 的"至少一个通道"不变量兼容）
    default = ai.default_channel if ai.default_channel in channels else payload.key
    new_ai = ai.model_copy(update={"channels": channels, "default_channel": default})
    return {"ok": True, **_persist_ai(runtime, new_ai)}


@router.delete("/api/ai/channels/{key}", summary="删除模型通道")
async def delete_channel(key: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    ai = runtime.config.ai
    if key not in ai.channels:
        raise HTTPException(status_code=404, detail=f"未知模型通道：{key}")
    if len(ai.channels) <= 1:
        raise HTTPException(status_code=422, detail="至少保留一个模型通道")
    channels = {k: v for k, v in ai.channels.items() if k != key}
    default = ai.default_channel if ai.default_channel in channels else next(iter(channels))
    new_ai = ai.model_copy(update={"channels": channels, "default_channel": default})
    return {"ok": True, **_persist_ai(runtime, new_ai)}


class DefaultChannelRequest(BaseModel):
    channel: str = Field(..., description="设为默认的模型通道 key")


@router.post("/api/ai/default-channel", summary="设置默认模型通道")
async def set_default_channel(payload: DefaultChannelRequest, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    ai = runtime.config.ai
    if payload.channel not in ai.channels:
        raise HTTPException(status_code=422, detail=f"未知模型通道：{payload.channel}")
    new_ai = ai.model_copy(update={"default_channel": payload.channel})
    return {"ok": True, **_persist_ai(runtime, new_ai)}
