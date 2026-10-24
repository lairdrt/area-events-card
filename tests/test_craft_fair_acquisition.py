"""Source acquisition pilot (Milestone 2G): craft fairs found by following public listings to the organizers' own pages.

The fixtures are real public pages read on 2026-10-24 (scripts removed, markup trimmed): two vendors' lists of the craft fairs they sell at, and the organizer
pages those lists link to. Nothing here hard-codes an event: the events are read from the pages."""
import dataclasses
import json
import os
import sqlite3
import unittest
from datetime import date

from discovery.safe import FetchError
from discovery.service.control import CreateAreaRequest
from discovery.service.fetching import RecordingFetcher
from discovery.service.producers.craft_fairs import (
    AcquisitionConfig,
    CraftFairProducer,
    SourceAcquirer,
)
from discovery.service.contracts import SourceSpec
from discovery.service.persistence.schema import LATEST
from discovery.service.producers.craft_fairs import reading as craftfairs

from tests.test_discovery_service import Clock, FakeClient
from tests.test_extraction import Fixture

DATA = os.path.join(os.path.dirname(__file__), "data", "craftfairs")
TODAY = date(2026, 10, 24)
SQ = "https://www.sweetandquirky.com/pages/upcoming-events"
DRAGON = "https://my2dragonflies.com/pages/local-markets"
HOLY = "https://www.holyfamilysd.org/ia-events/2026-holiday-craft-fair/"
SEVEN = "https://www.sevenoakscraftfair.com/"
CLAIREMONT = "https://clairemontcanyonsacademy.sandiegounified.org/school_clubs_and_activities/handmade_arts_and_crafts_fair"
EVENTBRITE = "https://www.eventbrite.com/e/north-park-winter-wonderland-fair-tickets-1978376489336"


