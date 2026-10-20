"""Changing the one active location: validation, safe switching, failure handling, persistence and the authenticated API."""
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from discovery import locationctl
from discovery import refresh as refresh_module
from discovery import sources as sources_module
from discovery.api import MIN_KEY_LENGTH, Api, load_api_keys
from discovery.core import FetchError, finish_event, make_event
from discovery.locationctl import ACTIVE_FILE, ApiError, LocationController, effective_config
from discovery.service import Service, make_server

KEY, OTHER_KEY = "k" * 40, "o" * 40
PDT = timezone(timedelta(hours=-7))
SAN_DIEGO = {"latitude": 32.7157, "longitude": -117.1611, "radius_miles": 25, "name": "San Diego, CA", "timezone": "America/Los_Angeles"}
AUSTIN = {"latitude": 30.2672, "longitude": -97.7431, "radius_miles": 25, "name": "Austin, TX"}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Regional:
    """A source whose events are always at its own coordinates and carry its own title."""
    ACCEPTS_LOCATION = True

    def __init__(self, title, latitude, longitude):
        self.title, self.latitude, self.longitude, self.fail, self.calls, self.hook = title, latitude, longitude, False, 0, None

    def discover(self, fetcher, previous, source_id="x", source_name="X", url=None, location=None, **_):
        self.calls += 1
        if self.hook:
            self.hook()
        if self.fail:
            raise FetchError("site down")
        start = datetime.now(PDT) + timedelta(hours=24)
        event = make_event(source_id, source_name, url, f"{url}/{self.title}", self.title, start, local_id=self.title, venue="Hall", address="1 Main St")
        event["geo"] = {"latitude": self.latitude, "longitude": self.longitude, "source": "test"}
        return [finish_event(event, "2030-01-01T00:00:00-08:00", ["e"], False)], {}


