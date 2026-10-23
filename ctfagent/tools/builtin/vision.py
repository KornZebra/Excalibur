"""视觉旁路内置工具：analyze_image。

当题目涉及截图、二维码、验证码、隐写预览图等"只有看图才能判断"的场景时，
把图片交给配置好的 VL 模型通道，返回文字摘要（主模型无需具备视觉能力）。

未配置时返回可操作的软失败，提示如何打开（config.yaml 的 ``vision`` 段）。
"""

from __future__ import annotations

import base64
import mimetypes
from typing import Any

from ...models.types import ToolOutcome
from ..context import ToolContext
from ..guard import soft_failure
from .paths import PathScopeError, display_path, resolve_path

_DEFAULT_PROMPT = (
    "你是 CTF 解题助手，请客观描述这张图片中的一切可能线索：文字（含 OCR）、二维码/条码内容、"
    "界面元素、坐标与数字、异常像素区域、隐写痕迹（如边缘噪点、色块）。"
    "请分点输出，不要臆测 flag；若看到疑似 flag 请原样引用。"
)


async def analyze_image(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    config = ctx.config
    if not config.vision.enabled:
        return ToolOutcome.error(
            soft_failure(
                "视觉旁路未启用，无法读图。",
                tool="analyze_image",
                hint=(
                    "在 config.yaml 中设置 vision.enabled=true 且 vision.channel=<某 VL 模型通道名>；"
                    "不启用时可用 zbarimg（二维码）、zsteg（PNG LSB）、exiftool（元数据）、"
                    "python_exec + PIL 做像素分析。"
                ),
            )
        )
    try:
        path = resolve_path(str(args.get("image") or args.get("file") or ""), ctx=ctx, must_exist=True)
    except (PathScopeError, FileNotFoundError, ValueError) as exc:
        return ToolOutcome.error(soft_failure(str(exc), tool="analyze_image"))

    data = path.read_bytes()
    if len(data) > config.vision.max_image_bytes:
        return ToolOutcome.error(
            soft_failure(
                f"图片过大（{len(data)} 字节 > 上限 {config.vision.max_image_bytes}）。",
                tool="analyze_image",
                hint="先缩放/裁剪（python_exec + PIL）再分析。",
            )
        )

    channel_name = config.vision.channel or config.ai.default_channel
    channel = config.ai.channels.get(channel_name)
    if channel is None:
        return ToolOutcome.error(
            soft_failure(
                f"vision.channel={channel_name} 在 ai.channels 中不存在。",
                tool="analyze_image",
                hint="检查 config.yaml 中通道名拼写。",
            )
        )

    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    prompt = str(args.get("prompt") or _DEFAULT_PROMPT)

    try:
        from ...models.client import OpenAICompatibleClient

        client = OpenAICompatibleClient(channel)
        try:
            text = await client.describe_image(base64.b64encode(data).decode(), mime, prompt)
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001 - 视觉调用失败不影响解题主线
        return ToolOutcome.error(
            soft_failure(
                f"视觉模型调用失败：{type(exc).__name__}: {exc}",
                tool="analyze_image",
                hint="检查 vision 通道的 base_url/api_key/模型是否支持多模态；也可改用 zsteg/zbarimg/exiftool。",
            )
        )

    return ToolOutcome.ok(
        f"# analyze_image: {display_path(path, ctx)}\n"
        f"- 通道：{channel_name}（{channel.model}）\n"
        f"- 图片：{len(data)} 字节 {mime}\n\n{text}\n\n"
        "注意：视觉摘要仅作为线索，必须用确定性工具（zsteg/zbarimg/grep/pwntools）复核后再写入黑板。"
    )


__all__ = ["analyze_image"]
