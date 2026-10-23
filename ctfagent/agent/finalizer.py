"""最终化检查：证据不足时自动续跑。

当模型给出"没有工具调用"的回复时，不能立刻结束——CTF 场景里模型经常过早总结。
本模块按启发式规则判断证据是否充分，不足则追加一条"请继续"消息让循环继续。

判定输入：本轮是否写过黑板、是否提交过 flag、是否生成过报告、迭代次数、已续跑次数。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import AppConfig
from ..models.types import ModelResponse
from .context import extract_flag_candidates

CONTINUE_PROMPT_TEMPLATE = """你的上一条回复没有调用任何工具，但当前证据链尚不足以结束解题：
{reasons}

请立刻继续推进（不要重复已经失败的命令）：
1. 如果还有未验证的假设：调用工具继续验证，并在调用前写明「假设 + 选它的原因 + 期望看到什么」。
2. 如果已拿到疑似 flag：调用 flag_submit 提交，并注明来源与验证方式。
3. 如果解题已完成：调用 report_generate 产出 writeup，然后给出最终结论。
4. 如果目标信息缺失：列出「缺失信息清单」向用户澄清，不要臆造目标。
"""


@dataclass
class FinalizeDecision:
    """最终化判定结果。"""

    continue_run: bool = False
    message: str | None = None
    reasons: list[str] = field(default_factory=list)


class Finalizer:
    """决定"是否允许结束本轮 Agent 循环"。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def check(
        self,
        *,
        response: ModelResponse,
        iterations: int,
        retries_used: int,
        blackboard_writes: int,
        tool_calls_made: int,
        flag_submitted: bool,
        report_generated: bool,
        flag_pattern: str,
    ) -> FinalizeDecision:
        agent_cfg = self.config.agent
        if not agent_cfg.finalizer_enabled:
            return FinalizeDecision()
        if retries_used >= max(0, agent_cfg.max_finalizer_retries):
            return FinalizeDecision()

        reasons: list[str] = []
        content = response.content or ""

        if not content.strip():
            reasons.append("- 你的回复是空的：需要给出结论，或继续调用工具。")
        if not flag_submitted and not report_generated:
            if iterations < 3:
                reasons.append(f"- 目前只迭代了 {iterations} 步，尚未完成「理题→侦察→验证」的最低循环。")
            if tool_calls_made == 0:
                reasons.append("- 本轮从未调用任何工具：没有证据的结论在 CTF 中不可接受。")
            elif blackboard_writes == 0:
                reasons.append(
                    "- 本轮没有写入任何黑板/账本事实：确认的认知必须 blackboard_upsert"
                    "（挑战会话中用 memory_write）落盘，否则证据会丢失。"
                )
            if not report_generated:
                reasons.append("- 尚未调用 report_generate 产出 writeup。")

        candidates = extract_flag_candidates(
            content, flag_pattern or r"(?i)[a-z0-9_]{2,32}\{[^{}\n]{1,256}\}"
        )
        if candidates and not flag_submitted:
            reasons.append(f"- 你的回复中出现了疑似 flag（{candidates[:3]}）但没有调用 flag_submit 提交。")

        if not reasons:
            return FinalizeDecision()
        return FinalizeDecision(
            continue_run=True,
            message=CONTINUE_PROMPT_TEMPLATE.format(reasons="\n".join(reasons)),
            reasons=reasons,
        )


__all__ = ["CONTINUE_PROMPT_TEMPLATE", "FinalizeDecision", "Finalizer"]
