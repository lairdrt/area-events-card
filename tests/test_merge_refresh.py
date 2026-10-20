import json
import os
import tempfile
import unittest
from unittest import mock

from discovery import refresh as refresh_module
from discovery import sources as sources_module
from discovery.core import FetchError, make_event, finish_event
from discovery.merge import merge_events, title_relation, venues_match
from discovery.publish import FeedError, publish_feed, validate_feed

from datetime import datetime, timedelta, timezone

PDT = timezone(timedelta(hours=-7))
PUBLISHERS = {"a": "a.example", "b": "b.example", "c": "c.example", "a2": "a.example"}


def record(key, title, hour=19, venue="House of Blues", address=None, day=25, evidence=None, corroborated=False):
    start = datetime(2030, 1, day, hour, 0, tzinfo=PDT)
    event = make_event(key, key.upper(), f"https://{key}.example/events", f"https://{key}.example/{title.replace(' ', '-')}-{hour}",
                       title, start, local_id=f"{title}-{hour}", venue=venue, address=address)
    return finish_event(event, "2030-01-01T00:00:00-08:00", evidence or [f"{key} says so"], corroborated)


class Deduplication(unittest.TestCase):
    def test_same_event_from_two_publishers_merges_and_keeps_both_urls(self):
        merged = merge_events({"a": [record("a", "Chelsea Wolfe", venue="The Magnolia")],
                               "b": [record("b", "Chelsea Wolfe: The Dark World Tour", venue="Magnolia Performing Arts Center")]}, PUBLISHERS)
        self.assertEqual(len(merged), 1)
        self.assertEqual({s["url"] for s in merged[0]["sources"]}, {"https://a.example/Chelsea-Wolfe-19", "https://b.example/Chelsea-Wolfe:-The-Dark-World-Tour-19"})

    def test_separate_showtimes_stay_separate(self):
        merged = merge_events({"a": [record("a", "Wings Over Water", hour=12), record("a", "Wings Over Water", hour=14)],
                               "b": [record("b", "Wings Over Water", hour=12)]}, PUBLISHERS)
        self.assertEqual(sorted(len(e["sources"]) for e in merged), [1, 2])

    def test_same_source_records_never_merge(self):
        merged = merge_events({"a": [record("a", "Open Mic", venue="Bar One"), record("a", "Open Mic", venue="Bar One")]}, PUBLISHERS)
        self.assertEqual(len(merged), 2)

    def test_similar_titles_at_different_venues_do_not_merge(self):
        merged = merge_events({"a": [record("a", "Jazz Night", venue="The Rooftop")], "b": [record("b", "Jazz Night", venue="Balboa Theatre")]}, PUBLISHERS)
        self.assertEqual(len(merged), 2)

    def test_missing_venue_never_matches(self):
        self.assertFalse(venues_match({"venue": None}, {"venue": "Anywhere"}))

    def test_unrelated_titles_do_not_merge(self):
        self.assertIsNone(title_relation("Halloween Party", "Halloween Pub Crawl Night"))
        self.assertEqual(title_relation("Jeff Ousley's Showcase", "Jeff Ousley’s Showcase"), "identical")


class Provenance(unittest.TestCase):
    def test_two_independent_publishers_verify(self):
        merged = merge_events({"a": [record("a", "Show")], "b": [record("b", "Show")]}, PUBLISHERS)[0]
        self.assertTrue(merged["verified"])
        self.assertEqual(merged["verification"]["independent_publishers"], ["a.example", "b.example"])

    def test_one_publisher_with_two_sources_does_not_verify(self):
        merged = merge_events({"a": [record("a", "Show")], "a2": [record("a2", "Show")]}, PUBLISHERS)[0]
        self.assertFalse(merged["verified"])
        self.assertIn("multiple pages from one publisher count as one source", merged["verification"]["evidence"])

    def test_single_source_is_never_verified_even_if_corroborated(self):
        merged = merge_events({"a": [record("a", "Show", corroborated=True)]}, PUBLISHERS)[0]
        self.assertFalse(merged["verified"])
        self.assertEqual(merged["verification"]["status"], "corroborated")

    def test_conflict_blocks_verification(self):
        merged = merge_events({"a": [record("a", "Show", evidence=["CONFLICT: dates differ"])], "b": [record("b", "Show")]}, PUBLISHERS)[0]
        self.assertFalse(merged["verified"])

    def test_threshold_is_configurable(self):
        merged = merge_events({"a": [record("a", "Show")], "b": [record("b", "Show")]}, PUBLISHERS, min_publishers=3)[0]
        self.assertFalse(merged["verified"])


class Adapter:

    def __init__(self, events=None, error=None):
        self.events, self.error = events, error

    def discover(self, fetcher, previous, **_):
        if self.error:
            raise FetchError(self.error)
        return self.events, {"fake": True}


def future_record(key, title):
    start = datetime.now(PDT) + timedelta(days=1)
    event = make_event(key, key, "https://x.example", f"https://{key}.example/{title}", title, start, local_id=title,
                       venue="Venue")
    return finish_event(event, "2030-01-01T00:00:00-08:00", ["e"], False)


