"""A date or time is never an event's title.

The listing is a real excerpt of summerspastfarms.com/events.htm (tests/data), where every event is an <h4> title followed by an <h5> date line and a time paragraph. An earlier
extractor titled each occurrence with its <h5> date line ("Saturday-Sunday December 5 & 6, 2026"); the stored records it made must keep their identities once the titles are read
correctly. Nothing here is specific to that site: the producer reads heading structure only.
"""
import hashlib
import unittest
from dataclasses import replace
from pathlib import Path

from area_events.discovery.producers.sources_producer import _schedule_only
from tests.test_event_detail_pages import crawl

DATA = Path(__file__).parent / "data"
SUMMERS = (DATA / "summers_past_events_excerpt.html").read_text(encoding="utf-8")
PAGE = "https://www.summerspastfarms.com/events.htm"
FAIR = "Antique & Craft Fair Vendors Day at the Farm"
EXPECTED = {   # start -> (title, end, the date line an earlier extractor used as the title)
    "2026-05-03T13:00:00-07:00": ("Vintage Alpine Kiwanis Wine & Food Event", "2026-05-03T16:00:00-07:00", "Sunday May 3, 2026"),
    "2026-05-09T09:00:00-07:00": ("Spring Antique & Craft Fair", "2026-05-09T15:00:00-07:00", "Saturday-Sunday May 9 & 10, 2026"),
    "2026-05-10T09:00:00-07:00": ("Spring Antique & Craft Fair", "2026-05-10T15:00:00-07:00", "Saturday-Sunday May 9 & 10, 2026"),
    "2026-06-20T09:00:00-07:00": ("Lavender Day & Antique Craft Fair", "2026-06-20T15:00:00-07:00", "Saturday June 20, 2026"),
    "2026-11-07T09:00:00-08:00": (FAIR, "2026-11-07T15:00:00-08:00", "Saturday-Sunday November 7 & 8, 2026"),
    "2026-11-08T09:00:00-08:00": (FAIR, "2026-11-08T15:00:00-08:00", "Saturday-Sunday November 7 & 8, 2026"),
    "2026-12-05T09:00:00-08:00": (FAIR, "2026-12-05T15:00:00-08:00", "Saturday-Sunday December 5 & 6, 2026"),
    "2026-12-06T09:00:00-08:00": (FAIR, "2026-12-06T15:00:00-08:00", "Saturday-Sunday December 5 & 6, 2026"),
}
# The identities the earlier extractor gave the December fair in the production database.
STORED_DECEMBER = {"2026-12-05T09:00:00-08:00": "0e147e7c071449a173b3d229de0ddf16", "2026-12-06T09:00:00-08:00": "6e3fe4ddb0d04eb685ed5dacb782c6a7"}


def scan(html=SUMMERS, previous=(), page=PAGE):
    result, _ = crawl({page: html}, urls=(page,), max_depth=0, previous=previous)
    return result


def as_stored_earlier(candidates):
    """The records the earlier extractor made of these occurrences: titled by their date line, with the id that title gave them."""
    return [replace(c, title=EXPECTED[c.starts_at][2],
                    local_id=hashlib.sha256(f"{PAGE}|{EXPECTED[c.starts_at][2].casefold()}|{c.starts_at}".encode("utf-8")).hexdigest()[:32]) for c in candidates]


def listing(*blocks, title="Farm Events"):
    return f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{''.join(blocks)}</body></html>"


class ScheduleOnlyText(unittest.TestCase):
    def test_dates_weekdays_ranges_times_and_their_labels_are_schedule_only(self):
        for text in ("Saturday-Sunday December 5 & 6, 2026", "Saturday June 20, 2026", "Sunday May 3, 2026", "Date: Sat. Nov. 7", "Mar 14 10:00-11:00am",
                     "December 5 - December 6", "9:00am-3:00pm", "Saturday & Sunday", "When: Friday, Dec. 11 at 7 pm", "2026-12-05", "12/05/2026 to 12/06/2026"):
            self.assertTrue(_schedule_only(text), text)

    def test_a_descriptive_title_containing_a_date_is_a_title(self):
        for text in ("Summer Fair — December 5", "May Day Festival", "Antique & Craft Fair Vendors Day at the Farm", "Closed December 21, 2026 - February 3, 2027",
                     "Reopens Thursday, February 4, 2027", "Sunday Brunch", "2026 Events", "Month of October", "Fair", ""):
            self.assertFalse(_schedule_only(text), text)


