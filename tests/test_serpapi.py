"""The SerpApi Google Events producer and the account guard that keeps it on the free plan.

Every test runs the real SafeFetcher (and, where a job runs, the real RecordingFetcher, Discovery Service, extraction, deduplication and SQLite store) over an
injected fake of serpapi.com; nothing touches the network, and the fake counts every billable search it is asked for.
"""
import json
import sqlite3
import threading
import types
import unittest
import zlib
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from area_events.contracts.models import SettingsRecord
from area_events.control.service import CreateAreaRequest, CreateSourceRequest, SettingsRequest, UpdateSourceRequest
from area_events.discovery import producer as contract
from area_events.discovery.fetching import RecordingFetcher
from area_events.discovery.producers.serpapi_google_events import (FREE_TIER_RESERVE, SEARCH_URL, SerpApiAccount, SerpApiGoogleEventsProducer, Unusable,
                                                                   UsageBlocked, decide, parse_when, read_account)
from area_events.discovery.safe import BlockedError, FetchError, HostPolicy, SafeFetcher
from tests.records import document
from tests.test_extraction import Fixture

KEY = "serpapi-test-key-0123456789abcdef0123456789abcdef"
NOW = int(datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc).timestamp())    # Saturday October 31, 2026, 09:00 in San Diego
JSON = {"Content-Type": "application/json; charset=utf-8"}
SD = {"query": "events in San Diego", "location": "San Diego, California, United States", "timezone": "America/Los_Angeles", "name": "SerpApi San Diego",
      "source_key": "serpapi_sd"}


def result(title, when, **extra):
    return {"title": title, "date": {"start_date": when.split(",")[1].strip() if "," in when else when, "when": when},
            "address": ["Balboa Park, 1549 El Prado", "San Diego, CA"], "link": f"https://example.org/{title.lower().replace(' ', '-')}",
            "description": f"{title} description.", "venue": {"name": "Balboa Park"}, "thumbnail": "https://encrypted-tbn0.gstatic.com/t.jpg", **extra}


VALID = [result("Holiday Market", "Sat, Nov 7, 10 AM – 4 PM", image="https://example.org/market.jpg"),
         result("Jazz Night", "Fri, Nov 6, 7 – 10 PM"),
         result("Late Show", "Sat, Nov 7, 10 PM – Sun, Nov 8, 2 AM"),
         result("Year End Gala", "Wed, Dec 30, 2026, 6 PM")]
UNUSABLE = [result("No Weekday", "Nov 7, 10 AM"), result("No Time", "Sat, Nov 7"), result("Tonight Only", "Today, 7 – 9 PM"),
            result("Wrong Weekday", "Fri, Nov 7, 10 AM"), result("Three Days", "Thu, Nov 12 – Sat, Nov 14"), {"date": {"when": "Sat, Nov 7, 10 AM"}},
            result("Odd Hours", "Sat, Nov 7, 11 – 1 PM")]


def page(results, more=False, extra=None):
    body = {"search_metadata": {"id": "6721abc", "status": "Success", "json_endpoint": "https://serpapi.com/searches/6721abc/x.json"},
            "search_parameters": {"engine": "google_events", "q": "events in San Diego"}, "events_results": results, **(extra or {})}
    if more:
        body["serpapi_pagination"] = {"next": "https://serpapi.com/search.json?engine=google_events&start=10"}
    return body


def resolver(host, port, type=None):
    return [(None, None, None, None, ("93.184.216.34", port))]


