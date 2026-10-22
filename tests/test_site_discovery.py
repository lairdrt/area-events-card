"""Finding events on ordinary websites: where a site keeps them (links, embedded calendars, PDFs), reading events written in page text, and the
shared detail-page work of the adapters. Everything runs on fixtures; nothing contacts a website."""
import base64
import json
import unittest
import zlib
from datetime import date, datetime, timedelta, timezone

from discovery import textloc
from discovery.core import FetchError, now_pacific
from discovery.safe import BlockedError
from discovery.sources import dosd, sandiego_gov, sandiegoreader, web, webdiscover, webtext
from discovery.sources.detailwork import RETRY_BASE, RETRY_MAX, DetailWork
from tests.test_web import AUSTIN, SITE, Reply, event, html, ics_stamp, ld


class Client:
    """Stands in for SafeFetcher: text pages, bytes (PDFs) and errors by address; every request is recorded."""

    def __init__(self, pages):
        self.pages, self.urls, self.binary_urls = pages, [], []

    def fetch(self, url, check_robots=True, binary=None, timeout=None):
        self.urls.append(url)
        if binary:
            self.binary_urls.append(url)
        entry = self.pages.get(url)
        if entry is None:
            raise FetchError(f"{url}: HTTP 404")
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, bytes):
            reply = Reply(url, "", "application/pdf")
            reply.body = entry
            return reply
        text, kind = entry if isinstance(entry, tuple) else (entry, "text/html")
        return Reply(url, text, kind)


def run(pages, url=SITE + "/", location=AUSTIN, **kwargs):
    web.clear_cache()
    client = Client(pages)
    events, notes = web.discover(client, [], url=url, location=location, source_id="web_t", source_name="Example", **kwargs)
    return events, notes, client


def pdf(lines, image_only=False):
    """A small text PDF (one Flate-compressed content stream), or a PDF that holds only an image."""
    if image_only:
        body = b"\x00" * 200
        return b"%PDF-1.4\n1 0 obj\n<< /Type /XObject /Subtype /Image /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream\nendobj\n%%EOF"
    content = "BT\n" + "".join(f"/F1 12 Tf 1 0 0 1 72 {700 - i * 20} Tm ({line}) Tj\n" for i, line in enumerate(lines)) + "ET"
    data = zlib.compress(content.encode("latin-1"))
    return b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(data)).encode() + b" /Filter /FlateDecode >>\nstream\n" + data + b"\nendstream\nendobj\ntrailer\n%%EOF"


def day(days):
    return datetime.now() + timedelta(days=days)


def written(days, hour="7:00 PM"):
    moment = day(days)
    return f"{moment:%A, %B} {moment.day}, {moment.year} {hour}"


