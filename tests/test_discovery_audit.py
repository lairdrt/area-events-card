"""The adversarial regression matrix for generic website discovery (listing -> event link -> event page -> merge -> candidate) and for producer renames.

Each test names the case it covers (A1.. = crawling and limits, B = extraction, C = merging and identity, D = renames and bookkeeping). Real Bates listing/detail copies are used
wherever they exist; the rest are synthetic counterexamples in the same layout. See tests/test_event_detail_pages.py for the happy paths and the fair-scheduling cases.
"""
import json
import unittest

from area_events.discovery.producer import candidate_digest
from tests.test_event_detail_pages import CRUISERS, DETAIL, LISTING, PAGE, SITE, crawl, detail_page, site

VC = "Christmas in Valley Center"


def ids(result):
    return {c.local_id for c in result.candidates}


def listing_page(*cards, extra=""):
    return "<html><body>" + extra + "<ul>" + "".join(cards) + "</ul></body></html>"


def card(title, date_text, link=None, time_text=None):
    body = f"<h2>{title}</h2><p>Date: {date_text}</p>" + (f"<p>Time: {time_text}</p>" if time_text else "")
    return f'<li class="event-item"><a href="{link}">{body}</a></li>' if link else f'<li class="event-item">{body}</li>'


class Crawling(unittest.TestCase):
    def test_A1_every_starting_url_is_read_even_when_the_first_site_fills_the_candidate_limit(self):
        a, pa = site("alpha.example", 5, "Alpha")
        b, pb = site("beta.example", 3, "Beta")
        result, client = crawl({**pa, **pb}, urls=(a, b), max_pages=2, max_depth=0, max_events=4)
        self.assertEqual(client.requests, [a, b], "the second website was still inspected")
        self.assertEqual(len(result.candidates), 4)
        self.assertTrue(result.notes["candidate_limit_reached"])
        self.assertEqual(result.notes["events_not_added"], 4)
        self.assertEqual(result.notes["candidate_limit"], 4)

    def test_A2_the_candidate_limit_does_not_stop_event_pages_from_enriching_known_candidates(self):
        a, pages = site("alpha.example", 4, "Alpha", generic=True)
        pages["https://alpha.example/events/calendar"] = "<p>calendar</p>"
        result, client = crawl(pages, urls=(a,), max_pages=12, max_depth=1, max_events=3)
        self.assertEqual(len(result.candidates), 3)
        self.assertEqual(sum(1 for c in result.candidates if c.venue == "Alpha"), 3, "the known candidates were enriched from their event pages")
        self.assertNotIn("https://alpha.example/events/calendar", client.requests, "generic pages cannot add anything once the limit is reached")
        stats = json.loads(result.notes["by_website"])["alpha.example"]
        self.assertGreaterEqual(stats["links_over_cap"] + stats["pages_skipped_candidate_limit"] + stats["pages_unvisited"], 0)

    def test_A3_minimum_and_maximum_budgets(self):
        a, pages = site("alpha.example", 30, "Alpha")
        smallest, client = crawl(pages, urls=(a,), max_pages=1, max_depth=2)
        self.assertEqual(client.requests, [a])
        self.assertEqual(len(smallest.candidates), 30)
        largest, client = crawl(pages, urls=(a,), max_pages=24, max_depth=2)
        self.assertLessEqual(len(client.requests), 24)
        self.assertEqual(len(largest.candidates), 30, "following links never adds or drops listing occurrences")
        self.assertLessEqual(json.loads(largest.notes["by_website"])["alpha.example"]["event_pages_followed"], 18, "the per-page link cap holds")

    def test_A4_a_failed_site_costs_the_others_nothing_and_two_starts_on_one_host_share_fairly(self):
        a, pa = site("alpha.example", 3, "Alpha")
        result, client = crawl(pa, urls=("https://dead.example/events", a), max_pages=6, max_depth=1)
        by_site = json.loads(result.notes["by_website"])
        self.assertEqual(by_site["dead.example"]["pages_failed"], 1)
        self.assertEqual(by_site["alpha.example"]["event_pages_followed"], 3)
        one, p1 = site("same.example", 3, "One")
        pages = {**p1, "https://same.example/more": listing_page(card("same.example Event 9", "Sat., Nov. 9, 2026", "/e/9", "9:00am to 1:00pm"))}
        pages["https://same.example/e/9"] = detail_page("same.example Event 9", "Saturday, November 9, 2026", "9:00am to 1:00pm", location="Nine")
        result, client = crawl(pages, urls=(one, "https://same.example/more"), max_pages=6, max_depth=1)
        self.assertEqual(len(client.requests), len(set(client.requests)), "no page is fetched twice")
        self.assertEqual(len(result.candidates), 4)

    def test_A5_event_links_come_before_navigation(self):
        a, pages = site("alpha.example", 3, "Alpha", generic=True)
        result, client = crawl(pages, urls=(a,), max_pages=4, max_depth=1)
        self.assertEqual([u.rsplit("/", 1)[-1] for u in client.requests[1:]], ["1", "2", "3"])

    def test_A6_repeated_links_tracking_variants_and_loops_cost_one_fetch_and_make_one_candidate(self):
        page = "https://loop.example/events"
        html = listing_page(card("Loop Fest", "Sat., Nov. 7, 2026", "/e/1?utm_source=a", "9:00am to 1:00pm"), card("Loop Fest", "Sat., Nov. 7, 2026", "/e/1#top", "9:00am to 1:00pm"),
                            extra='<a href="/events">Events</a><a href="/events/">Events</a>')
        detail = detail_page("Loop Fest", "Saturday, November 7, 2026", "9:00am to 1:00pm", location="Loop Hall") + '<a href="/events">back to all events</a>'
        result, client = crawl({page: html, "https://loop.example/e/1": detail}, urls=(page,), max_pages=10, max_depth=2)
        self.assertEqual(client.requests, [page, "https://loop.example/e/1"])
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].venue, "Loop Hall")

    def test_A7_the_notes_tell_the_causes_of_missing_coverage_apart(self):
        a, pages = site("alpha.example", 30, "Alpha")
        pages["https://alpha.example/e/2"] = None
        del pages["https://alpha.example/e/2"]
        budget, _ = crawl(pages, urls=(a,), max_pages=6, max_depth=1)
        stats = json.loads(budget.notes["by_website"])["alpha.example"]
        self.assertTrue(budget.notes["page_budget_exhausted"])
        self.assertEqual((stats["event_pages_followed"], stats["event_pages_failed"]), (5, 1), "a failure is counted apart from a skip")
        self.assertEqual(stats["links_over_cap"], 13, "links beyond the per-site cap on queued pages")
        self.assertEqual(stats["pages_unvisited"], 12, "queued but out of budget")
        self.assertFalse(budget.notes["candidate_limit_reached"])
        limited, _ = crawl(pages, urls=(a,), max_pages=6, max_depth=1, max_events=10)
        self.assertTrue(limited.notes["candidate_limit_reached"])
        self.assertFalse(limited.notes["page_budget_exhausted"] and not limited.notes["pages_unvisited"])


