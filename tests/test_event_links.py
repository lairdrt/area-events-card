"""The shared event-page finder (discovery/event_links.py) and its use by the SerpApi Google Events producer, offline.

The events are stored Jamestown (Crossville and Cookeville) results that Google listed without a page of their own; the search answers are fixtures in
the Tavily Search API's documented shape ({"results": [{"title", "url", "content", "score"}]}). No request leaves this process.
"""
import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone

from area_events.deduplication.exact import ExactMatch
from area_events.discovery import producer as contract
from area_events.discovery.event_links import MISS_TTL_S, EventLinkFinder, judge
from area_events.persistence.build import open_persistence
from tests.test_serpapi import FakeSerpApi, crawl, observed, page, setup

NOW = int(datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc).timestamp())
KEY = "tvly-test-key-0123456789abcdef"
TITLE, DAY, VENUE, CITY = "Sh-Boom! A Christmas Miracle", "2026-11-07", "Cumberland County Playhouse", "Crossville"   # stored serp_crossville event
SHOW = "https://ccplayhouse.com/shows/sh-boom-a-christmas-miracle/"


def hit(url, title="", content=""):
    return {"title": title, "url": url, "content": content, "score": 0.5}


CORRECT = hit(SHOW, "Sh-Boom! A Christmas Miracle | Cumberland County Playhouse",
              "Performances Nov 7 – Dec 20, 2026 at the Cumberland County Playhouse, Crossville, Tennessee.")
LAST_YEAR = hit("https://ccplayhouse.com/2025/sh-boom-a-christmas-miracle/", "Sh-Boom! A Christmas Miracle (2025)", "Crossville's holiday musical.")
ELSEWHERE = hit("https://knoxtheatre.org/events/sh-boom-a-christmas-miracle", "Sh-Boom! A Christmas Miracle", "Knoxville, December 12, 2026.")
VENUE_PAGE = hit("https://ccplayhouse.com/season", "2026 Season | Cumberland County Playhouse", "Crossville, TN. Shows all year.")
DIRECTORY = hit("https://www.crossville-chamber.com/", "Crossville Chamber of Commerce", "Events in Crossville, Nov 7, 2026.")
SEARCH = hit("https://www.google.com/search?q=sh-boom+christmas+miracle", "Sh-Boom! A Christmas Miracle - Google Search", "Crossville Nov 7")
OTHER_SHOW = hit("https://ccplayhouse.com/shows/a-jazzy-little-christmas/", "A Jazzy Little Christmas", "Cumberland County Playhouse, Crossville, Nov 7.")


