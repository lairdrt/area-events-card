"""Milestone 2C: independent extraction into the one discovery database, the processing contract, the event query service and the API around them.

Simulated: saved pages (the City of San Diego page format for both the San Diego source and an "Austin" fixture source; the Austin source is a
fixture of the same adapter, NOT live Austin discovery, and its times are interpreted in Pacific time by that adapter) and direct candidate rows
for the extraction edge cases. No website, no card and no Home Assistant are involved."""
import ast
import dataclasses
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.db import database
from area_events import app as app_module
from area_events.api.http_api import ApiTokens
from area_events.control.service import CreateAreaRequest, NotFound, ValidationFailed
from area_events.api.events import EventQuery
from area_events.extraction.service import (
    VERSION,
    ExtractionConfig,
    ExtractionService,
    HttpProcessingClient,
    LocalProcessingClient,
    ProcessingUnavailable,
    StoredCandidateExtractor,
)
from area_events.discovery.host import HostConfig
from area_events.contracts.models import LeaseLost, StaleResult
from area_events.contracts.models import SourceSpec
from area_events.persistence.schema import LATEST, MIGRATIONS
from area_events.persistence.store import DiscoveryStore

from area_events.persistence.repositories import ChangeLog, PageRepository, SourceRepository
from tests.records import document, put_document
from tests.test_discovery_control import CONTROL_TOKEN, READ_TOKEN, request
from tests.test_discovery_service import SPEC, Clock, FakeClient, city_pages
from tests.city_fixtures import CITY_URL, city_detail, city_listing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE_DIR = os.path.join(ROOT, "src", "area_events")
PROCESS_TOKEN = "p" * 40
AUSTIN_URL = "https://austin.example/events"
AUSTIN = SourceSpec("austin_fixture", "sandiego_gov", "Austin (fixture)", AUSTIN_URL, options=(("pages", 1),), interval_s=3600)
NAME = StoredCandidateExtractor.name


def austin_pages(n=3):
    pages = {AUSTIN_URL: city_listing([(f"a{i}", f"Austin Fest {i}", i + 1) for i in range(n)])}
    for i in range(n):
        pages[f"https://austin.example/event/a{i}"] = city_detail(f"Congress Avenue block {i}", f"{i + 1}00 Congress Avenue Austin, TX")
    return pages


def candidate_payload(title="Harbor Concert", **fields):
    """A stored candidate document (see tests/records.py): the standard fields, `starts_at`/`ends_at` for the times."""
    return document(title, **fields)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = os.path.join(self.folder.name, "discovery.db")
        self.clock = Clock()
        self.pages = {**city_pages(), **austin_pages()}
        self.client = FakeClient(self.pages)

    def build(self, **kwargs):
        built = app_module.build(self.db, clock=self.clock, fetch_client=kwargs.pop("fetch_client", self.client), **kwargs)
        self.addCleanup(built.close)
        built.service.register_source(SPEC)
        built.service.register_source(AUSTIN)
        return built

    def two_areas(self, built):
        sd = built.control.create_area(CreateAreaRequest("San Diego", 32.7157, -117.1611, 25, sources=("sandiego_gov",))).area
        austin = built.control.create_area(CreateAreaRequest("Austin", 30.2672, -97.7431, 35, sources=("austin_fixture",))).area
        return sd, austin

    def discover(self, built):
        outcomes = [r.outcome for r in built.service.run_until_idle(10)]
        self.assertEqual(outcomes.count("completed"), 2, outcomes)

    def drain(self, built, service=None):
        service = service or built.extraction
        total = ExtractionReportSum()
        for _ in range(20):
            report = service.run_once()
            total.add(report)
            if not report.claimed:
                break
        return total

    def count(self, table, where="1=1"):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
        finally:
            connection.close()

    def add_candidate(self, built, source_key, key, payload, url=None):
        now = int(self.clock())
        with database(built).write() as tx:
            source = SourceRepository().by_key(tx, source_key)
            page_id = PageRepository().touch(tx, url, source["source_id"], "event", now)[0] if url else None
            digest = put_document(tx, key, source["source_id"], page_id, payload, now)
            ChangeLog().append(tx, "candidate", key, digest, now)

    def all_events(self, built, **kwargs):
        events, cursor = [], None
        while True:
            page = built.events.query(EventQuery(after=cursor, limit=kwargs.pop("limit", 200), **kwargs))
            events.extend(page.events)
            cursor = page.next_cursor
            if not cursor:
                return events


class ExtractionReportSum:
    def __init__(self):
        self.claimed = self.submitted = self.failed = self.stale = 0
        self.outcomes = []

    def add(self, report):
        self.claimed += report.claimed
        self.submitted += report.submitted
        self.failed += report.failed
        self.stale += report.stale
        self.outcomes.extend(report.outcomes)


