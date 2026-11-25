"""统一的业务时间解析。

系统内部一律使用带时区的 UTC ``datetime``；政策生效日可能只给到日期，
因此这里同时接受 ``YYYY-MM-DD``（按 UTC 零点处理）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from silverwatch.errors import invalid


def parse_instant(value, *, field: str = "time", code: str | None = None) -> datetime:
    """把字符串/datetime 转换为带时区的 UTC 时间。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            if len(text) == 10:  # YYYY-MM-DD
                parsed = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
            else:
                parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise invalid(code or "invalid_time", f"{field} 不是有效的日期时间") from exc
    else:
        raise invalid(code or "invalid_time", f"{field} 必须是 ISO 日期时间")
    if parsed.tzinfo is None:
        # 未注明时区时按 UTC 解释，而不是按服务器本地时区。
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def instant_iso(value, *, field: str = "time") -> str:
    return parse_instant(value, field=field).isoformat()


def current_instant() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
