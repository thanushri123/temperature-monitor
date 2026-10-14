import csv
import os
import unittest

from tempmon.simulator import Fleet, backfill
from tempmon.timeutil import IST, fmt_ist, parse_timestamp
from tests.helpers import NOW, make

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SampleDataTests(unittest.TestCase):
    def test_sample_csv_end_to_end(self):
        p, mem = make(40)
        tally = {"ok": 0, "rejected": 0, "duplicate": 0}
        with open(os.path.join(ROOT, "data", "sample_readings.csv"), encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    now = parse_timestamp(row["timestamp"])
                except ValueError:
                    now = NOW
                tally[p.ingest(row, now=now).status] += 1
        self.assertEqual(tally["duplicate"], 1)
        self.assertEqual(tally["rejected"], 5)  # missing, ERR, 999, no device, bad timestamp
        self.assertEqual(tally["ok"], 10 + 2)
        kinds = [(e["device_id"], e["type"]) for e in mem.events]
        self.assertEqual(kinds, [("edge-0001", "BREACH_OPENED"), ("edge-0001", "BREACH_RESOLVED")])
        self.assertEqual(mem.events[0]["temp_c"], 40.0)   # exactly-at-threshold opens alert


class SimulatorTests(unittest.TestCase):
    def test_seeded_fleet_is_deterministic(self):
        runs = []
        for _ in range(2):
            p, _ = make(40)
            backfill(p, Fleet(5, seed=7), hours=6, end_ts=NOW)
            runs.append((p.store.counts()["readings"], p.store.counts()["alerts"]))
        self.assertEqual(runs[0], runs[1])

    def test_one_reading_per_minute_per_device(self):
        p, _ = make(40)
        f = Fleet(3, seed=1, p_fault=0, p_episode=0, p_dropout=0)
        backfill(p, f, hours=1, end_ts=NOW)
        self.assertEqual(p.store.counts()["readings"], 3 * 60)

    def test_high_volume_ingest(self):
        p, _ = make(40)
        backfill(p, Fleet(50, seed=3), hours=2, end_ts=NOW)  # ~6000 readings
        c = p.store.counts()
        self.assertGreater(c["readings"], 5000)
        self.assertGreater(c["rejected"], 0)  # faults were injected and quarantined


class IstTests(unittest.TestCase):
    def test_ist_formatting(self):
        ts = parse_timestamp("2026-10-12T03:30:00Z")
        self.assertEqual(fmt_ist(ts), "12-Oct-2026 09:00:00 IST")
        self.assertEqual(fmt_ist(None), "-")
        self.assertEqual(IST.utcoffset(None).total_seconds(), 19800)


if __name__ == "__main__":
    unittest.main()
