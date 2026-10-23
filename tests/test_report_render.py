"""report_generate 渲染纪律的回归测试。

历史问题（2026-10-19）：WP 会把全部黑板事实（含失败路径、攻击计划、内部笔记）当
「步骤」平铺，「关键命令与输出」倾倒全部执行记录（含 TOOL_ERROR、blackboard_get、
list_dir 等无命令调用），再加一节逐条执行日志「时间线」——一份 19 步 / 73KB 的
过程流水账。修复后：结论性事实才展开为步骤，过程记录只留一行摘要，命令只保留
成功且有 shell 命令的，「时间线」整节移除。
"""

from __future__ import annotations

from ctfagent.tools.builtin.report import (
    MAX_COMMANDS,
    MAX_STEPS,
    _classify_facts,
    _render,
    _usable_commands,
)


def _fact(key: str, summary: str = "", body: str = "", tags: list[str] | None = None) -> dict:
    return {"key": key, "summary": summary, "body": body, "tags": tags or []}


def _exec(tool: str, *, command: str = "", status: str = "completed", output: str = "") -> dict:
    return {"toolName": tool, "command": command, "status": status, "output": output}


def test_failed_and_plan_facts_are_not_steps() -> None:
    facts = [
        _fact("pwn.solved", "成功拿到 flag", "复现步骤……", tags=["pwn", "solved"]),
        _fact("pwn.render.scan.exhausted", "穷举排除", tags=["pwn", "failure", "excluded"]),
        _fact("pwn.attackplan", "计划：UAF → 任意写", tags=["pwn", "plan"]),
        _fact("pwn.attachments", "附件清单"),
    ]
    main, process = _classify_facts(facts)
    assert [f["key"] for f in main] == ["pwn.solved"]
    assert {f["key"] for f in process} == {
        "pwn.render.scan.exhausted",
        "pwn.attackplan",
        "pwn.attachments",
    }


def test_step_cap_degrades_overflow_to_summary() -> None:
    facts = [_fact(f"t.step{i}", "结论") for i in range(MAX_STEPS + 5)]
    main, process = _classify_facts(facts)
    assert len(main) == MAX_STEPS
    assert len(process) == 5


def test_command_filter_removes_internal_and_failed_calls() -> None:
    executions = [
        _exec("challenge_info", command=""),                      # 无命令 → 剔除
        _exec("blackboard_get", command="", output="# blackboard[x]"),  # 内部工具 → 剔除
        _exec("bash", command="ls", status="failed"),             # 失败 → 剔除
        _exec("bash", command="python3 solve.py", output="flag{...}"),
        _exec("python_exec", command="python3 x.py"),
    ]
    usable = _usable_commands(executions)
    assert [r["toolName"] for r in usable] == ["bash", "python_exec"]


def test_render_has_no_timeline_and_no_raw_logs() -> None:
    facts = [
        _fact("t.solved", "拿到 flag", "解密循环 out[i]=…", tags=["solved"]),
        _fact("t.failed_chain", "此路不通", tags=["failure"]),
    ]
    executions = [
        _exec("challenge_info", command="", output="[TOOL_ERROR] 未提供合法题目目标"),
        _exec("blackboard_get", command="", output="# blackboard[t.plan]"),
        _exec("bash", command="python3 solve.py", output="flag{Fr33}"),
    ]
    main, process = _classify_facts(facts)
    md = _render(
        title="t",
        challenge=None,
        main_facts=main,
        process_facts=process,
        commands=_usable_commands(executions),
        flags=[{"value": "flag{Fr33}", "validated": True, "source": "flag_submit"}],
    )

    assert "## 时间线" not in md                       # 逐条执行日志移除
    assert "[TOOL_ERROR]" not in md                    # 失败调用不进 WP
    assert "blackboard_get" not in md                  # 内部调用不进 WP
    assert md.count("### 步骤") == 1                   # 只有结论性事实展开
    assert "- **t.failed_chain**：此路不通" in md       # 失败路径只留一行摘要
    assert "python3 solve.py" in md                    # 成功命令保留
    assert "flag{Fr33}" in md                          # flag 节保留


def test_command_output_is_capped() -> None:
    commands = [_exec("bash", command="big.py", output="x" * 5000)]
    md = _render(title="t", challenge=None, main_facts=[], process_facts=[],
                 commands=commands, flags=[])
    assert "原文 5000 字符" in md
    assert len(md) < 5000
