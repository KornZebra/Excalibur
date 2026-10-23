"""子代理与工具管理路由（U3 后端）。

职责边界（**纯增量**）：

- 只读写 ``agents/custom/`` 与 ``tools/custom/``：内置目录 ``agents/``、``tools/`` 只读，
  通过 API 编辑/删除内置条目一律 403，并提示用「另存为副本」克隆到 custom 后再改；
- 元数据来自文件系统 + 内存注册表，**不新增数据库表/迁移**；
- 热重载原地刷新 ``runtime.agents`` / ``runtime.registry``，持有注册表引用的运行期组件立即
  看到新定义（无需重启进程）；响应回报 added/updated/removed 计数，幂等；
- 所有写入原子（同目录临时文件 + ``os.replace``），文件名只由 id/name 派生并消毒。

设计选择（详见 README「子代理与工具管理 API」）：

- custom 覆盖规则：先加载内置、后加载 ``custom/``，同 id **custom 覆盖内置**（与既有
  "后注册覆盖"、以及 config 覆盖 Markdown 的方向一致）；
- 重命名：``PUT`` 允许改 ``id``（写新文件后删除旧文件）；克隆用
  ``POST /api/agents/markdown-agents/{id}/duplicate``；
- 工具克隆：``POST /api/tools/copy``（仅 YAML 配方）；
- 工具改名：``PUT`` 不支持改名（请求体 name 与路径不一致 → 422），避免注册表 key 漂移。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Query, Request

from ..agents import markdown as agent_markdown
from ..agents.markdown import AgentDefinition
from ..tools import yaml_loader as tool_loader
from ..tools.registry import ToolDefinition
from .routes_agent import get_runtime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])

#: 子代理 id 规则（与前端契约一致）
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
#: 工具 name 规则（允许 _ . -，但必须字母数字开头）
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

#: 危险命令模式（命中且未 acknowledge_risk=true 时拒绝）
_DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("rm -rf", "递归强制删除"),
    ("rm -fr", "递归强制删除"),
    ("mkfs", "格式化文件系统"),
    ("shutdown", "关机"),
    ("reboot", "重启"),
    ("dd if=", "裸设备/磁盘写入"),
    (":(){", "fork 炸弹"),
    ("chmod 777 /", "危险的全盘权限放宽"),
    (":(){ :|:& };:", "fork 炸弹"),
    ("> /dev/sd", "直接写块设备"),
)

#: 管理 API 支持的参数类型（外部用 bool/number，落盘映射到 loader 的 boolean/integer）
_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "bool": "boolean",
    "boolean": "boolean",
    "number": "number",
    "integer": "integer",
    "int": "integer",
}


# ---------------------------------------------------------------------------
# 目录与安全写入
# ---------------------------------------------------------------------------
def _agents_root(config: Any) -> Path:
    return Path(config.project_root) / "agents"


def _tools_root(config: Any) -> Path:
    return Path(config.project_root) / "tools"


def _custom_agents_dir(config: Any) -> Path:
    # 通过模块属性调用，测试可 monkeypatch 到 tmp_path，避免污染真实仓库
    return agent_markdown.custom_agent_dir(_agents_root(config))


def _custom_tools_dir(config: Any) -> Path:
    return tool_loader.custom_tool_dir(_tools_root(config))


def _reject_unsafe(name: str, *, what: str) -> None:
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=422, detail=f"{what} 不能包含 / \\ 或 ..：{name!r}")


def _safe_agent_filename(agent_id: str) -> str:
    """文件名只由 id 派生：``[a-z0-9-]`` 且不含路径分隔符。"""
    _reject_unsafe(agent_id, what="id")
    if not _ID_RE.match(agent_id):
        raise HTTPException(
            status_code=422,
            detail=f"id 不合法：{agent_id!r}（必须匹配 ^[a-z0-9][a-z0-9-]*$）",
        )
    return f"{agent_id}.md"


def _safe_tool_filename(name: str) -> str:
    _reject_unsafe(name, what="工具名")
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise HTTPException(status_code=422, detail=f"工具名无法派生合法文件名：{name!r}")
    return f"{slug}.yaml"


def _atomic_write(path: Path, text: str) -> None:
    """同目录临时文件 + ``os.replace``：写一半崩掉也不会留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 允许空 body
        return {}
    return body if isinstance(body, dict) else {}


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def _custom_overrides(runtime: Any) -> dict[str, Any]:
    return getattr(runtime.config.agent, "custom_agents", None) or {}