class RefreshRetention(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.paths = dict(cache_path=os.path.join(self.folder, "cache.json"), output=os.path.join(self.folder, "events.json"),
                          lock_path=os.path.join(self.folder, "lock"))
        self.config = {"retention_days": 3,
                       "location": {"latitude": 40.0, "longitude": -100.0, "timezone": "America/Chicago", "radius_miles": 25},
                       "sources": [{"id": i, "name": i.upper(), "adapter": f"fake_{i}", "url": f"https://{i}.example", "coverage": {"type": "global"}}
                                   for i in ("a", "b")]}
        self.adapters = {"a": Adapter([future_record("a", "One")]), "b": Adapter([future_record("b", "Two")])}

    def run_refresh(self, **extra):
        with mock.patch.dict(sources_module.ADAPTERS, {f"fake_{k}": v for k, v in self.adapters.items()}):
            return refresh_module.refresh(self.config, {}, fetcher=object(), **self.paths, **extra)

    def titles(self):
        with open(self.paths["output"], encoding="utf-8") as handle:
            return sorted(e["title"] for e in json.load(handle)["events"])

    def test_partial_failure_keeps_previous_events_and_reports_freshness(self):
        self.assertEqual(self.run_refresh(), 0)
        self.adapters["b"] = Adapter(error="site down")
        self.assertEqual(self.run_refresh(), 2)
        self.assertEqual(self.titles(), ["One", "Two"])
        with open(self.paths["output"], encoding="utf-8") as handle:
            status = json.load(handle)["sources"]["b"]
        self.assertFalse(status["ok"])
        self.assertEqual(status["error"], "site down")
        self.assertTrue(status["last_success_at"])

    def test_empty_extraction_is_a_failure_not_a_wipe(self):
        self.run_refresh()
        self.adapters["a"] = Adapter([])
        self.assertEqual(self.run_refresh(), 2)
        self.assertEqual(self.titles(), ["One", "Two"])

    def test_total_failure_leaves_files_untouched(self):
        self.run_refresh()
        before = open(self.paths["output"], encoding="utf-8").read()
        self.adapters = {"a": Adapter(error="down"), "b": Adapter(error="down")}
        self.assertEqual(self.run_refresh(), 1)
        self.assertEqual(open(self.paths["output"], encoding="utf-8").read(), before)

    def test_events_from_a_source_failing_beyond_retention_are_dropped(self):
        self.run_refresh()
        with open(self.paths["cache_path"], encoding="utf-8") as handle:
            cache = json.load(handle)
        cache["b"]["last_success_at"] = "2020-01-01T00:00:00-08:00"
        with open(self.paths["cache_path"], "w", encoding="utf-8") as handle:
            json.dump(cache, handle)
        self.adapters["b"] = Adapter(error="down")
        self.run_refresh()
        self.assertEqual(self.titles(), ["One"])

    def test_concurrent_refresh_is_refused(self):
        refresh_module.Lock(self.paths["lock_path"]).acquire()
        self.assertEqual(self.run_refresh(), 1)
        self.assertFalse(os.path.exists(self.paths["output"]))

    def test_stale_lock_is_replaced(self):
        lock = refresh_module.Lock(self.paths["lock_path"]).acquire()
        old = os.path.getmtime(lock.path) - 3 * 3600
        os.utime(lock.path, (old, old))
        self.assertEqual(self.run_refresh(), 0)

    def test_publish_failure_reports_error_and_keeps_destination(self):
        destination = os.path.join(self.folder, "missing-dir", "events.json")
        self.assertEqual(self.run_refresh(publish_to=destination), 1)
        self.assertFalse(os.path.exists(destination))


class PublishSafety(unittest.TestCase):
    def feed(self, n=1):
        events = [{"id": f"x:{i}", "title": "T", "start": "2030-01-01T00:00:00-08:00", "url": "https://x.example"} for i in range(n)]
        return {"generated_at": "2030-01-01T00:00:00-08:00", "events": events}

    def test_valid_feed_is_published_and_no_temp_files_remain(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = os.path.join(folder, "events.json")
            self.assertEqual(publish_feed(self.feed(3), destination), 3)
            self.assertEqual(os.listdir(folder), ["events.json"])
            validate_feed(json.load(open(destination, encoding="utf-8")))

    def test_empty_or_invalid_feed_never_replaces_a_good_one(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = os.path.join(folder, "events.json")
            publish_feed(self.feed(2), destination)
            good = open(destination, encoding="utf-8").read()
            for bad in ({"generated_at": "x", "events": []}, {"events": [{"id": "1"}]}, {"events": None}, []):
                with self.assertRaises(FeedError):
                    publish_feed(bad, destination)
            self.assertEqual(open(destination, encoding="utf-8").read(), good)

    def test_failed_write_leaves_destination_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = os.path.join(folder, "events.json")
            publish_feed(self.feed(2), destination)
            good = open(destination, encoding="utf-8").read()
            with mock.patch("discovery.publish.os.replace", side_effect=OSError("share dropped")):
                with self.assertRaises(OSError):
                    publish_feed(self.feed(5), destination)
            self.assertEqual(open(destination, encoding="utf-8").read(), good)
            self.assertEqual(os.listdir(folder), ["events.json"])

    def test_missing_destination_directory_is_an_error(self):
        with self.assertRaises(FeedError):
            publish_feed(self.feed(), os.path.join(tempfile.gettempdir(), "no-such-dir-xyz", "events.json"))


if __name__ == "__main__":
    unittest.main()
