"""Which illustration an event gets: evidence-weighted classification in the engine, never guessed, always traceable."""
import copy
import json
import os
import re
import tempfile
import time
import unittest
from unittest import mock

from discovery import classify, images
from discovery import refresh as refresh_module
from discovery import sources as sources_module
from discovery.core import FetchError, finish_event, make_event
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pick(title, description=None, tags=(), venue=None):
    return classify.classify({"title": title, "description": description, "categories": list(tags), "venue": venue})


class Subjects(unittest.TestCase):
    """Synthetic listings that state the examples in the brief."""

    def test_the_subject_of_the_title_decides(self):
        cases = {
            "Astronomy lecture": "science",
            "Robotics workshop": "science",
            "Gallery exhibition": "arts",
            "Shakespeare in the park": "theater",
            "Live jazz concert": "music",
            "Stand up comedy night": "theater",
            "Neighborhood volunteer cleanup": "community",
            "Guided hike through the canyon": "outdoors",
            "Family storytime for toddlers": "family",
            "Fall street festival and parade": "street_festival",
            "Wine tasting and cheese pairing": "food",
            "Saturday morning 5k fun run": "sports",
        }
        for title, expected in cases.items():
            self.assertEqual(pick(title)["category"], expected, title)

    def test_jazz_at_a_restaurant_is_music_not_food(self):
        result = pick("Jazz Night at Giuseppe's Restaurant", "Dinner and drinks available.", venue="Giuseppe's Italian Kitchen")
        self.assertEqual(result["category"], "music")
        self.assertFalse(result["fallback"])

    def test_teams_playing_each_other_is_sports(self):
        for title in ("Oceanside Shock vs Minnesota Northern Lights", "San Diego Gulls vs. Coachella Valley Firebirds", "Tigers v Bears"):
            self.assertEqual(pick(title)["category"], "sports", title)

    def test_inflected_and_spelled_variants_match(self):
        self.assertEqual(pick("Hiking the canyon trails")["category"], "outdoors")
        self.assertEqual(pick("Paintings and sculptures on view")["category"], "arts")
        self.assertEqual(pick("Astronomical society telescopes night")["category"], "science")
        self.assertEqual(pick("Café and théâtre soirée")["category"], "theater")  # accents do not matter


class Evidence(unittest.TestCase):
    def test_a_generic_source_category_never_overrides_the_event_itself(self):
        self.assertEqual(pick("Watercolor painting class", tags=["Campus & Community"])["category"], "arts")
        self.assertEqual(pick("Jazz quartet", "Free admission.", tags=["Campus & Community", "Community"])["category"], "music")
        # a bare "Community" or "Sports & Recreation" tag is not enough on its own
        self.assertEqual(pick("Open house", tags=["Community"])["category"], "general")
        self.assertNotEqual(pick("Fortlandia", "Climb upon, crawl through and explore new play structures.", tags=["Sports & Recreation"])["category"], "sports")

    def test_a_specific_source_tag_is_reliable_structured_evidence(self):
        result = pick("Hovvdy, Emma Ogeir", tags=["Music"])
        self.assertEqual(result["category"], "music")
        self.assertEqual(result["strength"], "weak")
        self.assertIn('source tag "Music"', result["reasons"][0])  # quoted as the source wrote it

    def test_the_title_outweighs_a_noisy_description(self):
        result = pick("Pottery class for beginners", "Bring a friend. Concert tickets, live music, a band and a choir are mentioned in the shop next door.")
        self.assertEqual(result["category"], "arts")

    def test_the_venue_is_only_weak_supporting_evidence(self):
        self.assertEqual(pick("Smith and Jones", venue="Downtown Jazz Club and Blues Hall")["category"], "general")
        supported = pick("Smith and Jones Quartet", venue="Downtown Jazz Club")
        self.assertEqual(supported["category"], "music")
        self.assertTrue(any(r.startswith("venue") for r in pick("Jazz Smith and Jones", venue="Downtown Jazz Club and Blues Hall")["reasons"]) or supported["reasons"])

    def test_phrases_that_only_look_like_a_category_are_ignored(self):
        self.assertNotEqual(pick("Political science seminar")["category"], "science")
        self.assertNotEqual(pick("Fair housing workshop")["category"], "festival")
        self.assertNotEqual(pick("Lunch & Learn: women who built the city")["category"], "food")

    def test_mentioning_a_word_in_the_description_is_not_enough_alone(self):
        self.assertEqual(pick("Annual gathering", "There will be a band.")["category"], "general")


