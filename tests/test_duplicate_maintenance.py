"""High-confidence duplicate discovery and atomic event reconciliation."""
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from area_events import app as app_module
from area_events.contracts.models import CleanEvent
from area_events.deduplication import Incoming
from area_events.deduplication.exact import ExactMatch
from area_events.persistence.events import DuplicateMaintenanceError, EventRepository, _Lookup, _maintenance_connection
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

    def _dump(self, path=None):
        """Every row of every table reconciliation touches: what 'unchanged' means."""
        with closing(sqlite3.connect(path or self.db_path)) as db:
            return {table: sorted(map(tuple, db.execute(f"SELECT * FROM {table}")))
                    for table in ("events", "candidates", "event_candidates", "extractions", "event_revisions", "changes")}

    def _event(self, event_key, title, start, venue, address, order, source_key="sandiego_gov", url=None):
        """One fully owned event (candidate, extraction, primary link, revision 1), as processing creates it."""
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            source = db.execute("SELECT source_id FROM sources WHERE source_key=?", (source_key,)).fetchone()[0]
            digest = hashlib.sha256(event_key.encode()).hexdigest()
            candidate = db.execute("""INSERT INTO candidates(candidate_key,source_id,method,extractor,title,starts_at,payload_json,payload_hash,first_seen_at,last_seen_at,
                updated_at) VALUES(?,?,'adapter','test',?,?,'{}',?,1,1,?) RETURNING candidate_id""", (f"{source_key}:{event_key}", source, title, start, digest, order)).fetchone()[0]
            extraction = db.execute("""INSERT INTO extractions(candidate_id,event_id,candidate_payload_hash,extractor,version,status,result_json,result_hash,created_at)
                VALUES(?,NULL,?,'test',1,'complete','{}',?,?) RETURNING extraction_id""", (candidate, digest, digest, order)).fetchone()[0]
            event_id = db.execute("""INSERT INTO events(event_key,title,starts_at,timezone,venue_name,address,location_status,source_url,status,missing_json,revision,extraction_id,
                first_extracted_at,updated_at) VALUES(?,?,?,'America/Los_Angeles',?,?,'address',?,'partial','[]',1,?,?,?) RETURNING event_id""",
                (event_key, title, start, venue, address, url, extraction, order, order)).fetchone()[0]
            db.execute("UPDATE extractions SET event_id=? WHERE extraction_id=?", (event_id, extraction))
            db.execute("INSERT INTO event_candidates(candidate_id,event_id,role,linked_at) VALUES(?,?,'primary',?)", (candidate, event_id, order))
            db.execute("INSERT INTO event_revisions(event_id,revision,extraction_id,changed_json,created_at) VALUES(?,1,?,'{}',?)", (event_id, extraction, order))
            db.commit()

    def _trio(self):
        """ev-maintenance-1, -2 and -3 made one event in three listings (-3 from another source); -2 also has a second revision and a second candidate."""
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("UPDATE events SET address='5000 Juniper Canyon Road, San Diego, CA 92119',source_url='https://mtrp.org/marketplace/' WHERE event_key='ev-maintenance-3'")
            event_id, candidate = db.execute("SELECT e.event_id, ec.candidate_id FROM events e JOIN event_candidates ec USING(event_id) WHERE e.event_key='ev-maintenance-2'").fetchone()
            extraction = db.execute("""INSERT INTO extractions(candidate_id,event_id,candidate_payload_hash,extractor,version,status,result_json,result_hash,created_at)
                VALUES(?,?,?,'test',2,'complete','{}',?,20) RETURNING extraction_id""", (candidate, event_id, "b" * 64, "b" * 64)).fetchone()[0]
            db.execute("""INSERT INTO event_revisions(event_id,revision,extraction_id,changed_json,created_at) VALUES(?,2,?,'{"status": ["partial", "complete"]}',20)""",
                       (event_id, extraction))
            db.execute("UPDATE events SET revision=2, updated_at=20 WHERE event_id=?", (event_id,))
            source = db.execute("SELECT source_id FROM sources WHERE source_key='sandiego_gov'").fetchone()[0]
            other = db.execute("""INSERT INTO candidates(candidate_key,source_id,method,extractor,title,starts_at,payload_json,payload_hash,first_seen_at,last_seen_at,updated_at)
                VALUES('mission_trails:repeat-14',?,'adapter','test','Holiday Marketplace',1,'{}',?,1,1,21) RETURNING candidate_id""", (source, "r" * 64)).fetchone()[0]
            db.execute("INSERT INTO event_candidates(candidate_id,event_id,role,linked_at) VALUES(?,?,'duplicate',21)", (other, event_id))
            db.commit()
        return ("ev-maintenance-1", "ev-maintenance-2", "ev-maintenance-3")

    def test_events_that_state_no_place_are_scanned_by_title_and_start(self):
        """The Bates listing duplicates (a clean title and a "Date: Sat." variant, no place stated) must be found; unrelated events at the same start must not."""
        def row(event_id, title, start, **more):
            base = {"event_id": event_id, "event_key": f"ev-{event_id}", "title": title, "starts_at": start, "ends_at": None, "venue_name": None, "address": None, "location_text": None,
                    "revision": 1, "source_url": "https://batesnutfarm.biz/pages/events", "description": None, "timezone": None}
            return {**base, **more}
        start = epoch("2026-11-07T09:00:00-08:00")
        rows = [row(1, "Christmas on the Farm Arts & Crafts Fair", start), row(2, "East County Cruisers Car Show and Toy Drive", start),
                row(3, "Christmas on the Farm Arts & Crafts Fair Date: Sat.", start), row(4, "East County Cruisers Car Show and Toy Drive Date: Sat.", start),
                row(5, "Christmas on the Farm Arts & Crafts Fair", start + 86400), row(6, "Christmas on the Farm Arts & Crafts Fair Date: Sat.", start + 86400),
                row(7, "Lone event", start), row(8, "Harbor Concert", start, venue_name="Harbor Hall"), row(9, "Harbor Concert", start),
                row(10, "Blind Date", start), row(11, "Blind", start), row(12, "East County Cruisers Car Show and Toy Drive Date", start)]
        groups = self.maintenance._eligible_pairs(rows)
        self.assertEqual([group["event_ids"] for group in groups], [(1, 3), (2, 4), (5, 6)], "a bare trailing 'Date' (10, 11, 12) is never paired")
        self.assertTrue(all(group["eligible"] for group in groups))

    def test_read_only_scan_finds_only_the_high_confidence_pair(self):
        before = os.stat(self.db_path).st_mtime_ns
        groups = self.maintenance.scan()
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group["eligible"] for group in groups))
        self.assertEqual([group["event_keys"] for group in groups], [("ev-maintenance-1", "ev-maintenance-2"), ("ev-maintenance-4", "ev-maintenance-5")])
        self.assertEqual(os.stat(self.db_path).st_mtime_ns, before, "scan uses a URI read-only SQLite connection")

    def test_read_only_scan_finds_the_same_venue_and_instant_pair_whatever_the_titles(self):
        """The College Takeover pair as stored in production (two Google listings of one Facebook event, 2026-11-06 20:00 at Melómano): the scan pairs them, the
        oldest first, and pairs nothing else at that instant under a related title (another suite of the building, a venue named without an address)."""
        start = epoch("2026-11-06T20:00:00-08:00")
        venue, address = "Melómano Entertainment - Salsa & Bachata Lessons", "4255 Ruffin Rd #200, San Diego, CA 92123"
        url = ("https://www.facebook.com/events/4255-ruffin-road-suite-200-san-diego-ca-united-states-california-92123/"
               "the-college-takeover-night-fall-edition-mel%C3%B3mano-x-ucsd-x-sdsu-116/965303699984707/")
        rows = (("ev-970a148e6ca1ed277477", "The College Takeover Night: Fall Edition", venue, address),
                ("ev-00abe53e2317ec8fe838", "The College Takeover Night: Fall Edition (Melómano x UCSD x SDSU) 11/6", venue, address),
                ("ev-takeover-other-suite", "The College Takeover Night", venue, "4255 Ruffin Rd Suite 300, San Diego, CA 92123"),
                ("ev-takeover-no-address", "The College Takeover Night", venue, None))
        for order, (key, title, place, street) in enumerate(rows, 10):
            self._event(key, title, start, place, street, order, url=url)
        before = os.stat(self.db_path).st_mtime_ns
        groups = [group for group in self.maintenance.scan() if group["events"][0]["starts_at"] == start]
        self.assertEqual([(group["event_keys"], group["eligible"]) for group in groups], [(("ev-970a148e6ca1ed277477", "ev-00abe53e2317ec8fe838"), True)])
        self.assertEqual(os.stat(self.db_path).st_mtime_ns, before, "the scan only reads")

    def test_normal_deduplication_and_maintenance_keep_the_same_first_created_event(self):
        """Three listings of one event at one venue and instant, created in this order: the first has the last key and the least similar title. A later
        candidate links to it; the scan pairs the first two (the pair the strategy's matcher accepts) and the cleanup keeps it as the survivor."""
        start = epoch("2026-11-06T20:00:00-08:00")
        venue, address = "Melómano Entertainment - Salsa & Bachata Lessons", "4255 Ruffin Rd #200, San Diego, CA 92123"
        later = "The College Takeover Night: Fall Edition (Melómano x UCSD x SDSU) 11/6"
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            source = db.execute("SELECT source_id FROM sources WHERE source_key='sandiego_gov'").fetchone()[0]
            for index, (event_key, title) in enumerate((("ev-zz-first-created", "The College Takeover Night"), ("ev-aa-second-created", later)), 10):
                candidate = db.execute("""INSERT INTO candidates(candidate_key,source_id,method,extractor,title,starts_at,payload_json,payload_hash,first_seen_at,last_seen_at,
                    updated_at) VALUES(?,?,'adapter','test',?,?,'{}',?,1,1,1) RETURNING candidate_id""", (f"takeover:{index}", source, title, start, f"{index:064}")).fetchone()[0]
                extraction = db.execute("""INSERT INTO extractions(candidate_id,event_id,candidate_payload_hash,extractor,version,status,result_json,result_hash,created_at)
                    VALUES(?,NULL,?,'test',1,'complete','{}',?,?) RETURNING extraction_id""", (candidate, f"{index:064}", f"{index:064}", index)).fetchone()[0]
                event_id = db.execute("""INSERT INTO events(event_key,title,starts_at,timezone,venue_name,address,location_status,status,missing_json,revision,extraction_id,
                    first_extracted_at,updated_at) VALUES(?,?,?,'America/Los_Angeles',?,?,'address','partial','[]',1,?,?,?) RETURNING event_id""",
                    (event_key, title, start, venue, address, extraction, index, index)).fetchone()[0]
                db.execute("UPDATE extractions SET event_id=? WHERE extraction_id=?", (event_id, extraction))
                db.execute("INSERT INTO event_candidates(candidate_id,event_id,role,linked_at) VALUES(?,?,'primary',?)", (candidate, event_id, index))
                db.execute("INSERT INTO event_revisions(event_id,revision,extraction_id,changed_json,created_at) VALUES(?,1,?,'{}',?)", (event_id, extraction, index))
            db.commit()
        incoming = CleanEvent(later, start, None, "America/Los_Angeles", None, venue, None, address, None, "address", (), "partial")
        connection = _maintenance_connection(self.db_path, "ro")
        try:
            decision = ExactMatch().decide(Incoming("takeover:new", incoming, None), _Lookup(connection, EventRepository()))
        finally:
            connection.close()
        self.assertEqual((decision.action, decision.event_key), ("link", "ev-zz-first-created"), "the first created, not the identical title nor the first key")
        [group] = [group for group in self.maintenance.scan() if group["events"][0]["starts_at"] == start]
        self.assertEqual((group["event_keys"], group["eligible"]), (("ev-zz-first-created", "ev-aa-second-created"), True))
        [result] = self.maintenance.clean([group["event_keys"]])
        self.assertEqual((result["survivor"], result["removed"]), ("ev-zz-first-created", ("ev-aa-second-created",)))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT title FROM events WHERE event_key='ev-zz-first-created'").fetchone()[0], "The College Takeover Night",
                             "the first-created event keeps its own primary description; the later listing's title does not overwrite it")

    def test_clean_preserves_candidates_sources_links_and_revision_history(self):
        before = self._counts()
        keys = (("ev-maintenance-1", "ev-maintenance-2"), ("ev-maintenance-4", "ev-maintenance-5"))
        result = self.maintenance.clean(keys, limit=2)
        self.assertEqual([(row["survivor"], row["removed"]) for row in result],
                         [("ev-maintenance-1", ("ev-maintenance-2",)), ("ev-maintenance-4", ("ev-maintenance-5",))])
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
            # The first-created event keeps its own facts (the pair reconciler used to copy the most recently updated event's facts over them: here the later title).
            self.assertEqual(db.execute("SELECT title, extraction_id FROM events WHERE event_id=1").fetchone(), ("Holiday Marketplace at Mission Trails Regional Park", 1))
            self.assertEqual(db.execute("SELECT revision, changed_json FROM event_revisions WHERE event_id=1 ORDER BY revision").fetchall()[:2], [(1, "{}"), (2, "{}")],
                             "the survivor's own revision 1 keeps its number; the removed event's revision 1 follows it")
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

    def test_a_three_event_group_is_reported_whole_and_cannot_be_cleaned_as_a_pair(self):
        trio = self._trio()
        [group] = [group for group in self.maintenance.scan() if len(group["event_keys"]) == 3]
        self.assertEqual((group["event_keys"], group["eligible"], group["reason"], group["candidate_associations"]), (trio, True, "", 4))
        before = self._dump()
        for pair in ((trio[0], trio[1]), (trio[1], trio[2]), (trio[0], trio[2])):
            with self.subTest(pair=pair), self.assertRaisesRegex(DuplicateMaintenanceError, "no longer an unambiguous duplicate"):
                self.maintenance.clean((pair,))
        self.assertEqual(self._dump(), before)

    def test_three_event_cleanup_keeps_the_first_created_event_and_every_association_extraction_and_revision(self):
        trio = self._trio()
        before, counts = self._dump(), self._counts()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            original = {row["event_key"]: dict(row) for row in db.execute("SELECT * FROM events")}
            history = {row["event_key"]: [tuple(r) for r in db.execute("SELECT extraction_id, changed_json, created_at FROM event_revisions WHERE event_id=? ORDER BY revision",
                                                                         (row["event_id"],))] for row in db.execute("SELECT * FROM events")}
        [result] = self.maintenance.clean([trio[::-1]])   # the order a group is named in does not choose the survivor
        self.assertEqual(result, {"survivor": trio[0], "removed": trio[1:], "candidates_preserved": 4, "revisions_preserved": 4, "revision": 6})
        after = self._counts()
        self.assertEqual({k: after[k] for k in ("sources", "candidates", "event_candidates", "extractions")},
                         {k: counts[k] for k in ("sources", "candidates", "event_candidates", "extractions")}, "no candidate, link or extraction is lost")
        self.assertEqual((after["events"], after["event_revisions"]), (counts["events"] - 2, counts["event_revisions"] + 2), "one reconciliation revision per removed event")
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            survivor = dict(db.execute("SELECT * FROM events WHERE event_key=?", (trio[0],)).fetchone())
            for field in ("event_id", "event_key", "title", "description", "starts_at", "ends_at", "timezone", "venue_name", "location_text", "address", "location_status",
                          "source_url", "status", "missing_json", "extraction_id", "first_extracted_at"):
                self.assertEqual(survivor[field], original[trio[0]][field], f"the first-created event keeps its {field}")
            self.assertEqual(survivor["revision"], 6)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE event_key IN (?,?)", trio[1:]).fetchone()[0], 0)
            links = [tuple(r) for r in db.execute("""SELECT c.candidate_key, ec.role FROM event_candidates ec JOIN candidates c USING(candidate_id)
                                                      WHERE ec.event_id=? ORDER BY c.candidate_key""", (survivor["event_id"],))]
            self.assertEqual(links, [("mission_trails:new-14", "duplicate"), ("mission_trails:old-14", "primary"), ("mission_trails:repeat-14", "duplicate"),
                                     ("other:similar", "duplicate")], "the survivor's primary stays primary; every other candidate is its duplicate")
            self.assertEqual(db.execute("""SELECT COUNT(*) FROM events e JOIN event_candidates ec ON ec.event_id=e.event_id AND ec.role='primary'
                                           JOIN extractions x ON x.extraction_id=e.extraction_id AND x.candidate_id=ec.candidate_id WHERE e.event_id=?""",
                                        (survivor["event_id"],)).fetchone()[0], 1, "the event's facts still come from its primary candidate (its verification is unchanged)")
            self.assertEqual({r[0] for r in db.execute("""SELECT s.source_key FROM event_candidates ec JOIN candidates c USING(candidate_id) JOIN sources s USING(source_id)
                                                          WHERE ec.event_id=?""", (survivor["event_id"],))}, {"sandiego_gov", "ut_austin"}, "both sources stay attributed")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM extractions WHERE event_id=?", (survivor["event_id"],)).fetchone()[0], 4)
            revisions = db.execute("SELECT revision, extraction_id, changed_json, created_at FROM event_revisions WHERE event_id=? ORDER BY revision",
                                   (survivor["event_id"],)).fetchall()
            self.assertEqual([r["revision"] for r in revisions], [1, 2, 3, 4, 5, 6])
            self.assertEqual([tuple(r)[1:] for r in revisions[:1]], history[trio[0]], "the survivor's own revisions keep their numbers")
            self.assertEqual([tuple(r)[1:] for r in revisions[1:3]], history[trio[1]], "then the second event's revisions, unchanged")
            self.assertEqual([tuple(r)[1:] for r in revisions[4:5]], history[trio[2]], "then the third event's")
            for row, removed, appended, candidates in ((revisions[3], trio[1], [2, 3], ["mission_trails:new-14", "mission_trails:repeat-14"]),
                                                       (revisions[5], trio[2], [5], ["other:similar"])):
                reconciled = json.loads(row["changed_json"])
                self.assertEqual(set(reconciled), {"reconciled_from"}, "no fact is changed by the reconciliation")
                self.assertEqual(reconciled["reconciled_from"], {"event_key": removed, "event_id": original[removed]["event_id"], "revision": original[removed]["revision"],
                                                                 "title": original[removed]["title"], "appended_revisions": appended, "candidates": candidates})
                self.assertEqual(row["extraction_id"], survivor["extraction_id"])
            changes = [tuple(r) for r in db.execute("SELECT op, entity_key FROM changes WHERE kind='event.reconciled' ORDER BY seq")]
            self.assertEqual(changes, [("upsert", trio[0]), ("delete", trio[1]), ("upsert", trio[0]), ("delete", trio[2])])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_candidates WHERE event_id NOT IN (SELECT event_id FROM events)").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM extractions WHERE event_id IS NOT NULL AND event_id NOT IN (SELECT event_id FROM events)").fetchone()[0], 0)
        unrelated = [row for row in before["events"] if row[1] not in trio]
        self.assertEqual([row for row in self._dump()["events"] if row[1] not in trio], unrelated, "events outside the group are untouched")
        self.assertFalse(any(set(group["event_keys"]) & set(trio) for group in self.maintenance.scan()), "nothing of the group is a duplicate any more")
        once = self._dump()
        for repeat in (trio, trio[:2]):
            with self.subTest(repeat=repeat), self.assertRaisesRegex(DuplicateMaintenanceError, "no longer an unambiguous duplicate"):
                self.maintenance.clean([repeat])
        self.assertEqual(self._dump(), once, "a repeated request is refused and changes nothing")

    def test_a_chain_of_matches_or_a_larger_component_is_never_eligible(self):
        class Chain(ExactMatch):
            """A matches B and B matches C, but A does not match C."""
            def matches(self, left, right):
                return {left.event_key, right.event_key} != {"ev-maintenance-1", "ev-maintenance-3"} and super().matches(left, right)

        trio = self._trio()
        chained = app_module.duplicate_maintenance(self.db_path, Chain(), clock=lambda: epoch("2026-10-01T00:00:00+00:00"))
        [group] = [group for group in chained.scan() if trio[0] in group["event_keys"]]
        self.assertEqual((group["event_keys"], group["eligible"]), (trio, False))
        self.assertIn("chain of matches", group["reason"])
        before = self._dump()
        with self.assertRaisesRegex(DuplicateMaintenanceError, "not eligible .*chain of matches"):
            chained.clean([trio])
        for pair in ((trio[0], trio[1]), (trio[1], trio[2])):
            with self.subTest(pair=pair), self.assertRaisesRegex(DuplicateMaintenanceError, "no longer an unambiguous duplicate"):
                chained.clean([pair])
        self.assertEqual(self._dump(), before)

        class Asymmetric(ExactMatch):
            def matches(self, left, right):
                return left.event_key != "ev-maintenance-3" and super().matches(left, right)

        [group] = [group for group in app_module.duplicate_maintenance(self.db_path, Asymmetric(), clock=lambda: epoch("2026-10-01T00:00:00+00:00")).scan()
                   if trio[0] in group["event_keys"]]
        self.assertFalse(group["eligible"], "a match the matcher gives only one way round is no match")
        rows = [{"event_id": n, "event_key": f"ev-four-{n}", "title": "Harbor Concert", "starts_at": 1, "ends_at": None, "venue_name": "Harbor Hall",
                 "address": "500 Harbor Drive, San Diego, CA", "location_text": None, "revision": 1, "source_url": None, "description": None, "timezone": None}
                for n in range(1, 5)]
        [four] = self.maintenance._eligible_pairs(rows)
        self.assertEqual((len(four["event_ids"]), four["eligible"], four["reason"]), (4, False, "ambiguous match component has more than 3 events"))

    def test_a_group_naming_a_key_twice_or_sharing_a_key_with_another_group_is_refused(self):
        trio = self._trio()
        before = self._dump()
        for groups in ([trio + (trio[0],)], [(trio[0], trio[0])], [trio, trio[:2]], [trio + ("ev-maintenance-4",)]):
            with self.subTest(groups=groups), self.assertRaises(DuplicateMaintenanceError):
                self.maintenance.clean(groups, limit=2)
        self.assertEqual(self._dump(), before)

    def test_missing_or_inconsistent_ownership_in_a_three_event_group_is_refused(self):
        mutations = (("UPDATE event_candidates SET role='duplicate' WHERE event_id=3", "exactly one primary candidate"),
                     ("UPDATE event_candidates SET role='primary' WHERE candidate_id=(SELECT candidate_id FROM candidates WHERE candidate_key='mission_trails:repeat-14')",
                      "exactly one primary candidate"),
                     ("DELETE FROM event_candidates WHERE event_id=3", "candidate ownership"),
                     ("DELETE FROM event_revisions WHERE event_id=2 AND revision=1", "revision history is incomplete"))
        trio = self._trio()
        for statement, message in mutations:
            with self.subTest(statement=statement):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                copied = os.path.join(temp.name, "copy.sqlite3")
                with closing(sqlite3.connect(self.db_path)) as source, closing(sqlite3.connect(copied)) as target:
                    source.backup(target)
                with closing(sqlite3.connect(copied)) as db:
                    db.execute(statement)
                    db.commit()
                maintenance = app_module.duplicate_maintenance(copied, ExactMatch(), clock=lambda: epoch("2026-10-01T00:00:00+00:00"))
                [group] = [group for group in maintenance.scan() if trio[0] in group["event_keys"]]
                self.assertFalse(group["eligible"])
                self.assertIn(message, group["reason"])
                before = self._dump(copied)
                with self.assertRaisesRegex(DuplicateMaintenanceError, message):
                    maintenance.clean([trio])
                self.assertEqual(self._dump(copied), before)

    def test_a_change_after_the_scan_is_found_again_inside_the_transaction(self):
        trio = self._trio()
        self.assertTrue(any(group["event_keys"] == trio and group["eligible"] for group in self.maintenance.scan()))
        stale = (("UPDATE events SET title='Used Book Sale' WHERE event_key='ev-maintenance-3'", "no longer an unambiguous duplicate"),
                 ("UPDATE events SET starts_at=starts_at+3600 WHERE event_key='ev-maintenance-2'", "no longer an unambiguous duplicate"))
        for statement, message in stale:
            with self.subTest(statement=statement):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                copied = os.path.join(temp.name, "copy.sqlite3")
                with closing(sqlite3.connect(self.db_path)) as source, closing(sqlite3.connect(copied)) as target:
                    source.backup(target)
                with closing(sqlite3.connect(copied)) as db:   # what a concurrent writer did after the operator's scan
                    db.execute(statement)
                    db.commit()
                before = self._dump(copied)
                with self.assertRaisesRegex(DuplicateMaintenanceError, message):
                    app_module.duplicate_maintenance(copied, ExactMatch(), clock=lambda: epoch("2026-10-01T00:00:00+00:00")).clean([trio])
                self.assertEqual(self._dump(copied), before)
        start = epoch("2026-11-14T09:00:00-08:00")
        self._event("ev-maintenance-fourth", "Holiday Marketplace at Mission Trails Regional Park", start, "Mission Trails Regional Park",
                    "5000 Juniper Canyon Road, San Diego, CA 92119", 30, url="https://mtrp.org/marketplace/")
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE events SET ends_at=?, description='Event description' WHERE event_key='ev-maintenance-fourth'", (epoch("2026-11-14T15:00:00-08:00"),))
            db.commit()
        before = self._dump()
        with self.assertRaisesRegex(DuplicateMaintenanceError, "no longer an unambiguous duplicate"):
            self.maintenance.clean([trio])
        self.assertEqual(self._dump(), before, "a fourth listing that joined after the scan refuses the group")

    def test_a_failure_while_folding_in_the_third_event_rolls_back_the_whole_group(self):
        trio = self._trio()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("CREATE TRIGGER refuse_third_delete BEFORE DELETE ON events WHEN OLD.event_key='ev-maintenance-3' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
            db.commit()
        before = self._dump()
        with self.assertRaisesRegex(DuplicateMaintenanceError, "transaction failed and was rolled back"):
            self.maintenance.clean([trio, ("ev-maintenance-4", "ev-maintenance-5")], limit=2)
        self.assertEqual(self._dump(), before, "the second event, already folded in, and the other group are restored too")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_distinct_events_at_one_venue_and_instant_are_never_grouped_and_production_trios_are(self):
        def row(event_id, title, start, venue, address, timezone="America/Los_Angeles"):
            return {"event_id": event_id, "event_key": f"ev-{event_id}", "title": title, "starts_at": start, "ends_at": None, "venue_name": venue, "address": address,
                    "location_text": None, "revision": 1, "source_url": None, "description": None, "timezone": timezone}
        bates, cafe = epoch("2026-11-08T11:00:00-08:00"), epoch("2026-11-07T18:00:00-08:00")
        rodeo, fiesta = epoch("2026-11-08T18:00:00-06:00"), epoch("2026-11-08T12:00:00-08:00")
        library, park = ("Perry-Castañeda Library (PCL)", "101 21ST ST E, Austin, Texas 78705"), "Embarcadero Marina Park North"
        rows = [row(1, "8th Annual Guns & Hoses Chili Cook-Off Fundraiser", bates, "Bates Nut Farm", "15954 Woods Valley Road, Valley Center, CA"),
                row(2, "Bayou Brothers | Live Music", bates, "Bates Nut Farm", "15954 Woods Valley Road, Valley Center, CA"),
                row(3, "San Diego Singles Event: Speed Dating", cafe, "Coffee Shop", "3764 30th St, San Diego, CA 92104"),
                row(4, "San Diego Singles Event: Speed Dating (Ages 22-30) at Coffee Shop", cafe, "Coffee Shop", "3764 30th St, San Diego, CA 92104"),
                row(5, "Writing & Research Rodeo", rodeo, *library, "America/Chicago"), row(6, "Writing & Research Rodeos", rodeo, *library, "America/Chicago"),
                row(7, "Writing & Research Rodeos", rodeo, *library, "America/Chicago"),
                row(8, "San Diego Food and Wine Festival - Grand Fiesta", fiesta, park, "400 Kettner Blvd, San Diego, CA 92101"),
                row(9, "San Diego Food + Wine Festival", fiesta, park, "400 Kettner Blvd, San Diego, CA 92101"),
                row(10, "San Diego Food and Wine Festival - Grand Fiesta - Sunday", fiesta, park, "400 Kettner Blvd, San Diego, CA")]
        groups = self.maintenance._eligible_pairs(rows)
        self.assertEqual([(group["event_ids"], group["eligible"]) for group in groups], [((5, 6, 7), True), ((8, 9, 10), True)],
                         "Bates Nut Farm's two events and the Speed Dating pair stay apart")

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
