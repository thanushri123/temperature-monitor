import unittest

from tests.helpers import MIN, NOW, make, rd

DAY = 86400


class RetentionTests(unittest.TestCase):
    def test_default_retention_is_14_days(self):
        p, _ = make()
        self.assertEqual(p.cfg.retention_days, 14)

    def test_purge_removes_only_data_older_than_14_days(self):
        p, _ = make(40)
        for d in (15, 14.5, 13.9, 7, 0):
            ts = NOW - int(d * DAY)
            # ingest with "now" = reading time so old data is accepted at write-time
            p.ingest(rd("d1", ts, 30), now=ts)
        self.assertEqual(p.store.counts()["readings"], 5)
        out = p.apply_retention(now=NOW)
        self.assertEqual(out["readings"], 2)           # the 15d and 14.5d rows
        self.assertEqual(p.store.counts()["readings"], 3)
        oldest = p.store.counts()["oldest_ts"]
        self.assertGreaterEqual(oldest, NOW - 14 * DAY)

    def test_boundary_exactly_14_days_is_kept(self):
        p, _ = make(40)
        ts = NOW - 14 * DAY
        p.ingest(rd("d1", ts, 30), now=ts)
        p.apply_retention(now=NOW)
        self.assertEqual(p.store.counts()["readings"], 1)

    def test_history_queryable_across_14_days(self):
        p, _ = make(40)
        for d in range(0, 14):
            ts = NOW - d * DAY
            p.ingest(rd("d1", ts, 30 + d), now=ts)
        df = p.store.history("d1", NOW - 14 * DAY, NOW)
        self.assertEqual(len(df), 14)
        self.assertTrue(df["ts"].is_monotonic_increasing)
        self.assertEqual(len(p.store.history("d1", NOW - 2 * DAY, NOW)), 3)
        self.assertTrue(p.store.history("other", 0, NOW).empty)

    def test_open_alerts_survive_purge_resolved_old_ones_do_not(self):
        p, _ = make(40)
        old = NOW - 20 * DAY
        p.ingest(rd("a", old, 50), now=old)             # opened 20d ago, never resolved
        p.ingest(rd("b", old, 50), now=old)
        p.ingest(rd("b", old + MIN, 30), now=old + MIN)  # resolved 20d ago
        p.apply_retention(now=NOW)
        remaining = p.store.alerts()
        self.assertEqual(list(remaining["device_id"]), ["a"])

    def test_write_rejects_data_already_past_retention(self):
        p, _ = make(40)
        r = p.ingest(rd("d1", NOW - 15 * DAY, 30), now=NOW)
        self.assertEqual((r.status, r.code), ("rejected", "too_old"))

    def test_rejected_audit_rows_are_purged_too(self):
        p, _ = make(40)
        old = NOW - 20 * DAY
        p.ingest(rd("d1", old, None), now=old)
        self.assertEqual(p.store.counts()["rejected"], 1)
        p.apply_retention(now=NOW)
        self.assertEqual(p.store.counts()["rejected"], 0)


if __name__ == "__main__":
    unittest.main()