class FakeSerpApi:
    """serpapi.com: robots.txt (none), the Account API (which reports the key back, as the real one does) and the search, which costs one search."""

    def __init__(self, used=10, spm=250, left=None, price=0, renewal="2026-11-20", status="Active", lag=False, pages=None, account=None, search=None, robots=None):
        self.used, self.spm, self.left, self.price, self.renewal, self.status, self.lag = used, spm, left, price, renewal, status, lag
        self.robots = robots
        self.pages, self.account, self.search = pages or [page(VALID + UNUSABLE)], account, search
        self.searches, self.reads, self.targets, self.lock = 0, 0, [], threading.Lock()

    def __call__(self, scheme, host, port, ip, target, headers, timeout, limit):
        with self.lock:
            self.targets.append(target)
            path, _, query = target.partition("?")
            params = dict(parse_qsl(query))
            if path == "/robots.txt":
                return (200, {"Content-Type": "text/plain"}, self.robots.encode()) if self.robots else (404, {}, b"")
            if path == "/account.json":
                self.reads += 1
                if callable(self.account):
                    return self.account(params)
                body = {"account_id": "acc-42", "api_key": params.get("api_key"), "account_email": "owner@example.org", "account_status": self.status,
                        "plan_id": "free", "plan_name": "Free Plan", "plan_monthly_price": self.price, "searches_per_month": self.spm,
                        "plan_searches_left": self.spm - self.used if self.left is None else self.left, "extra_credits": 0,
                        "this_month_usage": self.used, "plan_renewal_date": self.renewal}
                return 200, dict(JSON), json.dumps(body).encode()
            if path == "/search":
                assert params.get("api_key") == KEY, "the key reaches the request line"
                self.searches += 1
                if not self.lag:
                    self.used += 1
                if callable(self.search):
                    return self.search(params)
                number = int(params.get("start", 0)) // 10
                return 200, dict(JSON), json.dumps(self.pages[min(number, len(self.pages) - 1)]).encode()
            return 404, {}, b""


def policy(auto_pause=True, pause_at=200):
    return lambda: types.SimpleNamespace(serpapi_auto_pause=auto_pause, serpapi_pause_at=pause_at)


def setup(fake=None, auto_pause=True, pause_at=200, key=KEY):
    fake = fake or FakeSerpApi()
    client = SafeFetcher(0, resolver=resolver, send=fake, sleep=lambda s: None, state=HostPolicy())
    account = SerpApiAccount(client, key, policy(auto_pause, pause_at), clock=lambda: NOW)
    return fake, client, account, SerpApiGoogleEventsProducer(account, clock=lambda: NOW)


def crawl(producer, client, options=SD):
    recording = RecordingFetcher(client, 10)
    return producer.crawl(recording, (), dict(options)), recording


def carries_key(*items):
    return any(KEY in json.dumps(item, default=str) for item in items)


# --- producer ----------------------------------------------------------------------------------------------------------

