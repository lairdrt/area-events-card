import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from discovery import __main__ as cli
from discovery.core import FetchError, pacific_offset, to_pacific
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


class Parsing(unittest.TestCase):
    def test_listing_text_start(self):
        self.assertEqual(kpbs._listing_text_start("Tuesday, October 20, 2026 at 4:30 PM"), datetime(2026, 10, 20, 16, 30))
        self.assertEqual(kpbs._listing_text_start("Saturday, October 31, 2026 from 9 PM to 2 AM"), datetime(2026, 10, 31, 21, 0))
        self.assertIsNone(kpbs._listing_text_start("Ongoing until October 31, 2026"))


class FailureHandling(unittest.TestCase):
    def test_failed_source_keeps_existing_data(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "events.json")
            original = {"events": [{"id": "kpbs:x", "start": "2030-01-01T00:00:00-08:00", "source": {"name": "KPBS"}}]}
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(original, handle)
            args = mock.Mock(output=path, no_details=False, limit=None, dry_run=False)
            with mock.patch.object(kpbs, "discover", side_effect=FetchError("offline")):
                self.assertEqual(cli.refresh(args), 1)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), original)


if __name__ == "__main__":
    unittest.main()
