"""终止评估器：结束条件**完全外置**（不依赖模型自称"完成了"）。

三路 verdict（对应升级提示词 §5.4）::

    TERMINATE_SUCCESS    : flag 已提交并通过校验
    TERMINATE_EXHAUSTED  : 无 pending/testing Idea 且 running==0 且至少完成过一次 explore（图静止）
    TERMINATE_TIMEUP     : 时间预算耗尽（无论进展）

另有两个运行期状态（不在提示词三条之内，但控制面必须有）::

    TERMINATE_STOPPED    : 人工请求停止
    TERMINATE_ABORTED    : 目标范围校验未通过（合规硬约束，fail-closed）

``finish`` 的动作由 :class:`~ctfagent.orchestrator.dispatcher.Dispatcher` 执行：
停掉所有 Solver（各自 conclude）→ 生成 writeup → 发 ``challenge_terminated`` → 归档状态。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from ..config import AppConfig
from .policy import ChallengeSnapshot

__all__ = [
    "PRIVATE_HOST_SUFFIXES",
    "ScopeCheck",
    "TerminationEvaluator",
    "TerminationVerdict",
    "check_target_scope",
]


class TerminationVerdict(StrEnum):
    """终止结论。"""

    SUCCESS = "TERMINATE_SUCCESS"
    EXHAUSTED = "TERMINATE_EXHAUSTED"
    TIMEUP = "TERMINATE_TIMEUP"
    STOPPED = "TERMINATE_STOPPED"
    ABORTED = "TERMINATE_ABORTED"


#: 视为"自建/本地"的主机名后缀（用于合规范围校验）
PRIVATE_HOST_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".internal",
    ".lan",
    ".test",
    ".localhost",
    ".docker",
)

#: 主机名中包含这些标记时视为 CTF 靶场（竞赛/实验室常见命名）
CTF_HOST_MARKERS: tuple[str, ...] = ("ctf", "challenge", "chall", "target", "vuln", "ctfshow", "buuoj")

#: 域名标签形态（RFC 1123 简化版；用于区分"可拨号域名"与描述性文字）
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class ScopeCheck:
    """目标范围校验结果。"""

    allowed: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason}


def _looks_like_network_host(host: str) -> bool:
    """判定 host 是否形如**可拨号**的网络目标（IP 或标准点分域名）。

    描述性文字（"本地 getshell"、"题目 v1.2 复现环境"）不是网络目标——合规校验
    只对真正可被工具拨号的形态（IP/域名）做拦截，避免把附件题当越界处理。
    """
    host = (host or "").strip().lower()
    if not host or " " in host:
        return False
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2:
        return False
    return all(_DOMAIN_LABEL_RE.match(label) for label in labels)


def check_target_scope(challenge: dict[str, Any] | None) -> ScopeCheck:
    """校验挑战目标是否落在允许范围内（**fail-closed**）。

    v1 只有提示词层面的 ``SCOPE_GUARD_PROMPT`` + ``challenge_info`` 的缺失信息清单；
    v2 的控制面是**自动派发**的，因此必须在代码层再做一次确定性校验，避免 Dispatcher
    把 Solver 派向未授权目标。

    合规边界要挡的是"对公网/未授权目标发起网络攻击"，因此：
    - 允许：**缺失目标（按纯附件/本地题处理）**、回环/私有地址、``*.local/.internal/.test``
      等内网后缀、含 ctf/challenge/target 等靶场标记的主机名、纯附件题（无网络目标）、
      非网络形态的 target 文本（描述性文字/本地路径——不可拨号即无网络攻击面）；
    - 拒绝：公网 IP / 公网域名。放行需显式声明：``challenge.meta.target_authorized = true``。
    """
    target = str((challenge or {}).get("target") or "").strip()
    meta = (challenge or {}).get("meta") or {}
    if meta.get("target_authorized") is True:
        return ScopeCheck(True, "已通过 challenge.meta.target_authorized 显式授权")
    if not target:
        # 缺靶机地址 ≠ 越界：本地 getshell 类附件题是 CTF 常态，派发只在本地/workspace
        # 操作；"缺少靶机信息"由提示词层（challenge_info 缺失信息清单）负责澄清。
        return ScopeCheck(True, "未填写网络目标：按纯附件/本地题处理（仅本地与 workspace 操作）")

    parsed = urlparse(target if "://" in target else f"//{target}", scheme="http")
    host = (parsed.hostname or "").strip().lower()
    if not _looks_like_network_host(host):
        return ScopeCheck(True, f"非网络目标（纯附件/本地路径/描述性文字）：{target[:60]}")

    if host in {"localhost", "::1"} or host.endswith(PRIVATE_HOST_SUFFIXES):
        return ScopeCheck(True, f"本地/内网目标：{host}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if any(marker in host for marker in CTF_HOST_MARKERS):
            return ScopeCheck(True, f"靶场命名的域名：{host}")
        return ScopeCheck(
            False,
            f"公网域名 {host}：仅允许 CTF 环境/自建靶场/书面授权目标。"
            "确认已获授权后可在题目 meta 中设置 target_authorized=true 放行"
            "（POST /api/challenges 时携带 meta 字段）。",
        )
    if address.is_private or address.is_loopback or address.is_link_local:
        return ScopeCheck(True, f"私有/回环地址：{host}")
    return ScopeCheck(
        False,
        f"公网地址 {host}：仅允许 CTF 环境/自建靶场/书面授权目标。"
        "确认已获授权后可在题目 meta 中设置 target_authorized=true 放行"
        "（POST /api/challenges 时携带 meta 字段）。",
    )


class TerminationEvaluator:
    """按快照判定是否结束挑战会话。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    def evaluate(self, snapshot: ChallengeSnapshot) -> TerminationVerdict | None:
        """返回终止结论；``None`` 表示继续运行。"""
        if not snapshot.scope_ok:
            return TerminationVerdict.ABORTED
        if snapshot.stop_requested:
            return TerminationVerdict.STOPPED
        if snapshot.flag_verified:
            return TerminationVerdict.SUCCESS
        budget = snapshot.time_budget_seconds
        if budget > 0 and snapshot.elapsed_seconds >= budget:
            return TerminationVerdict.TIMEUP
        if self._graph_settled(snapshot):
            return TerminationVerdict.EXHAUSTED
        return None

    def _graph_settled(self, snapshot: ChallengeSnapshot) -> bool:
        """图静止：图**动过**、无未收敛 Idea、无运行中 Solver、且完成过至少 N 次正向推进。

        ``advanced > 0`` 这一项很关键：否则"刚启动、还没派发任何 Solver"的空账本会被
        立刻判成 EXHAUSTED（``min_advances_before_exhausted=0`` 时尤其危险）。
        """
        min_advances = max(0, int(self.config.termination.min_advances_before_exhausted))
        # bootstrap 也算一次推进：否则"bootstrap 跑完但没产出任何 Idea/fact"的账本会卡住不终止
        advanced = snapshot.bootstraps_done + snapshot.explores_done + snapshot.reasons_done
        return (
            advanced > 0
            and not snapshot.open_ideas
            and snapshot.running_count == 0
            and advanced >= max(1, min_advances)
        )

    @staticmethod
    def describe(verdict: TerminationVerdict, snapshot: ChallengeSnapshot | None = None) -> str:
        """给人类/SSE 的一句话说明。"""
        base = {
            TerminationVerdict.SUCCESS: "flag 已捕获并通过本地校验（平台提交由竞赛引擎负责）",
            TerminationVerdict.EXHAUSTED: "所有方向已收敛且无运行中 Solver（图静止）",
            TerminationVerdict.TIMEUP: "时间预算耗尽",
            TerminationVerdict.STOPPED: "人工请求停止",
            TerminationVerdict.ABORTED: "目标范围校验未通过（合规边界）",
        }.get(verdict, str(verdict))
        if snapshot is not None and verdict is TerminationVerdict.TIMEUP:
            base += f"（已用 {snapshot.elapsed_seconds / 60:.1f} 分钟）"
        return base
