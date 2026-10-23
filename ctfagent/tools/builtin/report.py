"""writeup 生成内置工具：report_generate。

汇总「黑板 facts + 工具执行记录 + flag 记录」生成结构化 writeup（Markdown），
并写入 ``workspace/challenge/{id}/writeup.md``，同时把内容返回给模型做最终校对。

渲染纪律（防止 WP 变成「过程流水账」）：
- 黑板事实**分级**：失败/排除/计划/内部记录类条目只进「已排除路径与背景记录」一行摘要，
  不展开正文 —— 它们是解题过程的工作笔记，不是可复现步骤；
- 执行记录只保留**成功且有命令**的（``challenge_info`` / ``blackboard_get`` /
  ``list_dir`` 等无命令内部调用与失败调用是日志，不是步骤）；
- 不输出「时间线」逐条执行日志。
"""

from __future__ import annotations

from typing import Any

from ...models.types import ToolOutcome
from ..context import ToolContext
from ..guard import soft_failure
from .paths import display_path

_SECTIONS = (
    "## 题目信息",
    "## 解题思路",
    "## 关键步骤（可复现）",
    "## 已排除路径与背景记录",
    "## 关键命令与输出",
    "## flag",
    "## 复盘与可迁移技巧",
)

#: 步骤/命令明细的渲染上限——WP 是给人看的复现文档，不是执行日志的转储
MAX_STEPS = 12
MAX_COMMANDS = 12
MAX_FACT_BODY_CHARS = 1200
MAX_COMMAND_OUTPUT_CHARS = 400

#: 判定「过程性事实」的标记：这些只做一行摘要，不展开成步骤
_PROCESS_TAGS = {
    "failure",
    "failed",
    "excluded",
    "plan",
    "attackplan",
    "note",
    "hypothesis",
    "decoy",
    "superseded",
    "observation",
    "wip",
}
_PROCESS_KEY_HINTS = (
    "plan",
    "failed",
    "attachments",
    "note",
    "excluded",
    "hypothesis",
    "selftest",
    "todo",
)

#: 执行记录里的内部工具（永远不属于「关键命令」）
_INTERNAL_TOOLS = {
    "blackboard_get",
    "blackboard_upsert",
    "challenge_info",
    "report_generate",
    "write_todos",
    "memory_write",
    "memory_read",
    "idea_upsert",
    "idea_transition",
    "tool_search",
    "wait_tool_execution",
    "get_tool_execution",
}


def _is_process_fact(fact: dict[str, Any]) -> bool:
    """该黑板事实是否属于「过程性记录」（失败路径/计划/内部笔记）。"""
    tags = {str(t).strip().lower() for t in (fact.get("tags") or []) if t}
    if tags & _PROCESS_TAGS:
        return True
    key = str(fact.get("key") or "").lower()
    return any(hint in key for hint in _PROCESS_KEY_HINTS)