class Links(unittest.TestCase):
    def test_a_json_wrapped_page_is_unwrapped_and_other_text_is_not(self):
        self.assertEqual(webdiscover.unwrap(json.dumps({"content": "<p>hello</p>", "css": "x"})), "<p>hello</p>")
        self.assertEqual(webdiscover.unwrap("<html></html>"), "<html></html>")
        self.assertEqual(webdiscover.unwrap('{"content": "no markup"}'), '{"content": "no markup"}')
        self.assertEqual(webdiscover.unwrap("{broken"), "{broken")

    def test_embedded_public_google_calendars_become_their_published_feeds(self):
        page = ('<iframe src="https://calendar.google.com/calendar/embed?height=600&amp;src=parish%40example.org&amp;ctz=America%2FLos_Angeles"></iframe>'
                f'<div data-public-calendar-id="{base64.b64encode(b"club@gmail.com").decode()}"></div>'
                '<a href="https://calendar.google.com/calendar/ical/team%40example.org/public/basic.ics">feed</a>')
        found = webdiscover.embedded_calendars(page)
        self.assertEqual(found, ["https://calendar.google.com/calendar/ical/parish@example.org/public/basic.ics",
                                 "https://calendar.google.com/calendar/ical/team@example.org/public/basic.ics",
                                 "https://calendar.google.com/calendar/ical/club@gmail.com/public/basic.ics"])
        self.assertEqual(webdiscover.embedded_calendars('<div data-public-calendar-id="not base64 !!"></div><iframe src="calendar.google.com/calendar/embed?src=a b"></iframe>'), [])
        self.assertEqual(webdiscover.embedded_calendars(page * 3), found, "no duplicates, at most three")

    def test_links_are_ranked_by_how_event_like_they_are_and_unsafe_or_unrelated_ones_never_score(self):
        anchors = [(SITE + "/", "Home"), (SITE + "/about", "About us"), (SITE + "/calendar", "Calendar"), (SITE + "/upcoming-events", "What's coming"),
                   (SITE + "/give", "Give now"), (SITE + "/login", "Log in"), (SITE + "/news/2016/05/old-festival", "Old festival"), (SITE + "/shop/tickets", "Tickets"),
                   ("https://other.example/events", "Events"), ("javascript:alert(1)", "Events"), ("file:///etc/passwd", "Events"), ("mailto:a@b.example", "Events"),
                   (SITE + "/events/archive", "Past events"), (SITE + "/bulletin", "Bulletin"), (SITE + "/files/a.pdf", "Events flyer"), (SITE + "/fall-festival", "Fall Festival"),
                   ("http://127.0.0.1/events", "Events"), (SITE + "/events?page=2", "Events")]
        ranked = webdiscover.rank_links(anchors, SITE + "/", limit=10)
        urls = [url for _, url, _ in ranked]
        self.assertEqual(set(urls), {SITE + "/calendar", SITE + "/upcoming-events", SITE + "/bulletin", SITE + "/fall-festival", SITE + "/events?page=2"})
        self.assertEqual(len(webdiscover.rank_links(anchors, SITE + "/", limit=2)), 2)
        self.assertEqual(webdiscover.rank_links(anchors, SITE + "/", limit=10, exclude=[SITE + "/calendar"]).__len__(), 4)

    def test_documents_are_found_with_event_looking_ones_first(self):
        anchors = [(SITE + "/files/rules.pdf", "Raffle rules"), (SITE + "/files/Fall+Festival+Flyer.pdf", "Flyer"), (SITE + "/img/fall-flyer.jpg", "Fall flyer"),
                   (SITE + "/img/logo.png", "Logo"), (SITE + "/files/rules.pdf", "again")]
        found = webdiscover.document_links(anchors, SITE + "/")
        self.assertEqual([(d["kind"], d["eventish"]) for d in found], [("pdf", True), ("image", True), ("pdf", False)])
        self.assertEqual(len(found), 3)

    def test_calendar_ids_cannot_point_the_fetcher_anywhere_else(self):
        hostile = ('<iframe src="https://calendar.google.com/calendar/embed?src=x%2F..%2F..%2Fevil"></iframe><div data-public-calendar-id="../../etc"></div>'
                   '<iframe src="https://calendar.google.com/calendar/embed?src=a%40b.org%3Fx%3D1"></iframe>')
        self.assertEqual(webdiscover.embedded_calendars(hostile), [])
        for url in webdiscover.embedded_calendars('<iframe src="https://calendar.google.com/calendar/embed?src=a%40b.org"></iframe>'):
            self.assertTrue(url.startswith("https://calendar.google.com/calendar/ical/") and url.endswith("/public/basic.ics"))

    def test_a_page_never_chooses_the_hosts_the_crawler_visits(self):
        anchors = [("http://localhost/events", "Events"), ("http://10.0.0.5/calendar", "Calendar"), ("http://[::1]/events", "Events"), ("ftp://x.example/events", "Events"),
                   ("//evil.example/events", "Events"), (SITE.replace("events.", "events.evil-") + "/events", "Events")]
        self.assertEqual(webdiscover.rank_links(anchors, SITE + "/"), [])


