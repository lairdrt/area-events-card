"""The weighted fuzzy matcher against a labelled dataset: every pair is a hand-labelled same-event / different-event judgement, scored under both strategies.

A FALSE MATCH (two different events merged) is the serious error, so the dataset must produce none under either strategy. A MISSED MATCH (a real duplicate left apart) is
allowed only where the strategy is documented to be conservative; the exact set is asserted so that a change in behaviour is a visible decision.
"""
import dataclasses
import unittest

from area_events.contracts.models import CleanEvent
from area_events.deduplication import ExistingEvent, Incoming, same_event_facts
from area_events.deduplication.exact import ExactMatch
from area_events.deduplication.scoring import MatchProfile, compare
from area_events.deduplication.tolerant import TolerantMatch

START = 1893477600   # 2030-01-01T06:00:00Z = 22:00 on 31 December in Los Angeles
HOUR = 3600
LA = "America/Los_Angeles"
MTRP = "https://mtrp.org/marketplace/"


def event(title="Harbor Concert", starts_at=START, ends_at=None, venue="Harbor Hall", address="500 Harbor Drive, San Diego, CA", location_text=None,
          description=None, source_url=None, timezone=LA):
    return CleanEvent(title, starts_at, ends_at, timezone, description, venue, location_text, address, source_url, "address", (), "partial")


def known(key, e):
    return ExistingEvent(key, e.title, e.starts_at, e.ends_at, e.venue_name, e.address, e.location_text, 1, e.source_url, e.description, e.timezone)


MT_A = event("Holiday Marketplace at Mission Trails Regional Park", 1794675600, 1794697200, "Mission Trails Regional Park Visitor Center", "San Diego, CA 92119",
             description="Shop local this holiday season at Mission Trails Regional Park!", source_url=MTRP, timezone=None)
MT_B = event("Holiday Marketplace Benefiting Mission Trails Regional Park", 1794675600, 1794697200, "Mission Trails Regional Park Visitor Center", "San Diego, CA 92119",
             description="Holiday Marketplace Benefiting Mission Trails Regional Park", source_url=MTRP, timezone=None)
MT_NEXT_DAY = dataclasses.replace(MT_B, starts_at=1794762000, ends_at=1794783600)
BASE = event()

