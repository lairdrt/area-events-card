"""Authenticated multi-area API: authentication, validation, quotas, scheduling, isolation and an emulated HA bridge."""
import http.client
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest import mock

from discovery import sources as sources_module
from discovery.api import MIN_KEY_LENGTH, Api, load_api_keys
from discovery.areamgr import DAY, ApiError, AreaManager
from discovery.core import FetchError, finish_event, make_event
from discovery.service import Service, make_server

KEY = "k" * 40
OTHER_KEY = "o" * 40
PDT = timezone(timedelta(hours=-7))
SAN_DIEGO = {"latitude": 32.7157, "longitude": -117.1611, "radius_miles": 25}
AUSTIN = {"latitude": 30.2672, "longitude": -97.7431, "radius_miles": 25}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Local:
    """A regional source: events sit at its own coordinates and carry its own title."""
    ACCEPTS_LOCATION = True

    def __init__(self, title, latitude, longitude):
        self.title, self.latitude, self.longitude, self.fail, self.calls, self.active, self.peak = title, latitude, longitude, False, 0, 0, 0
        self._guard = threading.Lock()

    def discover(self, fetcher, previous, source_id="x", source_name="X", url=None, location=None, **_):
        with self._guard:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            self.calls += 1
            time.sleep(0.01)
            if self.fail:
                raise FetchError("site down")
            start = datetime.now(PDT) + timedelta(hours=24)
            event = make_event(source_id, source_name, url, f"{url}/{self.title}", self.title, start, local_id=self.title, venue="Hall",
                               address="1 Main St")
            event["geo"] = {"latitude": self.latitude, "longitude": self.longitude, "source": "test"}
            return [finish_event(event, "2030-01-01T00:00:00-08:00", ["e"], False)], {}
        finally:
            with self._guard:
                self.active -= 1


def registry_config():
    def area(name, lat, lon):
        return {"type": "area", "name": name, "latitude": lat, "longitude": lon, "radius_miles": 40}
    return {
        "location": {"latitude": 32.7157, "longitude": -117.1611, "timezone": "America/Los_Angeles", "radius_miles": 25},
        "sources": [
            {"id": "sd", "name": "SD Source", "adapter": "sd_adapter", "url": "https://sd.example", "coverage": area("San Diego", 32.7157, -117.1611)},
            {"id": "tx", "name": "TX Source", "adapter": "tx_adapter", "url": "https://tx.example", "coverage": area("Austin", 30.2672, -97.7431)},
        ],
    }


class Provider:
    name = "nominatim"

    def __init__(self):
        self.calls = 0

    def lookup(self, fetcher, spec):
        self.calls += 1
        places = {"austin, tx": (30.2672, -97.7431, "TX"), "san diego, ca": (32.7157, -117.1611, "CA")}
        hit = places.get(spec.get("query", "").lower())
        return {"display_name": spec["query"], "latitude": hit[0], "longitude": hit[1], "country": "US", "region": hit[2]} if hit else None


class ZipProvider(Provider):
    name = "zippopotam"

    def lookup(self, fetcher, spec):
        self.calls += 1
        return {"display_name": f"Austin, TX {spec['zip']}", "latitude": 30.2672, "longitude": -97.7431, "country": "US", "region": "TX"} if spec["zip"] == "78701" else None


class Base(unittest.TestCase):
    limits = {}

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.clock = Clock()
        self.sd, self.tx = Local("San Diego Show", 32.72, -117.16), Local("Austin Show", 30.27, -97.74)
        patcher = mock.patch.dict(sources_module.ADAPTERS, {"sd_adapter": self.sd, "tx_adapter": self.tx})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.nominatim, self.zippo = Provider(), ZipProvider()
        self.manager = AreaManager(self.folder, registry_config, clock=self.clock, fetcher=object(), limits=self.limits,
                                   providers={"nominatim": self.nominatim, "zippopotam": self.zippo})

    def run_all(self):
        ran = []
        while True:
            area = self.manager.run_next()
            if not area:
                return ran
            ran.append(area)


