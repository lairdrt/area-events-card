"""The generic website producer's two-stage workflow: listing pages supply the occurrences and the links, the linked event pages supply the authoritative facts.

The listing and the detail page are real copies of batesnutfarm.biz (tests/data); further detail pages are built in the same label layout. Nothing here is specific to that
site: the producer reads a listing card's own link and a page's labelled fields (Date, Time, Location, Address, Description).
"""
import json
import unittest
from pathlib import Path

from area_events.discovery.fetching import RecordingFetcher
from area_events.discovery.producers.sources_producer import SourcesProducer
from area_events.discovery.safe import FetchError
from area_events.image_extractor.goose3_adapter import Goose3ImageExtractor
from tests.test_image_extractor import ImageClient, photo
from tests.test_sources_producer import run_crawl

DATA = Path(__file__).parent / "data"
LISTING = (DATA / "bates_events_excerpt.html").read_text(encoding="utf-8")
DETAIL = (DATA / "bates_detail_east_county_cruisers.html").read_text(encoding="utf-8")
SITE = "https://batesnutfarm.biz"
PAGE = SITE + "/pages/events"
CRUISERS = SITE + "/blogs/events/east-county-cruisers-car-show-and-toy-drive"
XMAS = SITE + "/blogs/events/christmas-on-the-farm-arts-n-crafts-fair"
IMAGE = "https://batesnutfarm.biz/cdn/shop/articles/1_f5e5e225-9c55-4c23-aeb8-ab3f35d362a9.jpg?v=1783018822"
LA = "America/Los_Angeles"


def detail_page(title, date, time, location="Bates Nut Farm", address="15954 Woods Valley Road, Valley Center, CA", description="Come and see everything we have planned for the day."):
    """A detail page in the same labelled layout as the real one (the real Christmas on the Farm page could not be fetched: the site rate-limits)."""
    fields = [f"<p><strong>Event: {title}</strong></p>", f"<p><strong><span>Date: </span>{date}</strong></p>", f"<p><strong><span>Time: </span>{time}</strong></p>"]
    if location:
        fields.append(f"<p><strong><span>Location: </span>{location}</strong></p>")
    if address:
        fields.append(f"<p><strong><span>Address: </span>{address}</strong></p>")
    fields.append(f"<p><strong><span>Description: </span></strong>{description}</p>")
    return f"<html><head><title>{title} | Bates Nut Farm</title></head><body><article><h1>{title}</h1>{''.join(fields)}</article></body></html>"


class SiteClient(ImageClient):
    """Pages as text, images as bytes; a page the site does not have is a fetch failure, as with the real fetcher."""

    def fetch(self, url, binary=None, **_):
        if not binary and url not in self.pages:
            self.requests.append(url)
            raise FetchError(f"missing {url}")
        return super().fetch(url, binary=binary)


def crawl(pages, urls=(PAGE,), images=None, engine=None, name="Local Craft Fairs", previous=(), **options):
    producer = SourcesProducer(image_extractor=engine)
    base, clean = producer.validate_options({"urls": list(urls), "timezone": LA, **options})
    client = SiteClient(pages, images)
    result = producer.crawl(RecordingFetcher(client, 15, 60), previous, {**clean, "url": base, "source_key": "mission_trails", "name": name})
    return result, client


def by_title(result, title):
    return [c for c in result.candidates if c.title == title]


