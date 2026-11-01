"""The shared venue resolver (discovery/venues.py): a venue name, town and state to its Google Maps business record, through the one SerpApi account.

Every test runs the real SafeFetcher, the real SerpApiAccount (its permit, hard stop and automatic pause) and the real SQLite cache over an injected fake of
serpapi.com that answers Google Maps searches from recorded, minimized answers (tests/data/serpapi_maps: two live answers of 2026-11-01, and answers in the
provider's documented `local_results` form for the cases a live search did not show). Nothing touches the network.
"""
import json
import os
import socket
import sqlite3
import tempfile
import unittest
from dataclasses import replace

from area_events.control.service import CreateAreaRequest, CreateSourceRequest
from area_events.deduplication.exact import ExactMatch
from area_events.discovery.event_links import EventLink
from area_events.discovery.fetching import RecordingFetcher
from area_events.discovery.producers.serpapi_google_events import SerpApiAccount, SerpApiGoogleEventsProducer, UsageBlocked
from area_events.discovery.safe import HostPolicy, SafeFetcher
from area_events.discovery.venues import MONTHLY_LIMIT, SCAN_LIMIT, VenueResolver, same_name, state_code
from area_events.persistence.build import open_persistence
from tests.test_extraction import Fixture
from tests.test_serpapi import JSON, KEY, NOW, FakeSerpApi, carries_key, database_text, page, policy, resolver

DATA = os.path.join(os.path.dirname(__file__), "data", "serpapi_maps")
NO_RESULTS = {"error": "Google hasn't returned any results for this query."}
RED_SILO = "118 W 1st St, Cookeville, TN 38501"
COOKEVILLE = {"query": "events in Cookeville", "location": "Cookeville, Tennessee, United States", "timezone": "America/Chicago", "name": "SerpApi Cookeville",
              "source_key": "serp_cookeville"}


