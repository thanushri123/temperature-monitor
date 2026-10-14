"""Central configuration. Every value can be overridden via environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    db_path: str = field(default_factory=lambda: os.environ.get("TEMPMON_DB", "data/tempmon.db"))
    # Alert fires when temperature >= threshold (reaches OR exceeds).
    threshold_c: float = field(default_factory=lambda: _f("TEMPMON_THRESHOLD_C", 40.0))
    retention_days: int = field(default_factory=lambda: int(_f("TEMPMON_RETENTION_DAYS", 14)))
    # Plausible sensor range; readings outside are rejected as faulty, not alerted.
    min_valid_c: float = field(default_factory=lambda: _f("TEMPMON_MIN_VALID_C", -50.0))
    max_valid_c: float = field(default_factory=lambda: _f("TEMPMON_MAX_VALID_C", 150.0))
    # Re-notify while a breach persists (minutes). Avoids one page per minute.
    renotify_minutes: int = field(default_factory=lambda: int(_f("TEMPMON_RENOTIFY_MIN", 15)))
    # Device is "offline" if no valid reading for this long (reports once/min).
    offline_after_seconds: int = field(default_factory=lambda: int(_f("TEMPMON_OFFLINE_SEC", 180)))
    # Readings timestamped further than this into the future are rejected (clock skew).
    max_future_skew_seconds: int = field(default_factory=lambda: int(_f("TEMPMON_FUTURE_SKEW_SEC", 120)))
    webhook_url: str = field(default_factory=lambda: os.environ.get("TEMPMON_WEBHOOK_URL", ""))

    def __post_init__(self):
        if self.min_valid_c >= self.max_valid_c:
            raise ValueError("min_valid_c must be < max_valid_c")
        if self.retention_days < 1:
            raise ValueError("retention_days must be >= 1")
