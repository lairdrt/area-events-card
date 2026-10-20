import unittest
from datetime import datetime, timezone

from discovery.core import clock_range, localize_pacific, to_pacific
from discovery.sources import kpbs


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class PacificTime(unittest.TestCase):
    def test_dst_boundaries(self):
        self.assertEqual(to_pacific(utc(2026, 3, 8, 9, 59)).isoformat(), "2026-03-08T01:59:00-08:00")
        self.assertEqual(to_pacific(utc(2026, 3, 8, 10, 0)).isoformat(), "2026-03-08T03:00:00-07:00")
        self.assertEqual(to_pacific(utc(2026, 11, 1, 8, 59)).isoformat(), "2026-11-01T01:59:00-07:00")
        self.assertEqual(to_pacific(utc(2026, 11, 1, 9, 0)).isoformat(), "2026-11-01T01:00:00-08:00")

    def test_known_event(self):
        self.assertEqual(to_pacific(utc(2026, 10, 20, 23, 30)).isoformat(), "2026-10-20T16:30:00-07:00")


class Clock(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(clock_range("2 - 7pm"), ((14, 0), (19, 0)))
        self.assertEqual(clock_range("11 - 1pm"), ((11, 0), (13, 0)))
        self.assertEqual(clock_range("12pm"), ((12, 0), None))
        self.assertEqual(clock_range("7:30 pm - 11:30 pm"), ((19, 30), (23, 30)))
        self.assertEqual(clock_range("all day"), (None, None))

    def test_localize(self):
        self.assertEqual(localize_pacific(datetime(2026, 12, 1, 9)).isoformat(), "2026-12-01T09:00:00-08:00")
        self.assertEqual(localize_pacific(datetime(2026, 7, 1, 9)).isoformat(), "2026-07-01T09:00:00-07:00")


class Parsing(unittest.TestCase):
    def test_listing_text_start(self):
        self.assertEqual(kpbs._listing_text_start("Tuesday, October 20, 2026 at 4:30 PM"), datetime(2026, 10, 20, 16, 30))
        self.assertEqual(kpbs._listing_text_start("Saturday, October 31, 2026 from 9 PM to 2 AM"), datetime(2026, 10, 31, 21, 0))
        self.assertIsNone(kpbs._listing_text_start("Ongoing until October 31, 2026"))


if __name__ == "__main__":
    unittest.main()
