"""Edge-device simulator: emits one reading per device per minute.

Deterministic when seeded. Injects realistic problems so the pipeline is exercised:
heat episodes (threshold breaches), faulty payloads, duplicates and device dropouts.
"""
from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field

from .processor import Processor
from .timeutil import fmt_ist, now_ts


@dataclass
class DeviceSim:
    device_id: str
    base_c: float
    rng: random.Random
    episode_left: int = 0
    episode_peak: float = 0.0
    offline_left: int = 0

    def next_payload(self, ts: int, p_fault: float, p_episode: float, p_dropout: float, threshold: float):
        # diurnal wave (IST day) + noise
        hour = ((ts + 19800) % 86400) / 3600
        temp = self.base_c + 3.0 * math.sin((hour - 9) / 24 * 2 * math.pi) + self.rng.gauss(0, 0.6)
        if self.episode_left > 0:
            self.episode_left -= 1
            temp = max(temp, threshold + self.rng.uniform(-0.5, self.episode_peak))
        elif self.rng.random() < p_episode:
            self.episode_left = self.rng.randint(3, 12)
            self.episode_peak = self.rng.uniform(1.0, 6.0)
        if self.offline_left > 0:
            self.offline_left -= 1
            return None
        if self.rng.random() < p_dropout:
            self.offline_left = self.rng.randint(3, 6)
            return None
        payload = {"device_id": self.device_id, "timestamp": ts, "temperature_c": round(temp, 2)}
        if self.rng.random() < p_fault:
            kind = self.rng.choice(["missing", "string", "absurd", "nan", "no_device"])
            if kind == "missing":
                payload["temperature_c"] = None
            elif kind == "string":
                payload["temperature_c"] = "ERR"
            elif kind == "absurd":
                payload["temperature_c"] = 999.9
            elif kind == "nan":
                payload["temperature_c"] = float("nan")
            else:
                payload["device_id"] = ""
        return payload


@dataclass
class Fleet:
    n_devices: int = 10
    seed: int = 42
    p_fault: float = 0.01
    p_episode: float = 0.004
    p_dropout: float = 0.002
    devices: list = field(default_factory=list)

    def __post_init__(self):
        rng = random.Random(self.seed)
        self.devices = [
            DeviceSim(f"edge-{i + 1:04d}", rng.uniform(26, 34), random.Random(rng.random()))
            for i in range(self.n_devices)
        ]

    def tick(self, ts: int, threshold: float) -> list[dict]:
        out = []
        for d in self.devices:
            p = d.next_payload(ts, self.p_fault, self.p_episode, self.p_dropout, threshold)
            if p is not None:
                out.append(p)
                if d.rng.random() < 0.002:  # duplicate delivery
                    out.append(dict(p))
        return out


def backfill(proc: Processor, fleet: Fleet, hours: float, end_ts: int | None = None) -> int:
    """Generate `hours` of history at 1 reading/min/device. Returns payloads ingested."""
    end_ts = (now_ts() if end_ts is None else end_ts) // 60 * 60
    start = end_ts - int(hours * 3600)
    n = 0
    for ts in range(start, end_ts, 60):
        for p in fleet.tick(ts, proc.cfg.threshold_c):
            proc.ingest(p, now=ts)
            n += 1
    return n


class LiveSimulator(threading.Thread):
    """Background thread emitting a tick every `interval_s` (60 = real-time cadence)."""

    def __init__(self, proc: Processor, fleet: Fleet, interval_s: float = 60.0):
        super().__init__(daemon=True, name="tempmon-sim")
        self.proc, self.fleet, self.interval_s = proc, fleet, interval_s
        self._halt = threading.Event()
        self.ticks = 0
        self._last_retention = 0.0

    def stop(self):
        self._halt.set()

    def run(self):
        while not self._halt.is_set():
            ts = now_ts()
            for p in self.fleet.tick(ts, self.proc.cfg.threshold_c):
                self.proc.ingest(p, now=ts)
            self.ticks += 1
            if time.time() - self._last_retention > 3600:  # hourly retention sweep
                self.proc.apply_retention()
                self._last_retention = time.time()
            self._halt.wait(self.interval_s)