class DetailPages(unittest.TestCase):
    def test_the_real_detail_page_is_merged_into_its_listing_candidate(self):
        listing_only, _ = crawl({PAGE: LISTING}, max_depth=0)
        engine = Goose3ImageExtractor() if Goose3ImageExtractor.available() else None
        result, client = crawl({PAGE: LISTING, CRUISERS: DETAIL}, images={IMAGE: photo(480, 480), IMAGE.replace("https:", "http:"): photo(480, 480)}, engine=engine, max_pages=14, max_depth=1)
        before = by_title(listing_only, "East County Cruisers Car Show and Toy Drive")[0]
        merged, = by_title(result, "East County Cruisers Car Show and Toy Drive")
        self.assertEqual(merged.local_id, before.local_id, "the candidate identity is the listing's")
        self.assertEqual((before.venue, before.address, before.description, before.image_url, before.url), (None, None, None, None, PAGE), "the listing alone knows none of these")
        self.assertEqual(merged.starts_at, "2026-11-07T09:00:00-08:00")
        self.assertEqual(merged.ends_at, "2026-11-07T14:00:00-08:00")
        self.assertEqual(merged.venue, "Bates Nut Farm")
        self.assertEqual(merged.address, "15954 Woods Valley Road, Valley Center, CA")
        self.assertIn("classic cars on display", merged.description)
        self.assertEqual(merged.url, CRUISERS)
        self.assertEqual((merged.provenance.listing_url, merged.provenance.source_name), (PAGE, "Local Craft Fairs"))
        self.assertEqual(merged.provenance.found_on, (PAGE, CRUISERS))
        self.assertEqual(merged.verification.status, "corroborated")
        self.assertTrue(any("take precedence over the listing" in line for line in merged.verification.evidence))
        if engine is not None:
            self.assertEqual(merged.image_url.split(":", 1)[1], IMAGE.split(":", 1)[1], "the page photograph (the page offers it as http and https)")
        self.assertEqual(result.notes["event_pages_merged"], 1)

    def test_every_other_candidate_and_every_identity_is_unchanged(self):
        listing_only, _ = crawl({PAGE: LISTING}, max_depth=0)
        result, _ = crawl({PAGE: LISTING, CRUISERS: DETAIL}, max_pages=14, max_depth=1)
        self.assertEqual(len(result.candidates), len(listing_only.candidates), "no listing/detail duplicates")
        self.assertEqual({c.local_id for c in result.candidates}, {c.local_id for c in listing_only.candidates})
        for c in listing_only.candidates:
            if c.title != "East County Cruisers Car Show and Toy Drive":
                self.assertIn(c, result.candidates, c.title)
        fair = [c for c in result.candidates if c.title.startswith("Christmas on the Farm")]
        self.assertEqual({c.starts_at for c in fair}, {"2026-11-07T09:00:00-08:00", "2026-11-08T09:00:00-08:00"}, "an unrelated event at the same start is untouched")
        self.assertTrue(all(c.venue is None and c.url == PAGE for c in fair))

    def test_unavailable_detail_pages_leave_the_listing_facts_standing(self):
        result, client = crawl({PAGE: LISTING}, max_pages=14, max_depth=1)
        listing_only, _ = crawl({PAGE: LISTING}, max_depth=0)
        self.assertEqual(result.candidates, listing_only.candidates)
        self.assertEqual(result.notes["event_pages_followed"], 10)
        self.assertEqual(result.notes["event_pages_failed"], 10)
        self.assertEqual(result.notes["pages"], 1)

    def test_the_venue_is_never_taken_from_the_producers_display_name(self):
        result, _ = crawl({PAGE: LISTING}, name="Bates Nut Farm", max_depth=0)
        self.assertTrue(result.candidates)
        self.assertTrue(all(c.venue is None and c.address is None for c in result.candidates))
        self.assertTrue(all(c.provenance.source_name == "Bates Nut Farm" for c in result.candidates), "the name is attribution only")

    def test_multiple_occurrences_each_take_their_own_date_from_the_event_page(self):
        both = detail_page("Christmas on the Farm Arts & Crafts Fair", "Saturday, November 7, 2026 to Sunday, November 8, 2026", "9:00am to 4:00pm",
                           description="Handmade gifts from more than a hundred vendors, food and music.")
        result, _ = crawl({PAGE: LISTING, XMAS: both}, max_pages=14, max_depth=1)
        fair = sorted(by_title(result, "Christmas on the Farm Arts & Crafts Fair"), key=lambda c: c.starts_at)
        self.assertEqual([c.starts_at for c in fair], ["2026-11-07T09:00:00-08:00", "2026-11-08T09:00:00-08:00"])
        self.assertEqual([c.ends_at for c in fair], ["2026-11-07T16:00:00-08:00", "2026-11-08T16:00:00-08:00"])
        self.assertTrue(all(c.url == XMAS and c.venue == "Bates Nut Farm" and "Handmade gifts" in c.description for c in fair))
        self.assertEqual({c.local_id[:8] for c in fair}, {"566d43f9", "5bd4fd24"}, "the ids production holds")
        self.assertEqual(len(result.candidates), 13)

    def test_a_detail_page_that_states_one_of_two_occurrences_leaves_the_other_to_the_listing(self):
        only_first = detail_page("Christmas on the Farm Arts & Crafts Fair", "Saturday, November 7, 2026", "9:00am to 4:00pm")
        result, _ = crawl({PAGE: LISTING, XMAS: only_first}, max_pages=14, max_depth=1)
        first, second = sorted(by_title(result, "Christmas on the Farm Arts & Crafts Fair"), key=lambda c: c.starts_at)
        self.assertEqual((first.url, first.venue), (XMAS, "Bates Nut Farm"))
        self.assertEqual((second.url, second.venue), (PAGE, None))

    def test_a_corrected_time_on_the_event_page_replaces_the_listing_time_but_not_the_identity(self):
        listing_only, _ = crawl({PAGE: LISTING}, max_depth=0)
        moved = detail_page("East County Cruisers Car Show and Toy Drive", "Saturday, November 7, 2026", "10:00am to 3:00pm")
        result, _ = crawl({PAGE: LISTING, CRUISERS: moved}, max_pages=14, max_depth=1)
        cruisers, = by_title(result, "East County Cruisers Car Show and Toy Drive")
        self.assertEqual((cruisers.starts_at, cruisers.ends_at), ("2026-11-07T10:00:00-08:00", "2026-11-07T15:00:00-08:00"))
        self.assertEqual(cruisers.local_id, by_title(listing_only, "East County Cruisers Car Show and Toy Drive")[0].local_id)
        self.assertEqual(cruisers.verification.status, "unverified", "the listing and the event page disagree on the start")
        self.assertEqual(len(result.candidates), 13)

    def test_a_new_occurrence_only_the_event_page_lists_becomes_its_own_candidate(self):
        extra = detail_page("East County Cruisers Car Show and Toy Drive", "Saturday, November 14, 2026", "9:00am to 2:00pm")
        result, _ = crawl({PAGE: LISTING, CRUISERS: extra}, max_pages=14, max_depth=1)
        self.assertEqual(len(by_title(result, "East County Cruisers Car Show and Toy Drive")), 2)
        self.assertEqual(len(result.candidates), 14)

    def test_two_independent_websites_never_merge_with_each_other(self):
        def listing(host, link):
            return (f'<ul><li class="event-item"><a href="{link}"><h2>Farmers Market</h2><p>Date: Sat., Nov. 7, 2026 to Sat., Nov. 7, 2026</p><p>Time: 9:00am to 1:00pm</p></a></li></ul>')

        def detail(address):
            return detail_page("Farmers Market", "Saturday, November 7, 2026", "9:00am to 1:00pm", location="Town Square", address=address)

        a, b = "https://alpha.example/events", "https://beta.example/events"
        pages = {a: listing("alpha", "/events/market"), "https://alpha.example/events/market": detail("1 Alpha Way, Alpha, CA"),
                 b: listing("beta", "/events/market"), "https://beta.example/events/market": detail("2 Beta Way, Beta, CA")}
        result, _ = crawl(pages, urls=(a, b), max_pages=6, max_depth=1)
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual({c.address for c in result.candidates}, {"1 Alpha Way, Alpha, CA", "2 Beta Way, Beta, CA"})
        self.assertEqual({c.url for c in result.candidates}, {"https://alpha.example/events/market", "https://beta.example/events/market"})

    def test_page_budget_exhaustion_is_visible_and_the_listing_still_yields_its_events(self):
        result, _ = crawl({PAGE: LISTING}, max_pages=3, max_depth=1)
        self.assertEqual(len(result.candidates), 13)
        self.assertTrue(result.notes["page_budget_exhausted"])
        self.assertEqual((result.notes["page_budget"], result.notes["pages_attempted"]), (3, 3))
        self.assertGreater(result.notes["pages_unvisited"], 0)
        roomy, _ = crawl({PAGE: LISTING}, max_pages=24, max_depth=1)
        self.assertFalse(roomy.notes["page_budget_exhausted"])

    def test_a_budget_that_leaves_no_room_to_follow_links_is_explained(self):
        urls = ("https://mtrp.org/marketplace/", PAGE)
        self.assertEqual(SourcesProducer.configuration_warnings({"urls": urls, "max_pages": 2, "max_depth": 2}) and "no linked event page can be fetched" in
                         SourcesProducer.configuration_warnings({"urls": urls, "max_pages": 2, "max_depth": 2})[0], True)
        self.assertEqual(SourcesProducer.configuration_warnings({"urls": urls, "max_pages": 2, "max_depth": 0}), ())
        self.assertEqual(SourcesProducer.configuration_warnings({"urls": urls, "max_pages": 6, "max_depth": 1}), ())
        result, _ = crawl({PAGE: LISTING, "https://mtrp.org/marketplace/": "<p>none</p>"}, urls=urls, max_pages=2, max_depth=2)
        self.assertIn("max_pages (2)", result.notes["configuration_warning"])
        with self.assertRaisesRegex(ValueError, r"page budget for the whole scan, starting URLs included"):
            SourcesProducer.validate_options({"urls": list(urls), "max_pages": 1})
        labels = {f["name"]: f["label"] for f in SourcesProducer.configuration_fields}
        self.assertIn("starting URLs AND every page followed", labels["max_pages"])
        self.assertIn("0 = only the starting URLs", labels["max_depth"])

    def test_the_labelled_fields_of_a_single_page_are_read_without_their_labels(self):
        result, _ = run_crawl({PAGE: DETAIL}, urls=(PAGE,), timezone=LA, max_depth=0)
        event, = result.candidates
        self.assertEqual((event.venue, event.address), ("Bates Nut Farm", "15954 Woods Valley Road, Valley Center, CA"))
        self.assertTrue(event.description.startswith("The Annual East County Cruisers Car Show & Toy Drive"))