class Pipeline(Fixture):
    def test_saved_candidates_become_events_without_fetching_anything(self):
        built = self.build()
        self.two_areas(built)
        self.discover(built)
        fetched = len(self.client.urls)
        report = self.drain(built)
        self.assertEqual((report.submitted, report.failed, report.stale), (7, 0, 0))
        self.assertEqual(len(self.client.urls), fetched, "extraction used the stored pages only")
        events = {e.title: e for e in self.all_events(built)}
        self.assertEqual(len(events), 7)
        event = events["Austin Fest 0"]
        self.assertEqual((event.location_status, event.address, event.status, event.revision), ("address", "100 Congress Avenue", "complete", 1),
                         "the City adapter appends only a California city to a street address, so the Austin fixture's address stays partial")
        self.assertEqual((event.venue_name, event.location_text), ("Congress Avenue block 0", None), "a short phrase without sentence punctuation is kept as the venue name")
        self.assertEqual((event.sources, event.candidates), (("austin_fixture",), ("austin_fixture:a0@" + event.candidates[0].split("@")[1],)))
        self.assertTrue(event.source_url.startswith("https://austin.example/event/a0"))
        detail = built.events.get(event.event_key)
        self.assertEqual([h["revision"] for h in detail.history], [1])
        self.assertEqual(detail.history[0]["extractor"], NAME)
        self.assertIn("title", detail.evidence)
        self.assertTrue(any(ref.get("quote") for ref in detail.evidence["title"]["evidence"]), "the title's quote comes from the stored page")
        self.assertEqual(self.count("extractions", "status='complete'"), 7)
        kinds = [c.kind for c in built.reader.changes(0, 500).changes]
        self.assertEqual(kinds.count("event.created"), 7)

    def test_nothing_is_invented_and_failures_are_recorded_with_their_reason(self):
        built = self.build()
        no_offset = candidate_payload("Floating", starts_at="2030-05-01T18:00:00")
        no_start = candidate_payload("No start", starts_at=None)
        no_title = candidate_payload(title="")
        bare = candidate_payload("Bare", venue=None, address=None, description=None, ends_at=None)
        backwards = candidate_payload("Backwards", ends_at="2030-05-01T09:00:00-07:00")
        sentence = candidate_payload("Walk", venue="Meet near the fountain on the north side of the park.", address=None)
        spaced = candidate_payload("Spaced", description="Meet at 1200 Park Boulevard, San Diego, CA 92101 for the walk.", venue="Park Hall", address=None)
        for key, payload in (("s:1", no_offset), ("s:2", no_start), ("s:3", no_title), ("s:4", bare), ("s:5", backwards), ("s:6", sentence), ("s:7", spaced)):
            self.add_candidate(built, "sandiego_gov", key, payload)
        report = self.drain(built)
        self.assertEqual((report.submitted, report.failed), (7, 0))
        with database(built).read() as connection:
            failures = {r[0]: r[1] for r in connection.execute("SELECT c.candidate_key, x.failure_reason FROM extractions x JOIN candidates c USING (candidate_id) WHERE x.status='failed'")}
        self.assertEqual(sorted(failures), ["s:1", "s:2", "s:3"])
        self.assertIn("no UTC offset", failures["s:1"])
        self.assertEqual(self.count("events"), 4, "failed extractions create no event")
        events = {e.title: e for e in self.all_events(built)}
        self.assertEqual((events["Bare"].location_status, events["Bare"].missing, events["Bare"].status), ("unknown", ("ends_at", "description", "location"), "partial"))
        self.assertIsNone(events["Bare"].ends_at)
        self.assertIsNone(events["Backwards"].ends_at, "an end before the start is dropped, not repaired")
        self.assertEqual((events["Walk"].venue_name, events["Walk"].location_text, events["Walk"].location_status),
                         (None, "Meet near the fountain on the north side of the park.", "description_only"))
        self.assertEqual(events["Spaced"].address, "1200 Park Boulevard, San Diego, CA 92101")
        self.assertEqual(events["Spaced"].venue_name, "Park Hall")
        evidence = built.events.get(events["Spaced"].event_key).evidence["address"]
        self.assertEqual(evidence["origin"], "description_text")
        self.assertIn("1200 Park Boulevard", evidence["evidence"][0]["quote"])

    def test_a_source_revision_updates_the_same_event_and_explains_the_change(self):
        built = self.build()
        self.two_areas(built)
        self.discover(built)
        self.drain(built)
        before = {e.title: e for e in self.all_events(built, area_key=None)}
        target = before["Event 0"]
        urls = len(self.client.urls)
        for _ in range(30):  # clients reading events do not crawl
            self.all_events(built)
        self.assertEqual(len(self.client.urls), urls)
        self.pages[CITY_URL] = self.pages[CITY_URL].replace("10:00 AM - 2:00 PM", "6:30 PM - 9:00 PM")  # the source now says the event is in the evening
        self.clock.t += 7200
        built.service.make_due("sandiego_gov")
        self.assertIn("completed", [r.outcome for r in built.service.run_until_idle(10)])
        self.assertEqual(built.events.get(target.event_key).event.starts_at, target.starts_at, "the event is unchanged until extraction has processed the new revision")
        report = self.drain(built)
        self.assertEqual(report.submitted, 4)
        self.assertEqual(sorted(set(report.outcomes)), ["updated"])
        after = built.events.get(target.event_key)
        self.assertEqual((after.event.revision, after.event.event_key), (2, target.event_key))
        self.assertNotEqual(after.event.starts_at, target.starts_at)
        self.assertEqual([h["revision"] for h in after.history], [1, 2])
        self.assertEqual(set(after.history[1]["changed"]), {"starts_at", "ends_at"})
        self.assertEqual(after.history[1]["changed"]["starts_at"][0], target.starts_at)
        self.assertEqual(after.history[1]["changed"]["starts_at"][1], after.event.starts_at)
        self.assertEqual(self.count("events"), 7, "no duplicate event was created")
        self.assertEqual(self.count("extractions", f"candidate_id IN (SELECT candidate_id FROM event_candidates WHERE event_id={self.event_id(target.event_key)})"), 2,
                         "both revisions' extractions are kept")
        self.assertEqual([c.kind for c in built.reader.changes(0, 500).changes].count("event.updated"), 8,
                         "4 when the crawl restated the events' verification evidence (it quotes the listing's date line), 4 when extraction revised the events")
        # restarting the worker neither loses nor repeats the update
        built.close()
        again = self.build()
        self.assertEqual(self.drain(again, again.extraction).claimed, 0)
        self.assertEqual(sorted(e.revision for e in self.all_events(again)), [1, 1, 1, 2, 2, 2, 2])
        self.assertEqual(self.count("events"), 7)
        self.assertEqual(self.count("extractions"), 11)

    def event_id(self, event_key):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute("SELECT event_id FROM events WHERE event_key=?", (event_key,)).fetchone()[0]
        finally:
            connection.close()

    def test_a_better_extractor_version_reprocesses_without_recrawling(self):
        built = self.build()
        self.two_areas(built)
        self.discover(built)
        self.drain(built)
        urls = len(self.client.urls)
        newer = StoredCandidateExtractor()
        newer.version = VERSION + 1
        service = ExtractionService(LocalProcessingClient(built.processing), newer, self.clock, worker_id="v2")
        report = self.drain(built, service)
        self.assertEqual((report.submitted, sorted(set(report.outcomes))), (7, ["unchanged"]), "the same algorithm gives the same events: nothing is rewritten")
        self.assertEqual(self.count("extractions"), 14)
        self.assertEqual(self.count("events"), 7)
        self.assertEqual(len(self.client.urls), urls)