class ResultsBecomeStandardCandidates(unittest.TestCase):
    def test_stated_results_become_valid_candidates_and_the_rest_are_set_aside_with_reasons(self):
        fake, client, account, producer = setup()
        answer, recording = crawl(producer, client)
        accepted = contract.accept(answer)
        self.assertEqual(accepted.rejections, (), "everything produced passes the standard boundary")
        by_title = {c.title: c for c in accepted.candidates}
        self.assertEqual(sorted(by_title), ["Holiday Market", "Jazz Night", "Late Show", "Year End Gala"])
        market = by_title["Holiday Market"]
        self.assertEqual((market.starts_at, market.ends_at, market.timezone), ("2026-11-07T10:00:00-08:00", "2026-11-07T16:00:00-08:00", "America/Los_Angeles"))
        self.assertEqual((market.venue, market.address, market.url), ("Balboa Park", "Balboa Park, 1549 El Prado, San Diego, CA", "https://example.org/holiday-market"))
        self.assertEqual((market.image_url, market.description), ("https://example.org/market.jpg", "Holiday Market description."))
        self.assertEqual((by_title["Jazz Night"].starts_at, by_title["Jazz Night"].ends_at), ("2026-11-06T19:00:00-08:00", "2026-11-06T22:00:00-08:00"), "7 – 10 PM")
        self.assertEqual((by_title["Late Show"].starts_at, by_title["Late Show"].ends_at), ("2026-11-07T22:00:00-08:00", "2026-11-08T02:00:00-08:00"))
        self.assertEqual((by_title["Year End Gala"].starts_at, by_title["Year End Gala"].ends_at), ("2026-12-30T18:00:00-08:00", None), "no end stated: none invented")
        for candidate in accepted.candidates:
            self.assertEqual((candidate.verification.status, candidate.verification.verified), ("unverified", False), "aggregated results are not verified")
            self.assertEqual(candidate.provenance.listing_url, SerpApiGoogleEventsProducer.search_url(SD))
            self.assertNotIn("api_key", candidate.provenance.listing_url)
        self.assertTrue(any("Year 2026: the year in which November 7 falls on the stated Saturday" in line for line in market.verification.evidence))
        self.assertEqual((answer.notes["results"], answer.notes["candidates"], answer.notes["set_aside"], answer.notes["searches"]), (11, 4, 7, 1))
        reasons = " ".join(str(v) for k, v in answer.notes.items() if k.startswith("set_aside_"))
        for reason in ("neither a year nor a weekday", "no start time", "relative days", "no single year", "no title", "does not say AM or PM"):
            self.assertIn(reason, reasons)
        self.assertEqual(fake.searches, 1)

    def test_identities_do_not_depend_on_result_order_or_search_tokens(self):
        _, client, _, producer = setup(FakeSerpApi(pages=[page(VALID)]))
        first = {c.title: c.local_id for c in crawl(producer, client)[0].candidates}
        reordered = page(list(reversed(VALID)), extra={"search_metadata": {"id": "ffff999", "status": "Success"}})
        _, client, _, producer = setup(FakeSerpApi(pages=[reordered]))
        self.assertEqual({c.title: c.local_id for c in crawl(producer, client)[0].candidates}, first)
        self.assertEqual(len(set(first.values())), 4)

    def test_a_year_is_only_taken_from_the_stated_weekday_within_a_year_of_the_search(self):
        zone = __import__("zoneinfo").ZoneInfo("America/New_York")
        start, end, derived = parse_when("Sat, Jan 2, 11 AM – 1 PM", zone, datetime(2026, 12, 28).date())
        self.assertEqual((start.isoformat(), end.isoformat(), derived), ("2027-01-02T11:00:00-05:00", "2027-01-02T13:00:00-05:00", True))
        with self.assertRaises(Unusable):   # November 7 is a Saturday in 2026, more than a year after June 1, 2025, and a Friday in 2025: no year
            parse_when("Sat, Nov 7, 11 AM", zone, datetime(2025, 6, 1).date())
        with self.assertRaises(Unusable):
            parse_when("Sat, Nov 7, 2027, 10 AM", zone, datetime(2026, 10, 31).date())   # the stated year contradicts the weekday
        start, _, derived = parse_when("Sun, Nov 7, 2027, 10 AM", zone, datetime(2026, 10, 31).date())
        self.assertEqual((start.year, derived), (2027, False))

    def test_pagination_is_bounded_and_each_page_is_one_counted_search(self):
        full = page([result(f"Event {i}", f"Sat, Nov 7, {i % 12 + 1} PM") for i in range(10)], more=True)
        fake, client, _, producer = setup(FakeSerpApi(pages=[full, full, full, full]))
        answer, _ = crawl(producer, client, {**SD, "max_pages": 2})
        self.assertEqual((fake.searches, answer.notes["searches"], answer.notes["pages"]), (2, 2, 2))
        self.assertIn("start=10", [t for t in fake.targets if t.startswith("/search")][1])
        fake, client, _, producer = setup(FakeSerpApi(pages=[full, full]))
        crawl(producer, client)
        self.assertEqual(fake.searches, 1, "one page unless more are configured")
        fake, client, _, producer = setup(FakeSerpApi(pages=[page(VALID, more=True)]))
        crawl(producer, client, {**SD, "max_pages": 3})
        self.assertEqual(fake.searches, 1, "a short page ends the scan")
        with self.assertRaises(ValueError):
            SerpApiGoogleEventsProducer.validate_options({**{k: SD[k] for k in ("query", "location", "timezone")}, "max_pages": 4})

    def test_options_are_validated(self):
        base = {k: SD[k] for k in ("query", "location", "timezone")}
        self.assertEqual(SerpApiGoogleEventsProducer.validate_options(base), (SEARCH_URL, base))
        for bad in ({**base, "timezone": "Pacific Time"}, {**base, "timezone": "Mars/Olympus_Mons"}, {**base, "query": ""}, {k: v for k, v in base.items() if k != "location"},
                    {**base, "api_key": KEY}, {**base, "max_pages": True}, {**base, "url": "https://evil.example/search"}):
            with self.assertRaises(ValueError):
                SerpApiGoogleEventsProducer.validate_options(bad)

    def test_an_empty_search_is_no_results_and_another_error_is_a_failure(self):
        _, client, _, producer = setup(FakeSerpApi(pages=[{"error": "Google hasn't returned any results for this query."}]))
        self.assertEqual(crawl(producer, client)[0].candidates, ())
        _, client, _, producer = setup(FakeSerpApi(pages=[{"error": "Invalid location."}]))
        with self.assertRaises(FetchError):
            crawl(producer, client)


# --- credentials ---------------------------------------------------------------------------------------------------

