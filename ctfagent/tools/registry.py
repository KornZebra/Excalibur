"""工具注册表：ToolDefinition + tool_search 动态解锁。

三类工具（builtin / YAML recipe / MCP）统一收敛为 :class:`ToolDefinition`：

- ``builtin``：``handler`` 是 Python 协程函数；
- ``yaml``：``handler`` 为 None，``metadata["recipe"]`` 携带命令配方，由 ExecutionService 展开执行；
- ``mcp``：``handler`` 由 :mod:`ctfagent.tools.mcp_bridge` 注入，内部转发到外部 MCP 服务器。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..constants import CORE_TOOL_NAMES
from ..models.types import ToolOutcome

if TYPE_CHECKING:  # pragma: no cover
    from .context import ToolContext

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any], "ToolContext"], Awaitable[ToolOutcome]]

EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "required": []}


@dataclass
class ToolDefinition:
    """一个可被模型调用的工具。"""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=lambda: dict(EMPTY_SCHEMA))
    handler: ToolHandler | None = None
    short_description: str = ""
    source: str = "builtin"
    #: 文件系统来源：``builtin``（``tools/``）或 ``custom``（``tools/custom/``）。
    #: ``source`` 保留注册来源语义（builtin/yaml/mcp），本字段供工具管理板块判定只读。
    origin: str = "builtin"
    category: str = "generic"
    available: bool = True
    enabled: bool = True
    dangerous: bool = False
    interactive: bool = False
    always_expose: bool = False
    unavailable_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def model_description(self) -> str:
        """进模型上下文/schema 的描述文本。"""
        short = self.short_description.strip()
        detail = (self.description or "").strip()
        if short and detail and detail != short:
            text = f"{short}\n\n{detail}"
        else:
            text = short or detail or self.name
        if not self.available:
            text = f"[当前环境不可用：{self.unavailable_reason or '未安装依赖'}] 可改用 bash/python_exec 替代。\n{text}"
        return text

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.model_description,
                "parameters": self.parameters or dict(EMPTY_SCHEMA),
            },
        }

    def index_entry(self) -> dict[str, Any]:
        """紧凑索引项（注入系统提示词 / tool_search 结果）。"""
        return {
            "name": self.name,
            "short": self.short_description or (self.description or "")[:80],
            "category": self.category,
            "source": self.source,
            "available": self.available,
        }

    def index_line(self) -> str:
        flag = "" if self.available else "（不可用）"
        return f"- {self.name}{flag}: {self.short_description or (self.description or '')[:80]}"


class ToolRegistry:
    """工具注册表。"""

    def __init__(
        self,
        *,
        expose_all: bool = True,
        core_names: Iterable[str] = CORE_TOOL_NAMES,
    ) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._unlocked: set[str] = set()
        self.expose_all = expose_all
        self.core_names = set(core_names)
        #: 解析失败的配方（不进入 _tools，避免污染可调用工具面，但要在公开列表里可见）
        self._recipe_errors: list[ToolDefinition] = []

    # -- 注册/注销 --------------------------------------------------------
    def register(self, tool: ToolDefinition) -> ToolDefinition:
        if tool.metadata.get("broken"):
            self._recipe_errors = [b for b in self._recipe_errors if b.name != tool.name]
            self._recipe_errors.append(tool)
            return tool
        self._tools[tool.name] = tool
        return tool

    def register_many(self, tools: Iterable[ToolDefinition]) -> int:
        count = 0
        for tool in tools:
            self.register(tool)
            count += 1
        return count

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._unlocked.discard(name)

    def remove_by_source(self, source: str) -> int:
        names = [n for n, t in self._tools.items() if t.source == source]
        for name in names:
            self.unregister(name)
        return len(names)

    def clear_recipe_errors(self) -> int:
        """清空解析失败记录（热重载前调用）。"""
        count = len(self._recipe_errors)
        self._recipe_errors = []
        return count

    @property
    def recipe_errors(self) -> list[ToolDefinition]:
        return list(self._recipe_errors)

    # -- 查询 -------------------------------------------------------------
    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def require(self, name: str) -> ToolDefinition:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"工具未注册：{name}")
        return tool

    def all(self) -> list[ToolDefinition]:
        return sorted(self._tools.values(), key=lambda t: (t.category, t.name))

    def names(self) -> list[str]:
        return [t.name for t in self.all()]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # -- 动态解锁 ---------------------------------------------------------
    def unlock(self, names: str | Iterable[str]) -> list[str]:
        """解锁工具使其对模型可见（tool_search 命中后调用）。"""
        if isinstance(names, str):
            names = [names]
        added: list[str] = []
        for name in names:
            tool = self.get(name)
            if tool and tool.enabled and tool.available and name not in self._unlocked:
                self._unlocked.add(name)
                added.append(name)
        return added

    @property
    def unlocked(self) -> list[str]:
        return sorted(self._unlocked)

    def is_visible(self, tool: ToolDefinition) -> bool:
        if not tool.enabled:
            return False
        if self.expose_all:
            return True
        return tool.always_expose or tool.name in self.core_names or tool.name in self._unlocked

    def visible_tools(self) -> list[ToolDefinition]:
        return [t for t in self.all() if self.is_visible(t)]

    def schemas(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """产出 OpenAI function calling 的 tools 数组。"""
        if names is not None:
            wanted = set(names)
            tools = [t for t in self.all() if t.name in wanted and t.enabled]
        else:
            tools = self.visible_tools()
        return [t.openai_schema() for t in tools]

    # -- 检索 -------------------------------------------------------------
    def search(self, query: str, limit: int = 15) -> list[ToolDefinition]:
        """关键词检索：名称/短描述/详细描述/类别。命中后自动解锁。"""
        q = (query or "").strip().lower()
        if not q:
            return []
        terms = [t for t in q.replace(",", " ").split() if t]
        scored: list[tuple[float, ToolDefinition]] = []
        for tool in self.all():
            if not tool.enabled:
                continue
            name = tool.name.lower()
            short = (tool.short_description or "").lower()
            detail = (tool.description or "").lower()
            category = (tool.category or "").lower()
            score = 0.0
            for term in terms:
                if term == name:
                    score += 12
                elif term in name:
                    score += 6
                if term in short:
                    score += 4
                if term in detail:
                    score += 2
                if term == category:
                    score += 3
                elif term in category:
                    score += 1.5
            if score > 0:
                if tool.available:
                    score += 0.5
                scored.append((score, tool))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        results = [tool for _, tool in scored[:limit]]
        self.unlock([t.name for t in results])
        return results

    # -- 提示词索引 -------------------------------------------------------
    def index_text(self, *, include_unavailable: bool = True) -> str:
        """紧凑工具索引（name + 短描述），注入系统提示词或 tool_search 空查询结果。"""
        lines: list[str] = []
        current_category = ""
        for tool in self.all():
            if not tool.enabled:
                continue
            if not include_unavailable and not tool.available:
                continue
            if tool.category != current_category:
                current_category = tool.category
                lines.append(f"\n[{current_category}]")
            lines.append(f"- {tool.name}: {tool.short_description or (tool.description or '')[:70]}")
        return "\n".join(lines).strip()

    def summary(self) -> dict[str, int]:
        tools = self.all()
        return {
            "total": len(tools),
            "available": sum(1 for t in tools if t.available),
            "enabled": sum(1 for t in tools if t.enabled),
            "visible": len(self.visible_tools()),
            "builtin": sum(1 for t in tools if t.source == "builtin"),
            "yaml": sum(1 for t in tools if t.source == "yaml"),
            "mcp": sum(1 for t in tools if t.source == "mcp"),
            "custom": sum(1 for t in tools if t.origin == "custom"),
            "broken": len(self._recipe_errors),
        }

    @staticmethod
    def _schema_parameters(tool: ToolDefinition) -> list[dict[str, Any]]:
        """把 JSON schema 参数还原成前端可编辑的列表（YAML 配方再补 position/flag/template）。"""
        schema = tool.parameters or {}
        properties = schema.get("properties") if isinstance(schema, dict) else None
        required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
        formats: dict[str, dict[str, Any]] = {}
        recipe = (tool.metadata or {}).get("recipe")
        for param in getattr(recipe, "parameters", []) or []:
            node: dict[str, Any] = {}
            position = getattr(param, "position", None)
            flag = getattr(param, "flag", None)
            template = getattr(param, "template", None)
            if position is not None:
                node["position"] = position
            elif flag:
                node["flag"] = flag
            elif template:
                node["template"] = template
            if node:
                formats[param.name] = node
        params: list[dict[str, Any]] = []
        for name, spec in (properties or {}).items():
            item = spec if isinstance(spec, dict) else {}
            node = {
                "name": name,
                "type": item.get("type") or "string",
                "required": name in required,
                "description": item.get("description") or "",
            }
            if "default" in item:
                node["default"] = item["default"]
            node.update(formats.get(name, {}))
            params.append(node)
        return params

    def public_entry(self, tool: ToolDefinition) -> dict[str, Any]:
        """单个工具的公开行（``source`` 为 builtin|custom，``kind`` 为注册来源）。"""
        return {
            "name": tool.name,
            "description": tool.short_description or (tool.description or "")[:160],
            "category": tool.category,
            "source": tool.origin,
            "kind": tool.source,
            "available": tool.available,
            "enabled": tool.enabled,
            "dangerous": tool.dangerous,
            "unavailableReason": tool.unavailable_reason,
            "error": "",
            "path": str(tool.metadata.get("path") or ""),
            "parameters": self._schema_parameters(tool),
        }

    def _broken_entry(self, tool: ToolDefinition) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.short_description or tool.description,
            "category": tool.category,
            "source": tool.origin,
            "kind": "yaml",
            "available": False,
            "enabled": False,
            "dangerous": False,
            "unavailableReason": "配方解析失败",
            "error": str(tool.metadata.get("error") or ""),
            "path": str(tool.metadata.get("path") or ""),
        }

    def public_list(self) -> list[dict[str, Any]]:
        return [self.public_entry(t) for t in self.all()] + [
            self._broken_entry(b) for b in self._recipe_errors
        ]


__all__ = ["EMPTY_SCHEMA", "ToolDefinition", "ToolHandler", "ToolRegistry"]
