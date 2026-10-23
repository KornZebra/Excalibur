"""CLI 模式：``python -m ctfagent.cli "题目描述"``。

在终端里完整跑一轮解题循环（含 HITL 交互审批），并把关键事件渲染成可读输出。
适合本地靶场与快速调试；Web 模式请用 ``python -m ctfagent.server``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Literal

from .agent.hitl import AutoApprover, CLIApprover
from .config import ConfigError, load_config
from .constants import APP_NAME, COMPLIANCE_STATEMENT, __version__
from .events import Event
from .runtime import Runtime

# ---------------------------------------------------------------------------
# ANSI 渲染
# ---------------------------------------------------------------------------
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"

MAX_RESULT_PREVIEW = 2_000


class ConsoleRenderer:
    """把事件流渲染成终端输出。"""

    def __init__(self, *, show_thinking: bool = False, show_results: bool = True, color: bool = True) -> None:
        self.show_thinking = show_thinking
        self.show_results = show_results
        self.color = color and sys.stdout.isatty()
        self._streaming = False
        self._thinking = False

    # ------------------------------------------------------------------
    def _c(self, text: str, color: str) -> str:
        return f"{color}{text}{RESET}" if self.color else text

    async def __call__(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is not None:
            handler(event)

    # ------------------------------------------------------------------
    def _on_progress(self, event: Event) -> None:
        data = event.data
        stage = data.get("stage", "")
        message = data.get("message", "")
        if stage in {
            "start",
            "iteration",
            "finalizer",
            "compression",
            "failover",
            "retry",
            "sub_agent_start",
            "sub_agent_done",
        }:
            self._end_stream()
            print(self._c(f"[{stage}] {message}", DIM))

    def _on_planning(self, event: Event) -> None:
        content = (event.data.get("content") or "").strip()
        if content:
            self._end_stream()
            print(self._c("\n── 计划 ──", CYAN))
            print(content)

    def _on_todos(self, event: Event) -> None:
        todos = event.data.get("todos") or []
        icons = {"pending": "☐", "in_progress": "◐", "completed": "☑"}
        self._end_stream()
        print(self._c("── TODO ──", CYAN))
        for item in todos:
            print(f"  {icons.get(item.get('status'), '☐')} {item.get('content')}")

    def _on_thinking_stream_delta(self, event: Event) -> None:
        if not self.show_thinking:
            return
        if not self._thinking:
            self._end_stream()
            print(self._c("── 思考 ──", DIM), end="", flush=True)
            self._thinking = True
        sys.stdout.write(self._c(event.data.get("delta", ""), DIM))
        sys.stdout.flush()

    def _on_response_start(self, event: Event) -> None:
        pass

    def _on_response_delta(self, event: Event) -> None:
        if self._thinking:
            print(RESET)
            self._thinking = False
        if not self._streaming:
            self._end_stream()
            print(self._c("\n── 助理 ──", GREEN))
            self._streaming = True
        sys.stdout.write(event.data.get("delta", ""))
        sys.stdout.flush()

    def _on_tool_call(self, event: Event) -> None:
        self._end_stream()
        data = event.data
        args = data.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                pass
        rendered = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
        if len(rendered) > 600:
            rendered = rendered[:600] + " …"
        agent = data.get("agent") or "ctf-agent"
        print(self._c(f"\n→ [{agent}] {data.get('name')} {rendered}", BLUE))
        if data.get("command"):
            print(self._c(f"  $ {data['command']}", DIM))

    def _on_tool_result(self, event: Event) -> None:
        if not self.show_results:
            return
        data = event.data
        result = data.get("result") or ""
        if len(result) > MAX_RESULT_PREVIEW:
            result = result[:MAX_RESULT_PREVIEW] + f"\n…（已截断，共 {len(data.get('result') or '')} 字符）"
        color = RED if data.get("isError") else DIM
        print(self._c(result, color))
        if data.get("executionId"):
            print(self._c(f"  execution_id={data['executionId']}", DIM))
        if data.get("persistedFile"):
            print(self._c(f"  完整输出已落盘：{data['persistedFile']}", DIM))
        print()

    def _on_hitl_request(self, event: Event) -> None:
        data = event.data
        print(self._c(f"\n[HITL] 待审批：{data.get('name')}（{data.get('reason')}）", YELLOW))

    def _on_usage_summary(self, event: Event) -> None:
        data = event.data
        print(
            self._c(
                f"  · 模型调用 {data.get('modelCalls')} 次｜prompt {data.get('promptTokens')} "
                f"｜completion {data.get('completionTokens')}",
                DIM,
            )
        )

    def _on_sub_agent_reply_stream_delta(self, event: Event) -> None:
        data = event.data
        if data.get("kind") == "thinking" and not self.show_thinking:
            return
        if not self._streaming:
            self._end_stream()
            print(self._c(f"\n── 子代理 {data.get('agentName')} ──", CYAN))
            self._streaming = True
        sys.stdout.write(data.get("delta", ""))
        sys.stdout.flush()

    def _on_error(self, event: Event) -> None:
        self._end_stream()
        print(self._c(f"\n[错误] {event.data.get('message')}", RED))

    def _on_cancelled(self, event: Event) -> None:
        self._end_stream()
        print(self._c(f"\n[已取消] {event.data.get('reason')}", YELLOW))

    def _on_response(self, event: Event) -> None:
        self._end_stream()
        print(self._c("\n" + "═" * 60, GREEN))
        print(event.data.get("content") or "")

    def _on_done(self, event: Event) -> None:
        self._end_stream()
        data = event.data
        print(self._c("\n" + "═" * 60, GREEN))
        print(
            self._c(
                f"状态：{data.get('status')}｜迭代：{data.get('iterations')}｜"
                f"会话：{data.get('conversationId')}",
                BOLD,
            )
        )
        if data.get("flags"):
            print(self._c(f"flag：{', '.join(data['flags'])}", GREEN))
        usage = data.get("usage") or {}
        print(
            self._c(
                f"token：prompt {usage.get('prompt_tokens', 0)} / completion {usage.get('completion_tokens', 0)} / "
                f"调用 {usage.get('model_calls', 0)} 次",
                DIM,
            )
        )
        if data.get("tracePath"):
            print(self._c(f"trace：{data['tracePath']}", DIM))

    # ------------------------------------------------------------------
    def _end_stream(self) -> None:
        if self._streaming or self._thinking:
            print()
        self._streaming = False
        self._thinking = False


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctf-agent",
        description=f"{APP_NAME} CLI 模式：在终端里跑一轮 CTF 解题循环",
        epilog=COMPLIANCE_STATEMENT.strip(),
    )
    parser.add_argument("prompt", nargs="*", help="题目描述或指令（也可用 --prompt-file）")
    parser.add_argument("--config", "-c", default=None, help="配置文件路径")
    parser.add_argument(
        "--challenge", default=None, help="题目 ID（决定隔离工作区 workspace/challenge/<id>/）"
    )
    parser.add_argument("--conversation", default=None, help="已有会话 ID（续跑）")
    parser.add_argument("--multi", action="store_true", help="启用多代理编排模式")
    parser.add_argument("--orchestration", default=None, choices=["deep", "plan_execute"], help="编排模式")
    parser.add_argument("--prompt-file", default=None, help="从文件读取题目描述")
    parser.add_argument("--system-prompt-file", default=None, help="覆盖系统提示词")
    parser.add_argument(
        "--auto-approve",
        "-y",
        action="store_true",
        help="完全访问模式（U6：等价 --reviewer none，命令不经审查直接执行，仅限本地靶场调试！）",
    )
    parser.add_argument(
        "--reviewer",
        default=None,
        choices=["human", "audit", "none"],
        help="审查模式（U6）：human=人工逐条审（默认）；audit=Agent 快速审查；none=完全访问",
    )
    parser.add_argument("--show-thinking", action="store_true", help="实时显示模型思考过程")
    parser.add_argument("--no-results", action="store_true", help="不打印工具输出")
    parser.add_argument("--list-tools", action="store_true", help="列出已加载工具后退出")
    parser.add_argument("--list-agents", action="store_true", help="列出子代理后退出")
    parser.add_argument("--log-level", default="warning", help="日志级别")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser


def _read_prompt(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if args.prompt_file:
        from pathlib import Path

        parts.append(Path(args.prompt_file).read_text(encoding="utf-8"))
    if args.prompt:
        parts.append(" ".join(args.prompt))
    return "\n".join(p for p in parts if p).strip()


async def run_cli(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    # U6 审查模式：--reviewer 显式指定优先；-y / cli_auto_approve 等价 --reviewer none
    full_access = args.auto_approve or config.hitl.cli_auto_approve
    reviewer_arg = args.reviewer or ("none" if full_access else None)
    if reviewer_arg is not None:
        reviewer_map: dict[str, Literal["human", "audit_agent", "none"]] = {
            "human": "human",
            "audit": "audit_agent",
            "none": "none",
        }
        config.hitl.default_reviewer = reviewer_map[reviewer_arg]
    if full_access:
        approver: Any = AutoApprover("CLI 自动批准（-y / --auto-approve）")
    else:
        approver = CLIApprover()

    runtime = await Runtime.create(config, approver=approver)
    renderer = ConsoleRenderer(show_thinking=args.show_thinking, show_results=not args.no_results)
    try:
        if args.list_tools:
            summary = runtime.registry.summary()
            print(f"工具统计：{summary}\n")
            for tool in runtime.registry.all():
                flag = "" if tool.available else "（不可用）"
                print(f"- {tool.name}{flag} [{tool.category}/{tool.source}] {tool.short_description}")
            return 0
        if args.list_agents:
            for agent in runtime.agents.list():
                print(f"- {agent.id} | {agent.name} | tools={agent.tools or '全部'} | {agent.description}")
            return 0

        prompt = _read_prompt(args)
        if not prompt:
            print(COMPLIANCE_STATEMENT.strip())
            print('\n用法：python -m ctfagent.cli "题目描述" [--challenge <id>] [--multi]')
            print("提示：目标缺失时代理会主动列出「缺失信息清单」。\n")
            return 2

        print(f"{APP_NAME} v{__version__}")
        reviewer_label = {
            "human": "🧑 人工审查",
            "audit_agent": "🤖 Agent 快速审查",
            "none": "⚡ 完全访问",
        }.get(config.hitl.default_reviewer, config.hitl.default_reviewer)
        print(
            f"模式：{'多代理(' + (args.orchestration or config.multi_agent.default_orchestration) + ')' if args.multi else '单代理'}"
            f"｜审查：{reviewer_label}"
            f"{'｜自动批准（-y）' if full_access else ''}"
        )
        if config.hitl.default_reviewer == "none":
            print("⚠ 完全访问模式：命令将不经审查直接执行（执行治理/超时/隔离仍生效）")
        print("─" * 60)

        handle = runtime.start_run(
            prompt,
            conversation_id=args.conversation,
            challenge_id=args.challenge,
            orchestration=args.orchestration,
            multi=args.multi,
        )
        unsubscribe = handle.bus.subscribe(renderer)
        try:
            while True:
                event = await handle.bus.queue.get()
                if event is None:
                    break
        finally:
            unsubscribe()
        if handle.task is not None:
            await handle.task
        result = handle.result
        if result is None:
            return 1
        if result.status == "cancelled":
            return 130
        return 0 if result.status in {"completed", "max_iterations"} else 1
    finally:
        await runtime.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run_cli(args))
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
