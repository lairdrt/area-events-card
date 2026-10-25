"""User-supplied website discovery: the generic web adapter, its parsers and the safety rules of the fetcher."""
import gzip
import http.server
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from area_events.discovery import engine as engine_module
from area_events.discovery import refresh as refresh_module
from area_events.discovery import registry, safe
from area_events.discovery.core import FetchError
from area_events.discovery.profiles import normalize_url
from area_events.discovery.publish import validate_feed
from area_events.discovery.sources import web, webfeeds, webparse
from area_events.discovery.sources.webtime import parse_when, zone
from tests.test_engine import Clock, FakeRefresh, fake_geocode

AUSTIN = {"latitude": 30.2649, "longitude": -97.7474, "radius_miles": 25, "name": "Austin, TX", "timezone": "America/Chicago"}
SITE = "https://events.example.org"


def when(days, hour=19, offset=None):
    """A future local wall-clock time as ISO text (with an offset when given)."""
    moment = (datetime.now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + (offset or "")


def ics_stamp(days, hour=19, zulu=False):
    return (datetime.now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0).strftime("%Y%m%dT%H%M%S") + ("Z" if zulu else "")


def ld(*nodes):
    return '<script type="application/ld+json">' + json.dumps(list(nodes) if len(nodes) != 1 else nodes[0]) + "</script>"


def html(body="", head=""):
    return f"<!doctype html><html><head><title>Events</title>{head}</head><body>{body}</body></html>"


def event(name, days=3, **extra):
    node = {"@type": "Event", "name": name, "startDate": when(days, offset="-05:00"), "url": f"{SITE}/events/{name.lower().replace(' ', '-')}"}
    node.update(extra)
    return node


class Reply:
    def __init__(self, url, text, content_type="text/html"):
        self.url, self.text, self.content_type, self.status = url, text, content_type, 200


class FakeClient:
    """Stands in for SafeFetcher: pages by URL, every request recorded."""

    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def fetch(self, url, check_robots=True):
        self.urls.append(url)
        entry = self.pages.get(url)
        if entry is None:
            raise FetchError(f"{url}: HTTP 404")
        if isinstance(entry, Exception):
            raise entry
        text, kind = entry if isinstance(entry, tuple) else (entry, "text/html")
        return Reply(url, text, kind)


def run(pages, url=SITE + "/events", location=AUSTIN, **kwargs):
    web.clear_cache()
    client = FakeClient(pages)
    events, notes = web.discover(client, [], url=url, location=location, source_id="web_t", source_name="Example", **kwargs)
    return events, notes, client


class Times(unittest.TestCase):
    def test_offsets_are_exact_and_naive_times_use_the_named_or_area_zone(self):
        self.assertEqual(parse_when("2026-12-01T19:00:00Z")[0].utcoffset(), timedelta(0))
        self.assertEqual(parse_when("2026-12-01T19:00:00-0500")[0].utcoffset(), timedelta(hours=-5))
        chicago, _ = parse_when("2026-07-01T19:00:00", None, "America/Chicago")
        self.assertEqual(chicago.utcoffset(), timedelta(hours=-5))  # daylight time
        self.assertEqual(parse_when("2026-12-01T19:00:00", None, "America/Chicago")[0].utcoffset(), timedelta(hours=-6))
        self.assertEqual(parse_when("2026-07-01T19:00:00", "America/Denver", "America/Chicago")[0].utcoffset(), timedelta(hours=-6))

    def test_unusable_times_are_refused_with_a_reason_not_guessed(self):
        self.assertEqual(parse_when("2026-12-01")[1], "date_only")
        self.assertEqual(parse_when("2026-12-01T19:00:00")[1], "no_timezone")
        self.assertEqual(parse_when("2026-12-01T19:00:00", "Mars/Olympus")[1], "no_timezone")
        self.assertEqual(parse_when("tomorrow evening")[1], "invalid_date")
        self.assertEqual(parse_when("2026-13-45T10:00:00Z")[1], "invalid_date")

    def test_us_zones_work_without_system_tzdata(self):
        with mock.patch("area_events.discovery.sources.webtime.ZoneInfo", None):
            self.assertEqual(parse_when("2026-07-01T09:00:00", "America/Los_Angeles")[0].utcoffset(), timedelta(hours=-7))
            self.assertEqual(parse_when("2026-01-01T09:00:00", "America/Phoenix")[0].utcoffset(), timedelta(hours=-7))
            self.assertIsNone(zone("Europe/Berlin"))


class JsonLd(unittest.TestCase):
    def test_graph_with_referenced_venue_nested_events_and_lists(self):
        graph = {"@context": "https://schema.org", "@graph": [
            {"@type": "WebSite", "name": "Site"},
            {"@type": "Place", "@id": "#venue", "name": "Paramount Theatre", "address": {"@type": "PostalAddress", "streetAddress": "713 Congress Ave",
                                                                                         "addressLocality": "Austin", "addressRegion": "TX", "postalCode": "78701"},
             "geo": {"latitude": 30.2685, "longitude": -97.7413}},
            event("Opening Night", location={"@id": "#venue"}, image=[{"@type": "ImageObject", "url": "/img/a.jpg"}], organizer={"@type": "Organization", "name": "Hall Co"},
                  offers={"@type": "Offer", "price": "25.00", "priceCurrency": "USD"}, description="<p>An <b>evening</b> &amp; more.</p>"),
            {"@type": "MusicEvent", "name": "Parent Fest", "startDate": when(4, offset="-05:00"), "url": SITE + "/e/parent",
             "subEvent": [event("Child Set", 4)]}]}
        events, notes, _ = run({SITE + "/events": html(ld(graph))}, details=False)
        by = {e["title"]: e for e in events}
        self.assertEqual(sorted(by), ["Child Set", "Opening Night", "Parent Fest"])
        night = by["Opening Night"]
        self.assertEqual((night["venue"], night["address"], night["organizer"], night["price"]), ("Paramount Theatre", "713 Congress Ave, Austin, TX 78701", "Hall Co", "$25"))
        self.assertEqual(night["image_url"], SITE + "/img/a.jpg")
        self.assertEqual(night["description"], "An evening & more.")
        self.assertEqual(night["geo"]["latitude"], 30.2685)
        self.assertEqual(by["Parent Fest"]["categories"], ["Music"])
        self.assertTrue(night["start"].endswith("-05:00") and night["end"] is None)
        self.assertEqual(notes["format"], "jsonld")

    def test_nothing_is_invented_for_a_bare_event(self):
        events, _, _ = run({SITE + "/events": html(ld({"@type": "Event", "name": "Bare", "startDate": when(2, offset="Z")}))}, details=False)
        bare = events[0]
        self.assertEqual((bare["venue"], bare["address"], bare["description"], bare["image_url"], bare["end"]), (None, None, None, None, None))
        self.assertEqual(bare["url"], SITE + "/events")  # the page it was found on, since the event names none
        self.assertFalse(bare["verified"])

    def test_malformed_blocks_are_counted_and_good_ones_still_read(self):
        page = html('<script type="application/ld+json">{"@type": "Event", "name": </script>' + ld(event("Fine")))
        events, notes, _ = run({SITE + "/events": page}, details=False)
        self.assertEqual([e["title"] for e in events], ["Fine"])
        self.assertEqual(notes["malformed_jsonld"], 1)

    def test_skips_are_counted_by_reason(self):
        nodes = [event("Gone", eventStatus="https://schema.org/EventCancelled"), event("Virtual", eventAttendanceMode="https://schema.org/OnlineEventAttendanceMode"),
                 {"@type": "Event", "name": "Dated", "startDate": when(2)[:10]}, {"@type": "Event", "name": "Past", "startDate": when(-3, offset="Z")},
                 {"@type": "Event", "name": "Far", "startDate": when(400, offset="Z")}, {"@type": "Event", "name": "Floating", "startDate": when(2)}, event("Keeper")]
        events, notes, _ = run({SITE + "/events": html(ld(*nodes))}, details=False, location={**AUSTIN, "timezone": None})
        self.assertEqual([e["title"] for e in events], ["Keeper"])
        self.assertEqual(notes["skipped"], {"cancelled": 1, "online_only": 1, "date_only": 1, "ended": 1, "beyond_horizon": 1, "no_timezone": 1})

    def test_floating_times_take_the_area_timezone(self):
        events, _, _ = run({SITE + "/events": html(ld({"@type": "Event", "name": "Local", "startDate": when(2)}))}, details=False)
        self.assertEqual(events[0]["timezone"], "America/Chicago")
        self.assertRegex(events[0]["start"], r"-0[56]:00$")

    def test_microdata_events_are_read(self):
        body = ('<div itemscope itemtype="https://schema.org/Event"><a itemprop="url" href="/events/lecture"><span itemprop="name">Lecture</span></a>'
                f'<time itemprop="startDate" datetime="{when(2, offset="-05:00")}">soon</time>'
                '<div itemprop="location" itemscope itemtype="https://schema.org/Place"><span itemprop="name">Hall</span>'
                '<span itemprop="address">1 Main St, Austin, TX 78701</span></div><img itemprop="image" src="/p.jpg"></div>')
        events, notes, _ = run({SITE + "/events": html(body)}, details=False)
        self.assertEqual((events[0]["title"], events[0]["venue"], events[0]["address"], events[0]["image_url"]), ("Lecture", "Hall", "1 Main St, Austin, TX 78701", SITE + "/p.jpg"))
        self.assertEqual(notes["format"], "microdata")


class Calendars(unittest.TestCase):
    def feed(self, *events, extra=""):
        return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-WR-CALNAME:Town Calendar\r\n" + extra + "".join(f"BEGIN:VEVENT\r\n{e}\r\nEND:VEVENT\r\n" for e in events) + "END:VCALENDAR\r\n"

    def read(self, text, **kwargs):
        return run({SITE + "/cal.ics": (text, "text/calendar")}, url=SITE + "/cal.ics", **kwargs)

    def test_ics_fields_escapes_folding_zones_and_geo(self):
        text = self.feed("UID:1\r\nSUMMARY:Band\\, Live\r\nDTSTART;TZID=America/Chicago:" + ics_stamp(3) + "\r\nDTEND;TZID=America/Chicago:" + ics_stamp(3, 21) +
                         "\r\nLOCATION:Mohawk\\, 912 Red River St\\, Austin\\, TX 78701\r\nDESCRIPTION:Line one\\nLine\r\n  two\r\nURL:" + SITE + "/e/band\r\nGEO:30.2689;-97.7355"
                         "\r\nORGANIZER;CN=Mohawk:mailto:x@example.org\r\nCATEGORIES:Music,Live\r\nATTACH;FMTTYPE=image/jpeg:https://img.example.org/band.jpg")
        events, notes, _ = self.read(text)
        band = events[0]
        self.assertEqual((band["title"], band["venue"], band["address"]), ("Band, Live", "Mohawk", "912 Red River St, Austin, TX 78701"))
        self.assertEqual((band["organizer"], band["categories"], band["image_url"], band["url"]), ("Mohawk", ["Music", "Live"], "https://img.example.org/band.jpg", SITE + "/e/band"))
        self.assertEqual(band["geo"]["longitude"], -97.7355)
        self.assertIn("Line one", band["description"])
        self.assertTrue(band["end"] and band["end"] > band["start"])
        self.assertEqual((notes["format"], band["timezone"]), ("ics", "America/Chicago"))

    def test_utc_all_day_cancelled_and_broken_entries(self):
        text = self.feed("UID:a\r\nSUMMARY:Utc\r\nDTSTART:" + ics_stamp(2, 15, True), "UID:b\r\nSUMMARY:All day\r\nDTSTART;VALUE=DATE:" + ics_stamp(2)[:8],
                         "UID:c\r\nSUMMARY:Off\r\nSTATUS:CANCELLED\r\nDTSTART:" + ics_stamp(2, 15, True), "UID:d\r\nSUMMARY:Bad\r\nDTSTART:nonsense", "UID:e\r\nDTSTART:" + ics_stamp(2, 15, True))
        events, notes, _ = self.read(text)
        self.assertEqual([e["title"] for e in events], ["Utc"])
        self.assertEqual(notes["skipped"].get("date_only"), 1)

    def test_weekly_recurrence_with_exdate_override_and_limits(self):
        start = ics_stamp(1, 18)
        second = (datetime.now() + timedelta(days=8)).replace(hour=18, minute=0, second=0, microsecond=0)
        text = self.feed(f"UID:r\r\nSUMMARY:Weekly\r\nDTSTART;TZID=America/Chicago:{start}\r\nDTEND;TZID=America/Chicago:{ics_stamp(1, 19)}\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\n"
                         f"EXDATE;TZID=America/Chicago:{second.strftime('%Y%m%dT%H%M%S')}",
                         f"UID:r\r\nSUMMARY:Weekly (moved)\r\nRECURRENCE-ID;TZID=America/Chicago:{(second + timedelta(days=7)).strftime('%Y%m%dT%H%M%S')}\r\n"
                         f"DTSTART;TZID=America/Chicago:{(second + timedelta(days=7, hours=1)).strftime('%Y%m%dT%H%M%S')}")
        events, _, _ = self.read(text, days=90)
        titles = [e["title"] for e in events]
        # four weekly instances: the second is excluded, the third is replaced by its override
        self.assertEqual(sorted(titles), ["Weekly", "Weekly", "Weekly (moved)"])
        self.assertIn("Weekly (moved)", titles)

    def test_unsupported_recurrence_keeps_only_the_first_instance(self):
        text = self.feed(f"UID:x\r\nSUMMARY:Odd\r\nDTSTART:{ics_stamp(2, 15, True)}\r\nRRULE:FREQ=MONTHLY;BYSETPOS=-1;BYDAY=FR")
        events, notes, _ = self.read(text)
        self.assertEqual(len(events), 1)
        self.assertEqual(notes["recurrence_unsupported"], 1)

    def test_a_series_end_does_not_become_the_end_of_every_instance(self):
        text = self.feed(f"UID:s\r\nSUMMARY:Series\r\nDTSTART:{ics_stamp(1, 15, True)}\r\nDTEND:{ics_stamp(1000, 15, True)}\r\nRRULE:FREQ=WEEKLY;COUNT=2")
        events, _, _ = self.read(text)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["end"] is None for e in events))