class TwoAreas(Fixture):
    def test_areas_see_only_their_own_events_and_overlap_does_not_duplicate(self):
        built = self.build()
        sd, austin = self.two_areas(built)
        overlap = built.control.create_area(CreateAreaRequest("North County", 33.1, -117.2, 20, sources=("sandiego_gov",))).area
        empty = built.control.create_area(CreateAreaRequest("Nowhere", 10.0, 10.0, 5)).area
        self.discover(built)
        self.assertEqual(self.client.urls.count(CITY_URL), 1, "two areas, one crawl of the shared source")
        self.drain(built)
        titles = lambda **q: sorted(e.title for e in self.all_events(built, **q))  # noqa: E731
        self.assertEqual(titles(area_key=sd.area_key), [f"Event {i}" for i in range(4)])
        self.assertEqual(titles(area_key=austin.area_key), [f"Austin Fest {i}" for i in range(3)])
        self.assertEqual(titles(area_key=overlap.area_key), titles(area_key=sd.area_key))
        self.assertEqual(titles(area_key=empty.area_key), [])
        self.assertEqual(len(titles()), 7)
        self.assertEqual(self.count("events"), 7, "events are not copied per area")
        self.assertEqual(titles(source_key="austin_fixture"), titles(area_key=austin.area_key))
        with self.assertRaises(NotFound):
            built.events.query(EventQuery(area_key="nosucharea1"))
        with self.assertRaises(NotFound):
            built.events.query(EventQuery(source_key="nosuchsource"))

    def test_both_areas_are_scheduled_and_extracted_concurrently_by_their_own_components(self):
        built = self.build(host_config=HostConfig(idle_wait_s=0.05, busy_wait_s=0.01), extraction_config=ExtractionConfig(idle_wait_s=0.05, busy_wait_s=0.01))
        self.two_areas(built)
        built.host.start()
        built.extraction.start()
        deadline = time.time() + 15
        while time.time() < deadline and self.count("events") < 7:
            time.sleep(0.05)
        self.assertTrue(built.extraction.stop(5))
        self.assertTrue(built.host.stop(5))
        self.assertEqual(self.count("events"), 7)
        self.assertEqual(built.extraction.status()["state"], "stopped")

    def test_filters_and_pagination_are_bounded_and_stable(self):
        built = self.build()
        self.two_areas(built)
        self.discover(built)
        self.drain(built)
        everything = self.all_events(built)
        starts = [e.starts_at for e in everything]
        self.assertEqual(starts, sorted(starts))
        pages, cursor, seen = 0, None, []
        while True:
            page = built.events.query(EventQuery(after=cursor, limit=2))
            pages += 1
            seen += [e.event_key for e in page.events]
            cursor = page.next_cursor
            if not cursor:
                break
        self.assertEqual((pages, seen), (4, [e.event_key for e in everything]))
        self.assertEqual(len(built.events.query(EventQuery(limit=100000)).events), 7, "the page size is capped")
        middle = everything[3].starts_at
        self.assertTrue(all(e.starts_at >= middle for e in self.all_events(built, starts_from=middle)))
        self.assertTrue(all(e.starts_at < middle for e in self.all_events(built, starts_to=middle)))
        with self.assertRaises(ValidationFailed):
            built.events.query(EventQuery(after="1; DROP TABLE events"))
        with self.assertRaises(NotFound):
            built.events.get("ev-doesnotexist")


