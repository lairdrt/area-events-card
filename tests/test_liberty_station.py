"""A calendar of yearless link cards and the event pages they link to.

The pages are real excerpts (tests/data) of libertystation.com/events/calendar, whose cards (<a class="evcard">) give a title, a time, a place and a day such as "Fri 30 Oct" but
no year, and of two event pages: an <h1> title, then subordinate <h2> headings "Cost", "Dates and Times" (one dated line per occurrence) and "Location" (place and street).
An earlier extractor titled every occurrence "Dates and Times" (or "Date and Time") and lost its place; the records it stored must keep their identities once read correctly.
Nothing here is specific to that site: the producer reads heading structure and each card's own text only.
"""
import hashlib
import unittest
from dataclasses import replace
from pathlib import Path

from area_events.discovery.producers.sources_producer import _plain_text_events
from tests.test_event_detail_pages import crawl

DATA = Path(__file__).parent / "data"
SITE = "https://libertystation.com"
CALENDAR, LOCALS, DIA = SITE + "/events/calendar", SITE + "/do/locals-summer-art-exhibit-by-michaela-jean-upp", SITE + "/do/free-event-dia-de-los-muertos-celebration"
PAGES = {CALENDAR: (DATA / "liberty_station_calendar_excerpt.html").read_text(encoding="utf-8"),
         LOCALS: (DATA / "liberty_station_locals_summer_excerpt.html").read_text(encoding="utf-8"),
         DIA: (DATA / "liberty_station_dia_de_los_muertos_excerpt.html").read_text(encoding="utf-8")}
TITLE, DIA_TITLE = "\"Locals' Summer\" art exhibit by Michaela Jean Upp", "Free Event | Día de los Muertos Celebration"
OCCURRENCES = {   # start -> (end, the calendar card's day)
    "2026-10-30T10:00:00-07:00": ("2026-10-30T16:00:00-07:00", "Fri 30 Oct"),
    "2026-10-31T10:00:00-07:00": ("2026-10-31T16:00:00-07:00", "Sat 31 Oct"),
    "2026-11-01T10:00:00-08:00": ("2026-11-01T16:00:00-08:00", "Sun 1 Nov"),
    "2026-11-02T10:00:00-08:00": ("2026-11-02T16:00:00-08:00", "Mon 2 Nov"),
}
DIA_START = "2026-11-01T10:00:00-08:00"
# The identities the earlier extractor gave these occurrences in the production database, and the label it titled them with.
STORED = {(LOCALS, "2026-10-30T10:00:00-07:00"): "64849d42a590ff3a5246c2d87401c06a", (LOCALS, "2026-10-31T10:00:00-07:00"): "335c886e883db620805f90dc1abd0d03",
          (LOCALS, "2026-11-01T10:00:00-08:00"): "26f65884e370376473e2c5b12da3f4b2", (LOCALS, "2026-11-02T10:00:00-08:00"): "c8a8996a0384134968a0653d3fa2ef1e",
          (DIA, DIA_START): "b15d74fb9c0d838b976924516c9ae752"}
LABELS = {LOCALS: "Dates and Times", DIA: "Date and Time"}


def scan(pages=PAGES, previous=()):
    result, _ = crawl(pages, urls=(CALENDAR,), max_depth=1, max_pages=12, previous=previous)
    return result


def as_stored_earlier(candidates):
    """The records the earlier extractor made: titled by the page's field label, with the id that title gave them, and without the place it never read."""
    return [replace(c, title=LABELS[c.url], local_id=hashlib.sha256(f"{c.url}|{LABELS[c.url].casefold()}|{c.starts_at}".encode("utf-8")).hexdigest()[:32],
                    venue=None, address=None) for c in candidates]