class Feeds(unittest.TestCase):
    def rss(self, items, ns='xmlns:ev="http://purl.org/rss/1.0/modules/event/"'):
        return f'<?xml version="1.0"?><rss version="2.0" {ns}><channel><title>City Events</title>{items}</channel></rss>'

    def test_rss_with_event_dates_becomes_events_and_pubdate_is_never_used(self):
        stamp = when(3, offset="-05:00")
        text = self.rss(f"<item><title>Concert</title><link>{SITE}/e/concert</link><pubDate>Mon, 01 Jan 2024 10:00:00 -0500</pubDate><ev:startdate>{stamp}</ev:startdate>"
                        "<ev:location>Plaza</ev:location><category>Music</category></item>"
                        f"<item><title>Undated</title><link>{SITE}/e/undated</link><pubDate>{datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>")
        events, notes, client = run({SITE + "/feed": (text, "application/rss+xml"), SITE + "/e/undated": html()}, url=SITE + "/feed")
        self.assertEqual([e["title"] for e in events], ["Concert"])
        self.assertEqual((events[0]["venue"], events[0]["categories"]), ("Plaza", ["Music"]))
        self.assertIn(SITE + "/e/undated", client.urls)  # an undated entry is followed to its page, not dated by its publication time

    def test_rss_dates_in_rfc822_form(self):
        text = self.rss("<item><title>Fair</title><link>%s/e/fair</link><ev:startdate>%s</ev:startdate></item>" % (
            SITE, (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%a, %d %b %Y %H:%M:%S +0000")))
        events, _, _ = run({SITE + "/feed": (text, "application/xml")}, url=SITE + "/feed", details=False)
        self.assertEqual(len(events), 1)

    def test_atom_with_google_when(self):
        text = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:gd="http://schemas.google.com/g/2005"><title>Cal</title>'
                f'<entry><title>Walk</title><link rel="alternate" href="{SITE}/e/walk"/><gd:when startTime="{when(2, offset="-05:00")}"/><gd:where valueString="Park"/></entry></feed>')
        events, _, _ = run({SITE + "/atom": (text, "application/atom+xml")}, url=SITE + "/atom", details=False)
        self.assertEqual((events[0]["title"], events[0]["venue"]), ("Walk", "Park"))

    def test_entity_declarations_and_non_feeds_are_refused(self):
        with self.assertRaises(ValueError):
            webfeeds.parse_feed('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel/></rss>', SITE)
        with self.assertRaises(ValueError):
            webfeeds.parse_feed("<html></html>", SITE)
        with self.assertRaises(ValueError):
            webfeeds.parse_feed("not xml", SITE)


class Listings(unittest.TestCase):
    def card(self, title, days, extra=""):
        stamp = when(days, offset="-05:00")
        return (f'<article class="event"><h3><a href="/events/{title.lower().replace(" ", "-")}">{title}</a></h3><time datetime="{stamp}">x</time>'
                f'<span class="venue">Library</span><img src="/i/{title[:1]}.jpg"><p>{"A long enough description of the happening. " * 2}</p>{extra}</article>')

    def test_html_cards_are_used_only_when_nothing_structured_exists(self):
        events, notes, _ = run({SITE + "/events": html(self.card("Story Time", 2) + self.card("Book Sale", 4))}, details=False)
        self.assertEqual(sorted(e["title"] for e in events), ["Book Sale", "Story Time"])
        story = next(e for e in events if e["title"] == "Story Time")
        self.assertEqual((story["venue"], story["url"], story["image_url"]), ("Library", SITE + "/events/story-time", SITE + "/i/S.jpg"))
        self.assertEqual(notes["format"], "html")
        both = html(ld(event("Structured")) + self.card("Story Time", 2))
        events, notes, _ = run({SITE + "/events": both}, details=False)
        self.assertEqual([e["title"] for e in events], ["Structured"])  # structured data wins over listing guesswork

    def test_a_bare_date_written_as_midnight_is_not_a_start_time(self):
        card = '<article><h3><a href="/e/x">Midnight Marker</a></h3><time datetime="%sT00:00:00+00:00">Oct</time></article>' % when(3)[:10]
        events, notes, _ = run({SITE + "/events": html(card)}, details=False)
        self.assertEqual(events, [])
        self.assertEqual(notes["skipped"], {"date_only": 1})
        self.assertEqual(web.probe(FakeClient({SITE + "/events": html(card)}), SITE + "/events")["status"], "no_events")

    def test_blocks_with_many_dates_are_not_mistaken_for_one_event(self):
        page = '<div><a href="/a">Calendar page</a>' + "".join(f'<time datetime="{when(i, offset="-05:00")}">d</time>' for i in range(1, 6)) + "</div>"
        events, _, _ = run({SITE + "/events": html(page)}, details=False)
        self.assertEqual(events, [])


class Stages(unittest.TestCase):
    def test_item_list_links_are_followed_and_listing_gaps_filled_from_event_pages(self):
        listing = ld({"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "position": 1, "url": SITE + "/events/one"},
                                                                 {"@type": "ListItem", "position": 2, "url": "https://elsewhere.example/events/two"}]})
        detail = html(ld(event("One", location={"@type": "Place", "name": "Hall"})))
        events, notes, client = run({SITE + "/events": html(listing), SITE + "/events/one": detail})
        self.assertEqual([(e["title"], e["venue"]) for e in events], [("One", "Hall")])
        self.assertTrue(events[0]["extraction"]["detail_page"])
        self.assertNotIn("https://elsewhere.example/events/two", client.urls)  # other sites are never crawled
        listing_event = ld(event("Listed"))
        page = html(listing_event, '<meta property="og:image" content="/og.png">')
        events, _, _ = run({SITE + "/events": page, SITE + "/events/listed": html(ld(event("Listed", location={"@type": "Place", "name": "Barn"})), '<meta property="og:image" content="/og.png">')})
        self.assertEqual((events[0]["venue"], events[0]["image_url"]), ("Barn", SITE + "/og.png"))

    def test_detail_fetching_can_be_switched_off_and_is_bounded(self):
        stubs = [{"@type": "ListItem", "url": f"{SITE}/events/e{i}"} for i in range(40)]
        pages = {SITE + "/events": html(ld({"@type": "ItemList", "itemListElement": stubs}))}
        pages.update({f"{SITE}/events/e{i}": html(ld(event(f"E{i}", i % 5 + 1))) for i in range(40)})
        events, notes, client = run(pages, details=False)
        self.assertEqual((events, len(client.urls)), ([], 1))  # the listing page only
        events, notes, client = run(pages)
        self.assertEqual(notes["details_fetched"], web.MAX_DETAIL)
        self.assertLessEqual(len(client.urls), web.MAX_REQUESTS)

    def test_same_site_feeds_are_followed_and_other_sites_ignored(self):
        head = (f'<link rel="alternate" type="text/calendar" href="/cal.ics"><link rel="alternate" type="text/calendar" href="https://other.example/x.ics">'
                f'<link rel="alternate" type="application/rss+xml" href="{SITE}/feed">')
        feed = f"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nSUMMARY:From Feed\nDTSTART:{ics_stamp(2, 15, True)}\nEND:VEVENT\nEND:VCALENDAR\n"
        events, notes, client = run({SITE + "/events": html("<p>nothing</p>", head), SITE + "/cal.ics": (feed, "text/calendar"), SITE + "/feed": ("<rss/>", "application/rss+xml")})
        self.assertEqual([e["title"] for e in events], ["From Feed"])
        self.assertNotIn("https://other.example/x.ics", client.urls)
        self.assertEqual(notes["failed_fetches"], 0)

    def test_pagination_is_bounded_and_loops_are_harmless(self):
        def page(i, nxt):
            return html(ld(event(f"P{i}", i + 1)), f'<link rel="next" href="{nxt}">')
        pages = {SITE + "/events": page(0, "/events?page=2"), SITE + "/events?page=2": page(1, "/events?page=3"), SITE + "/events?page=3": page(2, "/events?page=4"),
                 SITE + "/events?page=4": page(3, "/events?page=5")}
        events, notes, client = run(pages, details=False)
        self.assertEqual(sorted(e["title"] for e in events), ["P0", "P1", "P2"])  # at most three pages
        loop = {SITE + "/events": html(ld(event("Only")), f'<link rel="next" href="{SITE}/events">')}
        events, _, client = run(loop, details=False)
        self.assertEqual((len(events), len(client.urls)), (1, 1))

    def test_the_same_event_from_two_formats_appears_once_with_the_richer_record(self):
        head = '<link rel="alternate" type="text/calendar" href="/cal.ics">'
        start = when(3, 19, "-05:00")
        feed = f"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nSUMMARY:Gala\nDTSTART:{(datetime.strptime(start[:19], '%Y-%m-%dT%H:%M:%S') + timedelta(hours=5)).strftime('%Y%m%dT%H%M%S')}Z\nEND:VEVENT\nEND:VCALENDAR\n"
        node = {"@type": "Event", "name": "Gala", "startDate": start, "location": {"@type": "Place", "name": "Ballroom"}, "url": SITE + "/e/gala"}
        events, notes, _ = run({SITE + "/events": html(ld(node), head), SITE + "/cal.ics": (feed, "text/calendar")}, details=False)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["venue"], "Ballroom")
        self.assertEqual(notes["skipped"]["duplicate"], 1)

    def test_results_are_cached_so_shared_websites_are_not_crawled_twice(self):
        web.clear_cache()
        client = FakeClient({SITE + "/events": html(ld(event("Once")))})
        first, _ = web.discover(client, [], url=SITE + "/events", location=AUSTIN, details=False)
        second, _ = web.discover(client, [], url=SITE + "/events", location=AUSTIN, details=False)
        self.assertEqual(client.urls, [SITE + "/events"])
        self.assertEqual(first, second)
        second[0]["title"] = "changed"
        third, _ = web.discover(client, [], url=SITE + "/events", location=AUSTIN, details=False)
        self.assertEqual(third[0]["title"], "Once")  # callers cannot corrupt the cache


