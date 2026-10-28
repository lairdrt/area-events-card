"""Generic SourcesProducer contract, guardrails, persistence and UI configuration."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

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


# Representative text and hierarchy from https://mtrp.org/marketplace/
# As checked 2026-10-26; offline fixture deliberately avoids website/network flakiness.
MTRP_MARKETPLACE = "https://mtrp.org/marketplace/"
MTRP_HTML = """
<main><h1>Holiday Marketplace at Mission Trails Regional Park</h1>
<p>Shop local this holiday season at Mission Trails Regional Park!</p>
<h2>2026 HOLIDAY MARKETPLACE</h2>
<p>Dates: Saturday, November 14 and Sunday, November 15, 2026</p>
<p>Time: 9 a.m.–3 p.m. both days</p>
<p>Location: Mission Trails Regional Park Visitor Center<br>
One Father Junipero Serra Trail<br>San Diego, CA 92119</p>
<p>The event is free but please register in advance.</p>
<h2>2026 VENDORS</h2>
<ul><li>Bradbury-Bennett</li><li>Candlefy</li><li>Chrysanthemum Art</li></ul>
<h2>PARKING + SHUTTLE</h2>
<p>Parking will be available near the visitor center.</p>
<footer><p>Visitor Center<br>Daily Hours: 9 AM – 5 PM</p></footer>
</main>
"""
MULTI_EVENT_HTML = """
<main><h1>Upcoming Events</h1>
<article><h2>Harvest Craft Fair</h2><p>Saturday, November 14, 2026</p>
<p>10 am to 2 pm</p><p>Location: Town Hall</p></article>
<article><h2>Moonlight Lecture</h2><p>Friday, November 20, 2026</p>
<p>6:30pm – 8pm</p><p>Location: Science Museum</p></article>
<h2>Hours</h2><p>Visitor Center open daily 9am to 5pm.</p></main>
"""
LISTING_HTML = """<main><h1>Events</h1><ul>
<li>Open Studio Art Fair — November 18, 2026 at 11 am–4 pm</li>
<li>Community Concert — December 2, 2026 at 7pm–9pm</li>
<li>Visitor Center open 9am–5pm daily</li></ul></main>"""


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
            {"urls": [SEED, OTHER], "max_pages": 1},
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




    def test_jsonld_event_image_takes_precedence_over_html_fallback(self):
        doc = {**DOC, "image": "/images/autumn-fair-jsonld.jpg"}
        page = ("<main><img src='/uploads/autumn-fair-1024x341.jpg' alt='Autumn Fair'></main>"
                '<script type="application/ld+json">' + json.dumps(doc) + "</script>")
        result, _ = run_crawl({SEED: page}, max_depth=0)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].image_url,
                         "https://example.org/images/autumn-fair-jsonld.jpg")
        self.assertEqual(result.candidates[0].image_source, "source JSON-LD image")












    def test_direct_jsonld_document_as_seed(self):
        result, client = run_crawl({SEED: json.dumps({"@graph": [DOC]})}, max_depth=0)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].title, "Autumn Fair")
        self.assertEqual(client.requests, [SEED])

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
        result, client = run_crawl({SEED: HTML, OTHER: HTML}, urls=(SEED, OTHER), max_pages=2)
        self.assertEqual(client.requests, [SEED, OTHER])
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

    def test_mtrp_marketplace_actual_webpage_copy_two_events_without_structured_data(self):
        result, client = run_crawl(
            {MTRP_MARKETPLACE: MTRP_HTML}, urls=(MTRP_MARKETPLACE,),
            timezone="America/Los_Angeles", max_depth=0
        )
        self.assertEqual(client.requests, [MTRP_MARKETPLACE])
        self.assertEqual(len(result.candidates), 2, result.candidates)
        self.assertEqual([e.title for e in result.candidates], ["Holiday Marketplace"] * 2)
        self.assertEqual(
            [e.starts_at for e in result.candidates],
            ["2026-11-14T09:00:00-08:00", "2026-11-15T09:00:00-08:00"]
        )
        self.assertEqual(
            [e.ends_at for e in result.candidates],
            ["2026-11-14T15:00:00-08:00", "2026-11-15T15:00:00-08:00"]
        )
        self.assertTrue(all(e.venue == "Mission Trails Regional Park Visitor Center" for e in result.candidates))
        self.assertTrue(all("San Diego, CA 92119" in (e.address or "") for e in result.candidates))
        self.assertEqual(result.notes["unstructured_candidates"], 2)
        self.assertEqual(len(producer.accept(result).candidates), 2)

    @unittest.skipUnless(os.getenv("AREA_EVENTS_LIVE_MTRP") == "1",
                         "live MTRP site smoke test requires explicit network opt-in")
    def test_mtrp_marketplace_live_public_page(self):
        """An opt-in real-site acceptance test; default CI uses the stable offline fixture."""
        from area_events.discovery.safe import SafeFetcher, HostPolicy
        actual = SafeFetcher(request_interval=2.0, state=HostPolicy())
        source = SourcesProducer()
        base, options = source.validate_options({
            "urls": [MTRP_MARKETPLACE], "timezone": "America/Los_Angeles",
            "max_pages": 1, "max_depth": 0,
        })
        observed = source.crawl(
            RecordingFetcher(actual, 4, 120), (),
            {**options, "url": base, "source_key": "mtrp", "name": "Mission Trails"}
        )
        self.assertEqual(
            {e.starts_at for e in producer.accept(observed).candidates},
            {"2026-11-14T09:00:00-08:00", "2026-11-15T09:00:00-08:00"}
        )
        self.assertTrue(all(e.venue == "Mission Trails Regional Park Visitor Center"
                            for e in observed.candidates))

    def test_unstructured_multiple_independent_event_headings(self):
        result, _ = run_crawl({SEED: MULTI_EVENT_HTML},
                              timezone="America/Los_Angeles", max_depth=0)
        self.assertEqual(
            [(e.title, e.starts_at) for e in result.candidates],
            [("Harvest Craft Fair", "2026-11-14T10:00:00-08:00"),
             ("Moonlight Lecture", "2026-11-20T18:30:00-08:00")]
        )

    def test_unstructured_list_items_and_no_visitor_hours_false_event(self):
        result, _ = run_crawl({SEED: LISTING_HTML},
                              timezone="America/Los_Angeles", max_depth=0)
        self.assertEqual({e.title for e in result.candidates},
                         {"Open Studio Art Fair", "Community Concert"})
        self.assertEqual(len(result.candidates), 2)

    def test_ordinary_narrative_paragraph_needs_no_special_site_template(self):
        page = """<h1>Evening Astronomy</h1>
