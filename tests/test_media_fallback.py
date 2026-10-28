"""MediaRepository.for_event: a primary candidate without a picture borrows the picture (only) of a current duplicate candidate."""
import json
import sqlite3
import unittest

from area_events.contracts.models import ENRICHMENT_VERSION
from area_events.persistence.media import MediaRepository

PHOTO = {"url": "/area_events/media/aaaa1111.png", "credit": "Source", "cached": True}
OTHER = {"url": "/area_events/media/bbbb2222.png", "credit": "Other", "cached": True}
MAP = {"files": {"light": "map-light.png"}, "label": "Park"}
ILLUSTRATION = {"category": "outdoors", "strength": "moderate"}


class ForEvent(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE candidates (candidate_id INTEGER PRIMARY KEY, payload_hash TEXT);
            CREATE TABLE event_candidates (candidate_id INTEGER PRIMARY KEY, event_id INTEGER, role TEXT);
            CREATE TABLE candidate_media (candidate_id INTEGER PRIMARY KEY, payload_hash TEXT, media_json TEXT, illustration_json TEXT, complete INTEGER, updated_at INTEGER, version INTEGER);""")
        self.repository = MediaRepository()

    def add(self, candidate_id, role, media, illustration=None, stale=False, version=ENRICHMENT_VERSION, enriched=True):
        self.db.execute("INSERT INTO candidates VALUES (?, ?)", (candidate_id, f"h{candidate_id}"))
        self.db.execute("INSERT INTO event_candidates VALUES (?, 1, ?)", (candidate_id, role))
        if enriched:
            self.db.execute("INSERT INTO candidate_media VALUES (?, ?, ?, ?, 1, 0, ?)",
                            (candidate_id, "old" if stale else f"h{candidate_id}", json.dumps(media), json.dumps(illustration) if illustration else None, version))

    def get(self):
        return self.repository.for_event(self.db, 1)

    def test_primary_picture_takes_precedence(self):
        self.add(1, "primary", {"image": PHOTO})
        self.add(2, "duplicate", {"image": OTHER})
        self.assertEqual(({"image": PHOTO}, None), self.get())

    def test_missing_primary_picture_falls_back_to_the_duplicates_picture_only(self):
        self.add(1, "primary", {"map": MAP}, ILLUSTRATION)
        self.add(2, "duplicate", {"image": PHOTO, "map": {"files": {"light": "other.png"}}}, {"category": "music"})
        media, illustration = self.get()
        self.assertEqual({"map": MAP, "image": PHOTO}, media)
        self.assertEqual(ILLUSTRATION, illustration)

    def test_primary_without_enrichment_row_still_gets_the_picture(self):
        self.add(1, "primary", None, enriched=False)
        self.add(2, "duplicate", {"image": PHOTO})
        self.assertEqual(({"image": PHOTO}, None), self.get())

    def test_stale_missing_or_pictureless_duplicates_are_not_selected(self):
        self.add(1, "primary", {})
        self.add(2, "duplicate", {"image": PHOTO}, stale=True)
        self.add(3, "duplicate", {"image": PHOTO}, version=ENRICHMENT_VERSION - 1)
        self.add(4, "duplicate", {"image": PHOTO}, enriched=False)
        self.add(5, "duplicate", {"map": MAP})
        self.add(6, "duplicate", {"image": {"credit": "no address"}})
        self.assertEqual((None, None), self.get())

    def test_lowest_candidate_id_wins_among_eligible_duplicates(self):
        self.add(1, "primary", {})
        self.add(9, "duplicate", {"image": OTHER})
        self.add(3, "duplicate", {"image": PHOTO})
        self.assertEqual({"image": PHOTO}, self.get()[0])
        self.assertEqual({"image": PHOTO}, self.get()[0])

    def test_no_image_anywhere_is_unchanged(self):
        self.assertEqual((None, None), self.get())
        self.add(1, "primary", {"map": MAP}, ILLUSTRATION)
        self.add(2, "duplicate", {"map": MAP})
        self.assertEqual(({"map": MAP}, ILLUSTRATION), self.get())
        self.db.execute("UPDATE candidate_media SET media_json='{}' WHERE candidate_id=1")
        self.assertEqual((None, ILLUSTRATION), self.get())


if __name__ == "__main__":
    unittest.main()
