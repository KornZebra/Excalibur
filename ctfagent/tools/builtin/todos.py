"""计划展示内置工具：write_todos。

把计划写入会话状态并通过 ``todos`` SSE 事件推送前端进度条。
不落库（属于运行期状态），但会随 run 的 trace 一起记录。
"""

from __future__ import annotations

from typing import Any

from ...models.types import ToolOutcome
from ..context import ToolContext
from ..guard import soft_failure

VALID_STATUS = {"pending", "in_progress", "completed"}
STATUS_ICON = {"pending": "☐", "in_progress": "◐", "completed": "☑"}


def _normalize(items: list[Any]) -> list[dict[str, str]]:
    todos: list[dict[str, str]] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            todos.append({"id": str(index + 1), "content": item, "status": "pending"})
            continue
        if isinstance(item, dict):
            status = str(item.get("status") or "pending").lower()
            if status not in VALID_STATUS:
                status = "pending"
            todos.append(
                {
                    "id": str(item.get("id") or (index + 1)),
                    "content": str(item.get("content") or item.get("task") or "").strip(),
                    "status": status,
                }
            )
    return [t for t in todos if t["content"]]


async def write_todos(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    raw = args.get("todos")
    if not isinstance(raw, list) or not raw:
        return ToolOutcome.error(
            soft_failure(
                "todos 必须是非空数组，元素可以是字符串或 {content, status} 对象。",
                tool="write_todos",
                hint='示例：{"todos":[{"content":"理题与登记目标","status":"completed"},{"content":"端口扫描","status":"in_progress"}]}',
            )
        )
    todos = _normalize(raw)
    if not todos:
        return ToolOutcome.error(soft_failure("todos 内容为空", tool="write_todos"))

    ctx.services["todos"] = todos
    await ctx.notify("todos", {"todos": todos})

    done = sum(1 for t in todos if t["status"] == "completed")
    lines = [f"{STATUS_ICON.get(t['status'], '☐')} [{t['status']}] {t['content']}" for t in todos]
    current = next((t["content"] for t in todos if t["status"] == "in_progress"), None)
    tail = f"\n当前进行中：{current}" if current else "\n提示：把正在做的那一条标记为 in_progress。"
    return ToolOutcome.ok(f"计划已更新（{done}/{len(todos)} 完成）：\n" + "\n".join(lines) + tail)


__all__ = ["write_todos"]
