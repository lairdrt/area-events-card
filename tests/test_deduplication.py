"""Deduplication, persistence and event processing as three separate capabilities.

* Deduplication decides (explicit `Decision`s) through one interface; two interchangeable strategies implement it, and choosing between them is the composition root's argument.
* Processing runs against a second, dictionary-backed Persistence that imports no database.
* Persistence runs the lookup, the decision and the write as one atomic operation: concurrent twins, strategy failures, invalid decisions, stale leases and replays.
"""
import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from area_events.contracts.models import CleanEvent, DecisionRejected, LeaseLost
from area_events.deduplication import Association, Decision, ExistingEvent, Incoming
from area_events.deduplication.exact import ExactMatch
from area_events.deduplication.tolerant import TolerantMatch
from area_events.deduplication import same_event_facts
from area_events.extraction.service import VERSION, ExtractionService, LocalProcessingClient, StoredCandidateExtractor
from area_events.api.events import EventQuery
from area_events.validation.processing import ProcessingService
from tests.db import database
from tests.inmemory import InMemoryProcessing
from tests.records import document, epoch
from tests.test_extraction import Fixture

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = StoredCandidateExtractor.name
START = 1893477600   # 2030-01-01T06:00:00Z


def clean(title="Harbor Concert", starts_at=START, address="500 Harbor Drive, San Diego, CA", venue="Harbor Hall", ends_at=None, location_text=None):
    return CleanEvent(title, starts_at, ends_at, "America/Los_Angeles", None, venue, location_text, address, None, "address" if address else "venue" if venue else "unknown", (), "partial")


def known(event_key, event):
    return ExistingEvent(event_key, event.title, event.starts_at, event.ends_at, event.venue_name, event.address, event.location_text, 1, event.source_url,
                         event.description, event.timezone)


class Lookup:
    def __init__(self, *events):
        self.events = events

    def starting_between(self, earliest, latest, after=None, limit=100):
        events = sorted((e for e in self.events if earliest <= e.starts_at <= latest), key=lambda e: (e.starts_at, e.event_key))
        if after is not None:
            events = [e for e in events if (e.starts_at, e.event_key) > after]
        return tuple(events[:limit])


class NoLookup:
    def starting_between(self, *args, **kwargs):
        raise AssertionError("a candidate that already has a place needs no lookup")


STRATEGIES = {"exact": ExactMatch, "tolerant": TolerantMatch}


class Interface(unittest.TestCase):
    def test_both_strategies_implement_the_same_interface(self):
        for name, make in STRATEGIES.items():
            with self.subTest(name):
                strategy = make()
                self.assertEqual((strategy.name, strategy.version), (name, 6))
                decision = strategy.decide(Incoming("src:a", clean(), None), Lookup())
                self.assertIsInstance(decision, Decision)
                self.assertEqual((decision.action, decision.event_key), ("create", None))
                self.assertIsNone(decision.event_key)

    def test_a_candidate_that_already_has_a_place_keeps_it_under_either_strategy(self):
        for name, make in STRATEGIES.items():
            with self.subTest(name):
                primary = make().decide(Incoming("src:a", clean(), Association("ev-1", "primary")), NoLookup())
                self.assertEqual((primary.action, primary.event_key), ("update", "ev-1"), "the primary description revises its event")
                secondary = make().decide(Incoming("src:b", clean(), Association("ev-1", "duplicate")), NoLookup())
                self.assertEqual((secondary.action, secondary.event_key), ("keep", "ev-1"), "a secondary description never rewrites the event")


