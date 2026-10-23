"""运行中引导（Steering）：会话级事件枢纽 + 引导队列协调器。

Claude Code 式人机协同：任务运行期间用户随时发消息，消息**不打断**当前轮次，
而是进入该会话的 FIFO 引导队列；当前轮（一次 LLM 调用 + 其触发的全部工具执行）
结束后的**安全点**（下一轮模型请求构建之前）由任务协程原子取出队列、按入队顺序
合并为一条 user 消息注入上下文，agent 带着新指令继续。

组成：
- :class:`ConversationHub`：会话级事件枢纽。聚合运行中任务经 run 总线转发的全部
  事件与 Steering 自身事件（message_queued / message_delivered / task_done…），
  带单调递增 seq 与回放缓冲，支撑 ``GET /api/conversations/{id}/events`` 的
  Last-Event-ID 断线补发与多订阅者（多标签页）。
- :class:`SteeringCoordinator`：按 conversationId 管理运行中任务（句柄、FIFO 队列、
  会话锁、轮次）。同一会话同时最多一个运行任务；并发发送由会话锁串行化。
  队列只有任务协程一个消费者（``drain`` 在会话锁内原子取出并置 delivered），
  天然恰好一次投递。

与持久化的关系：引导消息**入队时立即落库**（``delivery_status='queued'``），
投递后在会话锁内置为 ``delivered``——``/state`` 刷新恢复直接读库，不依赖内存。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .events import Event, EventType
from .textsafe import clean_value

logger = logging.getLogger(__name__)

#: 会话级回放缓冲上限（断线补发窗口；超出后最早的事件不可再补发）
REPLAY_BUFFER_LIMIT = 2000
#: 内存中保留的会话枢纽上限（LRU 淘汰无订阅者的旧枢纽，防泄漏）
MAX_HUBS = 200


class ConversationHub:
    """会话级事件枢纽：多订阅者 + seq 单调递增 + 回放缓冲。

    ``publish`` 为同步方法（订阅者队列无界、put_nowait），因此可以被 run 总线的
    事件处理器直接调用，不阻塞任务主循环。
    """

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.created_at = time.time()
        self._seq = 0
        self._buffer: deque[tuple[int, str, dict[str, Any]]] = deque(maxlen=REPLAY_BUFFER_LIMIT)
        self._subscribers: dict[int, asyncio.Queue[tuple[int, str, dict[str, Any]] | None]] = {}
        self._sub_seq = 0

    # ------------------------------------------------------------------
    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> tuple[int, str, dict[str, Any]]:
        """发布一条事件：分配 seq → 入回放缓冲 → 推给所有订阅者。返回 (seq, type, data)。

        入缓冲区前统一清洗孤立代理字符：缓冲区会被**回放**，若其中存有无法编码为
        UTF-8 的文本，任何一次重连都会让 SSE 帧编码失败（``surrogates not allowed``）。
        """
        self._seq += 1
        item = (self._seq, str(event_type), clean_value(dict(data or {})))
        self._buffer.append(item)
        for queue in list(self._subscribers.values()):
            queue.put_nowait(item)
        return item

    def subscribe(self, *, last_seq: int = 0) -> tuple[list[tuple[int, str, dict[str, Any]]], asyncio.Queue[Any]]:
        """订阅并返回 ``(回放补发, 活跃队列)``。

        先入订阅名单再快照回放缓冲：订阅之后发布的事件必然同时出现在队列里，
        消费方按 ``seq > last_seq`` 去重即可无缝衔接、不丢不重。
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._sub_seq += 1
        self._subscribers[self._sub_seq] = queue
        backlog = [item for item in self._buffer if item[0] > last_seq]
        return backlog, queue

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        for key, value in list(self._subscribers.items()):
            if value is queue:
                self._subscribers.pop(key, None)

    @property
    def current_seq(self) -> int:
        """当前最大事件序号。

        订阅方恢复运行中视图时应以 ``seq > current_seq`` 续订（而不是从 0 回放），
        否则切回运行中的会话会把整个回放缓冲重放进时间线。
        """
        return self._seq

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def last_seq(self) -> int:
        return self._seq


@dataclass
class SteerItem:
    """引导队列中的一条消息。"""

    id: str
    text: str
    enqueued_at: float = field(default_factory=time.time)


