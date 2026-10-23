"""Observer 可选小模型判官（升级提示词 §6.1）：**成本换精度**。

默认关闭（``observer.judge.enabled=false``）：所有 Observation 直接放行。
打开后，每条候选提醒先交给一个独立小通道复核，模型回答"不值得"就丢弃——
用一点 token 换取更少的误报打扰（提醒本身不改变任务，误报的代价是干扰模型）。

判官**永不阻塞解题**：模型未配置、调用异常、输出无法解析时一律放行（fail-open），
因为"少提醒一次"远比"提醒链路把 Solver 卡住"便宜。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import ObserverJudgeConfig
from ..models.types import ChatMessage
from .detector import Observation

logger = logging.getLogger(__name__)

JUDGE_SYSTEM = """\
你是 CTF 自主解题系统的效率观察员。系统检测到某个 Worker 可能在做无用功，
准备给它发一条提醒。你的唯一任务是判断这条提醒**此刻是否值得打断它**。

只输出一行，以「是」或「否」开头，后接不超过 20 字的理由。不要解释判据，不要复述提醒。
值得打断：提醒指向明确、可立即执行（换策略 / 落账本 / 收尾）。
不值得打断：信息已在上下文里、属于正常探索节奏、或提醒过于笼统。"""

JUDGE_USER = """\
检测规则：{rule}
提醒内容：{reason}
补充信息：{detail}
工作者当前任务：{task}"""

#: 明确否定的前缀（其余一律放行）
_NEGATIVE = ("否", "不", "no", "not", "nope")


class ObserverJudge:
    """提醒复核器（未启用时为透明放行）。"""

    def __init__(self, config: ObserverJudgeConfig, model: Any = None) -> None:
        self.config = config
        self.model = model

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self.model is not None)

    async def confirm(self, observation: Observation, *, task: str = "") -> bool:
        """复核一条提醒；返回 True 表示应当发出。"""
        if not self.enabled:
            return True
        prompt = JUDGE_USER.format(
            rule=observation.rule,
            reason=observation.reason,
            detail=observation.detail or "(无)",
            task=(task or "(未知)")[:200],
        )
        try:
            response = await self.model.chat([ChatMessage.system(JUDGE_SYSTEM), ChatMessage.user(prompt)])
        except Exception:  # noqa: BLE001 - 判官失败不影响解题，一律放行
            logger.debug("Observer 判官调用失败，按放行处理", exc_info=True)
            return True
        verdict = str(getattr(response, "content", "") or "").strip().lower()
        if not verdict:
            return True
        if verdict.startswith(_NEGATIVE):
            logger.info("Observer 判官驳回提醒：%s | %s", observation.rule, verdict[:60])
            return False
        return True