def _usable_commands(executions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从执行记录里筛出值得写进 WP 的命令：成功 + 有命令 + 非内部工具。

    ``challenge_info`` / ``blackboard_get`` / ``list_dir`` 这类调用没有 shell 命令，
    它们的输出属于过程日志；失败的调用属于「无效尝试」，在复盘里说明即可。
    """
    usable = [
        record
        for record in executions
        if (record.get("status") or "") == "completed"
        and str(record.get("command") or "").strip()
        and str(record.get("toolName") or "") not in _INTERNAL_TOOLS
    ]
    # 解题后段（接近成功的路径）更具复现价值
    return usable[-MAX_COMMANDS:]


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原文 {len(text)} 字符）"


def _classify_facts(facts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把黑板事实分成「关键步骤」与「过程记录」两类。

    未被标记但超出 ``MAX_STEPS`` 的条目也降级为过程记录（一行摘要），
    保证 WP 是可读的复现文档而不是黑板转储。
    """
    main_facts = [fact for fact in facts if not _is_process_fact(fact)]
    process_facts = [fact for fact in facts if _is_process_fact(fact)]
    if len(main_facts) > MAX_STEPS:
        process_facts = main_facts[MAX_STEPS:] + process_facts
        main_facts = main_facts[:MAX_STEPS]
    return main_facts, process_facts


def _render(
    *,
    title: str,
    challenge: dict[str, Any] | None,
    main_facts: list[dict[str, Any]],
    process_facts: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    flags: list[dict[str, Any]],
) -> str:
    lines: list[str] = [f"# {title}", ""]
    lines.append("> 由 CTF-Agent 自动生成 · 仅用于 CTF 竞赛与授权靶场的解题记录")
    lines.append("")

    lines += ["## 题目信息", ""]
    if challenge:
        lines += [
            f"- 题目：{challenge.get('name') or challenge.get('id')}",
            f"- 分类：{challenge.get('category')}",
            f"- 分值：{challenge.get('points')}",
            f"- 目标：{challenge.get('target') or '(未登记)'}",
            f"- flag 格式：{challenge.get('flagPattern') or '(未登记)'}",
            "",
        ]
        if challenge.get("description"):
            lines += ["**题目描述**：", "", challenge["description"], ""]
    else:
        lines += ["- （未登记题目元数据，以下内容依据会话黑板与执行记录生成）", ""]

    # ----------------------------- 解题思路 -----------------------------
    lines += ["## 解题思路", ""]
    if main_facts:
        for fact in main_facts:
            summary = (fact.get("summary") or "").strip()
            if summary:
                lines.append(f"- **{fact['key']}**：{summary}")
    else:
        lines += ["- （黑板无结论性记录：说明本题未按规范边做边记，复盘时应改进记录习惯）"]
    lines.append("")

    # -------------------------- 关键步骤（可复现） --------------------------
    lines += ["## 关键步骤（可复现）", ""]
    if main_facts:
        for index, fact in enumerate(main_facts, start=1):
            lines.append(f"### 步骤 {index}：{fact['key']}")
            lines.append("")
            lines.append(_clip(fact.get("body") or f"(summary) {fact.get('summary')}", MAX_FACT_BODY_CHARS))
            if fact.get("tags"):
                lines.append("")
                lines.append(f"`tags: {', '.join(fact['tags'])}`")
            lines.append("")
    else:
        lines.append("（无黑板记录）")
        lines.append("")

    # ---------------------------- 已排除路径 ----------------------------
    lines += ["## 已排除路径与背景记录", ""]
    if process_facts:
        for fact in process_facts:
            summary = (fact.get("summary") or "").strip() or "(无 summary)"
            lines.append(f"- **{fact['key']}**：{summary}")
    else:
        lines.append("- （无）")
    lines.append("")

    # --------------------------- 关键命令与输出 ---------------------------
    lines += ["## 关键命令与输出", ""]
    if commands:
        for record in commands:
            tool = record.get("toolName") or "?"
            command = str(record.get("command") or "").strip()
            lines.append(f"- **{tool}**：`{_clip(command, 300)}`")
            output = (record.get("output") or "").strip()
            if output:
                lines.append("")
                lines.append("```text")
                lines.append(_clip(output, MAX_COMMAND_OUTPUT_CHARS))
                lines.append("```")
            lines.append("")
    else:
        lines.append("（无可复现命令——关键操作请在上方步骤中补充说明）")
        lines.append("")

    # -------------------------------- flag --------------------------------
    lines += ["## flag", ""]
    if flags:
        for record in flags:
            mark = "✅ 格式通过" if record.get("validated") else "⚠️ 格式未通过"
            lines.append(f"- `{record.get('value')}`（{mark}，source={record.get('source') or '-'}）")
    else:
        lines += ["- （未记录 flag）"]
    lines.append("")

    # --------------------------- 复盘与可迁移技巧 ---------------------------
    lines += ["## 复盘与可迁移技巧", ""]
    lines += [
        "- 有效路径：见「关键步骤」中标注为可行的攻击链。",
        "- 无效尝试：见「已排除路径与背景记录」；复盘时说明为何排除，避免下次重走。",
        "- 可迁移技巧：把本题的可复用 payload / 脚本沉淀到 skills/ 或知识库，供后续同类题检索。",
        "",
    ]
    return "\n".join(lines)


async def report_generate(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    title = str(args.get("title") or "").strip()
    include_executions = bool(args.get("include_executions", True))
    write_to_file = bool(args.get("write_to_file", True))

    challenge = await ctx.repos.challenges.get(ctx.challenge_id) if ctx.challenge_id else None
    if not title:
        title = (challenge or {}).get("name") or f"CTF Writeup · {ctx.challenge_id or '未命名题目'}"

    facts = await ctx.repos.facts.list(ctx.blackboard_scope, limit=200)
    executions = await ctx.repos.executions.list(conversation_id=ctx.conversation_id, limit=200)
    # U18：绑定题目时按**题目**取 flag（同题多 Solver 的成果都要进 WP），否则按会话
    if ctx.challenge_id:
        flags = await ctx.repos.flags.list_by_challenge(ctx.challenge_id, limit=50)
    else:
        flags = await ctx.repos.flags.list(conversation_id=ctx.conversation_id, limit=50)

    if not facts and not executions:
        return ToolOutcome.error(
            soft_failure(
                "没有可汇总的证据（黑板为空且无执行记录），无法生成可信 writeup。",
                tool="report_generate",
                hint="先把已确认事实 blackboard_upsert，再重新生成报告。",
            )
        )

    main_facts, process_facts = _classify_facts(facts)
    commands = _usable_commands(executions) if include_executions else []

    markdown = _render(
        title=title,
        challenge=challenge,
        main_facts=main_facts,
        process_facts=process_facts,
        commands=commands,
        flags=flags,
    )

    saved_path = ""
    if write_to_file:
        workspace = ctx.resolved_workspace()
        path = workspace / "writeup.md"
        path.write_text(markdown, encoding="utf-8")
        saved_path = display_path(path, ctx)
        await ctx.notify(
            "progress",
            {"stage": "report", "message": f"writeup 已写入 {saved_path}", "tool": "report_generate"},
        )

    header = (
        f"writeup 已生成（{len(markdown)} 字符，{len(main_facts)} 个关键步骤，"
        f"{len(process_facts)} 条已排除路径，{len(commands)} 条关键命令，{len(flags)} 个 flag）"
    )
    if saved_path:
        header += f"\n已保存：{saved_path}"
    if not include_executions:
        header += "\n（按要求未附命令与输出）"
    header += "\n\n--- 以下为完整内容，请校对证据链是否自洽 ---\n\n"
    return ToolOutcome.ok(
        header + markdown,
        reportPath=saved_path,
        factCount=len(facts),
        flagCount=len(flags),
    )


__all__ = ["_SECTIONS", "report_generate"]
