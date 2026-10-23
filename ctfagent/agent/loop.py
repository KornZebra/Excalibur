"""核心 ReAct 循环（function calling + 工具结果回填）。

实现契约见提示词 §7：

1. 组装上下文（历史 + 黑板索引 + 角色提示）；
2. 调模型（失败：退避重试 → failover → 上下文压缩重试）；
3. **软失败协议**：任何工具错误都不抛异常，而是以 ``[TOOL_ERROR]`` 文本回填后继续循环；
4. **防挂死**：硬超时 + 连续无输出终止（在 executor 中），超时返回 execution_id 供续等；
5. **HITL 拦截**：白名单放行 / 审批 / 超时拒绝，拒绝理由回填模型；
6. **输出治理**：单条结果超限落盘 + ``<persisted-output>``；
7. 结束：finalizer 证据校验（不足则自动续跑）→ 生成 trace → 返回 RunResult。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..events import EventType
from ..models.resilience import ContextOverflowError, ModelClient
from ..scope import fact_scope
from ..models.types import ChatMessage, ModelResponse, ToolCall, ToolOutcome, Usage
from ..storage.repos import Repos, new_id
from ..tools.context import ToolContext
from ..tools.execution_service import ExecutionService
from ..tools.guard import command_preview, soft_failure
from ..tools.registry import ToolDefinition, ToolRegistry
from ..tools.yaml_loader import build_argv
from .context import (
    ContextPolicy,
    build_context,
    compress_messages,
    context_stats,
    estimate_messages_tokens,
)
from .finalizer import Finalizer
from .hitl import HITLManager, rejected_outcome
from .prompts import build_system_prompt
from .summarizer import IntentLedger, summarize_history

logger = logging.getLogger(__name__)


class _RoundInterrupted(Exception):
    """用户请求中断当前推理轮（「中断并继续」）。

    不是任务取消：AgentRunner 捕获后注入引导消息并在同一任务内续跑下一轮。
    """

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

#: 单次运行内允许的上下文压缩次数
MAX_COMPRESSIONS = 4

#: 视为"输出被截断"的 finish_reason（length=达到 max_completion_tokens；
#: degenerate_repetition=模型陷入重复叙述被流式层提前中止）
TRUNCATION_FINISH_REASONS = frozenset({"length", "degenerate_repetition"})
#: 截断后最多自动续跑次数（每次续跑都要再烧一次完整生成长度，必须设上限）
MAX_TRUNCATION_CONTINUES = 2
#: 截断响应落库/进历史的保留长度（垃圾重复块整段回灌会持续污染后续上下文）
TRUNCATED_KEEP_CHARS = 4000

#: 内置黑板提醒：先于第 N 轮、且工具调用达到阈值仍零黑板写入时注入（每 run 最多 2 次）
BLACKBOARD_REMINDER_MIN_ITERATION = 5
BLACKBOARD_REMINDER_MIN_TOOL_CALLS = 3
BLACKBOARD_REMINDER_INTERVAL = 5
BLACKBOARD_REMINDER_MAX = 2

TRUNCATION_CONTINUE_PROMPT = """你的上一条回复达到最大生成长度被强制截断，且没有发出任何工具调用——这通常意味着陷入了重复叙述。
请立刻纠正：
1. 直接发起工具调用推进任务（工具调用不需要先写在正文里）；
2. 正文只写必要的简短分析，不要复述已确认的内容，不要重复之前的计划。"""


async def _noop_emit(event_type: str, data: dict[str, Any]) -> None:
    return None


@dataclass
class RunResult:
    """一轮 Agent 运行的产物。"""

    run_id: str
    conversation_id: str
    agent_id: str = "ctf-agent"
    content: str = ""
    status: str = "completed"
    iterations: int = 0
    usage: Usage = field(default_factory=Usage)
    trace_path: str | None = None
    error: str | None = None
    flags: list[str] = field(default_factory=list)
    todos: list[dict[str, Any]] = field(default_factory=list)
    compressions: int = 0
    hitl_rejections: int = 0
    tool_calls: int = 0

    @property
    def solved(self) -> bool:
        return bool(self.flags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "conversationId": self.conversation_id,
            "agentId": self.agent_id,
            "content": self.content,
            "status": self.status,
            "iterations": self.iterations,
            "usage": self.usage.model_dump(),
            "tracePath": self.trace_path,
            "error": self.error,
            "flags": self.flags,
            "todos": self.todos,
            "compressions": self.compressions,
            "hitlRejections": self.hitl_rejections,
            "toolCalls": self.tool_calls,
        }


class AgentRunner:
    """执行一次完整的 ReAct 运行。"""

    def __init__(
        self,
        *,
        config: AppConfig,
        model: ModelClient,
        registry: ToolRegistry,
        execution_service: ExecutionService,
        repos: Repos,
        hitl: HITLManager,
        finalizer: Finalizer | None = None,
    ) -> None:
        self.config = config
        self.model = model
        self.registry = registry
        self.execution_service = execution_service
        self.repos = repos
        self.hitl = hitl
        self.finalizer = finalizer or Finalizer(config)

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    async def run(
        self,
        user_message: str,
        *,
        conversation_id: str | None = None,
        challenge_id: str | None = None,
        agent_id: str = "ctf-agent",
        agent_prompt: str | None = None,
        system_prompt: str | None = None,
        tool_names: list[str] | None = None,
        max_iterations: int | None = None,
        emit: EmitFn | None = None,
        cancel_event: asyncio.Event | None = None,
        is_sub_agent: bool = False,
        new_conversation: bool = False,
        conversation_title: str | None = None,
        parent_conversation_id: str | None = None,
        hitl_reviewer: str | None = None,
        blackboard_conversation_id: str | None = None,
        run_id: str | None = None,
        services: dict[str, Any] | None = None,
        state_block: str | None = None,
        dynamic_context: Callable[[], Awaitable[str]] | None = None,
        attachment_ids: list[str] | None = None,
        steering: Any = None,
        pre_persisted_user_message_id: str | None = None,
    ) -> RunResult:
        """执行一次运行。

        ``steering``（运行中引导）：非空时提供 ``drain(conversation_id, repos)`` 与
        ``has_pending(conversation_id)``。主循环在每个**安全点**（一轮结束、下一次
        模型请求构建之前）原子取出引导队列，把积压合并为一条 user 消息注入上下文；
        自然收尾前若队列仍有积压则开启新段继续，而不是结束任务。

        ``pre_persisted_user_message_id``（与 steering 配套）：统一消息入口已把触发
        消息落库，运行内跳过重复落库、只补发 ``message_saved`` 事件。
        """
        emit = emit or _noop_emit
        cancel_event = cancel_event or asyncio.Event()
        run_id = run_id or new_id("run_")
        agent_cfg = self.config.agent
        if max_iterations is None:
            max_iterations = agent_cfg.sub_agent_max_iterations if is_sub_agent else agent_cfg.max_iterations

        # ---------------- 会话 ----------------
        if new_conversation:
            conversation = await self.repos.conversations.create(
                title=conversation_title or (user_message[:40] or "子代理任务"),
                challenge_id=challenge_id,
                agent_id=agent_id,
                meta={"parentConversationId": parent_conversation_id, "subAgent": True},
                hitl_reviewer=hitl_reviewer,
            )
        else:
            conversation = await self.repos.conversations.get_or_create(
                conversation_id,
                title=(conversation_title or user_message[:60] or "新会话"),
                challenge_id=challenge_id,
                agent_id=agent_id,
                hitl_reviewer=hitl_reviewer,
            )
        conversation_id = conversation["id"]
        challenge = await self.repos.challenges.get(challenge_id) if challenge_id else None

        # U17 会话隔离：未绑定题目的会话使用**会话私有**工作区，不再共享 _default。
        # 子代理（new_conversation + parent_conversation_id）必须继承父会话的工作区，
        # 否则它会在自己的空目录里工作，看不到父代理的附件与产物。
        workspace_owner = parent_conversation_id or conversation_id
        run_workspace = self.config.run_workspace(challenge_id, workspace_owner)

        await emit(
            EventType.CONVERSATION,
            {"conversationId": conversation_id, "runId": run_id, "challengeId": challenge_id},
        )

        # ---------------- 上下文 ----------------
        policy = ContextPolicy(
            max_history_messages=agent_cfg.max_history_messages,
            max_total_tokens=max(4_000, self.model.channel.max_total_tokens),
            reserve_completion_tokens=self.model.channel.max_completion_tokens,
        )
        history = await self.repos.messages.to_chat_messages(
            conversation_id,
            limit=max(50, policy.max_history_messages * 2),
            # 引导运行：尚未在安全点注入的 queued 消息不属于本轮上下文
            exclude_queued=steering is not None,
        )
        ledger = IntentLedger()
        ledger.extract_from_messages(history)
        ledger.record(user_message, source="user")

        # U18：黑板按「题目」隔离——绑定题目时注入题目作用域，否则用会话作用域
        blackboard_scope = fact_scope(challenge_id, blackboard_conversation_id or conversation_id)
        blackboard_index = (
            await self.repos.facts.index(
                blackboard_scope,
                limit=self.config.blackboard.inject_index_limit,
            )
            if self.config.blackboard.enabled
            else []
        )
        tools = self._select_tools(tool_names)
        schemas = [t.openai_schema() for t in tools]
        allowed_names = {t.name for t in tools}

        prompt = build_system_prompt(
            config=self.config,
            agent_prompt=agent_prompt or system_prompt,
            # U17 会话隔离：未绑定题目的会话使用会话私有工作区，不再共享 _default
            workspace=str(run_workspace),
            conversation_id=conversation_id,
            challenge=challenge,
            blackboard_index=blackboard_index,
            tool_index=self._render_tool_index(tools),
            is_sub_agent=is_sub_agent,
            extra=("【本轮唯一子目标（来自父代理交接包）】\n" + user_message) if is_sub_agent else "",
            # v2：渲染式状态块（None/"" 时提示词与 v1 完全一致）
            state_block=state_block or "",
            # 引导：仅主代理运行且启用 steering 时告知模型"用户可能在任务中追加指令"
            steering_enabled=steering is not None and not is_sub_agent,
        )

        # 统一入口已落库的触发消息就在历史里（最后一条 user），不再追加一份
        messages = build_context(
            system_prompt=prompt,
            history=history,
            user_message=None if pre_persisted_user_message_id else user_message,
            policy=policy,
        )

        # ---------------- 运行记录 ----------------
        await self.repos.runs.create(
            run_id=run_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            orchestration="sub_agent" if is_sub_agent else "single",
        )
        user_message_id = pre_persisted_user_message_id or ""
        if pre_persisted_user_message_id:
            # 消息已由统一入口落库，这里只补发确认事件（前端时间线依赖该事件对齐）
            await emit(
                EventType.MESSAGE_SAVED,
                {
                    "messageId": pre_persisted_user_message_id,
                    "role": "user",
                    "content": user_message[:2000],
                },
            )
        else:
            user_message_id = await self._save_message(conversation_id, ChatMessage.user(user_message), emit)
        if attachment_ids and user_message_id:
            # U2a：把上传的附件归属到这条用户消息（失败不影响解题）
            try:
                await self.repos.attachments.link(user_message_id, list(attachment_ids))
            except Exception:  # noqa: BLE001 - 附件关联失败不应中断解题
                logger.warning("附件关联到消息失败", exc_info=True)
        await emit(
            "progress",
            {"stage": "start", "message": f"开始解题循环（最多 {max_iterations} 步）", "iteration": 0},
        )

        ctx = ToolContext(
            config=self.config,
            repos=self.repos,
            registry=self.registry,
            execution_service=self.execution_service,
            conversation_id=conversation_id,
            run_id=run_id,
            agent_id=agent_id,
            challenge_id=challenge_id,
            # 工作区在 run 开始时确定（子代理继承父会话的工作区），保证工具与提示词一致
            workspace=run_workspace,
            # run 启动时的会话级审查模式快照（HITL 拦截时会优先现读库中最新值，
            # 快照仅作现读失败时的回退）；子代理会话已在创建时继承父会话的审查模式
            hitl_reviewer=conversation.get("hitlReviewer"),
            # 黑板共享 scope：子代理派单时指向父会话（事实共享），普通 run 为 None
            blackboard_conversation_id=blackboard_conversation_id,
            user_message=user_message,
            emit=emit,
            cancel_event=cancel_event,
            services=dict(services or {}),
        )

        result = RunResult(run_id=run_id, conversation_id=conversation_id, agent_id=agent_id, trace_path=None)
        trace: list[dict[str, Any]] = []
        counters = {
            "blackboard_writes": 0,
            "tool_calls": 0,
            "flag_submitted": False,
            "report_generated": False,
            "compressions": 0,
            "iteration": 0,
        }
        finalizer_retries = 0
        truncation_retries = 0
        blackboard_reminders = 0
        last_blackboard_reminder_at = -100
        compressions = 0
        last_content = ""
        status = "completed"
        error: str | None = None
        iteration = 0
        #: 「中断并继续」事件（无 steering / 非会话任务时为 None）
        interrupt_event = (
            steering.interrupt_event(conversation_id) if steering is not None else None
        )

        # ---------------- 主循环 ----------------
        try:
            while iteration < max_iterations:
                iteration += 1
                counters["iteration"] = iteration
                if cancel_event.is_set():
                    status = "cancelled"
                    await emit(EventType.CANCELLED, {"reason": "用户请求停止"})
                    break

                # ---- 安全点（Steering）：上一轮（含工具）已结束，下一轮请求构建之前 ----
                # 取消时先于 drain 判定：被取消则任务结束，消息保持 queued（不出现
                # "已置 delivered 却没进模型上下文"的窗口）。
                if steering is not None:
                    await self._inject_steering(
                        steering, conversation_id=conversation_id, messages=messages,
                        iteration=iteration, emit=emit,
                    )
                    await emit(EventType.ROUND_STARTED, {"round": iteration})

                # v2：逐轮动态上下文（黑板的 Obsver 提醒 / 运行中插入的人工提示）。
                # dynamic_context=None 时不产生任何消息、不发任何事件 ⇒ v1 行为不变。
                if dynamic_context is not None:
                    try:
                        note = await dynamic_context()
                    except Exception:  # noqa: BLE001 - 动态上下文失败不能中断解题
                        logger.debug("dynamic_context 调用失败", exc_info=True)
                        note = ""
                    if note:
                        messages.append(ChatMessage.system(note))
                        await emit(
                            "progress",
                            {
                                "stage": "context_refresh",
                                "message": note.split("\n", 1)[0][:120],
                                "iteration": iteration,
                            },
                        )
                else:
                    # 内置黑板提醒（确定性兜底）：模型经常无视提示词里的"边做边记"，
                    # 在已有足够工具产出仍零黑板写入时注入一次定向提醒。
                    if (
                        counters["blackboard_writes"] == 0
                        and int(counters["tool_calls"]) >= BLACKBOARD_REMINDER_MIN_TOOL_CALLS
                        and iteration >= BLACKBOARD_REMINDER_MIN_ITERATION
                        and blackboard_reminders < BLACKBOARD_REMINDER_MAX
                        and iteration - last_blackboard_reminder_at >= BLACKBOARD_REMINDER_INTERVAL
                    ):
                        blackboard_reminders += 1
                        last_blackboard_reminder_at = iteration
                        note = (
                            f"【黑板提醒】本轮已调用 {counters['tool_calls']} 次工具，但黑板（blackboard）"
                            "至今没有任何记录——解题中断线重连、跨代理协作与结束校验都依赖它。"
                            "请**立即**调用 blackboard_upsert 把至今的关键认知落盘"
                            "（key 用稳定短键，summary 一行写「什么+在哪+如何验证」，body 写细节与证据），"
                            "然后继续解题。"
                        )
                        messages.append(ChatMessage.system(note))
                        await emit(
                            "progress",
                            {
                                "stage": "blackboard_reminder",
                                "message": note.split("\n", 1)[0][:120],
                                "iteration": iteration,
                            },
                        )

                await emit(
                    "progress",
                    {
                        "stage": "iteration",
                        "message": f"第 {iteration}/{max_iterations} 步",
                        "iteration": iteration,
                    },
                )

                try:
                    response, messages = await self._call_model(
                        messages,
                        schemas,
                        emit=emit,
                        policy=policy,
                        ledger=ledger,
                        counters=counters,
                        interrupt_event=interrupt_event,
                    )
                except _RoundInterrupted:
                    # 用户中断当前轮：安全点（主循环顶部）会取出引导队列注入，续跑下一轮
                    interrupt_event.clear()
                    await emit(
                        "progress",
                        {
                            "stage": "round_interrupted",
                            "message": "已中断当前执行，注入用户引导消息后继续解题",
                            "iteration": iteration,
                        },
                    )
                    continue
                compressions = int(counters.get("compressions", compressions))
                result.usage.accumulate(response.usage)
                await emit(
                    EventType.USAGE_SUMMARY,
                    {
                        "modelCalls": result.usage.model_calls,
                        "promptTokens": result.usage.prompt_tokens,
                        "completionTokens": result.usage.completion_tokens,
                        "totalTokens": result.usage.total_tokens,
                    },
                )
                trace.append(
                    {
                        "ts": time.time(),
                        "iteration": iteration,
                        "type": "model_response",
                        "content": response.content[:2000],
                        "toolCalls": [tc.name for tc in response.tool_calls],
                        "usage": response.usage.model_dump(),
                    }
                )

                # ---- 输出截断治理（finish_reason=length / degenerate_repetition）----
                # 不处理的话：截断文本被当"纯文本回复"→ finalizer 自动续跑 → 模型重新生成
                # 同样的长篇内容 → 再截断，形成"Let me ..."式无限循环且烧尽生成长度预算。
                truncated = response.finish_reason in TRUNCATION_FINISH_REASONS
                if truncated:
                    if response.tool_calls:
                        # 参数 JSON 被截断的调用解析结果不可靠（parse_arguments 得到 {"_raw": …}），
                        # 带残缺参数执行比丢弃更危险，这里统一丢弃
                        valid = [tc for tc in response.tool_calls if "_raw" not in (tc.arguments or {})]
                        dropped = len(response.tool_calls) - len(valid)
                        if dropped:
                            response = response.model_copy(update={"tool_calls": valid})
                            await emit(
                                "progress",
                                {
                                    "stage": "output_truncated",
                                    "message": f"输出被截断，已丢弃 {dropped} 个参数不完整的工具调用",
                                    "iteration": iteration,
                                },
                            )
                    if len(response.content) > TRUNCATED_KEEP_CHARS:
                        # 截断的超长文本（常见为重复叙述块）截断保存，避免整段回灌后续上下文
                        response = response.model_copy(
                            update={
                                "content": response.content[:TRUNCATED_KEEP_CHARS]
                                + "\n…[输出因达到生成长度上限被截断，已截断保存]"
                            }
                        )

                assistant_message = response.to_message()
                messages.append(assistant_message)
                await self._save_message(conversation_id, assistant_message, emit)
                if response.content.strip() and not truncated:
                    last_content = response.content

                if not response.tool_calls:
                    if truncated:
                        truncation_retries += 1
                        if truncation_retries > MAX_TRUNCATION_CONTINUES:
                            status = "error"
                            error = (
                                "输出连续多次达到生成长度上限（疑似陷入重复叙述），本轮已停止。"
                                "可提高通道 max_completion_tokens 后重试，或要求模型直接调用工具而非长篇叙述。"
                            )
                            await emit(EventType.ERROR, {"message": error})
                            break
                        await emit(
                            "progress",
                            {
                                "stage": "output_truncated",
                                "message": (
                                    f"模型输出被截断（{response.finish_reason}），"
                                    f"第 {truncation_retries}/{MAX_TRUNCATION_CONTINUES} 次要求其直接发起工具调用"
                                ),
                                "iteration": iteration,
                            },
                        )
                        continuation = ChatMessage.user(TRUNCATION_CONTINUE_PROMPT)
                        messages.append(continuation)
                        await self._save_message(conversation_id, continuation, emit)
                        trace.append(
                            {"ts": time.time(), "iteration": iteration, "type": "truncated_continue"}
                        )
                        continue
                    decision = self.finalizer.check(
                        response=response,
                        iterations=iteration,
                        retries_used=finalizer_retries,
                        blackboard_writes=counters["blackboard_writes"],
                        tool_calls_made=counters["tool_calls"],
                        flag_submitted=bool(counters["flag_submitted"]),
                        report_generated=bool(counters["report_generated"]),
                        flag_pattern=(challenge or {}).get("flagPattern") or "",
                    )
                    if decision.continue_run:
                        finalizer_retries += 1
                        await emit(
                            "progress",
                            {
                                "stage": "finalizer",
                                "message": "证据不足，自动续跑：" + "；".join(decision.reasons)
                                if decision.reasons
                                else "证据不足，自动续跑",
                                "iteration": iteration,
                            },
                        )
                        continuation = ChatMessage.user(decision.message or "请继续推进解题。")
                        messages.append(continuation)
                        await self._save_message(conversation_id, continuation, emit)
                        trace.append(
                            {"ts": time.time(), "iteration": iteration, "type": "finalizer_continue"}
                        )
                        continue
                    # 引导收尾规则：模型给出最终回答时若队列仍有积压，不结束任务，
                    # 开启新段继续（下一个安全点注入），直到队列清空且无排队指令。
                    if steering is not None and not cancel_event.is_set():
                        if await steering.has_pending(conversation_id):
                            trace.append(
                                {"ts": time.time(), "iteration": iteration, "type": "steer_continue"}
                            )
                            continue
                    break

                if response.content.strip():
                    await emit(EventType.PLANNING, {"content": response.content})

                for tool_call in response.tool_calls:
                    if interrupt_event is not None and interrupt_event.is_set():
                        interrupt_event.clear()
                        await emit(
                            "progress",
                            {
                                "stage": "round_interrupted",
                                "message": "已中断本轮剩余工具执行，注入用户引导消息后继续解题",
                                "iteration": iteration,
                            },
                        )
                        break
                    if cancel_event.is_set():
                        status = "cancelled"
                        break
                    content, is_error, execution_id, persisted = await self._handle_tool_call(
                        tool_call, ctx=ctx, allowed_names=allowed_names, emit=emit, counters=counters
                    )
                    tool_message = ChatMessage.tool(tool_call.id, content, name=tool_call.name)
                    messages.append(tool_message)
                    await self._save_message(conversation_id, tool_message, emit)
                    trace.append(
                        {
                            "ts": time.time(),
                            "iteration": iteration,
                            "type": "tool_result",
                            "tool": tool_call.name,
                            "arguments": tool_call.arguments,
                            "isError": is_error,
                            "executionId": execution_id,
                            "content": content[:2000],
                        }
                    )
                if status == "cancelled":
                    break

            else:
                status = "max_iterations"
                await emit(
                    "progress",
                    {
                        "stage": "max_iterations",
                        "message": f"达到最大迭代次数 {max_iterations}，停止本轮",
                        "iteration": iteration,
                    },
                )
        except ContextOverflowError as exc:
            status = "error"
            error = f"上下文压缩后仍超限：{exc}"
            await emit(EventType.ERROR, {"message": error})
        except asyncio.CancelledError:
            status = "cancelled"
            await emit(EventType.CANCELLED, {"reason": "运行被取消"})
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，保证事件与落库完整
            logger.exception("Agent 循环异常")
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
            await emit(EventType.ERROR, {"message": error})

        # ---------------- 收尾 ----------------
        result.iterations = iteration
        result.status = status
        result.error = error
        result.compressions = compressions
        result.tool_calls = counters["tool_calls"]
        flags = await self.repos.flags.list(conversation_id=conversation_id, limit=50)
        result.flags = [f["value"] for f in flags]
        result.todos = list(ctx.services.get("todos") or [])
        result.content = (last_content or "").strip() or self._fallback_content(status, counters, flags)

        trace_path = await self._write_trace(run_id, conversation_id, agent_id, trace, result)
        result.trace_path = trace_path
        await self.repos.traces.save(
            run_id=run_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            trace_path=trace_path or "",
            summary=result.content[:500],
            meta={"iterations": iteration, "status": status, "toolCalls": counters["tool_calls"]},
        )
        await self.repos.runs.finish(
            run_id,
            status=status,
            iterations=iteration,
            usage=result.usage,
            error=error,
        )
        await self.repos.conversations.touch(conversation_id)
        await emit(EventType.RESPONSE, {"content": result.content})
        await emit(
            EventType.DONE,
            {
                "runId": run_id,
                "conversationId": conversation_id,
                "status": status,
                "iterations": iteration,
                "flags": result.flags,
                "usage": result.usage.model_dump(),
                "tracePath": trace_path,
            },
        )
        return result

    # ------------------------------------------------------------------
    # 运行中引导（Steering）：安全点注入
    # ------------------------------------------------------------------
    async def _inject_steering(
        self,
        steering: Any,
        *,
        conversation_id: str,
        messages: list[ChatMessage],
        iteration: int,
        emit: EmitFn,
    ) -> None:
        """取出引导队列积压并合并为**一条** user 消息注入上下文（无积压则什么也不做）。

        - 队列只有本任务一个消费者，drain 原子（会话锁内取出 + 置 delivered），恰好一次；
        - 注入不落库（消息入队时已落库），仅进入本轮模型上下文；
        - 若上下文尾部已是 user 消息（本轮触发消息或 finalizer/截断续跑提示），合并进该条，
          避免给模型发连续多条 user 消息——历史还原（to_chat_messages）对相邻 user 行做
          同样合并，两边语义一致。
        """
        items = await steering.drain(conversation_id, self.repos)
        if not items:
            return
        merged = "\n\n".join(str(item.text) for item in items)
        tail = messages[-1] if messages else None
        if tail is not None and tail.role == "user":
            messages[-1] = ChatMessage.user((tail.content or "") + "\n\n" + merged)
        else:
            messages.append(ChatMessage.user(merged))
        await emit(
            EventType.MESSAGE_DELIVERED,
            {"ids": [item.id for item in items], "round": iteration},
        )
        logger.info(
            "会话 %s 第 %d 轮安全点注入 %d 条引导消息", conversation_id, iteration, len(items)
        )

    # ------------------------------------------------------------------
    # 模型调用（含上下文溢出压缩重试）
    # ------------------------------------------------------------------
    async def _call_model(
        self,
        messages: list[ChatMessage],
        schemas: list[dict[str, Any]],
        *,
        emit: EmitFn,
        policy: ContextPolicy,
        ledger: IntentLedger,
        counters: dict[str, Any],
        interrupt_event: asyncio.Event | None = None,
    ) -> tuple[ModelResponse, list[ChatMessage]]:
        """调模型；上下文超限时压缩历史后重试。"""
        compressions = int(counters.get("compressions", 0))
        while True:
            if estimate_messages_tokens(messages) > policy.prompt_budget and compressions < MAX_COMPRESSIONS:
                compressions += 1
                summary = await summarize_history(self.model, messages[1:-1], ledger=ledger)
                messages = compress_messages(
                    messages,
                    target_tokens=policy.target_tokens(),
                    policy=policy,
                    ledger=ledger,
                    summary=summary,
                    note=f"（第 {compressions} 次预压缩，原估算 {context_stats(messages)['estimatedTokens']} tokens）",
                )
                await emit(
                    "progress",
                    {
                        "stage": "compression",
                        "message": f"上下文接近上限，已预压缩（第 {compressions} 次）",
                        "stats": context_stats(messages),
                    },
                )
                counters["compressions"] = compressions

            await emit(EventType.RESPONSE_START, {"iteration": counters.get("iteration", 0)})

            async def _on_delta(delta: str) -> None:
                await emit(EventType.RESPONSE_DELTA, {"delta": delta})

            async def _on_reasoning(delta: str) -> None:
                await emit(EventType.THINKING_STREAM_DELTA, {"delta": delta})

            try:
                chat_task = asyncio.create_task(
                    self.model.chat(
                        messages, schemas, on_delta=_on_delta, on_reasoning=_on_reasoning
                    )
                )
                if interrupt_event is not None:
                    # 「中断并继续」竞速：用户发消息 → 取消当前推理/流式输出，
                    # 抛 _RoundInterrupted 让主循环注入引导消息后续跑。
                    evt_task = asyncio.create_task(interrupt_event.wait())
                    done, _pending = await asyncio.wait(
                        {chat_task, evt_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if chat_task not in done:
                        evt_task.cancel()
                        chat_task.cancel()
                        try:
                            await chat_task
                        except BaseException:  # noqa: BLE001 - 取消中再抛一律吞掉
                            pass
                        raise _RoundInterrupted()
                    evt_task.cancel()
                    response = chat_task.result()
                else:
                    response = await chat_task
                counters["compressions"] = compressions
                return response, messages
            except ContextOverflowError:
                if compressions >= MAX_COMPRESSIONS:
                    raise
                compressions += 1
                summary = await summarize_history(self.model, messages[1:-1], ledger=ledger)
                before = context_stats(messages)["estimatedTokens"]
                messages = compress_messages(
                    messages,
                    target_tokens=policy.target_tokens(),
                    policy=policy,
                    ledger=ledger,
                    summary=summary,
                    note=f"（通道报告上下文超限，第 {compressions} 次压缩；压缩前 {before} tokens）",
                )
                counters["compressions"] = compressions
                await emit(
                    "progress",
                    {
                        "stage": "compression",
                        "message": f"通道报告上下文超限，压缩历史后重试（第 {compressions} 次）",
                        "stats": context_stats(messages),
                    },
                )

    # ------------------------------------------------------------------
    # 工具调用处理
    # ------------------------------------------------------------------
    async def _handle_tool_call(
        self,
        tool_call: ToolCall,
        *,
        ctx: ToolContext,
        allowed_names: set[str],
        emit: EmitFn,
        counters: dict[str, Any],
    ) -> tuple[str, bool, str | None, str | None]:
        """执行一次工具调用，返回 ``(内容, is_error, execution_id, persisted_file)``。"""
        tool: ToolDefinition | None = self.registry.get(tool_call.name)
        await emit(
            EventType.TOOL_CALL,
            {
                "toolCallId": tool_call.id,
                "name": tool_call.name,
                "arguments": tool_call.arguments,
                "agent": ctx.agent_id,
            },
        )

        if tool is None:
            content = soft_failure(
                f"工具未注册：{tool_call.name}",
                tool=tool_call.name,
                hint="用 tool_search 检索可用工具，或改用 bash / python_exec 完成同等操作。",
            )
            await self._emit_tool_result(emit, tool_call, content, True, None, None)
            return content, True, None, None

        if tool_call.name not in allowed_names:
            content = soft_failure(
                f"工具不在当前代理的可用列表中：{tool_call.name}",
                tool=tool_call.name,
                hint="该工具仅主代理可用（如 task）；请改用你被授权的工具。",
            )
            await self._emit_tool_result(emit, tool_call, content, True, None, None)
            return content, True, None, None

        if not tool.enabled:
            content = soft_failure(f"工具已被禁用：{tool_call.name}", tool=tool_call.name)
            await self._emit_tool_result(emit, tool_call, content, True, None, None)
            return content, True, None, None

        command = self._command_for(tool, tool_call.arguments, ctx)
        counters["tool_calls"] = int(counters.get("tool_calls", 0)) + 1

        # -- HITL 拦截 --
        decision = await self.hitl.intercept(
            tool=tool,
            arguments=tool_call.arguments,
            tool_call_id=tool_call.id,
            ctx=ctx,
            command=command,
        )
        if decision.rejected:
            outcome = rejected_outcome(decision, tool.name)
            await self._emit_tool_result(emit, tool_call, outcome.content, True, None, None)
            return outcome.content, True, None, None
        arguments = decision.arguments if (decision.modified and decision.arguments) else tool_call.arguments
        if decision.modified and decision.arguments:
            command = self._command_for(tool, arguments, ctx)

        # -- 执行 --
        try:
            wait_result = await self.execution_service.submit_and_wait(tool, arguments, ctx, command=command)
            content, is_error = wait_result.as_tool_content()
            record = wait_result.record
            execution_id = record.id
            persisted = record.persisted_file
        except Exception as exc:  # noqa: BLE001 - 兜底软失败，绝不让单工具失败中断循环
            logger.warning("工具 %s 执行包装失败", tool.name, exc_info=True)
            content = soft_failure(
                f"工具执行异常：{type(exc).__name__}: {exc}",
                tool=tool.name,
                hint="可换用等价工具或 bash 重试。",
            )
            is_error, execution_id, persisted = True, None, None

        # -- U6b 审计闭环：放行的调用执行完成后回写结果摘要（失败不影响主流程） --
        if decision.decision_id:
            await self.hitl.attach_execution(decision.decision_id, execution_id, content)

        # -- 计数（供 finalizer 判定证据是否充分） --
        if not is_error:
            if tool.name in ("blackboard_upsert", "memory_write", "idea_upsert", "idea_transition"):
                # 黑板（facts）与挑战账本（memories/ideas）都算"已落盘认知"
                counters["blackboard_writes"] = int(counters.get("blackboard_writes", 0)) + 1
            elif tool.name == "flag_submit":
                counters["flag_submitted"] = True
            elif tool.name == "report_generate":
                counters["report_generated"] = True

        await self._emit_tool_result(emit, tool_call, content, is_error, execution_id, persisted)
        self.execution_service.cleanup(keep=200)
        return content, is_error, execution_id, persisted

    async def _emit_tool_result(
        self,
        emit: EmitFn,
        tool_call: ToolCall,
        content: str,
        is_error: bool,
        execution_id: str | None,
        persisted_file: str | None,
    ) -> None:
        await emit(
            EventType.TOOL_RESULT,
            {
                "toolCallId": tool_call.id,
                "name": tool_call.name,
                "result": content,
                "isError": is_error,
                "executionId": execution_id,
                "persistedFile": persisted_file,
            },
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _select_tools(self, tool_names: list[str] | None) -> list[ToolDefinition]:
        if tool_names is not None:
            wanted = set(tool_names)
            return [t for t in self.registry.all() if t.name in wanted and t.enabled]
        return self.registry.visible_tools()

    def _render_tool_index(self, tools: list[ToolDefinition]) -> str:
        lines = [f"- {t.name}: {t.short_description or (t.description or '')[:70]}" for t in tools]
        text = "\n".join(lines)
        if self.registry.expose_all:
            return text
        hidden = len(self.registry.all()) - len(tools)
        if hidden > 0:
            text += f"\n- （另有 {hidden} 个工具未展开，可用 tool_search 按关键词检索解锁）"
        return text

    def _command_for(self, tool: ToolDefinition, arguments: dict[str, Any], ctx: ToolContext) -> str:
        recipe = tool.metadata.get("recipe")
        if recipe is not None:
            try:
                argv = build_argv(
                    recipe,
                    arguments or {},
                    project_root=self.config.project_root,
                    workspace=ctx.resolved_workspace(),
                )
                return command_preview(argv)
            except Exception:  # noqa: BLE001 - 预览失败不影响执行
                return ""
        command = arguments.get("command")
        if command:
            return str(command)
        if tool.name == "python_exec":
            return "python3 <snippet>" if arguments.get("code") else f"python3 {arguments.get('script', '')}"
        return ""

    async def _save_message(self, conversation_id: str, message: ChatMessage, emit: EmitFn) -> str | None:
        """落库并广播；返回消息 ID（失败返回 None；调用方可忽略返回值）。"""
        try:
            saved = await self.repos.messages.append(conversation_id, message)
        except Exception:  # noqa: BLE001 - 落库失败不应中断解题
            logger.warning("消息落库失败", exc_info=True)
            return None
        await emit(
            EventType.MESSAGE_SAVED,
            {
                "messageId": saved.get("id"),
                "role": message.role,
                "content": message.content[:2000],
            },
        )
        return saved.get("id")

    def _fallback_content(self, status: str, counters: dict[str, Any], flags: list[dict[str, Any]]) -> str:
        if flags:
            return "## 解题结论\n已提交 flag：" + ", ".join(f"`{f['value']}`" for f in flags)
        if status == "max_iterations":
            return (
                "## 当前阶段\n达到最大迭代次数，本轮暂停（可继续对话让我接着解题）。\n"
                f"## 已确认事实(证据)\n本轮调用工具 {counters.get('tool_calls', 0)} 次，"
                "详见黑板（blackboard_list）与工具卡片。\n## 下一步\n请指示是否继续，或补充题目目标信息。"
            )
        if status == "cancelled":
            return "本轮运行已被取消。已确认的事实均已写入黑板，可随时让我继续。"
        return "本轮未产生文本结论，请查看工具卡片与黑板索引。"

    async def _write_trace(
        self,
        run_id: str,
        conversation_id: str,
        agent_id: str,
        trace: list[dict[str, Any]],
        result: RunResult,
    ) -> str | None:
        """把 agent_trace 写成 JSONL（供续跑与报告）。"""
        try:
            trace_dir: Path = self.config.workspace_root / "_traces"
            trace_dir.mkdir(parents=True, exist_ok=True)
            path = trace_dir / f"{run_id}.jsonl"
            lines = [
                json.dumps(
                    {
                        "runId": run_id,
                        "conversationId": conversation_id,
                        "agentId": agent_id,
                        **entry,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                for entry in trace
            ]
            lines.append(
                json.dumps(
                    {
                        "runId": run_id,
                        "type": "run_result",
                        "status": result.status,
                        "iterations": result.iterations,
                        "usage": result.usage.model_dump(),
                        "flags": result.flags,
                    },
                    ensure_ascii=False,
                )
            )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return str(path)
        except Exception:  # noqa: BLE001 - trace 失败不影响主流程
            logger.warning("写入 agent trace 失败", exc_info=True)
            return None


__all__ = ["AgentRunner", "EmitFn", "RunResult", "ToolOutcome"]
