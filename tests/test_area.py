"""Where the discovery area comes from: configuration, then Home Assistant, then the browser, else an error."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from discovery import hint as hint_cli
from discovery import refresh as refresh_module
from discovery import sources as sources_module
from discovery.core import finish_event, make_event
from discovery.location import HINT_FILE, LocationError, read_hints, resolve_area

PDT = timezone(timedelta(hours=-7))
HA = {"latitude": 30.2672, "longitude": -97.7431, "timezone": "America/Chicago", "display_name": "Home", "received_at": "2026-10-20T10:00:00+00:00"}
BROWSER = {"latitude": 51.5072, "longitude": -0.1276, "accuracy_m": 35, "received_at": "2026-10-20T11:00:00+00:00"}
EXPLICIT = {"latitude": -33.8688, "longitude": 151.2093, "name": "Sydney", "timezone": "Australia/Sydney", "radius_miles": 12}


class Resolution(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.hints = os.path.join(self.folder, HINT_FILE)

    def write(self, document):
        with open(self.hints, "w", encoding="utf-8") as handle:
            json.dump(document, handle)

    def resolve(self, config=None):
        return resolve_area(config or {}, self.hints, None, None, {})

    def test_explicit_configuration_beats_home_assistant_and_browser(self):
        self.write({"home_assistant": HA, "browser": BROWSER})
        area = self.resolve({"location": EXPLICIT})
        self.assertEqual((area["display_name"], area["basis"], area["radius_miles"]), ("Sydney", "configured", 12.0))

    def test_home_assistant_is_used_when_nothing_is_configured(self):
        self.write({"home_assistant": HA, "browser": BROWSER})
        area = self.resolve()
        self.assertEqual((area["basis"], area["source"], area["display_name"], area["timezone"], area["timezone_source"]),
                         ("home_assistant", "hint:home_assistant", "Home", "America/Chicago", "config"))
        self.assertEqual((area["latitude"], area["longitude"], area["radius_miles"]), (30.2672, -97.7431, 25.0))
        self.assertEqual(area["received_at"], HA["received_at"])
        self.assertEqual(self.resolve({"default_radius_miles": 40})["radius_miles"], 40.0)

    def test_browser_location_is_the_next_fallback_and_never_gets_an_assumed_timezone(self):
        self.write({"browser": BROWSER})
        area = self.resolve()
        self.assertEqual((area["basis"], area["display_name"], area["timezone"], area["timezone_source"], area["accuracy_m"]),
                         ("browser", "Browser location", None, "unknown", 35))

    def test_invalid_home_assistant_hint_falls_through_and_is_reported(self):
        for bad in ({"latitude": 0, "longitude": 0}, {"latitude": 95, "longitude": 1}, {"latitude": "x", "longitude": 1}, {"latitude": 30.0},
                    {"latitude": 30.0, "longitude": -97.0, "timezone": "Central"}, "text"):
            self.write({"home_assistant": bad, "browser": BROWSER})
            hints, problems = read_hints(self.hints)
            self.assertEqual(list(hints), ["browser"], bad)
            self.assertEqual(len(problems), 1, bad)
            self.assertEqual(self.resolve()["basis"], "browser")

    def test_nothing_available_asks_for_configuration(self):
        for document in (None, {}, {"home_assistant": {"latitude": 0, "longitude": 0}}):
            if document is not None:
                self.write(document)
            with self.assertRaisesRegex(LocationError, "no location available.*python -m discovery.hint"):
                self.resolve()
        with open(self.hints, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaisesRegex(LocationError, "unreadable"):
            self.resolve()

    def test_nothing_is_hardcoded(self):
        with self.assertRaises(LocationError):
            resolve_area({}, None)  # no hint file at all: no default place is substituted


class HintCommand(unittest.TestCase):
    def test_writes_validated_hints_and_clears_them(self):
        folder = tempfile.mkdtemp()
        base = ["--data-dir", folder]
        self.assertEqual(hint_cli.main(base + ["--source", "home_assistant", "--latitude", "30.2672", "--longitude", "-97.7431",
                                               "--timezone", "America/Chicago", "--name", "Home"]), 0)
        self.assertEqual(hint_cli.main(base + ["--source", "browser", "--latitude", "51.5", "--longitude", "-0.12", "--accuracy-m", "40"]), 0)
        hints, problems = read_hints(os.path.join(folder, HINT_FILE))
        self.assertEqual((sorted(hints), problems, hints["home_assistant"]["timezone"]), (["browser", "home_assistant"], [], "America/Chicago"))
        self.assertEqual(hint_cli.main(base + ["--source", "home_assistant", "--latitude", "999", "--longitude", "0"]), 1)  # rejected, file unchanged
        self.assertEqual(sorted(read_hints(os.path.join(folder, HINT_FILE))[0]), ["browser", "home_assistant"])
        self.assertEqual(hint_cli.main(base + ["--source", "home_assistant", "--clear"]), 0)
        self.assertEqual(sorted(read_hints(os.path.join(folder, HINT_FILE))[0]), ["browser"])
        with self.assertRaises(SystemExit):
            hint_cli.main(base + ["--source", "browser"])  # coordinates required


class Local:
    def discover(self, fetcher, previous, source_id="x", source_name="X", url=None, location=None, **_):
        start = datetime.now(PDT) + timedelta(hours=24)
        event = make_event(source_id, source_name, url, f"{url}/e", "Hello", start, local_id="e", venue="Hall")
        event["geo"] = {"latitude": location["latitude"], "longitude": location["longitude"], "source": "test"}
        return [finish_event(event, "2030-01-01T00:00:00-08:00", ["e"], False)], {}


class RefreshUsesTheHint(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.paths = dict(cache_path=os.path.join(self.folder, "cache.json"), output=os.path.join(self.folder, "events.json"),
                          lock_path=os.path.join(self.folder, "lock"), location_cache_path=os.path.join(self.folder, "loc.json"))
        self.config = {"sources": [{"id": "s", "name": "S", "adapter": "local", "url": "https://s.example", "coverage": {"type": "global"}}]}

    def run_refresh(self, config=None):
        with mock.patch.dict(sources_module.ADAPTERS, {"local": Local()}):
            return refresh_module.refresh(config or self.config, {}, fetcher=object(), **self.paths)

    def write_hint(self, document):
        with open(os.path.join(self.folder, HINT_FILE), "w", encoding="utf-8") as handle:
            json.dump(document, handle)

    def feed(self):
        with open(self.paths["output"], encoding="utf-8") as handle:
            return json.load(handle)

    def test_without_any_location_the_refresh_fails_clearly_and_keeps_the_feed(self):
        self.write_hint({"home_assistant": HA})
        self.assertEqual(self.run_refresh(), 0)
        before = open(self.paths["output"], encoding="utf-8").read()
        os.remove(os.path.join(self.folder, HINT_FILE))
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertEqual(self.run_refresh(), 1)
        self.assertTrue(any("no location available" in line for line in out))
        self.assertEqual(open(self.paths["output"], encoding="utf-8").read(), before)

    def test_feed_records_where_the_area_came_from(self):
        self.write_hint({"home_assistant": HA, "browser": BROWSER})
        self.assertEqual(self.run_refresh(), 0)
        location = self.feed()["location"]
        self.assertEqual((location["basis"], location["display_name"], location["timezone"]), ("home_assistant", "Home", "America/Chicago"))
        self.assertEqual(self.feed()["events"][0]["place"]["classification"], "confirmed")

    def test_changing_the_hint_moves_discovery_and_never_mixes_areas(self):
        self.write_hint({"home_assistant": HA})
        self.assertEqual(self.run_refresh(), 0)
        first = self.feed()["events"][0]["place"]
        self.write_hint({"home_assistant": {**HA, "latitude": 48.8566, "longitude": 2.3522, "timezone": "Europe/Paris", "display_name": "Paris"}})
        self.assertEqual(self.run_refresh(), 0)
        feed = self.feed()
        self.assertEqual((feed["location"]["display_name"], len(feed["events"])), ("Paris", 1))
        self.assertNotEqual(feed["events"][0]["place"]["latitude"], first["latitude"])  # rebuilt for the new area, nothing carried over

    def test_explicit_configuration_ignores_the_hint(self):
        self.write_hint({"home_assistant": HA})
        self.assertEqual(self.run_refresh({**self.config, "location": EXPLICIT}), 0)
        self.assertEqual(self.feed()["location"]["basis"], "configured")


if __name__ == "__main__":
    unittest.main()
