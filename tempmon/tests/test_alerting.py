import unittest

from tests.helpers import MIN, NOW, make, rd


def types(mem):
    return [e["type"] for e in mem.events]


class ThresholdTests(unittest.TestCase):
    def test_below_threshold_no_alert(self):
        p, mem = make(40)
        r = p.ingest(rd("d1", NOW, 39.99), now=NOW)
        self.assertEqual(r.status, "ok")
        self.assertFalse(r.breach)
        self.assertEqual(mem.events, [])
        self.assertTrue(p.store.alerts().empty)

    def test_exactly_at_threshold_alerts(self):
        """Spec: 'reaches OR exceeds' -> equality must alert."""
        p, mem = make(40)
        r = p.ingest(rd("d1", NOW, 40.0), now=NOW)
        self.assertTrue(r.breach)
        self.assertEqual(types(mem), ["BREACH_OPENED"])

    def test_above_threshold_alerts(self):
        p, mem = make(40)
        p.ingest(rd("d1", NOW, 55.5), now=NOW)
        self.assertEqual(types(mem), ["BREACH_OPENED"])
        self.assertEqual(mem.events[0]["temp_c"], 55.5)
        self.assertEqual(mem.events[0]["threshold_c"], 40)

    def test_custom_threshold_respected(self):
        p, mem = make(25)
        p.ingest(rd("d1", NOW, 26), now=NOW)
        self.assertEqual(types(mem), ["BREACH_OPENED"])

    def test_every_breaching_reading_is_flagged_in_storage(self):
        p, _ = make(40)
        for i, t in enumerate([41, 42, 43]):
            p.ingest(rd("d1", NOW + i * MIN, t), now=NOW + i * MIN)
        df = p.store.history("d1", 0, NOW + 10 * MIN)
        self.assertEqual(list(df["breach"]), [1, 1, 1])


class LifecycleTests(unittest.TestCase):
    def test_sustained_breach_dedupes_notifications(self):
        p, mem = make(40, renotify_minutes=15)
        for i in range(10):  # 10 minutes of breach
            p.ingest(rd("d1", NOW + i * MIN, 45 + i), now=NOW + i * MIN)
        self.assertEqual(types(mem), ["BREACH_OPENED"])
        a = p.store.alerts().iloc[0]
        self.assertEqual(a["breach_count"], 10)
        self.assertEqual(a["peak_c"], 54)
        self.assertEqual(len(p.store.alerts()), 1)

    def test_reminder_after_renotify_interval(self):
        p, mem = make(40, renotify_minutes=15)
        for i in range(31):
            p.ingest(rd("d1", NOW + i * MIN, 45), now=NOW + i * MIN)
        self.assertEqual(types(mem), ["BREACH_OPENED", "BREACH_ONGOING", "BREACH_ONGOING"])

    def test_resolution_when_back_below_threshold(self):
        p, mem = make(40)
        p.ingest(rd("d1", NOW, 41), now=NOW)
        p.ingest(rd("d1", NOW + MIN, 44), now=NOW + MIN)
        p.ingest(rd("d1", NOW + 2 * MIN, 39.9), now=NOW + 2 * MIN)
        self.assertEqual(types(mem), ["BREACH_OPENED", "BREACH_RESOLVED"])
        self.assertEqual(mem.events[-1]["peak_c"], 44)
        a = p.store.alerts().iloc[0]
        self.assertEqual(a["resolved_ts"], NOW + 2 * MIN)
        self.assertTrue(p.store.alerts(only_open=True).empty)

    def test_new_breach_after_resolution_opens_new_alert(self):
        p, mem = make(40)
        for i, t in enumerate([41, 30, 42]):
            p.ingest(rd("d1", NOW + i * MIN, t), now=NOW + i * MIN)
        self.assertEqual(types(mem), ["BREACH_OPENED", "BREACH_RESOLVED", "BREACH_OPENED"])
        self.assertEqual(len(p.store.alerts()), 2)

    def test_devices_are_independent(self):
        p, mem = make(40)
        p.ingest(rd("a", NOW, 50), now=NOW)
        p.ingest(rd("b", NOW, 30), now=NOW)
        p.ingest(rd("b", NOW + MIN, 31), now=NOW + MIN)
        self.assertEqual([e["device_id"] for e in mem.events], ["a"])
        st = p.device_status(now=NOW + MIN).set_index("device_id")
        self.assertEqual(st.loc["a", "status"], "ALERT")
        self.assertEqual(st.loc["b", "status"], "OK")

    def test_normal_readings_keep_monitoring_silently(self):
        p, mem = make(40)
        for i in range(60):
            p.ingest(rd("d1", NOW + i * MIN, 30 + (i % 5)), now=NOW + i * MIN)
        self.assertEqual(mem.events, [])
        self.assertEqual(p.store.counts()["readings"], 60)


