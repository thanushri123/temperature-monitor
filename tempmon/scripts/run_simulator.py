"""Headless producer: python scripts/run_simulator.py --devices 20 --interval 60 --backfill-hours 24"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tempmon.runtime import build, setup_logging  # noqa: E402
from tempmon.simulator import Fleet, LiveSimulator, backfill  # noqa: E402
from tempmon.timeutil import fmt_ist, now_ts  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", type=int, default=10)
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between ticks (60 = real-time)")
    ap.add_argument("--backfill-hours", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    setup_logging()
    proc = build()
    fleet = Fleet(a.devices, a.seed)
    if a.backfill_hours:
        n = backfill(proc, fleet, a.backfill_hours)
        print(f"Backfilled {n} payloads up to {fmt_ist(now_ts())}")
    sim = LiveSimulator(proc, fleet, a.interval)
    sim.start()
    print(f"Live: {a.devices} devices, tick every {a.interval}s, threshold {proc.cfg.threshold_c}°C. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
