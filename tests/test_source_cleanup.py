"""Operator source cleanup preserves shared records and refuses ambiguous ownership."""
import os
import sqlite3
import tempfile
import unittest

from area_events import app as app_module
from area_events.control.service import CreateAreaRequest, CreateSourceRequest
from area_events.persistence.repositories import SourceCleanupError
from area_events.persistence.store import create_verified_database_backup
from tests.db import database
from tests.test_discovery_service import Clock, FakeClient


OPTIONS = {"url": "https://calendar.example", "days": 30, "max_pages": 4, "timezone": "America/Los_Angeles"}


class SourceCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app_root = os.path.join(self.temp.name, "application")
        os.makedirs(self.app_root)
        self.db_path = os.path.join(self.app_root, "data", "discovery.db")
        self.clock = Clock()
        self.app = app_module.build(self.db_path, clock=self.clock, fetch_client=FakeClient({}))
        self.addCleanup(self.app.close)
        self.app.register_known_sources()
        self.retired = self.app.control.create_source(CreateSourceRequest("retired-calendar", "localist", "Retired Calendar", False, 60, OPTIONS)).source
        self.other = self.app.control.create_source(CreateSourceRequest("other-calendar", "localist", "Other Calendar", True, 60,
                                                                          {**OPTIONS, "url": "https://other.example"})).source
        self.area = self.app.control.create_area(CreateAreaRequest("Test Area", 32.7, -117.1, 25,
                                                                     sources=("retired-calendar", "other-calendar"))).area
        self._seed_records()

    def _seed_records(self):
        with database(self.app).write() as tx:
            source_id = tx.execute("SELECT source_id FROM sources WHERE source_key='retired-calendar'").fetchone()[0]
            other_id = tx.execute("SELECT source_id FROM sources WHERE source_key='other-calendar'").fetchone()[0]
            tx.execute("INSERT INTO jobs(job_key,kind,source_id,state,next_run_at,created_at,updated_at) VALUES('crawl_source:retired-calendar','crawl_source',?,'ready',1,1,1)", (source_id,))
            page_ids = []
            for i in range(2):
                page_ids.append(tx.execute("INSERT INTO pages(url_key,url,first_seen_at) VALUES(?,?,?) RETURNING page_id",
                                           (f"{i + 1:032x}", f"https://example.com/{i}", int(self.clock()))).fetchone()[0])
            for sid, page_id in ((source_id, page_ids[0]), (source_id, page_ids[1]), (other_id, page_ids[1])):
                tx.execute("INSERT INTO source_pages(source_id,page_id,role,first_seen_at,last_seen_at) VALUES(?,?,'event',?,?)",
                           (sid, page_id, int(self.clock()), int(self.clock())))
            candidate_ids = {}
            for sid, key, page_id in ((source_id, "retired:exclusive", page_ids[0]), (source_id, "retired:shared", page_ids[1]),
                                      (other_id, "other:shared", page_ids[1])):
                candidate_ids[key] = tx.execute("""INSERT INTO candidates(candidate_key,source_id,page_id,method,extractor,title,starts_at,payload_json,
                    payload_hash,state_json,first_seen_at,last_seen_at,updated_at) VALUES(?,?,?,'adapter','test','Test event',1000,'{}',?,?,1,1,1)
                    RETURNING candidate_id""", (key, sid, page_id, "a" * 64, "{}")).fetchone()[0]
            event_ids = {}
            for key in ("event-exclusive", "event-shared"):
                event_ids[key] = tx.execute("""INSERT INTO events(event_key,title,starts_at,location_status,status,first_extracted_at,updated_at)
                    VALUES(?,'Test event',1000,'unknown','complete',1,1) RETURNING event_id""", (key,)).fetchone()[0]
            for ckey, ekey, role in (("retired:exclusive", "event-exclusive", "primary"), ("retired:shared", "event-shared", "duplicate"),
                                     ("other:shared", "event-shared", "primary")):
                tx.execute("INSERT INTO event_candidates(candidate_id,event_id,role,linked_at) VALUES(?,?,?,1)",
                           (candidate_ids[ckey], event_ids[ekey], role))
            tx.execute("INSERT INTO evidence(candidate_id,field,kind,quote,page_id) VALUES(?,'title','extracted','Test event',?)",
                       (candidate_ids["retired:exclusive"], page_ids[0]))
            tx.execute("INSERT INTO processing_work(candidate_id,extractor,state,updated_at) VALUES(?,'test','ready',1)",
                       (candidate_ids["retired:exclusive"],))
    def test_cleanup_preserves_shared_event_and_page_and_removes_owned_graph(self):
        cleanup = self.app.persistence.source_cleanup
        plan = cleanup.preview("retired-calendar")
        self.assertEqual(plan["counts"]["candidates"], 2)
        self.assertEqual(plan["counts"]["events_exclusive"], 1)
        self.assertEqual(plan["counts"]["events_shared_preserved"], 1)
        self.assertEqual(plan["counts"]["pages_exclusive"], 1)
        self.assertEqual(len(plan["shared_events"]), 1)

        removed = cleanup.remove("retired-calendar")
        self.assertEqual(removed["candidates"], 2)
        with database(self.app).read() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sources WHERE source_key='retired-calendar'").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidates WHERE candidate_key LIKE 'retired:%'").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT 1 FROM events WHERE event_key='event-exclusive'").fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM events WHERE event_key='event-shared'").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_candidates WHERE event_id=(SELECT event_id FROM events WHERE event_key='event-shared')").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT 1 FROM pages WHERE url='https://example.com/0'").fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM pages WHERE url='https://example.com/1'").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_pages WHERE source_id=(SELECT source_id FROM sources WHERE source_key='other-calendar')").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM evidence WHERE candidate_id IN (SELECT candidate_id FROM candidates WHERE candidate_key LIKE 'retired:%')").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM processing_work WHERE candidate_id NOT IN (SELECT candidate_id FROM candidates)").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM changes WHERE entity='source' AND entity_key='retired-calendar'").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            area_id = db.execute("SELECT area_id FROM areas WHERE area_key=?", (self.area.area_key,)).fetchone()[0]
            attached = db.execute("SELECT source_id FROM area_sources WHERE area_id=?", (area_id,)).fetchall()
            self.assertEqual([r[0] for r in attached], [db.execute("SELECT source_id FROM sources WHERE source_key='other-calendar'").fetchone()[0]])

    def test_cleanup_refuses_builtin_leased_and_ambiguous_shared_history(self):
        with self.assertRaisesRegex(SourceCleanupError, "built-in"):
            self.app.persistence.source_cleanup.remove("sandiego_gov")
        with database(self.app).write() as tx:
            tx.execute("UPDATE jobs SET state='leased',lease_token='token',lease_until=10 WHERE source_id=(SELECT source_id FROM sources WHERE source_key='retired-calendar')")
        with self.assertRaisesRegex(SourceCleanupError, "leased"):
            self.app.persistence.source_cleanup.remove("retired-calendar")
        with database(self.app).write() as tx:
            tx.execute("UPDATE jobs SET state='ready',lease_token=NULL,lease_until=NULL WHERE source_id=(SELECT source_id FROM sources WHERE source_key='retired-calendar')")
        with database(self.app).write() as tx:
            candidate_id = tx.execute("SELECT candidate_id FROM candidates WHERE candidate_key='retired:shared'").fetchone()[0]
            event_id = tx.execute("SELECT event_id FROM events WHERE event_key='event-shared'").fetchone()[0]
            extraction_id = tx.execute("""INSERT INTO extractions(candidate_id,event_id,candidate_payload_hash,extractor,version,status,result_json,result_hash,created_at)
                VALUES(?,?,?,'test',1,'complete','{}',?,1) RETURNING extraction_id""",
                (candidate_id, event_id, "a" * 64, "b" * 64)).fetchone()[0]
            tx.execute("INSERT INTO event_revisions(event_id,revision,extraction_id,changed_json,created_at) VALUES(?,1,?,'{}',1)",
                       (event_id, extraction_id))
        with self.assertRaisesRegex(SourceCleanupError, "shared event revision"):
            self.app.persistence.source_cleanup.remove("retired-calendar")
        with database(self.app).read() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM sources WHERE source_key='retired-calendar'").fetchone())

    def test_cleanup_refuses_shared_event_primary_owned_by_source(self):
        with database(self.app).write() as tx:
            tx.execute("UPDATE event_candidates SET role='primary' WHERE candidate_id=(SELECT candidate_id FROM candidates WHERE candidate_key='retired:shared')")
            tx.execute("UPDATE event_candidates SET role='duplicate' WHERE candidate_id=(SELECT candidate_id FROM candidates WHERE candidate_key='other:shared')")
        with self.assertRaisesRegex(SourceCleanupError, "primarily owned"):
            self.app.persistence.source_cleanup.remove("retired-calendar")
        with database(self.app).read() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM events WHERE event_key='event-shared'").fetchone())

    def test_cleanup_preserves_page_with_unattributed_fetch_or_page_history(self):
        with database(self.app).write() as tx:
            page_id, url_key = tx.execute("SELECT page_id,url_key FROM pages WHERE url='https://example.com/0'").fetchone()
            tx.execute("INSERT INTO fetches(page_id,job_id,fetched_at,outcome) VALUES(?,NULL,1,'network')", (page_id,))
            tx.execute("INSERT INTO changes(entity,entity_key,op,kind,payload_hash,created_at) VALUES('page',?,'upsert','page.changed',?,1)",
                       (url_key, "c" * 64))
        plan = self.app.persistence.source_cleanup.preview("retired-calendar")
        self.assertEqual(plan["counts"]["pages_exclusive"], 0)
        self.assertEqual(plan["counts"]["pages_shared_or_ambiguous"], 2)
        self.app.persistence.source_cleanup.remove("retired-calendar")
        with database(self.app).read() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM pages WHERE url='https://example.com/0'").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_pages WHERE source_id=(SELECT source_id FROM sources WHERE source_key='other-calendar')").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fetches WHERE page_id=?", (page_id,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM changes WHERE entity='page' AND entity_key=?", (url_key,)).fetchone()[0], 1)

    def test_verified_backup_is_created_outside_application_and_never_overwritten(self):
        backup_dir = os.path.join(self.temp.name, "backups")
        path = create_verified_database_backup(self.db_path, backup_dir, self.app_root)
        self.assertTrue(os.path.isfile(path))
        db = sqlite3.connect(path)
        try:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            db.close()
        with self.assertRaisesRegex(ValueError, "outside"):
            create_verified_database_backup(self.db_path, os.path.join(self.app_root, "backups"), self.app_root)


if __name__ == "__main__":
    unittest.main()
