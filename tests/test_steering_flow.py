"""Steering 验收测试（后端行为）：统一入口分流、安全点注入、恰好一次投递、收尾续段、取消保留队列。

对应交付说明 §验收标准，逐条映射（test 名称前缀 S1–S8）。
"""

from __future__ import annotations

import asyncio
import contextlib
import re

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from ctfagent.api import routes_data, routes_steer
from ctfagent.events import EventType
from ctfagent.models.types import ModelResponse, ToolCall

from .conftest import (
    FakeModel,
    events_of_type,
    text_step,
    tool_call_step,
    wait_task_done,
    wait_until,
)

pytestmark = pytest.mark.asyncio

_SSE_BLOCK = re.compile(r"\r?\n\r?\n")


# ---------------------------------------------------------------------------
# S1 空闲发送：启动任务；运行中发送：返回 queued + message_queued 事件
# ---------------------------------------------------------------------------
async def test_s1_idle_send_starts_task_and_running_send_queues(client, runtime):
    create = await client.post("/api/conversations", json={"title": "steer"})
    cid = create.json()["conversation"]["id"]

    # 模型第一步挂起（gate），让任务保持 running
    model: FakeModel = runtime.model
    entered = asyncio.Event()
    release = asyncio.Event()

    async def step1(m: FakeModel) -> ModelResponse:
        entered.set()
        await release.wait()
        return tool_call_step()

    model.push(step1)
    model.push(text_step("最终结论"))

    first = await client.post(f"/api/conversations/{cid}/messages", json={"text": "初始任务"})
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "running"
    assert body["messageId"]
    assert body["taskId"].startswith("run_")
    await wait_until(entered.is_set)

    # 任务运行中再发送 → queued
    second = await client.post(f"/api/conversations/{cid}/messages", json={"text": "引导指令 A"})
    assert second.status_code == 200
    queued_body = second.json()
    assert queued_body["status"] == "queued"
    assert queued_body["taskId"] == body["taskId"]
    assert queued_body["queueLength"] == 1

    queued_events = events_of_type(runtime, cid, EventType.MESSAGE_QUEUED)
    assert len(queued_events) == 1
    assert queued_events[0]["text"] == "引导指令 A"
    assert queued_events[0]["queueLength"] == 1

    # 消息已持久化且为 queued 态
    messages = (await client.get(f"/api/conversations/{cid}/messages")).json()["messages"]
    steer_row = next(m for m in messages if m["id"] == queued_body["messageId"])
    assert steer_row["role"] == "user"
    assert steer_row["deliveryStatus"] == "queued"

    release.set()
    await wait_task_done(runtime, cid)

    # 投递后行状态翻转
    messages = (await client.get(f"/api/conversations/{cid}/messages")).json()["messages"]
    steer_row = next(m for m in messages if m["id"] == queued_body["messageId"])
    assert steer_row["deliveryStatus"] == "delivered"


# ---------------------------------------------------------------------------
# S2/S3/S4 本轮结束后 FIFO 注入、单条合并、恰好一次
# ---------------------------------------------------------------------------
async def _run_with_steers(client, runtime, steer_texts: list[str]) -> str:
    """启动任务 → 模型第一步挂起 → 入队若干引导消息 → 放行 → 等任务结束。返回会话 id。"""
    create = await client.post("/api/conversations", json={"title": "steer"})
    cid = create.json()["conversation"]["id"]
    model: FakeModel = runtime.model
    entered = asyncio.Event()
    release = asyncio.Event()

    async def step1(m: FakeModel) -> ModelResponse:
        entered.set()
        await release.wait()
        return tool_call_step()

    model.push(step1)
    model.push(text_step("第一轮结论"))

    first = await client.post(f"/api/conversations/{cid}/messages", json={"text": "初始任务"})
    assert first.json()["status"] == "running"
    await wait_until(entered.is_set)

    for text in steer_texts:
        resp = await client.post(f"/api/conversations/{cid}/messages", json={"text": text})
        assert resp.json()["status"] == "queued"

    release.set()
    await wait_task_done(runtime, cid)
    return cid


