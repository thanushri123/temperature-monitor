import unittest

from tempmon.config import Config
from tempmon.validation import InvalidReading, validate
from tests.helpers import NOW, rd

CFG = Config(db_path=":memory:")


class ValidationTests(unittest.TestCase):
    def ok(self, p):
        return validate(p, CFG, NOW)

    def bad(self, p, code):
        with self.assertRaises(InvalidReading) as cm:
            validate(p, CFG, NOW)
        self.assertEqual(cm.exception.code, code)

    def test_valid_reading(self):
        r = self.ok(rd("d1", NOW - 5, 25.5))
        self.assertEqual((r.device_id, r.ts, r.temp_c), ("d1", NOW - 5, 25.5))

    def test_numeric_string_temperature_is_accepted(self):
        self.assertEqual(self.ok(rd("d1", NOW, "31.5")).temp_c, 31.5)

    def test_missing_temperature(self):
        self.bad(rd("d1", NOW, None), "missing_temperature")
        self.bad(rd("d1", NOW, ""), "missing_temperature")
        self.bad({"device_id": "d1", "timestamp": NOW}, "missing_temperature")

    def test_non_numeric_and_nonfinite(self):
        self.bad(rd("d1", NOW, "ERR"), "bad_temperature")
        self.bad(rd("d1", NOW, float("nan")), "bad_temperature")
        self.bad(rd("d1", NOW, float("inf")), "bad_temperature")
        self.bad(rd("d1", NOW, True), "bad_temperature")

    def test_out_of_sensor_range(self):
        self.bad(rd("d1", NOW, 999), "out_of_range")
        self.bad(rd("d1", NOW, -80), "out_of_range")
        self.assertEqual(self.ok(rd("d1", NOW, 150)).temp_c, 150)  # boundary valid
        self.assertEqual(self.ok(rd("d1", NOW, -50)).temp_c, -50)

    def test_missing_device(self):
        self.bad(rd("", NOW, 20), "missing_device")
        self.bad(rd(None, NOW, 20), "missing_device")
        self.bad(rd("   ", NOW, 20), "missing_device")

    def test_bad_payload_type(self):
        self.bad("hello", "bad_payload")
        self.bad(None, "bad_payload")

    def test_timestamp_problems(self):
        self.bad(rd("d1", None, 20), "bad_timestamp")
        self.bad(rd("d1", "garbage", 20), "bad_timestamp")
        self.bad(rd("d1", NOW + 3600, 20), "future_timestamp")
        self.bad(rd("d1", NOW - 15 * 86400, 20), "too_old")

    def test_small_clock_skew_tolerated(self):
        self.assertEqual(self.ok(rd("d1", NOW + 30, 20)).ts, NOW + 30)

    def test_iso_timestamps_ist_and_utc(self):
        # naive ISO = IST; 09:00 IST == 03:30 UTC
        from tempmon.timeutil import parse_timestamp
        a = parse_timestamp("2026-10-12T09:00:00")
        b = parse_timestamp("2026-10-12T03:30:00Z")
        c = parse_timestamp("2026-10-12T09:00:00+05:30")
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_epoch_millis(self):
        from tempmon.timeutil import parse_timestamp
        self.assertEqual(parse_timestamp(NOW * 1000), NOW)


if __name__ == "__main__":
    unittest.main()