class Corrections(unittest.TestCase):
    """Real listings (excerpts) that the classifier got wrong, and the rule behind each fix."""

    LISTINGS = {
        "gulls": {"title": "San Diego Gulls Home Game", "venue": "Pechanga Arena San Diego", "categories": [],
                  "description": "The San Diego Gulls will host 14 different Western Conference opponents at Pechanga Arena as part of their 2026-27 schedule."},
        "affairs": {"title": "Student Affairs Meeting", "venue": None, "categories": ["Campus & Community"], "description": "N/A"},
        "assembly": {"title": "Public Assembly", "venue": "WCP 2.302", "categories": ["Campus & Community"],
                     "description": "Join us for our public assembly where you can engage in open forum, meet your representatives, listen to what is going on in Student Government, and propose your own ideas."},
        "mannerism": {"title": "Virtual Talk - Understanding Mannerism", "venue": "Online Internet Events", "categories": [],
                      "description": "Timken Museum of Art (Virtual): Mannerism originated primarily in Italy. It lasted from approximately 1530-1600 and was a reaction to the harmonious classicism and idealized naturalism of the High Renaissance art as practiced by Leonardo da Vinci, Michelangelo, and Raphael."},
        "trail": {"title": "The Haunted Trail of Balboa Park", "venue": "On the corner of Balboa Drive and Eighth Drive.", "categories": [],
                  "description": "The Haunted Trail of Balboa Park is BACK, with two haunts in one; a mile long terrifying outdoor trail through twisted pines and gnarled oaks and the 3,500 square foot eXperiment Maze, culminating with the NEW Dark Carnival!"},
        "popsicle": {"title": "Popsicle Pop-Up", "venue": "Gordon White Building (GWB)", "categories": ["Campus & Community"],
                     "description": "Refresh yourself with an ice-cold popsicle from Latino-owned Mom & Pop's all-natural and environmentally friendly frozen paletas! Grab a pop on your way to class."},
    }

    def category(self, key):
        return classify.classify(self.LISTINGS[key])

    def test_the_listings_now_get_the_category_their_content_supports(self):
        expected = {"gulls": "sports", "affairs": "community", "assembly": "community", "mannerism": "arts", "trail": "halloween", "popsicle": "food"}
        for key, category in expected.items():
            result = self.category(key)
            self.assertEqual((result["category"], result["fallback"]), (category, False), key)
            self.assertTrue(result["reasons"], key)

    def test_home_and_away_games_are_sports_whoever_plays(self):
        """The old rule erased the phrase "home game"; the fix is the rule, not the title."""
        self.assertNotIn("home game", classify.NEGATIVE.pattern)
        for title in ("Home Game", "Away Game: Tigers at Bears", "Thunder Home Game vs Lightning", "Rockets Home Game at the Arena"):
            self.assertEqual(pick(title)["category"], "sports", title)
        self.assertEqual(pick("Board game night at the library")["category"], "general")  # "game" alone stays weak

    def test_a_negative_phrase_never_hides_a_term_the_lexicon_values(self):
        """Negative phrases exist for words that mean something else; each may only contain lexicon words as part of that other meaning."""
        phrases = classify.NEGATIVE.pattern.strip("\b()").replace("(?: a)?", "").split("|")
        for phrase in phrases:
            words = classify._tokens(re.sub(r"\(.*?\)\??", "", phrase))
            for category, terms in classify.LEXICON.items():
                for term, weight in terms.items():
                    if weight >= 3 and term == " ".join(words):
                        self.fail(f"negative phrase {phrase!r} erases the strong {category} term {term!r}")

    def test_the_conservative_fallback_still_holds_for_thin_evidence(self):
        self.assertEqual(pick("Meeting")["category"], "general")                        # a bare generic word
        self.assertEqual(pick("Pop-Up")["category"], "general")
        self.assertEqual(pick("Museum")["category"], "general")
        self.assertEqual(pick("Student Union")["category"], "general")

    def test_the_haunted_attraction_is_not_an_outdoor_trail_but_a_real_trail_still_is(self):
        self.assertEqual(pick("Haunted Trail", "A scary walk.")["category"], "halloween")
        self.assertEqual(pick("Wildflower Trail Hike", "Walk the trail.")["category"], "outdoors")
        self.assertEqual(pick("Haunted House at the old mill")["category"], "halloween")


