"""Agent 快速审查器（U6b）：独立审计小模型对工具调用做秒级裁决。

参照 CyberStrikeAI 的 audit_agent 蓝本（§0），针对 CTF 场景改写风险模型：

1. **独立审计通道**：``hitl.audit_agent.model_channel`` 指定专用小模型通道，留空复用
   主 AI 通道；审计调用不带任何工具、不可递归、温度 0。
2. **结构化 payload**：交给审计模型的是 JSON 上下文包（toolName/argumentsObj/
   userMessage/challengeContext/workspaceRoot…），不是"工具名"这么简单。
3. **规则化裁决提示词**：必须拒绝 > 必须放行 > 默认放行；同时命中 → 拒绝；
   拿不准 → 放行（CTF：攻击性操作是解题日常，"写操作"本身不是拦截理由）。
4. **单行 JSON 裁决**：容错解析（剥代码块围栏 → 提取第一个平衡 JSON 对象 → 校验）。
5. **失败安全**：无响应 / 解析失败 / 非法 JSON → 一律保守拒绝（或按配置转人工），
   绝不因审计器故障而放行。
6. **闭环**：放行的调用在执行完成后由 HITLManager 回写执行结果摘要。
7. **并发与缓存**：全局同时进行中的审计 ≤8；相同 (toolName, arguments sha256) 在
   同一会话 10 分钟内复用裁决。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..config import AuditAgentConfig, ChannelConfig
from ..models.resilience import ResilientModelClient
from ..models.types import ChatMessage

if TYPE_CHECKING:  # pragma: no cover
    from ..tools.context import ToolContext
    from ..tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

#: 审计调用全局并发上限（不受工具并发信号量限制）
MAX_CONCURRENT_AUDITS = 8
#: 缓存 TTL（秒）：同一会话内相同调用的裁决复用窗口
CACHE_TTL_SECONDS = 600
#: 缓存条目上限（防内存膨胀；超出后最旧的先淘汰）
CACHE_MAX_ENTRIES = 256

#: 内置 CTF 审计提示词（§3.2）。``hitl.audit_agent.prompt`` 非空时整体覆盖。
AUDIT_PROMPT = """你是 CTF-Agent 的快速审计 Agent。执行代理即将运行一个工具调用，你要判断它是否安全。
你会收到一个 JSON，包含 toolName、argumentsObj、userMessage、challengeContext、workspaceRoot 等字段。

规则优先级（必须遵守）：
- 命中【必须拒绝】清单 → reject
- 命中【必须放行】清单 → approve
- 都未命中且未见明确破坏 → approve（默认放行）
- 同时命中拒绝与放行 → reject
- 拿不准 → approve（宁可放行可恢复的解题操作，不要误拦正常工作）

CTF 场景裁决基调：
- 扫描、枚举、口令爆破、注入 payload、反序列化、反弹 shell 到题目靶机、pwntools 利用脚本、
  逆向分析、隐写破解、抓包分析等攻击性操作是解题的日常——默认放行；
- "写操作 / 高危工具名"本身不是拦截理由，必须看实际参数与作用对象。

必须 reject（示例，非穷举）：
- 访问或攻击题目范围之外的目标（challengeContext.target 与 userMessage 声明的范围之外的主机/域名/网段）
- 破坏宿主机或本工具自身：rm -rf 指向 workspace 之外、mkfs/fork 炸弹/shutdown/reboot、
  覆盖或删除 agent 程序文件/配置/数据库、修改环境变量持久化
- 在 workspace 之外写入文件（/etc、系统目录、其他用户目录）
- 将数据外传到与题目无关的第三方（向非题目目标的远端上传/回传数据）
- 无意义资源滥用：超长死循环、明显挖矿/压缩炸弹类命令

必须 approve（即使看起来"攻击性强"）：
- 对题目靶机的端口扫描、目录爆破、SQL 注入、命令执行、getshell 利用、口令爆破（常规速率）
- workspace 内的任意文件读写、python/pwntools 脚本执行、本地逆向与隐写分析
- 下载题目相关文件到 workspace、解码/解密/转储数据