class Extraction(unittest.TestCase):
    def test_B8_each_occurrence_has_its_own_start_and_end(self):
        result, _ = crawl({PAGE: LISTING}, max_depth=0)
        vc = {c.starts_at: c.ends_at for c in result.candidates if c.title == VC}
        self.assertEqual(vc, {"2026-12-11T16:00:00-08:00": "2026-12-11T20:00:00-08:00", "2026-12-12T09:30:00-08:00": "2026-12-12T14:30:00-08:00"})

    def test_B8_the_same_in_a_page_whose_dates_and_times_are_on_separate_lines(self):
        html = ("<html><body><h1>Two Day Fest</h1><p>Date: Friday, December 11, 2026</p><p>Time: 4:00pm - 8:00pm</p>"
                "<p>Date: Saturday, December 12, 2026</p><p>Time: 9:30am - 2:30pm</p></body></html>")
        result, _ = crawl({PAGE: html}, max_depth=0)
        self.assertEqual({(c.starts_at, c.ends_at) for c in result.candidates},
                         {("2026-12-11T16:00:00-08:00", "2026-12-11T20:00:00-08:00"), ("2026-12-12T09:30:00-08:00", "2026-12-12T14:30:00-08:00")})

    def test_B9_a_date_range_with_one_set_of_hours_stays_separate_occurrences(self):
        result, _ = crawl({PAGE: LISTING}, max_depth=0)
        fair = sorted((c.starts_at, c.ends_at) for c in result.candidates if c.title.startswith("Christmas on the Farm"))
        self.assertEqual(fair, [("2026-11-07T09:00:00-08:00", "2026-11-07T16:00:00-08:00"), ("2026-11-08T09:00:00-08:00", "2026-11-08T16:00:00-08:00")])

    def test_B10_overnight_missing_end_and_no_time_are_not_invented(self):
        html = ("<html><body><h1>Night Owls</h1><p>Date: Saturday, November 7, 2026</p><p>Time: 10:00pm - 2:00am</p></body></html>")
        night, _ = crawl({PAGE: html}, max_depth=0)
        self.assertEqual((night.candidates[0].starts_at, night.candidates[0].ends_at), ("2026-11-07T22:00:00-08:00", "2026-11-08T02:00:00-08:00"))
        open_end, _ = crawl({PAGE: html.replace("10:00pm - 2:00am", "10:00pm")}, max_depth=0)
        self.assertIsNone(open_end.candidates[0].ends_at)
        no_time, _ = crawl({PAGE: html.replace("<p>Time: 10:00pm - 2:00am</p>", "")}, max_depth=0)
        self.assertEqual(no_time.candidates, (), "a date without a time is never turned into an event")

    def test_B11_offsets_are_compared_as_instants(self):
        ld = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"Event","name":"Instant Fair","startDate":"2026-11-07T17:00:00+00:00",'
              '"location":{"@type":"Place","name":"Zulu Hall","address":"1 Z St, Valley Center, CA"}}</script>')
        listing = listing_page(card("Instant Fair", "Sat., Nov. 7, 2026", "/e/1", "9:00am to 1:00pm"))
        result, _ = crawl({PAGE: listing, SITE + "/e/1": f"<html><head>{ld}</head><body><h1>Instant Fair</h1></body></html>"}, max_depth=1, max_pages=4)
        fair, = result.candidates
        self.assertEqual((fair.venue, fair.url), ("Zulu Hall", SITE + "/e/1"))
        self.assertEqual(fair.verification.status, "corroborated", "09:00-08:00 and 17:00+00:00 are the same instant")

    def test_B12_an_event_page_without_a_date_still_enriches_its_listing_occurrence(self):
        listing = listing_page(card("Dateless Detail Day", "Sat., Nov. 7, 2026", "/e/1", "9:00am to 1:00pm"))
        undated = ("<html><body><h1>Dateless Detail Day</h1><p>Location: Maple Hall</p><p>Address: 5 Maple Way, Valley Center, CA</p>"
                   "<p>Description: Bring a chair and a neighbour for a day of stories.</p></body></html>")
        result, _ = crawl({PAGE: listing, SITE + "/e/1": undated}, max_depth=1, max_pages=4)
        day, = result.candidates
        self.assertEqual((day.venue, day.address, day.starts_at), ("Maple Hall", "5 Maple Way, Valley Center, CA", "2026-11-07T09:00:00-08:00"))
        self.assertIn("neighbour", day.description)
        self.assertEqual(day.url, SITE + "/e/1")
        self.assertEqual(day.verification.status, "unverified", "an undated page corroborates nothing about when")
        self.assertEqual(result.notes["event_pages_facts_only"], 1)

    def test_B13_missing_fields_keep_the_listing_values(self):
        listing = listing_page(card("Half Page", "Sat., Nov. 7, 2026", "/e/1", "9:00am to 1:00pm"))
        thin = detail_page("Half Page", "Saturday, November 7, 2026", "9:00am to 1:00pm", location="Half Hall", address=None, description="x")
        result, _ = crawl({PAGE: listing.replace("</ul>", "<li>Location: Elsewhere</li></ul>"), SITE + "/e/1": thin}, max_depth=1, max_pages=4)
        half, = result.candidates
        self.assertEqual(half.venue, "Half Hall")
        self.assertEqual(half.ends_at, "2026-11-07T13:00:00-08:00", "the end the listing stated survives")

    def test_B14_conflicting_facts_are_resolved_predictably_and_the_disagreement_is_kept(self):
        listing = listing_page(card("Conflict Day", "Sat., Nov. 7, 2026", "/e/1", "9:00am to 1:00pm"))
        conflicting = detail_page("Conflict Day", "Saturday, November 7, 2026", "10:00am to 2:00pm", location="Real Hall")
        result, _ = crawl({PAGE: listing, SITE + "/e/1": conflicting}, max_depth=1, max_pages=4)
        day, = result.candidates
        self.assertEqual((day.starts_at, day.ends_at, day.venue), ("2026-11-07T10:00:00-08:00", "2026-11-07T14:00:00-08:00", "Real Hall"), "the event page wins")
        notes = [line for line in day.verification.evidence if "disagree" in line]
        self.assertTrue(any("start:" in line for line in notes) and any("end:" in line for line in notes), notes)
        self.assertEqual(day.verification.status, "unverified")

    def test_B15_structured_data_and_visible_text_make_one_candidate(self):
        ld = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"Event","name":"Both Ways Fair","startDate":"2026-11-07T09:00:00-08:00",'
              '"endDate":"2026-11-07T13:00:00-08:00","location":{"@type":"Place","name":"Both Hall","address":"1 B St, Valley Center, CA"}}</script>')
        html = f"<html><head>{ld}</head><body><h1>Both Ways Fair</h1><p>Date: Saturday, November 7, 2026</p><p>Time: 9:00am - 1:00pm</p><p>Location: Both Hall</p></body></html>"
        result, _ = crawl({PAGE: html}, max_depth=0)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].address, "1 B St, Valley Center, CA", "the structured claim is the one kept")

    def test_B16_related_events_and_navigation_on_an_event_page_are_not_the_linked_event(self):
        listing = listing_page(card("Main Event", "Sat., Nov. 7, 2026", "/e/1", "9:00am to 1:00pm"))
        page = ("<html><body><h1>Main Event</h1><p>Date: Saturday, November 7, 2026</p><p>Time: 9:00am to 1:00pm</p><p>Location: Main Hall</p>"
                "<h2>Related Event Elsewhere</h2><p>Date: Saturday, November 7, 2026</p><p>Time: 9:00am to 1:00pm</p><p>Location: Wrong Hall</p></body></html>")
        result, _ = crawl({PAGE: listing, SITE + "/e/1": page}, max_depth=1, max_pages=4)
        main, = result.candidates
        self.assertEqual((main.title, main.venue), ("Main Event", "Main Hall"))
        self.assertEqual(result.notes["event_page_unrelated_events_ignored"], 1)

    def test_B16_a_registration_link_is_not_taken_for_the_event_page(self):
        html = ('<ul><li class="event-item"><h2>Link Test</h2><p>Date: Sat., Nov. 7, 2026</p><p>Time: 9:00am to 1:00pm</p>'
                '<a href="/register/7">Register now</a><a href="/e/1">Learn more</a></li></ul>')
        result, client = crawl({PAGE: html, SITE + "/e/1": detail_page("Link Test", "Saturday, November 7, 2026", "9:00am to 1:00pm", location="Link Hall")},
                               max_depth=1, max_pages=4)
        self.assertEqual(client.requests, [PAGE, SITE + "/e/1"])
        self.assertEqual(result.candidates[0].venue, "Link Hall")

    def test_B17_an_event_page_with_two_unrelated_events_gives_each_listing_occurrence_only_its_own_facts(self):
        listing = listing_page(card("Alpha Concert", "Sat., Nov. 7, 2026", "/e/shared", "9:00am to 1:00pm"), card("Beta Workshop", "Sat., Nov. 7, 2026", "/e/shared", "9:00am to 1:00pm"))
        shared = ("<html><body><h1>Alpha Concert</h1><p>Date: Saturday, November 7, 2026</p><p>Time: 9:00am to 1:00pm</p><p>Location: Alpha Hall</p>"
                  "<h2>Beta Workshop</h2><p>Date: Saturday, November 7, 2026</p><p>Time: 9:00am to 1:00pm</p><p>Location: Beta Room</p></body></html>")
        result, _ = crawl({PAGE: listing, SITE + "/e/shared": shared}, max_depth=1, max_pages=4)
        self.assertEqual({c.title: c.venue for c in result.candidates}, {"Alpha Concert": "Alpha Hall", "Beta Workshop": "Beta Room"})


