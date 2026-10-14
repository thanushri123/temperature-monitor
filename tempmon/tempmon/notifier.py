"""Pluggable alert channels. In AWS: SNS -> email/SMS/PagerDuty/Slack."""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Protocol

from .timeutil import fmt_ist

log = logging.getLogger("tempmon.alerts")


class Notifier(Protocol):
    def send(self, event: dict) -> None: ...


def format_event(e: dict) -> str:
    kind = e["type"]
    t = fmt_ist(e["ts"])
    if kind == "BREACH_OPENED":
        return (f"[ALERT] {e['device_id']} reached {e['temp_c']:.1f}°C "
                f"(threshold {e['threshold_c']:.1f}°C) at {t}")
    if kind == "BREACH_ONGOING":
        return (f"[ALERT-REMINDER] {e['device_id']} still at {e['temp_c']:.1f}°C "
                f"(peak {e['peak_c']:.1f}°C, {e['breach_count']} breaching readings) at {t}")
    return f"[RESOLVED] {e['device_id']} back to {e['temp_c']:.1f}°C at {t}"


class LogNotifier:
    def send(self, event: dict) -> None:
        msg = format_event(event)
        (log.info if event["type"] == "BREACH_RESOLVED" else log.warning)(msg)


class WebhookNotifier:
    """POSTs JSON to a webhook (Slack/Teams/PagerDuty-compatible). Failures never break ingestion."""

    def __init__(self, url: str, timeout: float = 3.0):
        self.url, self.timeout = url, timeout

    def send(self, event: dict) -> None:
        body = json.dumps({"text": format_event(event), "event": event}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except Exception as exc:  # noqa: BLE001
            log.error("webhook failed: %s", exc)


class MemoryNotifier:
    """Collects events (used by tests and the dashboard's live feed)."""

    def __init__(self):
        self.events: list[dict] = []

    def send(self, event: dict) -> None:
        self.events.append(event)
