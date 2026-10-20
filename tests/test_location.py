import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from discovery import refresh as refresh_module
from discovery import registry
from discovery import sources as sources_module
from discovery.core import FetchError, finish_event, make_event
from discovery.location import LocationError, NominatimProvider, ZippopotamProvider, parse_spec, resolve_location
from discovery.refresh import load_config

PDT = timezone(timedelta(hours=-7))
SHIPPED = load_config()
AUSTIN = {"display_name": "Austin, Texas, United States", "latitude": 30.2672, "longitude": -97.7431, "country": "US", "region": "TX"}


class FakeProvider:
    def __init__(self, place=None, error=None, name="fake"):
        self.place, self.error, self.name, self.calls = place, error, name, 0

    def lookup(self, fetcher, spec):
        self.calls += 1
        if self.error:
            raise LocationError(self.error)
        return self.place


class FakeFetcher:
    def __init__(self, body=None):
        self.body, self.urls = body, []

    def get(self, url, check_robots=True):
        self.urls.append(url)
        if self.body is None:
            raise FetchError("offline")
        return self.body


class Resolution(unittest.TestCase):
    def setUp(self):
        self.cache = os.path.join(tempfile.mkdtemp(), "location-cache.json")

    def test_nothing_is_built_in(self):
        with self.assertRaisesRegex(LocationError, "no location configured"):
            resolve_location(None, providers={})
        # even the well-known city this project started with must be configured, then geocoded or given coordinates
        provider = FakeProvider({"display_name": "San Diego", "latitude": 32.7, "longitude": -117.1, "country": "US", "region": "CA"}, name="nominatim")
        resolve_location({"query": "San Diego, CA"}, None, self.cache, {"nominatim": provider})
        self.assertEqual(provider.calls, 1)

    def test_coordinates_need_no_network_and_keep_the_configured_details(self):
        fetcher = FakeFetcher()
        place = resolve_location({"latitude": -33.8688, "longitude": 151.2093, "name": "Sydney CBD", "country": "au", "region": "NSW",
                                  "timezone": "Australia/Sydney", "radius_miles": 10}, fetcher, self.cache, providers={})
        self.assertEqual((place["display_name"], place["country"], place["region"], place["timezone"], place["timezone_source"], place["source"]),
                         ("Sydney CBD", "AU", "NSW", "Australia/Sydney", "config", "config"))
        self.assertEqual(fetcher.urls, [])
        self.assertFalse(os.path.exists(self.cache))

    def test_timezone_is_never_assumed(self):
        no_zone = resolve_location({"latitude": 30.27, "longitude": -97.74, "country": "US", "region": "TX"}, None, None, providers={})
        self.assertEqual((no_zone["timezone"], no_zone["timezone_source"]), (None, "unknown"))
        failed = resolve_location({"latitude": 30.27, "longitude": -97.74}, FakeFetcher(), self.cache, providers={})
        self.assertEqual(failed["timezone"], None)

    def test_timezone_lookup_is_used_and_cached_when_not_configured(self):
        fetcher = FakeFetcher(json.dumps({"timeZone": "America/Chicago"}))
        for _ in range(2):
            place = resolve_location({"latitude": 30.27, "longitude": -97.74}, fetcher, self.cache, providers={})
        self.assertEqual((place["timezone"], place["timezone_source"]), ("America/Chicago", "lookup:timeapi"))
        self.assertEqual(len(fetcher.urls), 1)
        rejected = resolve_location({"latitude": 10.0, "longitude": 10.0}, FakeFetcher(json.dumps({"timeZone": "../../etc"})), self.cache, providers={})
        self.assertIsNone(rejected["timezone"])

    def test_invalid_configurations_are_clear_errors(self):
        bad = [{"latitude": 95, "longitude": 0}, {"latitude": 1}, {"latitude": "x", "longitude": 1}, {"query": " "}, {"query": "a", "zip": "1"},
               {}, {"query": "Austin", "radius_miles": 0}, {"query": "Austin", "radius_miles": 9999}, {"zip": "78701"},
               {"zip": "78701", "country": "USA"}, "Austin", {"query": "Austin", "radius_miles": True}, {"query": "Austin", "country": "United States"},
               {"latitude": 1, "longitude": 2, "timezone": "Central"}]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(LocationError):
                parse_spec(raw)

    def test_city_zip_and_country_handling(self):
        provider = FakeProvider(AUSTIN, name="nominatim")
        for _ in range(3):
            place = resolve_location({"query": "Austin, TX", "country": "US", "radius_miles": 15}, None, self.cache, {"nominatim": provider})
        self.assertEqual((provider.calls, place["source"], place["radius_miles"]), (1, "cache", 15.0))
        zippo, nominatim = FakeProvider(AUSTIN, name="zippopotam"), FakeProvider(error="unused")
        place = resolve_location({"zip": "78701", "country": "US", "timezone": "America/Chicago"}, None, self.cache, {"zippopotam": zippo, "nominatim": nominatim})
        self.assertEqual((zippo.calls, nominatim.calls, place["source"]), (1, 0, "geocoded:zippopotam"))
        with self.assertRaisesRegex(LocationError, "resolved to country US, but GB is configured"):
            resolve_location({"query": "Austin", "country": "GB"}, None, self.cache, {"nominatim": FakeProvider(AUSTIN, name="nominatim")})

    def test_unresolvable_locations_raise_and_are_not_cached(self):
        cache = os.path.join(tempfile.mkdtemp(), "c.json")
        with self.assertRaisesRegex(LocationError, "could not resolve location 'Nowhereville'.*found no match"):
            resolve_location({"query": "Nowhereville"}, None, cache, {"nominatim": FakeProvider(None, name="nominatim")})
        with self.assertRaisesRegex(LocationError, "could not resolve location 'Austin'.*offline"):
            resolve_location({"query": "Austin"}, None, cache, {"nominatim": FakeProvider(error="offline")})
        self.assertFalse(os.path.exists(cache))
        with self.assertRaisesRegex(LocationError, "unknown geocoding provider"):
            resolve_location({"query": "Austin", "provider": "nope"}, None, cache, {"nominatim": FakeProvider()})

    def test_stale_cache_entry_is_used_when_the_lookup_fails(self):
        resolve_location({"query": "Austin"}, None, self.cache, {"nominatim": FakeProvider(AUSTIN, name="nominatim")}, now=lambda: 1000.0)
        later = 1000.0 + 400 * 24 * 3600
        place = resolve_location({"query": "Austin"}, None, self.cache, {"nominatim": FakeProvider(error="down")}, now=lambda: later)
        self.assertEqual(place["source"], "cache (stale)")

    def test_provider_response_parsing(self):
        nominatim = NominatimProvider().parse(
            [{"lat": "30.2711", "lon": "-97.7437", "display_name": "Austin, Texas",
              "address": {"country_code": "us", "ISO3166-2-lvl4": "US-TX"}}], {"query": "Austin"})
        self.assertEqual((nominatim["latitude"], nominatim["country"], nominatim["region"]), (30.2711, "US", "TX"))
        self.assertIn("countrycodes=us", NominatimProvider().url({"query": "Austin", "country": "US"}))
        self.assertNotIn("countrycodes", NominatimProvider().url({"query": "Austin", "country": None}))
        self.assertIsNone(NominatimProvider().parse([], {"query": "x"}))
        zippo = ZippopotamProvider().parse(
            {"country abbreviation": "US", "places": [{"place name": "Austin", "longitude": "-97.7431", "latitude": "30.2672",
                                                       "state": "Texas", "state abbreviation": "TX"}]}, {"zip": "78701", "country": "US"})
        self.assertEqual((zippo["display_name"], zippo["region"]), ("Austin, TX 78701", "TX"))