# (label, left, right, same_event, exact_should_match, tolerant_should_match)
CASES = [
    # --- the same event ---------------------------------------------------------------------------------------------------------------------------------------
    ("mission trails duplicates, day one", MT_A, MT_B, True, True, True),
    ("mission trails duplicates, day two", dataclasses.replace(MT_A, starts_at=1794762000, ends_at=1794783600), MT_NEXT_DAY, True, True, True),
    ("identical", BASE, event(), True, True, True),
    ("case and punctuation", BASE, event(title="HARBOR concert!", address="500 harbor drive, san diego, ca"), True, True, True),
    ("one-letter title typo", BASE, event(title="Harbor Concrt"), True, True, True),
    ("british spelling", BASE, event(title="Harbour Concert"), True, True, True),
    ("street abbreviation", BASE, event(address="500 Harbor Dr., San Diego, CA"), True, True, True),
    ("postcode added", BASE, event(address="500 Harbor Drive, San Diego, CA 92101"), True, True, True),
    ("saint abbreviation", event(venue="St. Mary's Hall"), event(venue="Saint Mary's Hall"), True, True, True),
    ("description and timezone absent on one side", BASE, event(description="An evening of music by the water.", timezone=None), True, True, True),
    ("end time absent on one side", event(ends_at=START + 7200), event(), True, True, True),
    ("different pages cited by two sources", event(source_url="https://a.example/e/1"), event(source_url="https://b.example/shows/harbor"), True, False, True),
    ("address missing, same venue", BASE, event(address=None), True, False, True),
    ("start five minutes apart", BASE, event(starts_at=START + 300), True, False, True),
    ("end times ten minutes apart", event(ends_at=START + 7200), event(ends_at=START + 7800), True, False, True),
    ("title reworded at the same time and place", event(title="Harbor Concert Series"), event(title="Harbor Concert Series Night"), True, False, True),
    ("no place on either side, identical title and start", event(venue=None, address=None), event(venue=None, address=None), True, True, True),
    ("no place, both ends stated and equal", event(ends_at=START + 7200, venue=None, address=None), event(ends_at=START + 7200, venue=None, address=None), True, True, True),
    ("no place, a scraped 'Date:' label on one title", event(title="Christmas on the Farm Arts & Crafts Fair", venue=None, address=None),
     event(title="Christmas on the Farm Arts & Crafts Fair Date: Sat.", venue=None, address=None), True, True, True),
    ("no place, a scraped 'Date & Time' label and agreeing descriptions", event(title="Christmas in Valley Center", description="Lights, music and crafts.", venue=None, address=None),
     event(title="Christmas in Valley Center Date & Time", description="Lights, music, and crafts", venue=None, address=None), True, True, True),
    # --- different events -------------------------------------------------------------------------------------------------------------------------------------
    ("another day", BASE, event(starts_at=START + 86400), False, False, False),
    ("a week later (recurring)", BASE, event(starts_at=START + 7 * 86400), False, False, False),
    ("matinee and evening performance", BASE, event(starts_at=START + 5 * HOUR), False, False, False),
    ("thirty minutes apart", BASE, event(starts_at=START + 1800), False, False, False),
    ("ends hours apart", event(ends_at=START + 7200), event(ends_at=START + 14400), False, False, False),
    ("next street number", BASE, event(address="501 Harbor Drive, San Diego, CA"), False, False, False),
    ("another street", BASE, event(address="900 Other Street, San Diego, CA"), False, False, False),
    ("another room of the same hall", event(venue="Hall A"), event(venue="Hall B"), False, False, False),
    ("another venue at the same address", BASE, event(venue="Pier Pavilion"), False, False, False),
    ("a different genre", event(title="Jazz Night"), event(title="Rock Night"), False, False, False),
    ("a children's edition", BASE, event(title="Harbor Concert for Kids"), False, False, False),
    ("numbered nights", event(title="Harbor Concert 1"), event(title="Harbor Concert 2"), False, False, False),
    ("roman numeral edition", BASE, event(title="Harbor Concert II"), False, False, False),
    ("a year added", BASE, event(title="Harbor Concert 2030"), False, False, False),
    ("two shows, one stage and time", event(title="Yoga on the Green"), event(title="Used Book Sale"), False, False, False),
    ("no place on either side, titles reworded", event(title="Harbor Concert Series", venue=None, address=None), event(title="Harbor Concert Series Night", venue=None, address=None),
     False, False, False),
    ("no place, same start, different events", event(title="Yoga on the Green", venue=None, address=None), event(title="Used Book Sale", venue=None, address=None), False, False, False),
    ("no place, numbered nights", event(title="Harbor Concert 1", venue=None, address=None), event(title="Harbor Concert 2", venue=None, address=None), False, False, False),
    ("no place, a week later", event(venue=None, address=None), event(starts_at=START + 7 * 86400, venue=None, address=None), False, False, False),
    ("no place, five minutes apart", event(venue=None, address=None), event(starts_at=START + 300, venue=None, address=None), False, False, False),
    ("no place, ends differ", event(ends_at=START + 7200, venue=None, address=None), event(ends_at=START + 10800, venue=None, address=None), False, False, False),
    ("no place, same title and start but other descriptions", event(description="Live music on the harbor stage all evening.", venue=None, address=None),
     event(description="Annual clam chowder tasting with local chefs and judges.", venue=None, address=None), False, False, False),
    ("no place, an end stated on one side only", event(ends_at=START + 7200, venue=None, address=None), event(venue=None, address=None), False, False, False),
    ("no place, men's and women's", event(title="Men's Swimming and Diving at the Harbor Pool", venue=None, address=None),
     event(title="Women's Swimming and Diving at the Harbor Pool", venue=None, address=None), False, False, False),
    ("same title, one with no place", BASE, event(venue=None, address=None), False, False, False),
    ("same venue name, other city", BASE, event(address="500 Harbor Drive, Seattle, WA"), False, False, False),
    ("similar title, other venue and street", event(title="Holiday Craft Market", venue="Civic Center", address="1 Civic Plaza"),
     event(title="Holiday Craft Marker", venue="Library", address="20 Book Lane"), False, False, False),
]