class Identity(Fixture):
    def test_several_candidates_for_one_event_share_it_and_only_the_primary_defines_it(self):
        built = self.build()
        self.two_areas(built)
        page = "https://www.sandiego.gov/event/harbor"
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:harbor", candidate_payload(), page)
        self.add_candidate(built, "austin_fixture", "austin_fixture:harbor-copy", candidate_payload(venue="Harbor Hall", description="A different wording."), page)
        self.drain(built)
        events = self.all_events(built)
        self.assertEqual(len(events), 1, "the same title, start and address is one event")
        self.assertEqual((events[0].sources, len(events[0].candidates)), (("austin_fixture", "sandiego_gov"), 2))
        self.assertEqual(events[0].venue_name, "Harbor Hall", "the earlier candidate defines the event; the copy only adds provenance")
        detail = built.events.get(events[0].event_key)
        self.assertEqual(sorted(link["role"] for link in detail.candidate_links), ["duplicate", "primary"])
        self.assertEqual(self.count("pages"), 1, "one page referenced by both sources' candidates")
        self.assertEqual(self.count("source_pages"), 2)
        sd, austin = (built.events.query(EventQuery(area_key=a.area_key)).events for a in built.control.list_areas())
        self.assertEqual((len(sd), len(austin)), (1, 1), "an event several sources describe is reachable from each of their areas")
        # a changed copy does not rewrite the event; a changed primary does
        self.add_candidate(built, "austin_fixture", "austin_fixture:harbor-copy", candidate_payload(venue="Elsewhere", description="Again different."), page)
        self.assertEqual(self.drain(built).outcomes, ["duplicate"])
        self.assertEqual(self.all_events(built)[0].revision, 1)
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:harbor", candidate_payload(venue="Harbor Hall East"), page)
        self.assertEqual(self.drain(built).outcomes, ["updated"])
        self.assertEqual(self.all_events(built)[0].venue_name, "Harbor Hall East")

    def test_without_an_address_candidates_are_never_merged(self):
        built = self.build()
        self.add_candidate(built, "sandiego_gov", "s:a", candidate_payload(address=None))
        self.add_candidate(built, "sandiego_gov", "s:b", candidate_payload(address=None))
        self.drain(built)
        self.assertEqual(len(self.all_events(built)), 2)


