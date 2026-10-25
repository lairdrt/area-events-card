import os
import tempfile
import unittest

from area_events.geolocation.location import distance_miles
from area_events.discovery.merge import merge_events
from area_events.geolocation.place import CONFLICT_MILES, CityResolver, combine_places, locate_event, locate_events, parse_address
from tests.test_merge_evidence import PUBLISHERS, rec

LOCATION = {"display_name": "Austin, TX", "latitude": 30.2672, "longitude": -97.7431, "radius_miles": 25.0, "country": "US", "region": "TX"}


class Provider:
    name = "nominatim"

    def __init__(self, places):
        self.places, self.queries = places, []

    def lookup(self, fetcher, spec):
        self.queries.append((spec["query"], spec.get("country")))
        return self.places.get(spec["query"])


def resolver(places=None, max_lookups=10, cache=None):
    provider = Provider(places or {})
    return CityResolver(object(), cache or os.path.join(tempfile.mkdtemp(), "c.json"), {"nominatim": provider}, max_lookups), provider


def city(name, lat, lon, country="US", region="TX"):
    return {"display_name": name, "latitude": lat, "longitude": lon, "country": country, "region": region}


class AddressParsing(unittest.TestCase):
    def test_city_region_and_postal_code_only_when_the_text_states_them(self):
        cases = {
            "2228 Bacon St, San Diego": ("San Diego", None, None),
            "4801 La Crosse Avenue, Austin, TX 78739": ("Austin", "TX", "78739"),
            "2301 San Jacinto Blvd, Austin TX 78712": ("Austin", "TX", "78712"),
            "Third Avenue, Chula Vista": ("Chula Vista", None, None),
            "1 Main St": (None, None, None),
            "": (None, None, None),
            None: (None, None, None),
            "Room 5, Building 7": (None, None, None),
            "10 Downing Street, London SW1A 2AA": (None, None, None),  # digits in the tail: not a defensible city name
        }
        for text, (city_name, region, postal) in cases.items():
            parsed = parse_address(text)
            with self.subTest(text=text):
                self.assertEqual((parsed["city"], parsed["region"], parsed["postal"]), (city_name, region, postal))


