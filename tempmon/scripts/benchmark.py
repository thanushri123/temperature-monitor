"""Measure single-process ingest throughput: python scripts/benchmark.py --devices 1000 --minutes 10"""
import argparse, os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tempmon.config import Config  # noqa: E402
from tempmon.notifier import MemoryNotifier  # noqa: E402
from tempmon.processor import Processor  # noqa: E402
from tempmon.store import Store  # noqa: E402
from tempmon.timeutil import now_ts  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--devices", type=int, default=1000)
ap.add_argument("--minutes", type=int, default=10)
a = ap.parse_args()

db = os.path.join(tempfile.mkdtemp(), "bench.db")
p = Processor(Config(db_path=db), Store(db), [MemoryNotifier()])
end = now_ts() // 60 * 60
payloads = [{"device_id": f"d{d:05d}", "timestamp": end - m * 60,
             "temperature_c": 30 + (d + m) % 15}
            for m in range(a.minutes, 0, -1) for d in range(a.devices)]
t = time.perf_counter()
for x in payloads:
    p.ingest(x, now=end)
dt = time.perf_counter() - t
n = len(payloads)
print(f"{n} readings in {dt:.2f}s = {n/dt:,.0f} readings/s "
      f"(~{n/dt*60:,.0f} devices at 1 reading/min) | alerts opened: {p.store.counts()['alerts']}")