class TheExhibit(unittest.TestCase):
    def setUp(self):
        self.result = scan()
        self.locals = sorted((c for c in self.result.candidates if c.url == LOCALS), key=lambda c: c.starts_at)

    def test_every_dated_occurrence_is_titled_by_the_pages_own_heading_with_its_time_and_place(self):
        self.assertEqual([(c.title, c.starts_at, c.ends_at) for c in self.locals], [(TITLE, start, end) for start, (end, _) in OCCURRENCES.items()])
        for c in self.locals:
            self.assertEqual((c.venue, c.address), ("Dick Laub NTC Command Center", "2640 Historic Decatur Rd"), "the Location field; no city or ZIP code is invented")
            self.assertTrue(c.description.startswith("A love letter to the San Diego coast"), c.description)
            self.assertNotIn("Barracks", (c.venue or "") + (c.address or ""), "the artist's studio is not the event's venue")

    def test_each_occurrence_is_corroborated_by_its_own_calendar_card_and_nothing_else(self):
        for c in self.locals:
            day = OCCURRENCES[c.starts_at][1]
            card = [line for line in c.verification.evidence if line.startswith("Listing card on")]
            self.assertEqual(len(card), 1, c.verification.evidence)
            self.assertIn(f"links to {LOCALS}: {TITLE!r}, {day}, 10am - 4pm, at Dick Laub NTC Command Center", card[0])
            self.assertEqual(c.verification.status, "corroborated")
            self.assertFalse(c.verification.verified, "two statements of one website are corroboration, never verification")
            self.assertIn(CALENDAR, c.provenance.found_on)

    def test_the_other_event_on_the_same_morning_keeps_its_own_card_and_place(self):
        dia = [c for c in self.result.candidates if c.url == DIA]
        self.assertEqual([(c.title, c.starts_at, c.ends_at, c.venue, c.address) for c in dia],
                         [(DIA_TITLE, DIA_START, "2026-11-01T14:00:00-08:00", "San Diego Craft Collective", "2590 Truxtun Rd")])
        self.assertTrue(any(f"{DIA_TITLE!r}, Sun 1 Nov, 10am - 2pm, at San Diego Craft Collective" in line for line in dia[0].verification.evidence))
        self.assertFalse(any("Locals" in line for line in dia[0].verification.evidence), "never a neighbouring card's facts")

    def test_no_label_navigation_or_calendar_header_becomes_an_event_and_nothing_is_duplicated(self):
        self.assertEqual(len(self.result.candidates), 5)
        self.assertEqual(len({c.local_id for c in self.result.candidates}), 5)
        self.assertEqual({c.title for c in self.result.candidates}, {TITLE, DIA_TITLE})

    def test_a_second_scan_changes_nothing(self):
        again = scan(previous=self.result.candidates)
        self.assertEqual(sorted(again.candidates, key=lambda c: c.local_id), sorted(self.result.candidates, key=lambda c: c.local_id))


class StoredIdentities(unittest.TestCase):
    def test_the_records_titled_by_a_field_label_keep_their_ids(self):
        first = scan()
        earlier = as_stored_earlier(first.candidates)
        self.assertEqual({(c.url, c.starts_at): c.local_id for c in earlier}, STORED, "the ids the earlier extractor made are those stored in production")
        corrected = scan(previous=earlier)
        self.assertEqual({(c.url, c.starts_at): c.local_id for c in corrected.candidates}, STORED, "each corrected occurrence updates its stored record")
        self.assertEqual({c.title for c in corrected.candidates}, {TITLE, DIA_TITLE})
        self.assertEqual((corrected.notes["identities_kept_after_correction"], corrected.notes["identity_corrections_ambiguous"]), (5, 0))

    def test_an_ambiguous_earlier_record_is_not_guessed(self):
        earlier = as_stored_earlier(scan().candidates)
        twin = next(c for c in earlier if c.url == LOCALS)
        doubled = earlier + [replace(twin, local_id="f" * 32)]   # two earlier label-titled records of the same page and instant: which one is meant cannot be told
        corrected = scan(previous=doubled)
        start = twin.starts_at
        ids = {c.local_id for c in corrected.candidates if c.url == LOCALS and c.starts_at == start}
        self.assertEqual(len(ids), 1)
        self.assertNotIn(twin.local_id, ids)
        self.assertNotIn("f" * 32, ids)
        self.assertEqual(corrected.notes["identity_corrections_ambiguous"], 1, "reported, not silently replaced")


class TheCardsOwnFacts(unittest.TestCase):
    def test_a_card_whose_time_differs_does_not_speak_for_the_occurrence(self):
        pages = dict(PAGES)
        pages[CALENDAR] = PAGES[CALENDAR].replace("10am - 4pm</div>\n\t\t<div class=\"evcard-content-venue\"><span><i class=\"far fa-location-dot\" aria-hidden=\"true\"></i></span>Dick Laub",
                                                  "11am - 4pm</div>\n\t\t<div class=\"evcard-content-venue\"><span><i class=\"far fa-location-dot\" aria-hidden=\"true\"></i></span>Dick Laub", 1)
        october = next(c for c in scan(pages).candidates if c.starts_at == "2026-10-30T10:00:00-07:00")
        self.assertEqual((october.title, october.verification.status), (TITLE, "unverified"))
        self.assertFalse(any(line.startswith("Listing card on") for line in october.verification.evidence))

    def test_a_card_naming_another_place_is_recorded_as_a_disagreement(self):
        pages = dict(PAGES)
        pages[CALENDAR] = PAGES[CALENDAR].replace("</span>Dick Laub NTC Command Center", "</span>Barracks 15", 1)
        october = next(c for c in scan(pages).candidates if c.starts_at == "2026-10-30T10:00:00-07:00")
        self.assertEqual((october.venue, october.verification.status), ("Dick Laub NTC Command Center", "unverified"), "the event page's place stands")
        self.assertTrue(any("disagree on venue" in line for line in october.verification.evidence))

    def test_a_yearless_card_alone_is_not_an_event(self):
        self.assertEqual(scan({CALENDAR: PAGES[CALENDAR]}).candidates, (), "no year is assumed for a card whose event page was not read")