def entry(id, adapter, **extra):
    return {"id": id, "name": id.upper(), "adapter": adapter, "url": f"https://{id}.example/events", **extra}


AREA = {"type": "area", "name": "Somewhere", "latitude": 32.7157, "longitude": -117.1611, "radius_miles": 45}


class Adapters:
    """Stand-ins registered under fake adapter names so registry tests need no real site."""

    class One:
        def discover(self, *a, **k):
            return [], {}

    class Many:
        MULTIPLE = True

        def discover(self, *a, **k):
            return [], {}


class Registry(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(sources_module.ADAPTERS, {"one": Adapters.One(), "two": Adapters.One(), "many": Adapters.Many()})
        patcher.start()
        self.addCleanup(patcher.stop)

    def load(self, *entries):
        return registry.load_sources({"sources": list(entries)})

    def test_shipped_configuration_is_valid_and_data_driven(self):
        sources = registry.load_sources(SHIPPED)
        self.assertEqual([s.id for s in sources], ["kpbs", "sandiegoreader", "dosd", "sandiego_gov"])
        self.assertEqual([s.name for s in sources], ["KPBS", "San Diego Reader", "DoSD", "City of San Diego"])
        self.assertTrue(all(s.coverage["type"] == "area" and s.url.startswith("https://") for s in sources))
        for module in ("kpbs", "sandiegoreader", "dosd", "sandiego_gov", "localist"):
            adapter = sources_module.adapter_module(module)
            self.assertFalse(hasattr(adapter, "COVERAGE"), module)  # coverage lives in configuration, not adapters

    def test_example_configurations_load(self):
        for name in ("discovery.san-diego.json", "discovery.austin.json"):
            with open(os.path.join(os.path.dirname(__file__), "..", "examples", name), encoding="utf-8") as handle:
                config = json.load(handle)
            parse_spec(config["location"])
            registry.load_sources(config)

    def test_validation_errors(self):
        bad = {
            "duplicate id": [entry("a", "one"), entry("a", "many")],
            "bad id": [entry("Bad Id", "one")],
            "unknown adapter": [entry("a", "nope")],
            "single-use adapter twice": [entry("a", "one"), entry("b", "one")],
            "no url": [{"id": "a", "adapter": "one"}],
            "relative url": [entry("a", "one", url="/events")],
            "bad scheme": [entry("a", "one", url="ftp://x.example")],
            "enabled type": [entry("a", "one", enabled="yes")],
            "reserved option": [entry("a", "one", options={"url": "x"})],
            "bad coverage type": [entry("a", "one", coverage={"type": "everywhere"})],
            "area without radius": [entry("a", "one", coverage={"type": "area", "latitude": 1, "longitude": 2})],
            "area out of range": [entry("a", "one", coverage={"type": "area", "latitude": 99, "longitude": 2, "radius_miles": 5})],
            "legacy key format": [{"key": "kpbs", "enabled": True}],
            "empty name": [entry("a", "one", name=" ")],
        }
        for label, entries in bad.items():
            with self.subTest(label), self.assertRaises(registry.RegistryError):
                self.load(*entries)
        for config in ({}, {"sources": []}, {"sources": "x"}):
            with self.assertRaises(registry.RegistryError):
                registry.load_sources(config)

    def test_multiple_instances_of_a_reusable_adapter(self):
        sources = self.load(entry("a", "many"), entry("b", "many"))
        self.assertEqual({s.publisher for s in sources}, {"a.example", "b.example"})

    def test_selection_by_coverage(self):
        east = {"latitude": 40.7, "longitude": -74.0, "radius_miles": 25}
        san_diego = {"latitude": 32.7157, "longitude": -117.1611, "radius_miles": 25}
        sources = self.load(
            entry("regional", "one", coverage=AREA), entry("everywhere", "two", coverage={"type": "global"}),
            entry("undeclared", "many"), entry("explicit_unknown", "many", coverage={"type": "unknown"}),
            entry("off", "many", enabled=False, coverage={"type": "global"}))
        for location, expected in ((san_diego, ["regional", "everywhere"]), (east, ["everywhere"])):
            chosen, skipped = registry.select_sources(sources, location)
            self.assertEqual([s.id for s, _ in chosen], expected)
            reasons = {s.id: r for s, r in skipped}
            self.assertIn("not assumed", reasons["undeclared"])  # unknown coverage never means "everywhere"
            self.assertIn("not assumed", reasons["explicit_unknown"])
            self.assertEqual(reasons["off"], "disabled")

    def test_coverage_boundary_is_the_sum_of_both_radii(self):
        from discovery.location import distance_miles
        gap = distance_miles(32.7157, -117.1611, 33.12, -117.09)
        near = {"latitude": 33.12, "longitude": -117.09}
        area = registry._coverage({**AREA, "radius_miles": 20}, "test")
        self.assertTrue(registry.covers(area, {**near, "radius_miles": gap - 20 + 0.1})[0])
        self.assertFalse(registry.covers(area, {**near, "radius_miles": gap - 20 - 0.1})[0])

    def test_adapter_receives_registry_values(self):
        seen = {}

        class Spy:
            ACCEPTS_LOCATION = True

            def discover(self, fetcher, previous, details=True, limit=None, **options):
                seen.update(options, details=details, limit=limit)
                return [], {}

        with mock.patch.dict(sources_module.ADAPTERS, {"spy": Spy()}):
            source = self.load(entry("s", "spy", name="Spy Source", options={"days": 5}, coverage={"type": "global"}))[0]
            source.discover(None, [], details=False, limit=3, location={"latitude": 1})
        self.assertEqual((seen["source_id"], seen["source_name"], seen["url"], seen["days"], seen["location"], seen["limit"]),
                         ("s", "Spy Source", "https://s.example/events", 5, {"latitude": 1}, 3))


class FakeSource:
    def __init__(self, title, lat=None, lon=None, address=None, hours=24, **extra):
        self.title, self.lat, self.lon, self.address, self.hours, self.extra, self.seen = title, lat, lon, address, hours, extra, None

    def discover(self, fetcher, previous, source_id="x", source_name="X", url=None, location=None, **_):
        self.seen = location
        start = datetime.now(PDT) + timedelta(hours=self.hours)
        event = make_event(source_id, source_name, url, f"{url}/{self.title}", self.title, start, local_id=self.title, venue="Venue",
                           address=self.address, **self.extra)
        if self.lat is not None:
            event["geo"] = {"latitude": self.lat, "longitude": self.lon, "source": "test feed"}
        return [finish_event(event, "2030-01-01T00:00:00-08:00", ["e"], False)], {}


def area(name, lat, lon, tz, radius=25, **extra):
    return {"name": name, "latitude": lat, "longitude": lon, "timezone": tz, "radius_miles": radius, **extra}


AREAS = {
    "austin": area("Austin", 30.2672, -97.7431, "America/Chicago"),
    "sydney": area("Sydney", -33.8688, 151.2093, "Australia/Sydney", country="AU"),
    "london": area("London", 51.5072, -0.1276, "Europe/London", country="GB"),
    "nairobi": area("Nairobi", -1.2921, 36.8219, "Africa/Nairobi", country="KE"),
}


class RefreshAcrossAreas(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.paths = dict(cache_path=os.path.join(self.folder, "cache.json"), output=os.path.join(self.folder, "events.json"),
                          lock_path=os.path.join(self.folder, "lock"), location_cache_path=os.path.join(self.folder, "loc.json"))
        self.adapters = {
            "local_a": FakeSource("Austin Show", 30.27, -97.74),
            "local_s": FakeSource("Sydney Show", -33.87, 151.21),
            "local_l": FakeSource("London Show", 51.51, -0.12),
            "world": FakeSource("Worldwide Show"),
            "world2": FakeSource("Mystery Show"),
        }
        self.config_sources = [
            entry("local_a", "local_a", coverage={"type": "area", "latitude": 30.27, "longitude": -97.74, "radius_miles": 30}),
            entry("local_s", "local_s", coverage={"type": "area", "latitude": -33.87, "longitude": 151.21, "radius_miles": 30}),
            entry("local_l", "local_l", coverage={"type": "area", "latitude": 51.5, "longitude": -0.12, "radius_miles": 30}),
            entry("world", "world", coverage={"type": "global"}),
            entry("mystery", "world2"),
        ]

    def run_refresh(self, location, sources=None):
        config = {"location": location, "sources": sources or self.config_sources}
        with mock.patch.dict(sources_module.ADAPTERS, self.adapters):
            return refresh_module.refresh(config, {}, fetcher=object(), **self.paths)

    def feed(self):
        with open(self.paths["output"], encoding="utf-8") as handle:
            return json.load(handle)

    def titles(self):
        return sorted(e["title"] for e in self.feed()["events"])

    def test_unrelated_areas_each_get_only_their_own_events(self):
        expected = {"austin": ["Austin Show", "Worldwide Show"], "sydney": ["Sydney Show", "Worldwide Show"],
                    "london": ["London Show", "Worldwide Show"], "nairobi": ["Worldwide Show"]}
        for name in ("austin", "sydney", "london", "nairobi", "austin"):
            self.assertEqual(self.run_refresh(AREAS[name]), 0, name)
            self.assertEqual(self.titles(), expected[name], name)  # nothing from the previous area leaks in
            self.assertEqual(self.feed()["location"]["display_name"], AREAS[name]["name"])
        self.assertEqual(self.adapters["world"].seen["timezone"], "America/Chicago")
        self.assertIsNone(self.adapters["world2"].seen)  # unknown coverage: never run

    def test_unknown_coverage_alone_selects_nothing(self):
        self.assertEqual(self.run_refresh(AREAS["austin"]), 0)
        before = open(self.paths["output"], encoding="utf-8").read()
        self.assertEqual(self.run_refresh(AREAS["austin"], [entry("mystery", "world2")]), 1)
        self.assertEqual(open(self.paths["output"], encoding="utf-8").read(), before)

    def test_disabled_sources_do_not_run(self):
        self.adapters["world"].seen = "never"
        sources = [{**e, "enabled": False} if e["id"] == "world" else e for e in self.config_sources]
        self.assertEqual(self.run_refresh(AREAS["austin"], sources), 0)
        self.assertEqual(self.adapters["world"].seen, "never")
        self.assertEqual(self.titles(), ["Austin Show"])

    def test_missing_or_bad_location_and_registry_keep_the_last_feed(self):
        self.assertEqual(self.run_refresh(AREAS["austin"]), 0)
        before = open(self.paths["output"], encoding="utf-8").read()
        for location in (None, {"latitude": 100, "longitude": 0}, {"query": "Nowhere"}):
            with self.subTest(location=location):
                self.assertEqual(self.run_refresh(location), 1)
                self.assertEqual(open(self.paths["output"], encoding="utf-8").read(), before)
        self.assertEqual(self.run_refresh(AREAS["austin"], [{"key": "old-format"}]), 1)
        self.assertEqual(open(self.paths["output"], encoding="utf-8").read(), before)

    def test_geocoded_areas_are_cached_per_data_dir(self):
        provider = FakeProvider(AUSTIN, name="nominatim")
        config = {"location": {"query": "Austin, TX", "country": "US", "timezone": "America/Chicago"}, "sources": self.config_sources}
        for _ in range(3):
            with mock.patch.dict(sources_module.ADAPTERS, self.adapters):
                self.assertEqual(refresh_module.refresh(config, {}, fetcher=object(), location_providers={"nominatim": provider}, **self.paths), 0)
        self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