class Classification(unittest.TestCase):
    def test_confirmed_inside_and_outside_the_radius(self):
        inside = locate_event({"title": "a", "geo": {"latitude": 30.28, "longitude": -97.73, "source": "feed"}}, LOCATION)
        self.assertEqual((inside["classification"], inside["confidence"], inside["precision"], inside["within_radius"]), ("confirmed", "high", "venue", True))
        outside = locate_event({"title": "b", "geo": {"latitude": 32.7157, "longitude": -117.1611}}, LOCATION)
        self.assertEqual((outside["classification"], outside["within_radius"]), ("confirmed", False))
        kept, summary = locate_events([{"title": "a", "geo": {"latitude": 30.28, "longitude": -97.73}},
                                       {"title": "b", "geo": {"latitude": 32.7157, "longitude": -117.1611}}], LOCATION)
        self.assertEqual(([e["title"] for e in kept], summary["outside_area"], summary["confirmed"]), (["a"], 1, 1))

    def test_radius_boundary_is_inclusive(self):
        miles = distance_miles(30.2672, -97.7431, 31.2672, -97.7431)
        event = {"geo": {"latitude": 31.2672, "longitude": -97.7431}}
        self.assertTrue(locate_event(event, {**LOCATION, "radius_miles": miles})["within_radius"])
        self.assertFalse(locate_event(event, {**LOCATION, "radius_miles": miles - 0.01})["within_radius"])

    def test_estimated_city_level_location_with_evidence(self):
        res, provider = resolver({"Austin, TX": city("Austin", 30.2711, -97.7437)})
        place = locate_event({"venue": "Hall", "address": "4801 La Crosse Avenue, Austin, TX 78739"}, LOCATION, res)
        self.assertEqual((place["classification"], place["precision"], place["confidence"], place["city"], place["region"]),
                         ("estimated", "city", "medium", "Austin", "TX"))
        self.assertEqual((place["latitude"], place["longitude"]), (30.2711, -97.7437))  # the city centre, not the search center
        self.assertNotEqual((place["latitude"], place["longitude"]), (LOCATION["latitude"], LOCATION["longitude"]))
        self.assertTrue(any("taken from the event's address" in line for line in place["evidence"]))
        self.assertEqual(place["address"], "4801 La Crosse Avenue, Austin, TX 78739")  # the original address is preserved

    def test_estimate_without_a_stated_region_is_low_confidence_and_says_which_context_was_used(self):
        res, provider = resolver({"Austin, TX": city("Austin", 30.27, -97.74)})
        place = locate_event({"address": "1 Congress Ave, Austin"}, LOCATION, res)
        self.assertEqual((place["classification"], place["confidence"]), ("estimated", "low"))
        self.assertEqual(provider.queries, [("Austin, TX", "US")])
        self.assertTrue(any("search area's region used as context" in line for line in place["evidence"]))

    def test_city_that_cannot_be_geocoded_is_still_an_estimate_without_coordinates(self):
        res, _ = resolver({})
        place = locate_event({"address": "1 Main St, Smallville"}, LOCATION, res)
        self.assertEqual((place["classification"], place["latitude"], place["within_radius"]), ("estimated", None, None))
        self.assertTrue(any("could not be geocoded" in line for line in place["evidence"]))
        without_resolver = locate_event({"address": "1 Main St, Smallville"}, LOCATION, None)
        self.assertEqual((without_resolver["classification"], without_resolver["latitude"]), ("estimated", None))

    def test_estimated_outside_the_radius_is_retained_and_labeled(self):
        res, _ = resolver({"Waco, TX": city("Waco", 31.5493, -97.1467)})
        kept, summary = locate_events([{"title": "far", "address": "1 Elm St, Waco, TX"}], LOCATION, res)
        self.assertEqual((len(kept), summary["estimated"], summary["outside_area"]), (1, 1, 0))
        place = kept[0]["place"]
        self.assertFalse(place["within_radius"])
        self.assertTrue(any("kept because the estimate is uncertain" in line for line in place["evidence"]))

    def test_city_resolving_to_another_country_is_not_used(self):
        res, _ = resolver({"Paris, TX": city("Paris", 48.85, 2.35, country="FR", region=None)})
        place = locate_event({"address": "1 Rue X, Paris, TX"}, LOCATION, res)
        self.assertIsNone(place["latitude"])

    def test_unknown_when_nothing_is_defensible(self):
        place = locate_event({"title": "t", "venue": "Mystery Hall", "neighborhood": "Old Town"}, LOCATION, resolver()[0])
        self.assertEqual((place["classification"], place["confidence"], place["latitude"], place["city"]), ("unknown", "none", None, None))
        self.assertEqual((place["venue"], place["neighborhood"]), ("Mystery Hall", "Old Town"))  # what is known is preserved
        kept, summary = locate_events([{"title": "t"}], LOCATION)
        self.assertEqual((len(kept), summary["unknown"]), (1, 1))
        for bad in ({"latitude": 0, "longitude": 0}, {"latitude": "x", "longitude": 1}, {"latitude": 99, "longitude": 1}, {}):
            self.assertEqual(locate_event({"geo": bad}, LOCATION)["classification"], "unknown", bad)

    def test_city_lookups_are_cached_memoized_and_bounded(self):
        cache = os.path.join(tempfile.mkdtemp(), "c.json")
        res, provider = resolver({"Austin, TX": city("Austin", 30.27, -97.74)}, cache=cache)
        for _ in range(3):
            locate_event({"address": "1 A St, Austin, TX"}, LOCATION, res)
        self.assertEqual(len(provider.queries), 1)
        again, second = resolver({}, cache=cache)  # a later run reads the disk cache
        locate_event({"address": "9 B St, Austin, TX"}, LOCATION, again)
        self.assertEqual(second.queries, [])
        names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
        limited, third = resolver({f"{n}, TX": city(n, 30 + i / 100, -97) for i, n in enumerate(names)}, max_lookups=2)
        places = [locate_event({"address": f"1 St, {n}, TX"}, LOCATION, limited) for n in names]
        self.assertEqual(len(third.queries), 2)
        self.assertEqual([p["latitude"] is not None for p in places], [True, True, False, False, False])  # the rest wait for a later run


