"""Time helpers. Storage is UTC epoch seconds; everything user-facing is IST.

IST (UTC+05:30) has no daylight saving, so a fixed offset is exact and avoids
a tzdata dependency.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def now_ts() -> int:
    """Current time as UTC epoch seconds."""
    return int(datetime.now(timezone.utc).timestamp())


def to_ist(ts: int | float) -> datetime:
    return datetime.fromtimestamp(ts, tz=IST)


def fmt_ist(ts: int | float | None, with_date: bool = True) -> str:
    if ts is None:
        return "-"
    fmt = "%d-%b-%Y %H:%M:%S IST" if with_date else "%H:%M:%S IST"
    return to_ist(ts).strftime(fmt)


def parse_timestamp(value) -> int:
    """Parse a device timestamp to UTC epoch seconds.

    Accepts epoch seconds/millis (int/float/numeric str) or ISO-8601 strings.
    Naive ISO strings are interpreted as IST. Raises ValueError if unparseable.
    """
    if value is None or isinstance(value, bool):
        raise ValueError("timestamp missing")
    if isinstance(value, datetime):
        dt = value
    else:
        if isinstance(value, str):
            s = value.strip()
            if not s:
                raise ValueError("timestamp missing")
            try:
                value = float(s)
            except ValueError:
                try:
                    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError(f"unparseable timestamp: {value!r}") from exc
                value = None
        if value is not None:
            try:
                num = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unparseable timestamp: {value!r}") from exc
            if num != num or num in (float("inf"), float("-inf")):
                raise ValueError("timestamp not finite")
            if num > 1e11:  # epoch milliseconds
                num /= 1000.0
            return int(num)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return int(dt.timestamp())


def ist_naive(ts_series):
    """pandas Series of UTC epoch seconds -> naive datetimes expressed in IST (for plotting)."""
    import pandas as pd

    return pd.to_datetime(ts_series, unit="s") + pd.Timedelta(hours=5, minutes=30)
