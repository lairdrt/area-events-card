"""Per-area storage: stable ids, isolation, default-area compatibility, safe paths, atomic writes."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from discovery import refresh as refresh_module
from discovery import sources as sources_module
from discovery.areas import (
    AREA_META, DEFAULT_AREA, AreaError, AreaStore, canonical_location, derive_area_id, load_status, save_status, validate_area_id,
)
from discovery.core import finish_event, make_event
from discovery.service import Service

PDT = timezone(timedelta(hours=-7))
SAN_DIEGO = {"latitude": 32.7157, "longitude": -117.1611, "radius_miles": 25}
AUSTIN = {"latitude": 30.2672, "longitude": -97.7431, "radius_miles": 25}


class AreaIds(unittest.TestCase):
    def test_ids_are_stable_and_ignore_everything_but_coordinates_and_radius(self):
        base = derive_area_id(SAN_DIEGO)
        self.assertRegex(base, r"^a_[0-9a-f]{12}$")
        self.assertEqual(base, derive_area_id(dict(reversed(list(SAN_DIEGO.items())))))  # key order
        self.assertEqual(base, derive_area_id({**SAN_DIEGO, "display_name": "Elsewhere", "timezone": "Europe/Paris", "country": "FR"}))
        self.assertEqual(base, derive_area_id({"latitude": "32.71570", "longitude": -117.16110004, "radius_miles": 25.04}))  # rounding
        self.assertEqual(base, derive_area_id({**SAN_DIEGO, "radius_miles": "25"}))
        self.assertEqual(derive_area_id({"latitude": -0.00001, "longitude": 1, "radius_miles": 5}), derive_area_id({"latitude": 0.0, "longitude": 1, "radius_miles": 5}))
        self.assertEqual(base, "a_" + base[2:])  # no randomness: recomputing gives the same value
        self.assertEqual(canonical_location(SAN_DIEGO), {"latitude": "32.7157", "longitude": "-117.1611", "radius_miles": "25.0"})

    def test_different_places_or_radii_give_different_ids(self):
        ids = {derive_area_id(SAN_DIEGO), derive_area_id(AUSTIN), derive_area_id({**SAN_DIEGO, "radius_miles": 26}),
               derive_area_id({**SAN_DIEGO, "latitude": 32.7158}), derive_area_id({**SAN_DIEGO, "longitude": 117.1611}),
               derive_area_id({"latitude": -33.8688, "longitude": 151.2093, "radius_miles": 25})}
        self.assertEqual(len(ids), 6)

    def test_bad_locations_are_refused(self):
        for bad in ({}, {"latitude": 1, "longitude": 2}, {"latitude": 91, "longitude": 0, "radius_miles": 5},
                    {"latitude": 0, "longitude": 181, "radius_miles": 5}, {"latitude": 0, "longitude": 0, "radius_miles": 0},
                    {"latitude": "x", "longitude": 0, "radius_miles": 5}, None):
            with self.subTest(bad=bad), self.assertRaises(AreaError):
                derive_area_id(bad)


class PathSafety(unittest.TestCase):
    def setUp(self):
        self.store = AreaStore(tempfile.mkdtemp())

    def test_only_default_and_derived_ids_are_accepted(self):
        for good in (DEFAULT_AREA, derive_area_id(AUSTIN)):
            self.assertEqual(validate_area_id(good), good)
        bad = ["", "..", "../x", "a_../../etc", "a_" + "0" * 11, "a_" + "0" * 13, "A_" + "0" * 12, "a_" + "G" * 12, "default/", "default/..",
               "/etc/passwd", "C:\\Windows", "a_000000000000\x00", " default", "Default", None, 5, ["default"], "a_000000000000/x", "a_000000000000\\x"]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(AreaError):
                self.store.paths(value)
            with self.assertRaises(AreaError):
                self.store.ensure(value)

    def test_paths_stay_inside_the_storage_root(self):
        for area in (DEFAULT_AREA, derive_area_id(SAN_DIEGO), derive_area_id(AUSTIN)):
            paths = self.store.paths(area)
            for path in (paths.directory, paths.feed, paths.source_cache, paths.location_cache, paths.status, paths.lock, paths.hint):
                self.assertTrue(os.path.abspath(path).startswith(self.store.root + os.sep) or os.path.abspath(path) == self.store.root, path)

    def test_symlinked_area_directory_cannot_leave_the_tree(self):
        outside = tempfile.mkdtemp()
        area = derive_area_id(AUSTIN)
        os.makedirs(os.path.join(self.store.root, "areas"))
        try:
            os.symlink(outside, os.path.join(self.store.root, "areas", area), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        with self.assertRaises(AreaError):
            self.store.paths(area)

    def test_a_directory_is_bound_to_the_place_it_was_created_for(self):
        area = derive_area_id(AUSTIN)
        self.store.ensure(area, AUSTIN)
        self.store.ensure(area)  # reopening without a location is fine
        self.store.ensure(area, {**AUSTIN, "latitude": 30.26720001})  # the same place after rounding
        with self.assertRaises(AreaError):
            self.store.ensure(area, SAN_DIEGO)  # a different place can never claim this directory
        meta = os.path.join(self.store.paths(area).directory, AREA_META)
        with open(meta, "w", encoding="utf-8") as handle:
            json.dump({"id": area, "location": canonical_location(SAN_DIEGO)}, handle)  # tampered metadata
        with self.assertRaises(AreaError):
            self.store.ensure(area)
        with open(meta, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        with self.assertRaises(AreaError):
            self.store.ensure(area)

    def test_listing_only_returns_valid_area_directories(self):
        os.makedirs(os.path.join(self.store.root, "areas", "not-an-area"))
        os.makedirs(os.path.join(self.store.root, "areas", "a_" + "0" * 12))
        self.assertEqual(self.store.list_areas(), [DEFAULT_AREA, "a_" + "0" * 12])


class DefaultArea(unittest.TestCase):
    def test_default_is_the_existing_top_level_layout(self):
        folder = tempfile.mkdtemp()
        paths = AreaStore(folder).paths(DEFAULT_AREA)
        expected = {"directory": "", "feed": "events.json", "source_cache": "source-cache.json", "location_cache": "location-cache.json",
                    "status": "status.json", "lock": ".refresh.lock", "hint": "location-hint.json"}
        for field, name in expected.items():
            self.assertEqual(os.path.normpath(getattr(paths, field)), os.path.normpath(os.path.join(folder, name)), field)
        self.assertFalse(os.path.exists(os.path.join(folder, "areas")))  # nothing is created for the default area

    def test_the_service_still_uses_the_same_files_and_serves_the_existing_feed(self):
        folder = tempfile.mkdtemp()
        feed = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sources": {},
                "events": [{"id": "x:1", "title": "T", "start": "2030-01-01T00:00:00-08:00", "url": "https://x.example"}]}
        with open(os.path.join(folder, "events.json"), "w", encoding="utf-8") as handle:
            json.dump(feed, handle)
        with open(os.path.join(folder, "status.json"), "w", encoding="utf-8") as handle:
            json.dump({"last_success_at": "2030-01-01T00:00:00+00:00", "last_exit_code": 0}, handle)
        calls = []

        def fake_refresh(config, options, **paths):
            calls.append(paths)
            return 0

        service = Service(folder, config_path=os.path.join(os.path.dirname(__file__), "..", "discovery.config.json"), refresh_fn=fake_refresh)
        self.assertEqual((service.feed_path, service.status_path), (os.path.join(folder, "events.json"), os.path.join(folder, "status.json")))
        self.assertEqual(json.loads(service.read_feed()[0]), feed)
        self.assertEqual(service.status["last_exit_code"], 0)
        service.run_once()
        self.assertEqual(calls, [{"output": os.path.join(folder, "events.json"), "cache_path": os.path.join(folder, "source-cache.json"),
                                  "lock_path": os.path.join(folder, ".refresh.lock")}])  # exactly the arguments it always passed
        self.assertEqual(sorted(os.listdir(folder)), ["events.json", "status.json"])


class Local:
    def __init__(self, title, fail=False):
        self.title, self.fail, self.calls = title, fail, 0

    def discover(self, fetcher, previous, source_id="x", source_name="X", url=None, location=None, **_):
        self.calls += 1
        if self.fail:
            from discovery.core import FetchError
            raise FetchError("site down")
        start = datetime.now(PDT) + timedelta(hours=24)
        event = make_event(source_id, source_name, url, f"{url}/{self.title}", self.title, start, local_id=self.title, venue="Hall")
        event["geo"] = {"latitude": location["latitude"], "longitude": location["longitude"], "source": "test"}
        return [finish_event(event, "2030-01-01T00:00:00-08:00", ["e"], False)], {}


def config_for(location):
    return {"location": {**location, "timezone": "America/Chicago"},
            "sources": [{"id": "s", "name": "S", "adapter": "local", "url": "https://s.example", "coverage": {"type": "global"}}]}


class Isolation(unittest.TestCase):
    def setUp(self):
        self.store = AreaStore(tempfile.mkdtemp())
        self.sd, self.tx = derive_area_id(SAN_DIEGO), derive_area_id(AUSTIN)
        self.adapter = Local("Hello")

    def run_area(self, area, location, adapter=None, store=None):
        store = store or self.store
        paths = store.ensure(area, location) if area != DEFAULT_AREA else store.paths(area)
        with mock.patch.dict(sources_module.ADAPTERS, {"local": adapter or self.adapter}):
            code = refresh_module.refresh(config_for(location), {}, fetcher=object(), **paths.refresh_arguments())
        return code, paths

    def feed(self, paths):
        with open(paths.feed, encoding="utf-8") as handle:
            return json.load(handle)

    def test_each_area_writes_only_its_own_files(self):
        _, a = self.run_area(self.sd, SAN_DIEGO, Local("San Diego Show"))
        _, b = self.run_area(self.tx, AUSTIN, Local("Austin Show"))
        _, d = self.run_area(DEFAULT_AREA, SAN_DIEGO, Local("Default Show"))
        self.assertEqual([self.feed(p)["events"][0]["title"] for p in (a, b, d)], ["San Diego Show", "Austin Show", "Default Show"])
        for paths in (a, b, d):
            for name in ("events.json", "source-cache.json"):
                self.assertTrue(os.path.exists(os.path.join(paths.directory, name)), (paths.area_id, name))
        self.assertEqual(len({p.directory for p in (a, b, d)}), 3)
        # no cache contamination: each area's source cache holds only its own events and its own location key
        for paths, title in ((a, "San Diego Show"), (b, "Austin Show"), (d, "Default Show")):
            with open(paths.source_cache, encoding="utf-8") as handle:
                cache = json.load(handle)
            self.assertEqual([e["title"] for e in cache["s"]["events"]], [title])
        self.assertNotEqual(json.load(open(a.source_cache))["_location_key"], json.load(open(b.source_cache))["_location_key"])

    def test_one_area_failing_leaves_the_others_untouched(self):
        _, a = self.run_area(self.sd, SAN_DIEGO, Local("San Diego Show"))
        _, b = self.run_area(self.tx, AUSTIN, Local("Austin Show"))
        before_a, before_b = open(a.feed, "rb").read(), open(b.feed, "rb").read()
        code, _ = self.run_area(self.tx, AUSTIN, Local("Austin Show", fail=True))
        self.assertEqual(code, 1)
        self.assertEqual(open(b.feed, "rb").read(), before_b)  # its own previous feed is kept
        self.assertEqual(open(a.feed, "rb").read(), before_a)  # the other area is not even touched
        code, _ = self.run_area(self.sd, SAN_DIEGO, Local("San Diego Show 2"))
        self.assertEqual((code, self.feed(a)["events"][0]["title"], open(b.feed, "rb").read()), (0, "San Diego Show 2", before_b))

    def test_locks_are_per_area(self):
        a, b = self.store.ensure(self.sd, SAN_DIEGO), self.store.ensure(self.tx, AUSTIN)
        held = refresh_module.Lock(a.lock).acquire()
        try:
            self.assertEqual(self.run_area(self.sd, SAN_DIEGO)[0], 1)  # this area is busy
            self.assertEqual(self.run_area(self.tx, AUSTIN)[0], 0)  # another area is not blocked
        finally:
            held.release()
        self.assertNotEqual(a.lock, b.lock)

    def test_the_same_place_always_reuses_the_same_storage(self):
        self.run_area(self.sd, SAN_DIEGO)
        again = AreaStore(self.store.root).paths(derive_area_id({**SAN_DIEGO, "latitude": 32.71570001}))
        self.assertTrue(os.path.exists(again.feed))

    def test_geocoding_cache_is_per_area(self):
        class Provider:
            name = "nominatim"
            calls = 0

            def lookup(self, fetcher, spec):
                type(self).calls += 1
                return {"display_name": "Austin", "latitude": 30.2672, "longitude": -97.7431, "country": "US", "region": "TX"}

        a, b = self.store.ensure(self.sd, SAN_DIEGO), self.store.ensure(self.tx, AUSTIN)
        from discovery.location import resolve_location
        for paths in (a, b, b):
            resolve_location({"query": "Austin, TX", "timezone": "America/Chicago"}, None, paths.location_cache, {"nominatim": Provider()})
        self.assertEqual(Provider.calls, 2)  # once per area; repeats within an area come from its own cache
        self.assertTrue(os.path.exists(a.location_cache) and os.path.exists(b.location_cache))

    def test_location_change_inside_one_area_directory_cannot_mix_data(self):
        _, a = self.run_area(self.sd, SAN_DIEGO, Local("San Diego Show"))
        with self.assertRaises(AreaError):
            self.run_area(self.sd, AUSTIN, Local("Austin Show"))  # a different place cannot be written into this area
        self.assertEqual(self.feed(a)["events"][0]["title"], "San Diego Show")


class AtomicWrites(unittest.TestCase):
    def setUp(self):
        self.store = AreaStore(tempfile.mkdtemp())
        self.paths = self.store.ensure(derive_area_id(AUSTIN), AUSTIN)

    def test_status_round_trip_and_atomic_failure(self):
        save_status(self.paths, {"last_exit_code": 0, "last_success_at": "2030-01-01T00:00:00+00:00"})
        self.assertEqual(load_status(self.paths)["last_exit_code"], 0)
        with mock.patch("discovery.publish.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                save_status(self.paths, {"last_exit_code": 1})
        self.assertEqual(load_status(self.paths)["last_exit_code"], 0)  # previous status survives
        self.assertEqual(sorted(os.listdir(self.paths.directory)), [AREA_META, "status.json"])  # and no temp file is left

    def test_failed_publish_keeps_the_previous_feed(self):
        adapter = Local("Before")
        with mock.patch.dict(sources_module.ADAPTERS, {"local": adapter}):
            args = self.paths.refresh_arguments()
            self.assertEqual(refresh_module.refresh(config_for(AUSTIN), {}, fetcher=object(), **args), 0)
            before = open(self.paths.feed, "rb").read()
            adapter.title = "After"
            with mock.patch("discovery.core.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    refresh_module.refresh(config_for(AUSTIN), {}, fetcher=object(), **args)
        self.assertEqual(open(self.paths.feed, "rb").read(), before)

    def test_missing_or_corrupt_status_is_just_empty(self):
        self.assertEqual(load_status(self.paths), {})
        with open(self.paths.status, "w", encoding="utf-8") as handle:
            handle.write("[1, 2")
        self.assertEqual(load_status(self.paths), {})


if __name__ == "__main__":
    unittest.main()