class Identity(unittest.TestCase):
    def test_C18_a_page_of_the_same_site_never_merges_into_an_unrelated_candidate(self):
        listing = listing_page(card("Quilt Show", "Sat., Nov. 7, 2026", None, "9:00am to 1:00pm"), extra='<a href="/events/other">All events</a>')
        other = detail_page("Chess Tournament", "Saturday, November 7, 2026", "9:00am to 1:00pm", location="Chess Hall")
        result, _ = crawl({PAGE: listing, SITE + "/events/other": other}, max_depth=1, max_pages=4)
        self.assertEqual({c.title: c.venue for c in result.candidates}, {"Quilt Show": None, "Chess Tournament": "Chess Hall"})

    def test_C19_two_occurrences_linking_to_one_page_keep_their_own_times_and_facts(self):
        listing = listing_page(card("Twin Show", "Fri., Dec. 11, 2026", "/e/twin", "4:00pm - 8:00pm"), card("Twin Show", "Sat., Dec. 12, 2026", "/e/twin", "9:30am - 2:30pm"))
        page = ("<html><body><h1>Twin Show</h1><p>Date &amp; Time: Friday, December 11, 2026 from 4:00pm - 8:00pm</p><p>Date &amp; Time: Saturday, December 12, 2026 from 9:30am - 2:30pm</p>"
                "<p>Location: Twin Hall</p></body></html>")
        result, _ = crawl({PAGE: listing, SITE + "/e/twin": page}, max_depth=1, max_pages=4)
        got = {c.starts_at: (c.ends_at, c.venue, c.url) for c in result.candidates}
        self.assertEqual(got, {"2026-12-11T16:00:00-08:00": ("2026-12-11T20:00:00-08:00", "Twin Hall", SITE + "/e/twin"),
                               "2026-12-12T09:30:00-08:00": ("2026-12-12T14:30:00-08:00", "Twin Hall", SITE + "/e/twin")})

    def test_C21_enrichment_never_changes_a_candidate_id(self):
        plain, _ = crawl({PAGE: LISTING}, max_depth=0)
        rich, _ = crawl({PAGE: LISTING, CRUISERS: DETAIL}, max_pages=14, max_depth=1)
        self.assertEqual(ids(plain), ids(rich))

    def test_C22_a_corrected_time_keeps_the_stored_id_instead_of_creating_a_second_candidate(self):
        listing = lambda t: listing_page(card("Moving Day", "Sat., Nov. 7, 2026", None, t))   # noqa: E731
        first, _ = crawl({PAGE: listing("4:00pm - 8:00pm")}, max_depth=0)
        corrected, _ = crawl({PAGE: listing("9:30am - 2:30pm")}, max_depth=0, previous=first.candidates)
        self.assertEqual(ids(corrected), ids(first), "the stored candidate is updated in place")
        self.assertEqual(corrected.candidates[0].starts_at, "2026-11-07T09:30:00-08:00")
        self.assertEqual(corrected.notes["identities_kept_after_correction"], 1)
        fresh, _ = crawl({PAGE: listing("9:30am - 2:30pm")}, max_depth=0)
        self.assertNotEqual(ids(fresh), ids(first), "(without the earlier record the corrected time is a new id: the reason for keeping it)")

    def test_C22_an_ambiguous_correction_is_reported_not_guessed(self):
        both = lambda a, b: listing_page(card("Double Bill", "Sat., Nov. 7, 2026", None, a), card("Double Bill", "Sat., Nov. 7, 2026", None, b))   # noqa: E731
        first, _ = crawl({PAGE: both("2:00pm - 4:00pm", "7:00pm - 9:00pm")}, max_depth=0)
        moved, _ = crawl({PAGE: both("2:30pm - 4:30pm", "7:30pm - 9:30pm")}, max_depth=0, previous=first.candidates)
        self.assertEqual(len(moved.candidates), 2)
        self.assertEqual(moved.notes["identity_corrections_ambiguous"], 2)
        self.assertTrue(ids(moved).isdisjoint(ids(first)), "two performances on one day cannot be told apart: new ids, flagged for review")

    def test_C23_a_starting_url_that_is_also_an_event_page_is_neither_lost_nor_duplicated_in_either_order(self):
        both_orders = []
        for urls in ((PAGE, CRUISERS), (CRUISERS, PAGE)):
            result, _ = crawl({PAGE: LISTING, CRUISERS: DETAIL}, urls=urls, max_pages=14, max_depth=1)
            cruisers = [c for c in result.candidates if c.title.startswith("East County")]
            self.assertEqual(len(cruisers), 1, urls)
            self.assertEqual((cruisers[0].venue, cruisers[0].url), ("Bates Nut Farm", CRUISERS))
            both_orders.append(ids(result))
        listing_only, _ = crawl({PAGE: LISTING}, max_depth=0)
        self.assertEqual(both_orders[0], ids(listing_only), "the listing's id wins")
        self.assertEqual(both_orders[1], ids(listing_only), "whichever page is read first")

    def test_C24_unreadable_event_pages_never_erase_what_an_earlier_scan_learned(self):
        first, _ = crawl({PAGE: LISTING, CRUISERS: DETAIL}, max_pages=14, max_depth=1)
        for budget in (14, 3, 1):
            again, _ = crawl({PAGE: LISTING}, max_pages=14 if budget == 14 else budget, max_depth=1 if budget > 1 else 0, previous=first.candidates)
            cruisers, = [c for c in again.candidates if c.title.startswith("East County")]
            self.assertEqual((cruisers.venue, cruisers.address, cruisers.url), ("Bates Nut Farm", "15954 Woods Valley Road, Valley Center, CA", CRUISERS), budget)
            self.assertIn("earlier scan", " ".join(cruisers.verification.evidence))
            self.assertEqual(ids(again), ids(first))

    def test_C24_a_page_that_is_read_and_no_longer_has_the_facts_is_believed(self):
        first, _ = crawl({PAGE: LISTING, CRUISERS: DETAIL}, max_pages=14, max_depth=1)
        bare = "<html><body><h1>East County Cruisers Car Show and Toy Drive</h1><p>Come back soon.</p></body></html>"
        again, _ = crawl({PAGE: LISTING, CRUISERS: bare}, max_pages=14, max_depth=1, previous=first.candidates)
        cruisers, = [c for c in again.candidates if c.title.startswith("East County")]
        self.assertIsNone(cruisers.venue, "a successful read is not overridden by an old one")

    def test_C25_repeated_crawls_with_identical_input_are_idempotent(self):
        first, _ = crawl({PAGE: LISTING, CRUISERS: DETAIL}, max_pages=14, max_depth=1)
        second, _ = crawl({PAGE: LISTING, CRUISERS: DETAIL}, max_pages=14, max_depth=1, previous=first.candidates)
        self.assertEqual([candidate_digest(c) for c in first.candidates], [candidate_digest(c) for c in second.candidates])
        third, _ = crawl({PAGE: LISTING, CRUISERS: DETAIL}, max_pages=14, max_depth=1, previous=second.candidates)
        self.assertEqual([candidate_digest(c) for c in second.candidates], [candidate_digest(c) for c in third.candidates])

    def test_C26_budgets_and_link_order_never_change_the_ids_of_known_events(self):
        base, _ = crawl({PAGE: LISTING, CRUISERS: DETAIL}, max_pages=14, max_depth=1)
        for budget, depth in ((1, 0), (2, 1), (5, 1), (24, 2)):
            other, _ = crawl({PAGE: LISTING, CRUISERS: DETAIL}, max_pages=budget, max_depth=depth, previous=base.candidates)
            self.assertEqual(ids(other), ids(base), (budget, depth))
        reordered = LISTING.replace('<li class="event-item', '<li data-x class="event-item')
        shuffled, _ = crawl({PAGE: reordered, CRUISERS: DETAIL}, max_pages=14, max_depth=1)
        self.assertEqual(ids(shuffled), ids(base))

    def test_C27_the_occurrence_first_seen_on_an_event_page_is_reported_as_an_identity_risk(self):
        extra = detail_page("East County Cruisers Car Show and Toy Drive", "Saturday, November 14, 2026", "9:00am to 2:00pm")
        result, _ = crawl({PAGE: LISTING, CRUISERS: extra}, max_pages=14, max_depth=1)
        self.assertEqual(result.notes["event_page_only_occurrences"], 1, "if the listing later adds it, its id will differ: surfaced for review")