class Processing(Fixture):
    def setUp(self):
        super().setUp()
        self.built = self.build()
        self.two_areas(self.built)
        self.discover(self.built)

    def claim(self, limit=1, lease_s=60, worker="w1"):
        return self.built.processing.claim(worker, NAME, VERSION, limit, lease_s).items

    def result(self, item):
        return StoredCandidateExtractor().extract(dataclasses.asdict(item))

    def test_a_claimed_item_carries_the_stored_evidence_and_page_text(self):
        item = self.claim()[0]
        self.assertEqual(item.source_key, "sandiego_gov")
        self.assertIn("Location:", item.page["text"])
        self.assertTrue(any(e["field"] == "title" and e["quote"] for e in item.evidence))
        self.assertTrue(item.payload["title"].startswith("Event "))
        self.assertEqual(len(self.claim(limit=20)), 6, "the leased item is not offered again")

    def test_a_result_and_its_checkpoint_commit_together_or_not_at_all(self):
        item = self.claim()[0]
        with mock.patch.object(self.built.persistence.processing._changes, "append", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, self.result(item))
        self.assertEqual((self.count("extractions"), self.count("events"), self.count("event_candidates")), (0, 0, 0), "nothing of the failed attempt remains")
        self.assertEqual(self.count("processing_work", "state='leased'"), 1, "and the item is still leased, so it can be retried")
        ack = self.built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, self.result(item))
        self.assertEqual((ack.outcome, ack.event_revision, self.count("events"), self.count("processing_work", "state='done'")), ("created", 1, 1, 1))

    def test_a_crashed_worker_loses_nothing_and_its_old_lease_is_useless(self):
        item = self.claim(lease_s=30, worker="crashed")[0]
        self.assertNotIn(item.candidate_key, [i.candidate_key for i in self.claim(limit=20)])
        self.clock.t += 31
        retaken = [i for i in self.claim(limit=20, worker="second") if i.candidate_key == item.candidate_key][0]
        with self.assertRaises(LeaseLost):
            self.built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, self.result(item))
        ack = self.built.processing.submit(retaken.candidate_key, NAME, VERSION, retaken.lease_token, self.result(retaken))
        self.assertEqual(ack.outcome, "created")

    def test_a_result_from_an_older_revision_never_overwrites_newer_evidence(self):
        item = self.claim()[0]
        self.pages[CITY_URL] = self.pages[CITY_URL].replace("10:00 AM - 2:00 PM", "7:00 PM - 9:00 PM")
        self.built.service.make_due("sandiego_gov")
        self.assertIn("completed", [r.outcome for r in self.built.service.run_until_idle(10)])
        with self.assertRaises(StaleResult):
            self.built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, self.result(item))
        self.assertEqual((self.count("extractions"), self.count("events")), (0, 0))
        fresh = [i for i in self.claim(limit=20) if i.candidate_key == item.candidate_key][0]
        self.assertNotEqual(fresh.payload_hash, item.payload_hash)
        self.assertEqual(self.built.processing.submit(fresh.candidate_key, NAME, VERSION, fresh.lease_token, self.result(fresh)).outcome, "created")

    def test_repeated_submissions_are_acknowledged_without_duplicating_anything(self):
        item = self.claim()[0]
        result = self.result(item)
        first = self.built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, result)
        again = self.built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, result)
        self.assertEqual((first.replayed, again.replayed, again.event_key, again.extraction_id), (False, True, first.event_key, first.extraction_id))
        self.assertEqual((self.count("extractions"), self.count("events"), self.count("changes", "entity='event'")), (1, 1, 1))
        with self.assertRaises(LeaseLost):  # a different result on a used lease is not accepted
            self.built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, {**result, "issues": ["changed"]})

    def test_invalid_results_are_refused_and_write_nothing(self):
        item = self.claim()[0]
        good = self.result(item)
        bad = [{"event": {**good["event"], "title": 5}}, {"event": {**good["event"], "starts_at": "tomorrow"}}, {"event": {**good["event"], "secret": 1}},
               {"event": {**good["event"], "ends_at": 1}}, {"event": {**good["event"], "source_url": "file:///etc/passwd"}}, {"event": None},
               {"event": good["event"], "fields": {"title": {"origin": "x", "evidence": [{"sql": "DROP TABLE events"}]}}}, {"extra": 1, "event": good["event"]},
               {"event": {**good["event"], "description": "x" * 5000}}, "not an object", {"event": {**good["event"], "title": "x" * 301}}]
        for result in bad:
            with self.subTest(str(result)[:50]):
                with self.assertRaises(ValidationFailed):
                    self.built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, result)
        self.assertEqual((self.count("extractions"), self.count("events")), (0, 0))
        self.assertEqual(self.built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, good).outcome, "created")

    def test_failures_back_off_and_stop_after_the_last_attempt_until_the_source_changes(self):
        outcomes = []
        for attempt in range(4):
            item = [i for i in self.claim(limit=20) if i.candidate_key.startswith("sandiego_gov:e0@")][0]
            outcomes.append(self.built.processing.fail(item.candidate_key, NAME, VERSION, item.lease_token, "boom")["state"])
            self.assertNotIn(item.candidate_key, [i.candidate_key for i in self.claim(limit=20)], "not offered again before its retry time")
            self.clock.t += 300 * 2 ** attempt + 1
        self.assertEqual(outcomes, ["retrying", "retrying", "retrying", "failed"])
        self.clock.t += 10 ** 6
        self.assertNotIn(item.candidate_key, [i.candidate_key for i in self.claim(limit=20)], "given up until something changes")
        self.pages[CITY_URL] = self.pages[CITY_URL].replace("10:00 AM - 2:00 PM", "7:00 PM - 9:00 PM")
        self.built.service.make_due("sandiego_gov")
        self.built.service.run_until_idle(10)
        self.assertIn(item.candidate_key, [i.candidate_key for i in self.claim(limit=20)], "a new source revision makes it pending again")
        status = self.built.processing.status(NAME, VERSION)
        self.assertEqual(status.failed, 0)


class Isolation(Fixture):
    def test_an_extractor_that_fails_on_one_item_does_not_stop_the_others(self):
        built = self.build()
        self.two_areas(built)
        self.discover(built)

        class Flaky(StoredCandidateExtractor):
            def extract(self, item):
                if item["payload"]["title"] == "Event 1":
                    raise RuntimeError("parser bug")
                return super().extract(item)
        service = ExtractionService(LocalProcessingClient(built.processing), Flaky(), self.clock, worker_id="flaky")
        report = self.drain(built, service)
        self.assertEqual((report.submitted, report.failed), (6, 1))
        self.assertEqual(self.count("events"), 6)
        self.assertEqual(self.count("processing_work", "last_error LIKE '%parser bug%'"), 1)

    def test_discovery_keeps_running_while_extraction_is_stalled(self):
        built = self.build(host_config=HostConfig(idle_wait_s=0.05, busy_wait_s=0.01), extraction_config=ExtractionConfig(idle_wait_s=0.05, lease_s=300))
        self.two_areas(built)
        release = threading.Event()
        entered = threading.Event()

        class Stalled(StoredCandidateExtractor):
            def extract(self, item):
                entered.set()
                release.wait(20)
                return super().extract(item)
        stalled = ExtractionService(LocalProcessingClient(built.processing), Stalled(), self.clock, ExtractionConfig(idle_wait_s=0.05), worker_id="stalled")
        built.host.start()
        stalled.start()
        try:
            self.assertTrue(entered.wait(15))
            self.assertEqual(self.wait_count("candidates", 7), 7, "discovery of both areas completed while extraction was stuck")
            # more discovery writes keep landing
            self.pages[CITY_URL] = self.pages[CITY_URL].replace("10:00 AM - 2:00 PM", "8:00 AM - 9:00 AM")
            self.clock.t += 7200
            built.service.make_due("sandiego_gov")
            self.assertEqual(self.wait_count("changes", 1, "kind='candidate.changed' AND seq > 7", minimum=True), self.count("changes", "kind='candidate.changed' AND seq > 7"))
            self.assertEqual(self.count("events"), 0)
            # another worker is not blocked by the stalled one: it takes the items the stalled one has not leased
            other = ExtractionService(LocalProcessingClient(built.processing), StoredCandidateExtractor(), self.clock, worker_id="other")
            self.drain(built, other)
            self.assertGreaterEqual(self.count("events"), 1)
        finally:
            release.set()
            stalled.stop(10)
            built.host.stop(10)

    def wait_count(self, table, expected, where="1=1", minimum=False):
        deadline = time.time() + 15
        while time.time() < deadline:
            n = self.count(table, where)
            if n >= expected if minimum else n == expected:
                return n
            time.sleep(0.05)
        return self.count(table, where)

    def test_a_stopped_worker_resumes_from_the_database_alone(self):
        built = self.build()
        self.two_areas(built)
        self.discover(built)
        built.processing.claim("dying", NAME, VERSION, 20, 60)  # claimed everything, then the process died
        self.assertEqual(self.drain(built).claimed, 0)
        self.clock.t += 61
        built.close()
        again = self.build()
        report = self.drain(again, again.extraction)
        self.assertEqual((report.submitted, self.count("events")), (7, 7))

    def test_an_unreachable_service_is_survived_and_reported(self):
        def refuse(request, timeout):
            raise OSError("connection refused")
        client = HttpProcessingClient("http://127.0.0.1:9", PROCESS_TOKEN, open_url=refuse)
        service = ExtractionService(client, StoredCandidateExtractor(), self.clock, ExtractionConfig(idle_wait_s=0.01, error_wait_s=0.01, error_wait_cap_s=0.02), worker_id="remote")
        with self.assertRaises(ProcessingUnavailable) as caught:
            service.run_once()
        self.assertNotIn(PROCESS_TOKEN, str(caught.exception))
        service.start()
        deadline = time.time() + 5
        while time.time() < deadline and service.status()["errors"] < 2:
            time.sleep(0.02)
        self.assertGreaterEqual(service.status()["errors"], 2)
        self.assertEqual(service.status()["state"], "running")
        self.assertTrue(service.stop(5))


