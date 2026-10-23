"""YAML 工具配方加载器（与 CyberStrikeAI 配方格式兼容）。

一个配方形如::

    name: "nmap"
    command: "nmap"
    args: ["-sT", "-sV"]
    short_description: "..."
    description: |
      ...
    parameters:
      - name: "target"
        type: "string"
        position: 1
      - name: "ports"
        flag: "-p"
      - name: "timing"
        template: "-T{value}"
      - name: "additional_args"
        format: "positional"

参数支持三种传参格式：``flag``（``-p {value}``）、``position``（按序放在基础参数之后）、
``template``（``{value}`` 替换）以及 ``format: positional``（追加到命令末尾）。

配方中的 ``{{ROOT}}`` / ``{{WORKSPACE}}`` 会在执行时替换为项目根目录与题目工作区绝对路径。
新增工具零代码：把 YAML 丢进 ``tools/`` 目录即可。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .registry import EMPTY_SCHEMA, ToolDefinition

logger = logging.getLogger(__name__)

#: 参数类型 → JSON Schema 类型
_JSON_TYPES = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}


class RecipeError(ValueError):
    """配方格式错误。"""


@dataclass
class RecipeParameter:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    default: Any = None
    position: int | None = None
    flag: str | None = None
    template: str | None = None
    positional: bool = False
    choices: list[Any] = field(default_factory=list)

    def schema(self) -> dict[str, Any]:
        node: dict[str, Any] = {
            "type": _JSON_TYPES.get(self.type, "string"),
            "description": self.description or self.name,
        }
        if self.choices:
            node["enum"] = self.choices
        if self.default is not None:
            node["default"] = self.default
        return node


@dataclass
class ToolRecipe:
    """一份 YAML 工具配方。"""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    parameters: list[RecipeParameter] = field(default_factory=list)
    short_description: str = ""
    description: str = ""
    category: str = "generic"
    enabled: bool = True
    env: dict[str, str] = field(default_factory=dict)
    interactive: bool = False
    dangerous: bool = False
    requires_root: bool = False
    notes: str = ""
    #: 覆盖全局 ``agent.tool_timeout_seconds`` 的硬超时（长耗时工具如 gdb/volatility 可调大）
    timeout_seconds: int | None = None
    path: Path | None = None

    # -- 校验/转换 --------------------------------------------------------
    def validate(self) -> None:
        if not self.name:
            raise RecipeError(f"{self.path}: 缺少 name")
        if not self.command:
            raise RecipeError(f"{self.path}: 缺少 command")
        seen_positions: set[int] = set()
        for param in self.parameters:
            if not param.name:
                raise RecipeError(f"{self.path}: 存在无 name 的参数")
            modes = [bool(param.flag), bool(param.template), param.position is not None, param.positional]
            if sum(1 for m in modes if m) == 0:
                # 允许"仅出现在描述里"的参数，默认按 template 处理
                param.positional = True
            if param.position is not None:
                if param.position in seen_positions:
                    raise RecipeError(f"{self.path}: 参数 {param.name} 的 position 重复")
                seen_positions.add(param.position)

    def json_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in self.parameters:
            properties[param.name] = param.schema()
            if param.required:
                required.append(param.name)
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def to_index_text(self) -> str:
        return self.short_description or self.description[:120] or self.name


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_recipe(data: dict[str, Any], *, path: Path | None = None) -> ToolRecipe:
    """把 YAML dict 解析为 :class:`ToolRecipe`。"""
    if not isinstance(data, dict):
        raise RecipeError(f"{path}: 顶层必须是映射")

    raw_params = data.get("parameters") or []
    if not isinstance(raw_params, list):
        raise RecipeError(f"{path}: parameters 必须是列表")

    params: list[RecipeParameter] = []
    for item in raw_params:
        if not isinstance(item, dict):
            raise RecipeError(f"{path}: parameters 元素必须是映射")
        params.append(
            RecipeParameter(
                name=str(item.get("name", "")),
                type=str(item.get("type", "string")),
                description=str(item.get("description", "")),
                required=bool(item.get("required", False)),
                default=item.get("default"),
                position=int(item["position"]) if item.get("position") is not None else None,
                flag=str(item["flag"]) if item.get("flag") else None,
                template=str(item["template"]) if item.get("template") else None,
                positional=str(item.get("format", "")).lower() == "positional",
                choices=list(item.get("choices") or []),
            )
        )

    recipe = ToolRecipe(
        name=str(data.get("name", "")).strip(),
        command=str(data.get("command", "")).strip(),
        args=[str(a) for a in (data.get("args") or [])],
        parameters=params,
        short_description=str(data.get("short_description", "")).strip(),
        description=str(data.get("description", "")).strip(),
        category=str(data.get("category", "generic")).strip() or "generic",
        enabled=bool(data.get("enabled", True)),
        env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
        interactive=bool(data.get("interactive", False)),
        dangerous=bool(data.get("dangerous", False)),
        requires_root=bool(data.get("requires_root", False)),
        notes=str(data.get("notes", "")).strip(),
        timeout_seconds=int(data["timeout_seconds"]) if data.get("timeout_seconds") is not None else None,
        path=path,
    )
    recipe.validate()
    return recipe


def load_recipe_file(path: Path) -> ToolRecipe:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return parse_recipe(data, path=path)


# ---------------------------------------------------------------------------
# 可用性检测
# ---------------------------------------------------------------------------
def check_availability(recipe: ToolRecipe, *, project_root: Path) -> tuple[bool, str]:
    """检测配方命令是否可用；返回 ``(available, reason)``。"""
    command = recipe.command
    if not command:
        return False, "配方未指定 command"
    # 项目内脚本（python3 {{ROOT}}/tools/scripts/x.py）→ 检查脚本文件是否存在
    for arg in recipe.args:
        if "tools/scripts/" in str(arg):
            script = Path(str(arg).replace("{{ROOT}}", str(project_root)))
            if not script.is_file():
                return False, f"脚本缺失：{script}"
    resolved = shutil.which(command)
    if resolved is None and not Path(command).is_absolute():
        # 绝对路径命令（极少见）单独判断
        return False, f"命令未安装：{command}"
    if Path(command).is_absolute() and not Path(command).exists():
        return False, f"命令不存在：{command}"
    return True, ""


# ---------------------------------------------------------------------------
# 命令展开
# ---------------------------------------------------------------------------
def _substitute(text: str, *, project_root: Path, workspace: Path) -> str:
    return str(text).replace("{{ROOT}}", str(project_root)).replace("{{WORKSPACE}}", str(workspace))


def _coerce_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def build_argv(
    recipe: ToolRecipe,
    arguments: dict[str, Any],
    *,
    project_root: Path,
    workspace: Path,
) -> list[str]:
    """把配方 + 模型参数展开成 argv。

    顺序：``command`` → 基础 ``args`` → positional（按 position 升序）→ flag/template/追加参数（声明顺序）。
    """
    argv: list[str] = [_substitute(recipe.command, project_root=project_root, workspace=workspace)]
    argv += [_substitute(a, project_root=project_root, workspace=workspace) for a in recipe.args]

    positional_slots: dict[int, str] = {}
    trailing: list[str] = []

    for param in recipe.parameters:
        value = arguments.get(param.name, None)
        if value is None or value == "":
            value = param.default
        if param.type == "boolean":
            truthy = _coerce_boolean(value) if value is not None else False
            if not truthy:
                continue
            if param.flag:
                trailing.append(_substitute(param.flag, project_root=project_root, workspace=workspace))
            elif param.template:
                trailing.append(
                    _substitute(
                        param.template.replace("{value}", "true"),
                        project_root=project_root,
                        workspace=workspace,
                    )
                )
            elif param.position is not None:
                positional_slots[param.position] = "true"
            else:
                trailing.append("true")
            continue

        if value is None or value == "":
            continue
        text = str(value)
        if param.position is not None:
            positional_slots[param.position] = _substitute(
                text, project_root=project_root, workspace=workspace
            )
        elif param.flag:
            trailing.append(_substitute(param.flag, project_root=project_root, workspace=workspace))
            trailing.append(_substitute(text, project_root=project_root, workspace=workspace))
        elif param.template:
            trailing.append(
                _substitute(
                    param.template.replace("{value}", text), project_root=project_root, workspace=workspace
                )
            )
        else:  # format: positional（含缺省追加）
            trailing.append(_substitute(text, project_root=project_root, workspace=workspace))

    for position in sorted(positional_slots):
        argv.append(positional_slots[position])
    argv.extend(trailing)
    return argv


def make_tool_definition(
    recipe: ToolRecipe, *, project_root: Path, origin: str = "builtin"
) -> ToolDefinition:
    """配方 → :class:`ToolDefinition`（``source='yaml'``，handler 由 ExecutionService 展开）。"""
    available, reason = check_availability(recipe, project_root=project_root)
    description = recipe.description
    extras: list[str] = []
    if recipe.requires_root:
        extras.append("需要 root 权限（当前可能失败，可加 sudo 或改用等价非特权选项）。")
    if recipe.interactive:
        extras.append("该工具倾向于交互式运行，请使用非交互参数避免挂死。")
    if recipe.notes:
        extras.append(recipe.notes)
    if not available:
        extras.append(f"当前环境不可用：{reason}。可改用 bash / python_exec 实现同等效果。")
    if extras:
        description = description + "\n\n注意：" + " ".join(extras)

    return ToolDefinition(
        name=recipe.name,
        description=description,
        short_description=recipe.short_description or recipe.name,
        parameters=recipe.json_schema(),
        handler=None,
        source="yaml",
        origin=origin,
        category=recipe.category,
        available=available,
        enabled=recipe.enabled,
        dangerous=recipe.dangerous,
        interactive=recipe.interactive,
        unavailable_reason=reason,
        metadata={"recipe": recipe, "requires_root": recipe.requires_root},
    )


def custom_tool_dir(directory: str | Path) -> Path:
    """返回内置工具目录对应的 ``custom/`` 子目录（测试可 monkeypatch 到 tmp_path）。

    内置目录 ``tools/`` 只读；用户新增/编辑的配方一律写入 ``tools/custom/``。
    """
    return Path(directory) / "custom"


def _yaml_error_message(path: Path, exc: yaml.YAMLError) -> str:
    """带行号的 YAML 解析错误（有 ``problem_mark`` 时给出 1-based 行/列）。"""
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        return f"{path}: YAML 解析失败（第 {mark.line + 1} 行，第 {mark.column + 1} 列）：{exc}"
    return f"{path}: YAML 解析失败：{exc}"


def _broken_recipe_placeholder(path: Path, message: str, *, origin: str) -> ToolDefinition:
    """解析失败的配方占位：不进入可调用工具面，但让公开列表能看到文件名与错误。"""
    return ToolDefinition(
        name=path.stem,
        short_description=f"[配方加载失败] {path.name}",
        description=f"该 YAML 配方无法解析：{message}",
        parameters=dict(EMPTY_SCHEMA),
        handler=None,
        source="yaml",
        origin=origin,
        category="load_error",
        available=False,
        enabled=False,
        metadata={"broken": True, "error": message, "path": str(path)},
    )


def _load_yaml_dir(
    directory: Path,
    *,
    project_root: Path,
    recursive: bool,
    origin: str,
    errors: list[str],
) -> list[ToolDefinition]:
    pattern = "**/*.yaml" if recursive else "*.yaml"
    tools: list[ToolDefinition] = []
    for path in sorted(directory.glob(pattern)):
        if path.name.startswith("_"):
            continue
        try:
            recipe = load_recipe_file(path)
            tools.append(make_tool_definition(recipe, project_root=project_root, origin=origin))
        except RecipeError as exc:
            message = str(exc)
            errors.append(message)
            logger.warning("跳过非法配方 %s：%s", path, exc)
            tools.append(_broken_recipe_placeholder(path, message, origin=origin))
        except yaml.YAMLError as exc:
            message = _yaml_error_message(path, exc)
            errors.append(message)
            logger.warning("跳过非法配方 %s：%s", path, exc)
            tools.append(_broken_recipe_placeholder(path, message, origin=origin))
        except Exception as exc:  # noqa: BLE001 - 单个配方失败不影响整体
            message = f"{path}: {type(exc).__name__}: {exc}"
            errors.append(message)
            tools.append(_broken_recipe_placeholder(path, message, origin=origin))
    return tools


def load_yaml_tools(
    directory: str | Path,
    *,
    project_root: Path,
    recursive: bool = False,
) -> tuple[list[ToolDefinition], list[str]]:
    """加载目录下全部 ``*.yaml`` 配方（内置目录 + 其 ``custom/`` 子目录）。

    返回 ``(工具定义列表, 错误信息列表)``；单个配方出错不会影响其余配方（软失败）。
    解析失败的文件会以占位定义（``metadata['broken']``）返回，保证工具管理板块能看到
    「哪个文件坏了、坏在哪一行」，而不是静默消失。``custom/`` 缺失时静默跳过。
    """
    directory = Path(directory)
    errors: list[str] = []
    tools: list[ToolDefinition] = []
    if not directory.is_dir():
        return tools, [f"工具目录不存在：{directory}"]
    tools.extend(
        _load_yaml_dir(
            directory, project_root=project_root, recursive=recursive, origin="builtin", errors=errors
        )
    )
    custom = custom_tool_dir(directory)
    if custom.is_dir():
        tools.extend(
            _load_yaml_dir(custom, project_root=project_root, recursive=False, origin="custom", errors=errors)
        )
    return tools, errors


__all__ = [
    "RecipeError",
    "RecipeParameter",
    "ToolRecipe",
    "build_argv",
    "check_availability",
    "custom_tool_dir",
    "load_recipe_file",
    "load_yaml_tools",
    "make_tool_definition",
    "parse_recipe",
]