class Probe(unittest.TestCase):
    def probe(self, pages, url=SITE + "/events"):
        web.clear_cache()
        return web.probe(FakeClient(pages), url)

    def test_every_outcome_has_a_status_and_a_reason(self):
        self.assertEqual(self.probe({SITE + "/events": html(ld(event("A")))})["status"], "supported")
        self.assertEqual(self.probe({SITE + "/events": html(ld(event("A")))})["events"], 1)
        unsupported = self.probe({SITE + "/events": html("<p>Welcome to our site</p>")})
        self.assertEqual(unsupported["status"], "unsupported")
        self.assertIn("no event data", unsupported["reason"])
        self.assertEqual(self.probe({SITE + "/events": html(ld({"@type": "Event", "name": "Dated", "startDate": when(2)[:10]}))})["status"], "no_events")
        self.assertEqual(self.probe({SITE + "/events": (ics_empty(), "text/calendar")})["status"], "no_events")
        self.assertEqual(self.probe({SITE + "/events": safe.BlockedError("robots.txt of events.example.org does not allow this page")})["status"], "blocked")
        self.assertEqual(self.probe({SITE + "/events": FetchError("HTTP 503")})["status"], "unavailable")
        self.assertEqual(self.probe({})["status"], "unavailable")
        self.assertEqual(self.probe({SITE + "/events": ("%PDF-1.4", "application/pdf")})["status"], "unsupported")

    def test_a_script_rendered_page_is_called_out(self):
        shell = html('<div id="root"></div><script src="/a.js"></script><script src="/b.js"></script>')
        found = self.probe({SITE + "/events": shell})
        self.assertEqual(found["status"], "unsupported")
        self.assertIn("JavaScript", found["reason"])