class TheKeyStaysOut(unittest.TestCase):
    def test_the_account_api_reports_the_key_back_and_it_is_dropped_before_anything_reads_it(self):
        fake, client, account, _ = setup()
        usage = account.check()
        self.assertEqual((usage.used, usage.left, usage.searches_per_month, usage.monthly_price, usage.renewal), (10, 240, 250, 0.0, "2026-11-20"))
        raw = client.fetch("https://serpapi.com/account.json", secret_query={"api_key": KEY}, drop_fields=("api_key", "account_email", "account_id"))
        self.assertFalse(carries_key(raw.text, raw.url))
        self.assertNotIn("owner@example.org", raw.text)
        with self.assertRaises(BlockedError):   # without dropping the field, the echo is refused as before
            client.fetch("https://serpapi.com/account.json", secret_query={"api_key": KEY})

    def test_the_key_anywhere_else_in_an_answer_is_refused_and_no_search_follows(self):
        echoing = FakeSerpApi(account=lambda params: (200, dict(JSON), json.dumps({"api_key": params["api_key"], "note": "key " + params["api_key"],
                                                                                    "plan_monthly_price": 0, "searches_per_month": 250, "this_month_usage": 1,
                                                                                    "plan_searches_left": 249}).encode()))
        _, client, account, producer = setup(echoing)
        with self.assertRaises(UsageBlocked) as caught:
            crawl(producer, client)
        self.assertEqual(caught.exception.state, "usage_unavailable")
        self.assertFalse(carries_key(str(caught.exception)))
        self.assertEqual(echoing.searches, 0)
        search_echo = FakeSerpApi(search=lambda params: (200, dict(JSON), json.dumps(page(VALID, extra={"debug": params["api_key"]})).encode()))
        _, client, account, producer = setup(search_echo)
        recording = RecordingFetcher(client, 10)
        with self.assertRaises(BlockedError):
            producer.crawl(recording, (), dict(SD))
        self.assertEqual([(p.outcome, p.body) for p in recording.fetches], [("blocked", None)])
        self.assertFalse(carries_key([vars(p) for p in recording.fetches]))

    def test_a_redirected_or_failing_account_api_makes_no_search(self):
        for answer in ((302, {"Location": "https://elsewhere.example/"}, b""), (500, dict(JSON), b"{}"), (200, {"Content-Type": "text/html"}, b"<html>")):
            with self.subTest(status=answer[0]):
                fake = FakeSerpApi(account=lambda params, answer=answer: answer)
                _, client, _, producer = setup(fake)
                with self.assertRaises(UsageBlocked) as caught:
                    crawl(producer, client)
                self.assertEqual((caught.exception.state, fake.searches), ("usage_unavailable", 0))
                self.assertFalse(carries_key(str(caught.exception)))

    def test_recorded_pages_hold_the_public_search_only(self):
        _, client, _, producer = setup()
        _, recording = crawl(producer, client)
        [fetched] = recording.fetches
        self.assertEqual((fetched.url, fetched.outcome), (SerpApiGoogleEventsProducer.search_url(SD), "ok"))
        self.assertFalse(carries_key(fetched.url, fetched.final_url, fetched.body.decode(), fetched.error))
        self.assertEqual(len(recording.fetches), 1, "the account check is not a recorded page")

    def test_serpapi_robots_txt_as_published_refuses_the_search_before_it_is_sent(self):
        published = "User-agent: *\nDisallow: /samples/\nDisallow: /searches/\nDisallow: /search?\nDisallow: /search.json\nDisallow: /search.html\n"   # read 2026-10-31
        fake = FakeSerpApi(robots=published)
        _, client, account, producer = setup(fake)
        with self.assertRaises(BlockedError) as caught:
            crawl(producer, client)
        self.assertIn("robots.txt", str(caught.exception))
        self.assertEqual(fake.searches, 0, "the shared fetcher honours robots.txt for every request: nothing billable was sent")
        self.assertEqual(fake.reads, 1, "the Account API (/account.json) is allowed")
        self.assertFalse(any(t.startswith("/search") for t in fake.targets))

    def test_without_a_key_nothing_is_asked(self):
        fake, client, account, producer = setup(key=None)
        with self.assertRaises(UsageBlocked):
            crawl(producer, client)
        self.assertEqual(fake.targets, [])
        self.assertEqual(account.report()["state"], "usage_unavailable")


# --- quota -------------------------------------------------------------------------------------------------------