class RobustnessTests(unittest.TestCase):
    def test_invalid_readings_never_alert_and_are_audited(self):
        p, mem = make(40)
        for bad in [rd("d1", NOW, None), rd("d1", NOW, "ERR"), rd("d1", NOW, 999),
                    rd("", NOW, 50), rd("d1", "bad", 50)]:
            self.assertEqual(p.ingest(bad, now=NOW).status, "rejected")
        self.assertEqual(mem.events, [])
        self.assertEqual(p.store.counts()["readings"], 0)
        codes = set(p.store.rejected()["code"])
        self.assertEqual(codes, {"missing_temperature", "bad_temperature", "out_of_range",
                                 "missing_device", "bad_timestamp"})

    def test_invalid_reading_does_not_resolve_open_alert(self):
        p, mem = make(40)
        p.ingest(rd("d1", NOW, 45), now=NOW)
        p.ingest(rd("d1", NOW + MIN, None), now=NOW + MIN)
        self.assertEqual(types(mem), ["BREACH_OPENED"])
        self.assertEqual(len(p.store.alerts(only_open=True)), 1)

    def test_duplicate_delivery_is_idempotent(self):
        p, mem = make(40)
        self.assertEqual(p.ingest(rd("d1", NOW, 45), now=NOW).status, "ok")
        self.assertEqual(p.ingest(rd("d1", NOW, 45), now=NOW).status, "duplicate")
        self.assertEqual(p.store.counts()["readings"], 1)
        self.assertEqual(types(mem), ["BREACH_OPENED"])
        self.assertEqual(p.store.alerts().iloc[0]["breach_count"], 1)

    def test_out_of_order_reading_stored_but_does_not_flip_state(self):
        p, mem = make(40)
        p.ingest(rd("d1", NOW + 2 * MIN, 30), now=NOW + 2 * MIN)       # latest: normal
        p.ingest(rd("d1", NOW, 50), now=NOW + 2 * MIN)                 # late, breaching
        self.assertEqual(p.store.counts()["readings"], 2)
        self.assertEqual(int(p.store.history("d1", 0, NOW + 9 * MIN)["breach"].sum()), 1)
        self.assertEqual(mem.events, [])  # stale data must not page anyone
        self.assertEqual(p.device_status(now=NOW + 2 * MIN).iloc[0]["status"], "OK")

    def test_notifier_failure_does_not_lose_data(self):
        class Boom:
            def send(self, e): raise RuntimeError("smtp down")
        p, mem = make(40)
        p.notifiers.insert(0, Boom())
        r = p.ingest(rd("d1", NOW, 50), now=NOW)
        self.assertEqual(r.status, "ok")
        self.assertEqual(types(mem), ["BREACH_OPENED"])  # later channels still fire
        self.assertEqual(p.store.counts()["readings"], 1)

    def test_offline_detection(self):
        p, _ = make(40, offline_after_seconds=180)
        p.ingest(rd("d1", NOW, 30), now=NOW)
        self.assertEqual(p.device_status(now=NOW + 120).iloc[0]["status"], "OK")
        self.assertEqual(p.device_status(now=NOW + 181).iloc[0]["status"], "OFFLINE")

    def test_empty_status(self):
        p, _ = make(40)
        self.assertTrue(p.device_status().empty)


if __name__ == "__main__":
    unittest.main()
