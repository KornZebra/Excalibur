"""内置命令执行工具：``bash`` / ``bash_readonly`` / ``python_exec``。

与 YAML 配方工具共用同一套执行治理（硬超时、无输出终止、stdin 置空、输出落盘、软失败）。
``bash_readonly`` 只允许只读命令前缀，用于 HITL 白名单免审场景。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ...models.types import ToolOutcome
from ..context import ToolContext
from ..guard import (
    detect_dangerous_command,
    is_readonly_command,
    process_result_text,
    soft_failure,
)
from .paths import PathScopeError, display_path, resolve_path


def _log_path(ctx: ToolContext, tool: str) -> Path:
    outputs = ctx.config.outputs_dir()
    return outputs / f"{tool}-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000}.log"


async def _run(
    argv: list[str],
    *,
    ctx: ToolContext,
    tool: str,
    preview: str,
    timeout: float | None = None,
    stdin_data: str | None = None,
    env: dict[str, str] | None = None,
) -> ToolOutcome:
    executor = ctx.execution_service.executor
    log_file = _log_path(ctx, tool)
    await ctx.notify("progress", {"stage": "tool_start", "message": f"$ {preview}", "tool": tool})
    result = await executor.run(
        argv,
        cwd=ctx.resolved_workspace(),
        env=env,
        stdin_data=stdin_data,
        log_file=log_file,
        cancel_event=ctx.cancel_event,
        timeout=timeout if timeout is not None else float(ctx.config.agent.tool_timeout_seconds),
        no_output_timeout=float(ctx.config.agent.no_output_timeout_seconds),
    )
    content, is_error = process_result_text(
        result, preview=preview, tool=tool, hard_timeout_seconds=ctx.config.agent.tool_timeout_seconds
    )
    outcome = ToolOutcome(content=content, is_error=is_error)
    outcome.extra["logFile"] = result.log_file
    outcome.extra["exitCode"] = result.exit_code
    outcome.extra["status"] = result.status()
    return outcome


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------
async def bash(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    command = str(args.get("command") or "").strip()
    if not command:
        return ToolOutcome.error(soft_failure("缺少 command 参数", tool="bash"))
    danger = detect_dangerous_command(command)
    if danger:
        await ctx.notify(
            "progress",
            {
                "stage": "warning",
                "message": f"检测到高危命令模式（{danger}）：{command[:120]}",
                "tool": "bash",
            },
        )
    cwd = None
    if args.get("cwd"):
        try:
            cwd = resolve_path(str(args["cwd"]), ctx=ctx, must_exist=True)
        except (PathScopeError, FileNotFoundError, ValueError) as exc:
            return ToolOutcome.error(soft_failure(str(exc), tool="bash"))
    timeout = args.get("timeout_seconds")
    try:
        timeout_value = float(timeout) if timeout not in (None, "") else None
    except (TypeError, ValueError):
        timeout_value = None
    executor = ctx.execution_service.executor
    log_file = _log_path(ctx, "bash")
    preview = f"bash -lc {command!r}"
    await ctx.notify("progress", {"stage": "tool_start", "message": f"$ {command}", "tool": "bash"})
    result = await executor.run(
        ["bash", "-lc", command],
        cwd=cwd or ctx.resolved_workspace(),
        env={str(k): str(v) for k, v in (args.get("env") or {}).items()} or None,
        log_file=log_file,
        cancel_event=ctx.cancel_event,
        timeout=timeout_value or float(ctx.config.agent.tool_timeout_seconds),
        no_output_timeout=float(ctx.config.agent.no_output_timeout_seconds),
    )
    content, is_error = process_result_text(
        result, preview=preview, tool="bash", hard_timeout_seconds=ctx.config.agent.tool_timeout_seconds
    )
    outcome = ToolOutcome(content=content, is_error=is_error)
    outcome.extra["logFile"] = result.log_file
    outcome.extra["exitCode"] = result.exit_code
    return outcome


# ---------------------------------------------------------------------------
# bash_readonly
# ---------------------------------------------------------------------------
async def bash_readonly(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    command = str(args.get("command") or "").strip()
    if not command:
        return ToolOutcome.error(soft_failure("缺少 command 参数", tool="bash_readonly"))
    gate = ctx.config.hitl.readonly_gate
    if gate.enabled and not is_readonly_command(command, extra_allowlist=gate.extra_allowlist):
        return ToolOutcome.error(
            soft_failure(
                "命令未通过只读白名单校验（可能包含写操作、命令替换或未列入白名单的程序）。",
                tool="bash_readonly",
                hint=(
                    "只读白名单包含 cd/ls/cat/head/tail/grep/file/strings/xxd/base64/readelf/objdump、"
                    "unzip -l/7z l/tar -t 等归档列表命令，支持 cd <目录> && <只读命令> 组合与引号内含 < > | 的参数。"
                    "确需执行其他命令请改用 bash（会走 HITL 审批）或 python_exec；"
                    "也可在配置页 hitl.readonly_gate 中扩充白名单或整体关闭该校验。"
                ),
            )
        )
    return await bash({**args, "command": command}, ctx)


# ---------------------------------------------------------------------------
# python_exec
# ---------------------------------------------------------------------------
async def python_exec(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    code = args.get("code")
    script_path_arg = args.get("script")
    if not code and not script_path_arg:
        return ToolOutcome.error(
            soft_failure(
                "需要提供 code（代码片段）或 script（已存在的脚本路径）之一。",
                tool="python_exec",
            )
        )

    extra_args = [str(a) for a in (args.get("args") or [])]
    if script_path_arg:
        try:
            script = resolve_path(str(script_path_arg), ctx=ctx, must_exist=True)
        except (PathScopeError, FileNotFoundError, ValueError) as exc:
            return ToolOutcome.error(soft_failure(str(exc), tool="python_exec"))
        preview = f"python3 {display_path(script, ctx)} {' '.join(extra_args)}".strip()
        argv = ["python3", str(script), *extra_args]
        stdin_data = args.get("stdin")
        return await _run(
            argv,
            ctx=ctx,
            tool="python_exec",
            preview=preview,
            stdin_data=str(stdin_data) if stdin_data is not None else None,
        )

    # 代码片段：先落盘到 workspace/_scripts/ 便于复现与调试
    scripts_dir = ctx.resolved_workspace() / "_scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script = scripts_dir / f"snippet-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000}.py"
    script.write_text(str(code), encoding="utf-8")
    preview = f"python3 {display_path(script, ctx)}（代码片段已落盘，便于复现）"
    argv = ["python3", str(script), *extra_args]
    stdin_data = args.get("stdin")
    outcome = await _run(
        argv,
        ctx=ctx,
        tool="python_exec",
        preview=preview,
        stdin_data=str(stdin_data) if stdin_data is not None else None,
    )
    outcome.content = f"[脚本已保存：{display_path(script, ctx)}]\n{outcome.content}"
    return outcome


__all__ = ["bash", "bash_readonly", "python_exec"]