def registry_config():
    def area(name, lat, lon):
        return {"type": "area", "name": name, "latitude": lat, "longitude": lon, "radius_miles": 40}
    return {
        "location": SAN_DIEGO,
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


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.clock = Clock()
        self.sd, self.tx = Regional("San Diego Show", 32.72, -117.16), Regional("Austin Show", 30.27, -97.74)
        patcher = mock.patch.dict(sources_module.ADAPTERS, {"sd_adapter": self.sd, "tx_adapter": self.tx})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = Provider()
        self.controller = self.new_controller()
        self.run_default_refresh()  # the baseline: what the service publishes before anyone changes anything

    def new_controller(self, **kwargs):
        return LocationController(self.folder, registry_config, clock=self.clock, fetcher=object(), providers={"nominatim": self.provider}, **kwargs)

    def path(self, name):
        return os.path.join(self.folder, name)

    def run_default_refresh(self):
        config = effective_config(registry_config(), self.folder)
        return refresh_module.refresh(config, {}, fetcher=object(), output=self.path("events.json"), cache_path=self.path("source-cache.json"),
                                      lock_path=self.path(".refresh.lock"), location_cache_path=self.path("location-cache.json"))

    def feed(self):
        with open(self.path("events.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def feed_bytes(self):
        with open(self.path("events.json"), "rb") as handle:
            return handle.read()

    def titles(self):
        return sorted(e["title"] for e in self.feed()["events"])

    def change(self, body):
        status, payload = self.controller.request_change(body)
        if status == 202:
            self.assertTrue(self.controller.run_pending())
        return status, payload

    def outcome(self):
        return self.controller.status()["change"]

    def assert_consistent(self, place):
        """Every event and the label belong to `place`: nothing from another location is published under its name."""
        feed = self.feed()
        self.assertEqual(feed["location"]["display_name"], place["name"])
        for event in feed["events"]:
            self.assertEqual(event["place"]["classification"], "confirmed")
            self.assertTrue(event["place"]["within_radius"], event["title"])


class Switching(Base):
    def test_san_diego_baseline_is_unchanged_until_someone_changes_it(self):
        self.assertIs(effective_config(registry_config(), self.folder)["location"], SAN_DIEGO)
        self.assertEqual(self.titles(), ["San Diego Show"])
        self.assert_consistent(SAN_DIEGO)
        status = self.controller.status()
        self.assertEqual((status["active"]["source"], status["active"]["location"]["name"], status["change"]["state"]), ("configuration", "San Diego, CA", "idle"))
        self.assertEqual(status["feed"]["location"], "San Diego, CA")

    def test_san_diego_to_austin_and_back(self):
        old = self.feed_bytes()
        status, payload = self.change(AUSTIN)
        self.assertEqual((status, payload["change"]["state"]), (202, "queued"))
        self.assertEqual(self.outcome()["state"], "succeeded")
        self.assertEqual(self.titles(), ["Austin Show"])
        self.assert_consistent(AUSTIN)
        self.assertEqual(self.controller.status()["active"]["source"], "changed")
        self.assertEqual(self.sd.calls, 1)  # the San Diego source only ran for the baseline: it does not cover Austin
        self.clock.advance(301)
        self.assertEqual(self.change(SAN_DIEGO)[0], 202)
        self.assertEqual(self.titles(), ["San Diego Show"])
        self.assert_consistent(SAN_DIEGO)
        self.assertEqual([e["title"] for e in self.feed()["events"]], [e["title"] for e in json.loads(old)["events"]])

    def test_hourly_refresh_follows_the_new_place_and_keeps_labels_right(self):
        self.change(AUSTIN)
        self.tx.calls = 0
        self.assertEqual(self.run_default_refresh(), 0)  # the next scheduled refresh, as the service runs it
        self.assertEqual((self.tx.calls, self.titles()), (1, ["Austin Show"]))
        self.assert_consistent(AUSTIN)
        self.assertEqual(self.sd.calls, 1)

    def test_city_names_are_resolved_once_and_cached(self):
        self.change({"query": "Austin, TX", "radius_miles": 25})
        self.assertEqual((self.titles(), self.provider.calls), (["Austin Show"], 1))
        self.assertEqual(self.feed()["location"]["display_name"], "Austin, TX")
        self.clock.advance(301)
        self.change({"query": "Austin, TX", "radius_miles": 30})
        self.assertEqual(self.provider.calls, 1)  # same city: the geocoder cache answered

    def test_the_old_feed_is_served_unchanged_while_the_new_place_is_being_discovered(self):
        old, seen = self.feed_bytes(), []
        self.tx.hook = lambda: seen.append(self.feed_bytes())
        self.change(AUSTIN)
        self.assertEqual(seen, [old])  # the feed was still the old one while Austin discovery ran
        self.assertNotEqual(self.feed_bytes(), old)

    def test_the_new_place_survives_a_restart(self):
        self.change(AUSTIN)
        configs = []
        service = Service(self.folder, refresh_fn=lambda config, options, **paths: configs.append(config["location"]) or 0,
                          config_path=os.path.join(os.path.dirname(__file__), "..", "discovery.config.json"))
        service.run_once()
        self.assertEqual((configs[0]["name"], configs[0]["latitude"]), ("Austin, TX", 30.2672))
        self.assertEqual(self.new_controller().status()["active"]["source"], "changed")
        os.remove(self.path(ACTIVE_FILE))  # deleting the saved place returns to the configured one
        self.assertIs(effective_config(registry_config(), self.folder)["location"], SAN_DIEGO)

    def test_a_damaged_saved_place_is_ignored(self):
        for text in ("{broken", "[]", json.dumps({"location": {"latitude": 99}}), json.dumps({"location": None})):
            with open(self.path(ACTIVE_FILE), "w", encoding="utf-8") as handle:
                handle.write(text)
            self.assertIs(effective_config(registry_config(), self.folder)["location"], SAN_DIEGO)


class Rejections(Base):
    def test_invalid_locations_are_rejected_before_anything_changes(self):
        before = self.feed_bytes()
        bad = [{"latitude": 95, "longitude": 0}, {"latitude": 1}, {"query": "Nowhereville"}, {"query": " "}, {"query": "Austin, TX", "zip": "78701", "country": "US"},
               {"latitude": 1, "longitude": 2, "radius_miles": 0}, {"latitude": 1, "longitude": 2, "radius_miles": 101}, {"latitude": 1, "longitude": 2, "radius_miles": True},
               {"latitude": 1, "longitude": 2, "name": "x" * 61}, {"latitude": 1, "longitude": 2, "timezone": "Mars"}, {"zip": "78701"}, {}, [], "text", None,
               {"url": "http://evil.example", **AUSTIN}, {"adapter": "kpbs", **AUSTIN}, {"source": "x", **AUSTIN}, {"path": "../x", **AUSTIN},
               {"provider": "nominatim", "query": "Austin, TX"}, {"sources": ["a"], **AUSTIN}]
        for body in bad:
            with self.subTest(body=body):
                with self.assertRaises(ApiError) as raised:
                    self.controller.request_change(body)
                self.assertIn(raised.exception.status, (422,))
        self.assertEqual(self.controller.status()["change"]["state"], "idle")
        self.assertFalse(os.path.exists(self.path(ACTIVE_FILE)))
        self.assertEqual(self.feed_bytes(), before)

    def test_same_place_is_not_a_change(self):
        status, payload = self.controller.request_change({**SAN_DIEGO, "name": "Home"})
        self.assertEqual((status, payload["change"]["state"]), (200, "unchanged"))
        self.assertEqual(self.controller.status()["change"]["state"], "idle")

    def test_busy_and_rate_limits(self):
        self.assertEqual(self.controller.request_change(AUSTIN)[0], 202)
        with self.assertRaises(ApiError) as raised:
            self.controller.request_change(SAN_DIEGO)
        self.assertEqual(raised.exception.status, 409)
        self.controller.run_pending()
        with self.assertRaises(ApiError) as raised:
            self.controller.request_change(SAN_DIEGO)
        self.assertEqual(raised.exception.status, 429)
        self.assertGreater(int(raised.exception.headers["Retry-After"]), 0)
        self.clock.advance(301)
        self.assertEqual(self.controller.request_change(SAN_DIEGO)[0], 202)

    def test_the_hourly_geocoding_budget_is_enforced(self):
        controller = self.new_controller(limits={"geocodes_per_hour": 1, "min_change_interval_seconds": 0})
        controller.request_change({"query": "Austin, TX", "radius_miles": 25})
        controller.run_pending()
        with self.assertRaises(ApiError) as raised:
            controller.request_change({"query": "San Diego, CA", "radius_miles": 25})
        self.assertIn("geocoding budget", raised.exception.message)
        self.assertEqual(controller.request_change(SAN_DIEGO)[0], 202)  # coordinates need no lookup


class Failures(Base):
    def assert_nothing_changed(self, before, expected_titles=("San Diego Show",)):
        self.assertEqual(self.feed_bytes(), before)
        self.assertEqual(self.titles(), list(expected_titles))
        self.assert_consistent(SAN_DIEGO)
        self.assertFalse(os.path.exists(self.path(ACTIVE_FILE)))
        self.assertFalse(os.path.exists(self.path("staging")))
        self.assertIs(effective_config(registry_config(), self.folder)["location"], SAN_DIEGO)

    def test_unavailable_sources_leave_the_old_feed_and_place(self):
        before = self.feed_bytes()
        self.tx.fail = True
        self.change(AUSTIN)
        outcome = self.outcome()
        self.assertEqual(outcome["state"], "failed")
        self.assertIn("site down", outcome["error"])
        self.assert_nothing_changed(before)
        self.assertEqual(self.run_default_refresh(), 0)  # the hourly refresh still serves San Diego
        self.assertEqual(self.titles(), ["San Diego Show"])

    def test_no_covering_source_is_a_clear_failure(self):
        before = self.feed_bytes()
        self.change({"latitude": -33.87, "longitude": 151.21, "radius_miles": 10, "name": "Sydney"})
        self.assertIn("no enabled source applies", self.outcome()["error"])
        self.assert_nothing_changed(before)

    def test_a_crashing_discovery_is_contained(self):
        before = self.feed_bytes()
        controller = self.new_controller(refresh_fn=mock.Mock(side_effect=RuntimeError("bug")))
        controller.request_change(AUSTIN)
        controller.run_pending()
        self.assertIn("bug", controller.status()["change"]["error"])
        self.assert_nothing_changed(before)

    def test_an_interrupted_change_is_abandoned_cleanly_on_restart(self):
        before = self.feed_bytes()
        self.controller.request_change(AUSTIN)
        os.makedirs(self.path("staging"))  # what a killed run leaves behind
        with open(os.path.join(self.path("staging"), "events.json"), "w", encoding="utf-8") as handle:
            handle.write('{"half": "written"')
        restarted = self.new_controller()
        state = restarted.status()["change"]
        self.assertEqual(state["state"], "failed")
        self.assertIn("interrupted", state["error"])
        self.assertFalse(restarted.run_pending())
        self.assert_nothing_changed(before)

    def test_a_feed_labeled_for_another_place_is_never_published(self):
        before = self.feed_bytes()

        def wrong_label(config, options, **paths):
            code = refresh_module.refresh({**config, "location": SAN_DIEGO}, options, **paths)  # discovers the wrong place
            return code

        controller = self.new_controller(refresh_fn=wrong_label)
        controller.request_change(AUSTIN)
        controller.run_pending()
        self.assertIn("not labeled with the requested location", controller.status()["change"]["error"])
        self.assert_nothing_changed(before)

    def test_an_empty_or_invalid_new_feed_is_not_published(self):
        before = self.feed_bytes()
        for writer in (lambda paths: None, lambda paths: open(paths["output"], "w").write("{not json"),
                       lambda paths: open(paths["output"], "w").write(json.dumps({"generated_at": "x", "events": []}))):
            def fake(config, options, **paths):
                writer(paths)
                return 0

            controller = self.new_controller(refresh_fn=fake, limits={"min_change_interval_seconds": 0})
            controller.request_change(AUSTIN)
            controller.run_pending()
            self.assertEqual(controller.status()["change"]["state"], "failed")
            self.assert_nothing_changed(before)

    def test_failed_publication_restores_the_old_place_and_keeps_the_old_feed(self):
        before = self.feed_bytes()
        real = os.replace

        def fail_on_feed(src, dst, *a, **k):
            if os.path.abspath(dst) == os.path.abspath(self.path("events.json")) and "staging" in src:
                raise OSError("disk full")
            return real(src, dst, *a, **k)

        self.controller.request_change(AUSTIN)
        with mock.patch("discovery.locationctl.os.replace", side_effect=fail_on_feed):
            self.controller.run_pending()
        outcome = self.outcome()
        self.assertEqual(outcome["state"], "failed")
        self.assertIn("could not publish", outcome["error"])
        self.assertEqual(self.feed_bytes(), before)
        self.assertFalse(os.path.exists(self.path(ACTIVE_FILE)))  # the new place was rolled back too
        self.assertEqual(self.run_default_refresh(), 0)  # the cache was already swapped: the next refresh rebuilds it for San Diego
        self.assertEqual(self.titles(), ["San Diego Show"])
        self.assert_consistent(SAN_DIEGO)

    def test_failed_publication_after_an_earlier_change_restores_that_place(self):
        self.change(AUSTIN)
        self.clock.advance(301)
        austin_feed = self.feed_bytes()
        real = locationctl.write_atomic
        failed = []

        def flaky(path, text):
            if os.path.basename(path) == ACTIVE_FILE and not failed:
                failed.append(path)
                raise OSError("read-only file system")
            return real(path, text)

        self.controller.request_change(SAN_DIEGO)
        with mock.patch("discovery.locationctl.write_atomic", side_effect=flaky):
            self.controller.run_pending()
        self.assertEqual(self.outcome()["state"], "failed")
        self.assertEqual(self.feed_bytes(), austin_feed)
        self.assertEqual(effective_config(registry_config(), self.folder)["location"]["name"], "Austin, TX")

    def test_discovery_and_the_hourly_refresh_never_run_at_the_same_time(self):
        shared = {"active": 0, "peak": 0}
        guard = threading.Lock()

        def slow(config, options, **paths):
            with guard:
                shared["active"] += 1
                shared["peak"] = max(shared["peak"], shared["active"])
            time.sleep(0.05)
            with guard:
                shared["active"] -= 1
            return refresh_module.refresh(config, options, **paths)

        slot = threading.Lock()
        controller = self.new_controller(refresh_fn=slow, slot=slot)
        service = Service(self.folder, refresh_fn=slow, slot=slot, config_path=os.path.join(os.path.dirname(__file__), "..", "discovery.config.json"))
        controller.request_change(AUSTIN)
        workers = [threading.Thread(target=controller.run_pending), threading.Thread(target=service.run_once)]
        with mock.patch("discovery.service.effective_config", side_effect=lambda config, folder: effective_config(registry_config(), folder)):
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        self.assertEqual(shared["peak"], 1)


class Http(Base):
    """A real HTTP server on a loopback port; the API's clock follows the fake clock."""

    def setUp(self):
        super().setUp()
        self.service = Service(self.folder, refresh_fn=lambda *a, **k: 0, config_path=os.path.join(os.path.dirname(__file__), "..", "discovery.config.json"))
        self.api = Api(self.controller, [KEY], clock=self.clock)
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
    ROUTES = [("GET", "/api/v1/location"), ("PUT", "/api/v1/location")]

    def test_every_route_requires_the_key_and_unauthenticated_changes_do_nothing(self):
        before = self.feed_bytes()
        for method, path in self.ROUTES:
            for key in (None, "wrong" * 10, KEY[:-1], KEY + "x", ""):
                with self.subTest(method=method, key=key):
                    self.clock.advance(61)  # stay below the failed-attempt throttle (tested separately)
                    status, headers, body = self.json(method, path, AUSTIN if method == "PUT" else None, key=key)
                    self.assertEqual((status, body["error"]["code"], headers["www-authenticate"]), (401, "unauthorized", "Bearer"))
        for header in ("Basic " + KEY, "Bearer", KEY, "Token " + KEY):
            self.clock.advance(61)
            self.assertEqual(self.call("PUT", "/api/v1/location", AUSTIN, key=None, headers={"Authorization": header})[0], 401)
        self.assertEqual(self.controller.status()["change"]["state"], "idle")
        self.assertEqual(self.feed_bytes(), before)
        self.assertEqual(self.call("GET", "/api/v1/location", key=None, headers={"Authorization": f"bearer {KEY}"})[0], 200)

    def test_failed_attempts_are_throttled_and_recover(self):
        for _ in range(5):
            self.assertEqual(self.call("GET", "/api/v1/location", key="x" * 40)[0], 401)
        status, headers, _ = self.call("GET", "/api/v1/location")  # even the right key is refused during the block
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["retry-after"]), 0)
        self.clock.advance(61)
        self.assertEqual(self.call("GET", "/api/v1/location")[0], 200)

    def test_two_keys_support_rotation(self):
        self.api.keys = [KEY.encode(), OTHER_KEY.encode()]
        self.assertEqual([self.call("GET", "/api/v1/location", key=k)[0] for k in (KEY, OTHER_KEY)], [200, 200])

    def test_without_a_key_the_api_does_not_exist_but_the_public_endpoints_do(self):
        self.server.api = None
        for method, path in self.ROUTES:
            self.assertEqual(self.call(method, path, AUSTIN if method == "PUT" else None)[0], 404)
        self.assertEqual(self.call("GET", "/events.json", key=None)[0], 200)
        self.assertEqual(Api(self.controller, []).handle("GET", "/api/v1/location", {"Authorization": f"Bearer {KEY}"}, b"", "1.2.3.4")[0], 404)

    def test_key_loading_from_environment_and_files(self):
        self.assertEqual(load_api_keys({}), [])
        self.assertEqual(load_api_keys({"AREA_EVENTS_API_KEY": KEY}), [KEY])
        path = os.path.join(self.folder, "key")
        open(path, "w").write(KEY + "\n")
        self.assertEqual(load_api_keys({"AREA_EVENTS_API_KEY_FILE": path, "AREA_EVENTS_API_KEY_2": OTHER_KEY}), [KEY, OTHER_KEY])
        open(path, "w").write("")
        self.assertEqual(load_api_keys({"AREA_EVENTS_API_KEY_FILE": path}), [])
        self.assertEqual(load_api_keys({"AREA_EVENTS_API_KEY_FILE": os.path.join(self.folder, "missing")}), [])
        with self.assertRaises(ValueError):
            load_api_keys({"AREA_EVENTS_API_KEY": "short"})
        self.assertGreaterEqual(MIN_KEY_LENGTH, 32)

    def test_key_comparison_is_constant_time(self):
        with mock.patch("discovery.api.hmac.compare_digest", wraps=__import__("hmac").compare_digest) as compare:
            self.call("GET", "/api/v1/location", key="x" * 40)
        self.assertTrue(compare.called)


class Protocol(Http):
    def test_no_cors_on_the_api_and_the_legacy_endpoints_are_unchanged(self):
        self.assertNotIn("access-control-allow-origin", self.call("GET", "/api/v1/location")[1])
        self.assertNotIn("access-control-allow-origin", self.call("GET", "/api/v1/location", key=None)[1])
        self.assertEqual(self.call("OPTIONS", "/api/v1/location", key=None)[0], 404)
        status, headers, body = self.call("GET", "/events.json", key=None)
        self.assertEqual((status, headers["access-control-allow-origin"]), (200, "*"))
        self.assertEqual(json.loads(body), self.feed())
        self.assertEqual(self.call("GET", "/health", key=None)[0], 200)
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.call(method, "/events.json", {}, key=KEY)[0], 405, method)  # everything but the location route stays read-only

    def test_nothing_but_the_location_route_exists(self):
        for method, path in (("GET", "/api/v1/areas"), ("PUT", "/api/v1/areas"), ("GET", "/api/v1/health"), ("GET", "/api/v1/location/x"),
                             ("GET", "/api/v1/locations"), ("GET", "/api/v1/location/../x"), ("DELETE", "/api/v1/location"), ("POST", "/api/v1/location")):
            self.assertIn(self.call(method, path, {} if method in ("PUT", "POST") else None)[0], (404, 405), (method, path))

    def test_malformed_requests(self):
        self.assertEqual(self.json("PUT", "/api/v1/location", b"{not json")[0], 400)
        self.assertEqual(self.json("PUT", "/api/v1/location", b"\xff\xfe")[0], 400)
        self.assertEqual(self.json("PUT", "/api/v1/location", b" " * 5000)[0], 413)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest("PUT", "/api/v1/location")
        connection.putheader("Authorization", f"Bearer {KEY}")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders(b"0\r\n\r\n")
        self.assertEqual(connection.getresponse().status, 411)
        for body in ({"url": "http://evil.example", **AUSTIN}, {"latitude": 95, "longitude": 0}, {"query": "Nowhereville"}):
            self.assertEqual(self.json("PUT", "/api/v1/location", body)[0], 422)
            self.clock.advance(0)
        self.assertEqual(self.controller.status()["change"]["state"], "idle")

    def test_switching_over_http_san_diego_austin_san_diego(self):
        status, _, body = self.json("PUT", "/api/v1/location", AUSTIN)
        self.assertEqual((status, body["change"]["state"]), (202, "queued"))
        self.assertEqual(self.json("PUT", "/api/v1/location", SAN_DIEGO)[0], 409)  # one change at a time
        self.assertEqual(self.json("GET", "/api/v1/location")[2]["feed"]["location"], "San Diego, CA")  # still the old feed while it runs
        self.controller.run_pending()
        status, _, body = self.json("GET", "/api/v1/location")
        self.assertEqual((body["change"]["state"], body["feed"]["location"], body["active"]["source"]), ("succeeded", "Austin, TX", "changed"))
        public = json.loads(self.call("GET", "/events.json", key=None)[2])  # the legacy public feed now carries Austin, labeled Austin
        self.assertEqual(([e["title"] for e in public["events"]], public["location"]["display_name"]), (["Austin Show"], "Austin, TX"))
        self.clock.advance(301)
        self.assertEqual(self.json("PUT", "/api/v1/location", SAN_DIEGO)[0], 202)
        self.controller.run_pending()
        public = json.loads(self.call("GET", "/events.json", key=None)[2])
        self.assertEqual(([e["title"] for e in public["events"]], public["location"]["display_name"]), (["San Diego Show"], "San Diego, CA"))

    def test_failure_is_reported_through_the_api_without_touching_the_public_feed(self):
        before = self.call("GET", "/events.json", key=None)[2]
        self.tx.fail = True
        self.json("PUT", "/api/v1/location", AUSTIN)
        self.controller.run_pending()
        _, _, body = self.json("GET", "/api/v1/location")
        self.assertEqual(body["change"]["state"], "failed")
        self.assertIn("site down", body["change"]["error"])
        self.assertEqual(body["feed"]["location"], "San Diego, CA")  # nothing is mislabeled
        self.assertEqual(self.call("GET", "/events.json", key=None)[2], before)


if __name__ == "__main__":
    unittest.main()
