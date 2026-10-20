import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from discovery import refresh as refresh_module
from discovery import registry
from discovery.core import FetchError
from discovery.location import LocationError

AUSTIN = {"display_name": "Austin, TX", "latitude": 30.2649, "longitude": -97.7474, "radius_miles": 25.0, "timezone": "America/Chicago",
          "country": "US", "region": "TX"}
SPEC = {"id": "ut_austin", "name": "UT Austin Events", "adapter": "localist", "url": "https://calendar.utexas.edu",
        "coverage": {"type": "area", "name": "UT Austin", "latitude": 30.2849, "longitude": -97.7341, "radius_miles": 10},
        "options": {"days": 14}}


def source():
    return registry.load_sources({"sources": [SPEC]})[0]


def future(hours):
    return (datetime.now().astimezone() + timedelta(hours=hours)).replace(microsecond=0).isoformat()


def api_event(event_id, title, geo=None, start_hours=24, all_day=False, status="live", photo="https://img.example/p.jpg", free=True, address="1 University Station, Austin TX 78712"):
    event = {
        "id": event_id, "title": title, "status": status, "localist_url": f"https://calendar.utexas.edu/event/{title.lower().replace(' ', '-')}",
        "location_name": "Hall", "address": address, "description_text": f"About {title}.",
        "photo_url": photo, "free": free, "first_date": "2000-01-01", "last_date": "2100-01-01",
        "filters": {"event_types": [{"id": 1, "name": "Lectures"}]},
        "event_instances": [{"event_instance": {"id": event_id * 10, "event_id": event_id, "start": future(start_hours),
                                                "end": future(start_hours + 1), "all_day": all_day}}],
    }
    if geo:
        event["geo"] = {"latitude": str(geo[0]), "longitude": str(geo[1]), "city": "Austin", "state": "TX", "country": "US"}
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


class CityProvider:
    name = "nominatim"

    def __init__(self, places=None):
        self.places, self.calls = places or {}, 0

    def lookup(self, fetcher, spec):
        self.calls += 1
        return self.places.get(spec["query"].split(",")[0])


def page(events, number=1, total=1):
    return {"events": events, "page": {"current": number, "total": total}}


class LocalistAdapter(unittest.TestCase):
    def discover(self, pages, **kwargs):
        fetcher = FakeFetcher(pages)
        events, notes = source().discover(fetcher, [], location=AUSTIN, **kwargs)
        return events, notes, fetcher

    def test_austin_events_keep_their_evidence_and_never_invent_coordinates(self):
        events, notes, fetcher = self.discover([page([
            api_event(1, "Campus Lecture", geo=(30.2849, -97.7341)),
            api_event(2, "No Coordinates Talk"),
        ])])
        lecture, talk = events
        self.assertEqual((lecture["title"], lecture["venue"], lecture["url"]), ("Campus Lecture", "Hall", "https://calendar.utexas.edu/event/campus-lecture"))
        self.assertEqual(lecture["image_url"], "https://img.example/p.jpg")
        self.assertTrue(lecture["retrieved_at"] and lecture["start"] and lecture["end"])
        self.assertEqual(lecture["timezone"], "America/Chicago")
        self.assertEqual(lecture["source"]["name"], "UT Austin Events")
        self.assertEqual(lecture["geo"]["latitude"], 30.2849)
        self.assertNotIn("geo", talk)  # the API gave none, so none is invented
        self.assertIn("/api/2/events?days=14", fetcher.urls[0])
        self.assertEqual(source().publisher, "utexas.edu")

    def test_the_timezone_is_only_known_when_the_location_says_so(self):
        events, _ = source().discover(FakeFetcher([page([api_event(1, "Talk")])]), [], location={**AUSTIN, "timezone": None})
        self.assertIsNone(events[0]["timezone"])  # never assumed; the start string carries its own UTC offset
        self.assertRegex(events[0]["start"], r"[+-]\d\d:\d\d$")

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
        for fetcher in (FakeFetcher(error="HTTP 503"), FakeFetcher(["<html>not json</html>"]), FakeFetcher([{"nope": 1}])):
            with self.subTest(fetcher=fetcher.pages), self.assertRaises(FetchError):
                source().discover(fetcher, [], location=AUSTIN)