class FreeTierProtection(unittest.TestCase):
    def blocked(self, fake, **kwargs):
        _, client, account, producer = setup(fake, **kwargs)
        with self.assertRaises(UsageBlocked) as caught:
            crawl(producer, client)
        self.assertEqual(fake.searches, 0, "no billable search")
        return caught.exception.state, str(caught.exception)

    def test_the_account_report_shows_the_provider_numbers(self):
        _, _, account, _ = setup(FakeSerpApi(used=124))
        report = account.report()
        self.assertEqual(report["account"], {"plan_name": "Free Plan", "monthly_price": 0.0, "searches_per_month": 250, "used": 124, "remaining": 126,
                                             "renewal": "2026-11-20", "checked_at": NOW})
        self.assertEqual((report["state"], report["stale"], report["policy"]), ("active", False, {"auto_pause": True, "pause_at": 200, "free_tier_reserve": FREE_TIER_RESERVE}))
        self.assertFalse(carries_key(report))

    def test_searches_stop_at_the_threshold_while_automatic_pause_is_on(self):
        state, reason = self.blocked(FakeSerpApi(used=200))
        self.assertEqual(state, "paused_threshold")
        self.assertIn("200 searches used", reason)
        self.assertEqual(self.blocked(FakeSerpApi(used=150), pause_at=150)[0], "paused_threshold", "the threshold is configurable")
        fake, client, _, producer = setup(FakeSerpApi(used=200), auto_pause=False)
        crawl(producer, client)
        self.assertEqual(fake.searches, 1, "with automatic pause off, the threshold does not apply")

    def test_the_hard_stop_applies_whatever_the_policy(self):
        for auto_pause, pause_at in ((False, 200), (True, 10000), (False, 1)):
            with self.subTest(auto_pause=auto_pause, pause_at=pause_at):
                state, reason = self.blocked(FakeSerpApi(used=250 - FREE_TIER_RESERVE), auto_pause=auto_pause, pause_at=pause_at)
                self.assertEqual(state, "blocked_free_tier")
                self.assertIn(f"the last {FREE_TIER_RESERVE} are never used", reason)
        self.assertEqual(self.blocked(FakeSerpApi(used=10, left=3), auto_pause=False)[0], "blocked_free_tier", "the provider's own 'left' counts too")
        fake, client, _, producer = setup(FakeSerpApi(used=250 - FREE_TIER_RESERVE - 1), auto_pause=False)
        crawl(producer, client)
        self.assertEqual(fake.searches, 1, "one above the reserve may still be used")

    def test_a_paid_plan_or_an_unreadable_account_makes_no_search(self):
        self.assertEqual(self.blocked(FakeSerpApi(price=75))[0], "blocked_free_tier")
        self.assertIn("paid plan", self.blocked(FakeSerpApi(price="$50"))[1])
        self.assertEqual(self.blocked(FakeSerpApi(status="Suspended"))[0], "usage_unavailable")
        for broken in ({"plan_monthly_price": 0, "searches_per_month": 250, "plan_searches_left": 200},
                       {"plan_monthly_price": None, "searches_per_month": 250, "plan_searches_left": 200, "this_month_usage": 50},
                       {"plan_monthly_price": 0, "searches_per_month": "250", "plan_searches_left": 200, "this_month_usage": 50},
                       {"plan_monthly_price": 0, "searches_per_month": 250, "plan_searches_left": -1, "this_month_usage": 50},
                       {"error": "Invalid API key."}, ["not", "an", "object"]):
            with self.subTest(broken=broken):
                fake = FakeSerpApi(account=lambda params, body=broken: (200, dict(JSON), json.dumps(body).encode()))
                self.assertEqual(self.blocked(fake)[0], "usage_unavailable")
        self.assertEqual(read_account({"plan_monthly_price": "0", "searches_per_month": 250, "plan_searches_left": 1, "this_month_usage": 249}, NOW).monthly_price, 0.0)

    def test_every_source_shares_one_allowance_and_searches_the_provider_has_not_counted_yet_still_count(self):
        fake = FakeSerpApi(used=197, lag=True)    # the provider's count does not move: only this process knows what it spent
        _, client, account, _ = setup(fake)
        san_diego, austin = SerpApiGoogleEventsProducer(account, lambda: NOW), SerpApiGoogleEventsProducer(account, lambda: NOW)
        outcomes = []
        for producer, options in ((san_diego, SD), (austin, {**SD, "query": "events in Austin", "timezone": "America/Chicago"}), (san_diego, SD), (austin, SD)):
            try:
                crawl(producer, client, options)
                outcomes.append("searched")
            except UsageBlocked as blocked:
                outcomes.append(blocked.state)
        self.assertEqual(outcomes, ["searched", "searched", "searched", "paused_threshold"])
        self.assertEqual(fake.searches, 3, "197 + 3 = 200: one shared threshold, not one per city")

    def test_concurrent_searches_cannot_overspend(self):
        fake = FakeSerpApi(used=250 - FREE_TIER_RESERVE - 4, lag=True)
        _, client, account, producer = setup(fake, auto_pause=False)
        start, results = threading.Barrier(12), []

        def worker():
            start.wait()
            try:
                crawl(producer, client)
                results.append("searched")
            except UsageBlocked:
                results.append("blocked")
        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual((fake.searches, results.count("searched"), results.count("blocked")), (4, 4, 8))

    def test_a_new_cycle_confirmed_by_the_provider_lets_searches_resume(self):
        fake = FakeSerpApi(used=250 - FREE_TIER_RESERVE, renewal="2026-11-20")
        _, client, account, producer = setup(fake)
        with self.assertRaises(UsageBlocked):
            crawl(producer, client)
        fake.used, fake.renewal = 0, "2026-12-20"     # the provider reports the new cycle
        crawl(producer, client)
        self.assertEqual((fake.searches, account.report()["state"]), (1, "active"))

    def test_after_the_renewal_date_an_unrenewed_allowance_is_waiting_not_resumed(self):
        usage = read_account({"plan_monthly_price": 0, "searches_per_month": 250, "plan_searches_left": 50, "this_month_usage": 200, "plan_renewal_date": "2026-10-30"}, NOW)
        self.assertEqual(decide(usage, True, 200, today=datetime(2026, 10, 31).date())[0], "waiting_for_renewal")
        self.assertEqual(decide(usage, True, 200, today=datetime(2026, 10, 29).date())[0], "paused_threshold")
        self.assertEqual(decide(usage, False, 200, today=datetime(2026, 10, 31).date())[0], "active", "nothing to wait for while searches are allowed")

    def test_the_report_marks_a_failed_reading_unavailable_and_keeps_the_last_numbers_marked_stale(self):
        fake = FakeSerpApi(used=40)
        _, client, _, _ = setup(fake)
        clock = [NOW]
        account = SerpApiAccount(client, KEY, policy(), clock=lambda: clock[0])
        self.assertEqual(account.report()["account"]["used"], 40)
        fake.account = lambda params: (500, dict(JSON), b"{}")
        clock[0] += 3600
        report = account.report()
        self.assertEqual((report["state"], report["stale"], report["account"]["used"]), ("usage_unavailable", True, 40))
        self.assertIn("could not be read", report["error"])


