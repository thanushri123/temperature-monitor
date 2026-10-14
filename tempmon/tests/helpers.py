from tempmon.config import Config
from tempmon.notifier import MemoryNotifier
from tempmon.processor import Processor
from tempmon.store import Store

NOW = 1_800_000_000  # fixed "now" (UTC epoch) for deterministic tests
MIN = 60


def make(threshold=40.0, **kw):
    cfg = Config(db_path=":memory:", threshold_c=threshold, **kw)
    mem = MemoryNotifier()
    return Processor(cfg, Store(":memory:"), [mem]), mem


def rd(device, ts, temp):
    return {"device_id": device, "timestamp": ts, "temperature_c": temp}
