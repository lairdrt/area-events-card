"""The trashcan: an administrator deletes an event for good, in one transaction, through the control API; rediscovery never brings it back (and nothing else is lost)."""
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from area_events import app as app_module
from area_events.api.http_api import ApiTokens, DiscoveryApi
from area_events.control.service import CreateAreaRequest, CreateSourceRequest
from area_events.trashcan import normalize, one_edit_apart, suppresses
from tests.test_discovery_service import Clock
from tests.test_sources_producer import FakeClient

SEED = "https://example.org/events"
OTHER_LISTING = "https://example.org/calendar"
CONTROL, READ = "c" * 40, "r" * 40


def listing(*cards):
    return "<main><h1>Upcoming Events</h1>" + "".join(cards) + "</main>"


def card(title, date="Saturday, November 14, 2026", time="10 am to 2 pm", place="Town Hall"):
    return f"<article><h2>{title}</h2><p>{date}</p><p>{time}</p><p>Location: {place}</p></article>"


FAIR, LECTURE = card("Harvest Craft Fair"), card("Moonlight Lecture", "Friday, November 20, 2026", "6:30pm – 8pm", "Science Museum")


class Pipeline(unittest.TestCase):
    """A website source read by the real producer, stored, extracted into events; deletion and the next scans through the same objects as production."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = os.path.join(self.temp.name, "discovery.db")
        self.clock = Clock()
        self.client = FakeClient({SEED: listing(FAIR, LECTURE), OTHER_LISTING: listing()})
        self.open()
        self.app.register_known_sources()
        self.app.control.create_source(CreateSourceRequest("fairs", "sources_producer", "Local Fairs", True, 60,
                                                           {"urls": [SEED, OTHER_LISTING], "max_depth": 0, "timezone": "America/Los_Angeles"}))
        self.app.control.create_area(CreateAreaRequest("Example City", 32.71, -117.16, 20, sources=("fairs",)))
        self.app.scheduler.plan()
        self.scan()

    def open(self):
        self.app = app_module.build(self.db, clock=self.clock, fetch_client=self.client, request_interval=0)
        self.api = DiscoveryApi(self.app.control, self.app.reader, ApiTokens(CONTROL, READ), self.clock, events=self.app.events, trashcan=self.app.persistence.trashcan)
        self.addCleanup(self.app.close)

    def restart(self):
        self.app.close()
        self.open()

    def scan(self):
        self.clock.t += 7200
        self.app.service.make_due("fairs")
        reports = [r for r in self.app.service.run_until_idle(3) if r.outcome == "completed"]
        while self.app.extraction.run_once().claimed:
            pass
        return reports[-1]

    def call(self, method, path, body=None, token=CONTROL):
        raw = json.dumps(body).encode() if body is not None else b""
        return self.api.handle(method, path, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, raw, "127.0.0.1")

    def rows(self, sql, *args):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute(sql, args).fetchall()

    def events(self):
        return {row[1]: (row[0], row[2]) for row in self.rows("SELECT event_key, title, revision FROM events")}

    def key(self, title):
        return self.events()[title][0]

    def delete(self, title, reason="duplicate"):
        status, body = self.call("POST", f"/v1/events/{self.key(title)}/trash", {"reason": reason})
        self.assertEqual((status, body["state"]), (200, "deleted"), body)
        return body

    def assertIntact(self):
        self.assertEqual(self.rows("PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM candidates c LEFT JOIN event_candidates l USING (candidate_id) WHERE l.event_id IS NULL"), [(0,)], "every candidate has its event")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM candidate_media m JOIN candidates c USING (candidate_id) WHERE m.payload_hash <> c.payload_hash AND m.complete = 1"), [(0,)])

    # --- deletion -----------------------------------------------------------------------------------------------------

    def test_the_reason_and_record_are_kept_and_the_event_leaves_the_api_with_everything_only_it_owned(self):
        fair_key, lecture = self.key("Harvest Craft Fair"), self.events()["Moonlight Lecture"]
        candidate = self.rows("SELECT c.candidate_id, c.candidate_key FROM candidates c JOIN event_candidates l USING (candidate_id) JOIN events e USING (event_id) WHERE e.event_key=?", fair_key)[0]
        pages_before = self.rows("SELECT COUNT(*) FROM pages")[0][0]
        body = self.delete("Harvest Craft Fair", "not_relevant")
        self.assertEqual((body["schema"], body["event_key"], body["reason"]), ("discovery.trashcan.v1", fair_key, "not_relevant"))
        self.assertEqual(self.call("GET", f"/v1/events/{fair_key}", token=READ)[0], 404)
        _, page = self.call("GET", "/v1/events", token=READ)
        self.assertEqual([e["title"] for e in page["events"]], ["Moonlight Lecture"])
        self.assertEqual(self.events()["Moonlight Lecture"], lecture, "the other event is untouched")
        kept = self.rows("SELECT reason, record_json, match_json, suppressed_count FROM trashcan WHERE event_key=?", fair_key)[0]
        record, facts = json.loads(kept[1]), json.loads(kept[2])
        self.assertEqual((kept[0], kept[3]), ("not_relevant", 0))
        self.assertEqual((record["event"]["title"], record["event"]["venue_name"]), ("Harvest Craft Fair", "Town Hall"))
        self.assertEqual([c["candidate_key"] for c in record["candidates"]], [candidate[1]])
        self.assertTrue(record["revisions"])
        self.assertNotIn("media", json.dumps(record["event"]))
        self.assertEqual((facts["sources"], facts["titles"], facts["places"]), (["fairs"], ["harvest craft fair"], ["town hall"]))
        for table in ("candidates", "evidence", "extractions", "processing_work", "candidate_media", "event_candidates"):
            self.assertEqual(self.rows(f"SELECT COUNT(*) FROM {table} WHERE candidate_id=?", candidate[0]), [(0,)], table)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM pages")[0][0], pages_before, "pages are shared and stay")
        self.assertEqual(self.rows("SELECT op, kind FROM changes WHERE entity='event' AND entity_key=? ORDER BY seq DESC LIMIT 1", fair_key), [("delete", "event.removed")])
        self.assertEqual(self.rows("SELECT op FROM changes WHERE entity='candidate' AND entity_key=? ORDER BY seq DESC LIMIT 1", candidate[1]), [("delete",)])
        self.assertIntact()

    def test_deleting_twice_changes_nothing_and_bad_requests_are_refused_without_writes(self):
        fair_key = self.key("Harvest Craft Fair")
        self.delete("Harvest Craft Fair")
        changes = self.rows("SELECT COUNT(*) FROM changes")[0][0]
        status, body = self.call("POST", f"/v1/events/{fair_key}/trash", {"reason": "other"})
        self.assertEqual((status, body["state"], body["reason"]), (200, "already_deleted", "duplicate"), "the first deletion's reason stands")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM changes")[0][0], changes)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM trashcan"), [(1,)])
        lecture = self.key("Moonlight Lecture")
        self.assertEqual(self.call("POST", f"/v1/events/{lecture}/trash", {"reason": "spam"})[0], 400)
        self.assertEqual(self.call("POST", f"/v1/events/{lecture}/trash", {})[0], 400)
        self.assertEqual(self.call("POST", "/v1/events/ev-doesnotexist/trash", {"reason": "other"})[0], 404)
        self.assertEqual(self.call("GET", f"/v1/events/{lecture}/trash")[0], 405)
        self.assertIn("Moonlight Lecture", self.events())
        self.assertIntact()

    def test_only_a_control_token_may_delete(self):
        lecture = self.key("Moonlight Lecture")
        self.assertEqual(self.call("POST", f"/v1/events/{lecture}/trash", {"reason": "other"}, token=READ)[0], 403)
        self.assertEqual(self.call("POST", f"/v1/events/{lecture}/trash", {"reason": "other"}, token="x" * 40)[0], 401)
        self.assertIn("Moonlight Lecture", self.events())
        self.assertEqual(self.rows("SELECT COUNT(*) FROM trashcan"), [(0,)])

    def test_an_event_whose_candidates_another_event_history_refers_to_is_refused_whole(self):
        fair, lecture = self.key("Harvest Craft Fair"), self.key("Moonlight Lecture")
        with closing(sqlite3.connect(self.db)) as db:
            extraction = db.execute("SELECT x.extraction_id FROM extractions x JOIN event_candidates l USING (candidate_id) JOIN events e ON e.event_id = l.event_id WHERE e.event_key=?",
                                    (fair,)).fetchone()[0]
            db.execute("INSERT INTO event_revisions(event_id, revision, extraction_id, changed_json, created_at) SELECT event_id, revision + 1, ?, '{}', 1 FROM events WHERE event_key=?",
                       (extraction, lecture))
            db.commit()
        before = self.rows("SELECT COUNT(*) FROM candidates")
        status, body = self.call("POST", f"/v1/events/{fair}/trash", {"reason": "other"})
        self.assertEqual((status, body["error"]["code"]), (409, "shared_history"))
        self.assertIn("Harvest Craft Fair", self.events())
        self.assertEqual((self.rows("SELECT COUNT(*) FROM candidates"), self.rows("SELECT COUNT(*) FROM trashcan")), (before, [(0,)]), "nothing was written")

    # --- rediscovery --------------------------------------------------------------------------------------------------

    def assertNothingStored(self, title):
        self.assertNotIn(title, self.events())
        self.assertEqual(self.rows("SELECT COUNT(*) FROM candidates WHERE title=?", title), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM processing_work w JOIN candidates c USING (candidate_id) WHERE c.title=?", title), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM candidate_media m JOIN candidates c USING (candidate_id) WHERE c.title=?", title), [(0,)])

    def test_the_same_event_found_again_is_suppressed_across_a_restart_and_repeated_scans(self):
        lecture = self.events()["Moonlight Lecture"]
        self.delete("Harvest Craft Fair")
        self.restart()
        report = self.scan()
        self.assertEqual(report.candidates_suppressed, 1)
        self.assertNothingStored("Harvest Craft Fair")
        self.assertEqual(self.scan().candidates_suppressed, 1)
        self.assertNothingStored("Harvest Craft Fair")
        self.assertEqual(self.rows("SELECT suppressed_count FROM trashcan"), [(2,)])
        self.assertEqual(self.events()["Moonlight Lecture"], lecture, "the other event is rediscovered unchanged")
        _, listing_body = self.call("GET", "/v1/trashcan", token=READ)
        self.assertEqual((listing_body["schema"], listing_body["total"], listing_body["suppressed"], listing_body["by_reason"]["duplicate"]),
                         ("discovery.trashcan.v1", 1, 2, 1))
        self.assertEqual(listing_body["events"][0]["sources"], [{"key": "fairs", "name": "Local Fairs"}])
        self.assertIntact()

    def test_a_one_letter_title_change_at_the_same_time_and_place_is_suppressed(self):
        self.delete("Harvest Craft Fair")
        self.client.pages[SEED] = listing(card("Harvest Kraft Fair"), LECTURE)
        self.assertEqual(self.scan().candidates_suppressed, 1)
        self.assertNothingStored("Harvest Kraft Fair")
        self.assertIntact()

    def test_the_same_event_on_another_listing_page_of_the_source_is_suppressed(self):
        self.delete("Harvest Craft Fair")
        self.client.pages[SEED], self.client.pages[OTHER_LISTING] = listing(LECTURE), listing(FAIR)
        self.assertEqual(self.scan().candidates_suppressed, 1)
        self.assertNothingStored("Harvest Craft Fair")
        self.assertIntact()

    def test_different_events_and_other_occurrences_are_never_suppressed(self):
        self.delete("Harvest Craft Fair")
        others = {"next year": card("Harvest Craft Fair", "Saturday, November 13, 2027"), "same time and place": card("Town Hall Quilt Show"),
                  "a letter off, elsewhere": card("Harvest Kraft Fair", place="Library Annex"), "same day, later": card("Harvest Craft Fair", time="4 pm to 6 pm")}
        self.client.pages[SEED] = listing(LECTURE, *others.values())
        report = self.scan()
        self.assertEqual(report.candidates_suppressed, 0)
        titles = sorted(t for t in self.events())
        self.assertEqual(titles, ["Harvest Craft Fair", "Harvest Kraft Fair", "Moonlight Lecture", "Town Hall Quilt Show"])
        self.assertEqual(len(self.rows("SELECT 1 FROM events WHERE title='Harvest Craft Fair'")), 2, "next year's and the later one are their own events")
        self.assertIntact()


class Matcher(unittest.TestCase):
    ENTRY = {"sources": ["fairs"], "candidate_keys": ["fairs:abc"], "starts": [1000000], "titles": ["harvest craft fair"], "detail_urls": ["https://example.org/e/fair"],
             "places": ["town hall"]}

    def incoming(self, **values):
        return {"source_key": "fairs", "candidate_key": "fairs:new", "starts_at": 1000000, "title": "Harvest Craft Fair", "detail_url": None, "venue": None, "address": None, **values}

    def test_normalizing_sets_aside_case_punctuation_ampersands_accents_and_spacing(self):
        self.assertEqual(normalize("  Harvest  CRAFT-Fair! "), "harvest craft fair")
        self.assertEqual(normalize("Arts & Crafts"), normalize("arts and crafts"))
        self.assertEqual(normalize("Café Noël"), "cafe noel")
        self.assertTrue(one_edit_apart("harvest craft fair", "harvest kraft fair") and one_edit_apart("harvest craft fair", "harvest craft fairs"))
        self.assertFalse(one_edit_apart("harvest craft fair", "harvest kraft fare"))

    def test_the_rules(self):
        self.assertTrue(suppresses(self.ENTRY, self.incoming()))
        self.assertTrue(suppresses(self.ENTRY, self.incoming(title="HARVEST craft fair!")))
        self.assertTrue(suppresses(self.ENTRY, self.incoming(candidate_key="fairs:abc", starts_at=1000000 + 3600, title="Renamed")), "the source's own identity, corrected time")
        self.assertTrue(suppresses(self.ENTRY, self.incoming(title="Harvest Kraft Fair", venue="Town  Hall")))
        self.assertTrue(suppresses(self.ENTRY, self.incoming(title="Harvest Kraft Fair", detail_url="https://example.org/e/fair")))
        self.assertIsNone(suppresses(self.ENTRY, self.incoming(title="Harvest Kraft Fair")), "a changed letter needs one more fact")
        self.assertIsNone(suppresses(self.ENTRY, self.incoming(source_key="other")))
        self.assertIsNone(suppresses(self.ENTRY, self.incoming(starts_at=1000000 + 3600)))
        self.assertIsNone(suppresses(self.ENTRY, self.incoming(candidate_key="fairs:abc", starts_at=1000000 + 365 * 86400)), "another year is another occurrence")
        self.assertIsNone(suppresses(self.ENTRY, self.incoming(title="Town Hall Quilt Show", venue="Town Hall")))
        short = {**self.ENTRY, "titles": ["fun run"]}
        self.assertIsNone(suppresses(short, self.incoming(title="Fun Bun", venue="Town Hall")), "short titles never match with a letter changed")


if __name__ == "__main__":
    unittest.main()