# --- the service, persistence, the control API and Home Assistant ---------------------------------------------------------

def database_text(path):
    """Every value in every table, page bodies decompressed: what the database holds."""
    connection = sqlite3.connect(path)
    try:
        found = []
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in connection.execute(f"SELECT * FROM {table}"):
                for value in row:
                    if isinstance(value, bytes):
                        try:
                            value = zlib.decompress(value)
                        except zlib.error:
                            pass
                        value = value.decode("utf-8", "replace")
                    found.append(str(value))
        return "\n".join(found)
    finally:
        connection.close()


class ThroughTheService(Fixture):
    def setUp(self):
        super().setUp()
        self.clock.t = NOW
        self.fake = FakeSerpApi(pages=[page(VALID + UNUSABLE)])
        client = SafeFetcher(0, resolver=resolver, send=self.fake, sleep=lambda s: None, state=HostPolicy())
        self.built = self.build(fetch_client=client, serpapi_api_key=KEY)
        self.source = self.built.control.create_source(CreateSourceRequest("serpapi_sd", "serpapi_google_events", "SerpApi San Diego", True, 60,
                                                                           {k: SD[k] for k in ("query", "location", "timezone")})).source
        self.built.control.create_area(CreateAreaRequest("San Diego", 32.7157, -117.1611, 25, sources=("serpapi_sd", "sandiego_gov")))

    def scan(self):
        self.built.scheduler.plan()   # the new source's job, as the host's planning step makes it
        self.built.service.make_due("serpapi_sd")
        reports = [r for r in self.built.service.run_until_idle(10) if r.job_key and "serpapi_sd" in r.job_key]
        self.drain(self.built)
        return reports

    def test_candidates_pass_the_standard_pipeline_and_a_second_scan_changes_nothing(self):
        [first] = self.scan()
        self.assertEqual(first.outcome, "completed")
        events = {e.title: e for e in self.all_events(self.built)}
        self.assertLessEqual({"Holiday Market", "Jazz Night", "Late Show", "Year End Gala"}, set(events))
        self.assertEqual(events["Holiday Market"].starts_at, int(datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc).timestamp()))
        before = {(e.event_key, e.revision) for e in self.all_events(self.built)}
        candidates = self.count("candidates", "candidate_key LIKE 'serpapi_sd:%'")
        [second] = self.scan()
        self.assertEqual(second.outcome, "completed")
        self.assertEqual({(e.event_key, e.revision) for e in self.all_events(self.built)}, before, "same events, same revisions")
        self.assertEqual(self.count("candidates", "candidate_key LIKE 'serpapi_sd:%'"), candidates)
        self.assertEqual(self.fake.searches, 2)
        text = database_text(self.db)
        self.assertNotIn(KEY, text, "the key is nowhere in the database")
        self.assertNotIn("owner@example.org", text)

    def test_an_event_another_producer_already_has_is_linked_and_the_existing_url_veto_still_applies(self):
        market = dict(starts_at="2026-11-07T10:00:00-08:00", ends_at="2026-11-07T16:00:00-08:00", venue="Balboa Park", address="Balboa Park, 1549 El Prado, San Diego, CA")
        self.add_candidate(self.built, "sandiego_gov", "sandiego_gov:market", document("Holiday Market", url="https://example.org/holiday-market", **market))
        self.add_candidate(self.built, "sandiego_gov", "sandiego_gov:jazz", document("Jazz Night", url="https://other.example/jazz", starts_at="2026-11-06T19:00:00-08:00",
                                                                                     ends_at="2026-11-06T22:00:00-08:00", venue="Balboa Park",
                                                                                     address="Balboa Park, 1549 El Prado, San Diego, CA"))
        self.drain(self.built)
        self.scan()
        markets = [e for e in self.all_events(self.built) if e.title == "Holiday Market"]
        self.assertEqual(len(markets), 1, "the same event page: one event, now described by both sources")
        self.assertEqual(self.count("event_candidates ec JOIN events e USING (event_id)", f"e.event_key = '{markets[0].event_key}'"), 2)
        self.assertEqual([e.title for e in self.all_events(self.built)].count("Jazz Night"), 2,
                         "different event pages are never merged (the existing deduplication rule is unchanged)")

    def test_a_scan_the_account_does_not_allow_is_released_without_touching_anything_or_the_enabled_flag(self):
        self.scan()
        before = {(e.event_key, e.revision) for e in self.all_events(self.built)}
        self.fake.used = 200
        [report] = self.scan()
        self.assertEqual(report.outcome, "fetch_failed")
        self.assertIn("automatic pause", report.error)
        self.assertEqual(self.fake.searches, 1, "no search while paused")
        self.assertEqual({(e.event_key, e.revision) for e in self.all_events(self.built)}, before, "what was found stays")
        self.assertTrue(self.built.control.get_source("serpapi_sd").enabled, "quota never changes the manual setting")
        status = {s.source_key: s for s in self.built.control.get_status().sources}["serpapi_sd"]
        self.assertIn("automatic pause", status.last_error)
        self.fake.used = 0                                       # a new cycle
        [resumed] = self.scan()
        self.assertEqual((resumed.outcome, self.fake.searches), ("completed", 2))

    def test_the_policy_is_one_shared_setting_and_the_hard_stop_is_not_a_setting(self):
        settings = self.built.control.get_status().settings
        self.assertEqual((settings.serpapi_auto_pause, settings.serpapi_pause_at), (True, 200), "defaults")
        result = self.built.control.set_discovery_mode(SettingsRequest(settings.revision, serpapi_auto_pause=False, serpapi_pause_at=180))
        self.assertEqual((result.settings.serpapi_auto_pause, result.settings.serpapi_pause_at), (False, 180))
        report = self.built.serpapi.report(self.built.control.list_sources())
        self.assertEqual(report["policy"], {"auto_pause": False, "pause_at": 180, "free_tier_reserve": FREE_TIER_RESERVE})
        replay = self.built.control.set_discovery_mode(SettingsRequest(result.settings.revision, serpapi_auto_pause=False, serpapi_pause_at=180))
        self.assertTrue(replay.replayed)
        from area_events.contracts.models import ValidationFailed
        for bad in ({"serpapi_pause_at": 0}, {"serpapi_pause_at": 10001}, {"serpapi_pause_at": True}, {"serpapi_auto_pause": "yes"}):
            with self.assertRaises(ValidationFailed):
                self.built.control.set_discovery_mode(SettingsRequest(replay.settings.revision, **bad))
        self.assertFalse(hasattr(SettingsRecord, "serpapi_hard_stop"))
        disabled = self.built.control.update_source(UpdateSourceRequest("serpapi_sd", self.source.revision, enabled=False))
        report = self.built.serpapi.report(self.built.control.list_sources())
        self.assertEqual(report["sources"]["serpapi_sd"]["state"], "manually_disabled")
        self.assertFalse(disabled.source.enabled)