class Uncertainty(unittest.TestCase):
    def test_no_evidence_means_the_general_illustration_and_says_so(self):
        for title in ("Kishi Bashi", "Murda Beatz", "Quarterly planning", "Come Together"):
            result = pick(title)
            self.assertEqual((result["category"], result["strength"], result["fallback"]), ("general", "none", True), title)
            self.assertEqual(result["reasons"], ["not enough evidence about what the event is"])

    def test_conflicting_evidence_is_not_guessed(self):
        result = pick("Concert and brunch")
        self.assertEqual((result["category"], result["fallback"]), ("general", True))
        self.assertRegex(result["reasons"][0], r"conflicting evidence \((music|food) [\d.]+ vs (food|music) [\d.]+\)")

    def test_a_clear_win_over_a_rival_is_accepted(self):
        result = pick("Jazz and wine night")
        self.assertEqual(result["category"], "music")

    def test_strength_labels_the_amount_of_evidence_and_is_never_a_probability(self):
        strong = pick("University of Texas Football vs Ole Miss", "Football game day.", tags=["Sports"])
        weak = pick("Someone Live", tags=["Music"])
        self.assertEqual(strong["strength"], "strong")
        self.assertEqual(weak["strength"], "weak")
        for result in (strong, weak, pick("Nobody")):
            self.assertIn(result["strength"], ("strong", "moderate", "weak", "none"))
            self.assertFalse({"probability", "confidence", "percent"} & set(result))

    def test_the_decision_is_traceable(self):
        result = pick("Astronomy lecture and telescope night", "Bring a blanket.")
        self.assertEqual(set(result), {"category", "strength", "reasons", "fallback", "scores"})
        self.assertLessEqual(len(result["reasons"]), 3)
        self.assertTrue(all(re.match(r'(title|description|source tag|venue) "', reason) for reason in result["reasons"]))
        self.assertGreater(result["scores"]["science"], result["scores"].get("music", 0))


class Annotation(unittest.TestCase):
    def test_annotate_records_only_the_decision_and_never_touches_photos(self):
        event = {"title": "Jazz concert", "description": None, "categories": [], "venue": None, "image_url": "https://x.example/a.jpg",
                 "images": ["https://x.example/a.jpg"], "image_provenance": {"tier": 1, "kind": "event", "source": "A", "skipped": []}}
        before = copy.deepcopy(event)
        classify.annotate([event])
        self.assertEqual(set(event["illustration"]), {"category", "strength", "reasons", "fallback"})
        del event["illustration"]
        self.assertEqual(event, before)

    def test_the_classifier_is_cheap(self):
        events = [{"title": f"Jazz concert number {i}", "description": "A long description about nothing in particular. " * 20, "categories": ["Music", "Community"]}
                  for i in range(600)]
        started = time.perf_counter()
        classify.annotate(events)
        self.assertLess(time.perf_counter() - started, 3.0)


class Packaging(unittest.TestCase):
    def test_the_engine_only_names_illustrations_the_card_has(self):
        with open(os.path.join(ROOT, "src", "area-events-card.js"), encoding="utf-8") as handle:
            source = handle.read()
        art = source[source.index("const ART = {"):source.index("const ART_LABELS")]
        card_keys = set(re.findall(r"^  ([a-z_]+): SVG_HEAD", art, re.M))
        catalog = set(classify.CATEGORIES)
        self.assertEqual(card_keys, catalog)
        labels = source[source.index("const ART_LABELS"):source.index("/** The illustration for an event")]
        self.assertEqual(set(re.findall(r"\b([a-z_]+): \"", labels)), catalog)
        self.assertEqual(set(classify.LEXICON), set(classify.CATEGORIES) - {"general"})

    def test_lexicon_is_well_formed(self):
        for category, terms in classify.LEXICON.items():
            for term, weight in terms.items():
                self.assertTrue(0 < weight <= 6, (category, term))
                self.assertEqual(term, term.lower())
        for category, tags in classify.TAGS.items():
            self.assertIn(category, classify.LEXICON)
            self.assertTrue(all(0 < weight <= 3 for weight in tags.values()))
        # the generic tags the sources use can never carry a category on their own
        self.assertLess(classify.TAG_INDEX["community"][classify._tag_key("Campus & Community")], classify.MIN_SCORE)
        self.assertLess(classify.TAG_INDEX["sports"][classify._tag_key("Sports & Recreation")], classify.MIN_SCORE)
        self.assertEqual(classify._tag_key("Science & Technology"), classify._tag_key("science technology"))
        self.assertEqual(classify._tag_key("Food / Drinks"), classify._tag_key("food drinks"))