class Lifecycle(Base):
    def test_create_by_coordinates_city_and_zip(self):
        area, created = self.manager.ensure_area({**SAN_DIEGO, "name": "Home"})
        self.assertTrue(created)
        self.assertEqual((area["names"], area["status"]["state"], self.nominatim.calls), (["Home"], "queued", 0))  # coordinates: no geocoding
        city, _ = self.manager.ensure_area({"query": "Austin, TX", "radius_miles": 25, "name": "Austin"})
        again, created = self.manager.ensure_area({"query": "Austin, TX", "radius_miles": 25})
        self.assertEqual((city["id"], created, self.nominatim.calls), (again["id"], False, 1))  # same place, one lookup
        zipped, created = self.manager.ensure_area({"zip": "78701", "country": "US", "radius_miles": 25})
        self.assertEqual((zipped["id"], created, self.zippo.calls), (city["id"], False, 1))  # ZIP resolves to the same area
        self.assertRegex(city["id"], r"^a_[0-9a-f]{12}$")
        self.assertNotEqual(city["id"], area["id"])

    def test_names_are_unique_labels(self):
        first, _ = self.manager.ensure_area({**SAN_DIEGO, "name": "Home"})
        with self.assertRaises(ApiError) as raised:
            self.manager.ensure_area({**AUSTIN, "name": "Home"})
        self.assertEqual(raised.exception.status, 409)
        renamed, _ = self.manager.ensure_area({**SAN_DIEGO, "name": "Beach"})
        self.assertEqual((renamed["id"], renamed["names"]), (first["id"], ["Home", "Beach"]))
        for number in range(10):
            self.manager.ensure_area({**SAN_DIEGO, "name": f"Alias {number}"})
        self.assertEqual(len(self.manager.describe(first["id"])["names"]), 5)

    def test_definitions_survive_a_restart(self):
        area, _ = self.manager.ensure_area({**AUSTIN, "name": "Austin"})
        reopened = AreaManager(self.folder, registry_config, clock=self.clock, fetcher=object())
        self.assertEqual([a["id"] for a in reopened.list_areas()], [area["id"]])

    def test_unreadable_or_tampered_definitions_are_dropped_not_trusted(self):
        area, _ = self.manager.ensure_area(AUSTIN)
        path = os.path.join(self.folder, "areas.json")
        data = json.load(open(path, encoding="utf-8"))
        data["areas"]["a_000000000000"] = data["areas"][area["id"]]  # id that does not match its location
        data["areas"]["../evil"] = data["areas"][area["id"]]
        json.dump(data, open(path, "w", encoding="utf-8"))
        self.assertEqual([a["id"] for a in AreaManager(self.folder, registry_config, clock=self.clock, fetcher=object()).list_areas()], [area["id"]])

    def test_delete_removes_only_that_area(self):
        a, _ = self.manager.ensure_area(SAN_DIEGO)
        b, _ = self.manager.ensure_area(AUSTIN)
        self.run_all()
        self.manager.delete_area(a["id"])
        self.assertFalse(os.path.exists(os.path.join(self.folder, "areas", a["id"])))
        self.assertTrue(os.path.exists(os.path.join(self.folder, "areas", b["id"], "events.json")))
        with self.assertRaises(ApiError):
            self.manager.describe(a["id"])