# ---------------------------------------------------------------------------
# 子代理：序列化 / 校验
# ---------------------------------------------------------------------------
def _agent_row(agent: AgentDefinition) -> dict[str, Any]:
    tools = list(agent.tools or [])
    return {
        "id": agent.id,
        "name": agent.display_name,
        "description": agent.description,
        "maxIterations": agent.max_iterations,
        "toolCount": len(tools),
        "tools": tools,
        "source": agent.source,
        "body": agent.system_prompt,
        "frontMatter": dict(agent.front_matter),
        "systemPromptLength": len(agent.system_prompt or ""),
    }


def _render_agent_markdown(front: dict[str, Any], body: str) -> str:
    clean = {k: v for k, v in front.items() if v is not None}
    front_text = yaml.safe_dump(clean, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{front_text.strip()}\n---\n\n{body.strip()}\n"


def _normalize_tools(raw: Any, runtime: Any) -> list[str] | None:
    if raw in (None, "", []):
        return None
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="tools 必须是字符串数组")
    names = [str(item).strip() for item in raw if str(item).strip()]
    unknown = [name for name in names if runtime.registry.get(name) is None]
    if unknown:
        raise HTTPException(status_code=422, detail=f"引用了不存在的工具：{', '.join(unknown)}")
    return names


def _normalize_max_iterations(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"maxIterations 必须是正整数：{raw!r}") from exc
    if value <= 0:
        raise HTTPException(status_code=422, detail=f"maxIterations 必须大于 0：{value}")
    return value


def _existing_agent_path(config: Any, agent: AgentDefinition) -> Path:
    """定位 custom 文件；文件在 custom 目录内时按真实路径操作，否则由 id 派生。"""
    custom_dir = _custom_agents_dir(config)
    if agent.path:
        candidate = Path(agent.path)
        try:
            if candidate.resolve().parent == custom_dir.resolve():
                return candidate
        except OSError:  # pragma: no cover - 极少见
            pass
    return custom_dir / _safe_agent_filename(agent.id)


def _reload_agents(runtime: Any) -> dict[str, Any]:
    info = runtime.agents.reload(_agents_root(runtime.config), overrides=_custom_overrides(runtime))
    info["errors"] = list(runtime.agents.errors)
    return info