async def test_s2_fifo_order_and_single_merged_user_message(client, runtime):
    cid = await _run_with_steers(client, runtime, ["引导一", "引导二", "引导三"])
    model: FakeModel = runtime.model

    # 恰好一次：一条 message_delivered 事件，ids 按入队顺序
    delivered = events_of_type(runtime, cid, EventType.MESSAGE_DELIVERED)
    assert len(delivered) == 1, delivered
    assert len(delivered[0]["ids"]) == 3
    assert delivered[0]["round"] == 2  # 第 2 轮安全点注入

    # 注入内容作为**一条** user 消息出现在后续模型请求上下文（断言请求体）
    assert len(model.requests) == 2
    second_request = model.requests[1]
    tail_user = [m for m in second_request if m["role"] == "user"]
    # 初始任务消息 + 合并后的引导消息 = 2 条 user；引导消息内容完整、顺序正确
    assert tail_user[-1]["content"] == "引导一\n\n引导二\n\n引导三"
    assert tail_user[0]["content"] == "初始任务"
    # 注入的合并消息就是请求的最后一条（安全点位于 tool 结果之后）
    assert second_request[-1]["role"] == "user"
    assert second_request[-1]["content"] == "引导一\n\n引导二\n\n引导三"
    # 引导消息不在 delivered 前提前进入第一次请求
    first_request_users = [m["content"] for m in model.requests[0] if m["role"] == "user"]
    assert first_request_users == ["初始任务"]


async def test_s3_final_answer_with_pending_queue_starts_new_segment(client, runtime):
    """最终回答生成时队列仍有积压 → 不结束任务，开启新段继续（直到队列清空）。"""
    create = await client.post("/api/conversations", json={"title": "seg"})
    cid = create.json()["conversation"]["id"]
    model: FakeModel = runtime.model
    entered1, release1 = asyncio.Event(), asyncio.Event()
    entered2, release2 = asyncio.Event(), asyncio.Event()

    async def step1(m: FakeModel) -> ModelResponse:
        entered1.set()
        await release1.wait()
        return tool_call_step()

    async def step2(m: FakeModel) -> ModelResponse:
        # 第二步给出"最终回答"（无工具调用）——此刻入队的引导消息应触发新段
        entered2.set()
        await release2.wait()
        return text_step("看似最终的回答")

    model.push(step1)
    model.push(step2)
    model.push(text_step("带着引导的新段结论"))

    first = await client.post(f"/api/conversations/{cid}/messages", json={"text": "初始任务"})
    assert first.json()["status"] == "running"
    await wait_until(entered1.is_set)
    release1.set()
    await wait_until(entered2.is_set)  # 第 2 轮开始（安全点 drain 已过、队列当前为空）

    queued = await client.post(f"/api/conversations/{cid}/messages", json={"text": "继续时看这里"})
    assert queued.json()["status"] == "queued"
    release2.set()
    await wait_task_done(runtime, cid)

    assert model.calls == 3, f"应开启新段（3 次模型调用），实际 {model.calls}"
    # 新段请求中包含注入的引导内容（一条 user 消息）
    third_request = model.requests[2]
    user_contents = [m["content"] for m in third_request if m["role"] == "user"]
    assert any("继续时看这里" in c for c in user_contents)
    delivered = events_of_type(runtime, cid, EventType.MESSAGE_DELIVERED)
    assert len(delivered) == 1


async def test_s4_task_ends_only_after_queue_drained(client, runtime):
    """正常路径：注入后任务继续，队列清空且最终回答后才结束（task_done=completed）。"""
    cid = await _run_with_steers(client, runtime, ["引导 A"])
    await wait_task_done(runtime, cid)
    done_events = events_of_type(runtime, cid, EventType.TASK_DONE)
    assert done_events and done_events[-1]["reason"] == "completed"
    state = (await client.get(f"/api/conversations/{cid}/state")).json()
    assert state["taskStatus"] == "idle"
    assert state["queuedMessages"] == []


# ---------------------------------------------------------------------------
# S5 取消（graceful / hard）：排队消息保留
# ---------------------------------------------------------------------------
async def test_s5_graceful_cancel_keeps_queued_messages(client, runtime):
    create = await client.post("/api/conversations", json={"title": "graceful"})
    cid = create.json()["conversation"]["id"]
    model: FakeModel = runtime.model
    entered = asyncio.Event()
    release = asyncio.Event()

    async def step1(m: FakeModel) -> ModelResponse:
        entered.set()
        await release.wait()
        return tool_call_step()

    model.push(step1)
    model.push(text_step("不应该被看到"))

    await client.post(f"/api/conversations/{cid}/messages", json={"text": "初始任务"})
    await wait_until(entered.is_set)
    await client.post(f"/api/conversations/{cid}/messages", json={"text": "取消前排队"})

    cancel = await client.post(f"/api/conversations/{cid}/cancel", json={"mode": "graceful"})
    assert cancel.status_code == 200
    release.set()
    await wait_task_done(runtime, cid)

    done_events = events_of_type(runtime, cid, EventType.TASK_DONE)
    assert done_events[-1]["reason"] == "cancelled"

    # 排队消息保留：状态仍是 queued、历史可见，绝不静默丢弃
    state = (await client.get(f"/api/conversations/{cid}/state")).json()
    assert state["taskStatus"] == "idle"
    assert [m["text"] for m in state["queuedMessages"]] == ["取消前排队"]
    messages = (await client.get(f"/api/conversations/{cid}/messages")).json()["messages"]
    row = next(m for m in messages if m["content"] == "取消前排队")
    assert row["deliveryStatus"] == "queued"
    # 未投递：模型从未见过该内容
    assert all("取消前排队" not in m["content"] for req in model.requests for m in req)