class TextEvents(unittest.TestCase):
    today = date(2026, 10, 22)

    def events(self, *lines):
        return webtext.events_in(list(lines), self.today)

    def test_a_date_a_start_time_and_a_title_make_an_event_and_nothing_is_invented(self):
        found = self.events("Saturday, November 8, 2026 - 6:30 PM Harvest Dinner and Silent Auction", "Location: Parish Hall")
        self.assertEqual([(c["title"], c["start"], c["venue"], c["address"], c["end"]) for c in found],
                         [("Harvest Dinner and Silent Auction", "2026-11-08T18:30", "Parish Hall", None, None)])
        self.assertEqual(found[0]["kind"], "text")
        self.assertEqual([c["start"] for c in self.events("Wednesday, Dec 3 7pm - 9pm Advent Concert at 123 Main Street, Poway, CA")], ["2026-12-03T19:00"])
        concert = self.events("Wednesday, Dec 3 7pm - 9pm Advent Concert at 123 Main Street, Poway, CA")[0]
        self.assertEqual((concert["title"], concert["address"], concert["end"], concert["venue"]), ("Advent Concert", "123 Main Street, Poway, CA", "2026-12-03T21:00", None))

    def test_the_title_may_be_on_the_next_or_the_previous_line_but_never_a_label(self):
        self.assertEqual([c["title"] for c in self.events("Sun Nov 16 at 10:00 AM", "Annual Craft Fair")], ["Annual Craft Fair"])
        self.assertEqual([c["title"] for c in self.events("Winter Social", "Jan 10 at 6 pm")], ["Winter Social"])
        self.assertEqual(self.events("Nov 16 at 10:00 AM", "Location: Parish Hall"), [])

    def test_history_postings_deadlines_and_bare_dates_are_not_events(self):
        for line in ("October 9th (4:00 - 10:00 p.m.) Fall Festival", "Posted on October 25, 2026 at 9:00 am", "Registration deadline: Oct 30, 5:00 pm",
                     "Copyright 2026 Example Church", "Saturday, Nov 8", "Mass at 7 pm daily", "Calendar - Nov 8 at 6 pm", "Last updated Nov 3 at 4 pm", "Nov 31 at 6 pm Party"):
            self.assertEqual(self.events(line), [], line)

    def test_a_date_without_a_year_is_this_year_and_a_passed_one_is_not_rolled_forward_except_at_the_turn_of_the_year(self):
        self.assertEqual([c["start"] for c in webtext.events_in(["Mar 5 7pm Spring Fling"], date(2026, 10, 22))], [])
        self.assertEqual([c["start"] for c in webtext.events_in(["Jan 10 6pm Winter Social"], date(2026, 12, 20))], ["2027-01-10T18:00"])
        self.assertEqual([c["start"] for c in webtext.events_in(["10/25 Pumpkin Patch 2pm"], self.today)], ["2026-10-25T14:00"])

    def test_html_entities_are_decoded_and_duplicates_are_one_event(self):
        found = self.events("Nov 8, 2026 6 pm Fish &amp; Chips Night", "Nov 8, 2026 6 pm Fish &amp; Chips Night")
        self.assertEqual([c["title"] for c in found], ["Fish & Chips Night"])

    def test_a_title_must_be_a_title(self):
        for title in ("Calendar", "Read more", "12", "", "x" * 130, "Posted yesterday"):
            self.assertFalse(webtext.valid_title(title), title)
        self.assertTrue(webtext.valid_title("Harvest Dinner"))


class PdfText(unittest.TestCase):
    def test_text_pdfs_are_read_and_image_only_or_unreadable_ones_are_called_what_they_are(self):
        lines, reason = webtext.pdf_text(pdf(["Fall Festival Flyer", "Saturday November 8 2026 at 6:30 PM", "Parish Hall 123 Main Street Poway CA"]))
        self.assertEqual((reason, lines[:2]), (None, ["Fall Festival Flyer", "Saturday November 8 2026 at 6:30 PM"]))
        self.assertEqual(webtext.pdf_text(pdf([], image_only=True))[1], "no text (a scanned or image-only document)")
        self.assertEqual(webtext.pdf_text(b"<html>not a pdf</html>"), ([], "not a PDF"))
        self.assertEqual(webtext.pdf_text(b"%PDF-1.4 /Encrypt 1 0 R")[1], "encrypted")
        self.assertEqual(webtext.pdf_text(b"%PDF-1.4\n" + b"junk" * 50)[1], "no text (a scanned or image-only document)")

    def test_two_byte_fonts_are_decoded_with_the_to_unicode_table_that_fits(self):
        cmap = b"1 begincmap\n2 beginbfchar\n<0003> <0020>\n<0010> <0041>\nendbfchar\n1 beginbfrange\n<0011> <0013> <0042>\nendbfrange\nendcmap"
        first = b"<" + b"0010001100120013" * 8 + b"0003" + b"0010001100120013" * 4 + b">"
        content = b"BT\n/F2 12 Tf 1 0 0 1 72 700 Tm [" + first + b"] TJ\nET\nBT\n/F2 12 Tf 1 0 0 1 72 680 Tm <" + b"0010001100120013" * 5 + b"> Tj\nET"
        def stream(header, body):
            data = zlib.compress(body)
            return b"<< /Length " + str(len(data)).encode() + b" /Filter /FlateDecode " + header + b" >>\nstream\n" + data + b"\nendstream\n"
        document = b"%PDF-1.5\n" + stream(b"", cmap) + stream(b"", content) + b"%%EOF"
        lines, reason = webtext.pdf_text(document)
        self.assertEqual((reason, lines), (None, ["ABCD" * 8 + " " + "ABCD" * 4, "ABCD" * 5]))


