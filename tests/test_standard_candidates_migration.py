"""Migration 7: stored candidates become version-1 standard records; the producer's working state moves out of the record; work already done is not done again."""
import json
import os
import tempfile
import unittest

from discovery.service import producer
from discovery.service.persistence.schema import LATEST, MIGRATIONS
from discovery.service.persistence.store import DiscoveryStore

LEGACY = {"id": "sandiego_gov:fest@2030-05-01", "title": "Harbor Fest", "start": "2030-05-01T18:00:00-07:00", "end": "2030-05-01T20:00:00-07:00", "timezone": "America/Los_Angeles",
          "venue": "Harbor Hall", "address": "500 Harbor Drive, San Diego, CA", "description": "An evening.", "image_url": None, "image_source": None, "url": "https://www.sandiego.gov/event/fest",
          "categories": [], "price": None, "source": {"name": "City of San Diego", "listing_url": "https://www.sandiego.gov/events"}, "retrieved_at": "2030-04-01T00:00:00-07:00",
          "verified": False, "verification": {"status": "corroborated", "evidence": ["listing date line parsed"], "missing": ["image_url"], "note": "Single-source."},
          "extraction": {"detail_page": True, "detail_v": 2, "detail_failures": 1}}


class Migration(unittest.TestCase):
    def test_a_database_with_the_earlier_form_is_converted_once_and_nothing_is_redone(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "discovery.db")
            old = DiscoveryStore(path, migrations=MIGRATIONS[:6]).open()
            with old.write() as tx:
                tx.execute("INSERT INTO sources (source_key, adapter, name, base_url, created_at, updated_at) VALUES ('sandiego_gov', 'sandiego_gov', 'City', 'https://www.sandiego.gov/events', 1, 1)")
                tx.execute("INSERT INTO candidates (candidate_key, source_id, method, extractor, payload_json, payload_hash, first_seen_at, last_seen_at, updated_at, title) "
                           "VALUES (?, 1, 'adapter', 'x', ?, ?, 1, 1, 1, ?)", (LEGACY["id"], json.dumps(LEGACY), "a" * 64, LEGACY["title"]))
                tx.execute("INSERT INTO processing_work (candidate_id, extractor, state, done_hash, done_version, updated_at) VALUES (1, 'stored-candidate', 'done', ?, 1, 1)", ("a" * 64,))
                tx.execute("INSERT INTO candidate_media (candidate_id, payload_hash, media_json, complete, updated_at) VALUES (1, ?, '{}', 1, 1)", ("a" * 64,))
            old.close()

            store = DiscoveryStore(path).open()
            try:
                self.assertEqual(store.version(), LATEST)
                with store.read() as connection:
                    row = connection.execute("SELECT * FROM candidates").fetchone()
                    doc, state = json.loads(row["payload_json"]), json.loads(row["state_json"])
                    self.assertEqual((doc["schema"], doc["local_id"], doc["starts_at"], doc["ends_at"], doc["address"]), (producer.SCHEMA, "fest@2030-05-01", LEGACY["start"], LEGACY["end"], LEGACY["address"]))
                    self.assertEqual((doc["provenance"]["source_name"], doc["provenance"]["retrieved_at"], doc["verification"]["status"], doc["verification"]["verified"]),
                                     ("City of San Diego", LEGACY["retrieved_at"], "corroborated", False))
                    self.assertEqual(state, LEGACY["extraction"], "the producer's bookkeeping moved out of the record, unread")
                    self.assertNotIn("extraction", doc)
                    record = producer.candidate_from_document(doc, state)
                    self.assertEqual(row["payload_hash"], producer.candidate_digest(record), "the stored identity is the one the program computes")
                    self.assertEqual(connection.execute("SELECT done_hash FROM processing_work").fetchone()[0], row["payload_hash"], "extraction is not redone for unchanged facts")
                    self.assertEqual(connection.execute("SELECT payload_hash FROM candidate_media").fetchone()[0], row["payload_hash"], "nor media enrichment")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