async def test_s5_hard_cancel_aborts_running_tool(client, runtime):
    create = await client.post("/api/conversations", json={"title": "hard"})
    cid = create.json()["conversation"]["id"]
    model: FakeModel = runtime.model

    async def step1(m: FakeModel) -> ModelResponse:
        return ModelResponse(
            content="",
            tool_calls=[ToolCall(id="call_slow", name="slow_tool", arguments={})],
            finish_reason="tool_calls",
        )

    model.push(step1)
    model.push(text_step("取消后不会再走到这里"))

    await client.post(f"/api/conversations/{cid}/messages", json={"text": "初始任务"})
    # 等工具真正进入运行态
    await wait_until(lambda: any(r.tool_name == "slow_tool" for r in runtime.execution_service.active_records()))

    await client.post(f"/api/conversations/{cid}/messages", json={"text": "hard 前排队"})
    cancel = await client.post(f"/api/conversations/{cid}/cancel", json={"mode": "hard"})
    assert cancel.status_code == 200
    await wait_task_done(runtime, cid)

    assert events_of_type(runtime, cid, EventType.TASK_DONE)[-1]["reason"] == "cancelled"
    state = (await client.get(f"/api/conversations/{cid}/state")).json()
    assert [m["text"] for m in state["queuedMessages"]] == ["hard 前排队"]
    row = next(
        m
        for m in (await client.get(f"/api/conversations/{cid}/messages")).json()["messages"]
        if m["content"] == "hard 前排队"
    )
    assert row["deliveryStatus"] == "queued"


# ---------------------------------------------------------------------------
# S6 输入校验与并发
# ---------------------------------------------------------------------------
async def test_s6_empty_and_blank_messages_rejected(client):
    create = await client.post("/api/conversations", json={"title": "validate"})
    cid = create.json()["conversation"]["id"]
    for bad in ["", "   ", "\n\t "]:
        resp = await client.post(f"/api/conversations/{cid}/messages", json={"text": bad})
        assert resp.status_code == 400, bad


async def test_s6_concurrent_sends_all_queued_in_order(client, runtime):
    create = await client.post("/api/conversations", json={"title": "concurrent"})
    cid = create.json()["conversation"]["id"]
    model: FakeModel = runtime.model
    entered = asyncio.Event()
    release = asyncio.Event()

    async def step1(m: FakeModel) -> ModelResponse:
        entered.set()
        await release.wait()
        return tool_call_step()

    model.push(step1)
    model.push(text_step("结论"))

    await client.post(f"/api/conversations/{cid}/messages", json={"text": "初始任务"})
    await wait_until(entered.is_set)

    # 并发发送 5 条（同一事件循环内交错），全部入队且按提交顺序
    results = await asyncio.gather(
        *(
            client.post(f"/api/conversations/{cid}/messages", json={"text": f"并发-{i}"})
            for i in range(5)
        )
    )
    payloads = [r.json() for r in results]
    assert all(p["status"] == "queued" for p in payloads)
    assert sorted(p["queueLength"] for p in payloads) == [1, 2, 3, 4, 5]

    release.set()
    await wait_task_done(runtime, cid)

    delivered = events_of_type(runtime, cid, EventType.MESSAGE_DELIVERED)
    assert len(delivered) == 1  # 只注入一次
    assert len(delivered[0]["ids"]) == 5
    # FIFO：注入顺序与入队（message_queued 事件序）一致
    queued_events = events_of_type(runtime, cid, EventType.MESSAGE_QUEUED)
    assert delivered[0]["ids"] == [e["id"] for e in queued_events]
    # 合并内容完整、按队列顺序
    texts = [e["text"] for e in queued_events]
    assert sorted(texts) == sorted(f"并发-{i}" for i in range(5))
    second_request = model.requests[1]
    tail_user = [m for m in second_request if m["role"] == "user"][-1]["content"]
    assert tail_user == "\n\n".join(texts)