class Crawling(unittest.TestCase):
    def test_a_home_page_without_events_leads_to_the_pages_that_hold_them_within_a_few_requests(self):
        home = html('<a href="/about">About</a><a href="/calendar">Calendar</a><a href="/give">Give</a><a href="https://other.example/events">Other</a>')
        pages = {SITE + "/": home, SITE + "/calendar": html(ld(event("Spring Fair", 4, location={"@type": "Place", "name": "Hall"}))), SITE + "/about": html("<p>about</p>")}
        events, notes, client = run(pages)
        self.assertEqual([(e["title"], e["venue"]) for e in events], [("Spring Fair", "Hall")])
        self.assertEqual(notes["discovered"][0]["events"], 1)
        self.assertNotIn(SITE + "/about", client.urls)
        self.assertNotIn("https://other.example/events", client.urls)
        self.assertLessEqual(len(client.urls), web.MAX_HUBS + web.MAX_FEEDS + 2)

    def test_pages_are_only_searched_for_when_the_one_given_has_no_events_and_only_one_hop_deep(self):
        pages = {SITE + "/": html(ld(event("Here", 2)) + '<a href="/calendar">Calendar</a>'), SITE + "/calendar": html(ld(event("There", 3)))}
        events, notes, client = run(pages, details=False)
        self.assertEqual([e["title"] for e in events], ["Here"])
        self.assertEqual(client.urls, [SITE + "/"])
        deep = {SITE + "/": html('<a href="/calendar">Calendar</a>'), SITE + "/calendar": html('<a href="/events/2026">Events 2026</a><a href="/upcoming">Upcoming</a>'),
                SITE + "/upcoming": html(ld(event("Too deep", 3)))}
        events, notes, client = run(deep, details=False)
        self.assertEqual(events, [])
        self.assertNotIn(SITE + "/upcoming", client.urls)

    def test_discovery_never_exceeds_the_request_limit(self):
        links = "".join(f'<a href="/events-{i}">Events {i}</a>' for i in range(40))
        pages = {SITE + "/": html(links)}
        pages.update({f"{SITE}/events-{i}": html("".join(f'<a href="/events-{i}-{j}">Events</a>' for j in range(20))) for i in range(40)})
        events, notes, client = run(pages)
        self.assertLessEqual(len(client.urls), web.MAX_REQUESTS)
        self.assertLessEqual(len(notes["discovered"]), web.MAX_HUBS)

    def test_an_embedded_public_calendar_is_read_when_its_feed_is_allowed_and_reported_when_it_is_not(self):
        embed = '<iframe src="https://calendar.google.com/calendar/embed?src=parish%40example.org"></iframe>'
        feed_url = "https://calendar.google.com/calendar/ical/parish@example.org/public/basic.ics"
        feed = f"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nSUMMARY:Parish Supper\nDTSTART:{ics_stamp(5, 18, True)}\nEND:VEVENT\nEND:VCALENDAR\n"
        events, notes, _ = run({SITE + "/": html(embed), feed_url: (feed, "text/calendar")})
        self.assertEqual([e["title"] for e in events], ["Parish Supper"])
        self.assertEqual(notes["embedded_calendars"], [{"url": feed_url, "read": True}])
        events, notes, _ = run({SITE + "/": html(embed), feed_url: BlockedError("robots.txt of calendar.google.com does not allow this page")})
        self.assertEqual(events, [])
        self.assertFalse(notes["embedded_calendars"][0]["read"])
        web.clear_cache()
        probe = web.probe(Client({SITE + "/": html(embed), feed_url: BlockedError("robots.txt of calendar.google.com does not allow this page")}), SITE + "/")
        self.assertEqual(probe["status"], "unsupported")
        self.assertIn("embedded public calendar", probe["reason"])

    def test_a_json_wrapped_page_is_read(self):
        wrapped = json.dumps({"content": html(ld(event("Wrapped", 3))), "css": "x"})
        events, _, _ = run({SITE + "/": (wrapped, "text/html")}, details=False)
        self.assertEqual([e["title"] for e in events], ["Wrapped"])

    def test_events_written_in_page_text_are_found_with_their_source_and_nothing_inherited(self):
        body = (f"<nav><p>{written(2)} Menu Item</p></nav><h1>Upcoming</h1><ul><li>{written(6, '6:30 PM')} Harvest Dinner</li><li>Location: Parish Hall</li>"
                f"<li>Posted {written(1)} Update</li></ul><footer><p>{written(3)} Footer Event</p><p>1 Church Street</p></footer>")
        events, notes, _ = run({SITE + "/": html(body)})
        self.assertEqual([(e["title"], e["venue"], e["address"]) for e in events], [("Harvest Dinner", "Parish Hall", None)], "an explicit Location label next to the event is its venue; navigation and the footer are not read")
        self.assertEqual(events[0]["extraction"]["method"], "text")
        self.assertEqual(notes["formats"], {"text": 1})

    def test_text_events_never_duplicate_or_replace_a_structured_event(self):
        head = '<link rel="alternate" type="text/calendar" href="/cal.ics">'
        start = day(4).replace(hour=19, minute=0, second=0, microsecond=0)
        feed = f"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nSUMMARY:Fall Concert\nDTSTART:{(start + timedelta(hours=5)).strftime('%Y%m%dT%H%M%S')}Z\nEND:VEVENT\nEND:VCALENDAR\n"
        text = f"<p>{start:%B} {start.day}, {start.year} 7:00 PM Fall Concert</p>"
        events, notes, _ = run({SITE + "/": html(text, head), SITE + "/cal.ics": (feed, "text/calendar")})
        self.assertEqual([e["title"] for e in events], ["Fall Concert"])
        self.assertEqual(events[0]["extraction"]["method"], "ics")
        merged = web._without_restated([{"kind": "ics", "title": "Fall Concert", "start": "2026-11-08T19:00"}, {"kind": "text", "title": "Fall Concert Night", "start": "2026-11-08T19:00"},
                                         {"kind": "text", "title": "Craft Fair", "start": "2026-11-08T19:30"}, {"kind": "text", "title": "Craft Fair Sale", "start": "2026-11-08T19:45"}])
        self.assertEqual([c["title"] for c in merged], ["Fall Concert", "Craft Fair"], "an unrelated text event stays; a restated or repeated one goes")

    def test_event_pdfs_are_read_image_flyers_and_scanned_pdfs_are_kept_as_links(self):
        home = html('<a href="/files/Fall+Festival+Flyer.pdf">Flyer</a><a href="/files/scan.pdf">Event schedule</a><a href="/img/poster-flyer.jpg">Event poster</a>'
                    '<a href="/files/rules.pdf">Rules</a><a href="/files/news.pdf">Event news</a>')
        flyer = pdf([f"Fall Festival {written(7, '6:30 PM')} Harvest Dinner"])
        pages = {SITE + "/": home, SITE + "/files/Fall+Festival+Flyer.pdf": flyer, SITE + "/files/scan.pdf": pdf([], image_only=True), SITE + "/files/news.pdf": flyer}
        events, notes, client = run(pages)
        self.assertEqual([e["extraction"]["method"] for e in events], ["pdf"])
        self.assertEqual(events[0]["url"], SITE + "/files/Fall+Festival+Flyer.pdf")
        docs = {d["url"].rsplit("/", 1)[-1]: d for d in notes["documents"]}
        self.assertTrue(docs["Fall+Festival+Flyer.pdf"]["extracted"])
        self.assertEqual(docs["scan.pdf"]["reason"], "no text (a scanned or image-only document)")
        self.assertIn("text recognition", docs["poster-flyer.jpg"]["reason"])
        self.assertEqual(len(client.binary_urls), web.MAX_DOCS)
        self.assertNotIn(SITE + "/files/rules.pdf", client.urls, "a document that does not look like an event document is not fetched")

    def test_failures_and_hostile_pages_leave_what_was_found(self):
        pages = {SITE + "/": html(ld(event("Safe", 2)) + '<a href="/calendar">Calendar</a>'), SITE + "/calendar": FetchError("down")}
        events, notes, _ = run(pages, details=False)
        self.assertEqual([e["title"] for e in events], ["Safe"])
        hostile = {SITE + "/": html('<a href="/calendar">Calendar</a>'), SITE + "/calendar": ("{" * 1000 + "<p>" * 5000, "text/html")}
        events, notes, _ = run(hostile)
        self.assertEqual(events, [])