class Persistence(unittest.TestCase):
    """The producer's working state and `previous` records survive the real database round trip."""

    def test_C24_facts_from_an_event_page_survive_a_scan_in_which_that_page_cannot_be_read(self):
        import os
        import tempfile
        from area_events import app as app_module
        from area_events.control.service import CreateAreaRequest, CreateSourceRequest
        from tests.test_discovery_service import Clock
        from tests.test_event_detail_pages import SiteClient
        pages = {PAGE: LISTING, CRUISERS: DETAIL}
        client = SiteClient(pages)
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            app = app_module.build(os.path.join(tmp, "discovery.db"), clock=clock, fetch_client=client, request_interval=0)
            try:
                app.register_known_sources()
                app.control.create_source(CreateSourceRequest("generic_sites", "sources_producer", "Local Craft Fairs", True, 60,
                                                              {"urls": [PAGE], "timezone": "America/Los_Angeles", "max_depth": 1, "max_pages": 14}))
                app.control.create_area(CreateAreaRequest("Valley Center", 33.2, -117.0, 30, sources=("generic_sites",)))

                def stored():
                    with app.persistence._store.read() as db:
                        rows = db.execute("SELECT candidate_key, payload_json, state_json FROM candidates WHERE candidate_key LIKE 'generic_sites:%'").fetchall()
                    return {json.loads(r["payload_json"])["title"] + "|" + json.loads(r["payload_json"])["starts_at"]: (json.loads(r["payload_json"]), json.loads(r["state_json"])) for r in rows}

                def extract():
                    while app.extraction.run_once().claimed:
                        pass

                def scan():
                    clock.t += 7200
                    app.scheduler.plan()
                    app.service.run_until_idle(5)
                    extract()
                app.scheduler.plan()
                app.service.run_until_idle(5)
                extract()
                first = stored()
                key = "East County Cruisers Car Show and Toy Drive|2026-11-07T09:00:00-08:00"
                self.assertEqual(len(first), 13)
                self.assertEqual(first[key][0]["venue"], "Bates Nut Farm")
                self.assertEqual(first[key][1]["detail_url"], CRUISERS)
                del pages[CRUISERS]   # the event page is gone (or rate-limited) on the next scan
                scan()
                second = stored()
                self.assertEqual(set(second), set(first), "no candidate was added or lost")
                self.assertEqual(second[key][0]["venue"], "Bates Nut Farm", "the stored facts were not erased")
                self.assertEqual(second[key][0]["address"], first[key][0]["address"])
                with app.persistence._store.read() as db:
                    self.assertEqual(db.execute("SELECT venue_name FROM events WHERE title LIKE 'East County%'").fetchone()[0], "Bates Nut Farm")
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 13)
            finally:
                app.close()