class _Session:
    """一个会话的运行中任务登记（仅任务存活期间存在）。"""

    def __init__(self, conversation_id: str, run_id: str, handle: Any, hub: ConversationHub) -> None:
        self.conversation_id = conversation_id
        self.run_id = run_id
        self.handle = handle
        self.hub = hub
        self.queue: list[SteerItem] = []
        self.round = 0
        self.started_at = time.time()
        #: 「中断并继续」：用户在任务运行中发消息时置位；AgentRunner 在模型等待处/
        #: 工具循环边界捕获后清空，注入引导消息并续跑（任务本身不结束）。
        self.interrupt_event = asyncio.Event()


class SteeringCoordinator:
    """按会话管理运行中任务与引导队列。

    挂在 :class:`~ctfagent.runtime.Runtime` 上，``/api/conversations/{id}/messages``
    统一入口据此分流：无运行任务 → 启动任务（status=running）；有 → 入队（status=queued）。
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        #: 每会话一把锁：串行化"发送 vs 任务结束"竞态，保证不会出现既不入队也不触发任务的黑洞
        self._locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, _Session] = {}
        self._hubs: dict[str, ConversationHub] = {}

    # ------------------------------------------------------------------
    # 枢纽
    # ------------------------------------------------------------------
    def hub(self, conversation_id: str) -> ConversationHub:
        """取（或懒创建）会话事件枢纽。任务结束后枢纽保留，供断线重连回放历史事件。"""
        hub = self._hubs.get(conversation_id)
        if hub is None:
            hub = ConversationHub(conversation_id)
            self._hubs[conversation_id] = hub
            self._prune_hubs()
        return hub

    def _prune_hubs(self) -> None:
        if len(self._hubs) <= MAX_HUBS:
            return
        # 淘汰最旧的、无订阅者且无运行任务的枢纽（运行中/被订阅的绝不淘汰）
        for cid in sorted(self._hubs, key=lambda c: self._hubs[c].created_at):
            if len(self._hubs) <= MAX_HUBS:
                break
            if cid in self._sessions:
                continue
            if self._hubs[cid].subscriber_count:
                continue
            self._hubs.pop(cid, None)

    # ------------------------------------------------------------------
    # 会话锁
    # ------------------------------------------------------------------
    def lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def session(self, conversation_id: str) -> _Session | None:
        return self._sessions.get(conversation_id)

    def is_running(self, conversation_id: str) -> bool:
        session = self._sessions.get(conversation_id)
        return session is not None and session.handle.task is not None and not session.handle.task.done()

    def queue_snapshot(self, conversation_id: str) -> list[dict[str, Any]]:
        """内存队列快照（``/state`` 辅助；持久化真相源在 messages 表）。"""
        session = self._sessions.get(conversation_id)
        if session is None:
            return []
        return [
            {"id": item.id, "text": item.text, "enqueuedAt": item.enqueued_at * 1000}
            for item in session.queue
        ]

    def active_conversations(self) -> list[str]:
        return list(self._sessions)

    # ------------------------------------------------------------------
    # 中断并继续（Interrupt & Continue）
    # ------------------------------------------------------------------
    def request_interrupt(self, conversation_id: str) -> bool:
        """请求中断当前轮：置位中断事件（不取消任务、不清队列）。

        AgentRunner 在两处捕获：模型等待竞速处（取消当前推理流）与工具循环边界。
        捕获后取出引导队列、把用户消息合并注入上下文，同一任务内续跑下一轮。
        必须持会话锁（与发送/入队串行化）。
        """
        session = self._sessions.get(conversation_id)
        if session is None:
            return False
        session.interrupt_event.set()
        return True

    def interrupt_event(self, conversation_id: str) -> asyncio.Event | None:
        """取会话的中断事件（无运行任务时返回 None）。"""
        session = self._sessions.get(conversation_id)
        return session.interrupt_event if session is not None else None

    # ------------------------------------------------------------------
    # 任务登记 / 结束
    # ------------------------------------------------------------------
    def register(self, conversation_id: str, handle: Any) -> _Session:
        """登记一个已启动的运行（``runtime.start_run`` 的返回句柄），并把 run 总线
        事件转发到会话枢纽。必须在会话锁内调用。"""
        hub = self.hub(conversation_id)
        session = _Session(conversation_id, handle.run_id, handle, hub)
        self._sessions[conversation_id] = session
        def _forward(event: Event) -> None:
            # run 总线事件 → 会话枢纽（同步转发，不阻塞任务循环）
            try:
                hub.publish(event.type, event.to_dict())
                if event.type == EventType.ROUND_STARTED:
                    session.round = int((event.data or {}).get("round") or session.round)
                elif event.type == "progress" and (event.data or {}).get("stage") == "iteration":
                    session.round = int((event.data or {}).get("iteration") or session.round)
                elif event.type == EventType.MESSAGE_DELIVERED:
                    session.round = int((event.data or {}).get("round") or session.round)
            except Exception:  # noqa: BLE001 - 转发失败不影响任务主流程
                logger.debug("会话 %s 事件转发失败", conversation_id, exc_info=True)

        handle.bus.subscribe(_forward)
        return session

    def watch(self, session: _Session, repos: Any) -> asyncio.Task[None]:
        """监视任务结束：发 task_done / task_error，清理会话登记（防泄漏）。"""

        async def _watch() -> None:
            status = "error"
            error_message = ""
            try:
                await session.handle.task
                result = session.handle.result
                if result is not None:
                    status = result.status
                    error_message = result.error or ""
            except asyncio.CancelledError:  # pragma: no cover - 进程关闭路径
                status = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 - 兜底，保证会话登记一定被清理
                logger.exception("会话 %s 任务异常结束", session.conversation_id)
                status = "error"
                error_message = f"{type(exc).__name__}: {exc}"
            finally:
                # 只清理自己登记的会话（期间同一会话可能已注册了新任务）
                if self._sessions.get(session.conversation_id) is session:
                    self._sessions.pop(session.conversation_id, None)
            try:
                if status == "error":
                    session.hub.publish(
                        EventType.TASK_ERROR, {"message": error_message or "任务执行失败"}
                    )
                session.hub.publish(
                    EventType.TASK_DONE,
                    {
                        "reason": "cancelled" if status == "cancelled" else "completed",
                        "status": status,
                        "runId": session.run_id,
                    },
                )
            except Exception:  # noqa: BLE001
                logger.debug("会话 %s 收尾事件发布失败", session.conversation_id, exc_info=True)

        return asyncio.create_task(_watch(), name=f"steer-watch:{session.run_id}")

    # ------------------------------------------------------------------
    # 队列操作（任务协程是唯一消费者）
    # ------------------------------------------------------------------
    async def enqueue(self, conversation_id: str, item: SteerItem) -> int:
        """入队一条引导消息（调用方已负责落库）；返回入队后的队列长度。必须持会话锁。"""
        session = self._sessions.get(conversation_id)
        if session is None:
            raise RuntimeError(f"会话 {conversation_id} 没有运行中的任务，无法入队")
        session.queue.append(item)
        session.hub.publish(
            EventType.MESSAGE_QUEUED,
            {
                "id": item.id,
                "text": item.text,
                "queueLength": len(session.queue),
                "runId": session.run_id,
            },
        )
        return len(session.queue)

    async def drain(self, conversation_id: str, repos: Any) -> list[SteerItem] | None:
        """安全点取队列：原子取出全部积压并把消息置为 delivered。

        只有任务协程（AgentRunner 主循环）调用本方法——队列单消费者，恰好一次投递。
        无积压时返回 None。
        """
        async with self.lock(conversation_id):
            session = self._sessions.get(conversation_id)
            if session is None or not session.queue:
                return None
            items = session.queue
            session.queue = []
            # 取出 + 置 delivered 在同一把锁内完成：DB 始终是队列状态的真相源
            try:
                await repos.messages.mark_delivered([item.id for item in items])
            except Exception:  # noqa: BLE001 - 置状态失败不阻断注入
                logger.warning("引导消息置 delivered 失败：%s", [i.id for i in items], exc_info=True)
        return items

    async def has_pending(self, conversation_id: str) -> bool:
        """队列是否仍有积压（任务收尾前判断是否需要开启新段）。"""
        async with self.lock(conversation_id):
            session = self._sessions.get(conversation_id)
            return bool(session and session.queue)

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------
    def cancel(self, conversation_id: str, mode: str = "hard") -> bool:
        """取消会话的运行中任务。

        - ``graceful``：只置取消信号——当前轮（含工具）跑完后在下一个安全点停止；
        - ``hard``：置取消信号并立即取消该运行下所有进行中的工具执行（传播到
          工具执行层，避免僵尸工具进程）。

        两种模式都**不删除**排队消息（保持 queued，前端可见，由用户决定重发）。
        """
        session = self._sessions.get(conversation_id)
        if session is None:
            return False
        if mode == "hard":
            return bool(self._runtime.cancel_run(session.run_id))
        session.handle.cancel_event.set()
        return True


__all__ = ["ConversationHub", "SteerItem", "SteeringCoordinator"]