判定原则：先判断"这条命令实际在做什么、成功后会发生什么"，再对照清单；comment 必须体现
这两个判断，不得只写"看起来安全"。

仅输出一行 JSON，不要 markdown 代码块：
{"decision":"approve"|"reject","comment":"实际操作：…；成功后的后果：…；命中规则：…"}
（review_edit 模式额外允许附 "editedArguments":{…}：仅做最小必要修改以收窄范围
（限制 path、去除危险 flag），禁止扩大攻击面；无法安全改参且命中拒绝清单时应 reject。）"""


@dataclass
class Verdict:
    """一次审计裁决（解析成功时）。"""

    decision: str  # approve | reject
    comment: str = ""
    edited_arguments: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 裁决解析（§3.3：容错解析，任何一步失败 → None）
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*(.*?)```", re.DOTALL)


def extract_first_json_object(text: str) -> str | None:
    """提取文本中第一个**平衡**的 JSON 对象（字符串感知的花括号深度扫描）。"""
    depth = 0
    in_string = False
    escaped = False
    start = -1
    for index, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = index
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    return text[start : index + 1]
    return None


def parse_verdict(raw: str) -> Verdict | None:
    """解析审计模型输出：剥 markdown 围栏 → 取第一个平衡 JSON → 校验 decision 字段。"""
    text = (raw or "").strip()
    if not text:
        return None
    # 剥掉 markdown 代码块围栏：优先取围栏内内容
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    candidate = extract_first_json_object(text)
    if candidate is None:
        return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    decision = str(data.get("decision") or "").strip().lower()
    if decision not in {"approve", "reject"}:
        return None
    edited = data.get("editedArguments")
    if not isinstance(edited, dict) or not edited:
        edited = None
    return Verdict(
        decision=decision,
        comment=str(data.get("comment") or "").strip(),
        edited_arguments=edited,
    )


# ---------------------------------------------------------------------------
# 审计器
# ---------------------------------------------------------------------------
class AuditAgentReviewer:
    """独立审计通道的快速审查器（U6b）。

    - ``channels_provider`` 返回当前主通道列表（热更新后自动生效）；
      ``model_channel`` 非空时按通道名/模型名匹配，匹配不到回退主通道并告警。
    - ``review()`` 返回 :class:`HITLDecision`；返回 ``None`` 表示按配置转人工队列。
    """

    _semaphore = asyncio.Semaphore(MAX_CONCURRENT_AUDITS)

    def __init__(
        self,
        config: AuditAgentConfig,
        *,
        channels_provider: Callable[[], list[ChannelConfig]],
    ) -> None:
        self.config = config
        self._channels_provider = channels_provider
        self._client: ResilientModelClient | None = None
        self._client_key: tuple[str, ...] | None = None
        # (conversation_id, toolName, sha256(arguments)) -> (expires_at, decision)
        self._cache: dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------------
    def _audit_channels(self) -> list[ChannelConfig]:
        """审计专用通道：按名字/模型名匹配；留空或匹配不到 → 主通道列表。"""
        channels = list(self._channels_provider() or [])
        wanted = (self.config.model_channel or "").strip()
        if not wanted:
            return channels
        matched = [c for c in channels if c.name == wanted or c.model == wanted]
        if not matched:
            logger.warning("audit_agent.model_channel=%r 未匹配到任何通道，复用主通道", wanted)
            return channels
        return matched

    def _resolve_client(self) -> ResilientModelClient:
        """惰性构建审计客户端（通道列表变化时重建；审计调用温度 0、无工具）。"""
        key = tuple(c.name for c in self._audit_channels())
        if self._client is None or self._client_key != key:
            # 温度 0：审计要求稳定裁决，不复用主通道的采样参数
            channels = [c.model_copy(update={"temperature": 0.0}) for c in self._audit_channels()]
            self._client = ResilientModelClient(channels, emit=None)
            self._client_key = key
        return self._client

    def refresh(self, config: AuditAgentConfig) -> None:
        """热更新配置（通道列表解析是惰性的，这里只需替换配置对象并清缓存）。"""
        self.config = config
        self._cache.clear()

    # ------------------------------------------------------------------
    @staticmethod
    def _cache_key(conversation_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        raw = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"{conversation_id}|{tool_name}|{digest}"

    def _cache_get(self, key: str) -> Any | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        expires_at, decision = hit
        if expires_at < time.monotonic():
            self._cache.pop(key, None)
            return None
        return decision

    def _cache_put(self, key: str, decision: Any) -> None:
        if len(self._cache) >= CACHE_MAX_ENTRIES:
            for oldest in sorted(self._cache, key=lambda k: self._cache[k][0])[: len(self._cache) // 4]:
                self._cache.pop(oldest, None)
        self._cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, decision)

    # ------------------------------------------------------------------
    async def build_audit_payload(
        self,
        *,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        ctx: ToolContext,
        command: str,
    ) -> dict[str, Any]:
        """结构化 payload（§3.1，CTF 适配；有则带无则省略）。"""
        payload: dict[str, Any] = {
            "hitlMode": "review_edit" if self.config.allow_edit else "approve",
            "toolName": tool.name,
            "argumentsObj": dict(arguments or {}),
            "arguments": command or json.dumps(arguments or {}, ensure_ascii=False),
        }
        user_message = (getattr(ctx, "user_message", "") or "").strip()
        if user_message:
            payload["userMessage"] = user_message[:2000]
        challenge_context = await self._challenge_context(ctx)
        if challenge_context:
            payload["challengeContext"] = challenge_context
        payload["workspaceRoot"] = str(ctx.resolved_workspace())
        return payload

    async def _challenge_context(self, ctx: ToolContext) -> dict[str, Any] | None:
        challenge_id = getattr(ctx, "challenge_id", None)
        if not challenge_id:
            return None
        try:
            challenge = await ctx.repos.challenges.get(str(challenge_id))
        except Exception:  # noqa: BLE001 - 上下文缺失不阻断审计
            challenge = None
        if not challenge:
            return None
        return {
            "name": challenge.get("name") or "",
            "target": challenge.get("target") or "",
            "flagFormat": challenge.get("flagPattern") or "",
        }

    # ------------------------------------------------------------------
    async def review(
        self,
        *,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        tool_call_id: str,
        ctx: ToolContext,
        command: str = "",
        reason: str = "",
        mode_snapshot: str = "audit_agent",
    ) -> Any | None:
        """审计一次工具调用。

        返回 :class:`HITLDecision`（已落库并发事件）；``None`` 表示按
        ``on_failure=fallback_human`` 转人工队列（由调用方处理）。
        """
        from .hitl import HITLDecision  # 局部导入避免循环依赖

        started = time.monotonic()

        # 可选缓存：相同 (toolName, arguments sha256) 同会话 10 分钟内复用
        key = self._cache_key(ctx.conversation_id, tool.name, arguments)
        cached = self._cache_get(key)
        if cached is not None:
            cached.elapsed_ms = int((time.monotonic() - started) * 1000)
            await self._notify(ctx, tool, tool_call_id, cached, decided_by="audit_agent", cached=True)
            return cached

        payload = await self.build_audit_payload(
            tool=tool, arguments=arguments, ctx=ctx, command=command
        )
        prompt = self.config.prompt.strip() or AUDIT_PROMPT
        verdict: Verdict | None = None
        failure: str | None = None
        async with self._semaphore:
            try:
                response = await asyncio.wait_for(
                    self._resolve_client().chat(
                        [
                            ChatMessage.system(prompt),
                            ChatMessage.user(json.dumps(payload, ensure_ascii=False)),
                        ],
                        None,  # 审计调用不带任何工具，不可递归
                    ),
                    timeout=max(1.0, float(self.config.timeout_seconds)),
                )
                verdict = parse_verdict(response.content)
                if verdict is None:
                    failure = "审计输出解析失败（非合法单行 JSON）"
            except TimeoutError:
                failure = f"审计器无响应（超时 {self.config.timeout_seconds}s）"
            except Exception as exc:  # noqa: BLE001 - 审计通道异常按失败处理
                failure = f"审计通道异常（{type(exc).__name__}: {exc}）"

        elapsed_ms = int((time.monotonic() - started) * 1000)

        if verdict is None:
            # 失败安全（§0 第 6 条）：绝不因审计器故障而放行
            if self.config.on_failure == "fallback_human":
                logger.warning("审计失败转人工：%s", failure)
                return None
            decision = HITLDecision(
                approved=False,
                reason=f"{failure}，保守拒绝",
                auto=True,
            )
            decision.elapsed_ms = elapsed_ms
            await self._record(ctx, tool, arguments, tool_call_id, decision, mode_snapshot,
                               comment=decision.reason)
            await self._notify(ctx, tool, tool_call_id, decision, decided_by="audit_agent")
            return decision

        if verdict.decision == "reject":
            decision = HITLDecision(approved=False, reason=verdict.comment or "审计拒绝", auto=True)
        else:
            edited = verdict.edited_arguments if self.config.allow_edit else None
            modified = edited is not None and edited != dict(arguments or {})
            decision = HITLDecision(
                approved=True,
                reason=verdict.comment or "审计放行",
                arguments=edited if modified else None,
                modified=modified,
                auto=True,
                original_arguments=dict(arguments or {}) if modified else None,
            )
        decision.elapsed_ms = elapsed_ms
        # 落库（含改参后的完整参数）+ 事件 + 缓存
        await self._record(
            ctx, tool, arguments, tool_call_id, decision, mode_snapshot,
            comment=decision.reason, edited_arguments=decision.arguments if decision.modified else None,
        )
        await self._notify(ctx, tool, tool_call_id, decision, decided_by="audit_agent")
        self._cache_put(key, decision)
        return decision

    # ------------------------------------------------------------------
    async def _record(
        self,
        ctx: ToolContext,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        tool_call_id: str,
        decision: Any,
        mode_snapshot: str,
        *,
        comment: str,
        edited_arguments: dict[str, Any] | None = None,
    ) -> None:
        """审计裁决落库（decided_by=audit_agent）；失败不阻断解题。"""
        repos = getattr(ctx, "repos", None)
        repo = getattr(repos, "hitl_decisions", None) if repos is not None else None
        if repo is None:
            return
        try:
            record = await repo.save(
                conversation_id=ctx.conversation_id,
                tool_name=tool.name,
                reviewer="audit_agent",
                decision="approve" if decision.approved else "reject",
                tool_call_id=tool_call_id,
                run_id=ctx.run_id,
                arguments=dict(arguments or {}),
                mode=mode_snapshot,
                comment=comment,
                edited_arguments=edited_arguments,
                elapsed_ms=decision.elapsed_ms,
            )
            decision.decision_id = record["id"]
        except Exception:  # noqa: BLE001
            logger.warning("审计裁决落库失败（不影响执行）", exc_info=True)

    async def _notify(
        self,
        ctx: ToolContext,
        tool: ToolDefinition,
        tool_call_id: str,
        decision: Any,
        *,
        decided_by: str,
        cached: bool = False,
    ) -> None:
        data: dict[str, Any] = {
            "toolCallId": tool_call_id,
            "toolName": tool.name,
            "decision": "approve" if decision.approved else "reject",
            "comment": decision.reason,
            "decidedBy": decided_by,
            "elapsedMs": decision.elapsed_ms,
            "edited": bool(decision.modified),
            "cached": cached,
        }
        if decision.modified:
            # U6c 改参 diff：原参数 → 审计调整后的完整参数
            data["originalArguments"] = decision.original_arguments
            data["editedArguments"] = decision.arguments
        await ctx.notify("hitl_auto_decision", data)


__all__ = [
    "AUDIT_PROMPT",
    "AuditAgentReviewer",
    "CACHE_TTL_SECONDS",
    "Verdict",
    "extract_first_json_object",
    "parse_verdict",
]