class Quotas(Base):
    limits = {"max_areas": 2, "creations_per_hour": 3, "geocodes_per_hour": 2, "manual_refresh_min_seconds": 900}

    def test_area_limit(self):
        self.manager.ensure_area(SAN_DIEGO)
        self.manager.ensure_area(AUSTIN)
        with self.assertRaises(ApiError) as raised:
            self.manager.ensure_area({"latitude": 40.0, "longitude": -100.0, "radius_miles": 10})
        self.assertEqual((raised.exception.status, raised.exception.code), (429, "area_limit"))
        self.manager.ensure_area(AUSTIN)  # re-selecting an existing area is always allowed

    def test_creation_rate_limit_resets_after_an_hour(self):
        self.manager.limits["max_areas"] = 10
        for number in range(3):
            self.manager.ensure_area({"latitude": 10.0 + number, "longitude": 20.0, "radius_miles": 10})
        with self.assertRaises(ApiError) as raised:
            self.manager.ensure_area({"latitude": 20.0, "longitude": 20.0, "radius_miles": 10})
        self.assertEqual((raised.exception.status, raised.exception.code), (429, "creation_rate"))
        self.assertIn("Retry-After", raised.exception.headers)
        self.clock.advance(3601)
        self.manager.ensure_area({"latitude": 20.0, "longitude": 20.0, "radius_miles": 10})

    def test_geocoding_budget_counts_only_uncached_lookups(self):
        self.manager.limits["max_areas"] = 10
        self.manager.ensure_area({"query": "Austin, TX", "radius_miles": 25})
        self.manager.ensure_area({"query": "Austin, TX", "radius_miles": 25})  # cached
        self.manager.ensure_area({"query": "San Diego, CA", "radius_miles": 25})
        with self.assertRaises(ApiError) as raised:
            self.manager.ensure_area({"query": "Austin, TX", "radius_miles": 30})  # same city, but the cache is keyed by place so this is cached too
            self.manager.ensure_area({"query": "Elsewhere, ZZ", "radius_miles": 25})
        self.assertEqual(raised.exception.status, 422)
        self.assertEqual(self.nominatim.calls, 2)

    def test_manual_refresh_is_rate_limited_per_area(self):
        area, _ = self.manager.ensure_area(AUSTIN)
        other, _ = self.manager.ensure_area(SAN_DIEGO)
        self.run_all()
        self.manager.request_refresh(area["id"])
        with self.assertRaises(ApiError) as raised:
            self.manager.request_refresh(area["id"])
        self.assertEqual(raised.exception.status, 429)
        self.assertGreater(int(raised.exception.headers["Retry-After"]), 0)
        self.manager.request_refresh(other["id"])  # another area is not affected
        self.clock.advance(901)
        self.manager.request_refresh(area["id"])

    def test_default_area_cannot_be_deleted_or_refreshed_through_the_manager(self):
        for call in (self.manager.delete_area, self.manager.request_refresh):
            with self.assertRaises(ApiError) as raised:
                call("default")
            self.assertEqual(raised.exception.status, 403)


