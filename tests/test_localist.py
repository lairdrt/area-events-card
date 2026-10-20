import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from discovery import refresh as refresh_module
from discovery import sources
from discovery.core import FetchError
from discovery.location import classify_events, distance_miles, event_geo, resolve_location
from discovery.sources import localist

AUSTIN = {"display_name": "Austin, TX", "latitude": 30.2649, "longitude": -97.7474, "radius_miles": 25.0, "timezone": "America/Chicago"}
SPEC = {"key": "ut_austin", "adapter": "localist", "site": "https://calendar.utexas.edu", "name": "UT Austin Events", "days": 14}


def future(hours):
    return (datetime.now().astimezone() + timedelta(hours=hours)).replace(microsecond=0).isoformat()


def api_event(event_id, title, geo=None, start_hours=24, all_day=False, status="live", photo="https://img.example/p.jpg", free=True):
    event = {
        "id": event_id, "title": title, "status": status, "localist_url": f"https://calendar.utexas.edu/event/{title.lower().replace(' ', '-')}",
        "location_name": "Hall", "address": "1 University Station, Austin TX 78712", "description_text": f"About {title}.",
        "photo_url": photo, "free": free, "first_date": "2000-01-01", "last_date": "2100-01-01",
        "filters": {"event_types": [{"id": 1, "name": "Lectures"}]},
        "event_instances": [{"event_instance": {"id": event_id * 10, "event_id": event_id, "start": future(start_hours),
                                                "end": future(start_hours + 1), "all_day": all_day}}],
    }
    if geo:
        event["geo"] = {"latitude": str(geo[0]), "longitude": str(geo[1]), "city": "Austin", "state": "TX"}
    return {"event": event}


class FakeFetcher:
    def __init__(self, pages=None, error=None):
        self.pages, self.error, self.urls = pages or [], error, []

    def get(self, url, check_robots=True):
        self.urls.append(url)
        if self.error:
            raise FetchError(self.error)
        page = int(url.split("page=")[1])
        body = self.pages[page - 1] if page <= len(self.pages) else {"events": [], "page": {"total": len(self.pages)}}
        return body if isinstance(body, str) else json.dumps(body)


def page(events, number=1, total=1):
    return {"events": events, "page": {"current": number, "total": total}}


class LocalistAdapter(unittest.TestCase):
    def discover(self, pages, **options):
        adapter = sources.load_spec(SPEC)
        fetcher = FakeFetcher(pages)
        events, notes = adapter.discover(fetcher, [], location=AUSTIN, **options)
        return events, notes, fetcher

    def test_austin_events_keep_their_evidence_and_never_invent_coordinates(self):
        events, notes, fetcher = self.discover([page([
            api_event(1, "Campus Lecture", geo=(30.2849, -97.7341)),
            api_event(2, "No Coordinates Talk"),
        ])])
        self.assertEqual(len(events), 2)
        lecture, talk = events
        self.assertEqual((lecture["title"], lecture["venue"], lecture["url"]), ("Campus Lecture", "Hall", "https://calendar.utexas.edu/event/campus-lecture"))
        self.assertEqual(lecture["image_url"], "https://img.example/p.jpg")
        self.assertTrue(lecture["retrieved_at"] and lecture["start"] and lecture["end"])
        self.assertEqual(lecture["timezone"], "America/Chicago")
        self.assertEqual(lecture["geo"]["latitude"], 30.2849)
        self.assertNotIn("geo", talk)  # the API gave none, so none is invented
        self.assertTrue(any("coordinates" in e for e in lecture["verification"]["evidence"]))
        self.assertIn("/api/2/events?days=14", fetcher.urls[0])

    def test_all_day_not_live_and_ended_instances_are_skipped(self):
        events, notes, _ = self.discover([page([
            api_event(1, "Exhibit", all_day=True), api_event(2, "Draft", status="deleted"),
            api_event(3, "Earlier Today", start_hours=-5), api_event(4, "Keeper"),
        ])])
        self.assertEqual([e["title"] for e in events], ["Keeper"])
        self.assertEqual((notes["all_day_skipped"], notes["not_live_skipped"], notes["ended_skipped"]), (1, 1, 1))

    def test_paging_stops_at_the_last_page(self):
        events, notes, fetcher = self.discover([page([api_event(1, "One")], 1, 2), page([api_event(2, "Two")], 2, 2)])
        self.assertEqual((len(events), len(fetcher.urls)), (2, 2))

    def test_failures_raise_fetch_errors(self):
        adapter = sources.load_spec(SPEC)
        for fetcher in (FakeFetcher(error="HTTP 503"), FakeFetcher(["<html>not json</html>"]), FakeFetcher([{"nope": 1}])):
            with self.subTest(fetcher=fetcher.pages), self.assertRaises(FetchError):
                adapter.discover(fetcher, [], location=AUSTIN)

    def test_source_binding(self):
        adapter = sources.load_spec(SPEC)
        self.assertEqual((adapter.KEY, adapter.NAME, adapter.PUBLISHER, adapter.COVERAGE["kind"]), ("ut_austin", "UT Austin Events", "utexas.edu", "configured"))
        with self.assertRaises(ValueError):
            localist.make_source({"key": "x", "adapter": "localist"})


