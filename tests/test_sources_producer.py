"""Generic SourcesProducer contract, guardrails, persistence and UI configuration."""
import json
import os
import tempfile
import unittest

from area_events import app as app_module
from area_events.control.service import CreateAreaRequest, CreateSourceRequest
from area_events.contracts.models import ValidationFailed
from area_events.discovery import producer
from area_events.discovery.fetching import RecordingFetcher
from area_events.discovery.producers.sources_producer import SourcesProducer, _timestamp
from area_events.discovery.safe import FetchError, Response
from tests.test_discovery_service import Clock

SEED = "https://example.org/events"
DETAIL = "https://example.org/events/autumn-fair"
OTHER = "https://another.org/calendar"
ICAL = "https://example.org/events.ics"
DOC = {
    "@context": "https://schema.org", "@type": "Event", "@id": "autumn-fair-123",
    "name": "Autumn Fair", "startDate": "2026-11-05T11:30:00-08:00",
    "endDate": "2026-11-05T13:30:00-08:00",
    "url": DETAIL, "location": {"@type": "Place", "name": "Community Hall",
                                 "address": {"streetAddress": "1 Main St", "addressLocality": "San Diego",
                                             "addressRegion": "CA"}},
}
HTML = f"""<!DOCTYPE html><html><head><script type="application/ld+json">{json.dumps(DOC)}</script></head>
<body><h1>Autumn Fair</h1><a href="/events/autumn-fair">Autumn Fair</a>
<a href="https://remote.example/other-events">Another site's events</a></body></html>"""
ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:autumn-123
SUMMARY:Community Lecture
DTSTART;TZID=America/Los_Angeles:20261106T100000
DTEND;TZID=America/Los_Angeles:20261106T120000
LOCATION:Library
END:VEVENT
BEGIN:VEVENT
UID:all-day
SUMMARY:All-day Holiday
DTSTART;VALUE=DATE:20261107
END:VEVENT
END:VCALENDAR
"""


class FakeClient:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def fetch(self, url, **_):
        self.requests.append(url)
        if url not in self.pages:
            raise FetchError(f"missing {url}")
        return Response(url, 200, "text/calendar" if url.endswith(".ics") else "text/html; charset=utf-8", self.pages[url])


def run_crawl(pages, urls=(SEED,), **kwargs):
    adapter = SourcesProducer()
    options = {"urls": list(urls), **kwargs}
    base, clean = adapter.validate_options(options)
    client = FakeClient(pages)
    result = adapter.crawl(RecordingFetcher(client, 15, 30), (), {**clean, "url": base,
                                                                     "source_key": "site_a", "name": "Websites"})
    return result, client


class GenericProducer(unittest.TestCase):
    def test_validates_websites_and_bounded_options(self):
        adapter = SourcesProducer()
        base, opts = adapter.validate_options({"urls": [SEED, SEED + "#fragment", OTHER]})
        self.assertEqual(base, SEED)
        self.assertEqual(opts["urls"], [SEED, OTHER])
        for bad in (
            {"urls": []}, {"urls": "https://example.org"},
            {"urls": ["http://127.0.0.1:8780/private"]},
            {"urls": ["https://me:password@example.org/events"]},
            {"urls": ["file:///etc/passwd"]},
            {"urls": [SEED], "max_pages": 0},
            {"urls": [SEED], "max_depth": 10},
            {"urls": [SEED], "extra": "whatever"},
            {"urls": [SEED, OTHER], "url": OTHER},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                adapter.validate_options(bad)

    def test_jsonld_single_page_yields_accepted_standard_candidate_with_evidence(self):
        result, client = run_crawl({SEED: HTML}, max_depth=0)
        self.assertEqual(client.requests, [SEED])
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].title, "Autumn Fair")
        self.assertEqual(result.candidates[0].starts_at, "2026-11-05T11:30:00-08:00")
        self.assertEqual(result.candidates[0].address, "1 Main St, San Diego, CA")
        self.assertFalse(result.candidates[0].verification.verified)
        self.assertEqual(result.candidates[0].provenance.found_on, (SEED,))
        self.assertEqual(len(producer.accept(result).candidates), 1)

    def test_follows_only_bounded_same_host_event_links(self):
        landing = """<a href="/events/autumn-fair">Autumn Fair</a>