class Scheduling(Base):
    def test_areas_refresh_independently_and_never_share_events(self):
        a, _ = self.manager.ensure_area({**SAN_DIEGO, "name": "San Diego"})
        b, _ = self.manager.ensure_area({**AUSTIN, "name": "Austin"})
        self.assertCountEqual(self.run_all(), [a["id"], b["id"]])
        titles = {}
        for area in (a, b):
            _, raw = self.manager.read_feed(area["id"])
            titles[area["id"]] = sorted(e["title"] for e in json.loads(raw)["events"])
        self.assertEqual(titles, {a["id"]: ["San Diego Show"], b["id"]: ["Austin Show"]})
        self.assertEqual((self.sd.calls, self.tx.calls), (1, 1))  # each area ran only the source that covers it
        feed = json.loads(self.manager.read_feed(b["id"])[1])
        self.assertEqual(feed["location"]["latitude"], 30.2672)
        self.assertEqual(self.manager.describe(a["id"])["status"]["state"], "ok")

    def test_failure_is_reported_per_area_keeps_the_previous_feed_and_recovers(self):
        a, _ = self.manager.ensure_area(SAN_DIEGO)
        b, _ = self.manager.ensure_area(AUSTIN)
        self.run_all()
        before = {k: self.manager.read_feed(k)[1] for k in (a["id"], b["id"])}
        self.tx.fail = True
        self.manager.request_refresh(b["id"])
        self.assertEqual(self.manager.run_next(), b["id"])
        status = self.manager.describe(b["id"])["status"]
        self.assertEqual((status["state"], status["last_exit_code"]), ("failed", 1))
        self.assertIn("site down", status["last_error"])
        self.assertEqual(self.manager.read_feed(b["id"])[1], before[b["id"]])  # last valid feed still served
        self.assertEqual(self.manager.describe(a["id"])["status"]["state"], "ok")  # the other area is unaffected
        self.assertEqual(self.manager.read_feed(a["id"])[1], before[a["id"]])
        self.tx.fail = False
        self.clock.advance(901)
        self.manager.request_refresh(b["id"])
        self.manager.run_next()
        recovered = self.manager.describe(b["id"])["status"]
        self.assertEqual((recovered["state"], recovered["last_error"]), ("ok", None))

    def test_an_area_no_source_covers_reports_a_clear_error(self):
        area, _ = self.manager.ensure_area({"latitude": -33.87, "longitude": 151.21, "radius_miles": 10})
        self.manager.run_next()
        status = self.manager.describe(area["id"])["status"]
        self.assertEqual(status["state"], "failed")
        self.assertIn("no enabled source applies", status["last_error"])
        self.assertIsNone(self.manager.read_feed(area["id"]))

    def test_active_areas_refresh_hourly_and_inactive_ones_pause_then_are_removed(self):
        area, _ = self.manager.ensure_area(AUSTIN)
        self.run_all()
        self.assertEqual(self.manager.due(), [])
        self.clock.advance(3600 + 601)
        self.assertEqual(self.manager.due(), [area["id"]])  # an hour (plus the area's stagger) later
        self.manager.run_next()
        self.clock.advance(8 * DAY)
        self.assertEqual(self.manager.due(), [])  # nobody read it for 7+ days: paused
        self.assertEqual(self.manager.describe(area["id"])["status"]["paused"], True)
        self.manager.read_feed(area["id"])  # a read makes it active again
        self.assertEqual(self.manager.due(), [area["id"]])
        self.manager.run_next()
        self.clock.advance(31 * DAY)
        self.assertEqual(self.manager.cleanup(), [area["id"]])
        self.assertEqual(self.manager.list_areas(), [])
        self.assertFalse(os.path.exists(os.path.join(self.folder, "areas", area["id"])))

    def test_only_one_discovery_job_runs_at_a_time(self):
        a, _ = self.manager.ensure_area(SAN_DIEGO)
        b, _ = self.manager.ensure_area(AUSTIN)
        threads = [threading.Thread(target=self.manager.run_area, args=(k,)) for k in (a["id"], b["id"])]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(max(self.sd.peak, self.tx.peak), 1)
        shared = {"active": 0, "peak": 0}
        guard = threading.Lock()

        def slow(config, options, **paths):
            with guard:
                shared["active"] += 1
                shared["peak"] = max(shared["peak"], shared["active"])
            time.sleep(0.05)
            with guard:
                shared["active"] -= 1
            return 0

        slot = threading.Lock()
        manager = AreaManager(self.folder, registry_config, refresh_fn=slow, clock=self.clock, fetcher=object(), slot=slot)
        service = Service(tempfile.mkdtemp(), refresh_fn=slow, slot=slot, config_path=os.path.join(os.path.dirname(__file__), "..", "discovery.config.json"))
        workers = [threading.Thread(target=manager.run_area, args=(k,)) for k in (a["id"], b["id"])] + [threading.Thread(target=service.run_once)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(shared["peak"], 1)  # the default area's job and the area jobs share one slot

    def test_a_crashing_refresh_is_contained(self):
        def boom(*a, **k):
            raise RuntimeError("bug")

        manager = AreaManager(self.folder, registry_config, refresh_fn=boom, clock=self.clock, fetcher=object())
        area, _ = manager.ensure_area(AUSTIN)
        self.assertEqual(manager.run_area(area["id"]), 1)
        self.assertIn("bug", manager.describe(area["id"])["status"]["last_error"])


class Http(Base):
    """A real HTTP server on a loopback port; Api time follows the fake clock."""

    def setUp(self):
        super().setUp()
        self.default_dir = tempfile.mkdtemp()
        self.feed = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sources": {},
                     "events": [{"id": "d:1", "title": "Default Show", "start": "2030-01-01T00:00:00-08:00", "url": "https://d.example"}]}
        with open(os.path.join(self.default_dir, "events.json"), "w", encoding="utf-8") as handle:
            json.dump(self.feed, handle)
        self.service = Service(self.default_dir, refresh_fn=lambda *a, **k: 0, config_path=os.path.join(os.path.dirname(__file__), "..", "discovery.config.json"))
        self.api = Api(self.manager, [KEY], self.service, clock=self.clock)
        self.server = make_server(self.service, "127.0.0.1", 0, self.api)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.port = self.server.server_address[1]

    def call(self, method, path, body=None, key=KEY, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        send = dict(headers or {})
        if key is not None:
            send["Authorization"] = f"Bearer {key}"
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            send["Content-Type"] = "application/json"
        connection.request(method, path, data, send)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, {k.lower(): v for k, v in response.getheaders()}, raw)
        connection.close()
        return result

    def json(self, *args, **kwargs):
        status, headers, raw = self.call(*args, **kwargs)
        return status, headers, json.loads(raw) if raw else None


