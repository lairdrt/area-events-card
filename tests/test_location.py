import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from discovery import refresh as refresh_module
from discovery import sources
from discovery.core import FetchError, finish_event, make_event
from discovery.location import (
    LocationError, NominatimProvider, ZippopotamProvider, covers, parse_spec, resolve_location, select_sources,
)
from discovery.refresh import load_config

PDT = timezone(timedelta(hours=-7))
SAN_DIEGO_KEYS = ["kpbs", "sandiegoreader", "dosd", "sandiego_gov"]


class FakeProvider:
    def __init__(self, place=None, error=None, name="fake"):
        self.place, self.error, self.name, self.calls = place, error, name, 0

    def lookup(self, fetcher, spec):
        self.calls += 1
        if self.error:
            raise LocationError(self.error)
        return self.place


AUSTIN = {"display_name": "Austin, Texas, United States", "latitude": 30.2672, "longitude": -97.7431, "country": "US", "region": "TX"}


class Resolution(unittest.TestCase):
    def setUp(self):
        self.cache = os.path.join(tempfile.mkdtemp(), "location-cache.json")

    def test_default_and_san_diego_query_need_no_network(self):
        for raw in (None, {"query": "San Diego, CA", "radius_miles": 25}, {"query": "san  diego, california"}):
            place = resolve_location(raw, fetcher=None, cache_path=self.cache, providers={})
            self.assertEqual((place["latitude"], place["longitude"]), (32.7157, -117.1611))
            self.assertEqual((place["timezone"], place["country"], place["region"], place["source"]), ("America/Los_Angeles", "US", "CA", "builtin"))
            self.assertIn("San Diego", place["display_name"])
        self.assertEqual(resolve_location(None)["radius_miles"], 25)
        self.assertFalse(os.path.exists(self.cache))

    def test_coordinates(self):
        place = resolve_location({"latitude": 40.7128, "longitude": -74.006, "radius_miles": 10, "name": "NYC", "country": "US", "region": "NY"}, providers={})
        self.assertEqual((place["display_name"], place["radius_miles"], place["timezone"], place["source"]), ("NYC", 10.0, "America/New_York", "config"))
        self.assertEqual(resolve_location({"latitude": 1, "longitude": 2})["display_name"], "1.0000, 2.0000")

    def test_invalid_configurations_are_clear_errors(self):
        bad = [{"latitude": 95, "longitude": 0}, {"latitude": 1}, {"latitude": "x", "longitude": 1}, {"query": " "}, {"query": "a", "zip": "1"},
               {}, {"query": "Austin", "radius_miles": 0}, {"query": "Austin", "radius_miles": 9999}, {"zip": "", "country": "US"},
               {"zip": "78701", "country": "USA"}, "Austin", {"query": "Austin", "radius_miles": True}]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(LocationError):
                parse_spec(raw)

    def test_city_query_uses_the_provider_once_then_the_cache(self):
        provider = FakeProvider(AUSTIN, name="nominatim")
        for _ in range(3):
            place = resolve_location({"query": "Austin, TX", "radius_miles": 15}, None, self.cache, {"nominatim": provider})
        self.assertEqual(provider.calls, 1)
        self.assertEqual((place["timezone"], place["radius_miles"], place["source"]), ("America/Chicago", 15.0, "cache"))
        self.assertTrue(json.load(open(self.cache, encoding="utf-8")))

    def test_zip_uses_the_postal_provider(self):
        zippo, nominatim = FakeProvider(AUSTIN, name="zippopotam"), FakeProvider(error="should not be used")
        place = resolve_location({"zip": "78701"}, None, self.cache, {"zippopotam": zippo, "nominatim": nominatim})
        self.assertEqual((zippo.calls, nominatim.calls, place["source"]), (1, 0, "geocoded:zippopotam"))

    def test_unresolvable_locations_raise_and_are_not_cached(self):
        with self.assertRaisesRegex(LocationError, "could not resolve location 'Nowhereville'.*found no match"):
            resolve_location({"query": "Nowhereville"}, None, self.cache, {"nominatim": FakeProvider(None, name="nominatim")})
        with self.assertRaisesRegex(LocationError, "could not resolve location 'Austin'.*offline"):
            resolve_location({"query": "Austin"}, None, self.cache, {"nominatim": FakeProvider(error="offline")})
        self.assertFalse(os.path.exists(self.cache))
        with self.assertRaisesRegex(LocationError, "unknown geocoding provider"):
            resolve_location({"query": "Austin", "provider": "nope"}, None, self.cache, {"nominatim": FakeProvider()})

    def test_stale_cache_entry_is_used_when_the_lookup_fails(self):
        provider = FakeProvider(AUSTIN, name="nominatim")
        resolve_location({"query": "Austin"}, None, self.cache, {"nominatim": provider}, now=lambda: 1000.0)
        later = 1000.0 + 400 * 24 * 3600
        place = resolve_location({"query": "Austin"}, None, self.cache, {"nominatim": FakeProvider(error="down")}, now=lambda: later)
        self.assertEqual(place["source"], "cache (stale)")
        refreshed = FakeProvider({**AUSTIN, "latitude": 30.3}, name="nominatim")
        place = resolve_location({"query": "Austin"}, None, self.cache, {"nominatim": refreshed}, now=lambda: later)
        self.assertEqual((refreshed.calls, place["latitude"]), (1, 30.3))

    def test_provider_response_parsing(self):
        nominatim = NominatimProvider().parse(
            [{"lat": "30.2711", "lon": "-97.7437", "display_name": "Austin, Travis County, Texas, United States",
              "address": {"country_code": "us", "ISO3166-2-lvl4": "US-TX"}}], {"query": "Austin"})
        self.assertEqual((nominatim["latitude"], nominatim["country"], nominatim["region"]), (30.2711, "US", "TX"))
        self.assertIsNone(NominatimProvider().parse([], {"query": "x"}))
        zippo = ZippopotamProvider().parse(
            {"post code": "78701", "country abbreviation": "US",
             "places": [{"place name": "Austin", "longitude": "-97.7431", "latitude": "30.2672", "state": "Texas", "state abbreviation": "TX"}]},
            {"zip": "78701", "country": "US"})
        self.assertEqual((zippo["display_name"], zippo["region"], zippo["longitude"]), ("Austin, TX 78701", "TX", -97.7431))
        self.assertIsNone(ZippopotamProvider().parse({}, {"zip": "00000", "country": "US"}))

    def test_provider_lookup_wraps_fetch_errors(self):
        class Fetcher:
            def get(self, url, check_robots=True):
                raise FetchError("boom")
        with self.assertRaises(LocationError):
            NominatimProvider().lookup(Fetcher(), {"query": "x"})