class Dataset(unittest.TestCase):
    def test_scores_both_strategies_against_every_labelled_pair(self):
        results = {}
        for name, strategy in (("exact", ExactMatch()), ("tolerant", TolerantMatch())):
            false_matches, missed = [], []
            for label, left, right, same, exact_ok, tolerant_ok in CASES:
                expected = exact_ok if name == "exact" else tolerant_ok
                for a, b in ((left, right), (right, left)):   # a comparison must not depend on which side is which
                    got = same_event_facts(a, b, strategy.profile)
                    self.assertEqual(got, strategy.matches(a, b))
                    if got and not same:
                        false_matches.append(label)
                    elif not got and same and expected is False:
                        pass
                    if got != expected:
                        self.fail(f"{name}: {label!r} ({'left/right' if a is left else 'right/left'}) matched={got}, expected {expected}: {compare(a, b, strategy.profile)}")
                if same and not expected:
                    missed.append(label)
            results[name] = (false_matches, missed)
        self.assertEqual(results["exact"][0], [], "the exact strategy merged different events")
        self.assertEqual(results["tolerant"][0], [], "the tolerant strategy merged different events")
        same_events = sum(1 for case in CASES if case[3])
        self.assertEqual((len(CASES), same_events, len(results["exact"][1]), len(results["tolerant"][1])), (len(CASES), same_events, 5, 0),
                         "the exact strategy is the conservative one; the tolerant strategy misses none of the labelled duplicates")
        print(f"\ndedup dataset: {len(CASES)} pairs ({same_events} same, {len(CASES) - same_events} different); false matches exact/tolerant "
              f"{len(results['exact'][0])}/{len(results['tolerant'][0])}; missed exact/tolerant {len(results['exact'][1])}/{len(results['tolerant'][1])}")

    def test_the_confidence_of_the_known_mission_trails_duplicates_clears_both_thresholds(self):
        for strategy in (ExactMatch(), TolerantMatch()):
            score = compare(MT_B, MT_A, strategy.profile)
            self.assertTrue(score.matched, score)
            self.assertGreater(score.confidence, strategy.profile.threshold)

    def test_everything_is_configurable(self):
        stricter = ExactMatch(threshold=0.9999, min_title=0.99)
        self.assertFalse(stricter.matches(BASE, event(title="Harbor Concrt")))
        looser = ExactMatch(min_title=0.5, threshold=0.5, min_coverage=0.1)
        self.assertFalse(looser.matches(BASE, event(title="Jazz Night")), "a loose threshold still cannot override the place and number vetoes' companions: titles below the floor")
        self.assertTrue(ExactMatch(start_tolerance_s=600).matches(BASE, event(starts_at=START + 300)))
        self.assertFalse(ExactMatch(start_tolerance_s=600).matches(BASE, event(starts_at=START + 900)))
        self.assertTrue(TolerantMatch(venue_alone_agrees=False).profile.url_conflict_vetoes is False)
        self.assertFalse(TolerantMatch(venue_alone_agrees=False).matches(BASE, event(address=None)), "venue alone is not place evidence when so configured")
        self.assertIsInstance(ExactMatch(profile=MatchProfile(name="custom")).profile, MatchProfile)


PCL = "https://calendar.utexas.edu/event/"
SWIM_START = 1793376000
ZOOM_START = 1793122200
WIDGET_START = 1793730600
WIDGET = "I, Widget: Why It's Nearly Impossible to Trace Physical Flows Through the U.S. Economy, And What That Means for Industrial Energy Modeling"


def utx(title, starts_at, venue=None, address=None, page=""):
    return event(title, starts_at, None, venue, address, None, None, PCL + page, None)


