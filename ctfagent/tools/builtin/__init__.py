"""内置工具注册入口。

把全部代码实现的工具注册到 :class:`~ctfagent.tools.registry.ToolRegistry`：
文件系统、命令执行、黑板、题目元数据、flag、报告、视觉、计划、tool_search。

新增内置工具的步骤：在本目录写 ``async def handler(args, ctx) -> ToolOutcome``，
然后在 :func:`register_builtins` 里加一条 :class:`ToolDefinition` 即可。
"""

from __future__ import annotations

from typing import Any

from ...config import AppConfig
from ...models.types import ToolOutcome
from ..registry import ToolDefinition, ToolRegistry
from . import blackboard, challenge, flags, fs, idea_tools, platform, report, shell, todos, vision


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _s(description: str, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "string", "description": description}
    node.update(extra)
    return node


def _i(description: str, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "integer", "description": description}
    node.update(extra)
    return node


def _b(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


# ---------------------------------------------------------------------------
# tool_search（依赖 registry，单独构造）
# ---------------------------------------------------------------------------
def make_tool_search(registry: ToolRegistry) -> ToolDefinition:
    async def _search(args: dict[str, Any], ctx: Any) -> ToolOutcome:
        query = str(args.get("query") or "").strip()
        limit = int(args.get("limit") or 15)
        if not query:
            return ToolOutcome.ok(
                "请提供 query（如 'sql 注入'、'pwn'、'stego'、'pcap'）。当前工具索引：\n\n"
                + ctx.registry.index_text()
            )
        results = ctx.registry.search(query, limit=limit)
        if not results:
            return ToolOutcome.ok(
                f"没有匹配「{query}」的工具。可用工具索引如下（可换个关键词再搜）：\n\n"
                + ctx.registry.index_text()
            )
        lines: list[str] = []
        for tool in results:
            required = tool.parameters.get("required") or []
            params = ", ".join(required) if required else "无必填参数"
            status = "" if tool.available else f"（当前环境不可用：{tool.unavailable_reason}）"
            lines.append(
                f"- **{tool.name}**{status}\n  说明：{tool.short_description or tool.description[:100]}"
                f"\n  必填参数：{params}"
            )
        unlocked = [t.name for t in results]
        return ToolOutcome.ok(
            f"已解锁 {len(results)} 个工具：{', '.join(unlocked)}\n"
            "（本次调用后它们的完整 JSON Schema 已加入你可用的工具列表，可直接调用）\n\n" + "\n".join(lines)
        )

    return ToolDefinition(
        name="tool_search",
        short_description="按关键词检索并解锁工具（如 sql/pwn/stego/pcap/binary）",
        description=(
            "当你不确定有哪些工具可用、或需要某类能力（注入、逆向、隐写、取证、编码）时先调用本工具。"
            "命中后对应工具的完整参数 schema 会加入你的工具列表。空 query 会返回全部工具索引。"
        ),
        parameters=_schema(
            {
                "query": _s("能力关键词，可用空格分隔多个词；留空则列出全部工具"),
                "limit": _i("最多返回条数，默认 15"),
            },
            ["query"],
        ),
        handler=_search,
        category="meta",
        always_expose=True,
    )


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
def _ledger_tool_definitions() -> list[ToolDefinition]:
    """v2 挑战级账本工具（Idea / Memory）。

    仅在 ``state_v2.enabled`` 时注册：关闭该开关后工具平面与 v1 **完全一致**
    （``/api/tools`` 计数、系统提示词里的工具索引都不变）。
    """
    return [
        ToolDefinition(
            name="idea_upsert",
            short_description="声明一个方向假设（挑战级账本；同文本去重）",
            description=(
                "Idea = 「接下来该往哪走」的可验证假设，是系统调度的依据。"
                "text 必须一句写清「假设什么 + 如何验证」，例如「/login 的 u 参数存在字符串拼接注入」。"
                "只声明方向，不要在这里写结论（结论用 memory_write）。"
            ),
            parameters=_schema(
                {
                    "text": _s("一句可验证的假设/方向（必填）"),
                    "idea_id": _s("可选：指定已有 idea id 做幂等确认"),
                },
                ["text"],
            ),
            handler=idea_tools.idea_upsert,
            category="blackboard",
            always_expose=True,
        ),
        ToolDefinition(
            name="idea_transition",
            short_description="推进 Idea 状态（verified/failed 会自动写 fact/failure Memory）",
            description=(
                "状态机：pending → testing → verified|failed|skipped（终态不可再转）。"
                "转 testing 需先认领（Dispatcher 派发时自动认领）。"
                "转 verified/failed 必须给 evidence_summary，系统会自动联动写入对应的 fact/failure Memory。"
                "任务结束前必须收尾你认领的 Idea。"
            ),
            parameters=_schema(
                {
                    "idea_id": _s("Idea id"),
                    "status": _s("目标状态", enum=["testing", "verified", "failed", "skipped"]),
                    "evidence_summary": _s(
                        "证据/说明：verified 写如何验证成功；failed 写已排除的路径（必填）"
                    ),
                },
                ["idea_id", "status"],
            ),
            handler=idea_tools.idea_transition,
            category="blackboard",
            always_expose=True,
        ),
        ToolDefinition(
            name="memory_write",
            short_description="追加一条挑战级事实记录（fact/evidence/failure/note）",
            description=(
                "Memory 是追加式的「已发生/已确认」，不可改写历史条目。"
                "kind=fact 确认事实；evidence 证据要点；failure 此路不通（防重复踩坑）；note 观察线索。"
                "hint 属于人类提示，模型无权写入。summary 一行写清「什么+在哪+如何验证」，细节放 body。"
            ),
            parameters=_schema(
                {
                    "kind": _s("类型", enum=["fact", "evidence", "failure", "note"]),
                    "summary": _s("一行摘要：什么 + 在哪 + 如何验证"),
                    "body": _s("完整细节（不占上下文，需要时 blackboard_query 取回）"),
                },
                ["kind", "summary"],
            ),
            handler=idea_tools.memory_write,
            category="blackboard",
            always_expose=True,
        ),
        ToolDefinition(
            name="blackboard_query",
            short_description="结构化查询挑战账本（Idea + Memory，可按 kind/status 过滤）",
            description=(
                "查看当前挑战的全部 Idea 与 Memory。开工前用它对齐现状（避免重复认领/重复踩坑），"
                "需要某条 Memory 的完整细节时加 include_body=true。"
                "传入 project_id 时改为跨该项目全部会话检索（项目维度黑板）。"
            ),
            parameters=_schema(
                {
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "按 Memory 类型过滤，如 [failure, hint]",
                    },
                    "status": _s(
                        "按 Idea 状态过滤", enum=["pending", "testing", "verified", "failed", "skipped"]
                    ),
                    "limit": _i("最多返回条数，默认 20"),
                    "include_body": _b("是否返回 body 全文（默认 false）"),
                    "project_id": _s("可选：按项目（比赛）维度跨会话检索；缺省时按当前会话所属项目自动判定"),
                },
            ),
            handler=idea_tools.blackboard_query,
            category="blackboard",
            always_expose=True,
        ),
    ]


