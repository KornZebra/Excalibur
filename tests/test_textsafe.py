"""孤立代理字符（lone surrogate）防护的回归测试。

历史故障（2026-10-17）
----------------------
题目压缩包内的文件名是原始/加密字节（如 ``b"\\xa6\\xd4-+.txt"``），解压后
``os.listdir`` 以 ``surrogateescape`` 解码，在 ``str`` 中留下 U+D800–U+DFFF。
该文本经 ``list_dir`` 工具结果进入下一轮 LLM 请求，httpx 序列化请求体时抛
``UnicodeEncodeError: ... surrogates not allowed``，整轮解题以
``ModelCallError`` 结束（iterations=2、model_calls=1，工具结果尚未落库）。

本测试锁定四道闸门：工具结果出口、LLM 请求体、SSE 帧、通用清洗函数。
"""

from __future__ import annotations

import json

from ctfagent.api.routes_steer import _frame
from ctfagent.config import ChannelConfig
from ctfagent.models.client import OpenAICompatibleClient
from ctfagent.models.types import ChatMessage, ToolOutcome
from ctfagent.textsafe import REPLACEMENT, clean_text, clean_value, is_encodable
from ctfagent.tools.execution_service import ExecutionRecord, WaitResult

#: 真实故障样本：os.fsdecode(b"\xa6\xd4-+.txt")
SURROGATE_NAME = "\udca6\udcd4-+.txt"


def _encodable(payload: object) -> bool:
    """模拟 httpx / sse-starlette 的序列化 + 编码，判断是否会炸。"""
    try:
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# ---------------------------------------------------------------------------
# 清洗函数
# ---------------------------------------------------------------------------
def test_clean_text_replaces_lone_surrogates() -> None:
    cleaned = clean_text(SURROGATE_NAME)
    assert cleaned == REPLACEMENT * 2 + "-+.txt"
    assert is_encodable(cleaned)


def test_clean_text_keeps_valid_text_untouched() -> None:
    text = "正常中文 with emoji 🚩 and ascii"
    assert clean_text(text) is text  # 零拷贝快路径


def test_clean_text_keeps_astral_characters() -> None:
    # 合法辅助平面字符（单个码点）不得被误伤
    assert clean_text("🚩") == "🚩"


def test_clean_value_recurses_into_containers() -> None:
    payload = {
        "name": SURROGATE_NAME,
        "list": [SURROGATE_NAME, 1, None],
        "nested": {"k": SURROGATE_NAME},
    }
    cleaned = clean_value(payload)
    assert _encodable(cleaned)
    assert cleaned["list"][0] == REPLACEMENT * 2 + "-+.txt"
    assert cleaned["list"][1] == 1
    assert cleaned["nested"]["k"].startswith(REPLACEMENT)


# ---------------------------------------------------------------------------
# 闸门一：工具结果出口
# ---------------------------------------------------------------------------
def test_tool_result_content_is_cleaned() -> None:
    record = ExecutionRecord(
        id="exec_1",
        tool_name="list_dir",
        status="completed",
        outcome=ToolOutcome(content=f"work/\n  {SURROGATE_NAME}  8B\n"),
    )
    content, is_error = WaitResult(record=record).as_tool_content()
    assert is_error is False
    assert is_encodable(content)
    assert SURROGATE_NAME not in content
    assert REPLACEMENT in content


def test_tool_summary_output_is_cleaned() -> None:
    record = ExecutionRecord(
        id="exec_2",
        tool_name="list_dir",
        status="completed",
        outcome=ToolOutcome(content=SURROGATE_NAME),
    )
    assert _encodable(record.summary())


# ---------------------------------------------------------------------------
# 闸门二：LLM 请求体
# ---------------------------------------------------------------------------
def test_llm_request_body_is_utf8_encodable() -> None:
    # 不构造真实 SDK 客户端（_build_kwargs 只用 channel 与 stream_options 标记），
    # 避免测试依赖 openai 包是否装在当前解释器里。
    client = object.__new__(OpenAICompatibleClient)
    client.channel = ChannelConfig(name="test", model="m", base_url="http://127.0.0.1:1/v1")
    client._stream_options_supported = True
    messages = [
        ChatMessage.user("解压附件"),
        ChatMessage.assistant("", []),
        ChatMessage.tool("call_1", SURROGATE_NAME, "list_dir"),
    ]
    kwargs = client._build_kwargs(messages, [{"type": "function", "function": {"name": SURROGATE_NAME}}])
    assert _encodable(kwargs), "请求体仍含孤立代理字符，httpx 会抛 UnicodeEncodeError"
    assert kwargs["messages"][-1]["content"] == REPLACEMENT * 2 + "-+.txt"


# ---------------------------------------------------------------------------
# 闸门三：SSE 帧
# ---------------------------------------------------------------------------
def test_sse_frame_is_utf8_encodable() -> None:
    frame = _frame(7, "tool_result", {"name": "list_dir", "result": SURROGATE_NAME})
    assert is_encodable(frame["data"])
    # 仍是合法 JSON，且内容为替换字符
    parsed = json.loads(frame["data"])
    assert parsed["result"] == REPLACEMENT * 2 + "-+.txt"
    assert frame["id"] == "7" and frame["event"] == "tool_result"
