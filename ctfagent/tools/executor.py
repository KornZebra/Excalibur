"""进程执行器：asyncio.subprocess + 流式输出 + 防挂死三件套。

防挂死三件套（设计宪法第 6 条）：
1. **硬超时**：超过 ``tool_timeout`` 杀进程组；
2. **连续无输出终止**：超过 ``no_output_timeout`` 无任何输出 → 判定为"疑似等待交互输入"并杀进程组；
3. **stdin 置空**：默认 ``DEVNULL``，需要喂数据时才用 ``PIPE`` 并在写入后立即关闭。

进程以 ``start_new_session=True`` 启动，因此可以用 ``os.killpg`` 连同子进程一起清理。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

LineCallback = Callable[[str], "Awaitable[None] | None"]

#: 内存中保留的完整输出上限（超出部分只在日志文件里）
MAX_INMEMORY_OUTPUT = 32 * 1024 * 1024
#: 环形缓冲保留的尾部字节数（partial 展示用）
TAIL_KEEP_BYTES = 65_536


@dataclass
class ProcessResult:
    """一次进程执行的结果。"""

    exit_code: int | None = None
    output: str = ""
    output_tail: str = ""
    duration_ms: int = 0
    log_file: str | None = None
    hard_timeout: bool = False
    no_output_timeout: bool = False
    cancelled: bool = False
    spawn_error: str | None = None
    truncated: bool = False
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not (self.hard_timeout or self.no_output_timeout or self.cancelled)

    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.hard_timeout:
            return "hard_timeout"
        if self.no_output_timeout:
            return "no_output_timeout"
        if self.spawn_error:
            return "failed"
        return "completed" if self.exit_code == 0 else "failed"


class ProcessExecutor:
    """受限并发 + 硬超时 + 无输出检测的异步进程执行器。"""

    def __init__(
        self,
        *,
        tool_timeout: float = 900.0,
        no_output_timeout: float = 300.0,
        check_interval: float = 2.0,
        tail_bytes: int = TAIL_KEEP_BYTES,
        max_inmemory_bytes: int = MAX_INMEMORY_OUTPUT,
    ) -> None:
        self.tool_timeout = tool_timeout
        self.no_output_timeout = no_output_timeout
        self.check_interval = max(0.2, min(check_interval, max(1.0, no_output_timeout / 4)))
        self.tail_bytes = tail_bytes
        self.max_inmemory_bytes = max_inmemory_bytes

    # ------------------------------------------------------------------
    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        stdin_data: str | bytes | None = None,
        on_line: LineCallback | None = None,
        log_file: Path | None = None,
        cancel_event: asyncio.Event | None = None,
        timeout: float | None = None,
        no_output_timeout: float | None = None,
    ) -> ProcessResult:
        """执行命令并收集输出（stdout/stderr 合并）。"""
        started = time.monotonic()
        hard_limit = float(timeout if timeout is not None else self.tool_timeout)
        idle_limit = float(no_output_timeout if no_output_timeout is not None else self.no_output_timeout)

        argv = [str(a) for a in argv]
        if not argv:
            return ProcessResult(spawn_error="空命令", duration_ms=0)

        spawn_env = dict(os.environ)
        if env:
            spawn_env.update({k: str(v) for k, v in env.items()})

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd) if cwd else None,
                env=spawn_env,
                start_new_session=True,
            )
        except FileNotFoundError:
            return ProcessResult(
                spawn_error=f"命令不存在：{argv[0]}（可能未安装该工具，请改用 bash 或 python_exec 替代）",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except PermissionError:
            return ProcessResult(
                spawn_error=f"命令不可执行：{argv[0]}（缺少执行权限）",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except OSError as exc:  # pragma: no cover - 罕见
            return ProcessResult(spawn_error=f"启动进程失败：{exc}", duration_ms=0)

        # stdin 置空：写完立即关闭，避免子进程等待输入
        if stdin_data is not None and proc.stdin is not None:
            try:
                payload = stdin_data.encode() if isinstance(stdin_data, str) else stdin_data
                proc.stdin.write(payload)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                pass
            finally:
                with contextlib.suppress(Exception):
                    proc.stdin.close()

        result = await self._pump(
            proc,
            started=started,
            hard_limit=hard_limit,
            idle_limit=idle_limit,
            on_line=on_line,
            log_file=Path(log_file) if log_file else None,
            cancel_event=cancel_event,
        )
        return result

    # ------------------------------------------------------------------
    async def _pump(
        self,
        proc: asyncio.subprocess.Process,
        *,
        started: float,
        hard_limit: float,
        idle_limit: float,
        on_line: LineCallback | None,
        log_file: Path | None,
        cancel_event: asyncio.Event | None,
    ) -> ProcessResult:
        assert proc.stdout is not None
        chunks: list[str] = []
        in_memory = 0
        truncated = False
        tail = bytearray()
        last_output = time.monotonic()
        hard_timeout = no_output_timeout = cancelled = False

        log_handle = None
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_file, "wb", buffering=64 * 1024)  # noqa: SIM115 - 生命周期由本函数管理

        try:
            while True:
                # 每轮读取前先判定三类终止条件：
                # 取消 / 硬超时 / 连续无输出（无论命令是否有输出都必须判定）
                now = time.monotonic()
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    await self._terminate(proc)
                    break
                if hard_limit > 0 and now - started > hard_limit:
                    hard_timeout = True
                    await self._terminate(proc)
                    break
                if idle_limit > 0 and now - last_output > idle_limit:
                    no_output_timeout = True
                    await self._terminate(proc)
                    break
                try:
                    line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=self.check_interval)
                except TimeoutError:
                    continue
                if not line_bytes:
                    break  # EOF
                last_output = time.monotonic()
                text = line_bytes.decode("utf-8", errors="replace")
                if log_handle is not None:
                    log_handle.write(line_bytes)
                if in_memory < self.max_inmemory_bytes:
                    chunks.append(text)
                    in_memory += len(line_bytes)
                else:
                    truncated = True
                tail.extend(line_bytes)
                if len(tail) > self.tail_bytes:
                    del tail[: len(tail) - self.tail_bytes]
                if on_line is not None:
                    try:
                        outcome = on_line(text)
                        if outcome is not None:
                            await outcome
                    except Exception:  # noqa: BLE001 - 回调异常不影响执行
                        logger.debug("on_line 回调异常", exc_info=True)

            # 进程可能已经退出但缓冲区仍有残余数据
            with contextlib.suppress(Exception):
                leftover = await asyncio.wait_for(proc.stdout.read(), timeout=5.0)
                if leftover:
                    text = leftover.decode("utf-8", errors="replace")
                    if log_handle is not None:
                        log_handle.write(leftover)
                    if in_memory < self.max_inmemory_bytes:
                        chunks.append(text)
                    tail.extend(leftover)
                    if len(tail) > self.tail_bytes:
                        del tail[: len(tail) - self.tail_bytes]
        finally:
            if log_handle is not None:
                with contextlib.suppress(Exception):
                    log_handle.flush()
                    log_handle.close()

        exit_code: int | None
        try:
            exit_code = await asyncio.wait_for(proc.wait(), timeout=10.0)
        except TimeoutError:  # pragma: no cover - 已 kill 仍不退
            exit_code = None

        duration_ms = int((time.monotonic() - started) * 1000)
        if no_output_timeout:
            note = (
                "\n[executor] 命令连续无输出被终止，疑似等待交互输入；"
                "可改用非交互参数（-y/-batch/-c/管道输入）或 pwntools process()。\n"
            )
            chunks.append(note)
        elif hard_timeout:
            chunks.append(f"\n[executor] 命令超过硬超时 {hard_limit:.0f}s，已终止进程组。\n")
        elif cancelled:
            chunks.append("\n[executor] 命令被取消。\n")
        if truncated:
            chunks.insert(
                0,
                f"[executor] 输出超过内存上限 {self.max_inmemory_bytes} 字节，"
                f"仅保留尾部；完整输出见日志文件。\n",
            )

        return ProcessResult(
            exit_code=exit_code,
            output="".join(chunks),
            output_tail=tail.decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
            log_file=str(log_file) if log_file else None,
            hard_timeout=hard_timeout,
            no_output_timeout=no_output_timeout,
            cancelled=cancelled,
            truncated=truncated,
            extra={"hard_limit": hard_limit, "idle_limit": idle_limit},
        )

    # ------------------------------------------------------------------
    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        """杀掉整个进程组（先 SIGKILL，避免交互式进程残留）。"""
        if proc.returncode is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):  # pragma: no cover
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        except Exception:  # noqa: BLE001 - Windows/异常环境兜底
            with contextlib.suppress(Exception):
                proc.kill()


__all__ = ["MAX_INMEMORY_OUTPUT", "TAIL_KEEP_BYTES", "ProcessExecutor", "ProcessResult"]
