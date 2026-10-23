"""Milestone 2E: the first live Austin source (the University of Texas at Austin's public events calendar, a Localist API) through the EXISTING Discovery,
Extraction and Event Query services, next to San Diego, in the one database.

The regression data are a small sample of the REAL API (tests/data/localist_ut_austin_page*.json, long texts cut), re-dated at load so the events are upcoming.
The sample includes the awkward cases: an all-day entry, an entry that starts at 00:00 without being flagged all-day, an event without a venue, a venue
without an address. No website is contacted."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from discovery.core import FetchError
from discovery.safe import Response
from discovery.service import app as app_module
from discovery.service.adapters import LocalistAdapter
from discovery.service.control import CreateAreaRequest
from discovery.service.events import EventQuery
from discovery.service.service import DiscoveryService
from discovery.service.store import DiscoveryStore
from discovery.service.urls import canonical_url
from tests.test_discovery_service import Clock, SPEC, city_pages

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SITE = "https://calendar.utexas.edu"
API = f"{SITE}/api/2/events?days=14&pp=100&page="
UT = app_module.KNOWN_SOURCES["ut-austin"]
CITY = "https://www.sandiego.gov/events"


def sample_pages(days_ahead=2):
    """The two saved API pages with every date moved so the sample starts `days_ahead` days from today (the times of day and offsets are kept)."""
    pages = [json.load(open(os.path.join(DATA, f"localist_ut_austin_page{n}.json"), encoding="utf-8")) for n in (1, 2)]
    first = min(date.fromisoformat(w["event"]["first_date"]) for p in pages for w in p["events"])
    shift = timedelta(days=(date.today() + timedelta(days=days_ahead) - first).days)
    for page in pages:
        for wrapper in page["events"]:
            event = wrapper["event"]
            for field in ("first_date", "last_date"):
                if event.get(field):
                    event[field] = (date.fromisoformat(event[field]) + shift).isoformat()
            for item in event["event_instances"]:
                instance = item["event_instance"]
                for field in ("start", "end"):
                    if instance.get(field):
                        instance[field] = (datetime.fromisoformat(instance[field]) + shift).isoformat()
    return pages


class JsonClient:
    """Stands in for SafeFetcher: saved API pages (as application/json) and City pages by address; an Exception value is raised."""

    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def fetch(self, url, check_robots=True, binary=None, timeout=None):
        self.urls.append(url)
        entry = self.pages.get(url)
        if entry is None:
            raise FetchError(f"{url}: HTTP 404")
        if isinstance(entry, Exception):
            raise entry
        kind = "application/json; charset=utf-8" if "/api/" in url else "text/html; charset=utf-8"
        return Response(url, 200, kind, entry if isinstance(entry, str) else json.dumps(entry))


class Recorder:
    """What the adapter is given: get(url) -> text."""

    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def get(self, url, check_robots=True):
        self.urls.append(url)
        entry = self.pages[url]
        if isinstance(entry, Exception):
            raise entry
        return entry if isinstance(entry, str) else json.dumps(entry)


def options():
    return {**dict(UT.options), "source_key": UT.source_key, "name": UT.name, "url": UT.base_url}


class Adapter(unittest.TestCase):
    def crawl(self, pages=None):
        pages = pages or sample_pages()
        recorder = Recorder({API + "1": pages[0], API + "2": pages[1]})
        return LocalistAdapter().crawl(recorder, [], options()), recorder

    def test_the_known_source_is_a_real_localist_calendar_with_its_own_time_zone(self):
        self.assertEqual((UT.source_key, UT.adapter, UT.base_url), ("ut_austin", "localist", SITE))
        self.assertEqual(dict(UT.options)["timezone"], "America/Chicago")
        self.assertGreaterEqual(UT.interval_s, 3600, "an hourly read of a public calendar is polite")

    def test_every_page_is_read_and_each_instance_becomes_one_candidate_with_its_own_page_address(self):
        (result, recorder) = self.crawl()
        self.assertEqual(recorder.urls, [API + "1", API + "2"], "paging follows the API's page count")
        self.assertEqual(result.notes["pages"], 2)
        events = result.candidates
        self.assertEqual((len(events), result.notes["all_day_skipped"]), (7, 1), "8 sampled instances, one flagged all-day and skipped")
        self.assertEqual(len({e["id"] for e in events}), 7)
        for event in events:
            self.assertTrue(event["id"].startswith("ut_austin:"))
            self.assertTrue(event["url"].startswith(SITE + "/event/"), event["url"])
            self.assertEqual(event["timezone"], "America/Chicago")
            self.assertRegex(event["start"], r"[-+]\d\d:\d\d$", "times carry the API's UTC offset")
            self.assertTrue(any("Localist API event instance" in line for line in event["verification"]["evidence"]))
            self.assertFalse(event["verified"])

    def test_missing_facts_stay_missing_and_nothing_is_invented(self):
        (result, _) = self.crawl()
        by_title = {e["title"]: e for e in result.candidates}
        fort = by_title["Fortlandia"]
        self.assertEqual(fort["address"], "4801 La Crosse Avenue, Austin, TX 78739")
        self.assertTrue(fort["end"] and fort["geo"]["source"] == "Localist API geo")
        nowhere = by_title["Student Affairs Meeting"]
        self.assertEqual((nowhere["venue"], nowhere["address"]), (None, None))
        self.assertNotIn("geo", nowhere, "coordinates only from the API's own geo block")
        cpr = by_title["AHA BLS Provider (CPR)"]
        self.assertTrue(cpr["venue"] and cpr["address"] is None)

    def test_a_start_at_midnight_that_the_source_did_not_call_all_day_is_kept_as_it_was_given(self):
        """A product-quality limit, recorded rather than hidden: the source gives 00:00 for some date-only entries (offers, deadlines)."""
        (result, _) = self.crawl()
        midnight = [e for e in result.candidates if e["start"][11:19] == "00:00:00"]
        self.assertEqual([e["title"] for e in midnight], ["BCBSTX Fitness Discount Program: Enrollment Fee Waived"])

    def test_unusable_responses_are_errors_not_empty_results(self):
        pages = sample_pages()
        for name, broken in (("not json", "<html>maintenance</html>"), ("no events list", {"page": {"total": 1}}), ("a list", [1, 2])):
            with self.subTest(name):
                with self.assertRaises(FetchError):
                    LocalistAdapter().crawl(Recorder({API + "1": broken}), [], options())
        with self.assertRaises(FetchError):   # the second page fails: the run fails as a whole instead of storing half a calendar
            LocalistAdapter().crawl(Recorder({API + "1": pages[0], API + "2": FetchError("HTTP 503")}), [], options())


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = os.path.join(self.folder.name, "discovery.db")
        self.clock = Clock()
        self.pages = {API + "1": sample_pages()[0], API + "2": sample_pages()[1], **city_pages()}
        self.client = JsonClient(self.pages)
        self.built = self.build()
        self.sd = self.built.control.create_area(CreateAreaRequest("San Diego", 32.7157, -117.1611, 25, sources=("sandiego_gov",))).area
        self.austin = self.built.control.create_area(CreateAreaRequest("Austin", 30.2672, -97.7431, 35, sources=("ut_austin",))).area

    def build(self):
        built = app_module.build(self.db, clock=self.clock, fetch_client=self.client)
        self.addCleanup(built.close)
        built.service.register_source(SPEC)
        built.register_known_sources()
        return built

    def count(self, table, where="1=1"):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
        finally:
            connection.close()

    def discover_and_extract(self):
        reports = self.built.service.run_until_idle(10)
        for _ in range(20):
            if not self.built.extraction.run_once().claimed:
                break
        return reports

    def events(self, area):
        return self.built.events.query(EventQuery(area_key=area.area_key, limit=200)).events

    def test_austin_events_travel_the_existing_pipeline_into_the_same_database(self):
        reports = self.discover_and_extract()
        self.assertEqual(sorted(r.outcome for r in reports if r.job_key), ["completed", "completed"])
        self.assertEqual(self.client.urls.count(API + "1"), 1)
        self.assertEqual((self.count("candidates", "candidate_key LIKE 'ut_austin:%'"), self.count("candidates", "candidate_key LIKE 'sandiego_gov:%'")), (7, 4))
        austin, san_diego = self.events(self.austin), self.events(self.sd)
        self.assertEqual((len(austin), len(san_diego)), (7, 4))
        self.assertTrue(all(e.sources == ("ut_austin",) for e in austin))
        self.assertTrue(all(e.sources == ("sandiego_gov",) for e in san_diego), "San Diego is unchanged and holds no Austin event")
        self.assertEqual(self.count("events"), 11, "one table, one database, both areas")
        fort = [e for e in austin if e.title == "Fortlandia"][0]
        self.assertEqual((fort.address, fort.location_status, fort.timezone), ("4801 La Crosse Avenue, Austin, TX 78739", "address", "America/Chicago"))
        self.assertTrue(fort.source_url.startswith(SITE + "/event/"))
        detail = self.built.events.get(fort.event_key)
        self.assertEqual(detail.history[0]["extractor"], "stored-candidate")
        self.assertIn("title", detail.evidence)

    def test_times_are_the_instants_the_source_gave_with_their_offsets(self):
        self.discover_and_extract()
        with self.built.store.read() as connection:
            rows = connection.execute("SELECT c.payload_json, e.starts_at FROM candidates c JOIN event_candidates l USING (candidate_id) JOIN events e ON e.event_id = l.event_id "
                                      "WHERE c.candidate_key LIKE 'ut_austin:%'").fetchall()
        self.assertEqual(len(rows), 7)
        for payload, starts_at in rows:
            self.assertEqual(starts_at, int(datetime.fromisoformat(json.loads(payload)["start"]).timestamp()))
            self.assertTrue(starts_at > datetime.now(timezone.utc).timestamp() - 86400)

    def test_unlocated_and_unverified_places_are_marked_not_invented(self):
        self.discover_and_extract()
        by_title = {e.title: e for e in self.events(self.austin)}
        meeting = by_title["Student Affairs Meeting"]
        self.assertEqual((meeting.location_status, meeting.address, meeting.venue_name), ("unknown", None, None))
        self.assertIn("location", meeting.missing)
        cpr = by_title["AHA BLS Provider (CPR)"]
        self.assertEqual(cpr.location_status, "venue")
        self.assertIsNone(cpr.address)

    def test_provenance_and_evidence_point_at_the_stored_api_responses(self):
        self.discover_and_extract()
        with self.built.store.read() as connection:
            feeds = connection.execute("SELECT p.url, sp.role, p.status FROM pages p JOIN source_pages sp USING (page_id) JOIN sources s USING (source_id) "
                                       "WHERE s.source_key='ut_austin' AND sp.role='feed'").fetchall()
            quotes = connection.execute("SELECT e.field, e.quote, p.url FROM evidence e JOIN candidates c USING (candidate_id) JOIN pages p ON p.page_id = e.page_id "
                                        "WHERE c.candidate_key LIKE 'ut_austin:%' AND e.quote IS NOT NULL").fetchall()
        self.assertEqual(sorted(r[0] for r in feeds), sorted(canonical_url(API + n) for n in "12"), "stored under the canonical form of the address")
        self.assertTrue(all(r[2] == "ok" for r in feeds))
        self.assertTrue(any(field == "title" for field, _, _ in quotes), "titles are quoted from the stored API response")
        self.assertTrue(all(url.startswith(SITE + "/api/2/events") for _, _, url in quotes))
        self.assertEqual(self.count("evidence", "content_hash IS NOT NULL AND content_hash NOT IN (SELECT content_hash FROM contents)"), 0)

    def test_reading_the_source_again_duplicates_nothing(self):
        self.discover_and_extract()
        before = {t: self.count(t) for t in ("candidates", "events", "extractions", "contents", "pages", "evidence")}
        changes = self.count("changes")
        self.clock.t += 3601
        self.built.service.make_due("ut_austin")
        reports = [r for r in self.built.service.run_until_idle(10) if r.job_key == "crawl_source:ut_austin"]
        self.assertEqual(reports[0].outcome, "completed")
        self.assertEqual((reports[0].candidates_new, reports[0].candidates_changed, reports[0].candidates_unchanged), (0, 0, 7))
        self.assertEqual(self.built.extraction.run_once().claimed, 0, "no new work: the evidence did not change")
        self.assertEqual({t: self.count(t) for t in before}, before)
        self.assertEqual(self.count("changes"), changes, "nothing changed, so no change record")

    def test_a_changed_event_updates_the_same_record(self):
        self.discover_and_extract()
        page = json.loads(json.dumps(self.pages[API + "2"]))
        target = [w for w in page["events"] if w["event"]["title"] == "Fortlandia"][0]["event"]
        target["location_name"] = "Lady Bird Johnson Wildflower Center (moved to the north lawn)"
        self.pages[API + "2"] = page
        self.clock.t += 3601
        self.built.service.make_due("ut_austin")
        self.discover_and_extract()
        fort = [e for e in self.events(self.austin) if e.title == "Fortlandia"][0]
        self.assertEqual((fort.revision, fort.venue_name), (2, "Lady Bird Johnson Wildflower Center (moved to the north lawn)"))
        self.assertEqual(self.count("events"), 11)

    def test_a_failing_austin_source_backs_off_while_san_diego_carries_on(self):
        self.pages[API + "2"] = FetchError("HTTP 503")
        reports = {r.job_key: r for r in self.built.service.run_until_idle(10) if r.job_key}
        self.assertEqual((reports["crawl_source:ut_austin"].outcome, reports["crawl_source:sandiego_gov"].outcome), ("fetch_failed", "completed"))
        self.assertEqual(self.count("candidates", "candidate_key LIKE 'ut_austin:%'"), 0, "half a calendar is not stored")
        with self.built.store.read() as connection:
            job = connection.execute("SELECT state, attempts, next_run_at FROM jobs WHERE job_key='crawl_source:ut_austin'").fetchone()
        self.assertEqual((job["state"], job["attempts"], job["next_run_at"] - self.clock()), ("ready", 1, 900))
        self.pages[API + "2"] = sample_pages()[1]
        self.clock.t += 901
        self.assertEqual({r.job_key: r.outcome for r in self.built.service.run_until_idle(10) if r.job_key}, {"crawl_source:ut_austin": "completed"})
        self.assertEqual(self.count("candidates", "candidate_key LIKE 'ut_austin:%'"), 7)

    def test_a_service_restart_in_the_middle_of_a_crawl_loses_nothing(self):
        with self.built.store.write() as tx:
            self.built.service.jobs.claim(tx, "crashed", "lost-lease", self.clock(), 600, True)   # the process dies holding a lease
        self.built.close()
        self.clock.t += 601
        again = self.build()
        self.assertGreaterEqual(again.service.recover(), 1)
        outcomes = [r.outcome for r in again.service.run_until_idle(10) if r.job_key]
        self.assertEqual(sorted(outcomes), ["completed", "completed"])
        self.assertEqual(self.count("candidates", "candidate_key LIKE 'ut_austin:%'"), 7)

    def test_registering_the_known_sources_twice_registers_them_once(self):
        self.built.register_known_sources()
        self.built.register_known_sources()
        self.assertEqual((self.count("sources"), self.count("jobs")), (2, 2))
        self.assertEqual(self.count("area_sources"), 2)

    def test_the_one_database_stays_consistent(self):
        self.discover_and_extract()
        self.clock.t += 3601
        self.built.service.make_due("ut_austin")
        self.discover_and_extract()
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()
        self.assertEqual([f for f in os.listdir(self.folder.name) if f.endswith(".db")], ["discovery.db"], "no second database")

    def test_the_json_text_helper_is_bounded_and_tolerates_garbage(self):
        self.assertEqual(DiscoveryService._json_strings(b"not json"), [])
        self.assertEqual(DiscoveryService._json_strings(json.dumps({"a": ["x", {"b": "y"}], "n": 3}).encode()), ["x", "y"])
        self.assertLess(len("".join(DiscoveryService._json_strings(json.dumps(["z" * 1000] * 5000).encode(), limit=10_000))), 20_000)


class DatabaseOpens(unittest.TestCase):
    def test_a_database_from_before_this_milestone_opens_and_takes_the_new_source(self):
        with tempfile.TemporaryDirectory() as folder:
            db = os.path.join(folder, "discovery.db")
            DiscoveryStore(db).open().close()
            built = app_module.build(db, clock=Clock(), fetch_client=JsonClient({}))
            try:
                self.assertEqual(len(built.register_known_sources()), 2)
            finally:
                built.close()


if __name__ == "__main__":
    unittest.main()
