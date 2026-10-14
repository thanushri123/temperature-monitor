"""Ingest pipeline: validate -> evaluate threshold -> persist -> alert.

Alert semantics (threshold check is `temp >= threshold`):
  * Every breaching reading is flagged breach=1 in storage.
  * First breaching reading for a device opens an alert and notifies immediately.
  * While the breach persists we update the alert (peak, count) and send a reminder
    at most every `renotify_minutes` (alert-storm protection).
  * First reading below threshold resolves the alert and sends a RESOLVED notice.
  * Late / out-of-order readings are stored and flagged, but do not change live state.
  * Duplicate deliveries (same device_id + ts) are ignored (idempotent).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .config import Config
from .notifier import Notifier
from .store import Store
from .timeutil import now_ts
from .validation import InvalidReading, Reading, validate


@dataclass
class Result:
    status: str  # "ok" | "rejected" | "duplicate"
    breach: bool = False
    events: tuple = ()
    reason: str = ""
    code: str = ""


class Processor:
    def __init__(self, cfg: Config, store: Store, notifiers: Sequence[Notifier] = ()):
        self.cfg, self.store, self.notifiers = cfg, store, list(notifiers)

    def is_breach(self, temp_c: float) -> bool:
        return temp_c >= self.cfg.threshold_c

    def ingest(self, payload: Mapping[str, Any], now: int | None = None) -> Result:
        now = now_ts() if now is None else now
        try:
            r = validate(payload, self.cfg, now)
        except InvalidReading as exc:
            self.store.log_rejected(now, exc.code, str(exc), payload)
            return Result("rejected", reason=str(exc), code=exc.code)

        breach = self.is_breach(r.temp_c)
        events: list[dict] = []
        with self.store.tx() as c:
            if not self.store.insert_reading(c, r.device_id, r.ts, r.temp_c, breach):
                return Result("duplicate", breach=breach)
            state = self.store.get_state(c, r.device_id)
            if state is None or r.ts > state["last_ts"]:
                events = self._advance(c, r, breach, state)
                c.execute(
                    "INSERT INTO device_state(device_id,last_ts,last_temp_c,in_breach) VALUES (?,?,?,?) "
                    "ON CONFLICT(device_id) DO UPDATE SET last_ts=excluded.last_ts, "
                    "last_temp_c=excluded.last_temp_c, in_breach=excluded.in_breach",
                    (r.device_id, r.ts, r.temp_c, int(breach)),
                )
            # else: out-of-order reading -> stored for history, live state untouched
        for e in events:  # notify AFTER commit so a slow channel never holds the DB lock
            self._notify(e)
        return Result("ok", breach=breach, events=tuple(events))

    def ingest_many(self, payloads: Sequence[Mapping[str, Any]], now: int | None = None) -> list[Result]:
        return [self.ingest(p, now) for p in payloads]

    # ---- internals ----------------------------------------------------
    def _advance(self, c, r: Reading, breach: bool, state) -> list[dict]:
        was = bool(state["in_breach"]) if state else False
        base = {"device_id": r.device_id, "ts": r.ts, "temp_c": r.temp_c,
                "threshold_c": self.cfg.threshold_c}
        open_alert = c.execute(
            "SELECT * FROM alerts WHERE device_id=? AND resolved_ts IS NULL ORDER BY id DESC LIMIT 1",
            (r.device_id,),
        ).fetchone()

        if breach and not open_alert:
            c.execute(
                "INSERT INTO alerts(device_id,opened_ts,last_ts,peak_c,breach_count,last_notified_ts,threshold_c)"
                " VALUES (?,?,?,?,1,?,?)",
                (r.device_id, r.ts, r.ts, r.temp_c, r.ts, self.cfg.threshold_c),
            )
            return [{**base, "type": "BREACH_OPENED", "peak_c": r.temp_c, "breach_count": 1}]

        if breach and open_alert:
            peak = max(open_alert["peak_c"], r.temp_c)
            count = open_alert["breach_count"] + 1
            ev: list[dict] = []
            notified = open_alert["last_notified_ts"]
            if r.ts - notified >= self.cfg.renotify_minutes * 60:
                notified = r.ts
                ev.append({**base, "type": "BREACH_ONGOING", "peak_c": peak, "breach_count": count})
            c.execute(
                "UPDATE alerts SET last_ts=?, peak_c=?, breach_count=?, last_notified_ts=? WHERE id=?",
                (r.ts, peak, count, notified, open_alert["id"]),
            )
            return ev

        if not breach and open_alert:
            c.execute("UPDATE alerts SET resolved_ts=?, last_ts=? WHERE id=?",
                      (r.ts, r.ts, open_alert["id"]))
            return [{**base, "type": "BREACH_RESOLVED", "peak_c": open_alert["peak_c"],
                     "breach_count": open_alert["breach_count"]}]
        return []

    def _notify(self, event: dict) -> None:
        for n in self.notifiers:
            try:
                n.send(event)
            except Exception:  # noqa: BLE001 - alert channel failure must not lose data
                import logging
                logging.getLogger("tempmon").exception("notifier failed")

    # ---- housekeeping -------------------------------------------------
    def apply_retention(self, now: int | None = None) -> dict:
        now = now_ts() if now is None else now
        return self.store.purge_older_than(now - self.cfg.retention_days * 86400)

    def device_status(self, now: int | None = None):
        """Adds `online` / `status` columns to device_state."""
        now = now_ts() if now is None else now
        df = self.store.devices()
        if df.empty:
            return df
        df["age_s"] = now - df["last_ts"]
        df["online"] = df["age_s"] <= self.cfg.offline_after_seconds
        df["status"] = df.apply(
            lambda x: "OFFLINE" if not x["online"] else ("ALERT" if x["in_breach"] else "OK"), axis=1)
        return df