class DetailQueue(unittest.TestCase):
    def work(self, previous=(), budget=2, now=None, version=1):
        return DetailWork(list(previous), lambda e: e["id"], budget, now=now or datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc), version=version)

    def test_pages_are_fetched_within_the_budget_and_the_rest_is_deferred(self):
        work = self.work(budget=2)
        self.assertEqual([work.should_fetch(i) for i in "abc"], [True, True, False])
        work.done()
        work.done()
        self.assertEqual((work.counts["detail_fetched"], work.counts["detail_deferred"]), (2, 1))

    def test_a_completed_page_is_reused_until_the_adapter_learns_more(self):
        done = {"id": "a", "extraction": {"detail_page": True, "detail_v": 1}}
        self.assertIs(self.work([done]).reusable("a"), done)
        self.assertIsNone(self.work([done], version=2).reusable("a"), "read again at a newer extraction version")
        self.assertIsNone(self.work([{"id": "a", "extraction": {"detail_page": False}}]).reusable("a"))
        legacy = {"id": "a", "extraction": {"detail_page": True}}
        self.assertIs(self.work([legacy]).reusable("a"), legacy, "events cached before versions existed count as version 1")

    def test_a_failed_page_backs_off_and_the_budget_goes_to_the_others(self):
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        first = self.work(budget=1, now=now)
        self.assertTrue(first.should_fetch("a"))
        first.failed("a", "u", "502")
        event = first.stamp({"id": "a", "extraction": {}}, "a", False, failed=True)
        self.assertEqual((event["extraction"]["detail_failures"], event["extraction"]["detail_retry_after"]),
                         (1, (now + timedelta(seconds=RETRY_BASE)).isoformat(timespec="seconds")))
        second = self.work([event], budget=1, now=now + timedelta(minutes=5))
        self.assertFalse(second.should_fetch("a"))
        self.assertTrue(second.should_fetch("b"), "the broken page does not hold up the next one")
        self.assertEqual(second.counts["detail_backoff"], 1)
        carried = second.stamp({"id": "a", "extraction": {}}, "a", False)
        self.assertEqual(carried["extraction"]["detail_retry_after"], event["extraction"]["detail_retry_after"])
        third = self.work([event], budget=1, now=now + timedelta(minutes=16))
        self.assertTrue(third.should_fetch("a"))

    def test_the_wait_doubles_and_is_capped(self):
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        event = {"id": "a", "extraction": {}}
        for expected in (RETRY_BASE, 2 * RETRY_BASE, 4 * RETRY_BASE):
            work = self.work([event], now=now)
            event = work.stamp({"id": "a", "extraction": {}}, "a", False, failed=True)
            self.assertEqual(datetime.fromisoformat(event["extraction"]["detail_retry_after"]) - now, timedelta(seconds=expected))
        event["extraction"]["detail_failures"] = 30
        capped = self.work([event], now=now).stamp({"id": "a", "extraction": {}}, "a", False, failed=True)
        self.assertEqual(datetime.fromisoformat(capped["extraction"]["detail_retry_after"]) - now, timedelta(seconds=RETRY_MAX))

    def test_completing_a_page_clears_its_failure_state(self):
        failed = {"id": "a", "extraction": {"detail_page": False, "detail_failures": 3, "detail_retry_after": "2026-10-22T12:00:00+00:00"}}
        event = self.work([failed]).stamp({"id": "a", "extraction": {}}, "a", True)
        self.assertEqual(event["extraction"], {"detail_page": True, "detail_v": 1})

    def test_disabled_work_fetches_nothing_and_counts_nothing(self):
        work = DetailWork([], lambda e: e["id"], 5, enabled=False)
        self.assertFalse(work.should_fetch("a"))
        self.assertEqual(work.counts["detail_deferred"], 0)