# ---------------------------------------------------------------------------
# S7 刷新恢复：/state
# ---------------------------------------------------------------------------
async def test_s7_state_reflects_running_and_queue(client, runtime):
    create = await client.post("/api/conversations", json={"title": "state"})
    cid = create.json()["conversation"]["id"]

    idle_state = (await client.get(f"/api/conversations/{cid}/state")).json()
    assert idle_state["taskStatus"] == "idle"
    assert idle_state["round"] == 0

    model: FakeModel = runtime.model
    entered = asyncio.Event()
    release = asyncio.Event()

    async def step1(m: FakeModel) -> ModelResponse:
        entered.set()
        await release.wait()
        return tool_call_step()

    model.push(step1)
    model.push(text_step("结论"))

    await client.post(f"/api/conversations/{cid}/messages", json={"text": "初始任务"})
    await wait_until(entered.is_set)
    await client.post(f"/api/conversations/{cid}/messages", json={"text": "待注入指令"})

    running_state = (await client.get(f"/api/conversations/{cid}/state")).json()
    assert running_state["taskStatus"] == "running"
    assert running_state["round"] >= 1
    assert running_state["runId"]
    assert [m["text"] for m in running_state["queuedMessages"]] == ["待注入指令"]

    release.set()
    await wait_task_done(runtime, cid)
    final_state = (await client.get(f"/api/conversations/{cid}/state")).json()
    assert final_state["taskStatus"] == "idle"
    assert final_state["queuedMessages"] == []


# ---------------------------------------------------------------------------
# S8 事件流：Last-Event-ID 断点补发 + 多订阅者
# ---------------------------------------------------------------------------

def _iter_sse_blocks(buffer: str) -> tuple[list[str], str]:
    """按 SSE 事件块切分（sse-starlette 用 \\r\\n 行结束符）。返回 (完整块, 残余)。"""
    blocks: list[str] = []
    while True:
        m = _SSE_BLOCK.search(buffer)
        if not m:
            break
        blocks.append(buffer[: m.start()])
        buffer = buffer[m.end():]
    return blocks, buffer


def _parse_sse_block(block: str) -> tuple[int | None, str | None]:
    event_id: int | None = None
    event_type: str | None = None
    for line in block.splitlines():
        if line.startswith("id:"):
            event_id = int(line[3:].strip())
        elif line.startswith("event:"):
            event_type = line[6:].strip()
    return event_id, event_type


# ---------------------------------------------------------------------------
# S8 事件流：Last-Event-ID 断点补发 + 实时投递
# 说明：httpx 的 ASGITransport 会缓冲整个响应，无法测长驻 SSE——这两个测试用
# 进程内 uvicorn（真实 TCP）验证真正的流式行为。
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def tcp_client(runtime):
    """在进程内起一个真实 uvicorn（随机端口，lifespan 关闭以保留注入的假模型 runtime）。"""
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(routes_data.router)
    app.include_router(routes_steer.router)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 10
    while not server.started and loop.time() < deadline:  # noqa: ASYNC110 - 轮询 uvicorn 启动标志（无事件可等待）
        await asyncio.sleep(0.02)
    assert server.started, "uvicorn 未能在时限内启动"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as ac:
            yield ac
    finally:
        server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=10)