def ics_empty():
    return "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"


class ParserLimits(unittest.TestCase):
    def test_hostile_markup_is_bounded(self):
        deep = "<div>" * 5000 + "text" + "</div>" * 5000
        root = webparse.parse_html(deep)
        self.assertLessEqual(max(n.depth for n in root.walk()), webparse.MAX_DEPTH)
        many = "<p>x</p>" * (webparse.MAX_NODES + 5000)
        self.assertLessEqual(sum(1 for _ in webparse.parse_html(many).walk()), webparse.MAX_NODES + 1)

    def test_unclosed_list_items_do_not_nest(self):
        root = webparse.parse_html("<ul>" + "<li>item" * 400 + "</ul>")
        self.assertLessEqual(max(n.depth for n in root.walk()), 3)

    def test_only_http_addresses_survive(self):
        self.assertIsNone(webparse.absolute(SITE, "javascript:alert(1)"))
        self.assertIsNone(webparse.absolute(SITE, "data:text/html,x"))
        self.assertEqual(webparse.absolute(SITE, "/a#frag"), SITE + "/a")


# --- the fetcher -----------------------------------------------------------------------------------------

def resolver_for(table):
    calls = []

    def resolve(host, port, type=None):
        calls.append(host)
        answer = table.get(host)
        if answer is None:
            raise OSError("no such host")
        return [(2, 1, 6, "", (ip, port)) for ip in (answer if isinstance(answer, list) else [answer])]
    resolve.calls = calls
    return resolve