class Authentication(Http):
    ROUTES = [("GET", "/api/v1/health"), ("GET", "/api/v1/areas"), ("POST", "/api/v1/areas"), ("GET", "/api/v1/areas/default"),
              ("GET", "/api/v1/areas/default/events"), ("DELETE", "/api/v1/areas/default"), ("POST", "/api/v1/areas/default/refresh")]

    def test_every_management_route_requires_the_key(self):
        for method, path in self.ROUTES:
            for key in (None, "wrong" * 10, KEY[:-1], KEY + "x", ""):
                with self.subTest(method=method, path=path, key=key):
                    self.clock.advance(61)  # stay below the failed-attempt throttle (tested separately)
                    status, headers, body = self.json(method, path, {} if method == "POST" else None, key=key)
                    self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))
                    self.assertEqual(headers["www-authenticate"], "Bearer")
        for header in ("Basic " + KEY, "Bearer", KEY, "Token " + KEY):
            self.clock.advance(61)
            self.assertEqual(self.call("GET", "/api/v1/areas", key=None, headers={"Authorization": header})[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/areas", key=KEY)[0], 200)
        self.assertEqual(self.call("GET", "/api/v1/areas", key=None, headers={"Authorization": f"bearer {KEY}"})[0], 200)

    def test_failed_attempts_are_throttled_per_address_and_recover(self):
        for _ in range(5):
            self.assertEqual(self.call("GET", "/api/v1/areas", key="x" * 40)[0], 401)
        status, headers, _ = self.call("GET", "/api/v1/areas", key=KEY)  # even the right key is refused during the block
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["retry-after"]), 0)
        self.clock.advance(61)
        self.assertEqual(self.call("GET", "/api/v1/areas", key=KEY)[0], 200)

    def test_two_keys_support_rotation(self):
        self.api.keys = [KEY.encode(), OTHER_KEY.encode()]
        self.assertEqual([self.call("GET", "/api/v1/areas", key=k)[0] for k in (KEY, OTHER_KEY)], [200, 200])

    def test_without_a_key_the_api_does_not_exist_but_the_public_feed_still_works(self):
        self.server.api = None
        for method, path in self.ROUTES:
            self.assertEqual(self.call(method, path, {} if method == "POST" else None, key=KEY)[0], 404)
        self.assertEqual(self.call("GET", "/events.json", key=None)[0], 200)
        disabled = Api(self.manager, [], self.service)
        self.assertEqual(disabled.handle("GET", "/api/v1/areas", {"Authorization": f"Bearer {KEY}"}, b"", "1.2.3.4")[0], 404)

    def test_no_cors_on_the_api_and_the_public_feed_is_unchanged(self):
        status, headers, _ = self.call("GET", "/api/v1/areas")
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(self.call("OPTIONS", "/api/v1/areas", key=None)[0], 404)
        _, public, body = self.call("GET", "/events.json", key=None)
        self.assertEqual(public["access-control-allow-origin"], "*")
        self.assertEqual(json.loads(body), self.feed)
        self.assertEqual(self.call("POST", "/events.json", {}, key=KEY)[0], 405)  # still read-only outside /api/v1
        self.assertEqual(self.call("GET", "/health", key=None)[0], 200)

    def test_key_loading_from_environment_and_files(self):
        self.assertEqual(load_api_keys({}), [])
        self.assertEqual(load_api_keys({"AREA_EVENTS_API_KEY": KEY}), [KEY])
        path = os.path.join(self.folder, "key")
        open(path, "w").write(KEY + "\n")
        self.assertEqual(load_api_keys({"AREA_EVENTS_API_KEY_FILE": path, "AREA_EVENTS_API_KEY_2": OTHER_KEY}), [KEY, OTHER_KEY])
        open(path, "w").write("")
        self.assertEqual(load_api_keys({"AREA_EVENTS_API_KEY_FILE": path}), [])  # an empty secret file leaves the API off
        self.assertEqual(load_api_keys({"AREA_EVENTS_API_KEY_FILE": os.path.join(self.folder, "missing")}), [])
        with self.assertRaises(ValueError):
            load_api_keys({"AREA_EVENTS_API_KEY": "short"})
        self.assertGreaterEqual(MIN_KEY_LENGTH, 32)

    def test_key_comparison_is_constant_time(self):
        with mock.patch("discovery.api.hmac.compare_digest", wraps=__import__("hmac").compare_digest) as compare:
            self.call("GET", "/api/v1/areas", key="x" * 40)
        self.assertTrue(compare.called)