# The line the event page added to its dated list after the excerpt was taken (verbatim, as stored on 2026-10-30): one bounded range, not two dated days.
RANGE_LINE = ('<li><div class="awesome-box"><div class="awesome-box-icon"><i aria-hidden="true" class="far fa-calendar-day"></i></div><div class="awesome-box-link">'
              '<span class="dldate">Wed, Nov 4, 2026 - Mon, Nov 30, 2026</span>&nbsp;&nbsp;<span class="dltime">10am - 4pm</span></div></div></li>\n</ul>')
WITH_RANGE = dict(PAGES)
WITH_RANGE[LOCALS] = PAGES[LOCALS].replace("</ul>", RANGE_LINE, 1)
INTERVAL = ("2026-11-04T10:00:00-08:00", "2026-11-30T16:00:00-08:00")
# The records the producer made of that line before ranges were read: one per end, in the production database.
ENDPOINTS = {"2026-11-04T10:00:00-08:00": "bcc94d2c4783f9a21c280a2339acfc9a", "2026-11-30T10:00:00-08:00": "519a2ccc38f9db7ce6e930c02f0d2072"}


class TheBoundedRange(unittest.TestCase):
    def setUp(self):
        self.result = scan(WITH_RANGE)
        self.locals = sorted((c for c in self.result.candidates if c.url == LOCALS), key=lambda c: c.starts_at)

    def test_the_four_dated_days_and_one_interval_are_the_exhibits_five_entries(self):
        self.assertIn(RANGE_LINE.split("</ul>")[0], WITH_RANGE[LOCALS])
        self.assertEqual([(c.title, c.starts_at, c.ends_at) for c in self.locals],
                         [(TITLE, start, end) for start, (end, _) in OCCURRENCES.items()] + [(TITLE, *INTERVAL)])
        interval = self.locals[-1]
        self.assertEqual((interval.venue, interval.address), ("Dick Laub NTC Command Center", "2640 Historic Decatur Rd"))
        self.assertIn("Date shown: Wed, Nov 4, 2026 - Mon, Nov 30, 2026 10am - 4pm", interval.verification.evidence)

    def test_no_day_inside_the_range_and_not_its_last_day_becomes_an_occurrence(self):
        inside = [c for c in self.result.candidates if "2026-11-05" <= c.starts_at[:10] <= "2026-11-30"]
        self.assertEqual(inside, [])
        self.assertEqual(len(self.result.candidates), 6, "the exhibit's five entries and the Día de los Muertos celebration")
        self.assertEqual(len({c.local_id for c in self.result.candidates}), 6)

    def test_the_interval_keeps_the_identity_of_its_first_day_and_the_last_days_record_is_not_made_again(self):
        ids = {c.starts_at: c.local_id for c in self.locals}
        self.assertEqual(ids[INTERVAL[0]], ENDPOINTS[INTERVAL[0]], "the stored record of the range's first day is the interval")
        self.assertNotIn(ENDPOINTS["2026-11-30T10:00:00-08:00"], {c.local_id for c in self.result.candidates})
        before = {(c.url, c.starts_at): c.local_id for c in scan().candidates}
        self.assertEqual({(c.url, c.starts_at): c.local_id for c in self.result.candidates if (c.url, c.starts_at) in before}, before, "the dated days keep theirs")

    def test_a_second_scan_changes_nothing(self):
        again = scan(WITH_RANGE, previous=self.result.candidates)
        self.assertEqual(sorted(again.candidates, key=lambda c: c.local_id), sorted(self.result.candidates, key=lambda c: c.local_id))