# ---------------------------------------------------------------------------
# 子代理路由
# ---------------------------------------------------------------------------
@router.get("/api/agents/markdown-agents", summary="列出子代理（含 custom 与解析错误）")
async def list_markdown_agents(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    rows = [_agent_row(agent) for agent in runtime.agents.list()]
    return {
        "agents": rows,
        "errors": list(runtime.agents.errors),
        "summary": {
            "total": len(rows),
            "builtin": sum(1 for r in rows if r["source"] == "builtin"),
            "custom": sum(1 for r in rows if r["source"] == "custom"),
        },
    }


@router.post("/api/agents/markdown-agents", summary="创建自定义子代理")
async def create_markdown_agent(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    body = await _json_body(request)

    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name 不能为空")
    raw_id = str(body.get("id") or "").strip() or _slugify(name)
    filename = _safe_agent_filename(raw_id)
    if runtime.agents.get(raw_id) is not None:
        raise HTTPException(status_code=409, detail=f"子代理 id 已存在：{raw_id}")

    description = str(body.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=422, detail="description 不能为空")
    agent_body = str(body.get("body") or "").strip()
    if not agent_body:
        raise HTTPException(status_code=422, detail="body（系统提示词）不能为空")

    tools = _normalize_tools(body.get("tools"), runtime)
    max_iterations = _normalize_max_iterations(body.get("maxIterations", body.get("max_iterations")))

    target = _custom_agents_dir(runtime.config) / filename
    if target.exists():
        raise HTTPException(status_code=409, detail=f"文件已存在：{target.name}")

    front: dict[str, Any] = {
        "id": raw_id,
        "name": name,
        "description": description,
        "max_iterations": max_iterations,
        "tools": tools,
    }
    _atomic_write(target, _render_agent_markdown(front, agent_body))
    info = _reload_agents(runtime)
    agent = runtime.agents.get(raw_id)
    if agent is None:
        return {"ok": False, "error": f"写入后加载失败：{raw_id}", "reload": info, "errors": info["errors"]}
    logger.info("已创建自定义子代理 %s（%s）", raw_id, target)
    return {"ok": True, "agent": _agent_row(agent), "reload": info, "errors": info["errors"]}


@router.put("/api/agents/markdown-agents/{agent_id}", summary="更新自定义子代理（支持改 id 重命名）")
async def update_markdown_agent(agent_id: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    existing = runtime.agents.get(agent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"子代理不存在：{agent_id}")
    if existing.source != "custom":
        raise HTTPException(
            status_code=403,
            detail=(
                "内置子代理只读，不能直接编辑；请先另存为副本："
                f"POST /api/agents/markdown-agents/{agent_id}/duplicate"
            ),
        )

    body = await _json_body(request)
    new_id = str(body.get("id") or agent_id).strip() or agent_id
    new_filename = _safe_agent_filename(new_id)
    if new_id != agent_id and runtime.agents.get(new_id) is not None:
        raise HTTPException(status_code=409, detail=f"子代理 id 已存在：{new_id}")

    name = str(body.get("name", existing.name) or "").strip() or existing.name
    description = str(body.get("description", existing.description) or "").strip()
    if "description" in body and not description:
        raise HTTPException(status_code=422, detail="description 不能为空")
    agent_body = str(body.get("body", existing.system_prompt) or "").strip()
    if "body" in body and not agent_body:
        raise HTTPException(status_code=422, detail="body（系统提示词）不能为空")

    if "tools" in body:
        tools = _normalize_tools(body.get("tools"), runtime)
    else:
        tools = list(existing.tools) if existing.tools else None
    if "maxIterations" in body or "max_iterations" in body:
        max_iterations = _normalize_max_iterations(body.get("maxIterations", body.get("max_iterations")))
    else:
        max_iterations = existing.max_iterations

    custom_dir = _custom_agents_dir(runtime.config)
    old_path = _existing_agent_path(runtime.config, existing)
    new_path = custom_dir / new_filename
    if new_path != old_path and new_path.exists():
        raise HTTPException(status_code=409, detail=f"文件已存在：{new_filename}")

    front: dict[str, Any] = {
        "id": new_id,
        "name": name,
        "description": description,
        "max_iterations": max_iterations,
        "tools": tools,
    }
    _atomic_write(new_path, _render_agent_markdown(front, agent_body))
    if new_path != old_path:
        old_path.unlink(missing_ok=True)  # 改 id = 写新文件后删旧文件
    info = _reload_agents(runtime)
    agent = runtime.agents.get(new_id)
    if agent is None:
        return {"ok": False, "error": f"更新后加载失败：{new_id}", "reload": info, "errors": info["errors"]}
    logger.info("已更新自定义子代理 %s → %s", agent_id, new_id)
    return {
        "ok": True,
        "agent": _agent_row(agent),
        "renamedFrom": agent_id if new_id != agent_id else None,
        "reload": info,
        "errors": info["errors"],
    }


@router.post(
    "/api/agents/markdown-agents/{agent_id}/duplicate",
    summary="另存为副本（内置子代理编辑前的推荐入口）",
)
async def duplicate_markdown_agent(agent_id: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    source = runtime.agents.get(agent_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"子代理不存在：{agent_id}")

    body = await _json_body(request)
    raw_id = str(body.get("id") or "").strip() or _slugify(f"{source.id}-copy")
    filename = _safe_agent_filename(raw_id)
    if runtime.agents.get(raw_id) is not None:
        raise HTTPException(status_code=409, detail=f"子代理 id 已存在：{raw_id}")
    name = str(body.get("name") or f"{source.display_name}（副本）").strip()

    target = _custom_agents_dir(runtime.config) / filename
    if target.exists():
        raise HTTPException(status_code=409, detail=f"文件已存在：{filename}")
    front: dict[str, Any] = {
        "id": raw_id,
        "name": name,
        "description": source.description,
        "max_iterations": source.max_iterations,
        "tools": list(source.tools) if source.tools else None,
    }
    _atomic_write(target, _render_agent_markdown(front, source.system_prompt))
    info = _reload_agents(runtime)
    agent = runtime.agents.get(raw_id)
    if agent is None:
        return {"ok": False, "error": f"复制后加载失败：{raw_id}", "reload": info, "errors": info["errors"]}
    logger.info("已把子代理 %s 另存为 %s", agent_id, raw_id)
    return {
        "ok": True,
        "agent": _agent_row(agent),
        "copiedFrom": agent_id,
        "reload": info,
        "errors": info["errors"],
    }


@router.delete("/api/agents/markdown-agents/{agent_id}", summary="删除自定义子代理（需 ?confirm=true）")
async def delete_markdown_agent(
    agent_id: str,
    request: Request,
    confirm: bool = Query(default=False, description="二次确认；缺省拒绝删除"),
) -> dict[str, Any]:
    if not confirm:
        raise HTTPException(status_code=400, detail="删除必须显式携带 ?confirm=true（防误删）")
    runtime = get_runtime(request)
    agent = runtime.agents.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"子代理不存在：{agent_id}")
    if agent.source != "custom":
        raise HTTPException(status_code=403, detail="内置子代理只读，不能删除")

    path = _existing_agent_path(runtime.config, agent)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"自定义文件不存在：{path.name}")
    path.unlink()
    info = _reload_agents(runtime)
    logger.info("已删除自定义子代理 %s", agent_id)
    return {"ok": True, "removed": agent_id, "reload": info, "errors": info["errors"]}


@router.post("/api/agents/reload", summary="热重载子代理注册表")
@router.post("/api/agents/markdown-agents/reload", summary="热重载子代理注册表（别名）")
async def reload_agents(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    info = _reload_agents(runtime)
    logger.info("子代理热重载：%s", info)
    return {"ok": not info["errors"], **info}


# ---------------------------------------------------------------------------
# 工具：序列化 / 校验
# ---------------------------------------------------------------------------
def _dangerous_hits(text: str) -> list[str]:
    lowered = text.lower()
    hits: list[str] = []
    for pattern, reason in _DANGEROUS_PATTERNS:
        if pattern in lowered and reason not in hits:
            hits.append(f"{pattern}（{reason}）")
    return hits


def _check_dangerous(command: str, args: list[str], acknowledge: bool) -> list[str]:
    hits = _dangerous_hits(" ".join([command, *args]))
    if hits and not acknowledge:
        raise HTTPException(
            status_code=422,
            detail=(
                f"检测到危险命令模式：{', '.join(hits)}；"
                "确认是 CTF 靶场/授权环境请携带 acknowledge_risk=true 重试"
            ),
        )
    return hits


def _serialize_parameters(raw: Any) -> list[dict[str, Any]]:
    """把管理 API 的 parameters 列表序列化为 loader 期望的 YAML schema。"""
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="parameters 必须是数组")

    out: list[dict[str, Any]] = []
    seen_positions: set[int] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f"parameters[{index}] 必须是对象")
        name = str(item.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail=f"parameters[{index}] 缺少 name")
        raw_type = str(item.get("type") or "string").strip().lower()
        if raw_type not in _TYPE_MAP:
            raise HTTPException(
                status_code=422,
                detail=f"parameters[{index}].type 不支持：{raw_type}（string|bool|number）",
            )
        node: dict[str, Any] = {"name": name, "type": _TYPE_MAP[raw_type]}
        if item.get("required") is not None:
            node["required"] = bool(item["required"])
        if item.get("default") is not None:
            node["default"] = item["default"]
        if item.get("description"):
            node["description"] = str(item["description"])

        fmt = item.get("format")
        if fmt is None:
            node["format"] = "positional"
        elif not isinstance(fmt, dict):
            raise HTTPException(status_code=422, detail=f"parameters[{index}].format 必须是对象")
        else:
            kind = str(fmt.get("kind") or "").strip().lower()
            if kind == "positional":
                position = fmt.get("position")
                if position is not None:
                    try:
                        position_int = int(position)
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(
                            status_code=422,
                            detail=f"parameters[{index}].format.position 必须是整数",
                        ) from exc
                    if position_int in seen_positions:
                        raise HTTPException(status_code=422, detail=f"position 重复：{position_int}")
                    seen_positions.add(position_int)
                    node["position"] = position_int
                else:
                    node["format"] = "positional"
            elif kind == "flag":
                flag = str(fmt.get("flag") or "").strip()
                if not flag:
                    raise HTTPException(status_code=422, detail=f"parameters[{index}].format.flag 不能为空")
                node["flag"] = flag
            elif kind == "template":
                template = str(fmt.get("template") or "").strip()
                if not template:
                    raise HTTPException(
                        status_code=422, detail=f"parameters[{index}].format.template 不能为空"
                    )
                if "{value}" not in template:
                    raise HTTPException(
                        status_code=422,
                        detail=f"parameters[{index}].format.template 必须包含 {{value}} 占位符",
                    )
                node["template"] = template
            else:
                raise HTTPException(
                    status_code=422,
                    detail=f"parameters[{index}].format.kind 必须是 positional|flag|template",
                )
        out.append(node)
    return out


def _validate_tool_payload(body: dict[str, Any], *, default_name: str | None = None) -> dict[str, Any]:
    name = str(body.get("name") or default_name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name 不能为空")
    if not _TOOL_NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail=f"工具名不合法：{name!r}（必须字母数字开头，仅含字母数字 _ . -）",
        )
    command = str(body.get("command") or "").strip()
    if not command:
        raise HTTPException(status_code=422, detail="command 不能为空")
    short_description = str(body.get("short_description") or "").strip()
    if not short_description:
        raise HTTPException(status_code=422, detail="short_description 不能为空")

    raw_args = body.get("args")
    if raw_args in (None, ""):
        args: list[str] = []
    elif isinstance(raw_args, list):
        args = [str(a) for a in raw_args]
    else:
        raise HTTPException(status_code=422, detail="args 必须是字符串数组")

    return {
        "name": name,
        "command": command,
        "args": args,
        "short_description": short_description,
        "description": str(body.get("description") or "").strip(),
        "enabled": bool(body.get("enabled", True)),
        "parameters": _serialize_parameters(body.get("parameters")),
    }


def _tool_yaml_data(payload: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": payload["name"],
        "command": payload["command"],
        "short_description": payload["short_description"],
    }
    if payload["args"]:
        data["args"] = payload["args"]
    if payload["description"]:
        data["description"] = payload["description"]
    data["enabled"] = payload["enabled"]
    if payload["parameters"]:
        data["parameters"] = payload["parameters"]
    return data


def _recipe_to_dict(recipe: Any) -> dict[str, Any]:
    """把已加载的 :class:`ToolRecipe` 还原成 YAML dict（供克隆）。"""
    data: dict[str, Any] = {
        "name": recipe.name,
        "command": recipe.command,
        "short_description": recipe.short_description,
    }
    if recipe.args:
        data["args"] = list(recipe.args)
    if recipe.description:
        data["description"] = recipe.description
    if recipe.category and recipe.category != "generic":
        data["category"] = recipe.category
    if not recipe.enabled:
        data["enabled"] = False
    if recipe.env:
        data["env"] = dict(recipe.env)
    if recipe.interactive:
        data["interactive"] = True
    if recipe.dangerous:
        data["dangerous"] = True
    if recipe.requires_root:
        data["requires_root"] = True
    if recipe.notes:
        data["notes"] = recipe.notes
    if recipe.timeout_seconds is not None:
        data["timeout_seconds"] = recipe.timeout_seconds

    params: list[dict[str, Any]] = []
    for param in recipe.parameters:
        node: dict[str, Any] = {"name": param.name, "type": param.type}
        if param.description:
            node["description"] = param.description
        if param.required:
            node["required"] = True
        if param.default is not None:
            node["default"] = param.default
        if param.choices:
            node["choices"] = list(param.choices)
        if param.position is not None:
            node["position"] = param.position
        elif param.flag:
            node["flag"] = param.flag
        elif param.template:
            node["template"] = param.template
        elif param.positional:
            node["format"] = "positional"
        params.append(node)
    if params:
        data["parameters"] = params
    return data


def _existing_tool_path(config: Any, tool: ToolDefinition) -> Path:
    custom_dir = _custom_tools_dir(config)
    recipe = tool.metadata.get("recipe")
    recipe_path = getattr(recipe, "path", None) if recipe is not None else None
    if recipe_path:
        candidate = Path(str(recipe_path))
        try:
            if candidate.resolve().parent == custom_dir.resolve():
                return candidate
        except OSError:  # pragma: no cover - 极少见
            pass
    return custom_dir / _safe_tool_filename(tool.name)


def _tool_signature(tool: ToolDefinition) -> tuple[Any, ...]:
    return (
        tool.short_description,
        tool.description,
        tool.available,
        tool.enabled,
        tool.dangerous,
        repr(tool.parameters),
        tool.origin,
    )


def _reload_yaml_tools(runtime: Any) -> dict[str, Any]:
    """重新加载 ``tools/`` + ``tools/custom/`` 的 YAML 配方（保留 builtin/mcp 工具）。"""
    registry = runtime.registry
    before = {t.name: _tool_signature(t) for t in registry.all() if t.source == "yaml"}
    registry.remove_by_source("yaml")
    registry.clear_recipe_errors()
    tools, errors = tool_loader.load_yaml_tools(
        _tools_root(runtime.config), project_root=runtime.config.project_root
    )
    registry.register_many(tools)
    after = {t.name: _tool_signature(t) for t in registry.all() if t.source == "yaml"}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    updated = sorted(name for name in set(before) & set(after) if before[name] != after[name])
    runtime.tool_load_errors = list(errors)
    return {
        "added": len(added),
        "updated": len(updated),
        "removed": len(removed),
        "addedNames": added,
        "updatedNames": updated,
        "removedNames": removed,
        "total": len(after),
        "custom": sum(1 for t in registry.all() if t.origin == "custom"),
        "errors": errors,
    }


def _error_for_tool(runtime: Any, name: str, errors: list[str]) -> str:
    tool = runtime.registry.get(name)
    if tool is not None:
        return ""
    for broken in runtime.registry.recipe_errors:
        if broken.name == name:
            return str(broken.metadata.get("error") or "配方解析失败")
    return next((message for message in errors if name in message), "")


# ---------------------------------------------------------------------------
# 工具路由
# ---------------------------------------------------------------------------
@router.post("/api/tools", summary="创建自定义 YAML 工具")
async def create_tool(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    body = await _json_body(request)
    payload = _validate_tool_payload(body)
    if runtime.registry.get(payload["name"]) is not None:
        raise HTTPException(status_code=409, detail=f"工具已存在：{payload['name']}")
    _check_dangerous(payload["command"], payload["args"], bool(body.get("acknowledge_risk")))

    target = _custom_tools_dir(runtime.config) / _safe_tool_filename(payload["name"])
    if target.exists():
        raise HTTPException(status_code=409, detail=f"文件已存在：{target.name}")
    text = yaml.safe_dump(_tool_yaml_data(payload), allow_unicode=True, sort_keys=False)
    _atomic_write(target, text)

    info = _reload_yaml_tools(runtime)
    error = _error_for_tool(runtime, payload["name"], info["errors"])
    tool = runtime.registry.get(payload["name"])
    logger.info("已创建自定义工具 %s（%s）", payload["name"], target)
    return {
        "ok": tool is not None and not error,
        "tool": runtime.registry.public_entry(tool) if tool else None,
        "error": error,
        "reload": info,
        "errors": info["errors"],
    }


@router.put("/api/tools/{name}", summary="更新自定义 YAML 工具")
async def update_tool(name: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    existing = runtime.registry.get(name)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"工具不存在：{name}")
    if existing.origin != "custom":
        raise HTTPException(
            status_code=403,
            detail=(
                "内置工具只读，不能直接编辑；请先克隆到 tools/custom/："
                f'POST /api/tools/copy  {{"name": "{name}"}}'
            ),
        )

    body = await _json_body(request)
    payload = _validate_tool_payload(body, default_name=name)
    if payload["name"] != name:
        raise HTTPException(
            status_code=422,
            detail=f"不支持通过 PUT 改名（路径 name={name}，请求体 name={payload['name']}）；请删除后重建",
        )
    _check_dangerous(payload["command"], payload["args"], bool(body.get("acknowledge_risk")))

    target = _existing_tool_path(runtime.config, existing)
    text = yaml.safe_dump(_tool_yaml_data(payload), allow_unicode=True, sort_keys=False)
    _atomic_write(target, text)

    info = _reload_yaml_tools(runtime)
    error = _error_for_tool(runtime, name, info["errors"])
    tool = runtime.registry.get(name)
    logger.info("已更新自定义工具 %s（%s）", name, target)
    return {
        "ok": tool is not None and not error,
        "tool": runtime.registry.public_entry(tool) if tool else None,
        "error": error,
        "reload": info,
        "errors": info["errors"],
    }


@router.post("/api/tools/copy", summary="克隆工具到 tools/custom/（编辑内置工具的第一步）")
async def copy_tool(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    body = await _json_body(request)
    source_name = str(body.get("name") or "").strip()
    source = runtime.registry.get(source_name)
    if source is None:
        raise HTTPException(status_code=404, detail=f"工具不存在：{source_name}")
    recipe = source.metadata.get("recipe")
    if recipe is None:
        raise HTTPException(
            status_code=422, detail=f"{source_name} 不是 YAML 配方工具，无法序列化为 custom 文件"
        )

    new_name = str(body.get("new_name") or body.get("newName") or "").strip() or f"{source_name}-copy"
    if not _TOOL_NAME_RE.match(new_name):
        raise HTTPException(status_code=422, detail=f"工具名不合法：{new_name!r}")
    if runtime.registry.get(new_name) is not None:
        raise HTTPException(status_code=409, detail=f"工具已存在：{new_name}")

    target = _custom_tools_dir(runtime.config) / _safe_tool_filename(new_name)
    if target.exists():
        raise HTTPException(status_code=409, detail=f"文件已存在：{target.name}")
    data = _recipe_to_dict(recipe)
    data["name"] = new_name
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    _atomic_write(target, text)

    info = _reload_yaml_tools(runtime)
    error = _error_for_tool(runtime, new_name, info["errors"])
    tool = runtime.registry.get(new_name)
    logger.info("已克隆工具 %s → %s", source_name, new_name)
    return {
        "ok": tool is not None and not error,
        "tool": runtime.registry.public_entry(tool) if tool else None,
        "copiedFrom": source_name,
        "error": error,
        "reload": info,
        "errors": info["errors"],
    }


@router.delete("/api/tools/{name}", summary="删除自定义 YAML 工具（需 ?confirm=true）")
async def delete_tool(
    name: str,
    request: Request,
    confirm: bool = Query(default=False, description="二次确认；缺省拒绝删除"),
) -> dict[str, Any]:
    if not confirm:
        raise HTTPException(status_code=400, detail="删除必须显式携带 ?confirm=true（防误删）")
    runtime = get_runtime(request)
    tool = runtime.registry.get(name)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"工具不存在：{name}")
    if tool.origin != "custom":
        raise HTTPException(status_code=403, detail="内置工具只读，不能删除")

    path = _existing_tool_path(runtime.config, tool)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"自定义文件不存在：{path.name}")
    path.unlink()
    info = _reload_yaml_tools(runtime)
    logger.info("已删除自定义工具 %s", name)
    return {"ok": True, "removed": name, "reload": info, "errors": info["errors"]}


@router.post("/api/tools/reload", summary="热重载 YAML 工具注册表")
async def reload_tools(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    info = _reload_yaml_tools(runtime)
    logger.info("工具热重载：added=%s updated=%s removed=%s", info["added"], info["updated"], info["removed"])
    return {"ok": not info["errors"], **info}


__all__ = ["router"]