class DepthContainment(unittest.TestCase):
    """max_depth 1 reads listing -> event page and stops: a site's home page, linked from its event pages and listing the same events again on another page, is never reached.
    (Production 2026-10-29: at depth 2 the Bates home page was read and four extra candidates appeared under listing_url https://batesnutfarm.biz/.)"""

    def pages(self):
        listing = listing_page(card("Halloween Finale", "Sat., Oct. 31, 2026", "/e/1", "9:00am to 4:30pm"))
        event = detail_page("Halloween Finale", "Saturday, October 31, 2026", "9:00am to 4:30pm", location="Bates Nut Farm").replace("</article>", '</article><a href="/">Upcoming events</a>')
        home = listing_page(card("Halloween Finale", "Sat., Oct. 31, 2026", "/e/1", "9:00am to 4:30pm"), card("Alias | Live Music", "Sat., Oct. 31, 2026", "/e/2", "12:00pm to 3:00pm"))
        return {PAGE: listing, SITE + "/e/1": event, SITE + "/": home}

    def test_depth_one_follows_event_pages_but_never_the_pages_they_link_to(self):
        result, client = crawl(self.pages(), max_depth=1, max_pages=20)
        self.assertEqual(client.requests, [PAGE, SITE + "/e/1"])
        finale, = result.candidates
        self.assertEqual((finale.venue, finale.url, finale.provenance.listing_url), ("Bates Nut Farm", SITE + "/e/1", PAGE))

    def test_depth_two_reaches_the_home_page_and_its_second_listing(self):
        result, client = crawl(self.pages(), max_depth=2, max_pages=20)
        self.assertIn(SITE + "/", client.requests, "the hazard depth 1 contains")
        self.assertIn(SITE + "/", {c.provenance.listing_url for c in result.candidates}, "candidates attributed to the home page as a second listing")