class DateRanges(unittest.TestCase):
    def events(self, line):
        found, _ = _plain_text_events(page(f"<h1>Harbor Art Exhibit</h1><p>Paintings of the bay by local artists, shown in the old command center.</p><p>{line}</p>"),
                                      "https://example.org/do/show", "Example", "2026-10-30T00:00:00+00:00", "America/Los_Angeles")
        return [(e.starts_at, e.ends_at) for e in found]

    def test_an_explicitly_bounded_range_is_one_interval(self):
        cases = {
            "Wed, Nov 4, 2026 - Mon, Nov 30, 2026 10am - 4pm": [("2026-11-04T10:00:00-08:00", "2026-11-30T16:00:00-08:00")],
            "November 4 through November 30, 2026, 10am to 4pm": [("2026-11-04T10:00:00-08:00", "2026-11-30T16:00:00-08:00")],
            "Nov 4 – Nov 30, 2026 · 10am–4pm": [("2026-11-04T10:00:00-08:00", "2026-11-30T16:00:00-08:00")],
            "Dec 20, 2026 to Jan 3, 2027 6pm - 9pm": [("2026-12-20T18:00:00-08:00", "2027-01-03T21:00:00-08:00")],
            "Dec 20 - Jan 3, 2027 6pm - 9pm": [("2026-12-20T18:00:00-08:00", "2027-01-03T21:00:00-08:00")],
        }
        for line, expected in cases.items():
            with self.subTest(line):
                self.assertEqual(self.events(line), expected)

    def test_individually_dated_days_stay_separate_occurrences(self):
        cases = {
            "Saturday, November 14 and Sunday, November 15, 2026, 9am - 3pm": [("2026-11-14T09:00:00-08:00", "2026-11-14T15:00:00-08:00"),
                                                                                ("2026-11-15T09:00:00-08:00", "2026-11-15T15:00:00-08:00")],
            "Saturday, November 7, 2026 to Sunday, November 8, 2026 9am - 4pm": [("2026-11-07T09:00:00-08:00", "2026-11-07T16:00:00-08:00"),
                                                                                  ("2026-11-08T09:00:00-08:00", "2026-11-08T16:00:00-08:00")],
            "November 14–15, 2026 9am - 3pm": [("2026-11-14T09:00:00-08:00", "2026-11-14T15:00:00-08:00"), ("2026-11-15T09:00:00-08:00", "2026-11-15T15:00:00-08:00")],
            "Nov 14, 2026 9am - 3pm, Nov 21, 2026 9am - 3pm": [("2026-11-14T09:00:00-08:00", "2026-11-14T15:00:00-08:00"), ("2026-11-21T09:00:00-08:00", "2026-11-21T15:00:00-08:00")],
        }
        for line, expected in cases.items():
            with self.subTest(line):
                self.assertEqual(self.events(line), expected)

    def test_a_range_said_to_recur_is_not_one_continuous_interval(self):
        for line in ("Saturdays, Nov 7 - Dec 19, 2026, 10am - 2pm", "Every Friday, Nov 6 through Dec 18, 2026 6pm - 8pm"):
            with self.subTest(line):
                found = self.events(line)
                self.assertTrue(found)
                self.assertTrue(all(start[:10] == end[:10] for start, end in found), found)


def page(body, title="Arts District"):
    return f"<html><head><title>{title}</title></head><body><main>{body}</main></body></html>"


class SubordinateHeadings(unittest.TestCase):
    def events(self, html):
        found, _ = _plain_text_events(html, "https://example.org/do/show", "Example", "2026-10-30T00:00:00+00:00", "America/Los_Angeles")
        return found

    def test_field_headings_are_fields_of_the_event_above_them(self):
        found = self.events(page("<h1>Harbor Lantern Walk</h1><p>A guided evening walk along the bay with paper lanterns for every family.</p>"
                                 "<h2>Cost</h2><p>Free</p><h2>Date &amp; Time</h2><ul><li>Saturday, Nov 14, 2026 6pm - 8pm</li></ul>"
                                 "<h3>Where</h3><p>Harbor Pavilion<br>500 Harbor Drive</p>"))
        self.assertEqual([(e.title, e.starts_at, e.venue, e.address) for e in found],
                         [("Harbor Lantern Walk", "2026-11-14T18:00:00-08:00", "Harbor Pavilion", "500 Harbor Drive")])

    def test_a_field_label_is_never_a_title(self):
        for label in ("Dates and Times", "Date and Time", "Location", "Cost", "Description", "Details"):
            with self.subTest(label):
                found = self.events(page(f"<h2>{label}</h2><p>Saturday, Nov 14, 2026 6pm - 8pm</p>", title=""))
                self.assertFalse([e for e in found if e.title == label])

    def test_a_label_does_not_join_a_heading_of_lower_rank_and_is_still_never_the_title(self):
        found = self.events(page("<h4>Gallery Notes</h4><p>Upstairs.</p><h2>Dates and Times</h2><p>Saturday, Nov 14, 2026 6pm - 8pm</p>", title=""))
        self.assertEqual([e.verification.evidence[0] for e in found], ["Heading on https://example.org/do/show: Dates and Times"], "its own section, not the <h4>'s")
        self.assertFalse([e for e in found if e.title == "Dates and Times"])


if __name__ == "__main__":
    unittest.main()
