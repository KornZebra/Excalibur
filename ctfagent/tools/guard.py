"""工具输出治理：超限截断落盘、``<persisted-output>`` 通知、软失败协议。

设计宪法第 4/5 条：
- 工具输出 > ``max_tool_result_bytes`` 时全文落盘 ``workspace/_outputs/``，模型只收到摘要通知；
- 所有工具错误都转成带 ``[TOOL_ERROR]`` 前缀的文本结果返回，绝不让单工具失败中断 Agent 循环。
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..constants import (
    DANGEROUS_COMMAND_PATTERNS,
    PERSISTED_OUTPUT_TAG,
    READONLY_COMMAND_ALLOWLIST,
    READONLY_EXEC_FLAG_PREFIXES,
    TOOL_ERROR_PREFIX,
)

if TYPE_CHECKING:  # 仅用于类型标注，避免与 executor 形成运行时循环导入
    from .executor import ProcessResult

#: 单条工具结果默认上限（字节）
DEFAULT_MAX_RESULT_BYTES = 12_000
#: 摘要里保留的头部/尾部字节数
HEAD_BYTES = 2_048
TAIL_BYTES = 3_072


@dataclass
class GuardedOutput:
    """治理后的输出。"""

    content: str
    persisted_file: str | None = None
    truncated: bool = False
    original_bytes: int = 0


def _safe_slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.\-]", "_", text or "output")
    return slug[:limit] or "output"


def persist_output(
    text: str,
    *,
    workspace: Path,
    name: str = "output",
    execution_id: str | None = None,
) -> Path:
    """把完整输出落盘到 ``workspace/_outputs/``，返回绝对路径。"""
    outputs = Path(workspace) / "_outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"-{_safe_slug(execution_id, 24)}" if execution_id else ""
    path = outputs / f"{stamp}-{_safe_slug(name)}{suffix}.log"
    counter = 1
    while path.exists():  # 极端情况下避免覆盖
        path = outputs / f"{stamp}-{_safe_slug(name)}{suffix}-{counter}.log"
        counter += 1
    path.write_text(text, encoding="utf-8", errors="replace")
    return path


def truncate_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """按字节截断文本（保留合法 UTF-8 边界）。"""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text, False
    head = raw[:HEAD_BYTES].decode("utf-8", errors="ignore")
    tail = raw[-TAIL_BYTES:].decode("utf-8", errors="ignore")
    omitted = len(raw) - len(head.encode()) - len(tail.encode())
    merged = f"{head}\n\n... [省略 {omitted} 字节] ...\n\n{tail}"
    return merged, True


def guard_output(
    text: str,
    *,
    workspace: Path,
    name: str = "output",
    max_bytes: int = DEFAULT_MAX_RESULT_BYTES,
    execution_id: str | None = None,
    tool_name: str = "",
    hint: str | None = None,
    existing_file: str | None = None,
) -> GuardedOutput:
    """输出治理主入口：超限则落盘并返回摘要通知。

    ``existing_file`` 用于复用执行器已经写好的完整日志（避免重复落盘）。
    """
    text = text if text is not None else ""
    original = len(text.encode("utf-8", errors="replace"))
    if max_bytes > 0 and original > max_bytes:
        if existing_file:
            path = Path(existing_file)
        else:
            path = persist_output(
                text, workspace=workspace, name=name or tool_name, execution_id=execution_id
            )
        preview, _ = truncate_text(text, max(2_048, max_bytes // 2))
        rel = _relative_display(path, workspace)
        notice = (
            f'<{PERSISTED_OUTPUT_TAG} file="{path}" relative="{rel}" bytes="{original}">\n'
            f"输出超过 {max_bytes} 字节，已全文落盘。以下是头尾摘要，"
            f'可用 read_file(file="{rel}", offset=..., limit=...) 分段取回完整内容。\n'
            f"</{PERSISTED_OUTPUT_TAG}>\n\n{preview}"
        )
        if hint:
            notice += f"\n\n{hint}"
        return GuardedOutput(
            content=notice, persisted_file=str(path), truncated=True, original_bytes=original
        )
    content = text
    if hint:
        content = f"{content}\n\n{hint}" if content else hint
    return GuardedOutput(content=content, persisted_file=None, truncated=False, original_bytes=original)


def _relative_display(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(Path(workspace).parent))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# 软失败协议
# ---------------------------------------------------------------------------
def soft_failure(message: str, *, hint: str | None = None, tool: str = "") -> str:
    """包装为对模型友好的软失败文本。"""
    prefix = f"{TOOL_ERROR_PREFIX} "
    if tool:
        prefix += f"[{tool}] "
    text = f"{prefix}{message}"
    if hint:
        text += f"\n建议下一步：{hint}"
    return text


def wrap_exception(exc: BaseException, *, tool: str = "", hint: str | None = None) -> str:
    """把任意异常转成软失败文本（绝不向上抛）。"""
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, TimeoutError):
        return soft_failure(
            f"工具执行超时（{detail}）。",
            hint=hint or "缩小任务范围、改用非交互参数，或把命令放入后台并用 wait_tool_execution 续等。",
            tool=tool,
        )
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return soft_failure(
            f"文件/权限错误：{detail}",
            hint=hint or "检查路径是否正确、是否位于 workspace 内；必要时先 list_dir 确认。",
            tool=tool,
        )
    if isinstance(exc, KeyError):
        return soft_failure(
            f"缺少必需参数：{detail}",
            hint=hint or "调用前对照工具参数的 JSON Schema，补齐 required 字段。",
            tool=tool,
        )
    return soft_failure(
        f"执行失败：{detail}",
        hint=hint or "检查参数与目标可达性；若工具不可用可改用 bash 或 python_exec 替代。",
        tool=tool,
    )


# ---------------------------------------------------------------------------
# bash 安全判定
# ---------------------------------------------------------------------------
def detect_dangerous_command(command: str) -> str | None:
    """返回命中高危模式的中文说明；未命中返回 None。"""
    for pattern, label in DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, command or ""):
            return label
    return None


#: 允许通过的重定向目标：流复用/丢弃，不产生落盘写
_READONLY_STREAM_TARGETS = frozenset({"2>&1", "2>/dev/null"})
#: 未加引号的 token 中出现这些字符即判定为 shell 结构（重定向/管道/后台等），fail-closed
_SHELL_STRUCTURE_CHARS = ("<", ">", ";", "|", "&")


def _shlex_segments(command: str) -> list[list[str]] | None:
    """把复合命令按**未被引号包裹**的 ``&&/||/;/|`` 切成子命令段（token 列表）。

    引号内的这些字符是参数字面量（如 ``grep -E "a|b"``），不得据此切段；
    引号不平衡等解析失败返回 None（调用方 fail-closed 拒绝）。
    """
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in ("&&", "||", ";", "|"):
            segments.append(current)
            current = []
        else:
            current.append(tok)
    segments.append(current)
    return segments


def _token_unsafe(tok: str) -> bool:
    """单个 token 是否含未引用的 shell 结构字符（重定向/管道/顺序/后台）。"""
    if tok in _READONLY_STREAM_TARGETS:
        return False
    if tok[:1] in {'"', "'"}:  # 整体引号包裹的字面量参数
        return False
    if tok.startswith(READONLY_EXEC_FLAG_PREFIXES):
        return True  # 能执行外部程序的 flag（tar --to-command、binwalk --dd 等）
    return any(ch in tok for ch in _SHELL_STRUCTURE_CHARS)


def _segment_is_allowlisted(tokens: list[str], *, extra_allowlist: tuple[str, ...] = ()) -> bool:
    """按只读白名单前缀匹配一个子命令段。"""
    seg = " ".join(tokens)
    first = tokens[0] if tokens else ""
    # sed 的原地编辑是写操作
    if first == "sed" and any(
        t == "-i" or t.startswith("-i.") or t.startswith("--in-place") for t in tokens
    ):
        return False
    # awk 程序体内可重定向或调用 system()，出现即判非只读
    if first in {"awk", "gawk", "mawk"}:
        program = " ".join(t for t in tokens if t[:1] in {'"', "'"})
        if ">" in program or "system" in program:
            return False
    allowlist = READONLY_COMMAND_ALLOWLIST + extra_allowlist
    return any(seg == allowed or seg.startswith(allowed + " ") for allowed in allowlist)


def is_readonly_command(
    command: str, *, extra_allowlist: tuple[str, ...] | list[str] = ()
) -> bool:
    """判断命令是否落在只读白名单内（用于 ``bash_readonly`` 与 HITL 免审）。

    fail-closed：命令替换（``$()``/反引号）、未引用的重定向/管道/后台、
    能执行外部程序的 flag（``--to-command`` 等）、引号不平衡、未列入白名单的
    程序一律判否；引号内的 ``<``/``>``/``|`` 是参数字面量（如 ``grep "<html"``），不算。
    ``cd <目录> && <只读命令>`` 是合法组合（cd 只影响子 shell 工作目录）。

    ``extra_allowlist``：配置（``hitl.readonly_gate.extra_allowlist``）追加的自定义前缀。
    """
    stripped = (command or "").strip()
    if not stripped:
        return False
    # 命令替换无法按前缀静态判定，直接拒绝
    if "$(" in stripped or "`" in stripped:
        return False
    segments = _shlex_segments(stripped)
    if segments is None:
        return False
    for tokens in segments:
        if not tokens:
            continue
        if any(_token_unsafe(tok) for tok in tokens):
            return False
        if not _segment_is_allowlisted(tokens, extra_allowlist=tuple(extra_allowlist)):
            return False
    return True


def command_preview(argv: list[str], limit: int = 400) -> str:
    """把 argv 渲染成可读命令串（用于日志/HITL/前端展示）。"""
    import shlex

    rendered = " ".join(shlex.quote(a) for a in argv)
    return rendered if len(rendered) <= limit else rendered[:limit] + " ..."


# ---------------------------------------------------------------------------
# 进程结果 → 模型可见文本
# ---------------------------------------------------------------------------
def process_result_text(
    result: ProcessResult,
    *,
    preview: str,
    tool: str,
    hard_timeout_seconds: int = 0,
) -> tuple[str, bool]:
    """把 :class:`~ctfagent.tools.executor.ProcessResult` 转成 (文本, is_error)。

    YAML 配方工具与内置 bash/python_exec 共用此格式化逻辑，保证软失败措辞一致。
    """
    header = f"$ {preview}\n[exit={result.exit_code} status={result.status()} 用时 {result.duration_ms}ms]"
    if result.spawn_error:
        return (
            soft_failure(
                str(result.spawn_error),
                tool=tool,
                hint="确认命令名/路径；未安装的工具可用 bash 或 python_exec 替代。",
            ),
            True,
        )
    if result.no_output_timeout:
        return (
            soft_failure(
                "命令连续无输出被终止，疑似等待交互输入；"
                "可改用非交互参数（-y/--batch/-c/管道输入）或 pwntools process()。",
                tool=tool,
                hint="若确需长时间运行，请拆分为后台命令并用 wait_tool_execution 续等。",
            )
            + f"\n{header}\n{result.output}",
            True,
        )
    if result.hard_timeout:
        return (
            soft_failure(
                f"命令超过硬超时（{hard_timeout_seconds or '配置值'}s）被终止。",
                tool=tool,
                hint="缩小扫描范围/降低并发，或改为后台任务后用 wait_tool_execution 续等。",
            )
            + f"\n{header}\n{result.output}",
            True,
        )
    if result.cancelled:
        return (soft_failure("命令已被取消。", tool=tool) + f"\n{header}", True)

    body = f"{header}\n{result.output}".rstrip()
    is_error = result.exit_code not in (0, None)
    if is_error:
        body = (
            soft_failure(
                f"命令以非零退出码结束（exit={result.exit_code}）。",
                tool=tool,
                hint="读取上方输出定位原因；检查参数是否被目标拒绝或工具是否缺少依赖。",
            )
            + f"\n{body}"
        )
    return body, is_error


__all__ = [
    "DEFAULT_MAX_RESULT_BYTES",
    "GuardedOutput",
    "command_preview",
    "detect_dangerous_command",
    "guard_output",
    "is_readonly_command",
    "persist_output",
    "process_result_text",
    "soft_failure",
    "truncate_text",
    "wrap_exception",
]