class Coverage(unittest.TestCase):
    def selected(self, raw):
        location = resolve_location(raw, providers={})
        config = load_config()
        chosen, skipped = select_sources(config["sources"], location, sources.load_spec)
        return [s["key"] for s, _ in chosen], [s["key"] for s, _ in skipped]

    def test_default_configuration_keeps_all_four_san_diego_sources(self):
        self.assertEqual(self.selected(load_config()["location"]), (SAN_DIEGO_KEYS, []))
        self.assertEqual(self.selected(None), (SAN_DIEGO_KEYS, []))

    def test_other_regions_select_nothing(self):
        for raw in ({"latitude": 34.05, "longitude": -118.24}, {"latitude": 30.27, "longitude": -97.74}, {"latitude": 40.7, "longitude": -74.0}):
            self.assertEqual(self.selected(raw), ([], SAN_DIEGO_KEYS))

    def test_nearby_area_selects_overlapping_sources_only(self):
        # Escondido is ~28 mi from downtown San Diego: inside the county sources' reach (45 + 5 mi), beyond the city source's (20 + 5 mi)
        chosen, skipped = self.selected({"latitude": 33.12, "longitude": -117.09, "radius_miles": 5})
        self.assertEqual(chosen, ["kpbs", "sandiegoreader", "dosd"])
        self.assertEqual(skipped, ["sandiego_gov"])

    def test_coverage_kinds(self):
        austin = resolve_location({"latitude": 30.27, "longitude": -97.74}, providers={})
        self.assertTrue(covers({"kind": "searchable"}, austin)[0])
        self.assertTrue(covers({"kind": "configured"}, austin)[0])
        self.assertTrue(covers(None, austin)[0])  # undeclared = user-configured
        self.assertFalse(covers({"kind": "regional", "latitude": 32.7, "longitude": -117.1, "radius_miles": 45}, austin)[0])
        self.assertFalse(covers({"kind": "mystery"}, austin)[0])

    def test_source_spec_can_override_coverage(self):
        austin = resolve_location({"latitude": 30.27, "longitude": -97.74}, providers={})
        spec = {"key": "kpbs", "coverage": {"kind": "regional", "region": "Austin", "latitude": 30.27, "longitude": -97.74, "radius_miles": 30}}
        self.assertEqual(len(select_sources([spec], austin, sources.load_spec)[0]), 1)
        self.assertEqual(select_sources([{"key": "kpbs", "enabled": False}], austin, sources.load_spec), ([], []))