class OverTheApiAndHomeAssistant(ThroughTheService):
    def test_usage_and_policy_over_http_and_in_the_management_snapshot_without_the_key(self):
        from area_events import app as app_module
        from area_events.api.http_api import ApiTokens
        from tests.test_discovery_control import CONTROL_TOKEN, READ_TOKEN, request
        from tests.test_extraction import PROCESS_TOKEN
        server = app_module.build_api(self.built, ApiTokens(CONTROL_TOKEN, READ_TOKEN, PROCESS_TOKEN), clock=self.clock, port=0).start()
        self.addCleanup(server.stop)
        status, usage = request(server, "GET", "/v1/usage", token=READ_TOKEN)
        self.assertEqual((status, usage["schema"], usage["serpapi"]["account"]["remaining"], usage["serpapi"]["sources"]["serpapi_sd"]["state"]),
                         (200, "discovery.usage.v1", 240, "active"))
        self.assertFalse(carries_key(usage))
        revision = request(server, "GET", "/v1/discovery/status", token=READ_TOKEN)[1]["status"]["settings"]["revision"]
        self.assertEqual(request(server, "PUT", "/v1/discovery/settings", token=READ_TOKEN, body={"expected_revision": revision, "serpapi_pause_at": 150})[0], 403)
        status, saved = request(server, "PUT", "/v1/discovery/settings", token=CONTROL_TOKEN, body={"expected_revision": revision, "serpapi_auto_pause": True,
                                                                                                   "serpapi_pause_at": 150})
        self.assertEqual((status, saved["settings"]["serpapi_pause_at"]), (200, 150))
        self.assertEqual(request(server, "PUT", "/v1/discovery/settings", token=CONTROL_TOKEN,
                                 body={"expected_revision": saved["revision"], "serpapi_hard_stop": False})[0], 400, "the hard stop cannot be turned off")
        self.assertEqual(request(server, "GET", "/v1/usage", token=READ_TOKEN)[1]["serpapi"]["policy"]["pause_at"], 150)
        sources = request(server, "GET", "/v1/sources", token=READ_TOKEN)[1]["sources"]
        self.assertFalse(carries_key(sources, request(server, "GET", "/v1/discovery/status", token=READ_TOKEN)[1]))

        from tests.test_ha_integration import CountingClient, UrllibSession, client_module, hub_module, run
        url = "http://%s:%s" % server.address[:2]
        session = UrllibSession()
        hub = hub_module.DiscoveryHub(CountingClient(client_module.DiscoveryControlClient(session, url, READ_TOKEN)),
                                      CountingClient(client_module.DiscoveryControlClient(session, url, CONTROL_TOKEN)), clock=self.clock)
        snapshot = run(hub.management_snapshot())
        self.assertEqual(snapshot["usage"]["serpapi"]["account"]["used"], self.fake.used)
        self.assertFalse(carries_key(snapshot))
        revision = snapshot["status"]["settings"]["revision"]
        run(hub.set_settings(revision, serpapi_auto_pause=False, serpapi_pause_at=120))
        self.assertEqual(run(hub.management_snapshot())["usage"]["serpapi"]["policy"], {"auto_pause": False, "pause_at": 120, "free_tier_reserve": FREE_TIER_RESERVE})


if __name__ == "__main__":
    unittest.main()
