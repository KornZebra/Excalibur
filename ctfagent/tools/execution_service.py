"""ExecutionService：工具执行的状态机与 execution_id 生命周期。

状态机：``queued → running → completed | failed | cancelled | hard_timeout``

要点（设计宪法第 3 条）：
- **对模型同步、对系统异步**：``submit`` 立刻创建后台 asyncio task 并返回全局唯一 ``execution_id``；
- ``wait`` 到期只返回快照，**不取消**后台任务；
- 模型可用元工具 ``wait_tool_execution`` 续等、``cancel_tool_execution`` 取消、``get_tool_execution`` 看进度；
- 并发由信号量 ``max_concurrent_tools`` 限制。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..models.types import ToolOutcome
from ..storage.repos import Repos, new_id
from ..textsafe import clean_text, clean_value
from .context import ToolContext
from .executor import ProcessExecutor, ProcessResult
from .guard import command_preview, guard_output, process_result_text, soft_failure, wrap_exception
from .registry import ToolDefinition, ToolRegistry
from .yaml_loader import ToolRecipe, build_argv

logger = logging.getLogger(__name__)

#: 等待超时后回给模型的部分输出长度
PARTIAL_OUTPUT_CHARS = 4_096

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "hard_timeout", "no_output_timeout"}


@dataclass
class ExecutionRecord:
    """一次工具执行的完整状态。"""

    id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    command: str = ""
    conversation_id: str | None = None
    challenge_id: str | None = None
    run_id: str = ""
    agent_id: str = "ctf-agent"
    status: str = "queued"
    outcome: ToolOutcome | None = None
    exit_code: int | None = None
    error: str | None = None
    persisted_file: str | None = None
    log_file: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    duration_ms: int | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    #: 内建/MCP handler 是进程内 asyncio 任务，可被 hard 取消直接打断；
    #: yaml 配方走 cancel_event 协作终止（executor 轮询后 kill 进程组），不能粗暴 task.cancel()
    handler_cancellable: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def summary(self, *, include_output: bool = True, tail_chars: int = 1_200) -> dict[str, Any]:
        """对外快照（不含 asyncio 对象）。"""
        data: dict[str, Any] = {
            "executionId": self.id,
            "toolName": self.tool_name,
            "status": self.status,
            "command": self.command,
            "exitCode": self.exit_code,
            "isError": bool(self.outcome and self.outcome.is_error) or self.status == "failed",
            "error": self.error,
            "persistedFile": self.persisted_file,
            "logFile": self.log_file,
            "elapsedMs": int(self.elapsed * 1000),
            "durationMs": self.duration_ms,
            "agent": self.agent_id,
        }
        if include_output and self.outcome is not None:
            # 快照会经 get_tool_execution 回填模型，同样需要清洗代理字符
            content = clean_text(self.outcome.content or "")
            data["output"] = content
            if len(content) > tail_chars:
                data["outputTail"] = content[-tail_chars:]
        return data


@dataclass
class WaitResult:
    """``wait`` 的返回值。"""

    record: ExecutionRecord
    timed_out: bool = False

    @property
    def outcome(self) -> ToolOutcome | None:
        return self.record.outcome

    def as_tool_content(self) -> tuple[str, bool]:
        """转成回填给模型的内容与 is_error 标记。

        这里是**所有工具结果回填模型的唯一出口**，统一做一次孤立代理字符清洗：
        工具可能返回无法用 UTF-8 编码的文本（典型：题目压缩包内文件名是原始/加密
        字节，``os.listdir`` 以 surrogateescape 解码后带出 U+D800–U+DFFF）。若不清洗，
        该文本会在下一轮组装 LLM 请求体时触发
        ``UnicodeEncodeError: surrogates not allowed``，直接导致整轮解题失败。
        """
        record = self.record
        if self.timed_out:
            tail = (record.outcome.content if record.outcome else "") or ""
            body = (
                f"[执行中] execution_id={record.id} 工具 {record.tool_name} 仍在后台运行"
                f"（已运行 {record.elapsed:.0f}s）。\n"
                f'可用 wait_tool_execution(execution_id="{record.id}", timeout_seconds=120) 继续等待；'
                f'get_tool_execution(execution_id="{record.id}") 查看当前输出；'
                f'cancel_tool_execution(execution_id="{record.id}") 取消。\n'
            )
            if tail:
                body += f"\n--- 当前部分输出（末尾 {PARTIAL_OUTPUT_CHARS} 字符）---\n{tail[-PARTIAL_OUTPUT_CHARS:]}"
            return clean_text(body), False
        if record.outcome is None:
            return (
                clean_text(
                    soft_failure(f"执行 {record.id} 结束但无结果（状态={record.status}）", tool=record.tool_name)
                ),
                True,
            )
        return clean_text(record.outcome.content), record.outcome.is_error


class ExecutionService:
    """工具执行服务。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        executor: ProcessExecutor | None = None,
        repos: Repos | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.config = config
        self.repos = repos
        self.registry = registry
        self.executor = executor or ProcessExecutor(
            tool_timeout=config.agent.tool_timeout_seconds,
            no_output_timeout=config.agent.no_output_timeout_seconds,
        )
        self._records: dict[str, ExecutionRecord] = {}
        self._semaphore = asyncio.Semaphore(max(1, config.agent.max_concurrent_tools))
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # 提交 / 等待 / 取消
    # ------------------------------------------------------------------
    def submit(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        ctx: ToolContext,
        *,
        command: str = "",
    ) -> ExecutionRecord:
        """创建后台执行任务并立刻返回记录（状态机 queued→running）。"""
        execution_id = new_id("exec_")
        record = ExecutionRecord(
            id=execution_id,
            tool_name=tool.name,
            arguments=dict(arguments or {}),
            command=command,
            conversation_id=ctx.conversation_id,
            challenge_id=ctx.challenge_id,
            run_id=ctx.run_id,
            agent_id=ctx.agent_id,
            status="queued",
            handler_cancellable=tool.handler is not None,
        )
        self._records[execution_id] = record
        task = asyncio.create_task(
            self._guarded_execute(record, tool, arguments, ctx), name=f"tool:{tool.name}:{execution_id}"
        )
        record.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record

    async def _guarded_execute(
        self, record: ExecutionRecord, tool: ToolDefinition, arguments: dict[str, Any], ctx: ToolContext
    ) -> None:
        """信号量受限地执行工具，任何异常都转成软失败。"""
        async with self._semaphore:
            if record.cancel_event.is_set():
                record.status = "cancelled"
                record.outcome = ToolOutcome.error(soft_failure("执行在排队阶段被取消。", tool=tool.name))
                record.finished_at = time.time()
                self._persist(record)
                return
            record.status = "running"
            record.started_at = time.time()
            try:
                outcome = await self.execute_tool(tool, arguments, ctx, record=record)
            except asyncio.CancelledError:  # pragma: no cover - 任务被外部取消
                record.status = "cancelled"
                outcome = ToolOutcome.error(soft_failure("执行被取消。", tool=tool.name))
                raise
            except Exception as exc:  # noqa: BLE001 - 软失败协议
                logger.exception("工具 %s 执行异常", tool.name)
                record.status = "failed"
                record.error = f"{type(exc).__name__}: {exc}"
                outcome = ToolOutcome.error(wrap_exception(exc, tool=tool.name))
            finally:
                if record.status not in TERMINAL_STATUSES:
                    record.status = "completed"
                record.finished_at = time.time()
                record.duration_ms = int((record.finished_at - record.started_at) * 1000)
            record.outcome = outcome
            if outcome.execution_id is None:
                outcome.execution_id = record.id
            record.persisted_file = outcome.persisted_file
            self._persist(record)

    async def wait(self, execution_id: str, timeout: float | None = None) -> WaitResult:
        """等待执行结束；``timeout`` 到期返回快照但不取消后台任务。"""
        record = self._records.get(execution_id)
        if record is None:
            raise KeyError(f"未知 execution_id：{execution_id}")
        if record.finished:
            return WaitResult(record=record, timed_out=False)
        task = record.task
        if task is None:
            return WaitResult(record=record, timed_out=False)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout if timeout and timeout > 0 else None)
        except TimeoutError:
            return WaitResult(record=record, timed_out=True)
        except asyncio.CancelledError:  # pragma: no cover
            return WaitResult(record=record, timed_out=False)
        return WaitResult(record=record, timed_out=False)

    async def submit_and_wait(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        ctx: ToolContext,
        *,
        timeout: float | None = None,
        command: str = "",
    ) -> WaitResult:
        """提交并等待（模型侧同步语义）。"""
        wait_timeout = timeout if timeout is not None else float(self.config.agent.tool_wait_timeout_seconds)
        record = self.submit(tool, arguments, ctx, command=command)
        return await self.wait(record.id, wait_timeout)

    def get(self, execution_id: str) -> ExecutionRecord | None:
        return self._records.get(execution_id)

    def cancel(self, execution_id: str) -> bool:
        """请求取消（幂等）。

        yaml 配方工具：只置 ``cancel_event``，executor 轮询到后 kill 进程组（协作终止）。
        内建/MCP handler 工具：额外 ``task.cancel()`` 直接打断进程内协程——hard 取消
        需要传播到工具执行层，避免僵尸工具线程（Steering §四.8）。
        """
        record = self._records.get(execution_id)
        if record is None:
            return False
        record.cancel_event.set()
        if record.task and not record.task.done():
            if record.handler_cancellable:
                record.task.cancel()
            return True
        record.status = "cancelled" if not record.finished else record.status
        return True

    def active_records(self) -> list[ExecutionRecord]:
        return [r for r in self._records.values() if not r.finished]

    def all_records(self) -> list[ExecutionRecord]:
        return list(self._records.values())

    def cleanup(self, keep: int = 200) -> int:
        """回收已完成记录（保留最近 ``keep`` 条）。"""
        finished = sorted((r for r in self._records.values() if r.finished), key=lambda r: r.finished_at or 0)
        removed = 0
        for record in finished[:-keep] if keep > 0 else finished:
            self._records.pop(record.id, None)
            removed += 1
        return removed

    # ------------------------------------------------------------------
    # 实际执行
    # ------------------------------------------------------------------
    async def execute_tool(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        ctx: ToolContext,
        *,
        record: ExecutionRecord | None = None,
    ) -> ToolOutcome:
        """按工具来源分派执行，并统一做输出治理。"""
        workspace = ctx.resolved_workspace()
        recipe: ToolRecipe | None = tool.metadata.get("recipe")

        if tool.source == "yaml" and recipe is not None:
            outcome = await self._execute_recipe(recipe, arguments, ctx, record=record)
        elif tool.handler is not None:
            outcome = await tool.handler(arguments, ctx)
        elif tool.source == "mcp":
            outcome = ToolOutcome.error(
                soft_failure(
                    f"MCP 工具 {tool.name} 的桥接器未就绪。",
                    tool=tool.name,
                    hint="检查外部 MCP 服务器是否启动。",
                )
            )
        else:
            outcome = ToolOutcome.error(
                soft_failure(
                    f"工具 {tool.name} 未实现处理器（可能是配方缺少 command 或内置实现缺失）。",
                    tool=tool.name,
                    hint="改用 bash / python_exec 完成同等操作，或在 tools/ 下补全配方。",
                )
            )

        if record is not None and record.status == "cancelled":
            return ToolOutcome.error(soft_failure("执行已被取消。", tool=tool.name))
        return self._guard(outcome, tool=tool, workspace=workspace, record=record)

    # ------------------------------------------------------------------
    async def _execute_recipe(
        self,
        recipe: ToolRecipe,
        arguments: dict[str, Any],
        ctx: ToolContext,
        *,
        record: ExecutionRecord | None = None,
    ) -> ToolOutcome:
        workspace = ctx.resolved_workspace()
        try:
            argv = build_argv(
                recipe, arguments or {}, project_root=self.config.project_root, workspace=workspace
            )
        except Exception as exc:  # noqa: BLE001 - 参数展开失败 → 软失败
            return ToolOutcome.error(
                soft_failure(
                    f"参数展开失败：{exc}",
                    tool=recipe.name,
                    hint="对照工具 JSON Schema 检查参数名与类型。",
                )
            )

        preview = command_preview(argv)
        if record is not None:
            record.command = preview
            record.meta["argv"] = argv
        await ctx.notify("progress", {"stage": "tool_start", "message": f"$ {preview}", "tool": recipe.name})

        if not self._recipe_available(recipe):
            return ToolOutcome.error(
                soft_failure(
                    f"命令未安装：{recipe.command}",
                    tool=recipe.name,
                    hint="改用 bash / python_exec，或安装该工具后重试。",
                )
            )

        log_path = self.config.outputs_dir() / f"{(record.id if record else new_id('exec_'))}.log"
        cancel_event = record.cancel_event if record else None
        result = await self.executor.run(
            argv,
            cwd=workspace,
            env=recipe.env or None,
            log_file=log_path,
            cancel_event=cancel_event,
            timeout=float(self._effective_timeout(recipe)),
            no_output_timeout=float(self.config.agent.no_output_timeout_seconds),
        )
        if record is not None:
            record.exit_code = result.exit_code
            record.log_file = result.log_file
            record.status = result.status()
            if result.status() == "failed" and result.exit_code not in (0, None):
                record.status = "failed"
            elif result.status() == "completed":
                record.status = "completed"
        return self._process_result_to_outcome(result, recipe=recipe, preview=preview)

    def _process_result_to_outcome(
        self, result: ProcessResult, *, recipe: ToolRecipe, preview: str
    ) -> ToolOutcome:
        content, is_error = process_result_text(
            result,
            preview=preview,
            tool=recipe.name,
            hard_timeout_seconds=self._effective_timeout(recipe),
        )
        outcome = ToolOutcome(content=content, is_error=is_error)
        outcome.extra["logFile"] = result.log_file
        outcome.extra["exitCode"] = result.exit_code
        outcome.extra["status"] = result.status()
        return outcome

    # ------------------------------------------------------------------
    def _guard(
        self,
        outcome: ToolOutcome,
        *,
        tool: ToolDefinition,
        workspace: Path,
        record: ExecutionRecord | None,
    ) -> ToolOutcome:
        """统一输出治理：超限落盘 + ``<persisted-output>``。

        同时做孤立代理字符清洗——这是**最早**的统一出口：既保证 ``tool_executions``
        落库时 sqlite3 不会因无法编码而失败，也保证后续回填模型的内容可编码。
        """
        max_bytes = int(self.config.agent.max_tool_result_bytes)
        existing = outcome.extra.get("logFile") if isinstance(outcome.extra, dict) else None
        if existing and not Path(str(existing)).is_file():
            existing = None
        guarded = guard_output(
            clean_text(outcome.content or ""),
            workspace=workspace,
            name=tool.name,
            max_bytes=max_bytes,
            execution_id=record.id if record else outcome.execution_id,
            tool_name=tool.name,
            existing_file=str(existing) if existing else None,
        )
        outcome.content = guarded.content
        outcome.persisted_file = guarded.persisted_file or outcome.persisted_file
        if guarded.truncated:
            outcome.extra["truncated"] = True
        if outcome.execution_id is None and record is not None:
            outcome.execution_id = record.id
        return outcome

    # ------------------------------------------------------------------
    def _persist(self, record: ExecutionRecord) -> None:
        """把执行记录写入 SQLite（失败不影响主流程）。"""
        if self.repos is None:
            return
        payload = {
            "id": record.id,
            "conversation_id": record.conversation_id,
            "challenge_id": record.challenge_id,
            "run_id": record.run_id,
            "agent_id": record.agent_id,
            "tool_name": record.tool_name,
            "arguments": clean_value(record.arguments),
            "command": clean_text(record.command or ""),
            "status": record.status,
            "exit_code": record.exit_code,
            "output": clean_text((record.outcome.content if record.outcome else "") or ""),
            "error": clean_text(record.error) if record.error else record.error,
            "persisted_file": record.persisted_file,
            "is_error": bool(record.outcome.is_error) if record.outcome else record.status == "failed",
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "duration_ms": record.duration_ms,
        }

        async def _save() -> None:
            try:
                assert self.repos is not None
                await self.repos.executions.save(payload)
            except Exception:  # noqa: BLE001 - 落库失败不影响执行
                logger.debug("执行记录落库失败", exc_info=True)

        task = asyncio.create_task(_save())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    async def shutdown(self, *, timeout: float = 15.0) -> None:
        """取消运行中的工具并等待所有后台任务收尾。

        Runtime 关闭前必须调用：否则后台落库任务可能与数据库关闭并发，
        在 sqlite3 的 C 层造成 use-after-free（段错误）。
        """
        for record in list(self._records.values()):
            if not record.finished:
                record.cancel_event.set()
        await self.drain(timeout=timeout)
        self._records.clear()

    async def drain(self, *, timeout: float = 10.0) -> None:
        """等待已提交的后台任务结束（超时则取消）。"""
        pending = [t for t in self._tasks if not t.done()]
        if not pending:
            return
        _, still_pending = await asyncio.wait(pending, timeout=timeout)
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)

    def _effective_timeout(self, recipe: ToolRecipe) -> int:
        """配方的硬超时：优先取配方自身的 ``timeout_seconds``，否则用全局配置。"""
        return int(recipe.timeout_seconds or self.config.agent.tool_timeout_seconds)

    def _recipe_available(self, recipe: ToolRecipe) -> bool:
        """执行前再确认一次命令可用（避免加载后环境变化）。"""
        from .yaml_loader import check_availability

        available, _ = check_availability(recipe, project_root=self.config.project_root)
        return available

    # ------------------------------------------------------------------
    # 元工具
    # ------------------------------------------------------------------
    def meta_tools(self) -> list[ToolDefinition]:
        """execution_id 状态机的四个元工具。"""

        async def _get(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
            execution_id = str(args.get("execution_id", "")).strip()
            record = self.get(execution_id)
            if record is None:
                return ToolOutcome.error(
                    soft_failure(
                        f"未知 execution_id：{execution_id}",
                        tool="get_tool_execution",
                        hint="只可使用本会话返回过的 execution_id。",
                    )
                )
            return ToolOutcome.ok(
                json.dumps(record.summary(), ensure_ascii=False, indent=2),
                execution_id=execution_id,
            )

        async def _wait(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
            execution_id = str(args.get("execution_id", "")).strip()
            timeout_raw = args.get("timeout_seconds")
            try:
                timeout = (
                    float(timeout_raw)
                    if timeout_raw not in (None, "")
                    else float(self.config.agent.tool_wait_timeout_seconds)
                )
            except (TypeError, ValueError):
                timeout = float(self.config.agent.tool_wait_timeout_seconds)
            timeout = max(1.0, min(timeout, float(self.config.agent.tool_timeout_seconds)))
            record = self.get(execution_id)
            if record is None:
                return ToolOutcome.error(
                    soft_failure(
                        f"未知 execution_id：{execution_id}",
                        tool="wait_tool_execution",
                        hint="先调用工具获得 execution_id，再等待。",
                    )
                )
            result = await self.wait(execution_id, timeout)
            content, is_error = result.as_tool_content()
            return ToolOutcome(content=content, is_error=is_error, execution_id=execution_id)

        async def _cancel(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
            execution_id = str(args.get("execution_id", "")).strip()
            ok = self.cancel(execution_id)
            if not ok:
                return ToolOutcome.error(
                    soft_failure(f"未知 execution_id：{execution_id}", tool="cancel_tool_execution")
                )
            return ToolOutcome.ok(f"已请求取消 {execution_id}（后台任务将在下一次读取循环退出）。")

        async def _list(args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
            records = self.all_records()
            if not records:
                return ToolOutcome.ok("当前会话没有工具执行记录。")
            lines = [
                f"- {r.id} [{r.status}] {r.tool_name} 用时 {r.elapsed:.1f}s"
                + (f" :: {r.command[:120]}" if r.command else "")
                for r in sorted(records, key=lambda x: x.started_at, reverse=True)[:50]
            ]
            return ToolOutcome.ok("工具执行记录：\n" + "\n".join(lines))

        return [
            ToolDefinition(
                name="get_tool_execution",
                short_description="查看某个 execution_id 的状态与当前输出",
                description="查询后台工具执行的实时状态、退出码、部分输出与落盘文件。",
                parameters={
                    "type": "object",
                    "properties": {
                        "execution_id": {"type": "string", "description": "工具返回的 execution_id"}
                    },
                    "required": ["execution_id"],
                },
                handler=_get,
                category="meta",
                always_expose=True,
            ),
            ToolDefinition(
                name="wait_tool_execution",
                short_description="继续等待后台工具执行结束（默认等待 120s）",
                description=(
                    "等待某个 execution_id 完成。到期只返回当前快照，不会取消后台任务；"
                    "可反复调用直到 status 进入终态。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "execution_id": {"type": "string", "description": "工具返回的 execution_id"},
                        "timeout_seconds": {
                            "type": "integer",
                            "description": "本次等待上限（秒），默认 120，最大为工具硬超时",
                        },
                    },
                    "required": ["execution_id"],
                },
                handler=_wait,
                category="meta",
                always_expose=True,
            ),
            ToolDefinition(
                name="cancel_tool_execution",
                short_description="取消仍在运行的后台工具执行",
                description="请求取消指定 execution_id 的执行（杀进程组），用于止损或换路。",
                parameters={
                    "type": "object",
                    "properties": {
                        "execution_id": {"type": "string", "description": "工具返回的 execution_id"}
                    },
                    "required": ["execution_id"],
                },
                handler=_cancel,
                category="meta",
                always_expose=True,
            ),
            ToolDefinition(
                name="list_tool_executions",
                short_description="列出本会话所有工具执行记录与状态",
                description="排查「哪些命令跑过了 / 哪个还在跑」，避免重复执行同一条命令。",
                parameters={"type": "object", "properties": {}},
                handler=_list,
                category="meta",
                always_expose=True,
            ),
        ]


__all__ = [
    "ExecutionRecord",
    "ExecutionService",
    "PARTIAL_OUTPUT_CHARS",
    "TERMINAL_STATUSES",
    "WaitResult",
]