class Validation(Http):
    def test_only_geographic_fields_are_accepted(self):
        bad_bodies = [
            {"url": "http://evil.example", "latitude": 1, "longitude": 2}, {"adapter": "kpbs", **AUSTIN}, {"source": "x", **AUSTIN},
            {"path": "../../etc", **AUSTIN}, {"provider": "nominatim", "query": "Austin"}, {"sources": ["a"], **AUSTIN}, {"enabled": True, **AUSTIN},
            {"query": "Austin", "zip": "78701", "country": "US"}, {"latitude": 95, "longitude": 0, "radius_miles": 5},
            {"latitude": 1, "longitude": 2, "radius_miles": 0}, {"latitude": 1, "longitude": 2, "radius_miles": 101},
            {"latitude": 1, "longitude": 2, "radius_miles": True}, {"latitude": 1, "longitude": 2, "radius_miles": "5"},
            {"latitude": 1, "longitude": 2, "name": "x" * 61}, {"latitude": 1, "longitude": 2, "name": ""}, {"latitude": 1, "longitude": 2, "name": 5},
            {"latitude": 1, "longitude": 2, "name": "bad\x00name"}, {"query": 5}, {"zip": "78701"}, {"latitude": 1, "longitude": 2, "timezone": "Mars"},
            {}, [], "text", 5, None,
        ]
        for body in bad_bodies:
            with self.subTest(body=body):
                status, _, payload = self.json("POST", "/api/v1/areas", body)
                self.assertIn(status, (400, 422), payload)
        self.assertEqual(self.manager.list_areas(), [])  # nothing was created by any of them

    def test_bad_json_and_oversized_or_unframed_bodies(self):
        self.assertEqual(self.json("POST", "/api/v1/areas", b"{not json")[0], 400)
        self.assertEqual(self.json("POST", "/api/v1/areas", b"\xff\xfe")[0], 400)
        self.assertEqual(self.json("POST", "/api/v1/areas", b" " * 5000)[0], 413)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest("POST", "/api/v1/areas")
        connection.putheader("Authorization", f"Bearer {KEY}")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders(b"0\r\n\r\n")
        self.assertEqual(connection.getresponse().status, 411)

    def test_ids_in_paths_are_validated_and_methods_checked(self):
        for path in ("/api/v1/areas/..", "/api/v1/areas/a_zzzzzzzzzzzz", "/api/v1/areas/a_000000000000", "/api/v1/areas/%2e%2e%2f%2e%2e",
                     "/api/v1/areas/a_000000000000/../../x", "/api/v1/areas//events", "/api/v1/nope", "/api/v1/areas/x/y/z"):
            self.assertEqual(self.call("GET", path)[0], 404, path)
        status, headers, _ = self.call("PUT", "/api/v1/areas", {})
        self.assertIn(status, (405,))  # PUT is not routed at all
        status, headers, _ = self.call("DELETE", "/api/v1/areas")
        self.assertEqual((status, headers.get("allow")), (405, "GET, POST"))
        self.assertEqual(self.call("GET", "/api/v1/areas/default/refresh")[0], 405)

    def test_the_default_area_is_visible_but_read_only(self):
        status, _, listing = self.json("GET", "/api/v1/areas")
        self.assertEqual([a["id"] for a in listing["areas"]], ["default"])
        self.assertEqual(self.call("GET", "/api/v1/areas/default/events")[2], open(os.path.join(self.default_dir, "events.json"), "rb").read())
        for method, path in (("DELETE", "/api/v1/areas/default"), ("POST", "/api/v1/areas/default/refresh")):
            self.assertEqual(self.call(method, path)[0], 403)