class MergingLocations(unittest.TestCase):
    def place(self, classification, lat=None, lon=None, city_name=None, confidence="high"):
        return {"classification": classification, "confidence": confidence, "precision": "venue" if lat else None, "latitude": lat, "longitude": lon,
                "venue": None, "address": None, "neighborhood": None, "city": city_name, "region": None, "country": None,
                "distance_miles": None, "within_radius": None, "evidence": [f"{classification} evidence"]}

    def test_best_classification_wins_and_evidence_is_kept_per_source(self):
        merged = combine_places([("A", self.place("estimated", 30.27, -97.74, "Austin", "medium")),
                                 ("B", self.place("confirmed", 30.28, -97.73, "Austin")), ("C", self.place("unknown"))])
        self.assertEqual((merged["classification"], merged["latitude"], merged.get("conflict")), ("confirmed", 30.28, None))
        self.assertEqual(sorted(l.split("]")[0] for l in merged["evidence"]), ["[A", "[B", "[C"])

    def test_missing_descriptive_fields_are_filled_from_other_sources(self):
        a = self.place("unknown")
        b = self.place("estimated", city_name="Austin", confidence="low")
        b["neighborhood"] = "Zilker"
        merged = combine_places([("A", a), ("B", b)])
        self.assertEqual((merged["classification"], merged["neighborhood"], merged["city"]), ("estimated", "Zilker", "Austin"))

    def test_conflicting_positions_or_cities_are_flagged_and_lower_confidence(self):
        far = combine_places([("A", self.place("confirmed", 30.28, -97.73, "Austin")), ("B", self.place("estimated", 32.72, -117.16, "San Diego", "medium"))])
        self.assertTrue(far["conflict"])
        self.assertEqual(far["confidence"], "low")
        self.assertTrue(any("CONFLICT" in line for line in far["evidence"]))
        cities = combine_places([("A", self.place("estimated", city_name="Austin", confidence="low")), ("B", self.place("estimated", city_name="Dallas", confidence="low"))])
        self.assertTrue(cities["conflict"])
        near = combine_places([("A", self.place("confirmed", 30.28, -97.73, "Austin")), ("B", self.place("estimated", 30.40, -97.80, "Austin", "medium"))])
        self.assertNotIn("conflict", near)
        self.assertLess(distance_miles(30.28, -97.73, 30.40, -97.80), CONFLICT_MILES)

    def test_location_survives_deduplication_and_is_separate_from_event_verification(self):
        a = rec("kpbs", "KPBS", "Chelsea Wolfe", "2026-10-21T19:30:00-07:00", venue="The Magnolia", address="1 A St, Austin, TX 78701")
        b = rec("dosd", "DoSD", "Chelsea Wolfe", "2026-10-21T19:30:00-07:00", venue="The Magnolia", address="1 A St, Austin, TX 78701")
        a["place"] = locate_event(a, LOCATION, resolver({"Austin, TX": city("Austin", 30.27, -97.74)})[0])
        b["place"] = locate_event({**b, "geo": {"latitude": 30.2672, "longitude": -97.7431, "source": "DoSD"}}, LOCATION)
        merged = merge_events({"kpbs": [a], "dosd": [b]}, PUBLISHERS)[0]
        self.assertTrue(merged["verified"])  # two independent publishers agree on the event ...
        self.assertEqual(merged["place"]["classification"], "confirmed")  # ... and location confidence is judged separately
        c = rec("kpbs", "KPBS", "Mystery Show", "2026-10-22T20:00:00-07:00", venue="Hidden Room")
        d = rec("dosd", "DoSD", "Mystery Show", "2026-10-22T20:00:00-07:00", venue="Hidden Room")
        for record in (c, d):
            record["place"] = locate_event(record, LOCATION)
        verified = merge_events({"kpbs": [c], "dosd": [d]}, PUBLISHERS)[0]
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["place"]["classification"], "unknown")  # a verified event may still have an unknown location

    def test_records_without_places_still_merge(self):
        a = rec("kpbs", "KPBS", "Old Cache", "2026-10-22T20:00:00-07:00", venue="Hall")
        merged = merge_events({"kpbs": [a]}, PUBLISHERS)[0]
        self.assertNotIn("place", merged)


if __name__ == "__main__":
    unittest.main()
