"""提示词资产：内置单代理系统提示词 + 运行期上下文注入。

``DEFAULT_SYSTEM_PROMPT`` 与提示词规格 §6.1 保持一致（逐字保留合规与解题方法论段落），
可用 ``agent.system_prompt_path`` 指向自定义文件覆盖。
"""

from __future__ import annotations

from typing import Any

from ..config import AppConfig
from ..constants import SCOPE_GUARD_PROMPT

DEFAULT_SYSTEM_PROMPT = """你是 CTF-Agent，一名顶级 CTF 选手与安全研究员，正在解题。

【范围与合规】
- 你只能在用户提供的题目目标（题目平台地址、靶机 IP:Port、附件文件）范围内行动。
- 目标不明确时：先用 challenge_info 查看题目元数据；仍缺失则列出"缺失信息清单"要求用户补充，禁止臆造目标。
- 不对清单之外的任何主机/域名发起扫描或利用。

【解题方法论】
1. 理题：先读全题目描述、附件清单、给分提示；把"已知/未知/要拿什么(flag 格式)"写进计划。
2. 侦察：对靶机做最小必要探测（端口、指纹、目录、文件类型）；附件先 file/exiftool/binwalk 鉴别类型再深入。
3. 假设驱动：每次工具调用前用 1-3 句话写明"当前假设 + 选择该工具的原因 + 期望看到什么"。
4. 快速试错：crypto/misc 类优先跑自动化脚本（编码识别、常见弱口令、已知攻击脚本），失败即换路。
5. 深度优先带回溯：一条技术线 3-5 次工具调用无进展就记录到黑板并切换，避免死磕单点。
6. 证据链：任何结论必须附带可复现证据（命令+输出片段）；禁止无证据的"我猜 flag 是..."。
7. flag 纪律：发现疑似 flag 立即 flag_submit；提交前核对格式（如 flag{...}）。

【边做边记（强制）】
- 每确认一条新认知（开放端口、服务版本、加密参数、关键偏移、可行/不可行的攻击路径）立即 blackboard_upsert；
  summary 一行写"什么+在哪+如何验证"，body 写完整细节。先落黑板再继续下一步。

【表达】
- 面向用户的回复结构：## 当前阶段 / ## 已确认事实(证据) / ## 下一步。
- 工具失败时：读错误信息→改参数或换工具→继续；单点失败不终止解题。
- 需要交互式输入的进程用 pwntools 脚本而非裸命令，避免挂死。

【输出格式】
- 结束时调用 report_generate 产出完整 writeup（题目/思路/步骤/关键命令/flag/复盘）。
"""


def load_system_prompt(config: AppConfig, override_path: str | None = None) -> str:
    """读取系统提示词：优先显式 override → 配置的路径 → 内置默认。"""
    from pathlib import Path

    candidates = [override_path, config.agent.system_prompt_path]
    for candidate in candidates:
        if candidate:
            path = Path(candidate)
            if not path.is_absolute():
                path = config.project_root / path
            if path.is_file():
                return path.read_text(encoding="utf-8")
    return DEFAULT_SYSTEM_PROMPT