class Exact(unittest.TestCase):
    def test_the_same_title_start_and_address_are_one_event_however_they_are_written(self):
        first = clean()
        existing = known("ev-1", first)
        for variant in (clean(title="HARBOR concert!", address="500 harbor drive, san diego, ca"), clean(title=" Harbor  Concert ", address="500 Harbor Drive San Diego CA")):
            decision = ExactMatch().decide(Incoming("other:b", variant, None), Lookup(existing))
            self.assertEqual((decision.action, decision.event_key), ("link", "ev-1"))

    def test_a_different_date_place_or_title_is_a_different_event(self):
        first = clean()
        existing = known("ev-1", first)
        for other in (clean(starts_at=START + 86400), clean(address="900 Other Street, San Diego, CA"), clean(title="Harbor Concert II")):
            self.assertEqual(ExactMatch().decide(Incoming("other:b", other, None), Lookup(existing)).action, "create")

    def test_contradicting_stated_end_time_is_additional_evidence(self):
        existing = known("ev-1", clean(ends_at=START + 7200))
        decision = ExactMatch().decide(Incoming("other:b", clean(ends_at=START + 10800), None), Lookup(existing))
        self.assertEqual(decision.action, "create")

    def test_incomplete_location_evidence_matches_only_when_the_identity_is_exceptionally_strong(self):
        existing = known("ev-1", clean(address=None, venue=None))
        for other in (clean(address=None, venue=None, starts_at=START + 300), clean(address=None, venue=None, title="Harbor Concert Night"), clean()):
            decision = ExactMatch().decide(Incoming("other:b", other, None), Lookup(existing))
            self.assertEqual(decision.action, "create", "nothing is invented to make a placeless description comparable")
        decision = ExactMatch().decide(Incoming("other:b", clean(address=None, venue=None), None), Lookup(existing))
        self.assertEqual((decision.action, decision.event_key), ("link", "ev-1"), "identical title and start, and no place on either side")

    def test_high_confidence_title_edits_match_with_identical_date_place_and_url(self):
        first = clean(title="Holiday Marketplace at Mission Trails Regional Park")
        first = dataclasses.replace(first, source_url="https://mtrp.org/marketplace/", ends_at=START + 7200)
        second = clean(title="Holiday Marketplace Benefitting Mission Trails Regional Park")
        second = dataclasses.replace(second, source_url="https://mtrp.org/marketplace/")
        decision = ExactMatch().decide(Incoming("other:b", second, None), Lookup(known("ev-1", first)))
        self.assertEqual((decision.action, decision.event_key), ("link", "ev-1"))

    def test_similar_titles_do_not_override_conflicting_event_facts(self):
        first = clean(title="Holiday Marketplace at Mission Trails Regional Park")
        first = dataclasses.replace(first, source_url="https://mtrp.org/marketplace/", ends_at=START + 7200)
        variants = (
            dataclasses.replace(first, starts_at=START + 86400),
            dataclasses.replace(first, address="900 Other Street, San Diego, CA"),
            dataclasses.replace(first, ends_at=START + 10800),
            dataclasses.replace(first, source_url="https://other.example/marketplace/", venue_name="Other Hall"),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertFalse(same_event_facts(first, variant))
        self.assertTrue(same_event_facts(first, dataclasses.replace(first, source_url="https://other.example/marketplace/")),
                        "another page alone does not keep the same venue, address and instant apart")


class Tolerant(unittest.TestCase):
    def existing(self, **kwargs):
        event = clean(**kwargs)
        return known("ev-1", event)

    def test_exact_start_is_required_but_ends_may_differ_within_window(self):
        event = clean(ends_at=START + 7200)
        existing = self.existing(ends_at=START + 7500)
        decision = TolerantMatch(window_s=900).decide(Incoming("o:b", event, None), Lookup(existing))
        self.assertEqual((decision.action, decision.event_key), ("link", "ev-1"))
        self.assertEqual(TolerantMatch(window_s=900).decide(Incoming("o:b", clean(starts_at=START + 300), None), Lookup(self.existing())).action, "link",
                         "a start a few minutes apart is within the tolerant strategy's tolerance")
        self.assertEqual(TolerantMatch(window_s=900).decide(Incoming("o:b", clean(starts_at=START + 3600), None), Lookup(self.existing())).action, "create")

    def test_contradicting_places_are_different_events_and_a_missing_address_needs_the_same_venue(self):
        strategy = TolerantMatch()
        self.assertEqual(strategy.decide(Incoming("o:b", clean(address="900 Other Street, San Diego, CA"), None), Lookup(self.existing())).action, "create")
        self.assertEqual(strategy.decide(Incoming("o:b", clean(address=None, venue="Harbor Hall"), None), Lookup(self.existing())).action, "link")
        self.assertEqual(strategy.decide(Incoming("o:b", clean(address=None, venue="Elsewhere Hall"), None), Lookup(self.existing())).action, "create")
        self.assertEqual(strategy.decide(Incoming("o:b", clean(address=None, venue=None), None), Lookup(self.existing())).action, "create", "no place at all: not matched")

    def test_materially_different_stated_end_times_do_not_match(self):
        existing = self.existing(ends_at=START + 7200)
        self.assertEqual(TolerantMatch(window_s=900).decide(Incoming("o:b", clean(starts_at=START + 300, ends_at=START + 10800), None), Lookup(existing)).action, "create")

    def test_title_edits_are_weighed_by_similarity_and_never_override_the_place(self):
        original = clean(title="Holiday Marketplace at Mission Trails Regional Park")
        original = dataclasses.replace(original, source_url="https://mtrp.org/marketplace/")
        changed = clean(title="Holiday Marketplace Benefitting Mission Trails Regional Park")
        for url in ("https://mtrp.org/events/", None, "https://other.example/marketplace/"):
            candidate = dataclasses.replace(changed, source_url=url)
            self.assertEqual(TolerantMatch().decide(Incoming("o:b", candidate, None), Lookup(known("ev-1", original))).action, "link",
                             "the permissive strategy accepts a similar title at the same time and place without a shared page")
        for other in (dataclasses.replace(changed, address="900 Other Street, San Diego, CA"), dataclasses.replace(changed, title="Spring Craft Fair at the Park")):
            self.assertEqual(TolerantMatch().decide(Incoming("o:b", other, None), Lookup(known("ev-1", original))).action, "create")


MELOMANO = "Melómano Entertainment - Salsa & Bachata Lessons"
RUFFIN = "4255 Ruffin Rd #200, San Diego, CA 92123"
TAKEOVER_URL = ("https://www.facebook.com/events/4255-ruffin-road-suite-200-san-diego-ca-united-states-california-92123/"
                "the-college-takeover-night-fall-edition-mel%C3%B3mano-x-ucsd-x-sdsu-116/965303699984707/")
TAKEOVER_START = epoch("2026-11-06T20:00:00-08:00")   # Friday 6 November 2026


def takeover(title="The College Takeover Night: Fall Edition", **fields):
    """The College Takeover pair as stored in production on 2026-11-04 (events ev-970a148e6ca1ed277477 and ev-00abe53e2317ec8fe838)."""
    event = dataclasses.replace(clean(title=title, starts_at=TAKEOVER_START, venue=MELOMANO, address=RUFFIN), source_url=TAKEOVER_URL)
    return dataclasses.replace(event, **fields)


LATER = "The College Takeover Night: Fall Edition (Melómano x UCSD x SDSU) 11/6"   # the second listing's title


class VenueTimeIdentity(unittest.TestCase):
    """The exact same start instant at the same specific venue and address, under related titles, is one event whatever the pages say; anything less keeps
    the conservative score, and any stated location distinction, or an unrelated or differently numbered title, keeps the events apart."""

    def decide(self, incoming, *existing, strategy=None):
        return (strategy or ExactMatch()).decide(Incoming("serp_sandiego:later", incoming, None), Lookup(*existing))

    def test_the_college_takeover_pair_is_one_event(self):
        first = known("ev-970a148e6ca1ed277477", takeover())
        later = takeover(LATER)
        self.assertFalse(same_event_facts(takeover(), later, ExactMatch(venue_time_identity=False).profile), "the score alone keeps them apart (titles too different)")
        for strategy in (ExactMatch(), TolerantMatch()):
            with self.subTest(strategy.name):
                decision = self.decide(later, first, strategy=strategy)
                self.assertEqual((decision.action, decision.event_key), ("link", "ev-970a148e6ca1ed277477"))
                self.assertIn("same start instant at the same venue and address", decision.reason)
                self.assertTrue(strategy.matches(known("ev-970a148e6ca1ed277477", takeover()), known("ev-00abe53e2317ec8fe838", later)))

    def test_a_reworded_title_or_another_page_does_not_keep_them_apart(self):
        first = known("ev-1", takeover())
        for later in (takeover(LATER, source_url="https://www.eventbrite.com/e/123"), takeover("THE COLLEGE TAKEOVER NIGHT", source_url=None),
                      takeover("College Takeover Night: Fall Edition at Melómano")):
            with self.subTest(later.title, url=later.source_url):
                self.assertEqual(self.decide(later, first).action, "link")

    def test_unrelated_or_differently_numbered_titles_stay_different_events(self):
        bates = dict(venue="Bates Nut Farm", address="15954 Woods Valley Road, Valley Center, CA", ends_at=START + 3 * 3600)
        cook_off = known("ev-1", clean(title="8th Annual Guns & Hoses Chili Cook-Off Fundraiser", **bates))
        self.assertEqual(self.decide(clean(title="Bayou Brothers | Live Music", **bates), cook_off).action, "create",
                         "two events on one farm at one time (production, 2026-11-08)")
        self.assertEqual(self.decide(clean(title="Chili Cook-Off: Bayou Brothers Live", **bates), known("ev-2", clean(title="Bayou Brothers Live", **bates))).action,
                         "create", "a title that only ends like the other is not its subtitle")
        for first, other in (("Jazz Night", "Rock Night"), ("Yoga on the Green", "Used Book Sale"),
                             ("Harbor Concert", "Harbor Concert 2"), ("Harbor Night", "Harbor Night 1"), ("Harbor Night 1", "Harbor Night 2"),
                             ("Harbor Concert 1", "Harbor Concert 2"), ("Harbor Concert", "Harbor Concert II"), ("Harbor Concert", "Harbor Concert X"),
                             ("Harbor Concert", "Harbor Concert 2030"), ("Harbor Concert", "Harbor Concert: Night Two"), ("Harbor Concert", "Harbor Concert (Part 2)"),
                             ("Harbor Concert: Fall Edition", "Harbor Concert: Fall Edition 2030"), ("Harbor Concert", "Harbor Concert: Second Show")):
            with self.subTest(first=first, other=other):
                self.assertEqual(self.decide(clean(title=other), known("ev-1", clean(title=first))).action, "create")
                self.assertEqual(self.decide(clean(title=first), known("ev-1", clean(title=other))).action, "create", "whichever came first")

    def test_audience_qualifiers_and_words_added_without_a_subtitle_stay_different_events(self):
        for first, other in (("Harbor Concert", "Harbor Concert for Kids"), ("Harbor Concert", "Harbor Concert (Kids)"), ("Harbor Concert", "Harbor Concert: Family Matinee"),
                             ("Blind Date", "Blind"), ("Dinner Date", "Dinner"), ("Harbor Concert Series", "Harbor Concert Series Night"),
                             ("Men's Swim Clinic", "Women's Swim Clinic"), ("Harbor Concert", "Harbor Concert (Sunday)"), ("Harbor Concert", "Harbor Concert: Saturday")):
            with self.subTest(first=first, other=other):
                self.assertEqual(self.decide(clean(title=other), known("ev-1", clean(title=first))).action, "create")
                self.assertEqual(self.decide(clean(title=first), known("ev-1", clean(title=other))).action, "create")

    def test_the_events_own_date_in_a_title_is_no_difference_and_any_other_date_is(self):
        first = known("ev-1", takeover(source_url=None))                          # Friday 6 November 2026, 20:00 in Los Angeles; no page: the identity alone
        base = "The College Takeover Night: Fall Edition"
        for title in (LATER, base + " 11/6", base + " (11/6/26)", base + " - Nov 6th", base + " (November 6, 2026)", base + " - Friday", base + " | Fri Nov. 6"):
            with self.subTest(title):
                self.assertEqual(self.decide(takeover(title, source_url=None), first).action, "link")
        for title in (LATER.replace("11/6", "11/7"), base + " 6/11", base + " 11/6/2027", base + " - Nov 7", base + " - Saturday", base + " 11/6 #2"):
            with self.subTest(title):
                self.assertEqual(self.decide(takeover(title, source_url=None), first).action, "create", "a date that is not the event's own day is a difference")
        march = epoch("2027-03-06T20:00:00-08:00")
        self.assertEqual(self.decide(takeover("Harbor Marathon 6", starts_at=march), known("ev-2", takeover("Harbor Marathon", starts_at=march))).action, "create",
                         "no month inside another word: 'Marathon 6' is no 6 March")

    def test_collaboration_notation_is_no_roman_numeral(self):
        first = known("ev-1", takeover("Fall Edition"))
        self.assertEqual(self.decide(takeover("Fall Edition (Melómano x UCSD x SDSU)"), first).action, "link")
        self.assertEqual(self.decide(takeover("Fall Edition: Salsa I"), first).action, "create")
        self.assertEqual(self.decide(takeover("Fall Edition: Salsa X"), known("ev-2", takeover("Fall Edition: Salsa"))).action, "create")

    def test_repeated_candidates_link_to_the_first_event_and_the_choice_is_stable(self):
        listing = dict(starts_at="2026-11-06T20:00:00-08:00", ends_at=None, venue=MELOMANO, address=RUFFIN, url=TAKEOVER_URL)
        processed = run_in_memory(ExactMatch(), [stored("serp_sandiego:a", "serp_sandiego", title="The College Takeover Night: Fall Edition", **listing),
                                                 stored("serp_sandiego:b", "serp_sandiego", title=LATER, **listing),
                                                 stored("serp_sandiego:c", "serp_sandiego", title="College Takeover Night", **{**listing, "url": "https://www.eventbrite.com/e/123"})])
        self.assertEqual(len(processed.events), 1, "one canonical event")
        self.assertEqual({key: role for key, (_, role) in processed.links.items()},
                         {"serp_sandiego:a": "primary", "serp_sandiego:b": "duplicate", "serp_sandiego:c": "duplicate"})
        # duplicates made before the identity existed: every later candidate goes to the one created first, never to a new event
        twins = (dataclasses.replace(known("ev-b", takeover()), created_order=708), dataclasses.replace(known("ev-a", takeover(LATER)), created_order=734))
        decisions = {self.decide(takeover(LATER), *order).event_key for order in (twins, twins[::-1]) for _ in range(3)}
        self.assertEqual(decisions, {"ev-b"})

    def test_the_first_created_event_wins_over_title_similarity_and_key_order(self):
        incoming = takeover(LATER)
        oldest = dataclasses.replace(known("ev-zzz", takeover("The College Takeover Night")), created_order=1)   # the least similar title, the last key
        nearer = dataclasses.replace(known("ev-mmm", takeover()), created_order=2)
        identical = dataclasses.replace(known("ev-aaa", takeover(LATER)), created_order=3)                     # the same title, the first key
        for strategy in (ExactMatch(), TolerantMatch()):
            for order in ((oldest, nearer, identical), (identical, nearer, oldest), (nearer, identical, oldest)):
                with self.subTest(strategy.name, order=[e.event_key for e in order]):
                    decision = self.decide(incoming, *order, strategy=strategy)
                    self.assertEqual((decision.action, decision.event_key), ("link", "ev-zzz"))
        unordered = (known("ev-aaa", takeover(LATER)), known("ev-zzz", takeover()))
        self.assertEqual(self.decide(incoming, *unordered).action, "create", "an unknown creation order is never guessed from a key or a title")

    def test_different_venues_or_start_times_stay_different_events(self):
        first = known("ev-1", takeover())
        for other in (takeover(LATER, venue_name="Ruffin Road Studios"),                        # another business at the same address
                      takeover(LATER, venue_name=MELOMANO + " Annex"),                           # never a longer name that starts the same way
                      takeover(LATER, address="900 Other Street, San Diego, CA 92123"),          # the same name at another address
                      takeover(LATER, starts_at=TAKEOVER_START + 60), takeover(LATER, starts_at=TAKEOVER_START + 1800)):
            for strategy in (ExactMatch(), TolerantMatch()):
                with self.subTest(other=(other.venue_name, other.address, other.starts_at - TAKEOVER_START), strategy=strategy.name):
                    self.assertEqual(self.decide(other, first, strategy=strategy).action, "create")

    def test_a_shared_building_with_different_suites_or_rooms_is_not_one_venue(self):
        office = dataclasses.replace(takeover("Open House"), venue_name="Ruffin Business Center")
        tour = "Open House: Meet the Tenants"   # related to "Open House", which the score alone would not accept
        first = known("ev-1", office)
        for other in (dataclasses.replace(office, title=tour, address="4255 Ruffin Rd Suite 300, San Diego, CA 92123"),
                      dataclasses.replace(office, title=tour, address="4255 Ruffin Rd, Ste 300, San Diego, CA 92123")):
            with self.subTest(other.address):
                self.assertEqual(self.decide(other, first).action, "create")
        in_room = known("ev-3", dataclasses.replace(office, location_text="Room 10"))
        self.assertEqual(self.decide(dataclasses.replace(office, title=tour, location_text="Room 12"), in_room).action, "create", "another stated room")
        seminar = known("ev-2", dataclasses.replace(office, venue_name="SEAY Library - Room 4.244", address="2400 Speedway, Austin, TX 78712"))
        other_room = dataclasses.replace(office, title=tour, venue_name="SEAY Library - Room 2.110", address="2400 Speedway, Austin, TX 78712")
        self.assertEqual(self.decide(other_room, seminar).action, "create", "a room within the venue's name is part of its identity")
        same_suite = dataclasses.replace(office, title=tour, address="4255 Ruffin Road, Suite 200, San Diego, CA")
        self.assertEqual(self.decide(same_suite, first).action, "link", "the same suite written another way, without a postcode")

    def test_incomplete_location_evidence_never_stands_in_for_the_identity(self):
        first = known("ev-1", takeover())
        for other in (takeover(LATER, address=None),                                            # the venue name alone
                      takeover(LATER, address="4255 Ruffin Rd #200"),                            # no town
                      takeover(LATER, venue_name=None),                                          # the address alone
                      takeover(LATER, venue_name="Virtual", address=None)):
            with self.subTest(venue=other.venue_name, address=other.address):
                self.assertEqual(self.decide(other, first).action, "create")
        self.assertEqual(self.decide(takeover(LATER), known("ev-2", takeover(address=None))).action, "create", "nor on the existing side")

    def test_conflicting_towns_postcodes_end_times_or_written_locations_keep_them_apart(self):
        first = known("ev-1", takeover(ends_at=TAKEOVER_START + 7200, location_text="Main ballroom"))
        for other in (takeover(LATER, ends_at=TAKEOVER_START + 7200, location_text="Main ballroom", address="4255 Ruffin Rd #200, La Mesa, CA 92123"),
                      takeover(LATER, ends_at=TAKEOVER_START + 7200, location_text="Main ballroom", address="4255 Ruffin Rd #200, San Diego, CA 92124"),
                      takeover(LATER, ends_at=TAKEOVER_START + 3600, location_text="Main ballroom"),
                      takeover(LATER, ends_at=TAKEOVER_START + 7200, location_text="Back patio")):
            with self.subTest(address=other.address, ends=other.ends_at - TAKEOVER_START, text=other.location_text):
                self.assertEqual(self.decide(other, first).action, "create")

    def test_compatible_addresses_written_differently_are_the_same_place(self):
        cases = ((("Revolver Dance Hall of Cookeville", "565 South Jefferson Avenue, Suite H, Cookeville, TN 38501"),
                  ("Revolver Dance Hall of Cookeville", "565 S Jefferson Ave Ste H, Cookeville, TN 38501")),
                 (("Melómano", "4255 Ruffin Road #200, San Diego, CA 92123"), ("MELÓMANO", "4255  ruffin rd,  suite 200, San Diego, CA")),
                 (("Embarcadero Marina Park North", "400 Kettner Blvd, San Diego, CA 92101"), ("Embarcadero Marina Park North", "400 Kettner Blvd, San Diego, CA")),
                 (("Bayfront Hall", "100 Bay St, Miami, FL 33101"), ("bayfront  hall", "100 Bay Street, Miami, FL")),
                 (("Red Silo Brewing", "Red Silo Brewing, Cookeville, TN"), ("Red Silo Brewing", "Cookeville, TN 38501")))   # neither states a street
        for (venue_a, address_a), (venue_b, address_b) in cases:
            with self.subTest(venue_a, a=address_a, b=address_b):
                first = known("ev-1", clean(title="Open Mic Night", venue=venue_a, address=address_a))
                self.assertEqual(self.decide(clean(title="Open Mic Night: Hosted by Sam", venue=venue_b, address=address_b), first).action, "link")

    def test_a_street_or_suite_stated_on_one_side_only_is_no_proof_of_one_place(self):
        cases = ((("Red Silo Brewing", "Red Silo Brewing, Cookeville, TN"), ("Red Silo Brewing", "118 W 1st St, Cookeville, TN 38501")),
                 (("Revolver Dance Hall of Cookeville", "565 South Jefferson Avenue, Cookeville, TN 38501"),
                  ("Revolver Dance Hall of Cookeville", "565 S Jefferson Ave Ste H, Cookeville, TN 38501")),
                 ((MELOMANO, RUFFIN), (MELOMANO, "4255 Ruffin Rd, San Diego, CA 92123")))
        for (venue_a, address_a), (venue_b, address_b) in cases:
            for first, other in (((venue_a, address_a), (venue_b, address_b)), ((venue_b, address_b), (venue_a, address_a))):
                with self.subTest(first=first[1], other=other[1]):
                    existing = known("ev-1", clean(title="Open Mic Night", venue=first[0], address=first[1]))
                    self.assertEqual(self.decide(clean(title="Open Mic Night: Hosted by Sam", venue=other[0], address=other[1]), existing).action, "create")
        in_room = known("ev-2", takeover(location_text="Room 10"))
        self.assertEqual(self.decide(takeover(LATER), in_room).action, "create", "a room stated on one side only")

    def test_the_identity_can_be_switched_off(self):
        self.assertEqual(self.decide(takeover(LATER), known("ev-1", takeover()), strategy=ExactMatch(venue_time_identity=False)).action, "create")


class LookupPagination(unittest.TestCase):
    def test_both_strategies_search_past_the_first_200_events(self):
        unrelated = [known(f"ev-{i:03}", clean(title=f"Other {i}")) for i in range(250)]
        target = known("zz-target", clean())
        lookup = Lookup(*(unrelated + [target]))
        for strategy in (ExactMatch(), TolerantMatch()):
            with self.subTest(strategy=strategy.name):
                decision = strategy.decide(Incoming("new:candidate", clean(), None), lookup)
                self.assertEqual((decision.action, decision.event_key), ("link", "zz-target"))


def run_in_memory(strategy, candidates):
    storage = InMemoryProcessing(strategy, candidates)
    clock = lambda: 1.0   # noqa: E731
    worker = ExtractionService(LocalProcessingClient(ProcessingService(storage, clock)), StoredCandidateExtractor(), clock)
    while worker.run_once().claimed:
        pass
    return storage


def stored(key, source, **fields):
    return {"key": key, "source_key": source, "payload": document(**fields)}


class ProcessingOnAnotherPersistence(unittest.TestCase):
    """Event processing and extraction run on a Persistence that is plain Python: they know no database."""

    def candidates(self):
        return [stored("a:1", "a", title="Harbor Concert"),
                stored("b:1", "b", title="HARBOR CONCERT", address="500 harbor drive, san diego, ca"),                      # another source, the same event
                stored("c:1", "c", title="Harbor Concert", starts_at="2030-01-02T18:00:00-08:00"),                          # same title, another day
                stored("d:1", "d", title="Harbor Concert", address="900 Other Street, San Diego, CA"),                      # same title and day, another place
                stored("e:1", "e", title="Harbor Concert", address=None, venue="Harbor Hall"),                              # no address
                stored("f:1", "f", title="Harbor Concert", address=None, venue="Harbor Hall")]                              # no address, same venue

    def test_duplicates_within_one_batch_and_against_existing_events(self):
        exact = run_in_memory(ExactMatch(), self.candidates())
        self.assertEqual(len(exact.events), 5, "a:1 and b:1 are one event; the other day, address, and descriptions with no address stay separate")
        self.assertEqual(sorted(role for _, role in exact.links.values()), ["duplicate", "primary", "primary", "primary", "primary", "primary"])
        self.assertEqual(exact.links["b:1"], exact.links["a:1"][:1] + ("duplicate",))

    def test_replacing_the_strategy_changes_the_outcome_and_nothing_else(self):
        tolerant = run_in_memory(TolerantMatch(), self.candidates())
        groups = {}
        for key, (event_key, _) in tolerant.links.items():
            groups.setdefault(event_key, []).append(key)
        self.assertEqual(sorted(sorted(g) for g in groups.values()), [["a:1", "b:1"], ["c:1"], ["d:1"], ["e:1"], ["f:1"]],
                         "a description naming only the venue is ambiguous between two events at that venue, so it is never guessed; other days and places stay separate")
        self.assertEqual(tolerant.extractions, run_in_memory(ExactMatch(), self.candidates()).extractions, "the same extraction work was done for the same candidates")

    def test_processing_and_the_in_memory_persistence_import_no_database(self):
        code = ("import sys, json\n"
                "from area_events.validation.processing import ProcessingService\n"
                "from area_events.extraction.service import ExtractionService, LocalProcessingClient, StoredCandidateExtractor\n"
                "from area_events.deduplication.exact import ExactMatch\n"
                "from tests.inmemory import InMemoryProcessing\n"
                "from tests.records import document\n"
                "storage = InMemoryProcessing(ExactMatch(), [{'key': 'a:1', 'source_key': 'a', 'payload': document()}])\n"
                "clock = lambda: 1.0\n"
                "worker = ExtractionService(LocalProcessingClient(ProcessingService(storage, clock)), StoredCandidateExtractor(), clock)\n"
                "worker.run_once()\n"
                "print(json.dumps({'events': len(storage.events), 'sqlite3': 'sqlite3' in sys.modules, 'persistence': [m for m in sys.modules if m.startswith('area_events.persistence.') and not m.endswith('.ports')]}))\n")
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        self.assertEqual(json.loads(done.stdout), {"events": 1, "sqlite3": False, "persistence": []})


class ThroughRealPersistence(Fixture):
    """The same pipeline with SQLite behind it: the strategy is a composition-root argument, nothing else differs."""

    def test_the_composition_root_selects_the_strategy_and_the_pipeline_is_otherwise_identical(self):
        outcomes = {}
        for name in ("exact", "tolerant"):
            self.folder = tempfile.TemporaryDirectory()
            self.addCleanup(self.folder.cleanup)
            self.db = os.path.join(self.folder.name, "discovery.db")
            self.pages = {}
            built = self.build(deduplication=name)
            self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
            self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert", address=None, venue="Harbor Hall"))
            self.drain(built)
            outcomes[name] = (self.count("events"), self.count("event_candidates", "role='duplicate'"), len(self.all_events(built)))
            built.close()
        self.assertEqual(outcomes["exact"], (2, 0, 2), "a description with no address is never matched by the conservative default")
        self.assertEqual(outcomes["tolerant"], (1, 1, 1), "the alternative links it to the event that states the same venue; Persistence, processing and the API are unchanged")

    def test_v1_decide_only_plugin_still_processes_events(self):
        class DecideOnlyV1:
            name, version = "legacy-v1", 1

            def decide(self, incoming, lookup):
                return Decision("create", reason="a valid v1-only strategy")

        strategy = DecideOnlyV1()
        self.assertFalse(hasattr(strategy, "matches"))
        self.assertFalse(hasattr(strategy, "anchors"))
        built = self.build(deduplication=strategy)
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:legacy-v1", document("Legacy Plugin Event"))
        self.drain(built)
        self.assertEqual(self.count("events"), 1)
        built.close()

    def test_a_strategy_object_can_be_registered_instead_of_a_name(self):
        built = self.build(deduplication=TolerantMatch(window_s=3600))
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert"))
        self.drain(built)
        self.assertEqual((self.count("events"), self.count("event_candidates", "role='duplicate'")), (1, 1))

    def test_switching_strategies_reuses_the_populated_database_and_stable_associations(self):
        exact = self.build(deduplication="exact")
        primary_key = "sandiego_gov:strategy-switch-primary"
        self.add_candidate(exact, "sandiego_gov", primary_key, document("Harbor Concert"))
        self.drain(exact)
        original = exact.events.query(EventQuery(limit=20)).events[0]
        stable_key = original.event_key
        exact.close()

        # Recreate the version-7 schema shape and historical fingerprint so this
        # populated database exercises the actual version-8 removal migration.
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("ALTER TABLE events ADD COLUMN match_key TEXT")
            connection.execute("UPDATE events SET match_key='legacy-fingerprint'")
            connection.execute("DROP INDEX events_start_key")
            connection.execute("CREATE INDEX events_match ON events(match_key) WHERE match_key IS NOT NULL")
            connection.execute("DELETE FROM schema_migrations WHERE version=8")
            connection.execute("PRAGMA user_version=7")
            connection.commit()
        finally:
            connection.close()

        tolerant = self.build(deduplication="tolerant")
        secondary_key = "austin_fixture:strategy-switch-secondary"
        self.add_candidate(tolerant, "austin_fixture", secondary_key, document("HARBOR CONCERT!"))
        self.drain(tolerant)
        self.assertEqual((self.count("events"), self.count("event_candidates", "role='duplicate'")), (1, 1))
        event = tolerant.events.get(stable_key).event
        self.assertEqual(event.event_key, stable_key)
        with database(tolerant).read() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
            association = connection.execute("""SELECT e.event_key FROM event_candidates ec JOIN candidates c USING(candidate_id)
                                                   JOIN events e USING(event_id) WHERE c.candidate_key=?""", (secondary_key,)).fetchone()[0]
        self.assertNotIn("match_key", columns)
        self.assertEqual(association, stable_key)

    def test_real_sqlite_pagination_finds_twins_after_page_two_under_both_strategies(self):
        exact = self.build(deduplication="exact")
        starts_at = epoch("2030-05-01T18:00:00-07:00")
        ends_at = epoch("2030-05-01T20:00:00-07:00")
        rows = [(f"ev-page-{number:03}", f"Unrelated event {number}") for number in range(260)]
        rows.append(("ev-page-target", "Harbor Concert"))  # stable key sorts after all 260 preceding lookup rows
        with database(exact).write() as tx:
            tx.executemany("""INSERT INTO events (event_key, title, starts_at, ends_at, venue_name, address, location_status, status,
                                                      missing_json, revision, first_extracted_at, updated_at)
                               VALUES (?, ?, ?, ?, 'Harbor Hall', '500 Harbor Drive, San Diego, CA', 'address', 'complete', '[]', 1, 1, 1)""",
                           [(key, title, starts_at, ends_at) for key, title in rows])
            self.assertEqual(tx.execute("SELECT COUNT(*) FROM events WHERE starts_at BETWEEN ? AND ?", (starts_at, starts_at)).fetchone()[0], 261)
            plan = tx.execute("EXPLAIN QUERY PLAN SELECT event_key FROM events WHERE starts_at BETWEEN ? AND ? ORDER BY starts_at, event_key LIMIT ?",
                              (starts_at, starts_at, 100)).fetchall()
            self.assertTrue(any("events_start_key" in row["detail"] for row in plan), [row["detail"] for row in plan])

        exact_candidate = "sandiego_gov:sqlite-page-exact"
        self.add_candidate(exact, "sandiego_gov", exact_candidate,
                           document("Harbor Concert", starts_at="2030-05-01T18:00:00-07:00", ends_at="2030-05-01T20:00:00-07:00"))
        self.drain(exact)
        self.assertEqual((self.count("events"), self.count("event_candidates", "role='duplicate'")), (261, 1))
        exact.close()

        tolerant = self.build(deduplication="tolerant")
        tolerant_candidate = "austin_fixture:sqlite-page-tolerant"
        self.add_candidate(tolerant, "austin_fixture", tolerant_candidate,
                           document("HARBOR CONCERT!", starts_at="2030-05-01T18:00:00-07:00", ends_at="2030-05-01T20:00:00-07:00"))
        self.drain(tolerant)
        self.assertEqual((self.count("events"), self.count("event_candidates", "role='duplicate'")), (261, 2))
        with database(tolerant).read() as connection:
            associations = {row["candidate_key"]: row["event_key"] for row in connection.execute("""SELECT c.candidate_key, e.event_key FROM event_candidates ec
                                                                                                  JOIN candidates c USING(candidate_id) JOIN events e USING(event_id)
                                                                                                  WHERE c.candidate_key IN (?, ?)""", (exact_candidate, tolerant_candidate))}
        self.assertEqual(associations, {exact_candidate: "ev-page-target", tolerant_candidate: "ev-page-target"})

    def test_existing_primary_and_secondary_associations_reprocessing_and_revisions(self):
        built = self.build()
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert"))
        self.drain(built)
        self.assertEqual((self.count("events"), self.count("event_candidates", "role='primary'"), self.count("event_candidates", "role='duplicate'")), (1, 1, 1))
        self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert", venue="A different wording of the venue", description="Changed by the secondary source"))
        self.drain(built)
        event = self.all_events(built)[0]
        self.assertEqual((event.revision, event.description), (1, "An evening concert."), "a secondary description never rewrites the event")
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert", description="Changed by the primary source"))
        self.drain(built)
        event = self.all_events(built)[0]
        self.assertEqual((event.revision, event.description, self.count("events"), self.count("event_candidates", "role='duplicate'")), (2, "Changed by the primary source", 1, 1))
        detail = built.events.get(event.event_key)
        self.assertEqual([h["revision"] for h in detail.history], [1, 2])
        self.assertEqual(sorted(link["role"] for link in detail.candidate_links), ["duplicate", "primary"])

    def test_different_sources_describing_one_event_and_the_same_title_elsewhere(self):
        built = self.build()
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert", address="9 Other Road, Austin, TX"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:three", document("Harbor Concert", starts_at="2030-06-01T18:00:00-07:00"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:four", document("harbor concert", address="500 Harbor Drive San Diego CA"))
        self.drain(built)
        self.assertEqual((self.count("events"), self.count("event_candidates", "role='duplicate'")), (3, 1))

    # --- atomicity --------------------------------------------------------------------------------------------------

    def claim_results(self, built, count):
        items = built.processing.claim("w1", NAME, VERSION, limit=count, lease_s=300).items
        return [(item, StoredCandidateExtractor().extract(dataclasses.asdict(item))) for item in items]

    def test_concurrent_twins_are_decided_one_after_the_other_never_both_as_new(self):
        intervals, lock = [], threading.Lock()

        class Slow(ExactMatch):
            name = "exact-slowed"

            def decide(self, incoming, lookup):
                started = time.monotonic()
                decision = super().decide(incoming, lookup)   # the lookup has happened ...
                time.sleep(0.3)                               # ... and the decision is still pending while the other submission tries to go
                with lock:
                    intervals.append((started, time.monotonic()))
                return decision

        built = self.build(deduplication=Slow())
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert"))
        work = self.claim_results(built, 2)
        self.assertEqual(len(work), 2)
        acks, errors, start = [], [], threading.Barrier(2)

        def submit(item, result):
            try:
                start.wait(5)
                acks.append(built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, result))
            except Exception as error:   # pragma: no cover - reported below
                errors.append(repr(error))

        threads = [threading.Thread(target=submit, args=pair) for pair in work]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(a.outcome for a in acks), ["created", "duplicate"])
        self.assertEqual((self.count("events"), self.count("event_candidates", "role='primary'"), self.count("event_candidates", "role='duplicate'")), (1, 1, 1))
        first, second = sorted(intervals)
        self.assertGreaterEqual(second[0], first[1] - 0.01, "the second decision began only after the first had committed")

    def test_a_strategy_that_fails_leaves_nothing_behind_and_the_work_can_be_retried(self):
        class Flaky(ExactMatch):
            name, broken = "flaky", True

            def decide(self, incoming, lookup):
                if self.broken:
                    raise RuntimeError("the strategy failed")
                return super().decide(incoming, lookup)

        strategy = Flaky()
        built = self.build(deduplication=strategy)
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        (item, result), = self.claim_results(built, 1)
        with self.assertRaises(RuntimeError):
            built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, result)
        self.assertEqual((self.count("extractions"), self.count("events"), self.count("event_candidates")), (0, 0, 0))
        self.assertEqual(self.count("processing_work", "state='leased'"), 1, "still leased, so the same worker can retry")
        strategy.broken = False
        ack = built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, result)
        self.assertEqual((ack.outcome, self.count("events"), self.count("event_candidates")), ("created", 1, 1))

    def test_a_decision_the_candidates_state_does_not_allow_is_refused_and_rolled_back(self):
        class Wrong(ExactMatch):
            name, answer = "wrong", None

            def decide(self, incoming, lookup):
                return self.answer

        strategy = Wrong()
        built = self.build(deduplication=strategy)
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        (item, result), = self.claim_results(built, 1)
        for answer in (Decision("link", "ev-nope"), Decision("update", "ev-nope"), Decision("keep", "ev-nope"), Decision("merge"), "create", None):
            strategy.answer = answer
            with self.assertRaises(DecisionRejected, msg=str(answer)):
                built.processing.submit(item.candidate_key, NAME, VERSION, item.lease_token, result)
            self.assertEqual((self.count("extractions"), self.count("events"), self.count("event_candidates")), (0, 0, 0), "nothing of the refused attempt remains")

    def test_a_stale_lease_and_a_replayed_submission_change_nothing_more(self):
        built = self.build()
        self.add_candidate(built, "sandiego_gov", "sandiego_gov:one", document("Harbor Concert"))
        self.add_candidate(built, "austin_fixture", "austin_fixture:two", document("Harbor Concert"))
        (first, first_result), (second, second_result) = self.claim_results(built, 2)
        ack = built.processing.submit(first.candidate_key, NAME, VERSION, first.lease_token, first_result)
        again = built.processing.submit(first.candidate_key, NAME, VERSION, first.lease_token, first_result)
        self.assertEqual((ack.outcome, again.replayed), ("created", True))
        self.assertEqual((self.count("events"), self.count("extractions")), (1, 1))
        built.processing.submit(second.candidate_key, NAME, VERSION, second.lease_token, second_result)
        replay = built.processing.submit(second.candidate_key, NAME, VERSION, second.lease_token, second_result)
        self.assertEqual((replay.replayed, self.count("events"), self.count("event_candidates", "role='duplicate'")), (True, 1, 1), "a replay never links a second time")
        with self.assertRaises(LeaseLost):
            built.processing.submit(second.candidate_key, NAME, VERSION, "not-the-token", dict(second_result, issues=["a different result"]))   # (an identical result is a replay, acknowledged again)
        with database(built).read() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM event_candidates").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