class TimeBleed(unittest.TestCase):
    """A date takes only the time stated with it: the search for a time stops at the next date line and never reuses a line another date took."""

    def starts(self, body):
        result, _ = crawl({PAGE: f"<html><body><h1>Bleed Fest</h1>{body}</body></html>"}, max_depth=0)
        return sorted((c.starts_at, c.ends_at) for c in result.candidates)

    def test_a_date_does_not_borrow_the_time_of_the_next_date(self):
        got = self.starts("<p>Date: Saturday, November 7, 2026</p><p>Date: Sunday, November 8, 2026</p><p>Time: 9:00am to 1:00pm</p>")
        self.assertEqual(got, [("2026-11-08T09:00:00-08:00", "2026-11-08T13:00:00-08:00")], "Nov 7 states no time of its own, so none is invented")

    def test_a_time_before_two_dates_is_not_shared_between_them(self):
        got = self.starts("<p>Time: 9:00am to 1:00pm</p><p>Date: Saturday, November 7, 2026</p><p>Date: Sunday, November 8, 2026</p>")
        self.assertEqual(got, [("2026-11-07T09:00:00-08:00", "2026-11-07T13:00:00-08:00")])

    def test_a_time_that_followed_one_date_is_not_reused_by_the_next_date(self):
        got = self.starts("<p>Date: Saturday, November 7, 2026</p><p>Time: 9:00am to 1:00pm</p><p>Date: Sunday, November 8, 2026</p>")
        self.assertEqual(got, [("2026-11-07T09:00:00-08:00", "2026-11-07T13:00:00-08:00")])

    def test_missing_first_time_followed_by_a_timed_second_occurrence(self):
        got = self.starts("<p>Date: Saturday, November 7, 2026</p><p>Date: Sunday, November 8, 2026 10:00am to 2:00pm</p>")
        self.assertEqual(got, [("2026-11-08T10:00:00-08:00", "2026-11-08T14:00:00-08:00")])

    def test_separate_date_and_time_lines_with_distinct_times_still_work(self):
        got = self.starts("<p>Date: Saturday, November 7, 2026</p><p>Time: 9:00am to 1:00pm</p><p>Date: Sunday, November 8, 2026</p><p>Time: 2:00pm to 6:00pm</p>")
        self.assertEqual(got, [("2026-11-07T09:00:00-08:00", "2026-11-07T13:00:00-08:00"), ("2026-11-08T14:00:00-08:00", "2026-11-08T18:00:00-08:00")])

    def test_a_range_that_states_one_time_shares_it(self):
        got = self.starts("<p>Date: Saturday and Sunday, November 7 and 8, 2026</p><p>Time: 9:00am to 1:00pm</p>")
        self.assertEqual(got, [("2026-11-07T09:00:00-08:00", "2026-11-07T13:00:00-08:00"), ("2026-11-08T09:00:00-08:00", "2026-11-08T13:00:00-08:00")])

    def test_the_bates_dec_11_and_12_times_and_every_id_are_unchanged(self):
        result, _ = crawl({PAGE: LISTING}, max_depth=0)
        vc = {c.starts_at: c.ends_at for c in result.candidates if c.title == VC}
        self.assertEqual(vc, {"2026-12-11T16:00:00-08:00": "2026-12-11T20:00:00-08:00", "2026-12-12T09:30:00-08:00": "2026-12-12T14:30:00-08:00"})
        again, _ = crawl({PAGE: LISTING}, max_depth=0, previous=result.candidates)
        self.assertEqual(ids(again), ids(result))


