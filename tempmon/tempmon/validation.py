"""Validation of raw edge-device payloads."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .config import Config
from .timeutil import now_ts, parse_timestamp


class InvalidReading(ValueError):
    """Raised for malformed / missing / implausible readings."""

    def __init__(self, reason: str, code: str):
        super().__init__(reason)
        self.code = code


@dataclass(frozen=True)
class Reading:
    device_id: str
    ts: int  # UTC epoch seconds
    temp_c: float


def validate(payload: Mapping[str, Any], cfg: Config, now: int | None = None) -> Reading:
    """Return a clean Reading or raise InvalidReading.

    Expected payload: {"device_id": str, "timestamp": iso|epoch, "temperature_c": number}
    """
    if not isinstance(payload, Mapping):
        raise InvalidReading("payload is not an object", "bad_payload")

    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        raise InvalidReading("device_id missing", "missing_device")
    device_id = device_id.strip()

    raw_temp = payload.get("temperature_c")
    if raw_temp is None or (isinstance(raw_temp, str) and not raw_temp.strip()):
        raise InvalidReading("temperature missing", "missing_temperature")
    if isinstance(raw_temp, bool):
        raise InvalidReading("temperature is boolean", "bad_temperature")
    try:
        temp = float(raw_temp)
    except (TypeError, ValueError):
        raise InvalidReading(f"temperature not numeric: {raw_temp!r}", "bad_temperature")
    if math.isnan(temp) or math.isinf(temp):
        raise InvalidReading("temperature not finite", "bad_temperature")
    if not (cfg.min_valid_c <= temp <= cfg.max_valid_c):
        raise InvalidReading(
            f"temperature {temp} outside sensor range [{cfg.min_valid_c}, {cfg.max_valid_c}]",
            "out_of_range",
        )

    try:
        ts = parse_timestamp(payload.get("timestamp"))
    except ValueError as exc:
        raise InvalidReading(str(exc), "bad_timestamp")
    now = now_ts() if now is None else now
    if ts > now + cfg.max_future_skew_seconds:
        raise InvalidReading("timestamp in the future", "future_timestamp")
    if ts < now - cfg.retention_days * 86400:
        raise InvalidReading("timestamp older than retention window", "too_old")

    return Reading(device_id=device_id, ts=ts, temp_c=temp)
