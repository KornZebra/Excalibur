"""时间工具：账本表按迁移脚本约定使用 ISO-8601 UTC 文本存储。

统一在此转换，避免各处手写 ``strftime``/``fromisoformat`` 造成格式漂移。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

__all__ = ["now_iso", "parse_iso", "seconds_since", "iso_after"]


def now_iso() -> str:
    """当前 UTC 时间（``YYYY-MM-DDTHH:MM:SSZ``，可字典序比较）。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    """解析账本时间戳；无法解析时返回 ``None``（而不是抛异常，避免污染调度回路）。"""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def seconds_since(value: str | None, *, now: datetime | None = None) -> float:
    """距离给定时间戳过去了多少秒；无法解析时返回 0（视为"刚刚"）。"""
    parsed = parse_iso(value)
    if parsed is None:
        return 0.0
    return max(0.0, ((now or datetime.now(UTC)) - parsed).total_seconds())


def iso_after(minutes: float, *, base: datetime | None = None) -> str:
    """``base``（默认现在）之后 ``minutes`` 分钟的时间戳。"""
    clock = (base or datetime.now(UTC)).timestamp() + minutes * 60
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock))