# --- the City of San Diego adapter on fixtures --------------------------------------------------------------------------

CITY_URL = "https://www.sandiego.gov/events"


def city_listing(items):
    out = []
    for slug, title, days in items:
        moment = now_pacific() + timedelta(days=days)
        out.append(f'<div><div class="grid-x grid-margin-x event-listing-large"><h2>{title}</h2><p class="post__date"><strong>{moment:%A}, {moment:%B} {moment.day}, {moment.year}, '
                   f'10:00 AM - 2:00 PM</strong></p><p>About {title}.</p><p><a href="/event/{slug}">More</a></p></div></div>')
    return "<html><body>" + "".join(out) + "</body></html>"


def city_detail(location, directions):
    return f"<html><main><p>page</p> Location: {location} --> {directions} Get Directions </main></html>"


class CityFetcher:
    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def get(self, url):
        self.urls.append(url)
        entry = self.pages.get(url)
        if entry is None or isinstance(entry, Exception):
            raise entry or FetchError("404")
        return entry


class CityAdapter(unittest.TestCase):
    def pages(self, items, details):
        pages = {CITY_URL: city_listing(items)}
        for slug, (location, directions) in details.items():
            pages[f"https://www.sandiego.gov/event/{slug}"] = city_detail(location, directions)
        return pages

    def discover(self, fetcher, previous=(), **kwargs):
        return sandiego_gov.discover(fetcher, list(previous), pages=1, **kwargs)

    def test_the_get_directions_address_is_taken_when_it_belongs_to_the_event(self):
        items = [("clairemont", "Winter Carnival", 2), ("newport", "Ocean Beach Market", 3), ("balboa", "Haunted Trail", 4), ("school", "Car Show", 5), ("blocks", "Gaslamp Market", 6)]
        details = {"clairemont": ("This will be a carnival on the front side of South Clairemont Park area.", "South Clairemont Rec Ctr. And Park, 3605 Clairemont Drive, San Diego CA"),
                   "newport": ("The event is on the 4900 block of Newport Avenue.", "4900 Newport Avenue San Diego, CA"),
                   "balboa": ("On the corner of Balboa Drive and Eighth Drive.", "324.062583, -117.157500 San Diego, CA"),
                   "school": ("Vehicles will be parked along Palm Street between 30th Street and Bancroft Street.", "3266 Nutmeg St San Diego, CA"),
                   "blocks": ("400 and 500 blocks of 5th Avenue", "400 And 500 Blocks Of Fifth Avenue San Diego, CA")}
        events, notes = self.discover(CityFetcher(self.pages(items, details)))
        by_title = {e["title"]: e for e in events}
        self.assertEqual(by_title["Winter Carnival"]["address"], "3605 Clairemont Drive, San Diego, CA")
        self.assertEqual(by_title["Ocean Beach Market"]["address"], "4900 Newport Avenue, San Diego, CA")
        for title in ("Haunted Trail", "Car Show", "Gaslamp Market"):
            self.assertIsNone(by_title[title]["address"], title)
        self.assertIn("Get Directions", " ".join(by_title["Winter Carnival"]["verification"]["evidence"]))
        self.assertNotIn("address", by_title["Winter Carnival"]["verification"]["missing"])
        self.assertEqual(by_title["Winter Carnival"]["venue"], "This will be a carnival on the front side of South Clairemont Park area.", "the source's own location text is kept")

    def test_an_address_on_several_unrelated_events_is_a_general_address_not_a_venue(self):
        items = [(f"e{i}", f"Event {i}", i + 1) for i in range(3)]
        details = {f"e{i}": ("Main Street", "202 Main Street San Diego, CA") for i in range(3)}
        events, notes = self.discover(CityFetcher(self.pages(items, details)))
        self.assertEqual([e["address"] for e in events], [None, None, None])
        self.assertEqual(notes["general_address_dropped"], 3)

    def test_pages_are_read_once_failures_wait_and_older_reads_are_redone_for_the_address(self):
        items = [(f"e{i}", f"Event {i}", i + 1) for i in range(4)]
        details = {f"e{i}": (f"Street {i}", f"{100 + i} Oak Street San Diego, CA") for i in range(4)}
        pages = self.pages(items, details)
        pages["https://www.sandiego.gov/event/e0"] = FetchError("502")
        fetcher = CityFetcher(pages)
        events, notes = self.discover(fetcher, max_details_per_run=2)
        self.assertEqual((notes["detail_failed"], notes["detail_fetched"]), (1, 1))
        broken = next(e for e in events if e["title"] == "Event 0")
        self.assertEqual(broken["extraction"]["detail_failures"], 1)
        self.assertIn("detail_retry_after", broken["extraction"])
        fetcher2 = CityFetcher(pages)
        events, notes = self.discover(fetcher2, events, max_details_per_run=2)
        fetched = [u.rsplit("/", 1)[-1] for u in fetcher2.urls if "/event/" in u]
        self.assertEqual(fetched, ["e2", "e3"], "e1 is reused, e0 is backing off, and the budget reached the events behind it")
        self.assertEqual((notes["detail_reused"], notes["detail_backoff"]), (1, 1))
        self.assertEqual(sorted(e["address"] for e in events if e["address"]), ["101 Oak Street, San Diego, CA", "102 Oak Street, San Diego, CA", "103 Oak Street, San Diego, CA"])
        old = [{**e, "extraction": {"detail_page": True}, "address": None} for e in events]   # read before addresses were taken from the page
        fetcher3 = CityFetcher(pages)
        _, notes = self.discover(fetcher3, old, max_details_per_run=10)
        self.assertEqual(notes["detail_reused"], 0)

    def test_details_can_be_switched_off(self):
        fetcher = CityFetcher(self.pages([("a", "A", 1)], {"a": ("x", "1 Oak Street San Diego, CA")}))
        events, notes = self.discover(fetcher, details=False)
        self.assertEqual((len(fetcher.urls), events[0]["address"], notes["detail_fetched"]), (1, None, 0))