def recorded(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as handle:
        return json.load(handle)


def listed(*entries):
    """An answer in the provider's documented list form: several business records for one search."""
    return {"search_parameters": {"engine": "google_maps", "type": "search"}, "local_results": [dict(position=n, **e) for n, e in enumerate(entries, 1)]}


def place(**record):
    return {"search_parameters": {"engine": "google_maps", "type": "search"}, "place_results": record}


ANSWERS = {"Red Silo Brewing, Cookeville, TN": recorded("red_silo_brewing_cookeville_tn.json"),
           "Cumberland County Playhouse, Crossville, TN": recorded("cumberland_county_playhouse_crossville_tn.json")}


def maps(answers):
    """The Maps side of the fake: each query's answer (a payload, an (HTTP status, headers, body) answer, or an exception raised as the network would)."""
    def answer(params):
        body = answers.get(params["q"], NO_RESULTS)
        if isinstance(body, BaseException):
            raise body
        if isinstance(body, tuple):
            return body
        return 200, dict(JSON), json.dumps(body).encode()
    return answer


class Setup(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = os.path.join(self.folder.name, "discovery.db")
        self.now = NOW
        self.fake = FakeSerpApi(maps=maps(dict(ANSWERS)))
        self.open()

    def open(self, key=KEY, pause_at=200):
        self.persistence = open_persistence(self.db, ExactMatch(), lambda: self.now)
        self.addCleanup(self.persistence.close)
        self.client = SafeFetcher(0, resolver=resolver, send=self.fake, sleep=lambda s: None, state=HostPolicy())
        self.account = SerpApiAccount(self.client, key, policy(True, pause_at), clock=lambda: self.now)
        self.venues = VenueResolver(self.account, self.persistence.venues, clock=lambda: self.now, fetch=self.client)

    def restart(self):
        self.persistence.close()
        self.open()

    def answer(self, query, body):
        self.fake.maps = maps({**ANSWERS, query: body})

    def maps_queries(self):
        from urllib.parse import parse_qsl
        return [dict(parse_qsl(t.partition("?")[2])) for t in self.fake.targets if t.startswith("/search?") and "engine=google_maps" in t]

    def rows(self, table):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            connection.close()


# --- A, D-I: matching ----------------------------------------------------------------------------------------------------------------------------------

class Matching(Setup):
    def test_a_red_silo_brewing_resolves_to_its_own_street_address(self):
        found = self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        self.assertEqual((found.status, found.name, found.address), ("matched", "Red Silo Brewing", RED_SILO))
        self.assertAlmostEqual(found.latitude, 36.1653451, places=6)
        self.assertAlmostEqual(found.longitude, -85.5082642, places=6)
        self.assertEqual(found.place_id, "ChIJx9zMihAiZ4gRxyEIb929wlA")
        self.assertIn("ChIJx9zMihAiZ4gRxyEIb929wlA", found.evidence)
        self.assertIn("provider-reported", found.evidence)
        [query] = self.maps_queries()
        self.assertEqual({k: query[k] for k in ("engine", "type", "q")}, {"engine": "google_maps", "type": "search", "q": "Red Silo Brewing, Cookeville, TN"})
        self.assertEqual(self.fake.maps_searches, 1)

    def test_the_second_live_answer_and_a_full_state_name(self):
        found = self.venues.resolve_venue("Cumberland County Playhouse", "Crossville", "Tennessee")
        self.assertEqual((found.status, found.address), ("matched", "221 Tennessee Ave, Crossville, TN 38555"))

    def test_d_the_right_name_in_another_town_or_state_is_rejected(self):
        self.answer("Red Silo Brewing, Cookeville, TN", listed({"title": "Red Silo Brewing", "address": "412 Broadway, Nashville, TN 37203", "place_id": "nash"},
                                                               {"title": "Red Silo Brewing", "address": "118 W 1st St, Cookeville, GA 30000", "place_id": "ga"}))
        found = self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        self.assertEqual((found.status, found.address), ("no_match", None))
        self.assertIn("only elsewhere", found.evidence)

    def test_e_a_similar_but_different_business_is_rejected(self):
        self.answer("Red Silo Brewing, Cookeville, TN", place(title="Red Silo Coffee", address="120 W 1st St, Cookeville, TN 38501", place_id="coffee"))
        self.assertEqual(self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN").status, "no_match")
        self.answer("Red Silo Brewing, Cookeville, TN", listed({"title": "Red Silo Brewing Taproom Two", "address": "9 Main St, Cookeville, TN 38501"}))
        self.now += 31 * 86400   # past the no-match reuse
        self.assertEqual(self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN").status, "no_match")

    def test_names_compare_through_punctuation_abbreviations_suffixes_and_the_town(self):
        for wanted, offered in (("Red Silo Brewing", "Red Silo Brewing Co."), ("Red Silo Brewing", "RED SILO BREWING, LLC"), ("Brown's Sparrow Books", "Browns Sparrow Books"),
                                ("Revolver Dance Hall of Cookeville", "Revolver Dance Hall"), ("Bear Trace At Cumberland Mountain (TN Golf Trail)", "Bear Trace at Cumberland Mountain"),
                                ("St. Mary Church", "Saint Mary Church"), ("Arts & Crafts Centre", "Arts and Crafts Center"), ("The Palace Theatre", "Palace Theater")):
            with self.subTest(wanted=wanted, offered=offered):
                self.assertTrue(same_name(wanted, offered, "Cookeville"))
        for wanted, offered in (("Red Silo Brewing", "Red Silo Coffee"), ("Crossville Depot", "The Depot"), ("Cookeville Community Center Building", "Cookeville Community Center"),
                                ("Grinder House Coffee Shop", "Grinder House"), ("First Methodist Church", "First United Methodist Church")):
            with self.subTest(wanted=wanted, offered=offered):
                self.assertFalse(same_name(wanted, offered, "Crossville" if "Crossville" in wanted else "Cookeville"))
        self.assertEqual((state_code("TN"), state_code("TN 38501"), state_code("Tennessee"), state_code("New York 10001"), state_code("Ontario")), ("TN", "TN", "TN", "NY", None))

    def test_f_several_places_of_that_name_in_the_town_are_ambiguous_but_one_place_listed_twice_is_not(self):
        self.answer("Red Silo Brewing, Cookeville, TN", listed({"title": "Red Silo Brewing", "address": RED_SILO, "place_id": "one"},
                                                               {"title": "Red Silo Brewing", "address": "900 S Jefferson Ave, Cookeville, TN 38501", "place_id": "two"}))
        found = self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        self.assertEqual((found.status, found.address), ("ambiguous", None))
        live = recorded("red_silo_brewing_cookeville_tn.json")
        self.answer("Abode Coffee, Cookeville, TN", {**live, "place_results": {**live["place_results"], "title": "Abode Coffee"},
                                                    "local_results": [{**live["place_results"], "title": "Abode Coffee"}]})
        self.assertEqual(self.venues.resolve_venue("Abode Coffee", "Cookeville", "TN").status, "matched")

    def test_g_a_business_without_a_street_address_is_no_match(self):
        self.answer("Red Silo Brewing, Cookeville, TN", place(title="Red Silo Brewing", address="Cookeville, TN 38501", place_id="x"))
        found = self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        self.assertEqual((found.status, found.address), ("no_match", None))
        self.assertIn("no street address", found.evidence)
        self.answer("Abode Coffee, Cookeville, TN", place(title="Abode Coffee", place_id="y"))
        self.assertEqual(self.venues.resolve_venue("Abode Coffee", "Cookeville", "TN").status, "no_match")

    def test_nothing_is_inferred_from_a_partial_record(self):
        self.answer("Red Silo Brewing, Cookeville, TN", place(title="Red Silo Brewing", address=RED_SILO, gps_coordinates={"latitude": "36.1", "longitude": None}))
        found = self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        self.assertEqual((found.status, found.address, found.latitude, found.longitude, found.place_id), ("matched", RED_SILO, None, None, None))

    def test_h_a_provider_failure_timeout_quota_or_missing_key_is_unavailable_and_never_remembered(self):
        for label, body in (("error", ({"error": "Your account has run out of searches."})), ("unreadable", (200, dict(JSON), b"<html>")),
                            ("no records", {"search_metadata": {"status": "Success"}})):
            with self.subTest(label):
                self.answer("Red Silo Brewing, Cookeville, TN", body)
                found = self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
                self.assertEqual(found.status, "unavailable", found.evidence)
                self.assertNotIn(KEY, found.evidence)
        self.assertEqual(self.rows("venue_lookups"), [], "a failure is never remembered as an answer")
        self.answer("Red Silo Brewing, Cookeville, TN", recorded("red_silo_brewing_cookeville_tn.json"))
        self.assertEqual(self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN").status, "matched", "a later lookup asks again")

    def test_h_a_network_timeout_is_unavailable(self):
        self.answer("Red Silo Brewing, Cookeville, TN", socket.timeout("timed out"))
        found = self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        self.assertEqual((found.status, self.rows("venue_lookups")), ("unavailable", []))
        self.assertEqual(self.fake.maps_searches, 1, "one request, no retry")

    def test_h_an_account_at_its_hard_stop_or_without_a_key_makes_no_maps_search(self):
        self.fake.used = 245   # five left: the hard stop
        self.assertEqual(self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN").status, "unavailable")
        self.assertIn("blocked_free_tier", self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN").evidence)
        self.fake.used = 10
        self.persistence.close()
        self.open(key=None)
        found = self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        self.assertEqual((found.status, self.fake.maps_searches, self.rows("venue_lookups")), ("unavailable", 0, []))

    def test_a_missing_venue_town_or_state_is_not_looked_up(self):
        for args in (("", "Cookeville", "TN"), ("Red Silo Brewing", "", "TN"), ("Red Silo Brewing", "Cookeville", ""), ("Red Silo Brewing", "Cookeville", "Ontario")):
            with self.subTest(args=args):
                self.assertEqual(self.venues.resolve_venue(*args).status, "unavailable")
        self.assertEqual(self.fake.maps_searches, 0)


# --- B, I-L: cache and budgets -------------------------------------------------------------------------------------------------------------------------

class CacheAndBudgets(Setup):
    def test_i_a_cached_answer_makes_no_new_search_whatever_its_spelling(self):
        first = self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        again = self.venues.resolve_venue("red silo brewing", " Cookeville ", "Tennessee")
        self.assertEqual((again.status, again.address, again.cached, first.cached), ("matched", RED_SILO, True, False))
        self.assertEqual(self.fake.maps_searches, 1)

    def test_answers_are_reused_for_a_bounded_time(self):
        self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        self.venues.resolve_venue("Abode Coffee", "Cookeville", "TN")   # no results: no_match
        self.now += 31 * 86400
        self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        self.venues.resolve_venue("Abode Coffee", "Cookeville", "TN")
        self.assertEqual(self.fake.maps_searches, 3, "the match is still reused; the no-match is asked again")
        self.now += 60 * 86400
        self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        self.assertEqual(self.fake.maps_searches, 4, "a match is asked again after 90 days")

    def test_j_the_cache_and_the_maps_budget_survive_a_restart(self):
        self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        self.restart()
        found = self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        self.assertEqual((found.status, found.address, found.cached, self.fake.maps_searches), ("matched", RED_SILO, True, 1))
        self.assertEqual(self.rows("venue_usage"), [("2026-10", 1)])

    def test_k_maps_searches_share_the_serpapi_account_allowance_and_its_policy(self):
        self.fake.used, self.fake.lag = 199, True   # the provider has not counted searches yet; the account counts its own
        self.persistence.close()
        self.open(pause_at=200)
        events = SerpApiGoogleEventsProducer(self.account, clock=lambda: self.now)
        self.assertEqual(self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN").status, "matched")
        with self.assertRaises(UsageBlocked) as blocked:
            events.crawl(RecordingFetcher(self.client, 10), (), dict(COOKEVILLE))
        self.assertEqual(blocked.exception.state, "paused_threshold", "the Maps search counted in the one allowance")
        self.assertEqual(self.venues.resolve_venue("Abode Coffee", "Cookeville", "TN").status, "unavailable", "and the automatic pause stops Maps too")
        self.assertEqual((self.fake.searches, self.fake.maps_searches), (0, 1))

    def test_l_a_scan_makes_at_most_three_new_searches_and_a_month_at_most_twenty(self):
        scan = self.venues.for_scan()
        answers = [scan.resolve_venue(f"Venue {n}", "Cookeville", "TN") for n in range(5)]
        self.assertEqual([a.status for a in answers], ["no_match"] * SCAN_LIMIT + ["unavailable"] * 2)
        self.assertIn("this scan has made its 3 new Maps searches", answers[-1].evidence)
        self.assertEqual(scan.resolve_venue("Venue 0", "Cookeville", "TN").status, "no_match", "a cached answer still comes at the limit")
        n = 5
        while self.fake.maps_searches < MONTHLY_LIMIT:
            scan = self.venues.for_scan()
            for _ in range(SCAN_LIMIT):
                scan.resolve_venue(f"Venue {n}", "Cookeville", "TN")
                n += 1
        self.assertEqual(self.fake.maps_searches, MONTHLY_LIMIT)
        self.restart()
        last = self.venues.for_scan().resolve_venue("Venue 999", "Cookeville", "TN")
        self.assertEqual((last.status, self.fake.maps_searches), ("unavailable", MONTHLY_LIMIT))
        self.assertIn("20 Maps searches of 2026-10 are used", last.evidence)
        self.now += 31 * 86400   # a new month
        self.assertEqual(self.venues.for_scan().resolve_venue("Venue 999", "Cookeville", "TN").status, "no_match")

    def test_nothing_private_is_stored(self):
        self.venues.resolve_venue("Red Silo Brewing", "Cookeville", "TN")
        text = database_text(self.db)
        self.assertNotIn(KEY, text)
        self.assertNotIn("owner@example.org", text)
        self.assertFalse(carries_key(self.rows("venue_lookups")))


# --- B, C, M, N and the Google-venue policy: the Google Events producer -------------------------------------------------------------------------------

def observed(title, when, venue="Red Silo Brewing", lines=("Red Silo Brewing", "Cookeville, TN"), link=None):
    return {"title": title, "date": {"when": when}, "address": list(lines), "venue": {"name": venue} if venue else None, "link": link,
            "description": f"{title} at {venue}."}


OPEN_MIC = observed("Open Mic Night", "Fri, Nov 6, 7 – 10 PM", link="https://www.redsilobrewing.com/events/open-mic-night")
TRIVIA = observed("Trivia Tuesday", "Tue, Nov 10, 6 – 8 PM")


class StubLinks:
    """The event-page finder as the producer sees it: one page (and perhaps an address) per title."""

    def __init__(self, found):
        self.found, self.asked, self.status = found, [], "searched"

    def find_event_link(self, title, starts_at, venue, city):
        self.asked.append((title, venue, city))
        return self.found.get(title)


class TheProducer(Setup):
    def crawl(self, results, venues=True, links=None, previous=()):
        self.fake.pages = [page(list(results))]
        producer = SerpApiGoogleEventsProducer(self.account, clock=lambda: self.now, links=links, venues=self.venues if venues else None)
        return producer.crawl(RecordingFetcher(self.client, 10), tuple(previous), dict(COOKEVILLE))

    def test_b_and_a_events_at_one_venue_share_one_lookup_and_get_its_street_address(self):
        result = self.crawl([OPEN_MIC, TRIVIA])
        self.assertEqual([c.address for c in result.candidates], [RED_SILO, RED_SILO])
        self.assertEqual(self.fake.maps_searches, 1)
        self.assertEqual((result.notes["venues_looked_up"], result.notes["venue_addresses"], result.notes["venue_searches"]), (2, 2, 1))
        [evidence] = [line for line in result.candidates[0].verification.evidence if " from Google Maps: " in line]
        self.assertTrue(evidence.startswith(f"Street address {RED_SILO} from Google Maps: the Google Maps business record 'Red Silo Brewing'"))
        self.assertNotIn("address", result.candidates[0].verification.missing)

    def test_c_an_existing_street_address_is_kept_and_nothing_is_looked_up(self):
        given = observed("Open Mic Night", "Fri, Nov 6, 7 – 10 PM", lines=("Red Silo Brewing, 118 W 1st St", "Cookeville, TN"))
        [candidate] = self.crawl([given]).candidates
        self.assertEqual((candidate.address, self.fake.maps_searches), ("Red Silo Brewing, 118 W 1st St, Cookeville, TN", 0))

    def test_n_identity_dates_url_and_verification_status_are_those_without_the_resolver(self):
        without = self.crawl([OPEN_MIC, TRIVIA], venues=False).candidates
        with_maps = self.crawl([OPEN_MIC, TRIVIA]).candidates
        for before, after in zip(without, with_maps):
            with self.subTest(before.title):
                self.assertEqual(replace(after, address=before.address, verification=before.verification), before, "only the address and its evidence differ")
                self.assertEqual((after.verification.status, after.verification.verified), (before.verification.status, before.verification.verified))
                self.assertEqual(after.verification.evidence[:len(before.verification.evidence)], before.verification.evidence)

    def test_m_the_event_page_finder_is_asked_as_before_and_its_street_address_comes_first(self):
        page_only = StubLinks({"Trivia Tuesday": EventLink("https://www.redsilobrewing.com/events/trivia-tuesday")})
        result = self.crawl([OPEN_MIC, TRIVIA], links=page_only)
        self.assertEqual(page_only.asked, [("Trivia Tuesday", "Red Silo Brewing", "Cookeville")], "asked only for the event Google gave no page for")
        self.assertEqual([(c.url, c.address) for c in result.candidates], [(OPEN_MIC["link"], RED_SILO), ("https://www.redsilobrewing.com/events/trivia-tuesday", RED_SILO)])
        with_address = StubLinks({"Trivia Tuesday": EventLink("https://www.redsilobrewing.com/events/trivia-tuesday", "118 W 1st St", "the event's structured location")})
        again = self.crawl([OPEN_MIC, TRIVIA], links=with_address)
        self.assertEqual(again.candidates[1].address, "118 W 1st St, Cookeville, TN", "the page's street address, completed with Google's town line, as before")
        self.assertEqual(again.notes["venues_looked_up"], 1, "only the event still without a street address asks the resolver")
        self.assertEqual(self.fake.maps_searches, 1)

    def test_a_definitive_no_match_withholds_a_new_event_but_never_an_existing_one(self):
        unknown = observed("Karaoke Night", "Fri, Nov 6, 8 – 11 PM", venue="El Magueyal Mexican Restaurant", lines=("El Magueyal Mexican Restaurant", "Cookeville, TN"))
        result = self.crawl([OPEN_MIC, unknown])
        self.assertEqual([c.title for c in result.candidates], ["Open Mic Night"])
        self.assertTrue(any("a new event whose venue is not one Google Maps business" in v for k, v in result.notes.items() if k.startswith("set_aside_")))
        known = self.crawl([unknown], venues=False).candidates
        again = self.crawl([OPEN_MIC, unknown], previous=known)
        self.assertEqual([c.title for c in again.candidates], ["Open Mic Night", "Karaoke Night"], "an event the source already has is submitted as before")
        self.assertEqual(again.candidates[1], known[0])
        self.assertEqual(again.notes["venues_unmatched_kept"], 1)
        self.assertEqual(self.fake.maps_searches, 2, "the no-match was remembered")

    def test_unavailable_is_never_a_no_match_and_an_earlier_maps_address_is_kept(self):
        [first] = self.crawl([OPEN_MIC]).candidates
        empty = open_persistence(os.path.join(self.folder.name, "other.db"), ExactMatch(), lambda: self.now)
        self.addCleanup(empty.close)
        self.venues = VenueResolver(self.account, empty.venues, clock=lambda: self.now, fetch=self.client, monthly_limit=0)   # nothing cached, no budget
        searches = self.fake.maps_searches
        [kept] = self.crawl([OPEN_MIC], previous=[first]).candidates
        self.assertEqual(kept, first, "the address an earlier scan took from Google Maps, and its evidence, are kept while Maps is unavailable")
        [plain] = self.crawl([TRIVIA]).candidates
        self.assertEqual(plain.address, "Red Silo Brewing, Cookeville, TN", "a new event is kept as Google gave it, not withheld")
        self.assertEqual(self.fake.maps_searches, searches)

    def test_an_event_without_a_venue_is_not_looked_up(self):
        [candidate] = self.crawl([observed("Street Fair", "Sat, Nov 7, 10 AM – 4 PM", venue=None, lines=("Downtown", "Cookeville, TN"))]).candidates
        self.assertEqual((candidate.address, self.fake.maps_searches), ("Downtown, Cookeville, TN", 0))


# --- O: composition --------------------------------------------------------------------------------------------------------------------------------------

class Composition(Fixture):
    def test_o_one_resolver_is_composed_and_only_the_google_events_producer_is_given_it(self):
        fake = FakeSerpApi()
        built = self.build(fetch_client=SafeFetcher(0, resolver=resolver, send=fake, sleep=lambda s: None, state=HostPolicy()), serpapi_api_key=KEY)
        serpapi = built.service.adapters["serpapi_google_events"]
        self.assertIsInstance(built.venues, VenueResolver)
        self.assertIs(serpapi.venues, built.venues)
        self.assertIs(built.venues._account, built.serpapi, "the one SerpApi account")
        for name, adapter in built.service.adapters.items():
            if name != "serpapi_google_events":
                with self.subTest(name):
                    self.assertNotIn(built.venues, vars(adapter).values(), "other producers are unchanged")


# --- the isolated end-to-end test: the real composition on a scratch database ----------------------------------------------------------------------------

class EndToEnd(Fixture):
    def setUp(self):
        super().setUp()
        self.clock.t = NOW
        self.fake = FakeSerpApi(pages=[page([OPEN_MIC, TRIVIA])], maps=maps(dict(ANSWERS)))
        client = SafeFetcher(0, resolver=resolver, send=self.fake, sleep=lambda s: None, state=HostPolicy())
        self.built = self.build(fetch_client=client, serpapi_api_key=KEY)
        self.built.control.create_source(CreateSourceRequest("serp_cookeville", "serpapi_google_events", "SerpApi Cookeville", True, 60,
                                                             {k: COOKEVILLE[k] for k in ("query", "location", "timezone")}))
        self.area = self.built.control.create_area(CreateAreaRequest("Cookeville", 36.1628, -85.5016, 25, sources=("serp_cookeville",))).area

    def scan(self):
        self.built.scheduler.plan()
        self.built.service.make_due("serp_cookeville")
        reports = [r for r in self.built.service.run_until_idle(10) if r.job_key and "serp_cookeville" in r.job_key]
        self.drain(self.built)
        return reports

    def test_a_google_event_reaches_the_standard_event_and_the_api_with_its_venue_street_address(self):
        [report] = self.scan()
        self.assertEqual(report.outcome, "completed")
        events = {e.title: e for e in self.all_events(self.built)}
        self.assertEqual(set(events), {"Open Mic Night", "Trivia Tuesday"})
        self.assertEqual({t: e.address for t, e in events.items()}, {"Open Mic Night": RED_SILO, "Trivia Tuesday": RED_SILO})
        self.assertEqual(events["Open Mic Night"].source_url, OPEN_MIC["link"], "the event's own page is unchanged")
        self.assertEqual(events["Open Mic Night"].location_status, "address")
        self.assertEqual(self.fake.maps_searches, 1)

        from area_events import app as app_module
        from area_events.api.http_api import ApiTokens
        from tests.test_discovery_control import CONTROL_TOKEN, READ_TOKEN, request
        from tests.test_extraction import PROCESS_TOKEN
        server = app_module.build_api(self.built, ApiTokens(CONTROL_TOKEN, READ_TOKEN, PROCESS_TOKEN), clock=self.clock, port=0).start()
        self.addCleanup(server.stop)
        status, body = request(server, "GET", f"/v1/events?area={self.area.area_key}", token=READ_TOKEN)
        self.assertEqual(status, 200)
        listed_events = {e["title"]: e for e in body["events"]}
        self.assertEqual(listed_events["Open Mic Night"]["address"], RED_SILO)
        self.assertEqual(listed_events["Open Mic Night"]["source_url"], OPEN_MIC["link"])
        self.assertFalse(carries_key(body))

        before = {(e.event_key, e.revision) for e in self.all_events(self.built)}
        candidates = self.count("candidates", "candidate_key LIKE 'serp_cookeville:%'")
        links = self.count("event_candidates")
        [second] = self.scan()
        self.assertEqual(second.outcome, "completed")
        self.assertEqual({(e.event_key, e.revision) for e in self.all_events(self.built)}, before, "no new event, no new revision")
        self.assertEqual((self.count("candidates", "candidate_key LIKE 'serp_cookeville:%'"), self.count("event_candidates")), (candidates, links))
        self.assertEqual((self.fake.searches, self.fake.maps_searches), (2, 1), "the second scan reads the venue from the cache")
        self.assertNotIn(KEY, database_text(self.db))


if __name__ == "__main__":
    unittest.main()