<a href="https://remote.example/other-events">External event</a>
<a href="/privacy">Privacy</a>"""
        detail = '<script type="application/ld+json">' + json.dumps(DOC) + '</script>'
        result, client = run_crawl({SEED: landing, DETAIL: detail}, max_depth=1)
        self.assertEqual(client.requests, [SEED, DETAIL])
        self.assertEqual([c.title for c in result.candidates], ["Autumn Fair"])
        self.assertEqual(len(result.leads), 2)
        shallow, client = run_crawl({SEED: landing, DETAIL: detail}, max_depth=0)
        self.assertEqual(shallow.candidates, ())
        self.assertEqual(client.requests, [SEED])

    def test_multiple_seed_urls_respect_request_budget_and_make_no_cross_domain_links(self):
        result, client = run_crawl({SEED: HTML, OTHER: HTML}, urls=(SEED, OTHER), max_pages=1)
        self.assertEqual(client.requests, [SEED])
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.notes["sites"], 2)

    def test_ical_parses_tzid_and_skips_all_day_events(self):
        result, client = run_crawl({ICAL: ICS}, urls=(ICAL,))
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].title, "Community Lecture")
        self.assertEqual(result.candidates[0].starts_at, "2026-11-06T10:00:00-08:00")
        self.assertEqual(result.notes["invalid_or_ambiguous_event_dates"], 1)
        self.assertEqual(len(producer.accept(result).candidates), 1)

    def test_repeated_ical_uid_with_distinct_instances_keeps_both(self):
        calendar = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:same-event
SUMMARY:Weekly Talk
DTSTART:20261106T100000Z
END:VEVENT
BEGIN:VEVENT
UID:same-event
SUMMARY:Weekly Talk
DTSTART:20261113T100000Z
END:VEVENT
END:VCALENDAR"""
        result, _ = run_crawl({ICAL: calendar}, urls=(ICAL,))
        self.assertEqual(len(result.candidates), 2)
        self.assertNotEqual(result.candidates[0].local_id, result.candidates[1].local_id)

    def test_never_guesses_dates_offsets_or_publication_dates(self):
        page = '<script type="application/ld+json">' + json.dumps({
            "@type": "Event", "name": "Ambiguous", "startDate": "2026-11-05T10:00:00"
        }) + '</script>'
        result, _ = run_crawl({SEED: page})
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.notes["invalid_or_ambiguous_event_dates"], 1)
        self.assertIsNone(_timestamp("2026-11-05"))
        self.assertIsNone(_timestamp("2026-11-01T01:30:00", "America/Los_Angeles"))

    def test_all_inaccessible_pages_report_failure_without_faking_success(self):
        with self.assertRaises(FetchError):
            run_crawl({})

    def test_persistence_and_scheduler_use_single_existing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient({SEED: HTML})
            app = app_module.build(os.path.join(tmp, "discovery.db"), clock=Clock(),
                                   fetch_client=client, request_interval=0)
            try:
                app.register_known_sources()
                config = app.control.create_source(CreateSourceRequest(
                    "generic_sites", "sources_producer", "Site Events", True, 60,
                    {"urls": [SEED], "max_depth": 0}))
                self.assertEqual(config.source.adapter, "sources_producer")
                self.assertEqual(config.source.options_dict()["urls"], [SEED])
                app.control.create_area(CreateAreaRequest(
                    "Example City", 32.71, -117.16, 20, sources=("generic_sites",)))
                app.scheduler.plan()
                reports = app.service.run_until_idle(3)
                self.assertTrue(any(r.outcome == "completed" for r in reports), reports)
                with app.persistence._store.read() as db:
                    self.assertEqual(db.execute(
                        "SELECT COUNT(*) FROM candidates c JOIN sources s USING(source_id) "
                        "WHERE s.source_key='generic_sites'").fetchone()[0], 1)
                    self.assertEqual(db.execute(
                        "SELECT COUNT(*) FROM source_leads sl JOIN sources s USING(source_id) "
                        "WHERE s.source_key='generic_sites'").fetchone()[0], 1)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