def site(host, count, venue, missing=(), generic=False):
    """A website: its listing page with `count` event cards (each linking to its own page) and those pages, except the ones in `missing`."""
    cards = "".join(f'<li class="event-item"><a href="/e/{n}"><h2>{host} Event {n}</h2><p>Date: Sat., Nov. {n}, 2026</p><p>Time: 9:00am to 1:00pm</p></a></li>' for n in range(1, count + 1))
    nav = '<nav></nav><p><a href="/events/calendar">Event calendar</a> <a href="/events/archive">Event archive</a></p>' if generic else ""
    pages = {f"https://{host}/events": f"<html><body>{nav}<ul>{cards}</ul></body></html>"}
    for n in range(1, count + 1):
        if n not in missing:
            pages[f"https://{host}/e/{n}"] = detail_page(f"{host} Event {n}", f"Saturday, November {n}, 2026", "9:00am to 1:00pm", location=venue, address=f"{n} Main Street, {venue}, CA")
    return f"https://{host}/events", pages


class FairCrawling(unittest.TestCase):
    """Several starting URLs share one page budget: every start is read first and the websites then take turns, so none starves another."""

    def run_sites(self, sites, **options):
        urls, pages = [], {}
        for url, site_pages in sites:
            urls.append(url)
            pages.update(site_pages)
        result, client = crawl(pages, urls=tuple(urls), **options)
        return result, client

    def test_two_websites_with_many_links_share_the_remaining_budget_equally(self):
        result, client = self.run_sites([site("alpha.example", 6, "Alpha"), site("beta.example", 6, "Beta")], max_pages=8, max_depth=1)
        by_site = json.loads(result.notes["by_website"])
        self.assertEqual((by_site["alpha.example"]["event_pages_followed"], by_site["beta.example"]["event_pages_followed"]), (3, 3))
        self.assertEqual({h: (v["pages_attempted"], v["pages_skipped"]) for h, v in by_site.items()}, {"alpha.example": (4, 3), "beta.example": (4, 3)})
        self.assertEqual(client.requests[:2], ["https://alpha.example/events", "https://beta.example/events"], "every starting URL is read first")
        self.assertEqual(result.notes["pages_attempted"], 8)
        self.assertTrue(result.notes["page_budget_exhausted"])
        merged = [c for c in result.candidates if c.venue]
        self.assertEqual(len(merged), 6)
        self.assertEqual({c.venue for c in merged}, {"Alpha", "Beta"})
        self.assertEqual(sum(1 for c in merged if c.venue == "Alpha"), 3)

    def test_a_website_with_few_links_gets_all_of_them_and_the_other_takes_the_rest(self):
        result, _ = self.run_sites([site("big.example", 30, "Big"), site("small.example", 2, "Small")], max_pages=10, max_depth=1)
        by_site = json.loads(result.notes["by_website"])
        self.assertEqual(by_site["small.example"]["event_pages_followed"], 2, "the small site is fully covered")
        self.assertEqual(by_site["big.example"]["event_pages_followed"], 6, "the large site uses what the small one leaves")
        self.assertEqual(by_site["small.example"]["pages_skipped"], 0)
        self.assertEqual(by_site["big.example"]["pages_skipped"], 24)

    def test_the_first_website_cannot_use_up_the_budget_of_the_second(self):
        result, client = self.run_sites([site("first.example", 20, "First"), site("second.example", 20, "Second")], max_pages=4, max_depth=1)
        by_site = json.loads(result.notes["by_website"])
        self.assertEqual((by_site["first.example"]["event_pages_followed"], by_site["second.example"]["event_pages_followed"]), (1, 1))
        self.assertEqual(client.requests, ["https://first.example/events", "https://second.example/events", "https://first.example/e/1", "https://second.example/e/1"])

    def test_event_detail_links_come_before_generic_discovery_links(self):
        result, client = self.run_sites([site("alpha.example", 4, "Alpha", generic=True), site("beta.example", 4, "Beta", generic=True)], max_pages=6, max_depth=1)
        fetched = [u for u in client.requests if u not in ("https://alpha.example/events", "https://beta.example/events")]
        self.assertTrue(all("/e/" in u for u in fetched), fetched)
        self.assertEqual(len(fetched), 4)

    def test_generic_links_are_used_when_a_website_has_no_event_cards(self):
        pages = {"https://plain.example/events": '<a href="/events/calendar">Event calendar</a>', "https://plain.example/events/calendar": "<p>nothing</p>"}
        result, client = crawl(pages, urls=("https://plain.example/events",), max_pages=4, max_depth=1)
        self.assertIn("https://plain.example/events/calendar", client.requests)

    def test_unavailable_event_pages_cost_budget_but_never_candidates_or_identity(self):
        sites = [site("alpha.example", 5, "Alpha", missing={1, 2, 3, 4, 5}), site("beta.example", 5, "Beta")]
        listing_only, _ = self.run_sites(sites, max_pages=2, max_depth=0)
        result, _ = self.run_sites(sites, max_pages=8, max_depth=1)
        by_site = json.loads(result.notes["by_website"])
        self.assertEqual((by_site["alpha.example"]["event_pages_followed"], by_site["alpha.example"]["event_pages_failed"]), (3, 3))
        self.assertEqual((by_site["beta.example"]["event_pages_followed"], by_site["beta.example"]["event_pages_failed"]), (3, 0))
        self.assertEqual(len(result.candidates), len(listing_only.candidates), "no duplicates")
        self.assertEqual({c.local_id for c in result.candidates}, {c.local_id for c in listing_only.candidates}, "no identity churn")
        alpha = [c for c in result.candidates if "alpha" in c.title]
        self.assertTrue(all(c.venue is None and c.url.endswith("/events") for c in alpha), "the listing facts stand where the event page failed")

    def test_the_run_notes_stay_flat_so_the_service_can_report_them(self):
        from collections.abc import Mapping
        result, _ = self.run_sites([site("alpha.example", 3, "Alpha"), site("beta.example", 3, "Beta")], max_pages=6, max_depth=1)
        self.assertFalse([k for k, v in result.notes.items() if isinstance(v, (Mapping, list, tuple))])
        self.assertEqual(set(json.loads(result.notes["by_website"])), {"alpha.example", "beta.example"})

    def test_the_budget_and_depth_settings_still_decide_everything(self):
        sites = [site("alpha.example", 6, "Alpha"), site("beta.example", 6, "Beta")]
        none, client = self.run_sites(sites, max_pages=12, max_depth=0)
        self.assertEqual(len(client.requests), 2)
        everything, client = self.run_sites(sites, max_pages=14, max_depth=1)
        self.assertEqual(len(client.requests), 14)
        self.assertFalse(everything.notes["page_budget_exhausted"])
        self.assertTrue(all(v["pages_skipped"] == 0 for v in json.loads(everything.notes["by_website"]).values()))
        self.assertEqual(len([c for c in everything.candidates if c.venue]), 12)


if __name__ == "__main__":
    unittest.main()