class SummersPastFarms(unittest.TestCase):
    def test_each_occurrence_takes_its_events_heading_not_its_date_line(self):
        result = scan()
        got = {c.starts_at: (c.title, c.ends_at) for c in result.candidates}
        self.assertEqual(got, {start: (title, end) for start, (title, end, _) in EXPECTED.items()})
        self.assertFalse([c.title for c in result.candidates if _schedule_only(c.title)], "no title is a date")

    def test_the_two_december_days_are_two_occurrences(self):
        december = [c for c in scan().candidates if c.starts_at.startswith("2026-12-")]
        self.assertEqual(sorted(c.starts_at for c in december), ["2026-12-05T09:00:00-08:00", "2026-12-06T09:00:00-08:00"])
        self.assertEqual({c.title for c in december}, {FAIR})
        self.assertEqual(len({c.local_id for c in december}), 2)
        self.assertTrue(all(c.description == "Come to this fun antique & craft fair on our spacious lawn area." for c in december))

    def test_the_earlier_records_keep_their_ids_when_the_title_is_corrected(self):
        earlier = as_stored_earlier(scan().candidates)
        self.assertEqual({c.starts_at: c.local_id for c in earlier if c.starts_at in STORED_DECEMBER}, STORED_DECEMBER,
                         "the fixture reproduces the records stored in production")
        corrected = scan(previous=earlier)
        self.assertEqual({c.starts_at: c.local_id for c in corrected.candidates}, {c.starts_at: c.local_id for c in earlier}, "every stored record is updated in place")
        self.assertEqual({c.starts_at: c.title for c in corrected.candidates}, {start: title for start, (title, _, _) in EXPECTED.items()})
        self.assertEqual(corrected.notes["identities_kept_after_correction"], 8)
        self.assertEqual(corrected.notes["identity_corrections_ambiguous"], 0)
        again = scan(previous=corrected.candidates)
        self.assertEqual({c.starts_at: c.local_id for c in again.candidates}, {c.starts_at: c.local_id for c in earlier}, "a further scan creates no new records")
        self.assertEqual([c.title for c in again.candidates], [c.title for c in corrected.candidates])

    def test_an_ambiguous_earlier_record_is_refused_not_guessed(self):
        current = scan().candidates
        earlier = as_stored_earlier(current)
        twin = replace(next(c for c in earlier if c.starts_at == "2026-12-05T09:00:00-08:00"), local_id="f" * 32)
        result = scan(previous=earlier + [twin])
        december = next(c for c in result.candidates if c.starts_at == "2026-12-05T09:00:00-08:00")
        self.assertNotIn(december.local_id, {twin.local_id, STORED_DECEMBER["2026-12-05T09:00:00-08:00"]}, "two earlier records at that instant: neither is chosen")
        self.assertEqual(result.notes["identity_corrections_ambiguous"], 1)
        self.assertEqual(result.notes["identities_kept_after_correction"], 7)

    def test_an_earlier_record_is_not_claimed_when_two_new_occurrences_share_its_instant(self):
        page = listing("<h4>Craft Fair</h4><h5>Saturday December 5, 2026</h5><p>9:00am-3:00pm</p>",
                       "<h4>Wreath Workshop</h4><h5>Saturday December 5, 2026</h5><p>9:00am-11:00am</p>")
        current = scan(page).candidates
        self.assertEqual(sorted(c.title for c in current), ["Craft Fair", "Wreath Workshop"])
        old = replace(current[0], title="Saturday December 5, 2026", local_id="e" * 32)
        result = scan(page, previous=[old])
        self.assertNotIn(old.local_id, {c.local_id for c in result.candidates})
        self.assertEqual(result.notes["identity_corrections_ambiguous"], 2)

    def test_a_record_whose_title_was_genuine_is_never_retitled_into_another_event(self):
        page = listing("<h4>Craft Fair</h4><h5>Saturday December 5, 2026</h5><p>9:00am-3:00pm</p>")
        current, = scan(page).candidates
        old = replace(current, title="Pottery Sale", local_id="d" * 32)
        result = scan(page, previous=[old])
        self.assertNotEqual(result.candidates[0].local_id, old.local_id, "only a record titled by a date line is taken to be this occurrence")


class Headings(unittest.TestCase):
    def test_a_descriptive_heading_that_contains_a_date_stays_the_title(self):
        page = listing("<h4>Summer Fair — December 5</h4><p>Saturday, December 5, 2026</p><p>9:00am-3:00pm</p>")
        self.assertEqual([c.title for c in scan(page).candidates], ["Summer Fair — December 5"])

    def test_a_date_heading_with_no_title_directly_above_it_makes_no_candidate(self):
        page = listing("<h4>Craft Fair</h4><p>Our fair is held twice a year with over fifty fine local artisans.</p><h5>Saturday December 5, 2026</h5><p>9:00am-3:00pm</p>")
        self.assertEqual(scan(page).candidates, (), "the date is not a title, and the page's title is not invented in its place")

    def test_a_date_heading_is_not_given_to_a_lower_ranked_heading_above_it(self):
        page = listing("<h5>Craft Fair</h5><h3>Saturday December 5, 2026</h3><p>9:00am-3:00pm</p>")
        self.assertEqual(scan(page).candidates, ())


if __name__ == "__main__":
    unittest.main()
