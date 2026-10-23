"""文本安全：清除「孤立代理字符」（lone surrogate），保证字符串可编码为 UTF-8。

背景
----
当文件名、外部命令输出或上游模型返回的内容包含**非法 UTF-8 字节**时，Python 可能
以 ``surrogateescape`` 解码，在 ``str`` 中留下 U+D800–U+DFFF 区间的孤立代理码点。
这类字符**无法编码为 UTF-8**，一旦进入下列任一环节就会抛
``UnicodeEncodeError: ... surrogates not allowed``：

1. ``json.dumps(..., ensure_ascii=False).encode("utf-8")`` —— 组装 LLM 请求体（httpx）、
   写 SSE 帧（sse-starlette）、返回普通 JSON 响应；
2. ``sqlite3`` 绑定参数 —— 消息/工具结果落库；
3. 日志输出。

典型触发场景：CTF 题目压缩包内的文件名是加密或非 UTF-8 编码的原始字节，解压后
``os.listdir`` / ``Path.glob`` 返回带代理字符的名称，经 ``list_dir`` 工具进入下一轮
模型请求，导致整轮解题失败（``ModelCallError``）。

策略
----
统一替换为 ``U+FFFD``（REPLACEMENT CHARACTER）。选择替换而非删除，是为了让「这里有
一个不可解码的字节」在界面上可见，且不改变字符串长度语义的直觉（一段乱码仍是乱码）。
"""

from __future__ import annotations

import re
from typing import TypeVar

__all__ = [
    "REPLACEMENT",
    "clean_json",
    "clean_text",
    "clean_value",
    "describe_surrogates",
    "is_encodable",
]

REPLACEMENT = "\ufffd"

# U+D800–U+DFFF：UTF-16 代理区。Python 的 str 以码点存储，合法的辅助平面字符
# （如 emoji）已是单个码点，因此**任何**出现在该区间的码点都是孤立的、非法的。
_SURROGATE = re.compile("[\ud800-\udfff]")

T = TypeVar("T")


def clean_text(text: str) -> str:
    """把字符串中的孤立代理字符替换为 ``U+FFFD``；无需替换时原样返回（零拷贝）。"""
    if not text:
        return text
    if _SURROGATE.search(text) is None:
        return text
    return _SURROGATE.sub(REPLACEMENT, text)


def clean_value(value: T) -> T:
    """递归清洗 ``str`` / ``dict`` / ``list`` / ``tuple`` 中的孤立代理字符。

    非字符串标量（int/float/bool/None）与其它对象原样返回——调用方若需序列化，
    仍由 ``json.dumps(default=str)`` 兜底。
    """
    if isinstance(value, str):
        return clean_text(value)  # type: ignore[return-value]
    if isinstance(value, dict):
        return {clean_text(k) if isinstance(k, str) else k: clean_value(v) for k, v in value.items()}  # type: ignore[return-value]
    if isinstance(value, list):
        return [clean_value(v) for v in value]  # type: ignore[return-value]
    if isinstance(value, tuple):
        return tuple(clean_value(v) for v in value)  # type: ignore[return-value]
    return value


def clean_json(text: str) -> str:
    """清洗**已序列化**的 JSON 文本（序列化后兜底，覆盖 ``default=str`` 分支）。"""
    return clean_text(text)


def is_encodable(text: str) -> bool:
    """该字符串能否安全编码为 UTF-8（诊断与测试用）。"""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def describe_surrogates(text: str, *, window: int = 40) -> list[tuple[int, str, str]]:
    """返回 ``[(位置, 码点, 上下文)]``，用于定位问题来源（诊断用，不参与业务逻辑）。"""
    hits: list[tuple[int, str, str]] = []
    for match in _SURROGATE.finditer(text):
        start = match.start()
        hits.append(
            (
                start,
                hex(ord(match.group())),
                text[max(0, start - window) : start + window].encode("utf-8", "backslashreplace").decode("ascii"),
            )
        )
    return hits
