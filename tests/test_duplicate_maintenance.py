"""High-confidence duplicate discovery and atomic event reconciliation."""
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from area_events import app as app_module
from area_events.deduplication.exact import ExactMatch
from area_events.persistence.events import DuplicateMaintenanceError
from tests.records import epoch
from tests.test_discovery_service import Clock, FakeClient


class DuplicateMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = os.path.join(self.temp.name, "discovery.db")
        self.clock = Clock()
        self.app = app_module.build(self.db_path, clock=self.clock, fetch_client=FakeClient({}))
        self.addCleanup(self.app.close)
        self.app.register_known_sources()
        self._seed()
        self.maintenance = app_module.duplicate_maintenance(self.db_path, ExactMatch(), clock=lambda: epoch("2026-10-01T00:00:00+00:00"))

    def _seed(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA foreign_keys=ON")
        sources = {row[1]: row[0] for row in connection.execute("SELECT source_id,source_key FROM sources")}
        variants = (
            ("mission_trails:old-14", "Holiday Marketplace at Mission Trails Regional Park", "5000 Juniper Canyon Road, San Diego, CA 92119", "https://mtrp.org/marketplace/", "2026-11-14"),
            ("mission_trails:new-14", "Holiday Marketplace Benefitting Mission Trails Regional Park", "5000 Juniper Canyon Road, San Diego, CA 92119", "https://mtrp.org/marketplace/", "2026-11-14"),
            ("other:similar", "Holiday Marketplace Benefitting Mission Trails Regional Park", "900 Other Street, San Diego, CA", "https://other.example/marketplace/", "2026-11-14"),
            ("mission_trails:old-15", "Holiday Marketplace at Mission Trails Regional Park", "5000 Juniper Canyon Road, San Diego, CA 92119", "https://mtrp.org/marketplace/", "2026-11-15"),
            ("mission_trails:new-15", "Holiday Marketplace Benefitting Mission Trails Regional Park", "5000 Juniper Canyon Road, San Diego, CA 92119", "https://mtrp.org/marketplace/", "2026-11-15"),
        )
        try:
            for index, (candidate_key, title, address, url, day) in enumerate(variants, 1):
                source_id = sources["sandiego_gov"] if index in (1, 2, 4, 5) else sources["ut_austin"]
                start = epoch(f"{day}T09:00:00-08:00")
                end = epoch(f"{day}T15:00:00-08:00")
                payload_hash = hashlib.sha256(candidate_key.encode()).hexdigest()
                candidate_id = connection.execute("""INSERT INTO candidates(candidate_key,source_id,method,extractor,title,starts_at,payload_json,payload_hash,
                    first_seen_at,last_seen_at,updated_at) VALUES(?,?,'adapter','test',?,?, '{}', ?,1,1,?) RETURNING candidate_id""",
                    (candidate_key, source_id, title, start, payload_hash, index)).fetchone()[0]
                result = json.dumps({"fields": {"title": {"value": title}}})
                extraction_id = connection.execute("""INSERT INTO extractions(candidate_id,event_id,candidate_payload_hash,extractor,version,status,result_json,
                    result_hash,created_at) VALUES(?,NULL,?,'test',1,'complete',?,?,?) RETURNING extraction_id""",
                    (candidate_id, payload_hash, result, hashlib.sha256(result.encode()).hexdigest(), index)).fetchone()[0]
                event_key = f"ev-maintenance-{index}"
                event_id = connection.execute("""INSERT INTO events(event_key,title,description,starts_at,ends_at,timezone,venue_name,location_text,address,
                    location_status,source_url,status,missing_json,revision,extraction_id,first_extracted_at,updated_at)
                    VALUES(?,?,'Event description',?,?, 'America/Los_Angeles','Mission Trails Regional Park',NULL,?,'address',?,'complete','[]',1,?,?,?)
                    RETURNING event_id""", (event_key, title, start, end, address, url, extraction_id, index, index)).fetchone()[0]
                connection.execute("UPDATE extractions SET event_id=? WHERE extraction_id=?", (event_id, extraction_id))
                connection.execute("INSERT INTO event_candidates(candidate_id,event_id,role,linked_at) VALUES(?,?,'primary',?)", (candidate_id, event_id, index))
                connection.execute("INSERT INTO event_revisions(event_id,revision,extraction_id,changed_json,created_at) VALUES(?,1,?,'{}',?)", (event_id, extraction_id, index))
            connection.commit()
        finally:
            connection.close()

    def _counts(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("sources", "candidates", "events", "event_candidates", "extractions", "event_revisions")}

    def test_events_that_state_no_place_are_scanned_by_title_and_start(self):
        """The Bates listing duplicates (a clean title and a "Date: Sat." variant, no place stated) must be found; unrelated events at the same start must not."""
        def row(event_id, title, start, **more):
            base = {"event_id": event_id, "event_key": f"ev-{event_id}", "title": title, "starts_at": start, "ends_at": None, "venue_name": None, "address": None, "location_text": None,
                    "revision": 1, "source_url": "https://batesnutfarm.biz/pages/events", "description": None, "timezone": None}
            return {**base, **more}
        start = epoch("2026-11-07T09:00:00-08:00")
        rows = [row(1, "Christmas on the Farm Arts & Crafts Fair", start), row(2, "East County Cruisers Car Show and Toy Drive", start),
                row(3, "Christmas on the Farm Arts & Crafts Fair Date: Sat.", start), row(4, "East County Cruisers Car Show and Toy Drive Date", start),
                row(5, "Christmas on the Farm Arts & Crafts Fair", start + 86400), row(6, "Christmas on the Farm Arts & Crafts Fair Date: Sat.", start + 86400),
                row(7, "Lone event", start), row(8, "Harbor Concert", start, venue_name="Harbor Hall"), row(9, "Harbor Concert", start)]
        groups = self.maintenance._eligible_pairs(rows)
        self.assertEqual([group["event_ids"] for group in groups], [(1, 3), (2, 4), (5, 6)])
        self.assertTrue(all(group["eligible"] for group in groups))

    def test_read_only_scan_finds_only_the_high_confidence_pair(self):
        before = os.stat(self.db_path).st_mtime_ns
        groups = self.maintenance.scan()
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group["eligible"] for group in groups))
        self.assertEqual([group["event_keys"] for group in groups], [("ev-maintenance-1", "ev-maintenance-2"), ("ev-maintenance-4", "ev-maintenance-5")])
        self.assertEqual(os.stat(self.db_path).st_mtime_ns, before, "scan uses a URI read-only SQLite connection")

    def test_clean_preserves_candidates_sources_links_and_revision_history(self):
        before = self._counts()
        keys = (("ev-maintenance-1", "ev-maintenance-2"), ("ev-maintenance-4", "ev-maintenance-5"))
        result = self.maintenance.clean(keys, limit=2)
        self.assertEqual([(row["survivor"], row["removed"]) for row in result],
                         [("ev-maintenance-1", "ev-maintenance-2"), ("ev-maintenance-4", "ev-maintenance-5")])
        after = self._counts()
        self.assertEqual((before["sources"], before["candidates"], before["event_candidates"], before["extractions"]),
                         (after["sources"], after["candidates"], after["event_candidates"], after["extractions"]))
        self.assertEqual(after["events"], before["events"] - 2)
        self.assertEqual(after["event_revisions"], before["event_revisions"] + 2)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT event_key FROM events WHERE event_id=1").fetchone()[0], "ev-maintenance-1")
            links = db.execute("SELECT c.candidate_key,ec.role,e.event_key FROM event_candidates ec JOIN candidates c USING(candidate_id) JOIN events e USING(event_id) ORDER BY c.candidate_key").fetchall()
            self.assertEqual(links, [("mission_trails:new-14", "duplicate", "ev-maintenance-1"),
                                     ("mission_trails:new-15", "duplicate", "ev-maintenance-4"),
                                     ("mission_trails:old-14", "primary", "ev-maintenance-1"),
                                     ("mission_trails:old-15", "primary", "ev-maintenance-4"),
                                     ("other:similar", "primary", "ev-maintenance-3")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_revisions WHERE event_id=1").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_revisions WHERE event_id=4").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM extractions WHERE event_id=1").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM extractions WHERE event_id=4").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT title FROM events WHERE event_id=1").fetchone()[0], "Holiday Marketplace Benefitting Mission Trails Regional Park")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM changes WHERE entity='event' AND kind='event.reconciled'").fetchone()[0], 4)
        self.assertEqual(self.maintenance.scan(), (), "a second scan finds no additional duplicate groups")
        with self.assertRaisesRegex(DuplicateMaintenanceError, "no longer an unambiguous duplicate"):
            self.maintenance.clean(keys)

    def test_transaction_rolls_back_all_reconciliation_writes_on_delete_failure(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("CREATE TRIGGER refuse_selected_event_delete BEFORE DELETE ON events WHEN OLD.event_key='ev-maintenance-2' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
            db.commit()
        before = self._counts()
        with self.assertRaisesRegex(DuplicateMaintenanceError, "transaction failed and was rolled back"):
            self.maintenance.clean((("ev-maintenance-1", "ev-maintenance-2"),))
        self.assertEqual(self._counts(), before)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_candidates WHERE role='primary'").fetchone()[0], 5)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v1_decide_only_plugin_is_rejected_only_for_maintenance(self):
        class DecideOnlyV1:
            name, version = "legacy-v1", 1

            def decide(self, incoming, lookup):
                return None  # maintenance must reject before any decision is requested

        with self.assertRaisesRegex(DuplicateMaintenanceError, "discovery.dedup.maintenance.v1"):
            app_module.duplicate_maintenance(self.db_path, DecideOnlyV1())
        self.assertEqual(len(self.maintenance.scan()), 2, "rejection does not modify stored events")

    def test_selected_pair_must_be_explicit_and_currently_unambiguous(self):
        for group in ((), (("missing-a", "missing-b"),), (("ev-maintenance-1",),)):
            with self.subTest(group=group), self.assertRaises(DuplicateMaintenanceError):
                self.maintenance.clean(group)

    def test_three_way_ambiguity_is_reported_and_cannot_be_cleaned_as_a_pair(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE events SET address='5000 Juniper Canyon Road, San Diego, CA 92119',source_url='https://mtrp.org/marketplace/' WHERE event_key='ev-maintenance-3'")
            db.commit()
        groups = self.maintenance.scan()
        self.assertTrue(any(not group["eligible"] and len(group["event_keys"]) == 3 for group in groups))
        with self.assertRaisesRegex(DuplicateMaintenanceError, "no longer an unambiguous duplicate"):
            self.maintenance.clean((("ev-maintenance-1", "ev-maintenance-2"),))

    def test_missing_primary_ownership_or_incomplete_history_is_refused(self):
        mutations = (
            ("UPDATE event_candidates SET role='duplicate' WHERE event_id=2", "exactly one primary candidate"),
            ("UPDATE events SET revision=2 WHERE event_id=2", "revision history is incomplete"),
        )
        for statement, message in mutations:
            with self.subTest(statement=statement):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                copied = os.path.join(temp.name, "copy.sqlite3")
                with closing(sqlite3.connect(self.db_path)) as source, closing(sqlite3.connect(copied)) as target:
                    source.backup(target)
                maintenance = app_module.duplicate_maintenance(copied, ExactMatch(), clock=lambda: epoch("2026-10-01T00:00:00+00:00"))
                with closing(sqlite3.connect(copied)) as db:
                    db.execute(statement)
                    db.commit()
                before = {}
                with closing(sqlite3.connect(copied)) as db:
                    before = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                              for table in ("events", "event_candidates", "event_revisions", "extractions")}
                with self.assertRaisesRegex(DuplicateMaintenanceError, message):
                    maintenance.clean((("ev-maintenance-1", "ev-maintenance-2"),))
                with closing(sqlite3.connect(copied)) as db:
                    after = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                             for table in before}
                    self.assertEqual(after, before)
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