class EventApi(Fixture):
    def setUp(self):
        super().setUp()
        self.built = self.build()
        self.sd, self.austin = self.two_areas(self.built)
        self.discover(self.built)
        self.drain(self.built)
        self.server = app_module.build_api(self.built, ApiTokens(CONTROL_TOKEN, READ_TOKEN, PROCESS_TOKEN), clock=self.clock, port=0).start()
        self.addCleanup(self.server.stop)

    def call(self, method, path, token=READ_TOKEN, body=None):
        return request(self.server, method, path, token=token, body=body)

    def test_events_are_read_with_a_read_token_filtered_and_paged(self):
        status, page = self.call("GET", f"/v1/events?area={self.austin.area_key}&limit=2")
        self.assertEqual((status, page["schema"], len(page["events"]), bool(page["next_cursor"])), (200, "discovery.events.v1", 2, True))
        status, rest = self.call("GET", f"/v1/events?area={self.austin.area_key}&limit=2&after={page['next_cursor']}")
        self.assertEqual([e["title"] for e in page["events"] + rest["events"]], [f"Austin Fest {i}" for i in range(3)])
        self.assertIsNone(rest["next_cursor"])
        self.assertTrue(all(e["location_status"] in ("address", "venue", "description_only", "unknown") and "latitude" not in e for e in page["events"]), "no coordinates are invented")
        key = page["events"][0]["event_key"]
        status, detail = self.call("GET", f"/v1/events/{key}")
        self.assertEqual((status, detail["event"]["revision"], len(detail["history"]), "title" in detail["evidence"]), (200, 1, 1, True))
        starts = page["events"][1]["starts_at"]
        self.assertEqual(self.call("GET", f"/v1/events?from={starts}&area={self.austin.area_key}")[1]["events"][0]["starts_at"], starts)
        iso = time.strftime("%Y-%m-%dT%H:%M:%S%%2B00:00", time.gmtime(starts))  # the + of the offset must be percent-encoded in a query
        self.assertEqual(self.call("GET", f"/v1/events?to={iso}&area={self.austin.area_key}")[1]["events"][0]["title"], "Austin Fest 0")
        self.assertEqual(self.call("GET", "/v1/events?source=sandiego_gov")[1]["events"][0]["sources"], ["sandiego_gov"])

    def test_bad_event_requests_get_explicit_errors(self):
        for path, expected in (("/v1/events?area=nosucharea1", 404), ("/v1/events?after=abc", 400), ("/v1/events?limit=x", 400), ("/v1/events?sql=1", 400),
                               ("/v1/events?from=yesterday", 400), ("/v1/events?from=2030-01-01T00:00:00", 400), ("/v1/events/ev-nothing", 404)):
            self.assertEqual(self.call("GET", path)[0], expected, path)
        self.assertEqual(self.call("POST", "/v1/events", body={})[0], 405)
        self.assertEqual(self.call("GET", "/v1/events", token=None)[0], 401)

    def test_only_a_processing_token_can_submit_results_and_it_can_do_nothing_else(self):
        claim = {"worker": "w", "extractor": NAME, "version": VERSION, "limit": 1}
        for token in (READ_TOKEN, CONTROL_TOKEN):
            self.assertEqual(self.call("POST", "/v1/processing/claim", token=token, body=claim)[0], 403, "read and control tokens cannot process")
        status, claimed = self.call("POST", "/v1/processing/claim", token=PROCESS_TOKEN, body={**claim, "extractor": "other-extractor"})
        self.assertEqual(status, 200)
        item = claimed["items"][0]
        for path in ("/v1/areas", "/v1/events", "/v1/changes", "/v1/discovery/status"):
            self.assertEqual(self.call("GET", path, token=PROCESS_TOKEN)[0], 403, path)
        self.assertEqual(self.call("POST", "/v1/areas", token=PROCESS_TOKEN, body={})[0], 403)
        self.assertEqual(self.call("POST", "/v1/discovery/pause", token=PROCESS_TOKEN, body={})[0], 403)
        result = StoredCandidateExtractor().extract(item)
        submit = {"extractor": "other-extractor", "version": VERSION, "candidate_key": item["candidate_key"], "lease_token": item["lease_token"], "result": result}
        self.assertEqual(self.call("POST", "/v1/processing/submit", token=READ_TOKEN, body=submit)[0], 403)
        self.assertEqual(self.call("POST", "/v1/processing/submit", token=PROCESS_TOKEN, body={**submit, "lease_token": "guess"})[1]["error"]["code"], "lease_lost")
        self.assertEqual(self.call("POST", "/v1/processing/submit", token=PROCESS_TOKEN, body={**submit, "sql": "x"})[0], 400)
        self.assertEqual(self.call("POST", "/v1/processing/submit", token=PROCESS_TOKEN, body={**submit, "result": {"event": {"title": 1}}})[0], 400)
        big = {**submit, "result": {"event": result["event"], "fields": {}, "issues": [], "pad": "x" * 70000}}
        self.assertEqual(self.call("POST", "/v1/processing/submit", token=PROCESS_TOKEN, body=big)[0], 413)
        status, ack = self.call("POST", "/v1/processing/submit", token=PROCESS_TOKEN, body=submit)
        self.assertEqual((status, ack["outcome"]), (200, "unchanged"), "the same algorithm under another name gives the event already stored")
        self.assertEqual(self.call("GET", "/v1/processing/status?extractor=other-extractor", token=PROCESS_TOKEN)[0], 200)

    def test_a_remote_worker_over_http_produces_the_same_events(self):
        host, port = self.server.address[:2]
        client = HttpProcessingClient(f"http://{host}:{port}", PROCESS_TOKEN)
        self.assertEqual(client.status("remote-extractor", 1)["pending"], 7)
        # a different extractor name starts its own work: the remote worker processes every candidate again, and the events are unchanged
        remote_extractor = StoredCandidateExtractor()
        remote_extractor.name = "remote-extractor"
        remote = ExtractionService(client, remote_extractor, self.clock, worker_id="remote-1")
        report = self.drain(self.built, remote)
        self.assertEqual((report.submitted, sorted(set(report.outcomes))), (7, ["unchanged"]))
        self.assertEqual(client.status("remote-extractor", 1)["done"], 7)
        with self.assertRaises(ProcessingUnavailable) as caught:
            HttpProcessingClient(f"http://{host}:{port}", "w" * 40).status(NAME, VERSION)
        self.assertNotIn("w" * 40, str(caught.exception))