async def test_s8_events_stream_replay_with_last_event_id(runtime):

    async with tcp_client(runtime) as client:
        create = await client.post("/api/conversations", json={"title": "events"})
        cid = create.json()["conversation"]["id"]
        model: FakeModel = runtime.model
        entered = asyncio.Event()
        release = asyncio.Event()

        async def step1(m: FakeModel) -> ModelResponse:
            entered.set()
            await release.wait()
            return tool_call_step()

        model.push(step1)
        model.push(text_step("结论"))

        await client.post(f"/api/conversations/{cid}/messages", json={"text": "初始任务"})
        await wait_until(entered.is_set)
        await client.post(f"/api/conversations/{cid}/messages", json={"text": "断线前入队"})

        # 记下当前位置，模拟断线
        seqs = [seq for seq, _, _ in runtime.steering.hub(cid)._buffer]
        assert seqs
        last_seen = seqs[-1]

        release.set()
        await wait_task_done(runtime, cid)

        # 断线重连：Last-Event-ID 之后的事件必须补发（不重不漏）
        received: list[tuple[int | None, str | None]] = []
        async with client.stream(
            "GET",
            f"/api/conversations/{cid}/events",
            headers={"Last-Event-ID": str(last_seen)},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            buffer = ""
            async for chunk in resp.aiter_text():
                blocks, buffer = _iter_sse_blocks(buffer + chunk)
                for block in blocks:
                    received.append(_parse_sse_block(block))
                if any(t == EventType.TASK_DONE for _, t in received):
                    break

    assert any(t == EventType.TASK_DONE for _, t in received)
    # 补发的事件 id 全部 > last_seen（断点之后不重不漏）
    ids = [i for i, _ in received if i is not None]
    assert ids and min(ids) > last_seen
    # 队列里那条消息的 delivered 事件在补发中
    assert any(t == EventType.MESSAGE_DELIVERED for _, t in received)


async def test_s8_events_stream_live_delivery(runtime):
    """先连事件流再发送：message_queued / message_delivered 按序实时到达。"""
    async with tcp_client(runtime) as client:
        create = await client.post("/api/conversations", json={"title": "live"})
        cid = create.json()["conversation"]["id"]
        model: FakeModel = runtime.model
        entered = asyncio.Event()
        release = asyncio.Event()

        async def step1(m: FakeModel) -> ModelResponse:
            entered.set()
            await release.wait()
            return tool_call_step()

        model.push(step1)
        model.push(text_step("结论"))

        await client.post(f"/api/conversations/{cid}/messages", json={"text": "初始任务"})
        await wait_until(entered.is_set)

        received: list[str] = []
        stream_ready = asyncio.Event()

        async def consume() -> None:
            async with client.stream("GET", f"/api/conversations/{cid}/events") as resp:
                buffer = ""
                stream_ready.set()
                async for chunk in resp.aiter_text():
                    blocks, buffer = _iter_sse_blocks(buffer + chunk)
                    for block in blocks:
                        _, event_type = _parse_sse_block(block)
                        if event_type:
                            received.append(event_type)
                    if EventType.TASK_DONE in received:
                        return

        consumer = asyncio.create_task(consume())
        await wait_until(stream_ready.is_set)
        await asyncio.sleep(0.05)
        await client.post(f"/api/conversations/{cid}/messages", json={"text": "实时引导"})
        release.set()
        await wait_until(lambda: EventType.TASK_DONE in received, timeout=10)
        await asyncio.wait_for(consumer, timeout=5)

    assert EventType.MESSAGE_QUEUED in received
    assert EventType.MESSAGE_DELIVERED in received
    assert received.index(EventType.MESSAGE_QUEUED) < received.index(EventType.MESSAGE_DELIVERED)


# ---------------------------------------------------------------------------
# S9 会话隔离：A 会话的队列不影响 B 会话
# ---------------------------------------------------------------------------
async def test_s9_conversation_isolation(client, runtime):
    create_a = await client.post("/api/conversations", json={"title": "A"})
    create_b = await client.post("/api/conversations", json={"title": "B"})
    cid_a = create_a.json()["conversation"]["id"]
    cid_b = create_b.json()["conversation"]["id"]
    model: FakeModel = runtime.model
    entered = asyncio.Event()
    release = asyncio.Event()

    async def step1(m: FakeModel) -> ModelResponse:
        entered.set()
        await release.wait()
        return tool_call_step()

    model.push(step1)
    model.push(tool_call_step("call_a"))
    model.push(text_step("A 结论"))
    model.push(tool_call_step("call_b"))
    model.push(text_step("B 结论"))

    await client.post(f"/api/conversations/{cid_a}/messages", json={"text": "A 初始任务"})
    await wait_until(entered.is_set)

    # B 会话空闲：发送应启动 B 自己的任务，而不是排进 A 的队列
    resp_b = await client.post(f"/api/conversations/{cid_b}/messages", json={"text": "B 初始任务"})
    assert resp_b.json()["status"] == "running"
    assert resp_b.json()["taskId"] != _current_task_id(runtime, cid_a)

    state_a = (await client.get(f"/api/conversations/{cid_a}/state")).json()
    state_b = (await client.get(f"/api/conversations/{cid_b}/state")).json()
    assert state_b["queuedMessages"] == []
    assert state_b["runId"] != state_a["runId"]

    release.set()
    await wait_task_done(runtime, cid_a)
    await wait_task_done(runtime, cid_b)


def _current_task_id(runtime, cid: str) -> str:
    session = runtime.steering.session(cid)
    return session.run_id if session else ""