class GeographicFiltering(unittest.TestCase):
    def test_radius_boundary_is_inclusive_and_exact(self):
        center = {"latitude": 30.0, "longitude": -97.0}
        event = {"geo": {"latitude": 31.0, "longitude": -97.0}}
        miles = distance_miles(30.0, -97.0, 31.0, -97.0)
        self.assertAlmostEqual(miles, 69.1, delta=0.2)
        for radius, expected in ((miles, "within"), (miles + 0.01, "within"), (miles - 0.01, "outside")):
            kept, outside = classify_events([dict(event)], {**center, "radius_miles": radius})
            self.assertEqual((len(kept), outside), (1, 0) if expected == "within" else (0, 1), radius)

    def test_events_with_unknown_coordinates_are_kept_and_marked_unknown(self):
        events = [{"title": "no geo"}, {"title": "bad geo", "geo": {"latitude": "x", "longitude": 1}},
                  {"title": "zero geo", "geo": {"latitude": 0, "longitude": 0}}, {"title": "near", "geo": {"latitude": 30.27, "longitude": -97.74}},
                  {"title": "far", "geo": {"latitude": 32.7, "longitude": -117.1}}]
        kept, outside = classify_events(events, AUSTIN)
        self.assertEqual(outside, 1)
        self.assertEqual({e["title"]: e["location_status"] for e in kept}, {"no geo": "unknown", "bad geo": "unknown", "zero geo": "unknown", "near": "within"})
        self.assertIsNone(kept[0]["distance_miles"])
        self.assertIsNotNone(kept[3]["distance_miles"])
        self.assertIsNone(event_geo({"title": "x"}))