def render_runtime_context(
    *,
    config: AppConfig,
    workspace: str,
    conversation_id: str,
    challenge: dict[str, Any] | None,
    blackboard_index: list[dict[str, str]],
    tool_index: str,
    is_sub_agent: bool = False,
    extra: str = "",
    state_block: str = "",
    steering_enabled: bool = False,
) -> str:
    """渲染注入到系统提示词末尾的运行期上下文块。

    ``state_block``（v2）：挑战/Solver 上下文的**渲染式状态块**（由
    :func:`ctfagent.state.renderer.render_worker_context` 生成，固定预算）。
    为空时输出与 v1 **逐字节一致**——这是"关闭 v2 开关即回到 v1"的保证点。
    """
    lines: list[str] = ["", "=" * 60, "【运行期上下文】"]
    lines.append(f"- 会话 ID：{conversation_id}")
    lines.append(f"- 题目工作区（附件与产物都放这里）：{workspace}")
    lines.append(
        "- 会话隔离（强制）：只处理**本会话上传的附件**。禁止读取 "
        "`workspace/challenge/` 下其它题目的目录、旧 `writeup.md`、历史解题脚本或 flag；"
        "即使它们看起来相关，也必须以本轮附件为准（跨题引用会导致答案串题）。"
    )
    lines.append(f"- 项目根目录：{config.project_root}")
    lines.append(
        f"- 单轮最大迭代：{config.agent.max_iterations}（子代理 {config.agent.sub_agent_max_iterations}）"
    )
    lines.append(
        f"- 工具等待上限：{config.agent.tool_wait_timeout_seconds}s；硬超时 {config.agent.tool_timeout_seconds}s"
    )
    lines.append(
        f"- 单条工具结果上限：{config.agent.max_tool_result_bytes} 字节（超出会落盘并给 <persisted-output> 提示）"
    )

    if challenge:
        lines.append("")
        lines.append("【题目元数据】")
        lines.append(f"- 题目：{challenge.get('name')}（{challenge.get('id')}）")
        lines.append(f"- 分类：{challenge.get('category')}；分值：{challenge.get('points')}")
        lines.append(f"- 目标：{challenge.get('target') or '（未填写——若为靶机题必须向用户澄清）'}")
        lines.append(f"- flag 格式：{challenge.get('flagPattern') or '（未指定，默认 xxxx{...}）'}")
        description = (challenge.get("description") or "").strip()
        if description:
            lines.append("- 题目描述：")
            lines.append(description[:2000])
    else:
        lines.append("")
        lines.append('【题目元数据】未登记。请先 challenge_info(action="get")；目标缺失时必须向用户澄清。')

    lines.append("")
    lines.append("【黑板（跨步骤事实账本，key → summary；用 blackboard_get(key=...) 取完整 body）】")
    if blackboard_index:
        for item in blackboard_index:
            lines.append(f"- {item['key']}: {item.get('summary') or '(无 summary)'}")
        lines.append("- （以上为已记录事实；新认知同样要 blackboard_upsert 落盘，避免丢失或重复验证）")
    else:
        lines.append("- （当前为空）")
    lines.append(
        "【边做边记（强制）】每确认一条新认知（端口/服务版本/漏洞/关键偏移/可行或已排除的攻击路径）"
        "必须**立即**调用 blackboard_upsert(key=稳定短键如 pwn.libc.version, "
        "summary=一行写清「什么+在哪+如何验证」, body=完整细节与证据) 落盘，先落黑板再继续下一步；"
        "任务结束时会校验黑板证据，空黑板视为未完成。"
    )

    if state_block:
        lines.append("")
        lines.append(state_block)

    if tool_index:
        lines.append("")
        lines.append("【可用工具索引（name: 用途；调用前请核对参数）】")
        lines.append(tool_index)

    if extra:
        lines.append("")
        lines.append(extra)

    if steering_enabled and not is_sub_agent:
        lines.append("")
        lines.append(
            "【运行中引导】用户可能在任务执行期间随时追加指令：这些指令会在当前步骤结束、"
            "下一步开始前作为新的用户消息出现。收到后应把它们纳入当前计划并继续执行"
            "（不要当作打断重来的信号），必要时先用一两句话简述调整后的计划再行动；"
            "新指令与原任务冲突时以新指令为准。"
        )

    lines.append("")
    lines.append(SCOPE_GUARD_PROMPT)
    if is_sub_agent:
        lines.append("")
        lines.append(
            "【子代理约束】你只拥有 task.description 中的上下文，没有父对话，也没有 task 工具（禁止嵌套派单）。"
            "缺少目标或附件路径时立即停止并返回「缺失信息清单」。交付必须包含：## 结论 / ## 证据 / ## 风险与不确定性 / ## 建议下一步。"
        )
    return "\n".join(lines)


def build_system_prompt(
    *,
    config: AppConfig,
    agent_prompt: str | None = None,
    **runtime_kwargs: Any,
) -> str:
    """拼装最终系统提示词：角色提示词 + 运行期上下文。"""
    role = agent_prompt or load_system_prompt(config)
    return role.rstrip() + "\n" + render_runtime_context(config=config, **runtime_kwargs)


__all__ = ["DEFAULT_SYSTEM_PROMPT", "build_system_prompt", "load_system_prompt", "render_runtime_context"]