def page(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as handle:
        return handle.read()


def web(**changes):
    """The saved pages by address. The listing's other links (Coronado, Golden Guide) are not saved, so they fail like an unreachable site."""
    pages = {SQ: page("sweetandquirky_upcoming_events.html"), DRAGON: page("my2dragonflies_local_markets.html"), HOLY: page("holyfamily_craft_fair.html"),
             SEVEN: page("sevenoaks_home.html"), CLAIREMONT: page("clairemont_craft_fair.html"), EVENTBRITE: page("eventbrite_north_park.html")}
    pages.update(changes)
    return pages


def acquire(pages, seeds=(SQ, DRAGON), config=AcquisitionConfig(), max_requests=40):
    client = FakeClient(pages)
    acquirer = SourceAcquirer(RecordingFetcher(client, max_requests), "sd_craft_fairs", "San Diego craft fairs", TODAY, config)
    events, leads, notes = acquirer.run(seeds)
    return events, {lead.url: dataclasses.asdict(lead) for lead in leads}, notes, client


class Reading(unittest.TestCase):
    def test_listing_cards_carry_dates_clock_times_and_the_places_they_write(self):
        cards = {e["title"]: e for e in craftfairs.listing_entries(page("sweetandquirky_upcoming_events.html"), SQ, TODAY)}
        self.assertEqual(len(cards), 7)
        seven = cards["Seven Oaks Craft Fair"]
        self.assertEqual((seven["day"], seven["start"], seven["end"]), (date(2026, 12, 5), (9, 0), (15, 0)))
        self.assertEqual((seven["venue"], seven["address"]), ("Seven Oaks Community Center", "16789 Bernardo Oaks Drive"))
        self.assertEqual([url for url, _ in seven["links"]], [SEVEN])
        clairemont = cards["Clairemont Canyons Academy 50th Craft Fair"]
        self.assertEqual((clairemont["day"], clairemont["start"], clairemont["venue"], clairemont["address"]), (date(2026, 11, 14), (9, 0), None, "4133 Mt. Albertine Ave."),
                         "a card with no venue line has no venue; nothing is borrowed from the card above or below it")
        dragon = {e["title"]: e for e in craftfairs.listing_entries(page("my2dragonflies_local_markets.html"), DRAGON, TODAY)}
        self.assertEqual(dragon["50th Annual Clairemont Canyons Academy Craft Fair"]["address"], "4133 Mt. Albertine Ave, San Diego, CA 92111")
        self.assertEqual(dragon["50th Annual Clairemont Canyons Academy Craft Fair"]["venue"], "Clairemont Canyons Academy")

    def test_past_dates_and_cards_without_a_clock_time_are_not_events(self):
        later = date(2026, 12, 1)
        titles = {e["title"] for e in craftfairs.listing_entries(page("sweetandquirky_upcoming_events.html"), SQ, later)}
        self.assertNotIn("Clairemont Canyons Academy 50th Craft Fair", titles)
        self.assertIn("Seven Oaks Craft Fair", titles)
        self.assertEqual(craftfairs.listing_entries("<ul><li><h3>Craft Fair</h3><p>Saturday, December 5, 2026</p></li></ul>", SQ, TODAY), [], "a date alone is not enough")

    def test_craft_fairs_are_told_apart_from_markets_and_generic_fairs(self):
        for title in ("Seven Oaks Holiday Craft Fair", "Golden Makers Market", "Handmade Arts & Crafts Fair", "54th Annual Coronado Artisans Alley Craft Fair"):
            self.assertTrue(craftfairs.craft_fair_text(title), title)
        self.assertTrue(craftfairs.craft_fair_text("North Park Winter Wonderland Fair", "Artisan marketplace with 60+ vendors, seasonal eats"))
        for title in ("Downtown Farmers Market", "County Fair and Rodeo", "Science Fair", "Job Fair", "Holiday in the Village", "Día de los Muertos Spooky Market",
                      "Halloween Skate Event", "Flea Market and Swap Meet"):
            self.assertFalse(craftfairs.craft_fair_text(title), title)
        self.assertFalse(craftfairs.craft_fair_text("Farmers Market", "also some craft vendors"), "a farmers market stays a farmers market")

    def test_the_county_comes_from_what_the_page_writes(self):
        self.assertEqual(craftfairs.region_signal("16789 Bernardo Oaks Drive, Rancho Bernardo"), "Rancho Bernardo")
        self.assertEqual(craftfairs.region_signal("Austin, TX 78701"), None)
        self.assertEqual(craftfairs.region_signal("4133 Mt. Albertine Ave, S.D., CA 92111"), "CA 92111")
        self.assertIsNone(craftfairs.region_signal("Dallas, TX 75201", "Portland, OR 97201"))

    def test_organizer_pages_state_their_own_event(self):
        seven = craftfairs.page_event(page("sevenoaks_home.html"), SEVEN, TODAY)
        self.assertEqual((seven["title"], str(seven["naive_start"]), str(seven["naive_end"]), seven["venue"], seven["address"].split(",")[0]),
                         ("Seven Oaks Holiday Craft Fair", "2026-12-05 09:00:00", "2026-12-05 15:00:00", "Seven Oaks Community Center", "16789 Bernardo Oaks Drive"))
        holy = craftfairs.page_event(page("holyfamily_craft_fair.html"), HOLY, TODAY)
        self.assertEqual((holy["title"], str(holy["naive_start"]), holy["address"], holy["organizer"]), ("Holiday Craft Fair", "2026-11-21 09:00:00", None, "Holy Family Roman Catholic Church"),
                         "no address is invented when the page gives none")
        eventbrite = craftfairs.page_event(page("eventbrite_north_park.html"), EVENTBRITE, TODAY)
        self.assertEqual((eventbrite["method"], eventbrite["venue"], eventbrite["address"], eventbrite["organizer"]),
                         ("jsonld", "North Park Mini Park", "3812 29th Street, San Diego, CA 92104", "Viridian Productions"), "schema.org data is preferred and its repeated city is dropped")
        self.assertIsNone(craftfairs.page_event(page("clairemont_craft_fair.html"), CLAIREMONT, TODAY), "the school's page gives no date, so it makes no event")

    def test_two_descriptions_of_one_event_are_recognised(self):
        a = {"day": date(2026, 11, 14), "title": "Clairemont Canyons Academy 50th Craft Fair", "address": "4133 Mt. Albertine Ave."}
        b = {"day": date(2026, 11, 14), "title": "50th Annual Clairemont Canyons Academy Craft Fair", "address": "4133 Mt. Albertine Ave, San Diego, CA 92111"}
        self.assertTrue(craftfairs.same_event(a, b))
        self.assertFalse(craftfairs.same_event(a, {**b, "day": date(2026, 11, 21)}), "another day is another event")
        self.assertFalse(craftfairs.same_event(a, {**b, "address": "900 Other Street"}), "another address is another event")
        self.assertTrue(craftfairs.same_event({"day": a["day"], "title": "Seven Oaks Craft Fair", "address": None}, {"day": a["day"], "title": "2026 Seven Oaks Holiday Craft Fair", "address": None}))
        self.assertFalse(craftfairs.same_event({"day": a["day"], "title": "Seven Oaks Craft Fair", "address": None}, {"day": a["day"], "title": "Coronado Craft Fair", "address": None}))


class Acquiring(unittest.TestCase):
    def test_links_in_the_listings_lead_to_organizer_pages_that_were_never_supplied(self):
        events, leads, notes, client = acquire(web())
        self.assertEqual(set(client.urls) - {SQ, DRAGON}, {SEVEN, CLAIREMONT, EVENTBRITE, "https://coronadoartisansalley.com/", "https://www.goldenguide.org/"},
                         "only seeds and the pages their cards link to were fetched")
        discovered = {url for url, lead in leads.items() if lead["found_on"] and lead["fetched"]}
        self.assertEqual(discovered, {SEVEN, CLAIREMONT, EVENTBRITE})
        for url in discovered:
            self.assertIn(leads[url]["found_on"], (SQ, DRAGON), "every discovered page records where it was found")
        self.assertEqual(sorted(e.title for e in events), ["50th Annual Clairemont Canyons Academy Craft Fair", "54th Annual Coronado Artisans Alley Craft Fair", "Golden Makers Market",
                                                               "North Park Winter Fair", "Seven Oaks Holiday Craft Fair"])

    def test_pages_are_accepted_or_rejected_with_reasons(self):
        _, leads, _, _ = acquire(web())
        self.assertEqual(leads[SEVEN]["decision"], "accepted")
        self.assertEqual((leads[SEVEN]["kind"], leads[CLAIREMONT]["kind"]), ("event", "organizer"))
        self.assertTrue(any("dated event on the page: yes" in r for r in leads[SEVEN]["reasons"]))
        self.assertTrue(any("no" in r for r in leads[CLAIREMONT]["reasons"] if r.startswith("dated event")))
        facebook = leads["https://www.facebook.com/events/2122424228707583/"]
        self.assertEqual((facebook["decision"], facebook["fetched"], facebook["kind"]), ("rejected", False, "social"))
        self.assertTrue(any("social network" in r for r in facebook["reasons"]))
        unreachable = leads["https://coronadoartisansalley.com/"]
        self.assertEqual(unreachable["decision"], "rejected")
        self.assertTrue(any("not read" in r for r in unreachable["reasons"]), "one unreachable site is recorded and does not stop the others")
        self.assertEqual(leads[SQ]["kind"], "listing")

    def test_unrelated_links_and_cards_are_rejected(self):
        shop = web(**{SQ: page("sweetandquirky_upcoming_events.html").replace("https://www.sevenoakscraftfair.com/", "https://www.etsy.com/shop/someone")})
        _, leads, notes, client = acquire(shop)
        self.assertEqual(leads["https://www.etsy.com/shop/someone"]["decision"], "rejected")
        self.assertNotIn("https://www.etsy.com/shop/someone", client.urls)
        self.assertIn("San Diego Ice Arena - Halloween Skate Event", notes["not_craft_fair"])
        self.assertIn("Holiday in the Village : A La Mesa Tradition", notes["not_craft_fair"], "a street festival with vendors is not a craft fair")

    def test_one_event_described_by_two_listings_and_an_organizer_is_one_event(self):
        events, _, _, _ = acquire(web())
        clairemont = [e for e in events if "Clairemont" in e.title]
        self.assertEqual(len(clairemont), 1)
        event = clairemont[0]
        self.assertEqual((event.starts_at, event.timezone, event.venue, event.address),
                         ("2026-11-14T09:00:00-08:00", "America/Los_Angeles", "Clairemont Canyons Academy", "4133 Mt. Albertine Ave, San Diego, CA 92111"))
        self.assertEqual((len(event.provenance.found_on), event.verification.status), (2, "corroborated"))
        self.assertEqual(event.provenance.found_on, (DRAGON, SQ))
        self.assertEqual(event.categories, ("Craft Fair",))
        self.assertEqual(event.url, CLAIREMONT, "the organizer's page is the event's address")

    def test_the_organizers_page_wins_and_the_difference_is_reported(self):
        wrong = web(**{SEVEN: page("sevenoaks_home.html").replace("9am to 3pm", "10am to 4pm")})
        events, _, notes, _ = acquire(wrong)
        seven = next(e for e in events if "Seven Oaks" in e.title)
        self.assertEqual((seven.starts_at, seven.ends_at), ("2026-12-05T10:00:00-08:00", "2026-12-05T16:00:00-08:00"))
        conflicts = [line.removeprefix("conflict: ") for line in seven.verification.evidence if line.startswith("conflict:")]
        self.assertEqual(len(conflicts), 1)
        self.assertIn("sweetandquirky.com says 2026-12-05 09:00, the organizer says 2026-12-05 10:00", conflicts[0])
        self.assertEqual(notes["conflicts"], conflicts)

    def test_source_imagery_comes_only_from_the_organizers_event_page(self):
        events, _, _, _ = acquire(web())
        images = {e.title: e.image_url for e in events}
        self.assertTrue(images["Seven Oaks Holiday Craft Fair"].startswith("https://"))
        self.assertTrue(images["North Park Winter Fair"].startswith("https://img.evbuc.com/"))
        self.assertIsNone(images["Golden Makers Market"], "a vendor's card photo is not used")

    def test_a_seed_that_is_an_event_page_needs_the_operators_region_when_it_names_none(self):
        events, leads, notes, _ = acquire(web(), seeds=(HOLY,))
        self.assertEqual(events, [])
        self.assertIn("Holiday Craft Fair", notes["outside_region"])
        events, leads, _, _ = acquire(web(), seeds=(HOLY,), config=AcquisitionConfig(seed_region="San Diego County"))
        self.assertEqual([(e.title, e.starts_at, e.address, e.organizer) for e in events],
                         [("Holiday Craft Fair", "2026-11-21T09:00:00-08:00", None, "Holy Family Roman Catholic Church")])
        self.assertEqual(leads[HOLY]["kind"], "event")
        self.assertIn("San Diego County: declared by the seed list (San Diego County)", leads[HOLY]["reasons"])

    def test_events_outside_the_county_are_dropped(self):
        listing = ("<html><body><h1>Craft markets in San Diego</h1><ul>"
                   "<li><h3>Austin Holiday Craft Fair</h3><p>Sat, December 5, 2026<br/>Palmer Events Center<br/>900 Barton Springs Rd, Austin, TX 78704<br/>9AM - 3PM</p></li>"
                   "<li><h3>Poway Holiday Craft Fair</h3><p>Sat, December 12, 2026<br/>Poway Community Park<br/>13094 Civic Center Dr, Poway, CA 92064<br/>9AM - 3PM</p></li>"
                   "<li><h3>Mystery Craft Fair</h3><p>Sat, December 19, 2026<br/>10AM - 2PM</p></li></ul></body></html>")
        events, _, notes, _ = acquire({SQ: listing}, seeds=(SQ,))
        self.assertEqual([e.title for e in events], ["Poway Holiday Craft Fair"], "a card needs a start time and a San Diego County place of its own; one that says Texas is dropped even under a San Diego heading")
        self.assertIn("Austin Holiday Craft Fair", notes["outside_region"])

    def test_crawling_is_bounded(self):
        _, leads, notes, client = acquire(web(), config=AcquisitionConfig(max_organizer_pages=1))
        self.assertEqual(notes["links_followed"], 1)
        self.assertEqual(len(client.urls), 3, "two seeds and one followed link")
        self.assertTrue(any("limit" in r for lead in leads.values() for r in lead["reasons"]))
        events, leads, notes, client = acquire(web(), max_requests=3)
        self.assertTrue(notes["budget_stopped"])
        self.assertEqual(len(client.urls), 3)
        self.assertTrue(events, "what was read before the budget ran out is still delivered")

    def test_an_unreadable_seed_does_not_stop_the_other_and_no_seed_at_all_fails_loudly(self):
        events, leads, notes, _ = acquire(web(**{SQ: FetchError("timed out")}))
        self.assertEqual(notes["seeds_read"], 1)
        self.assertEqual(leads[SQ]["decision"], "rejected")
        self.assertEqual({e.title for e in events} >= {"50th Annual Clairemont Canyons Academy Craft Fair"}, True)
        with self.assertRaises(FetchError) as caught:
            acquire({SQ: FetchError("timed out"), DRAGON: FetchError("blocked by robots.txt")})
        self.assertIn("no seed page could be read", str(caught.exception))


class ThroughThePipeline(Fixture):
    """The adapter inside the real service: stored candidates and leads, extraction, the event query, and the other cities untouched."""

    def setUp(self):
        super().setUp()
        self.clock = Clock(1793000000)   # 2026-10-26 07:33 UTC, the saved pages' time
        self.pages.update(web())
        self.spec = SourceSpec("sd_craft_fairs", "craft_fairs", "San Diego craft fairs", SQ, options=(("seeds", (SQ, DRAGON)), ("max_organizer_pages", 10), ("seed_region", "San Diego County")),
                               interval_s=21600)

    def built_with_craft(self):
        built = self.build()
        built.service.adapters["craft_fairs"] = CraftFairProducer(today=lambda: TODAY)
        built.service.register_source(self.spec)
        sd = built.control.create_area(CreateAreaRequest("San Diego Crafts", 32.7157, -117.1611, 25, sources=("sd_craft_fairs",))).area
        austin = built.control.create_area(CreateAreaRequest("Austin", 30.2672, -97.7431, 35, sources=("austin_fixture",))).area
        return built, sd, austin

    def crawl_craft(self, built):
        built.service.make_due("sd_craft_fairs")
        reports = []
        for _ in range(10):
            report = built.service.run_once()
            reports.append(report)
            if report.outcome == "nothing_due":
                break
        return [r for r in reports if r.job_key == "crawl_source:sd_craft_fairs"]

    def test_craft_fairs_flow_from_the_listings_to_the_event_query(self):
        built, sd, austin = self.built_with_craft()
        report = self.crawl_craft(built)[0]
        self.assertEqual((report.outcome, report.candidates_new), ("completed", 5))
        self.drain(built)
        events = {e.title: e for e in self.all_events(built, area_key=sd.area_key)}
        self.assertEqual(sorted(events), ["50th Annual Clairemont Canyons Academy Craft Fair", "54th Annual Coronado Artisans Alley Craft Fair", "Golden Makers Market",
                                          "North Park Winter Fair", "Seven Oaks Holiday Craft Fair"])
        seven = events["Seven Oaks Holiday Craft Fair"]
        self.assertEqual((seven.timezone, seven.venue_name, seven.address, seven.location_status, seven.sources), ("America/Los_Angeles", "Seven Oaks Community Center",
                                                                                                               "16789 Bernardo Oaks Drive, San Diego, CA 92128", "address", ("sd_craft_fairs",)))
        self.assertEqual(seven.source_url, SEVEN)
        self.assertEqual(built.events.get(seven.event_key).event.revision, 1)
        self.assertEqual({e.sources for e in self.all_events(built, area_key=austin.area_key)}, {("austin_fixture",)}, "an Austin area never sees San Diego craft fairs")

    def test_the_stored_candidate_keeps_the_category_the_evidence_and_the_pages(self):
        built, sd, _ = self.built_with_craft()
        self.crawl_craft(built)
        connection = sqlite3.connect(self.db)
        try:
            row = connection.execute("SELECT payload_json, candidate_id FROM candidates WHERE title LIKE 'Seven Oaks%'").fetchone()
            payload = json.loads(row[0])
            self.assertEqual((payload["categories"], payload["timezone"], payload["provenance"]["found_on"]), (["Craft Fair"], "America/Los_Angeles", [SQ]))
            quotes = {r[0]: r[1] for r in connection.execute("SELECT field, quote FROM evidence WHERE candidate_id=?", (row[1],))}
            self.assertIn("title", quotes, "the title is quoted from a page that was read")
            urls = {r[0] for r in connection.execute("SELECT p.url FROM pages p JOIN source_pages sp USING (page_id)")}
            self.assertTrue({SQ, DRAGON, SEVEN, CLAIREMONT, EVENTBRITE} <= urls, "every page read is stored with the source that found it")
            leads = {r[0]: (r[1], r[2], r[3]) for r in connection.execute("SELECT url, decision, found_on, kind FROM source_leads")}
            self.assertEqual(leads[SEVEN], ("accepted", SQ, "event"))
            self.assertEqual(leads["https://www.facebook.com/events/2122424228707583/"][0], "rejected")
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], LATEST)
        finally:
            connection.close()

    def test_a_second_run_changes_nothing_and_keeps_the_first_sighting_of_each_lead(self):
        built, _, _ = self.built_with_craft()
        self.crawl_craft(built)
        self.drain(built)
        before = self.snapshot()
        self.clock.t += 7200
        again = self.crawl_craft(built)[0]
        self.assertEqual((again.candidates_new, again.candidates_changed, again.candidates_unchanged), (0, 0, 5), "no duplicate candidates, nothing rewritten")
        self.drain(built)
        after = self.snapshot()
        self.assertEqual(before["events"], after["events"])
        self.assertEqual(before["leads"], after["leads"])
        self.assertGreater(after["last_seen"], before["last_seen"])
        self.assertEqual(self.count("events", "event_id IN (SELECT event_id FROM event_candidates WHERE role='duplicate')"), 0)

    def snapshot(self):
        connection = sqlite3.connect(self.db)
        try:
            return {"events": connection.execute("SELECT group_concat(event_key || ':' || revision) FROM (SELECT * FROM events ORDER BY event_key)").fetchone()[0],
                    "leads": connection.execute("SELECT group_concat(url_key || first_seen_at) FROM (SELECT * FROM source_leads ORDER BY url_key)").fetchone()[0],
                    "last_seen": connection.execute("SELECT max(last_seen_at) FROM source_leads").fetchone()[0]}
        finally:
            connection.close()

    def test_a_failing_acquisition_source_leaves_the_other_cities_crawls_alone(self):
        pages = web(**{SQ: FetchError("timed out"), DRAGON: FetchError("timed out")})
        self.pages.update(pages)
        built, sd, austin = self.built_with_craft()
        failed = self.crawl_craft(built)
        self.assertEqual(failed[0].outcome, "fetch_failed")
        self.drain(built)
        self.assertEqual(self.count("candidates", "source_id IN (SELECT source_id FROM sources WHERE source_key='sd_craft_fairs')"), 0)
        outcomes = [r.outcome for r in built.service.run_until_idle(10)]
        self.assertNotIn("store_failed", outcomes)


if __name__ == "__main__":
    unittest.main()