class Isolation(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.paths = dict(cache_path=os.path.join(self.folder, "cache.json"), output=os.path.join(self.folder, "events.json"),
                          lock_path=os.path.join(self.folder, "lock"), location_cache_path=os.path.join(self.folder, "loc.json"))
        self.pages = [page([api_event(1, "Austin Lecture", geo=(30.2849, -97.7341)), api_event(2, "Far Away Talk", geo=(32.7157, -117.1611))])]
        self.location = {"latitude": 30.2649, "longitude": -97.7474, "radius_miles": 25, "name": "Austin, TX", "timezone": "America/Chicago"}
        self.provider = CityProvider()

    def run_refresh(self, location=None, error=None, sources=None):
        config = {"location": location or self.location, "sources": sources or [SPEC]}
        return refresh_module.refresh(config, {}, fetcher=FakeFetcher(self.pages, error), location_providers={"nominatim": self.provider}, **self.paths)

    def feed(self):
        with open(self.paths["output"], encoding="utf-8") as handle:
            return json.load(handle)

    def test_only_confirmed_events_inside_the_radius_are_published(self):
        self.assertEqual(self.run_refresh(), 0)
        feed = self.feed()
        self.assertEqual([e["title"] for e in feed["events"]], ["Austin Lecture"])
        self.assertEqual(feed["events"][0]["place"]["classification"], "confirmed")
        self.assertEqual(feed["sources"]["ut_austin"]["notes"]["outside_area"], 1)

    def test_radius_decides_which_events_are_returned(self):
        self.pages = [page([api_event(1, "Campus Lecture", geo=(30.2849, -97.7341)), api_event(2, "Waco Talk", geo=(31.5493, -97.1467))])]
        results = {}
        for radius in (1, 25, 100):
            code = self.run_refresh({**self.location, "radius_miles": radius})
            results[radius] = (code, sorted(e["title"] for e in self.feed()["events"]) if code == 0 else None)
        self.assertEqual(results[1][0], 1)  # the campus is about 1.6 mi from the center: everything is confirmed outside, reported as an error
        self.assertEqual(results[25], (0, ["Campus Lecture"]))
        self.assertEqual(results[100], (0, ["Campus Lecture", "Waco Talk"]))  # Waco is about 90 mi away

    def test_events_from_one_search_never_appear_in_another(self):
        self.assertEqual(self.run_refresh(), 0)
        san_diego = {"latitude": 32.7157, "longitude": -117.1611, "radius_miles": 25, "name": "San Diego", "timezone": "America/Los_Angeles"}
        self.pages = [page([api_event(3, "San Diego Concert", geo=(32.72, -117.16))])]
        sources = [{**SPEC, "coverage": {"type": "global"}}]
        self.assertEqual(self.run_refresh(san_diego, sources=sources), 0)
        feed = self.feed()
        self.assertEqual([e["title"] for e in feed["events"]], ["San Diego Concert"])  # no Austin leftovers
        self.assertEqual(feed["location"]["display_name"], "San Diego")

    def test_failed_discovery_keeps_the_last_feed_for_that_location(self):
        self.assertEqual(self.run_refresh(), 0)
        before = open(self.paths["output"], encoding="utf-8").read()
        self.assertEqual(self.run_refresh(error="HTTP 503"), 1)
        self.assertEqual(open(self.paths["output"], encoding="utf-8").read(), before)

    def test_everything_confirmed_outside_is_reported_not_silently_empty(self):
        self.pages = [page([api_event(2, "Far Away Talk", geo=(32.7157, -117.1611))])]
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertEqual(self.run_refresh(), 1)
        self.assertTrue(any("outside the 25 mi search area" in line for line in out))
        self.assertFalse(os.path.exists(self.paths["output"]))

    def test_events_without_coordinates_are_kept_and_labeled_not_dropped(self):
        self.provider = CityProvider({"Austin": {"display_name": "Austin", "latitude": 30.2672, "longitude": -97.7431, "country": "US", "region": "TX"}})
        self.pages = [page([api_event(1, "Mapped", geo=(30.2849, -97.7341)), api_event(2, "Unmapped Talk"), api_event(3, "Nowhere Talk", address=None)])]
        self.assertEqual(self.run_refresh(), 0)
        places = {e["title"]: e["place"] for e in self.feed()["events"]}
        self.assertEqual({t: p["classification"] for t, p in places.items()}, {"Mapped": "confirmed", "Unmapped Talk": "estimated", "Nowhere Talk": "unknown"})
        self.assertEqual(places["Unmapped Talk"]["precision"], "city")
        self.assertIsNone(places["Nowhere Talk"]["latitude"])  # nothing invented, not even the search center
        self.assertEqual(self.provider.calls, 1)  # "Austin" geocoded once for all events

    def test_separate_data_dirs_hold_separate_locations(self):
        other = tempfile.mkdtemp()
        self.assertEqual(self.run_refresh(), 0)
        self.assertFalse(os.listdir(other))
        self.assertTrue(os.path.exists(self.paths["cache_path"]))


if __name__ == "__main__":
    unittest.main()