<p>Join us November 22, 2026 from 6pm to 8pm at the observatory.</p>"""
        result, _ = run_crawl({SEED: page}, timezone="America/Los_Angeles")
        self.assertEqual([(v.title, v.starts_at, v.ends_at) for v in result.candidates],
                         [("Evening Astronomy", "2026-11-22T18:00:00-08:00",
                           "2026-11-22T20:00:00-08:00")])

    def test_generic_html_event_table_rows_are_separate_events(self):
        page = """<h1>Event Calendar</h1>
<table><tr><th>Event</th><th>Date</th><th>Time</th></tr>
<tr><td>Garden Workshop</td><td>November 22, 2026</td><td>10am–12pm</td></tr>
<tr><td>Star Party</td><td>November 25, 2026</td><td>7pm–9pm</td></tr></table>"""
        result, _ = run_crawl({SEED: page}, timezone="America/Los_Angeles")
        self.assertEqual({v.title for v in result.candidates}, {"Garden Workshop", "Star Party"})

    def test_publication_date_and_business_hours_do_not_fabricate_events(self):
        page = """<h1>Visitor Center News</h1><p>Published November 22, 2026</p>
<p>Staff meeting at 6pm, Visitor Center open daily 9am to 5pm.</p>"""
        result, _ = run_crawl({SEED: page}, timezone="America/Los_Angeles")
        self.assertEqual(result.candidates, ())

    def test_text_events_need_explicit_configured_timezone_and_event_time(self):
        no_zone, _ = run_crawl({MTRP_MARKETPLACE: MTRP_HTML},
                               urls=(MTRP_MARKETPLACE,), max_depth=0)
        self.assertEqual(no_zone.candidates, ())
        date_only = "<h1>Farmers Market</h1><p>Dates: November 14, 2026</p><p>Location: Village Green</p>"
        no_time, _ = run_crawl({SEED: date_only}, timezone="America/Los_Angeles")
        self.assertEqual(no_time.candidates, ())

    def test_local_text_occurrence_ids_remain_stable_across_scans(self):
        a, _ = run_crawl({MTRP_MARKETPLACE: MTRP_HTML},
                          urls=(MTRP_MARKETPLACE,), timezone="America/Los_Angeles")
        b, _ = run_crawl({MTRP_MARKETPLACE: MTRP_HTML},
                          urls=(MTRP_MARKETPLACE,), timezone="America/Los_Angeles")
        self.assertEqual([c.local_id for c in a.candidates],
                         [c.local_id for c in b.candidates])

    def test_bad_timezone_option_is_rejected(self):
        with self.assertRaises(ValueError):
            SourcesProducer.validate_options({"urls": [SEED], "timezone": "Somewhere/Nowhere"})

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