class FakeTavily:
    """api.tavily.com/search: each call is one request (one credit); `answers` are (status, payload) given in turn, the last one repeated."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers) or [(200, {"results": []})], []

    def __call__(self, host, path, headers, body):
        self.calls.append({"host": host, "path": path, "headers": dict(headers), "body": json.loads(body)})
        status, payload = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(payload, Exception):
            raise payload
        return status, payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def results(*items):
    return 200, {"query": "q", "results": list(items), "response_time": 0.5}


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class Finding(unittest.TestCase):
    """Through the real EventLinkCache port on a discovery database; `restart` closes it and opens it again, as a service restart does."""

    def setUp(self):
        if getattr(self, "persistence", None) is not None:   # a fresh start within one test
            self.persistence.close()
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.db = os.path.join(folder.name, "discovery.db")
        self.clock = Clock()
        self.persistence = None
        self.restart()
        self.addCleanup(lambda: self.persistence.close())

    def restart(self):
        if self.persistence is not None:
            self.persistence.close()
        self.persistence = open_persistence(self.db, ExactMatch(), self.clock)

    def finder(self, fake, key=KEY, **limits):
        return EventLinkFinder(self.persistence.event_links, api_key=key, clock=self.clock, post=fake, sleep=lambda s: None, **limits)

    def rows(self, table):
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            return db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()

    def find(self, finder, title=TITLE, day=DAY, venue=VENUE, city=CITY):
        return finder.find_event_url(title, day, venue, city)

    def test_the_stored_event_s_own_page_is_found_among_wrong_ones(self):
        fake = FakeTavily(results(SEARCH, DIRECTORY, VENUE_PAGE, LAST_YEAR, ELSEWHERE, OTHER_SHOW, CORRECT))
        self.assertEqual(self.find(self.finder(fake)), SHOW)
        self.assertEqual(len(fake.calls), 1, "one search")
        call = fake.calls[0]
        self.assertEqual((call["host"], call["path"]), ("api.tavily.com", "/search"))
        self.assertEqual({k: call["body"][k] for k in ("search_depth", "max_results", "auto_parameters", "include_answer", "include_raw_content")},
                         {"search_depth": "basic", "max_results": 5, "auto_parameters": False, "include_answer": False, "include_raw_content": False},
                         "only the one-credit search")
        self.assertEqual(call["body"]["query"], "Sh-Boom! A Christmas Miracle Cumberland County Playhouse Crossville November 7, 2026")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertNotIn(KEY, json.dumps(call["body"]), "the key travels only in the Authorization header")

    def test_similar_but_wrong_pages_are_refused(self):
        for wrong, why in ((LAST_YEAR, "last year's address"), (ELSEWHERE, "another place and day"), (VENUE_PAGE, "the venue's own page"),
                           (DIRECTORY, "a directory home page"), (SEARCH, "a search"), (OTHER_SHOW, "another show at the venue")):
            with self.subTest(why):
                self.assertIsNone(judge([wrong], TITLE, datetime(2026, 11, 7).date(), VENUE, CITY))
        undated = hit("https://allevents.example/nashville/sh-boom-a-christmas-miracle", "Sh-Boom! A Christmas Miracle", "A holiday musical.")
        self.assertIsNone(judge([undated], TITLE, datetime(2026, 11, 7).date(), VENUE, CITY), "neither its date nor its place is shown")
        town_only = hit("https://b.example/events/sh-boom-a-christmas-miracle", "Sh-Boom!", "Crossville, Saturday November 7, 2026")
        self.assertIsNone(judge([town_only], TITLE, datetime(2026, 11, 7).date(), VENUE, CITY), "the town and day do not show this venue's show")
        rivals = [hit("https://a.example/shows/sh-boom-christmas-miracle", "Sh-Boom!", "Cumberland County Playhouse, Crossville, TN"),
                  hit("https://b.example/events/sh-boom-a-christmas-miracle", "Sh-Boom!", "Cumberland County Playhouse, Crossville, TN")]
        self.assertIsNone(judge(rivals, TITLE, datetime(2026, 11, 7).date(), VENUE, CITY), "two pages that agree only on the place are ambiguous")
        dated = [rivals[0], hit("https://b.example/events/sh-boom-a-christmas-miracle", "Sh-Boom!", "Cumberland County Playhouse, Saturday November 7, 2026")]
        self.assertEqual(judge(dated, TITLE, datetime(2026, 11, 7).date(), VENUE, CITY), "https://b.example/events/sh-boom-a-christmas-miracle",
                         "the page that shows the event's day wins")
        fake = FakeTavily(results(LAST_YEAR, ELSEWHERE, VENUE_PAGE, DIRECTORY, SEARCH))
        self.assertIsNone(self.find(self.finder(fake)))

    def test_no_result_is_none_and_is_not_asked_again_until_it_may_have_changed(self):
        fake = FakeTavily(results())
        finder = self.finder(fake)
        self.assertIsNone(self.find(finder))
        self.assertIsNone(self.find(finder))
        self.assertEqual(len(fake.calls), 1, "a miss is remembered")
        self.clock.now += MISS_TTL_S + 1
        fake.answers = [results(CORRECT)]
        self.assertEqual(self.find(finder), SHOW, "asked again once the miss is old")
        self.assertEqual(len(fake.calls), 2)

    def test_a_repeated_event_is_never_searched_again_even_after_a_restart(self):
        fake = FakeTavily(results(CORRECT))
        self.assertEqual(self.find(self.finder(fake)), SHOW)
        self.assertEqual(self.find(self.finder(fake)), SHOW)
        self.restart()
        self.clock.now += 30 * 86400
        self.assertEqual(self.find(self.finder(fake)), SHOW)
        self.assertEqual(len(fake.calls), 1, "one search for one event")
        self.assertEqual(self.find(self.finder(fake), day="2026-11-06"), None, "another day, outside the show's run, is another event")
        self.assertEqual(len(fake.calls), 2)
        self.clock.now += 40 * 86400
        self.find(self.finder(fake), title="Mistletoe Market", day="2026-12-20")
        self.assertEqual([row[3] for row in self.rows("event_link_lookups")], ["2026-12-20"], "lookups for long-past events are dropped")
        self.persistence.close()
        for folder, _, names in os.walk(os.path.dirname(self.db)):
            for name in names:
                with open(os.path.join(folder, name), "rb") as stored:
                    self.assertNotIn(KEY.encode(), stored.read(), f"the key is never stored ({name})")
        self.restart()

    def test_without_a_key_nothing_is_searched_or_written(self):
        fake = FakeTavily(results(CORRECT))
        previous = os.environ.pop("TAVILY_API_KEY", None)
        self.addCleanup(lambda: previous is not None and os.environ.__setitem__("TAVILY_API_KEY", previous))
        for key in (None, "", "   "):
            finder = self.finder(fake, key=key)
            self.assertIsNone(self.find(finder))
            self.assertEqual(finder.status, "unconfigured")
        self.assertEqual(fake.calls, [])
        self.assertEqual((self.rows("event_link_lookups"), self.rows("event_link_usage")), ([], []), "nothing stored without a key")

    def test_a_provider_failure_is_none_without_retries_and_pauses_searching(self):
        for answer, state in (((500, {"detail": "error"}), "provider answered HTTP 500"), ((200, b"<html>"), "provider answer unreadable"),
                              ((0, OSError(f"timed out sending Bearer {KEY}")), "provider unavailable"), ((401, {}), "key refused")):
            with self.subTest(state):
                self.setUp()
                fake = FakeTavily(answer, results(CORRECT))
                finder = self.finder(fake)
                self.assertIsNone(self.find(finder))
                self.assertEqual(finder.status, state)
                self.assertNotIn(KEY, finder.status)
                self.assertIsNone(self.find(finder), "paused: no retry loop")
                self.assertIsNone(self.find(finder, title="Mistletoe Market", day="2026-11-06", venue="Leslie Town Centre", city="Cookeville"))
                self.assertEqual(len(fake.calls), 1)
                self.clock.now += 86400 + 1
                self.assertEqual(self.find(finder), SHOW, "a failure is not remembered as a miss")

    def test_the_free_allowance_is_respected(self):
        self.clock.now = int(datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc).timestamp())
        fake = FakeTavily((432, {"detail": {"error": "This request exceeds your plan's set usage limit."}}))
        finder = self.finder(fake)
        self.assertIsNone(self.find(finder))
        self.assertEqual(finder.status, "provider usage limit reached")
        self.restart()
        self.clock.now += 3 * 86400
        self.assertIsNone(self.find(self.finder(fake)), "the rest of the month, after a restart too")
        self.assertEqual(len(fake.calls), 1)
        self.clock.now = int(datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc).timestamp())
        fake.answers = [results(CORRECT)]
        self.assertEqual(self.find(self.finder(fake)), SHOW, "a new month searches again")

        self.setUp()
        self.clock.now = int(datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc).timestamp())
        fake = FakeTavily(results())
        finder = self.finder(fake, daily_limit=2, monthly_limit=3)
        for number in range(4):
            self.find(finder, title=f"Holiday Market {number}")
        self.assertEqual(len(fake.calls), 2, "the daily limit")
        self.assertEqual(finder.status, "limit reached")
        self.clock.now += 86400
        for number in range(4, 8):
            self.find(finder, title=f"Holiday Market {number}")
        self.assertEqual(len(fake.calls), 3, "the monthly limit")
        self.assertEqual(dict(self.rows("event_link_usage")), {"2026-10-10": 2, "2026-10-11": 1, "2026-10": 3})


# --- live answers recorded in the November 2026 qualification (15 stored events, every page checked by hand); contents trimmed, words as recorded ---

STUBHUB_SH_BOOM = hit("https://www.stubhub.com/in/sh-boom-a-christmas-miracle-crossville-tickets-11-7-2026/event/162089003", "Sh-Boom! A Christmas Miracle Crossville Tickets",
                      "See Sh-Boom! A Christmas Miracle live at Cumberland County Playhouse in Crossville on November 7, 2026, at 2:30 pm. Sorry, there are no tickets")
PLAYHOUSE_SH_BOOM = hit("https://ccplayhouse.com/sh-boom-a-christmas-miracle", "Sh-Boom! A Christmas Miracle - Cumberland County Playhouse",
                        "## Sh-Boom! A Christmas Miracle Written & Created by Roger Bean The holiday sequel to 2025’s smash hit Sh-Boom! Life Could Be a Dream! "
                        "[...] Filled with great ’60s doo-wop hits and holiday classics. November 6-December 19, rated PG Mainstage")


class RecordedAnswers(unittest.TestCase):
    """Each case is one recorded search (the last, another town's page, is invented): the page the helper must choose, or why it must choose none."""

    def test_E01_another_venue_s_event_of_the_same_name_is_refused(self):
        title, day, venue, city = "Día de los Muertos Night", date(2026, 11, 1), "Gaslamplighter Karaoke Cocktail Bar", "San Diego"
        old_globe = hit("https://www.sandiego.org/events-festivals/old-globe-dia-de-los-muertos-day-of-the-dead-celebration/f5a5a26e59be11f1a7f2",
                        "Old Globe Día de los Muertos (Day of the Dead Celebration)",
                        "This annual gathering fosters connection and cultural appreciation in a welcoming, festive atmosphere. Dates & Times. Nov 1, 2026 11:00am-1:30pm. 1363 ...")
        old_town = hit("https://fiestadereyes.com/dia-de-los-muertos", "Dia de los Muertos - La Experiencia 2026 | Fiesta De Reyes",
                       "Dates September 1st – November 2nd, 2026 Location Fiesta De Reyes, 754 calhoun street, old town san diego state Historic Park")
        self.assertIsNone(judge([old_town, old_globe], title, day, venue, city), "the right name and day at another venue is another event")

    def test_E02_the_festival_s_own_page_is_chosen_over_a_resale_listing(self):
        title, day, venue, city = "San Diego Food and Wine Festival - Icons and Legends", date(2026, 11, 6), "JULEP Venue", "San Diego"
        stubhub = hit("https://www.stubhub.com/in/san-diego-food-and-wine-festival-san-diego-tickets-11-6-2026/event/161086388",
                      "San Diego Food and Wine Festival - Icons and Legends",
                      "See San Diego Food and Wine Festival - Icons and Legends - Friday live at JULEP venue in San Diego on November 6, 2026, at 6:30 pm.")
        official = hit("https://www.sandiegowineclassic.com/icons-legends", "SDFW Opening Night: Icons + Legends",
                       "Icons + Legends, Friday November 6, 2026 at JULEP Venue: opening night of the San Diego Food + Wine Festival, with celebrated chefs and acclaimed wineries.")
        visit = hit("https://www.visitcalifornia.com/event/san-diego-bay-wine-food-festival", "World-Class Tastings, Top Chefs, and Bayfront Celebrations | Visit California",
                    "San Diego Food + Wine Festival November 6–8, 2026 [...] Held at a different venue each November")
        self.assertEqual(judge([stubhub, official, visit], title, day, venue, city), official["url"], "its heading words it differently; it names the event, venue and day")
        self.assertEqual(judge([stubhub, visit], title, day, venue, city), stubhub["url"], "a resale listing that shows all of it is still the event's")

    def test_E05_speed_dating_with_no_sign_of_the_venue_is_refused(self):
        title, day, venue, city = "San Diego Singles Event: Speed Dating", date(2026, 11, 7), "Drift Cafe", "San Diego"
        other_cafe = hit("https://www.eventbrite.com/e/san-diego-singles-event-speed-dating-ages-22-30-at-coffee-shop-tickets-2000177736453",
                         "San Diego Singles Event: Speed Dating (Ages 22-30) at Coffee Shop Tickets, Saturday, November 7  •  3 PM - 5 PM | Eventb",
                         "### Discover more events from Jigsaw Dating, from Food & Drink to other experiences you might love.")
        city_page = hit("https://cityswoon.com/speed-dating-sandiego.jsp", "Speed Dating San Diego - Singles Events in San Diego - CitySwoon",
                        "The city's speed dating scene is as diverse as its vibrant neighborhoods")
        self.assertIsNone(judge([other_cafe, city_page], title, day, venue, city), "the session the page is for is at Subterranean Coffee")

    def test_E10_a_day_written_06_nov_2026_is_read(self):
        title, day, venue, city = "Veterans Day Program", date(2026, 11, 6), "Cumberland County High School", "Crossville"
        listing = hit("https://allevents.in/crossville/veterans-day-program/200030859355641", "Veterans Day Program - Crossville",
                      "Veterans Day Program takes place on Fri, 06 Nov, 2026 at 02:00 pm to Fri, 06 Nov, 2026 at 02:00 pm at Cumberland County Schools, 660 Stanley St,Crossville, ...Read more")
        post = hit("https://www.facebook.com/1025wowcountry/posts/-cumberland-county-high-school-veterans-day-celebration-mark-your-calendars-cumb/1782746873854446",
                   "Cumberland County High School Veterans Day celebration ...",
                   "❤️     Join the CCHS faculty, staff, and students for a Veterans Day celebration on Friday, November 6, at 2:00 PM in the Jets Auditorium.Read more")
        self.assertEqual(judge([listing, post], title, day, venue, city), listing["url"], "the dated event listing, before a dated social post")
        self.assertEqual(judge([post], title, day, venue, city), post["url"], "a post that shows all of it is kept when nothing better is offered")
        self.assertIsNone(judge([listing], title, date(2026, 11, 13), venue, city), "another Friday is another occurrence")

    def test_E12_a_day_within_the_production_s_run_is_its_day(self):
        title, day, venue, city = TITLE, date(2026, 11, 7), VENUE, CITY
        self.assertEqual(judge([STUBHUB_SH_BOOM, PLAYHOUSE_SH_BOOM], title, day, venue, city), PLAYHOUSE_SH_BOOM["url"])
        self.assertEqual(judge([PLAYHOUSE_SH_BOOM], title, date(2026, 12, 19), venue, city), PLAYHOUSE_SH_BOOM["url"], "the run's last day")
        self.assertIsNone(judge([PLAYHOUSE_SH_BOOM], title, date(2026, 12, 20), venue, city), "a day after the run")
        self.assertIsNone(judge([CORRECT], title, date(2027, 11, 7), venue, city), "a run of 2026 ('Nov 7 – Dec 20, 2026') is not next year's production")

    def test_E14_a_listing_worded_differently_is_kept_and_an_address_that_does_not_name_the_event_is_not(self):
        title, day, venue, city = "Holiday Marketplace at Mission Trails Regional Park", date(2026, 11, 14), "Mission Trails Regional Park Visitor Center", "San Diego"
        dates = "2026 HOLIDAY MARKETPLACE Dates: Saturday, November 14 and Sunday, November 15, 2026 Time: 9 a.m.–3 p.m. both days Location: Mission Trails Regional Park"
        listing = hit("https://www.eventbrite.com/e/holiday-marketplace-at-mission-trails-regional-park-tickets-2001272952273", "Holiday Marketplace at Mission Trails Regional Park", dates)
        organizer = hit("https://mtrp.org/marketplace", "Holiday Marketplace Benefiting Mission Trails Regional Park", dates)
        last_year = hit("https://www.sandiego.gov/event/holiday-marketplace-mission-trails-regional-park", "Holiday Marketplace at Mission Trails Regional Park | City of San Diego Official Website",
                        "📅 Happening November 15 and16 | 9AM to 3PM Mission Trails Regional Park [...] ## 🎁 Holiday Marketplace 2025 ✨ 📅 Sunday, November 16, 2025")
        self.assertEqual(judge([listing, organizer, last_year], title, day, venue, city), listing["url"])
        reworded = hit("https://www.eventbrite.com/e/holiday-marketplace-at-mission-trails-regional-park-tickets-2001272952273",
                       "Holiday Marketplace Benefiting Mission Trails Regional Park", dates)
        self.assertEqual(judge([reworded], title, day, venue, city), reworded["url"], "a heading worded differently does not refuse the event's page")
        self.assertIsNone(judge([organizer], title, day, venue, city), "its address names only 'marketplace': the rule every producer link meets")
        self.assertIsNone(judge([last_year], title, date(2026, 11, 15), venue, city), "the days it states are 2025's")

    def test_when_a_page_was_posted_is_not_its_event_s_day(self):
        title, day, venue, city = "Open Mic Night", date(2026, 10, 7), "Red Silo Brewing", "Cookeville"
        calendar = hit("https://www.1077gracefm.com/2026/10/07/open-mic-night-thu-nov-5-2026-600pm-900pm-red-silo-brewing", "Open Mic Night – Thu, Nov 5, 2026 6:00pm",
                       "Open Mic Night – Thu, Nov 5, 2026 6:00pm – 9:00pm @ Red Silo Brewing. Cookeville, TN, USA / 107.7 Grace FM. Hip Cookeville Community Calendar.")
        self.assertIsNone(judge([calendar], title, day, venue, city), "posted on October 7 for the November 5 open mic")
        self.assertEqual(judge([calendar], title, date(2026, 11, 5), venue, city), calendar["url"])
        article = hit("https://www.valleycenter.com/articles/guns-and-hoses-cook-off-will-benefit-explorers", "Guns and Hoses cook-off will benefit Explorers",
                      "Oct 14, 2026 — The 8th annual Guns & Hoses Chili cook-off, Sunday, November 8, noon-3 p.m. will be at Bates Nut Farm.")
        cook_off = ("8th Annual Guns & Hoses Chili Cook-Off Fundraiser", "Bates Nut Farm", "Valley Center")
        self.assertIsNone(judge([article], cook_off[0], date(2026, 10, 14), *cook_off[1:]))
        self.assertEqual(judge([article], cook_off[0], date(2026, 11, 8), *cook_off[1:]), article["url"])

    def test_another_town_s_page_is_refused(self):
        knox = hit("https://knoxtheatre.org/events/sh-boom-a-christmas-miracle", "Sh-Boom! A Christmas Miracle | Cumberland County Playhouse tour",
                   "On tour from the Cumberland County Playhouse: Knoxville, TN, November 7, 2026.")
        self.assertIsNone(judge([knox], TITLE, date(2026, 11, 7), VENUE, CITY))
        self.assertEqual(judge([PLAYHOUSE_SH_BOOM], TITLE, date(2026, 11, 7), VENUE, CITY), PLAYHOUSE_SH_BOOM["url"], "a page that names no town at all")


# --- the SerpApi Google Events producer -------------------------------------------------------------------------------

WHEN = "Sat, Nov 7, 2:30 PM"


def crossville(tickets=(("viagogo.com.mx", "tickets", "https://www.viagogo.com.mx/Boletos-Teatro/E-162089003"),)):
    """The stored serp_crossville result: no top-level link and only a ticket reseller's page that does not name the show."""
    item = observed(TITLE, WHEN, tickets=tickets)
    item.update(venue={"name": VENUE}, address=["Cumberland County Playhouse, 221 Tennessee Ave", "Crossville, TN"])
    return item


class Recording:
    def __init__(self, answer=None):
        self.answer, self.asked = answer, []
        self.status = "searched"

    def find_event_url(self, title, date, venue, city):
        self.asked.append((title, date, venue, city))
        return self.answer


def scan_with(links, *items):
    _, client, account, _ = setup(FakeSerpApi(pages=[page(list(items))]))
    from area_events.discovery.producers.serpapi_google_events import SerpApiGoogleEventsProducer
    answer = crawl(SerpApiGoogleEventsProducer(account, clock=lambda: NOW, links=links), client)[0]
    accepted = contract.accept(answer)
    assert accepted.rejections == (), accepted.rejections
    return answer, {c.title: c for c in answer.candidates}


class TheProducerAsksOnlyWhenGoogleGivesNoPage(unittest.TestCase):
    def test_an_event_without_a_page_gets_the_one_found_and_keeps_its_identity(self):
        links = Recording(SHOW)
        answer, found = scan_with(links, crossville())
        _, before = scan_with(None, crossville())
        show = found[TITLE]
        self.assertEqual(links.asked, [(TITLE, "2026-11-07T14:30:00-08:00", VENUE, "Crossville")], "the title, date, venue and town")
        self.assertEqual(show.url, SHOW)
        self.assertIsNone(before[TITLE].url)
        self.assertEqual(show.local_id, before[TITLE].local_id, "the event's identity does not depend on its link")
        self.assertEqual((show.verification.status, show.verification.verified), (before[TITLE].verification.status, False), "a link never verifies")
        self.assertIn("Event page found by a web search for the title, date and place; Google listed none", show.verification.evidence)
        self.assertEqual((answer.notes["links_looked_up"], answer.notes["links_found"], answer.notes["link_search"]), (1, 1, "searched"))

    def test_an_event_with_a_valid_page_is_never_looked_up(self):
        links = Recording("https://elsewhere.example/should-not-be-used")
        named = "https://www.ccplayhouse.com/shows/sh-boom-a-christmas-miracle-2026"
        _, found = scan_with(links, crossville([("ccplayhouse.com", "more info", named)]))
        self.assertEqual(found[TITLE].url, named)
        self.assertEqual(links.asked, [])

    def test_a_failed_lookup_leaves_the_event_as_it_was_and_the_scan_goes_on(self):
        fake = FakeTavily((0, OSError("unreachable")))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        persistence = open_persistence(os.path.join(folder.name, "discovery.db"), ExactMatch(), lambda: NOW)
        self.addCleanup(persistence.close)
        finder = EventLinkFinder(persistence.event_links, api_key=KEY, clock=lambda: NOW, post=fake, sleep=lambda s: None)
        second = crossville()
        second.update(title="Mistletoe Market", date={"when": "Fri, Nov 6, 9:00 AM"})
        answer, found = scan_with(finder, crossville(), second)
        self.assertEqual(sorted(found), ["Mistletoe Market", TITLE], "every event is still submitted")
        self.assertTrue(all(c.url is None for c in found.values()))
        self.assertEqual((answer.notes["links_looked_up"], answer.notes.get("links_found"), answer.notes["link_search"]), (2, None, "provider unavailable"))
        self.assertEqual(len(fake.calls), 1, "one failed request, not one for each event")


if __name__ == "__main__":
    unittest.main()