class RealEvents(unittest.TestCase):
    """The held-out set of real San Diego and Austin listings: before (the card's old keyword matching) and after."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "tests", "data", "classification_eval.json"), encoding="utf-8") as handle:
            cls.rows = json.load(handle)["events"]

    def test_the_snapshot_still_holds(self):
        drift = []
        for row in self.rows:
            got = classify.classify({"title": row["title"], "description": row["description"], "categories": row["tags"], "venue": row["venue"]})["category"]
            if got != row["after"]:
                drift.append((row["title"], row["after"], got))
        self.assertEqual(drift, [], "classification changed for reviewed real events; review them and refresh tests/data/classification_eval.json")

    def test_the_set_is_diverse(self):
        self.assertGreaterEqual(len(self.rows), 30)
        self.assertEqual({row["city"] for row in self.rows}, {"San Diego", "Austin"})
        self.assertGreaterEqual(len({row["before"] for row in self.rows}), 8)
        self.assertGreaterEqual(len({row["source"] for row in self.rows}), 4)

    def test_reviewed_changes_are_mostly_improvements(self):
        reviewed = [row["review"] for row in self.rows if "review" in row]
        self.assertEqual(len(reviewed), sum(1 for row in self.rows if row["before"] != row["after"]))
        self.assertGreaterEqual(reviewed.count("better"), 5 * reviewed.count("worse"))

    def test_specific_corrections(self):
        by_title = {row["title"]: row for row in self.rows}
        expect = {
            "Austin Tech Week": ("festival", "science"),            # a tech conference was drawn as a festival
            "Postdoc Zumba Wellness Session!": ("theater", "sports"),
            "Car Show": ("sports", "car_show"),                    # not a sport
            "Mitchell Sustainability Symposium": ("arts", "general"),
        }
        for title, (was, now) in expect.items():
            row = next(r for t, r in by_title.items() if t.startswith(title))
            self.assertEqual((row["before"], row["after"]), (was, now), title)
        rooftop = [r for r in self.rows if "Rooftop Cinema Club" in r["title"]]
        self.assertTrue(rooftop and all(r["before"] == "festival" and r["after"] == "general" for r in rooftop))
        market = next(r for r in self.rows if r["title"].startswith("ASCENDKemet"))
        self.assertEqual((market["before"], market["after"]), ("arts", "farmers_market"))


class InTheFeed(unittest.TestCase):
    """The decision is made when the feed is built and rides along with the events; photos are untouched."""

    def test_refresh_adds_the_illustration_and_keeps_the_photo_choice(self):
        start = (datetime.now(timezone(timedelta(hours=-7))) + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)

        def record(key, title, image=None, tags=()):
            event = make_event(key, key.upper(), f"https://{key}.example", f"https://{key}.example/{title.replace(' ', '-')}", title, start, local_id=title,
                               venue="Venue", categories=list(tags), image_url=image, image_source="listing" if image else None)
            return finish_event(event, "2030-01-01T00:00:00-08:00", ["e"], False)

        class Adapter:
            def __init__(self, events=None, error=None):
                self.events, self.error = events, error

            def discover(self, fetcher, previous, **_):
                if self.error:
                    raise FetchError(self.error)
                return self.events, {}

        with tempfile.TemporaryDirectory() as folder:
            paths = dict(cache_path=os.path.join(folder, "cache.json"), output=os.path.join(folder, "events.json"), lock_path=os.path.join(folder, "lock"))
            config = {"retention_days": 3, "location": {"latitude": 40.0, "longitude": -100.0, "timezone": "America/Chicago", "radius_miles": 25},
                      "sources": [{"id": "a", "name": "A", "adapter": "fake_a", "url": "https://a.example", "coverage": {"type": "global"}}]}
            adapters = {"fake_a": Adapter([record("a", "Astronomy lecture", image="https://a.example/sky.jpg"), record("a", "Kishi Bashi"),
                                           record("a", "Jazz night", tags=["Music"])])}
            with mock.patch.dict(sources_module.ADAPTERS, adapters):
                self.assertEqual(refresh_module.refresh(config, {}, fetcher=object(), **paths), 0)
            with open(paths["output"], encoding="utf-8") as handle:
                events = {e["title"]: e for e in json.load(handle)["events"]}
        self.assertEqual(events["Astronomy lecture"]["illustration"]["category"], "science")
        self.assertEqual(events["Astronomy lecture"]["image_url"], "https://a.example/sky.jpg", "a real photo is never replaced by the illustration")
        self.assertEqual(events["Jazz night"]["illustration"]["category"], "music")
        self.assertEqual(events["Kishi Bashi"]["illustration"], {"category": "general", "strength": "none", "reasons": ["not enough evidence about what the event is"], "fallback": True})
        self.assertIsNone(events["Kishi Bashi"]["image_url"])
        self.assertTrue(images.judge(events["Astronomy lecture"]["image_url"])[0])


if __name__ == "__main__":
    unittest.main()