class ProductionExamples(unittest.TestCase):
    """Pairs from the production event database (evaluation of 0e875bf)."""

    def strategies(self):
        return (ExactMatch(), TolerantMatch())

    def test_mens_and_womens_swimming_are_never_the_same_event(self):
        men = utx("University of Texas Men's Swimming and Diving at South Carolina", SWIM_START, "Columbia, South Carolina", page="mens")
        women = utx("University of Texas Women's Swimming and Diving at South Carolina", SWIM_START, "Columbia, South Carolina", page="womens")
        for strategy in self.strategies():
            for a, b in ((men, women), (women, men)):
                score = compare(a, b, strategy.profile)
                self.assertFalse(score.matched, score)
        self.assertEqual(compare(men, women, TolerantMatch().profile).reason, "the titles name opposing groups")

    def test_opposing_qualifiers_are_whole_words_and_never_veto_a_title_naming_both(self):
        def tolerant(a, b):
            return compare(utx(a, SWIM_START, "Pool"), utx(b, SWIM_START, "Pool"), TolerantMatch().profile).reason
        for left, right in (("Boys Summer Soccer Camp at Harbor Park", "Girls Summer Soccer Camp at Harbor Park"), ("Men's Open Mic Night at the Harbor Hall", "Women's Open Mic Night at the Harbor Hall"), ("Ladies Winter Night Social at the Library", "Men Winter Night Social at the Library")):
            self.assertEqual(tolerant(left, right), "the titles name opposing groups", (left, right))
        for left, right in (("Men's and Women's Swimming", "Men's Swimming"), ("Men's and Women's Swimming", "Women's Swimming"),
                            ("Men's & Women's Swimming", "Men's & Women's Swimming"), ("Mental Health Workshop", "Womental Health Workshop")):
            self.assertNotEqual(tolerant(left, right), "the titles name opposing groups", (left, right))
        self.assertTrue(compare(utx("Boys and Girls Club Open House", SWIM_START, "Pool"), utx("Boys and Girls Club Open House", SWIM_START, "Pool"),
                                TolerantMatch().profile).matched)

    def test_room_numbers_in_a_venue_do_not_reject_a_pair(self):
        zoom = "Zoning the AI and Cloud Data Center Boom: How Local Governments Are Regulating Data Centers"
        room, stream = "EER 3.646 or online via Zoom or YouTube", "Live Stream Online (Zoom and YouTube)"
        for left, right in ((room, stream), (room, "Engineering Education and Research Center (EER)")):
            a, b = utx(zoom, ZOOM_START, left, page="a"), utx(zoom, ZOOM_START, right, page="b")
            for strategy in self.strategies():
                self.assertNotIn("number or letter", compare(a, b, strategy.profile).reason, (left, right))
        widget_a = utx("UT Energy Symposium: " + WIDGET, WIDGET_START, "Engineering Education and Research Center (EER)", "2501 Speedway, Austin, TX 78712", "ut-energy")
        widget_b = utx(WIDGET, WIDGET_START, room, page="i-widget")
        for strategy in self.strategies():
            for a, b in ((widget_a, widget_b), (widget_b, widget_a)):
                self.assertNotIn("number or letter", compare(a, b, strategy.profile).reason)

    def test_the_marker_rule_still_separates_rooms_editions_and_numbers(self):
        from area_events.deduplication.scoring import _marker_differs
        self.assertTrue(_marker_differs("hall a", "hall b", digits=False))
        self.assertTrue(_marker_differs("room 3", "room 4", digits=True))
        self.assertTrue(_marker_differs("concert", "concert ii", digits=False))
        self.assertFalse(_marker_differs("eer 3 646", "eer", digits=False))
        self.assertFalse(_marker_differs("room 3", "room 4", digits=False), "digits are judged by _numbers_differ for places")
        for strategy in self.strategies():
            for left, right in ((event(venue="EER 3"), event(venue="EER 4")), (event(venue="Hall A"), event(venue="Hall B")),
                                (event(title="Harbor Concert 1"), event(title="Harbor Concert 2")), (event(), event(title="Harbor Concert II"))):
                self.assertFalse(compare(left, right, strategy.profile).matched)