class StrictAssociation(unittest.TestCase):
    """An event page speaks for a listing occurrence only on strong evidence of the same event."""

    def venue_of(self, listing_title, page_title):
        listing = listing_page(card(listing_title, "Sat., Nov. 7, 2026", "/e/1", "9:00am to 1:00pm"))
        page = detail_page(page_title, "Saturday, November 7, 2026", "9:00am to 1:00pm", location="Page Hall")
        result, _ = crawl({PAGE: listing, SITE + "/e/1": page}, max_depth=1, max_pages=4)
        return result, {c.title: c.venue for c in result.candidates}

    def test_generic_words_alone_never_relate_two_events(self):
        for listing_title, page_title in (("Holiday Craft Fair", "Spring Craft Fair"), ("Summer Concert", "Winter Concert"), ("Harvest Festival", "Spring Festival"), ("Craft Fair", "Holiday Craft Fair")):
            with self.subTest(listing=listing_title, page=page_title):
                result, venues = self.venue_of(listing_title, page_title)
                self.assertIsNone(venues[listing_title], "the other event's place was not taken")
                self.assertEqual(result.notes["event_page_unrelated_events_ignored"], 1)

    def test_exact_and_harmlessly_reformatted_titles_match(self):
        for listing_title, page_title in (("Christmas on the Farm", "Christmas on the Farm"), ("CHRISTMAS ON THE FARM", "Christmas on the Farm"),
                                          ("Christmas on the Farm", "The 2026 Annual Christmas on the Farm"), ("Christmas on the Farm", "Christmas on the Farm at Bates Nut Farm")):
            with self.subTest(listing=listing_title, page=page_title):
                _, venues = self.venue_of(listing_title, page_title)
                self.assertEqual(list(venues.values()), ["Page Hall"])

    def test_the_cards_link_does_not_license_every_related_event_on_the_page(self):
        listing = listing_page(card("Main Event Concert", "Sat., Nov. 7, 2026", "/e/1", "9:00am to 1:00pm"))
        page = ("<html><body><h1>Main Event Concert</h1><p>Date: Saturday, November 7, 2026</p><p>Time: 9:00am to 1:00pm</p><p>Location: Main Hall</p>"
                "<h2>Other Concert</h2><p>Date: Saturday, November 7, 2026</p><p>Time: 9:00am to 1:00pm</p><p>Location: Other Hall</p></body></html>")
        result, _ = crawl({PAGE: listing, SITE + "/e/1": page}, max_depth=1, max_pages=4)
        self.assertEqual({c.title: c.venue for c in result.candidates}, {"Main Event Concert": "Main Hall"})

    def test_two_plausible_owners_at_one_time_are_not_guessed(self):
        listing = listing_page(card("Farm Day Parade", "Sat., Nov. 7, 2026", "/e/1", "9:00am to 1:00pm"), card("Farm Day Picnic", "Sat., Nov. 7, 2026", "/e/1", "9:00am to 1:00pm"))
        page = detail_page("Farm Day", "Saturday, November 7, 2026", "9:00am to 1:00pm", location="Guess Hall")
        result, _ = crawl({PAGE: listing, SITE + "/e/1": page}, max_depth=1, max_pages=4)
        self.assertEqual({c.title: c.venue for c in result.candidates}, {"Farm Day Parade": None, "Farm Day Picnic": None})

    def test_separately_dated_occurrences_sharing_a_page_stay_separate_and_ids_hold(self):
        listing = listing_page(card("Two Day Show", "Sat., Nov. 7, 2026", "/e/1", "9:00am to 1:00pm"), card("Two Day Show", "Sun., Nov. 8, 2026", "/e/1", "2:00pm to 5:00pm"))
        page = ("<html><body><h1>Two Day Show</h1><p>Date: Saturday, November 7, 2026</p><p>Time: 9:00am to 1:00pm</p><p>Date: Sunday, November 8, 2026</p><p>Time: 2:00pm to 5:00pm</p>"
                "<p>Location: Show Hall</p></body></html>")
        before, _ = crawl({PAGE: listing}, max_depth=0)
        result, _ = crawl({PAGE: listing, SITE + "/e/1": page}, max_depth=1, max_pages=4, previous=before.candidates)
        self.assertEqual(ids(result), ids(before))
        self.assertEqual({(c.starts_at, c.venue) for c in result.candidates}, {("2026-11-07T09:00:00-08:00", "Show Hall"), ("2026-11-08T14:00:00-08:00", "Show Hall")})

    def test_an_unambiguous_corrected_time_is_still_matched_and_keeps_its_id(self):
        listing = listing_page(card("Fix Fest", "Sat., Nov. 7, 2026", "/e/1", "9:00am to 1:00pm"))
        before, _ = crawl({PAGE: listing}, max_depth=0)
        result, _ = crawl({PAGE: listing, SITE + "/e/1": detail_page("Fix Fest", "Saturday, November 7, 2026", "10:00am to 2:00pm", location="Fix Hall")},
                          max_depth=1, max_pages=4, previous=before.candidates)
        fest, = result.candidates
        self.assertEqual((fest.local_id, fest.starts_at, fest.venue), (before.candidates[0].local_id, "2026-11-07T10:00:00-08:00", "Fix Hall"))


if __name__ == "__main__":
    unittest.main()