def register_builtins(registry: ToolRegistry, config: AppConfig | None = None) -> list[ToolDefinition]:
    """注册全部内置工具，返回新增的工具定义列表。"""
    tools: list[ToolDefinition] = [
        # ---------------- 文件系统 ----------------
        ToolDefinition(
            name="read_file",
            short_description="读取文件（支持 offset/limit 分段，默认最多 400 行）",
            description=(
                "读取工作区或项目内的文本文件并带行号返回。大文件请用 offset/limit 分段读取；"
                "二进制文件请先用 file/strings/xxd 处理。"
            ),
            parameters=_schema(
                {
                    "file": _s("文件路径：相对当前题目工作区，或以 workspace/ 开头的相对路径"),
                    "offset": _i("起始行号（从 1 开始），默认 1"),
                    "limit": _i("最多读取行数，默认 400，最大 5000"),
                },
                ["file"],
            ),
            handler=fs.read_file,
            category="fs",
        ),
        ToolDefinition(
            name="write_file",
            short_description="写入文件到题目工作区（覆盖/追加/仅新建）",
            description=(
                "把内容写入 workspace/challenge/<题目>/ 下的文件（禁止写项目源码）。"
                "写脚本、payload、笔记时使用，便于后续复现。"
            ),
            parameters=_schema(
                {
                    "file": _s("目标文件路径（相对题目工作区）"),
                    "content": _s("文件内容"),
                    "mode": _s("写入模式", enum=["overwrite", "append", "create_new"]),
                },
                ["file", "content"],
            ),
            handler=fs.write_file,
            category="fs",
        ),
        ToolDefinition(
            name="list_dir",
            short_description="列出目录内容（含文件大小，可递归）",
            description="列出目录下的文件与子目录。附件鉴别前先用它确认文件清单。",
            parameters=_schema(
                {
                    "path": _s("目录路径，默认当前题目工作区"),
                    "recursive": _b("是否递归列出（默认 false）"),
                    "max_entries": _i("最多返回条数，默认 500"),
                }
            ),
            handler=fs.list_dir,
            category="fs",
        ),
        ToolDefinition(
            name="glob",
            short_description="按通配符查找文件（如 **/*.py、*.zip）",
            description="在目录下按 glob 模式查找文件，返回相对路径与大小。",
            parameters=_schema(
                {
                    "pattern": _s("glob 模式，如 **/*.png 或 *.zip"),
                    "path": _s("搜索根目录，默认当前题目工作区"),
                    "max_results": _i("最多返回条数，默认 200"),
                },
                ["pattern"],
            ),
            handler=fs.glob_files,
            category="fs",
        ),
        ToolDefinition(
            name="grep",
            short_description="在文件内容中做正则检索（返回 文件:行号:内容）",
            description="对目录内文本文件做正则搜索；适合在大批附件中定位关键字、密钥、URL、flag 片段。",
            parameters=_schema(
                {
                    "pattern": _s("正则表达式"),
                    "path": _s("搜索路径，默认当前题目工作区"),
                    "include": _s("文件名过滤 glob，如 *.py、*.txt"),
                    "ignore_case": _b("忽略大小写"),
                    "max_results": _i("最多命中条数，默认 120"),
                },
                ["pattern"],
            ),
            handler=fs.grep_files,
            category="fs",
        ),
        ToolDefinition(
            name="download",
            short_description="下载 URL 到题目工作区（题面附件/远程资源）",
            description=(
                "用 HTTP(S) 下载文件到工作区。仅用于题目给定的资源地址；"
                "下载后请用 file/exiftool/binwalk 鉴别类型。"
            ),
            parameters=_schema(
                {
                    "url": _s("http/https URL"),
                    "filename": _s("保存文件名（默认取 URL 末段）"),
                    "max_bytes": _i("最大下载字节数，默认 200MB"),
                    "timeout_seconds": _i("超时秒数，默认 120"),
                },
                ["url"],
            ),
            handler=fs.download_file,
            category="fs",
        ),
        # ---------------- 命令执行 ----------------
        ToolDefinition(
            name="bash",
            short_description="执行 shell 命令（会走 HITL 审批，支持超时与后台续等）",
            description=(
                "执行任意 bash 命令，stdout/stderr 合并返回。高危命令（rm -rf、mkfs、dd of=/dev/* 等）"
                "会强制人工确认。请务必使用非交互参数避免挂死；长时间命令超时后会返回 execution_id，"
                "可用 wait_tool_execution 续等。"
            ),
            parameters=_schema(
                {
                    "command": _s("要执行的命令"),
                    "cwd": _s("工作目录（相对题目工作区）"),
                    "timeout_seconds": _i("本次命令超时（秒），默认取配置的 tool_timeout_seconds"),
                    "env": {"type": "object", "description": "附加环境变量（键值对）"},
                },
                ["command"],
            ),
            handler=shell.bash,
            category="generic",
            dangerous=True,
        ),
        ToolDefinition(
            name="bash_readonly",
            short_description="执行只读命令（白名单免审：ls/cat/grep/strings/xxd/readelf 等）",
            description=(
                "仅允许只读命令前缀，避免触发审批。命令不在白名单时返回软失败，"
                "此时改用 bash（会走 HITL 审批）。"
            ),
            parameters=_schema(
                {
                    "command": _s("只读命令，如 `file ./a.bin`、`strings -n 8 ./a.bin | head -50`"),
                    "cwd": _s("工作目录（相对题目工作区）"),
                    "timeout_seconds": _i("超时秒数"),
                },
                ["command"],
            ),
            handler=shell.bash_readonly,
            category="generic",
        ),
        ToolDefinition(
            name="python_exec",
            short_description="执行 Python 代码片段或脚本（推荐用于 exploit/pwntools/加解密）",
            description=(
                "代码片段会落盘到 workspace/_scripts/ 后执行（便于复现），也可直接指定已有脚本路径。"
                "需要交互的进程请用 pwntools 的 process()/remote() 写法，不要用裸命令。"
            ),
            parameters=_schema(
                {
                    "code": _s("Python 代码片段（与 script 二选一）"),
                    "script": _s("已存在的脚本路径（与 code 二选一）"),
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "传给脚本的命令行参数",
                    },
                    "stdin": _s("要喂给脚本的标准输入内容"),
                }
            ),
            handler=shell.python_exec,
            category="generic",
        ),
        # ---------------- 黑板 ----------------
        ToolDefinition(
            name="blackboard_upsert",
            short_description="写入/更新黑板事实（summary 一行 + body 详情），立即落盘",
            description=(
                "每确认一条新认知（开放端口、服务版本、加密参数、关键偏移、可行/不可行的攻击路径、"
                "flag 片段）就立即调用本工具。summary 写「什么+在哪+如何验证」，body 写完整细节与证据。"
                "先落黑板再继续下一步。"
            ),
            parameters=_schema(
                {
                    "key": _s("稳定短键，如 web.sqli.param、pwn.libc.version、crypto.rsa.n"),
                    "summary": _s("一行摘要：什么 + 在哪 + 如何验证"),
                    "body": _s("完整细节：命令、关键输出片段、推断依据"),
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签，如 [web, sqli, confirmed]",
                    },
                },
                ["key"],
            ),
            handler=blackboard.blackboard_upsert,
            category="blackboard",
            always_expose=True,
        ),
        ToolDefinition(
            name="blackboard_get",
            short_description="按 key 取回黑板条目的完整 body",
            description="系统提示词只注入 key+summary 索引；需要细节时用本工具按 key 取回完整内容。",
            parameters=_schema({"key": _s("黑板条目的 key")}, ["key"]),
            handler=blackboard.blackboard_get,
            category="blackboard",
            always_expose=True,
        ),
        ToolDefinition(
            name="blackboard_list",
            short_description="列出黑板全部条目的 key 与 summary",
            description="查看已确认事实索引，避免重复劳动或漏掉线索。",
            parameters=_schema({"limit": _i("最多返回条数，默认 60")}),
            handler=blackboard.blackboard_list,
            category="blackboard",
            always_expose=True,
        ),
        ToolDefinition(
            name="blackboard_search",
            short_description="在黑板的 key/summary/body/tags 中全文检索",
            description="需要回忆某条线索（如「哪个端口」「上次试过什么 payload」）时使用。",
            parameters=_schema({"query": _s("检索关键词"), "limit": _i("最多返回条数")}, ["query"]),
            handler=blackboard.blackboard_search,
            category="blackboard",
            always_expose=True,
        ),
        # ---------------- 题目 / flag / 报告 ----------------
        ToolDefinition(
            name="challenge_info",
            short_description="查看/登记题目元数据与附件清单（目标范围校验入口）",
            description=(
                "开始解题前先用本工具确认题目目标与附件。若目标缺失，工具会返回「缺失信息清单」，"
                "此时必须向用户澄清，禁止臆造目标。action=set 可登记题目（name/category/target/flag_pattern）。"
            ),
            parameters=_schema(
                {
                    "action": _s("动作", enum=["get", "list", "set", "attachments"]),
                    "challenge_id": _s("题目 ID"),
                    "name": _s("题目名（action=set）"),
                    "category": _s("分类：web/pwn/crypto/reverse/forensics/misc"),
                    "description": _s("题目描述全文（action=set）"),
                    "target": _s("靶机 IP:Port 或 URL（action=set）"),
                    "points": _i("分值"),
                    "flag_pattern": _s("flag 正则，如 flag\\{[^}]+\\}"),
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "附件路径列表",
                    },
                },
                ["action"],
            ),
            handler=challenge.challenge_info,
            category="challenge",
            always_expose=True,
        ),
        ToolDefinition(
            name="flag_submit",
            short_description="提交并校验疑似 flag（记录 + 格式校验 + 提醒证据链）",
            description=(
                "发现疑似 flag 立即调用。工具会按题目 flag 模式校验格式、记录到数据库并返回后续动作提醒。"
                "格式不符时仍会记录并给出修正建议。"
            ),
            parameters=_schema(
                {
                    "flag": _s("flag 原文（不要自行增删花括号）"),
                    "note": _s("来源与验证方式备注"),
                    "source": _s("来源：agent / user"),
                },
                ["flag"],
            ),
            handler=flags.flag_submit,
            category="challenge",
            always_expose=True,
        ),
        ToolDefinition(
            name="report_generate",
            short_description="汇总黑板+执行记录+flag 生成 writeup（Markdown，可落盘）",
            description=(
                "解题结束时调用：生成结构化 writeup（题目信息/解题思路/关键步骤/"
                "已排除路径/关键命令与输出/flag/复盘），并写入题目工作区 writeup.md。"
                "只收录结论性黑板事实与成功的关键命令；失败路径、计划、内部调用"
                "会自动归入「已排除路径」或剔除，保证 WP 是可读的复现文档而非过程日志。"
            ),
            parameters=_schema(
                {
                    "title": _s("报告标题，默认取题目名"),
                    "include_executions": _b("是否附上成功的关键命令与输出，默认 true"),
                    "write_to_file": _b("是否写入 writeup.md，默认 true"),
                }
            ),
            handler=report.report_generate,
            category="challenge",
            always_expose=True,
        ),
        ToolDefinition(
            name="write_todos",
            short_description="写入/更新解题计划（前端显示 todo 进度条）",
            description=(
                "接到题目先写 3-6 条计划（理题/侦察/分析/利用/收 flag/写报告）。"
                "每完成一步更新状态：pending / in_progress / completed。"
            ),
            parameters=_schema(
                {
                    "todos": {
                        "type": "array",
                        "description": "计划项数组，元素为字符串或 {content, status} 对象",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                            },
                        },
                    }
                },
                ["todos"],
            ),
            handler=todos.write_todos,
            category="planning",
            always_expose=True,
        ),
        # ---------------- 视觉 ----------------
        ToolDefinition(
            name="analyze_image",
            short_description="视觉旁路：把图片交给 VL 模型输出文字线索（需配置 vision）",
            description=(
                "适用于截图、二维码、验证码、隐写预览图。未配置 vision 通道时返回软失败并提示替代工具。"
                "视觉结论必须用确定性工具复核。"
            ),
            parameters=_schema(
                {"image": _s("图片路径"), "prompt": _s("自定义提问，默认要求描述所有可能线索")},
                ["image"],
            ),
            handler=vision.analyze_image,
            category="vision",
        ),
        # ---------------- 元工具（tool_search） ----------------
        make_tool_search(registry),
    ]
    if config is None or config.state_v2.enabled:
        tools.extend(_ledger_tool_definitions())
    registry.register_many(tools)
    platform.register(registry)
    return tools


__all__ = ["make_tool_search", "register_builtins"]
