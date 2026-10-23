"""Markdown 子代理定义：``agents/*.md`` front matter 解析与合并。

front matter 字段：``id / name / description / tools / max_iterations / bind_role``。
正文即该子代理的系统提示词。启动时加载目录并与 config 中的同 id 定义合并（**同 id 以 config 覆盖**）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

#: 子代理交付格式的必需段落（用于交付校验）
REQUIRED_DELIVERY_SECTIONS: tuple[str, ...] = ("## 结论", "## 证据")


class AgentDefinitionError(ValueError):
    """子代理定义格式错误。"""


@dataclass
class AgentDefinition:
    """一个 Markdown 子代理。"""

    id: str
    name: str = ""
    description: str = ""
    system_prompt: str = ""
    tools: list[str] | None = None
    max_iterations: int | None = None
    bind_role: str | None = None
    path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    #: 文件系统来源：``builtin``（``agents/``）或 ``custom``（``agents/custom/``）
    source: str = "builtin"
    #: 原始 front matter（供管理 API 原样展示）
    front_matter: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @property
    def dispatchable(self) -> bool:
        """是否可由 roles 模式的 ``task`` 工具派发（``dispatch: false`` 可关闭）。

        blackboard 模式的 Worker 用 ``dispatch: false`` 注册：它出现在 ``/api/agents``，
        但不进入 ``task`` 的工具枚举（避免被编排者 LLM 误当成专项子代理派发）。
        """
        return bool(self.metadata.get("dispatch", True))

    def tool_filter(self) -> list[str] | None:
        """返回工具白名单；None 表示继承全部工具。"""
        return list(self.tools) if self.tools else None

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.display_name,
            "description": self.description,
            "tools": self.tools or [],
            "maxIterations": self.max_iterations,
            "bindRole": self.bind_role,
            "path": self.path,
            "dispatch": self.dispatchable,
            "source": self.source,
        }

    def validate_delivery(self, content: str) -> list[str]:
        """校验交付格式，返回缺失段落列表。"""
        text = content or ""
        return [section for section in REQUIRED_DELIVERY_SECTIONS if section not in text]


def parse_agent_markdown(text: str, *, path: Path | None = None) -> AgentDefinition:
    """解析一个 Markdown 子代理定义文件的内容。"""
    match = _FRONT_MATTER.match(text)
    if not match:
        raise AgentDefinitionError(f"{path}: 缺少 front matter（文件必须以 --- 包裹的 YAML 开头）")
    try:
        front = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise AgentDefinitionError(f"{path}: front matter YAML 解析失败：{exc}") from exc
    if not isinstance(front, dict):
        raise AgentDefinitionError(f"{path}: front matter 必须是映射")

    agent_id = str(front.get("id") or (path.stem if path else "")).strip()
    if not agent_id:
        raise AgentDefinitionError(f"{path}: 缺少 id")

    tools = front.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise AgentDefinitionError(f"{path}: tools 必须是列表")

    body = text[match.end() :].strip()
    if not body:
        raise AgentDefinitionError(f"{path}: 正文（系统提示词）为空")

    return AgentDefinition(
        id=agent_id,
        name=str(front.get("name") or agent_id),
        description=str(front.get("description") or ""),
        system_prompt=body,
        tools=[str(t) for t in tools] if tools else None,
        max_iterations=int(front["max_iterations"]) if front.get("max_iterations") is not None else None,
        bind_role=str(front["bind_role"]) if front.get("bind_role") else None,
        path=str(path) if path else "",
        metadata={
            k: v
            for k, v in front.items()
            if k not in {"id", "name", "description", "tools", "max_iterations", "bind_role"}
        },
        front_matter=dict(front),
    )


def custom_agent_dir(directory: str | Path) -> Path:
    """返回内置子代理目录对应的 ``custom/`` 子目录（测试可 monkeypatch 到 tmp_path）。

    内置目录 ``agents/`` 只读；用户新增/编辑的子代理一律写入 ``agents/custom/``。
    """
    return Path(directory) / "custom"


def load_agent_file(path: Path) -> AgentDefinition:
    return parse_agent_markdown(path.read_text(encoding="utf-8"), path=path)


class AgentRegistry:
    """子代理注册表：加载、合并、查询。"""

    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}
        self.errors: list[str] = []

    # ------------------------------------------------------------------
    def load_directory(self, directory: str | Path, *, source: str = "builtin") -> list[AgentDefinition]:
        """加载目录下所有 ``*.md`` 子代理定义。

        先扫描内置目录，再扫描其 ``custom/`` 子目录（缺失则静默跳过，不影响启动）。
        custom 定义在后注册，因此**同 id 时 custom 覆盖内置**（与既有"后注册覆盖"规则一致，
        也与 config 覆盖 Markdown 的方向一致）。
        """
        directory = Path(directory)
        loaded: list[AgentDefinition] = []
        if not directory.is_dir():
            self.errors.append(f"子代理目录不存在：{directory}")
            return loaded
        loaded.extend(self._load_markdown_dir(directory, source=source))
        custom_dir = custom_agent_dir(directory)
        if custom_dir.is_dir():
            loaded.extend(self._load_markdown_dir(custom_dir, source="custom"))
        return loaded

    def _load_markdown_dir(self, directory: Path, *, source: str) -> list[AgentDefinition]:
        loaded: list[AgentDefinition] = []
        for path in sorted(directory.glob("*.md")):
            if path.name.lower() in {"readme.md", "index.md"}:
                continue
            try:
                agent = load_agent_file(path)
            except AgentDefinitionError as exc:
                self.errors.append(str(exc))
                logger.warning("跳过非法子代理定义：%s", exc)
                continue
            except Exception as exc:  # noqa: BLE001 - 单文件失败不影响整体
                self.errors.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            agent.source = source
            self.register(agent)
            loaded.append(agent)
        return loaded

    def reset(self) -> None:
        """清空内存中的注册结果（热重载的第一步）。"""
        self._agents.clear()
        self.errors.clear()

    def reload(self, directory: str | Path, *, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """原地热重载：不重建对象，持有本注册表的运行期组件立即可见新定义。

        返回新增/更新/删除的数量与 id 列表，便于前端提示"改了什么"。
        """
        before = {agent.id: _agent_signature(agent) for agent in self._agents.values()}
        self.reset()
        self.load_directory(directory)
        if overrides:
            self.merge(overrides)
        after = {agent.id: _agent_signature(agent) for agent in self._agents.values()}
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        updated = sorted(aid for aid in set(before) & set(after) if before[aid] != after[aid])
        return {
            "added": len(added),
            "updated": len(updated),
            "removed": len(removed),
            "addedIds": added,
            "updatedIds": updated,
            "removedIds": removed,
            "total": len(after),
        }

    def register(self, agent: AgentDefinition) -> AgentDefinition:
        self._agents[agent.id] = agent
        return agent

    def merge(self, overrides: dict[str, Any]) -> list[str]:
        """用 config 中的同 id 定义覆盖已加载的子代理（保留未覆盖的字段）。"""
        applied: list[str] = []
        for agent_id, raw in (overrides or {}).items():
            if not isinstance(raw, dict):
                continue
            base = self._agents.get(agent_id)
            merged = AgentDefinition(
                id=agent_id,
                name=str(raw.get("name") or (base.name if base else agent_id)),
                description=str(raw.get("description") or (base.description if base else "")),
                system_prompt=str(raw.get("system_prompt") or (base.system_prompt if base else "")),
                tools=raw.get("tools") if raw.get("tools") is not None else (base.tools if base else None),
                max_iterations=(
                    int(raw["max_iterations"])
                    if raw.get("max_iterations") is not None
                    else (base.max_iterations if base else None)
                ),
                bind_role=str(raw.get("bind_role") or (base.bind_role if base else agent_id)),
                path=str(raw.get("path") or (base.path if base else "")),
                metadata={
                    **(base.metadata if base else {}),
                    **{
                        k: v
                        for k, v in raw.items()
                        if k
                        not in {
                            "name",
                            "description",
                            "system_prompt",
                            "tools",
                            "max_iterations",
                            "bind_role",
                            "path",
                        }
                    },
                },
                source=base.source if base else "builtin",
                front_matter=dict(base.front_matter) if base else {},
            )
            if not merged.system_prompt:
                self.errors.append(f"config 覆盖子代理 {agent_id} 失败：system_prompt 为空且无 Markdown 兜底")
                continue
            self.register(merged)
            applied.append(agent_id)
        return applied

    # ------------------------------------------------------------------
    def get(self, agent_id: str) -> AgentDefinition | None:
        return self._agents.get(agent_id)

    def require(self, agent_id: str) -> AgentDefinition:
        agent = self.get(agent_id)
        if agent is None:
            raise KeyError(f"未定义的子代理：{agent_id}")
        return agent

    def ids(self) -> list[str]:
        return [a.id for a in self.list()]

    def sub_agents(self) -> list[AgentDefinition]:
        """排除主编排代理与 ``dispatch: false`` 的代理后的专项子代理。"""
        return [a for a in self.list() if a.id not in {"ctf-orchestrator", "orchestrator"} and a.dispatchable]

    def workers(self) -> list[AgentDefinition]:
        """可被 blackboard 模式 Dispatcher 驱动的 Worker（含 ``dispatch: false`` 的代理）。"""
        return [a for a in self.list() if a.id not in {"ctf-orchestrator", "orchestrator"}]

    def public_list(self) -> list[dict[str, Any]]:
        return [a.to_public() for a in self.list()]

    def __len__(self) -> int:
        return len(self._agents)

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._agents

    def list(self) -> list[AgentDefinition]:
        return sorted(self._agents.values(), key=lambda a: a.id)


def _agent_signature(agent: AgentDefinition) -> tuple[Any, ...]:
    """用于热重载"是否变化"的稳定指纹（含正文，改提示词也算更新）。"""
    return (
        agent.name,
        agent.description,
        agent.system_prompt,
        tuple(agent.tools or ()),
        agent.max_iterations,
        agent.bind_role,
        agent.source,
    )


__all__ = [
    "REQUIRED_DELIVERY_SECTIONS",
    "AgentDefinition",
    "AgentDefinitionError",
    "AgentRegistry",
    "custom_agent_dir",
    "load_agent_file",
    "parse_agent_markdown",
]
