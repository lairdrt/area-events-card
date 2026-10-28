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
    ("no place on either side", event(venue=None, address=None), event(venue=None, address=None), False, False, False),
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


class Lookup:
    """Counts the pages asked for, like a database would be."""

    def __init__(self, *events):
        self.events, self.calls = sorted(events, key=lambda e: (e.starts_at, e.event_key)), []

    def starting_between(self, earliest, latest, after=None, limit=100):
        self.calls.append((earliest, latest, after, limit))
        pool = [e for e in self.events if earliest <= e.starts_at <= latest and (after is None or (e.starts_at, e.event_key) > after)]
        return tuple(pool[:limit])


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