class Readers(Fixture):
    def test_twenty_clients_read_events_while_discovery_and_extraction_write(self):
        built = self.build(host_config=HostConfig(idle_wait_s=0.02, busy_wait_s=0.01, max_jobs_per_cycle=1), extraction_config=ExtractionConfig(idle_wait_s=0.02, busy_wait_s=0.01))
        sd, austin = self.two_areas(built)
        server = app_module.build_api(built, ApiTokens(CONTROL_TOKEN, READ_TOKEN, PROCESS_TOKEN), clock=self.clock, port=0).start()
        self.addCleanup(server.stop)
        built.host.start()
        built.extraction.start()
        deadline = time.time() + 15
        while time.time() < deadline and self.count("events") < 7:
            time.sleep(0.02)
        self.assertEqual(self.count("events"), 7)
        errors, latencies = [], []
        fetched = len(self.client.urls)

        def reader(i):
            for n in range(10):
                path = (f"/v1/events?area={sd.area_key}", f"/v1/events?area={austin.area_key}&limit=2", "/v1/events", "/v1/events?source=austin_fixture")[(i + n) % 4]
                started = time.perf_counter()
                try:
                    status, payload = request(server, "GET", path, token=READ_TOKEN)
                    titles = [e["title"] for e in payload["events"]] if status == 200 else []
                    if status != 200 or ("area=" + sd.area_key in path and any(t.startswith("Austin") for t in titles)) or \
                            ("area=" + austin.area_key in path and any(t.startswith("Event") for t in titles)):
                        errors.append((i, path, status, titles))
                except Exception as error:  # pragma: no cover
                    errors.append((i, path, repr(error)))
                latencies.append(time.perf_counter() - started)

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for round_ in range(3):  # discovery keeps writing meanwhile: the source's times change and the sources are made due
            self.pages[CITY_URL] = self.pages[CITY_URL].replace("10:00 AM - 2:00 PM" if round_ == 0 else f"{round_ + 5}:00 PM - 9:00 PM", f"{round_ + 6}:00 PM - 9:00 PM")
            built.service.make_due("sandiego_gov")
            time.sleep(0.15)
        for t in threads:
            t.join()
        deadline = time.time() + 15
        while time.time() < deadline and self.count("event_revisions", "revision > 1") < 4:
            time.sleep(0.05)
        self.assertTrue(built.extraction.stop(10))
        self.assertTrue(built.host.stop(10))
        self.assertEqual(errors, [])
        self.assertGreaterEqual(self.count("event_revisions", "revision > 1"), 4, "updates flowed while the readers read")
        self.assertEqual(self.count("events"), 7)
        values = sorted(latencies)
        summary = {"clients": 20, "requests": len(values), "p50_ms": round(values[len(values) // 2] * 1000, 1), "p95_ms": round(values[int(len(values) * 0.95)] * 1000, 1),
                   "max_ms": round(values[-1] * 1000, 1), "writes": built.reader.status().write_stats}
        print("\nevent readers (simulated pages, development machine):", json.dumps(summary))
        self.assertEqual(summary["writes"]["failures"], 0)
        self.assertGreater(len(self.client.urls), fetched, "the crawls came from the made-due jobs")

    def test_reading_events_causes_no_crawl_and_no_extraction(self):
        built = self.build()
        self.two_areas(built)
        self.discover(built)
        self.drain(built)
        before = (len(self.client.urls), self.count("changes"), self.count("extractions"), self.count("processing_work"), built.extraction.status()["cycles"])
        for _ in range(100):
            self.all_events(built)
            built.events.get(self.all_events(built)[0].event_key)
        self.assertEqual((len(self.client.urls), self.count("changes"), self.count("extractions"), self.count("processing_work"), built.extraction.status()["cycles"]), before)


class Storage(Fixture):
    def test_a_database_from_milestone_2b_is_migrated_and_its_candidates_become_events(self):
        old = DiscoveryStore(self.db, migrations=MIGRATIONS[:2]).open()
        old.close()
        self.assertEqual(self.check_version(), 2)
        built = self.build()
        self.assertEqual(database(built).version(), LATEST)
        self.add_candidate(built, "sandiego_gov", "s:1", candidate_payload())
        self.assertEqual(self.drain(built).submitted, 1)
        self.assertEqual(self.count("events"), 1)

    def check_version(self):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()

    def test_old_events_are_pruned_with_their_history(self):
        built = self.build()
        self.add_candidate(built, "sandiego_gov", "s:1", candidate_payload(starts_at="2030-05-01T18:00:00-07:00"))
        self.drain(built)
        self.clock.t = 1893456000 + 90 * 86400  # 2030-01-01 + 90 days: still before the event
        self.assertEqual(built.service.prune()["candidates"], 0)
        self.assertEqual(self.count("events"), 1)
        self.clock.t = 1893456000 + 300 * 86400
        built.service.prune()
        self.assertEqual((self.count("events"), self.count("event_revisions"), self.count("event_candidates"), self.count("extractions")), (0, 0, 0, 0))

    def test_foreign_keys_and_constraints_protect_the_event_tables(self):
        built = self.build()
        with self.assertRaises(sqlite3.IntegrityError):
            with database(built).write() as tx:
                tx.execute("INSERT INTO event_candidates (candidate_id, event_id, role, linked_at) VALUES (999, 999, 'primary', 1)")
        with self.assertRaises(sqlite3.IntegrityError):
            with database(built).write() as tx:
                tx.execute("INSERT INTO events (event_key, title, starts_at, location_status, status, first_extracted_at, updated_at) VALUES ('ev-12345678', 't', 1, 'precise', 'complete', 1, 1)")


class Boundaries(unittest.TestCase):
    """Components reach the database only through the database-owning service's contracts."""

    def imports_of(self, name):
        paths = {
            "service.py": "discovery/service.py", "scheduler.py": "discovery/scheduler.py",
            "host.py": "discovery/host.py", "control.py": "control/service.py",
            "extraction.py": "extraction/service.py", "events.py": "api/events.py",
        }
        name = paths.get(name, name)
        with open(os.path.join(SERVICE_DIR, name), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                found.add(("." * node.level) + (node.module or ""))
        return found

    def test_the_extraction_worker_has_no_database_access(self):
        imports = self.imports_of("extraction.py")
        self.assertFalse(imports & {"sqlite3", ".persistence", ".persistence.store", ".store", ".repositories", ".events", ".schema", ".app"}, imports)
        with open(os.path.join(SERVICE_DIR, "extraction", "service.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("sqlite", source.lower())

    def test_the_event_query_service_only_reads(self):
        with open(os.path.join(SERVICE_DIR, "api", "events.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        service = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EventQueryService"][0]
        calls = {n.func.attr for n in ast.walk(service) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse(calls & {"read", "write", "append", "apply", "execute"}, "the query service holds no SQL and no transaction: it asks the EventQueries port")
        self.assertFalse(self.imports_of("events.py") & {".processing", ".extraction", ".service", ".scheduler", ".host", "sqlite3"})

    def test_discovery_does_not_import_processing_or_extraction(self):
        for name in ("service.py", "scheduler.py", "host.py", "persistence/reading.py", "control.py"):
            self.assertFalse(self.imports_of(name) & {".processing", ".extraction", ".events", "..processing", "..extraction", "..events"}, name)


if __name__ == "__main__":
    unittest.main()