class Sender:
    """A transport that answers from a table and records the address each request was pinned to."""

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, scheme, host, port, ip, target, headers, timeout, limit):
        self.calls.append((scheme, host, port, ip, target))
        answer = self.responses.get((host, target)) or self.responses.get(host) or (404, {}, b"")
        if isinstance(answer, Exception):
            raise answer
        return answer


def fetcher(table, responses, **kwargs):
    sleeps = []
    sender = Sender(responses)
    client = safe.SafeFetcher(2.0, resolver=resolver_for(table), send=sender, clock=lambda: 1000.0 + len(sleeps) * 0, sleep=sleeps.append, state=safe.State(), **kwargs)
    return client, sender, sleeps


OK = (200, {"Content-Type": "text/html"}, b"<html>hello</html>")
NO_ROBOTS = (404, {}, b"")


class SafeFetching(unittest.TestCase):
    def test_non_public_addresses_are_refused_in_every_form(self):
        for ip in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.0.9", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1"):
            with self.subTest(ip=ip):
                client, sender, _ = fetcher({"site.example": ip}, {})
                with self.assertRaises(safe.BlockedError):
                    client.fetch("https://site.example/")
                self.assertEqual(sender.calls, [])

    def test_a_host_with_any_private_answer_is_refused_and_ip_literals_are_checked(self):
        client, sender, _ = fetcher({"mixed.example": ["93.184.216.34", "10.0.0.5"], "127.0.0.1": "127.0.0.1", "169.254.169.254": "169.254.169.254"}, {})
        for url in ("https://mixed.example/", "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://localhost/"):
            with self.subTest(url=url), self.assertRaises(FetchError):
                client.fetch(url)
        self.assertEqual(sender.calls, [])

    def test_schemes_ports_and_credentials(self):
        client, _, _ = fetcher({"site.example": "93.184.216.34"}, {})
        for url in ("ftp://site.example/", "file:///etc/passwd", "gopher://site.example/", "https://site.example:8443/", "http://site.example:22/", "https://user:pw@site.example/", "//site.example/"):
            with self.subTest(url=url), self.assertRaises(safe.BlockedError):
                client.fetch(url)

    def test_every_request_is_pinned_to_the_address_that_was_just_checked(self):
        answers = iter(["93.184.216.34", "127.0.0.1"])  # a DNS answer that turns internal after the first lookup

        def rebinding(host, port, type=None):
            return [(2, 1, 6, "", (next(answers), port))]
        sender = Sender({("site.example", "/robots.txt"): NO_ROBOTS, "site.example": OK})
        client = safe.SafeFetcher(2.0, resolver=rebinding, send=sender, clock=lambda: 0.0, sleep=lambda s: None, state=safe.State())
        with self.assertRaises(safe.BlockedError):
            client.fetch("https://site.example/page")  # each request resolves again, sees the internal address and is refused
        self.assertEqual(sender.calls, [])  # nothing was ever sent to the internal address
        seen = iter(["93.184.216.34", "93.184.216.99", "93.184.216.77"])
        sender = Sender({("site.example", "/robots.txt"): NO_ROBOTS, "site.example": OK})
        client = safe.SafeFetcher(2.0, resolver=lambda h, p, type=None: [(2, 1, 6, "", (next(seen), p))], send=sender, clock=lambda: 0.0, sleep=lambda s: None, state=safe.State())
        client.fetch("https://site.example/page")
        self.assertEqual({call[3] for call in sender.calls}, {"93.184.216.99", "93.184.216.34"})  # always an address that was just validated

    def test_redirects_are_checked_at_every_hop(self):
        table = {"site.example": "93.184.216.34", "internal.example": "10.0.0.8", "ok.example": "93.184.216.35"}
        responses = {("site.example", "/robots.txt"): NO_ROBOTS, ("ok.example", "/robots.txt"): NO_ROBOTS, ("internal.example", "/robots.txt"): NO_ROBOTS,
                     ("site.example", "/go-internal"): (302, {"Location": "http://internal.example/secret"}, b""),
                     ("site.example", "/go-ok"): (301, {"Location": "https://ok.example/final"}, b""), ("ok.example", "/final"): OK,
                     ("site.example", "/loop"): (302, {"Location": "/loop"}, b""), ("site.example", "/nowhere"): (302, {}, b""),
                     ("site.example", "/metadata"): (307, {"Location": "http://169.254.169.254/latest/"}, b"")}
        client, sender, _ = fetcher(table, responses)
        with self.assertRaises(safe.BlockedError):
            client.fetch("https://site.example/go-internal")
        self.assertNotIn("internal.example", {c[1] for c in sender.calls})
        self.assertEqual(client.fetch("https://site.example/go-ok").url, "https://ok.example/final")
        with self.assertRaisesRegex(FetchError, "too many redirects"):
            client.fetch("https://site.example/loop")
        with self.assertRaisesRegex(FetchError, "without a destination"):
            client.fetch("https://site.example/nowhere")
        with self.assertRaises(FetchError):
            client.fetch("https://site.example/metadata")

    def test_robots_txt_follows_rfc_9309(self):
        table = {"a.example": "93.184.216.1", "b.example": "93.184.216.2", "c.example": "93.184.216.3", "d.example": "93.184.216.4"}
        responses = {"a.example": OK, ("a.example", "/robots.txt"): (200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /private\n"),
                     "b.example": OK, ("b.example", "/robots.txt"): NO_ROBOTS,
                     "c.example": OK, ("c.example", "/robots.txt"): (503, {}, b""),
                     "d.example": OK, ("d.example", "/robots.txt"): (200, {}, b"User-agent: *\nDisallow: /\n")}
        client, _, _ = fetcher(table, responses)
        self.assertIn("hello", client.get("https://a.example/public"))
        with self.assertRaisesRegex(safe.BlockedError, "robots.txt"):
            client.get("https://a.example/private/page")
        self.assertIn("hello", client.get("https://b.example/anything"))  # no robots.txt: allowed
        with self.assertRaises(safe.BlockedError):
            client.get("https://c.example/")  # robots.txt unreachable: nothing allowed
        with self.assertRaises(safe.BlockedError):
            client.get("https://d.example/")

    def test_access_refusals_are_reported_as_blocked_and_never_retried(self):
        table = {"site.example": "93.184.216.34"}
        for status in (401, 403, 429):
            client, sender, _ = fetcher(table, {("site.example", "/robots.txt"): NO_ROBOTS, "site.example": (status, {}, b"")})
            with self.subTest(status=status), self.assertRaises(safe.BlockedError):
                client.fetch("https://site.example/x")
            self.assertEqual(len([c for c in sender.calls if c[4] == "/x"]), 1)
        client, _, _ = fetcher(table, {("site.example", "/robots.txt"): NO_ROBOTS, "site.example": (404, {}, b"")})
        with self.assertRaises(FetchError) as caught:
            client.fetch("https://site.example/x")
        self.assertNotIsInstance(caught.exception, safe.BlockedError)

    def test_only_text_documents_are_accepted(self):
        client, _, _ = fetcher({"site.example": "93.184.216.34"}, {("site.example", "/robots.txt"): NO_ROBOTS, "site.example": (200, {"Content-Type": "image/png"}, b"\x89PNG")})
        with self.assertRaisesRegex(FetchError, "not a text document"):
            client.fetch("https://site.example/pic")

    def timed(self, responses, interval=2.0):
        now, sleeps = [100.0], []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        state = safe.State()
        make = lambda: safe.SafeFetcher(interval, resolver=resolver_for({"site.example": "93.184.216.34"}), send=Sender(responses), clock=lambda: now[0], sleep=sleep, state=state)
        return make, sleeps

    def test_requests_to_one_host_are_spaced_out_across_fetchers(self):
        make, sleeps = self.timed({("site.example", "/robots.txt"): NO_ROBOTS, "site.example": OK})
        make().fetch("https://site.example/a")
        before = len(sleeps)
        make().fetch("https://site.example/b")  # a second fetcher in the same process still waits its turn
        self.assertEqual(sleeps[before:], [2.0])

    def test_crawl_delay_from_robots_is_honoured_up_to_a_cap(self):
        make, sleeps = self.timed({("site.example", "/robots.txt"): (200, {}, b"User-agent: *\nCrawl-delay: 60\n"), "site.example": OK})
        client = make()
        client.fetch("https://site.example/a")
        client.fetch("https://site.example/b")
        self.assertEqual(sleeps[-1], safe.MAX_CRAWL_DELAY)


class Wire(unittest.TestCase):
    """The real transport against a local server (it is given the address directly, so no safety checks apply here)."""

    @classmethod
    def setUpClass(cls):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/big":
                    body = b"x" * (safe.MAX_BYTES + 10)
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                elif self.path == "/gzip":
                    body = gzip.compress(b"<html>zipped</html>")
                    self.send_response(200); self.send_header("Content-Encoding", "gzip"); self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                elif self.path == "/bomb":
                    body = gzip.compress(b"0" * (safe.MAX_BYTES * 3))
                    self.send_response(200); self.send_header("Content-Encoding", "gzip"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                elif self.path == "/redirect":
                    self.send_response(302); self.send_header("Location", "http://127.0.0.1:1/x"); self.end_headers()
                else:
                    self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        return safe.transport("http", "site.example", self.port, "127.0.0.1", path, {"User-Agent": "t"}, 5, safe.MAX_BYTES)

    def test_responses_over_the_limit_are_refused(self):
        with self.assertRaisesRegex(FetchError, "too large"):
            self.get("/big")

    def test_gzip_is_inflated_within_the_limit_and_bombs_are_refused(self):
        status, headers, body = self.get("/gzip")
        self.assertEqual((status, body), (200, b"<html>zipped</html>"))
        with self.assertRaisesRegex(FetchError, "too large"):
            self.get("/bomb")

    def test_redirects_are_returned_not_followed(self):
        status, headers, body = self.get("/redirect")
        self.assertEqual((status, headers["Location"], body), (302, "http://127.0.0.1:1/x", b""))

    def test_connection_errors_become_fetch_errors(self):
        with self.assertRaises(FetchError):
            safe.transport("http", "site.example", 1, "127.0.0.1", "/", {}, 2, 1000)


# --- wiring ----------------------------------------------------------------------------------------------

class Wiring(unittest.TestCase):
    def test_urls_keep_their_query_and_unusual_port_so_feeds_survive(self):
        self.assertEqual(normalize_url("HTTPS://Events.Example.org/cal/?ical=1&x=2"), "https://events.example.org/cal?ical=1&x=2")
        self.assertEqual(normalize_url("https://events.example.org:443/"), "https://events.example.org")
        self.assertEqual(normalize_url("http://events.example.org:8080/e/"), "http://events.example.org:8080/e")

    def test_the_web_adapter_is_a_registrable_multiple_use_source(self):
        config = {"sources": [{"id": f"web_{i}", "name": "A", "adapter": "web", "url": f"https://s{i}.example/events", "coverage": {"type": "global"}, "options": {"days": 30, "safe": True}} for i in range(2)]}
        sources = registry.load_sources(config)
        self.assertEqual(len(sources), 2)
        self.assertTrue(sources[0].wants_location)

    def test_user_entered_sources_are_always_read_through_the_safe_fetcher(self):
        config = {"sources": [{"id": "web_x", "name": "X", "adapter": "web", "url": SITE + "/events", "coverage": {"type": "global"}, "options": {"days": 30, "safe": True}}]}
        source = registry.load_sources(config)[0]
        seen = []

        class Spy:
            def __init__(self, interval):
                self.interval = interval

            def fetch(self, url, check_robots=True):
                seen.append(url)
                return Reply(url, html(ld(event("Spied"))))

        web.clear_cache()
        with mock.patch.object(registry, "SafeFetcher", Spy):
            events, _ = source.discover(object(), [], details=False, location=AUSTIN)
        self.assertEqual((seen, [e["title"] for e in events]), ([SITE + "/events"], ["Spied"]))

    def test_discovered_events_pass_the_whole_refresh_pipeline(self):
        folder = tempfile.mkdtemp()
        near = event("Near Show", location={"@type": "Place", "name": "Hall", "geo": {"latitude": 30.27, "longitude": -97.74}})
        far = event("Far Show", location={"@type": "Place", "name": "Away", "geo": {"latitude": 32.7157, "longitude": -117.1611}})
        web.clear_cache()
        client = FakeClient({SITE + "/events": html(ld(near, far))})
        config = {"retention_days": 3, "verification": {}, "location": AUSTIN,
                  "sources": [{"id": "web_x", "name": "Example", "adapter": "web", "url": SITE + "/events", "coverage": {"type": "global"}, "options": {"days": 30}}]}
        output = os.path.join(folder, "events.json")
        code = refresh_module.refresh(config, {"interval": 0, "no_details": True}, os.path.join(folder, "cache.json"), output, os.path.join(folder, "lock"),
                                      fetcher=client, location_cache_path=os.path.join(folder, "loc.json"))
        self.assertEqual(code, 0)
        feed = json.load(open(output, encoding="utf-8"))
        validate_feed(feed)
        self.assertEqual([e["title"] for e in feed["events"]], ["Near Show"])  # the geographic filter still applies
        self.assertEqual(feed["events"][0]["place"]["classification"], "confirmed")
        self.assertIn("category", feed["events"][0]["illustration"])
        self.assertEqual(feed["sources"]["web_x"]["events"], 1)


class EngineSources(unittest.TestCase):
    """How typed-in addresses are classified by the engine (the engine's own probe is replaced by canned answers)."""

    def make(self, answers, localist=()):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        refresh = FakeRefresh()
        engine = engine_module.Engine(folder.name, interval=3600, clock=Clock(), refresh_fn=refresh, fetcher_factory=lambda: object(),
                                      host_check=lambda host: host != "intranet.example", probe=lambda f, u: u in localist,
                                      web_probe=lambda f, u: answers[u], geocode=fake_geocode)
        return engine, refresh

    def test_each_outcome_is_reported_and_only_readable_sites_become_sources(self):
        answers = {"https://good.example/events": {"status": "supported", "format": "jsonld", "events": 7, "name": "Good Events"},
                   "https://empty.example/events": {"status": "no_events", "reason": "no upcoming events were found"},
                   "https://shell.example": {"status": "unsupported", "reason": "the page builds its events with JavaScript"},
                   "https://nope.example": {"status": "blocked", "reason": "robots.txt does not allow this page"},
                   "https://down.example": {"status": "unavailable", "reason": "could not be read: HTTP 503"}}
        engine, refresh = self.make(answers, localist=("https://uni.example",))
        calls = refresh.calls
        pid = engine.register({"area": "Austin, TX", "sources": [*answers, "https://uni.example", "https://intranet.example"]})
        while engine.run_next():
            pass
        report = {item["url"]: item for item in engine.snapshot(pid)["report"]}
        self.assertEqual({u: r["status"] for u, r in report.items()},
                         {"https://good.example/events": "supported", "https://empty.example/events": "no_events", "https://shell.example": "unsupported",
                          "https://nope.example": "blocked", "https://down.example": "unavailable", "https://uni.example": "supported", "https://intranet.example": "blocked"})
        self.assertEqual((report["https://good.example/events"]["adapter"], report["https://good.example/events"]["found"]), ("web", 7))
        self.assertEqual(report["https://uni.example"]["adapter"], "localist")  # a dedicated adapter wins over the generic reader
        self.assertEqual(len(calls), 2)  # the automatic sources first, then the websites that were added
        added = calls[1][0]["sources"][len(calls[0][0]["sources"]):]
        entries = {e["adapter"]: e for e in added}
        self.assertEqual(sorted(entries), ["localist", "web"])
        self.assertEqual((entries["web"]["name"], entries["web"]["options"]), ("Good Events", {"days": 60, "safe": True}))
        self.assertTrue(entries["localist"]["options"]["safe"])
        self.assertEqual(report["https://good.example/events"]["id"], entries["web"]["id"])
        registry.load_sources(calls[1][0])  # the generated registry entries are valid

    def test_when_no_added_website_can_be_read_the_automatic_results_stand(self):
        engine, refresh = self.make({"https://nope.example": {"status": "blocked", "reason": "refused"}})
        pid = engine.register({"area": "Austin, TX", "sources": ["https://nope.example"]})
        while engine.run_next():
            pass
        snapshot = engine.snapshot(pid)
        self.assertEqual((snapshot["state"], snapshot["error"], len(refresh.calls)), ("ready", None, 1))
        self.assertEqual(snapshot["report"][0]["status"], "blocked")

if __name__ == "__main__":
    unittest.main()