class Adapter:
    NAME, PUBLISHER = "Fake", "fake.example"

    def __init__(self, key, title, coverage=None):
        self.KEY, self.title = key, title
        if coverage:
            self.COVERAGE = coverage
        self.seen_location = None

    def discover(self, fetcher, previous, location=None, **_):
        self.seen_location = location
        start = datetime.now(PDT) + timedelta(days=1)
        event = make_event(self.KEY, self.KEY, "https://x.example", f"https://{self.KEY}.example/{self.title}", self.title, start,
                           local_id=self.title, venue="Venue")
        return [finish_event(event, "2030-01-01T00:00:00-08:00", ["e"], False)], {}


SD = {"kind": "regional", "region": "San Diego", "latitude": 32.7157, "longitude": -117.1611, "radius_miles": 45}


class RefreshIntegration(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.paths = dict(cache_path=os.path.join(self.folder, "cache.json"), output=os.path.join(self.folder, "events.json"),
                          lock_path=os.path.join(self.folder, "lock"))
        self.adapters = {"sd": Adapter("sd", "San Diego Concert", SD),
                         "search": Adapter("search", "Searchable Event", {"kind": "searchable"})}
        self.config = {"sources": [{"key": "sd"}, {"key": "search"}]}

    def run_refresh(self, location=None, providers=None):
        config = {**self.config, **({"location": location} if location is not None else {})}
        with mock.patch.object(refresh_module.sources, "load", side_effect=lambda k: self.adapters[k]):
            return refresh_module.refresh(config, {}, fetcher=object(), location_providers=providers, **self.paths)

    def feed(self):
        with open(self.paths["output"], encoding="utf-8") as handle:
            return json.load(handle)

    def test_san_diego_default_runs_regional_and_searchable_sources(self):
        self.assertEqual(self.run_refresh(), 0)
        feed = self.feed()
        self.assertEqual(sorted(e["title"] for e in feed["events"]), ["San Diego Concert", "Searchable Event"])
        self.assertEqual(feed["location"]["display_name"].split(",")[0], "San Diego")
        self.assertEqual(self.adapters["search"].seen_location["latitude"], 32.7157)
        self.assertIsNone(self.adapters["sd"].seen_location)  # regional adapters do not receive a location

    def test_other_city_never_gets_san_diego_events(self):
        self.assertEqual(self.run_refresh(), 0)
        self.assertEqual(self.run_refresh({"latitude": 30.27, "longitude": -97.74, "name": "Austin"}), 0)
        feed = self.feed()
        self.assertEqual([e["title"] for e in feed["events"]], ["Searchable Event"])  # the cached San Diego event did not leak
        self.assertEqual(feed["location"]["display_name"], "Austin")
        self.assertNotIn("sd", feed["sources"])
        self.assertEqual(self.adapters["search"].seen_location["display_name"], "Austin")

    def test_back_to_san_diego_rebuilds_from_fresh_data(self):
        self.run_refresh()
        self.run_refresh({"latitude": 30.27, "longitude": -97.74})
        self.assertEqual(self.run_refresh(), 0)
        self.assertEqual(sorted(e["title"] for e in self.feed()["events"]), ["San Diego Concert", "Searchable Event"])

    def test_uncovered_location_is_an_error_and_keeps_the_last_feed(self):
        del self.adapters["search"], self.config["sources"][1]
        self.assertEqual(self.run_refresh(), 0)
        before = open(self.paths["output"], encoding="utf-8").read()
        self.assertEqual(self.run_refresh({"latitude": 40.7, "longitude": -74.0}), 1)
        self.assertEqual(open(self.paths["output"], encoding="utf-8").read(), before)

    def test_unresolvable_location_is_an_error_and_keeps_the_last_feed(self):
        self.assertEqual(self.run_refresh(), 0)
        before = open(self.paths["output"], encoding="utf-8").read()
        providers = {"nominatim": FakeProvider(None, name="nominatim")}
        self.assertEqual(self.run_refresh({"query": "Nowhereville"}, providers), 1)
        self.assertEqual(open(self.paths["output"], encoding="utf-8").read(), before)
        self.assertEqual(self.run_refresh({"latitude": 100, "longitude": 0}), 1)
        self.assertEqual(open(self.paths["output"], encoding="utf-8").read(), before)
        for adapter in self.adapters.values():
            adapter.seen_location = "unchanged"
        self.run_refresh({"query": "Nowhereville"}, providers)
        self.assertTrue(all(a.seen_location == "unchanged" for a in self.adapters.values()))  # no adapter ran

    def test_resolved_city_is_cached_between_refreshes(self):
        provider = FakeProvider(AUSTIN, name="nominatim")
        self.config["sources"] = [{"key": "search"}]
        for _ in range(3):
            self.assertEqual(self.run_refresh({"query": "Austin, TX"}, {"nominatim": provider}), 0)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(self.feed()["location"]["timezone"], "America/Chicago")


if __name__ == "__main__":
    unittest.main()