class OtherAdapters(unittest.TestCase):
    def test_reader_and_dosd_share_the_detail_queue(self):
        today = now_pacific().date()
        page = ""
        for number in range(3):
            page += (f'<div class="event-item"><a href="/events/{today:%Y}/{["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"][today.month - 1]}/{today.day:02d}/e{number}/?et={number}" '
                     f'class="event-title">Show {number}</a><div class="event-time"><div>11:30 PM</div></div><div class="event-location">Hall {number}</div></div>')
        listing = f"{sandiegoreader.BASE}/events/{today:%Y}/{['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'][today.month - 1]}/{today.day:02d}/"
        fetcher = CityFetcher({listing: page})
        events, notes = sandiegoreader.discover(fetcher, [], days=1, max_details_per_run=2)
        self.assertEqual(len(events), 3)
        self.assertEqual((notes["detail_failed"], notes["detail_deferred"]), (2, 1), "pages that cannot be read are counted, the rest wait")
        self.assertTrue(all("detail_failures" in e["extraction"] for e in events if e["extraction"].get("detail_retry_after")))
        again, notes = sandiegoreader.discover(CityFetcher({listing: page}), events, days=1, max_details_per_run=2)
        self.assertEqual(notes["detail_backoff"], 2, "the two that failed are not asked again at once")
        self.assertEqual(notes["detail_failed"], 1, "the budget went to the one that had waited")

        self.assertTrue(callable(dosd.discover))


class TextLocationUse(unittest.TestCase):
    def test_provenance_of_an_address_written_in_text_is_the_text_itself(self):
        self.assertEqual(textloc.extract_addresses("Dinner at 123 Main Street, Poway, CA 92064")[0]["address"], "123 Main Street, Poway, CA 92064")


if __name__ == "__main__":
    unittest.main()