class Isolation(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.paths = dict(cache_path=os.path.join(self.folder, "cache.json"), output=os.path.join(self.folder, "events.json"),
                          lock_path=os.path.join(self.folder, "lock"), location_cache_path=os.path.join(self.folder, "loc.json"))
        self.pages = [page([api_event(1, "Austin Lecture", geo=(30.2849, -97.7341)), api_event(2, "Far Away Talk", geo=(32.7157, -117.1611))])]
        self.config = {"location": {"latitude": 30.2649, "longitude": -97.7474, "radius_miles": 25, "name": "Austin, TX", "timezone": "America/Chicago"},
                       "sources": [SPEC]}

    def run_refresh(self, location=None, error=None):
        config = {**self.config, "location": location or self.config["location"]}
        fetcher = FakeFetcher(self.pages, error)
        code = refresh_module.refresh(config, {}, fetcher=fetcher, **self.paths)
        return code, fetcher

    def feed(self):
        with open(self.paths["output"], encoding="utf-8") as handle:
            return json.load(handle)

    def test_only_events_inside_the_radius_are_published(self):
        self.assertEqual(self.run_refresh()[0], 0)
        feed = self.feed()
        self.assertEqual([e["title"] for e in feed["events"]], ["Austin Lecture"])
        self.assertEqual(feed["events"][0]["location_status"], "within")
        self.assertEqual(feed["sources"]["ut_austin"]["notes"]["outside_area"], 1)

    def test_radius_decides_which_events_are_returned(self):
        self.pages = [page([api_event(1, "Campus Lecture", geo=(30.2849, -97.7341)), api_event(2, "Waco Talk", geo=(31.5493, -97.1467))])]
        results = {}
        for radius in (1, 25, 100):
            code, _ = self.run_refresh({**self.config["location"], "radius_miles": radius})
            results[radius] = (code, sorted(e["title"] for e in self.feed()["events"]) if code == 0 else None)
        self.assertEqual(results[1][0], 1)  # the campus is about 1.6 mi from the center: everything is outside, reported as an error
        self.assertEqual(results[25], (0, ["Campus Lecture"]))
        self.assertEqual(results[100], (0, ["Campus Lecture", "Waco Talk"]))  # Waco is about 90 mi away

    def test_events_from_one_search_never_appear_in_another(self):
        self.assertEqual(self.run_refresh()[0], 0)
        san_diego = {"latitude": 32.7157, "longitude": -117.1611, "radius_miles": 25, "name": "San Diego", "timezone": "America/Los_Angeles"}
        self.pages = [page([api_event(3, "San Diego Concert", geo=(32.72, -117.16))])]
        self.assertEqual(self.run_refresh(san_diego)[0], 0)
        feed = self.feed()
        self.assertEqual([e["title"] for e in feed["events"]], ["San Diego Concert"])  # no Austin leftovers
        self.assertEqual(feed["location"]["display_name"], "San Diego")

    def test_failed_discovery_keeps_the_last_feed_for_that_location(self):
        self.assertEqual(self.run_refresh()[0], 0)
        before = open(self.paths["output"], encoding="utf-8").read()
        self.assertEqual(self.run_refresh(error="HTTP 503")[0], 1)
        self.assertEqual(open(self.paths["output"], encoding="utf-8").read(), before)

    def test_everything_outside_the_area_is_reported_not_silently_empty(self):
        self.pages = [page([api_event(2, "Far Away Talk", geo=(32.7157, -117.1611))])]
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertEqual(self.run_refresh()[0], 1)
        self.assertTrue(any("outside the 25 mi search area" in line for line in out))
        self.assertFalse(os.path.exists(self.paths["output"]))

    def test_separate_data_dirs_hold_separate_locations(self):
        other = tempfile.mkdtemp()
        self.assertEqual(self.run_refresh()[0], 0)
        self.assertFalse(os.listdir(other))
        self.assertTrue(os.path.exists(self.paths["cache_path"]))

    def test_geocoded_location_cache_is_per_location_file(self):
        class Provider:
            name = "nominatim"
            calls = 0

            def lookup(self, fetcher, spec):
                type(self).calls += 1
                return {"display_name": "Austin, TX", "latitude": 30.2649, "longitude": -97.7474, "country": "US", "region": "TX"}

        for _ in range(2):
            resolve_location({"query": "Austin, TX"}, None, self.paths["location_cache_path"], {"nominatim": Provider()})
        self.assertEqual(Provider.calls, 1)
        resolve_location({"query": "Austin, TX"}, None, os.path.join(self.folder, "other-loc.json"), {"nominatim": Provider()})
        self.assertEqual(Provider.calls, 2)  # a different data dir has its own cache


class SanDiegoRegression(unittest.TestCase):
    def test_existing_sources_are_kept_unfiltered_and_marked_unknown(self):
        from discovery.merge import merge_events
        from tests.test_merge_evidence import ob_records, PUBLISHERS
        records = ob_records()
        for source in records.values():
            classify_events(source, resolve_location(None))
        merged = merge_events(records, PUBLISHERS)
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0]["location_status"], len(merged[0]["sources"])), ("unknown", 3))

    def test_default_location_still_selects_the_four_san_diego_sources(self):
        from discovery.location import select_sources
        config = refresh_module.load_config()
        chosen, skipped = select_sources(config["sources"], resolve_location(config["location"]), sources.load_spec)
        self.assertEqual([s["key"] for s, _ in chosen], ["kpbs", "sandiegoreader", "dosd", "sandiego_gov"])
        self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()