class Lookup:
    """Counts the pages asked for, like a database would be."""

    def __init__(self, *events):
        self.events, self.calls = sorted(events, key=lambda e: (e.starts_at, e.event_key)), []

    def starting_between(self, earliest, latest, after=None, limit=100):
        self.calls.append((earliest, latest, after, limit))
        pool = [e for e in self.events if earliest <= e.starts_at <= latest and (after is None or (e.starts_at, e.event_key) > after)]
        return tuple(pool[:limit])


def local(day, clock):
    """Epoch seconds of a Pacific wall-clock time (the Bates listing states no zone for the stored events, so none is used on the events themselves)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return int(datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=ZoneInfo(LA)).timestamp())


def bates(title, day, clock, end=None):
    """A Bates Nut Farm listing candidate as production holds it: no venue, address or location text, one listing page."""
    return event(title, local(day, clock), local(day, end) if end else None, None, None, None, None, "https://batesnutfarm.biz/pages/events", None)


# (clean title, polluted variant production holds, day, start, end)
BATES = [
    ("The 2026 Pumpkin Patch", "The 2026 Pumpkin Patch Date: Thurs., Sept. 17 to Sat.", "2026-10-31", "09:00:00", "17:00:00"),
    ("Halloween Finale", "Halloween Finale Date", "2026-10-31", "09:00:00", "17:00:00"),
    ("Annual Costume Contest & Parade", "Annual Costume Contest & Parade Date", "2026-10-31", "10:00:00", None),
    ("Howl-o-ween Doggie Costume Contest", "Howl-o-ween Doggie Costume Contest Date", "2026-10-31", "13:00:00", None),
    ("Alias | Live at Bates on Halloween", "Alias | Live at Bates on Halloween Date", "2026-10-31", "12:00:00", None),
    ("Christmas on the Farm Arts & Crafts Fair", "Christmas on the Farm Arts & Crafts Fair Date: Sat.", "2026-11-07", "09:00:00", "16:00:00"),
    ("Christmas on the Farm Arts & Crafts Fair", "Christmas on the Farm Arts & Crafts Fair Date: Sat.", "2026-11-08", "09:00:00", "16:00:00"),
    ("East County Cruisers Car Show and Toy Drive", "East County Cruisers Car Show and Toy Drive Date", "2026-11-07", "09:00:00", None),
    ("8th Annual Guns & Hoses Chili Cook-Off Fundraiser", "8th Annual Guns & Hoses Chili Cook-Off Fundraiser Date", "2026-11-08", "12:00:00", "15:00:00"),
    ("Santa's Coming! Craft Fair", "Santa's Coming! Craft Fair Date", "2026-11-27", "09:00:00", None),
    ("Santa's Coming! Craft Fair", "Santa's Coming! Craft Fair Date", "2026-11-28", "09:00:00", None),
    ("Christmas in Valley Center", "Christmas in Valley Center Date & Time", "2026-12-11", "16:00:00", None),
    ("Christmas in Valley Center", "Christmas in Valley Center Date & Time", "2026-12-12", "16:00:00", None),
]


class BatesListing(unittest.TestCase):
    """The duplicate canonical events visible in the Home Assistant card on 28 October: a clean title and a "Date: ..." variant of every occurrence, no place stated."""

    def strategies(self):
        return (ExactMatch(), TolerantMatch())

    def test_each_polluted_variant_is_the_same_event_as_its_clean_twin(self):
        for strategy in self.strategies():
            for clean, polluted, day, start, end in BATES:
                a, b = bates(clean, day, start, end), bates(polluted, day, start, end)
                for left, right in ((a, b), (b, a)):
                    score = compare(left, right, strategy.profile)
                    self.assertTrue(score.matched, (strategy.name, polluted, score))

    def test_other_occurrences_and_other_events_never_match(self):
        events = [bates(clean, day, start, end) for clean, _, day, start, end in BATES] + [bates(polluted, day, start, end) for _, polluted, day, start, end in BATES]
        for strategy in self.strategies():
            for i, a in enumerate(events):
                for b in events[i + 1:]:
                    twin = (i % len(BATES)) == (events.index(b) % len(BATES))
                    self.assertEqual(compare(a, b, strategy.profile).matched, twin, (strategy.name, a.title, a.starts_at, b.title, b.starts_at))

    def test_unrelated_events_sharing_a_start_are_not_merged(self):
        cruisers = bates(BATES[7][0], BATES[7][2], BATES[7][3])
        fair = bates(BATES[5][0], BATES[5][2], BATES[5][3], BATES[5][4])
        self.assertEqual(cruisers.starts_at, fair.starts_at)
        pumpkin, finale = bates(BATES[0][0], BATES[0][2], BATES[0][3], BATES[0][4]), bates(BATES[1][0], BATES[1][2], BATES[1][3], BATES[1][4])
        self.assertEqual(pumpkin.starts_at, finale.starts_at)
        for strategy in self.strategies():
            self.assertFalse(compare(cruisers, fair, strategy.profile).matched)
            self.assertFalse(compare(pumpkin, finale, strategy.profile).matched)

    def test_a_polluted_candidate_links_to_its_own_clean_event_among_all_of_them(self):
        known_events = [known(f"ev-{n:02d}", bates(clean, day, start, end)) for n, (clean, _, day, start, end) in enumerate(BATES)]
        for strategy in self.strategies():
            for n, (_, polluted, day, start, end) in enumerate(BATES):
                decision = strategy.decide(Incoming("mission_trails:new", bates(polluted, day, start, end), None), Lookup(*known_events))
                self.assertEqual((decision.action, decision.event_key), ("link", f"ev-{n:02d}"), (strategy.name, polluted))

    def test_two_events_with_a_place_missing_on_one_side_are_still_not_merged(self):
        placed = event("Christmas on the Farm Arts & Crafts Fair", local("2026-11-07", "09:00:00"), local("2026-11-07", "16:00:00"), "Bates Nut Farm", "15954 Woods Valley Rd",
                       None, None, "https://batesnutfarm.biz/pages/events", None)
        for strategy in self.strategies():
            self.assertFalse(compare(placed, bates("Christmas on the Farm Arts & Crafts Fair", "2026-11-07", "09:00:00", "16:00:00"), strategy.profile).matched)


class TitleLabels(unittest.TestCase):
    """A scraped field label is removed only with explicit evidence; a legitimate title containing the word "Date" is never altered."""

    def titles(self, left, right):
        from area_events.deduplication.scoring import _titles
        return _titles(left, right)

    def test_scraped_labels_are_removed(self):
        for left, right in (("Christmas on the Farm Arts & Crafts Fair Date: Sat.", "Christmas on the Farm Arts & Crafts Fair"),
                            ("Christmas in Valley Center Date & Time:", "Christmas in Valley Center"),
                            ("The 2026 Pumpkin Patch Date: Thurs., Sept. 17 to Sat.", "The 2026 Pumpkin Patch"),
                            ("Halloween Finale Date", "Halloween Finale"), ("Christmas in Valley Center Date & Time", "Christmas in Valley Center")):
            a, b = self.titles(left, right)
            self.assertEqual(a, b, (left, right))

    def test_legitimate_titles_are_not_altered(self):
        from area_events.deduplication.scoring import normalize
        for title in ("The Date Night Dinner", "Dinner Date at the Museum", "Save the Date Festival", "Save the Date: Festival of Lights", "Save the Date", "Blind Date"):
            for other in ("Harbor Concert", "Dinner", "Festival of Lights"):
                a, b = self.titles(title, other)
                self.assertEqual((a, b), (normalize(title), normalize(other)), (title, other))

    def test_a_bare_trailing_label_needs_the_other_title_to_be_the_same_title_without_it(self):
        self.assertEqual(self.titles("Save the Date", "Save the Date Festival"), ("save the date", "save the date festival"))
        self.assertEqual(self.titles("Dinner Date", "Dinner Party"), ("dinner date", "dinner party"))

    def test_legitimate_titles_do_not_match_a_different_event_on_the_strength_of_a_stripped_word(self):
        for strategy in (ExactMatch(), TolerantMatch()):
            for left, right in (("Dinner Date at the Museum", "Dinner at the Museum"), ("Save the Date Festival", "Save the Festival")):
                a, b = event(left, venue=None, address=None), event(right, venue=None, address=None)
                self.assertFalse(compare(a, b, strategy.profile).matched, (left, right))


class Decisions(unittest.TestCase):
    def decide(self, strategy, incoming, *existing):
        lookup = Lookup(*existing)
        return strategy.decide(Incoming("src:new", incoming, None), lookup), lookup

    def test_the_best_unambiguous_match_wins_not_the_first(self):
        near = known("ev-a", event(title="Harbor Concrt"))      # sorts first, a weaker title match
        best = known("ev-b", event(title="Harbor Concert"))
        for strategy in (ExactMatch(), TolerantMatch()):
            decision, _ = self.decide(strategy, event(), near, best)
            self.assertEqual((decision.action, decision.event_key), ("link", "ev-b"), strategy.name)

    def test_an_ambiguous_match_is_never_linked(self):
        twin_a, twin_b = known("ev-a", event()), known("ev-b", event())
        for strategy in (ExactMatch(), TolerantMatch()):
            decision, _ = self.decide(strategy, event(), twin_a, twin_b)
            self.assertEqual(decision.action, "create", strategy.name)
            self.assertIn("ambiguous", decision.reason)

    def test_a_clear_winner_over_a_much_weaker_candidate_is_linked(self):
        weak = known("ev-a", event(title="Harbor Concert Series", description="Different text entirely about boats and tides."))
        strong = known("ev-b", event(description="An evening of music by the water."))
        decision, _ = self.decide(TolerantMatch(), event(description="An evening of music by the water."), weak, strong)
        self.assertEqual((decision.action, decision.event_key), ("link", "ev-b"))

    def test_several_performances_at_one_venue_resolve_to_the_right_one(self):
        shows = [known(f"ev-{n}", event(starts_at=START + n * 3 * HOUR)) for n in range(6)]
        for strategy in (ExactMatch(), TolerantMatch()):
            decision, _ = self.decide(strategy, event(starts_at=START + 3 * 3 * HOUR), *shows)
            self.assertEqual((decision.action, decision.event_key), ("link", "ev-3"), strategy.name)

    def test_a_dense_lookup_finds_a_twin_among_hundreds_in_bounded_pages(self):
        crowd = [known(f"ev-{n:04}", event(title=f"Stage {n} Showcase", venue=f"Room {n}", address=f"{n + 1} Long Street, San Diego, CA")) for n in range(450)]
        twin = known("ev-zzzz", event())
        decision, lookup = self.decide(ExactMatch(), event(), *crowd, twin)
        self.assertEqual((decision.action, decision.event_key), ("link", "ev-zzzz"))
        self.assertLessEqual(len(lookup.calls), 6, "pages of 100: five pages hold 451 events")
        self.assertTrue(all(call[3] == 100 for call in lookup.calls))

    def test_an_overfull_lookup_stops_at_its_bound_and_never_guesses(self):
        crowd = [known(f"ev-{n:05}", event(title=f"Showcase {n}", venue=f"Room {n}", address=f"{n} Long Street")) for n in range(2500)]
        twin = known("ev-zzzzz", event())
        strategy = ExactMatch(max_compared=1000)
        decision, lookup = self.decide(strategy, event(), *crowd, twin)
        self.assertEqual(decision.action, "create")
        self.assertIn("1000", decision.reason)
        self.assertLessEqual(len(lookup.calls), 10, "bounded: at most max_compared / page size queries")

    def test_the_tolerant_lookup_window_is_bounded_by_its_tolerance(self):
        _, lookup = self.decide(TolerantMatch(), event())
        self.assertEqual((lookup.calls[0][0], lookup.calls[0][1]), (START - 600, START + 600))
        _, lookup = self.decide(ExactMatch(), event())
        self.assertEqual((lookup.calls[0][0], lookup.calls[0][1]), (START, START))


if __name__ == "__main__":
    unittest.main()