class EmulatedHomeAssistant(Http):
    """Emulates the Home Assistant scripts (validate the id, add the secret, call the API) and drives two cards."""

    ID = re.compile(r"^(default|a_[0-9a-f]{12})$")

    def script(self, name, data=None):
        data = data or {}
        if name == "area_events_list":
            return self.json("GET", "/api/v1/areas")
        if name == "area_events_ensure":
            return self.json("POST", "/api/v1/areas", data["spec"])
        if not self.ID.match(str(data.get("area_id", ""))):
            return 400, {}, {"error": "invalid area id"}  # the script refuses before calling the rest_command
        if name == "area_events_get":
            return self.json("GET", f"/api/v1/areas/{data['area_id']}")
        if name == "area_events_feed":
            return self.json("GET", f"/api/v1/areas/{data['area_id']}/events")
        if name == "area_events_refresh":
            return self.json("POST", f"/api/v1/areas/{data['area_id']}/refresh")
        raise AssertionError(name)

    def wait_ready(self, area_id):
        for _ in range(5):
            status, _, feed = self.script("area_events_feed", {"area_id": area_id})
            if status == 200:
                return feed
            self.manager.run_next()
        self.fail("no feed")

    def test_two_cards_select_different_areas_at_once_and_switch_without_leaking(self):
        before_default = self.call("GET", "/events.json", key=None)[2]
        # card 1 (San Diego, coordinates from the HA home location) and card 2 (Austin, typed as a city) choose their areas
        status, _, sd = self.script("area_events_ensure", {"spec": {**SAN_DIEGO, "name": "Home", "timezone": "America/Los_Angeles"}})
        status2, _, tx = self.script("area_events_ensure", {"spec": {"query": "Austin, TX", "radius_miles": 25, "name": "Austin"}})
        self.assertEqual((status, status2), (201, 201))
        self.assertEqual(self.script("area_events_feed", {"area_id": sd["id"]})[0], 503)  # first discovery still queued: honest state
        feeds = {k: self.wait_ready(k) for k in (sd["id"], tx["id"])}
        self.assertEqual([e["title"] for e in feeds[sd["id"]]["events"]], ["San Diego Show"])
        self.assertEqual([e["title"] for e in feeds[tx["id"]]["events"]], ["Austin Show"])
        self.assertEqual(feeds[tx["id"]]["location"]["timezone"], None)  # a typed city gets no assumed timezone
        # card 1 switches to Austin (an existing area), then back: results follow the selection, nothing leaks
        self.assertEqual(self.script("area_events_ensure", {"spec": {"query": "Austin, TX", "radius_miles": 25}})[2]["id"], tx["id"])
        self.assertEqual(self.script("area_events_ensure", {"spec": SAN_DIEGO})[2]["id"], sd["id"])
        self.assertEqual(self.script("area_events_feed", {"area_id": sd["id"]})[2], feeds[sd["id"]])
        # the default area is untouched, and a bogus id never reaches the API
        self.assertEqual(self.call("GET", "/events.json", key=None)[2], before_default)
        self.assertEqual(self.script("area_events_feed", {"area_id": "../x"})[0], 400)
        listed = self.script("area_events_list")[2]["areas"]
        self.assertEqual({a["id"] for a in listed}, {"default", sd["id"], tx["id"]})

    def test_etag_allows_unchanged_responses_and_a_card_with_no_key_cannot_reach_the_api(self):
        area = self.script("area_events_ensure", {"spec": AUSTIN})[2]
        self.wait_ready(area["id"])
        status, headers, raw = self.call("GET", f"/api/v1/areas/{area['id']}/events")
        self.assertEqual(self.call("GET", f"/api/v1/areas/{area['id']}/events", headers={"If-None-Match": headers["etag"]})[0], 200)  # conditional GET is advisory; body is small
        self.assertEqual(self.call("GET", f"/api/v1/areas/{area['id']}/events", key=None)[0], 401)  # a browser has no key and gets nothing
        self.assertTrue(headers["etag"].startswith('"'))

    def test_failures_show_up_in_the_area_status_the_card_displays(self):
        area = self.script("area_events_ensure", {"spec": AUSTIN})[2]
        self.tx.fail = True
        self.manager.run_next()
        status = self.script("area_events_get", {"area_id": area["id"]})[2]["status"]
        self.assertEqual((status["state"], "site down" in status["last_error"]), ("failed", True))
        self.assertEqual(self.script("area_events_feed", {"area_id": area["id"]})[0], 503)


if __name__ == "__main__":
    unittest.main()
