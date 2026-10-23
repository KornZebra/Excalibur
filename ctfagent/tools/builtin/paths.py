"""路径解析与访问范围控制。

规则：
- 相对路径基于本次运行的工作区解析（绑定题目 → ``workspace/challenge/{id}/``；
  未绑定 → 会话私有 ``workspace/conversations/{cid}/work/``）；
- 以 ``workspace/`` 开头的相对路径基于项目根解析（便于直接使用落盘输出给出的相对路径）；
- 读取允许落在工作区与项目根内；**写入只允许落在 workspace 内**（防止污染项目源码）；
- **跨题隔离（U17）**：任何运行都不得访问其它题目的目录
  （``workspace/challenge/<其它 id>``）或其它会话的目录
  （``workspace/conversations/<其它 cid>``）——否则新会话解题会读到旧题的附件与
  writeup（含已解出的 flag），直接给出旧题答案。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..context import ToolContext


class PathScopeError(PermissionError):
    """路径越界。"""


def _safe_segment(value: str | None, fallback: str) -> str:
    """与 ``AppConfig`` 的目录命名规则保持一致（防目录穿越）。"""
    text = (value or "").strip() or fallback
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", text)


def _allowed_read_roots(ctx: ToolContext) -> list[Path]:
    roots = [ctx.config.workspace_root, ctx.config.project_root]
    return [r.resolve() for r in roots]


def _cross_scope_violation(path: Path, ctx: ToolContext) -> str | None:
    """判断路径是否越入**其它题目/其它会话**的目录；命中返回人类可读原因。

    ``workspace_root`` 位于 ``project_root`` 之内，因此只放宽/收紧根目录无法实现隔离，
    必须对 ``workspace`` 下的两个命名空间做显式判定。
    """
    try:
        relative = path.relative_to(ctx.config.workspace_root.resolve())
    except ValueError:
        return None  # 不在 workspace 内，与本题无关
    parts = relative.parts
    if not parts:
        return None

    if parts[0] == "challenge":
        own = _safe_segment(ctx.challenge_id, "_default") if ctx.challenge_id else None
        if len(parts) == 1:
            return None
        if own is None:
            return (
                f"路径越界：{path} 属于题目工作区，但本会话未绑定任何题目。"
                "本会话请只处理自己的附件（workspace/conversations/<本会话>/attachments）。"
            )
        if parts[1] != own:
            return (
                f"路径越界：{path} 属于其它题目（{parts[1]}），当前题目为 {own}。"
                "跨题读取会导致串题，请只使用本题工作区内的文件。"
            )
        return None

    if parts[0] == "conversations":
        allowed_convs = {_safe_segment(ctx.conversation_id, "_anon")}
        # 子代理继承父会话工作区（workspace/conversations/<父>/work）时，
        # 父会话的附件目录同样属于可读范围。
        try:
            rel_ws = ctx.resolved_workspace().relative_to(ctx.config.workspace_root.resolve())
            if rel_ws.parts and rel_ws.parts[0] == "conversations" and len(rel_ws.parts) > 1:
                allowed_convs.add(rel_ws.parts[1])
        except ValueError:
            pass
        if len(parts) > 1 and parts[1] not in allowed_convs:
            return (
                f"路径越界：{path} 属于其它会话（{parts[1]}），当前会话为 {sorted(allowed_convs)}。"
                "请只使用本会话上传的附件与产物。"
            )
    return None


def resolve_path(
    raw: str,
    *,
    ctx: ToolContext,
    must_exist: bool = False,
    for_write: bool = False,
) -> Path:
    """把工具参数里的路径解析为受控的绝对路径。"""
    if raw is None or str(raw).strip() == "":
        raise PathScopeError("路径不能为空")
    text = os.path.expanduser(os.path.expandvars(str(raw).strip()))
    project_root = ctx.config.project_root.resolve()
    workspace_root = ctx.config.workspace_root.resolve()

    candidate = Path(text)
    if candidate.is_absolute():
        path = candidate
    elif text.startswith("workspace/") or text.startswith("workspace" + os.sep):
        path = project_root / text
    else:
        path = ctx.resolved_workspace() / text
    path = path.resolve()

    violation = _cross_scope_violation(path, ctx)
    if violation:
        raise PathScopeError(violation)

    if for_write:
        if not _is_within(path, workspace_root):
            raise PathScopeError(
                f"写入路径越界：{path} 不在工作区 {workspace_root} 内。"
                "所有产物请写入本次运行的工作区内。"
            )
    else:
        if not any(_is_within(path, root) for root in _allowed_read_roots(ctx)):
            raise PathScopeError(
                f"读取路径越界：{path} 不在工作区或项目根内（只允许访问题目环境与项目文件）。"
            )
    if must_exist and not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    return path


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def display_path(path: Path, ctx: ToolContext) -> str:
    """展示用路径：优先相对项目根，其次绝对路径。"""
    try:
        return str(path.relative_to(ctx.config.project_root.resolve()))
    except ValueError:
        return str(path)


__all__ = ["PathScopeError", "display_path", "resolve_path"]
