"""The profile engine and its HTTP API: ownership, isolation, failure handling, honest source reporting."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from discovery import server as server_module
from discovery.core import FetchError, save_events
from discovery.engine import Engine, EngineError
from discovery.profiles import ProfileError, location_spec, normalize_profile, profile_id, public_host
from discovery.server import Api, make_server

TOKEN = "t" * 40
AUSTIN = {"area": "Austin, TX", "radius_miles": 25}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def feed(label, n=2, generated="2030-01-01T00:00:00+00:00"):
    events = [{"id": f"{label}:{i}", "title": f"{label} {i}", "start": "2030-01-02T00:00:00+00:00", "url": "https://x.example"} for i in range(n)]
    return {"generated_at": generated, "location": {"display_name": label}, "sources": {"fake": {"ok": True, "events": n}}, "events": events}


class FakeRefresh:
    """Stands in for discovery.refresh.refresh: writes a feed named after the requested place, or fails."""

    def __init__(self):
        self.calls, self.fail, self.running_now, self.max_parallel = [], None, 0, 0

    def __call__(self, config, options, cache_path, output, lock_path, fetcher=None, location_cache_path=None, location_providers=None):
        self.running_now += 1
        self.max_parallel = max(self.max_parallel, self.running_now)
        try:
            self.calls.append((config, options, output))
            if self.fail:
                print(self.fail, file=__import__("sys").stderr)
                return 1
            place = config["location"]
            save_events(feed(place.get("query") or place.get("name") or "coordinates"), output)
            return 0
        finally:
            self.running_now -= 1


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock, self.refresh = Clock(), FakeRefresh()
        self.engine = self.make()

    def make(self, **extra):
        options = dict(interval=3600, clock=self.clock, refresh_fn=self.refresh, fetcher_factory=lambda: object(),
                       host_check=lambda host: host not in ("intranet.example",), probe=lambda fetcher, url: "localist" in url)
        options.update(extra)
        return Engine(self.tmp.name, **options)

    def drain(self, engine=None):
        engine = engine or self.engine
        while engine.run_next():
            pass


class Profiles(unittest.TestCase):
    def test_equivalent_requests_share_one_profile_and_different_ones_do_not(self):
        a = normalize_profile({"area": "  Austin,   TX ", "radius_miles": 25.0, "sources": ["https://B.example/", "https://a.example"]})
        b = normalize_profile({"area": "austin, tx".title(), "radius_miles": 25, "sources": ["https://a.example", "https://b.example"]})
        self.assertEqual(profile_id(a), profile_id(b))
        for other in ({"area": "Austin, TX", "radius_miles": 30}, {"area": "Dallas, TX", "radius_miles": 25}, {"area": "Austin, TX"}):
            self.assertNotEqual(profile_id(normalize_profile(other)), profile_id(b))

    def test_validation(self):
        for bad in ([], {}, {"area": ""}, {"area": "x", "radius_miles": 0}, {"area": "x", "radius_miles": 500}, {"area": "x", "extra": 1},
                    {"latitude": 1}, {"latitude": 0, "longitude": 0}, {"area": "x", "sources": ["ftp://a.example"]},
                    {"area": "x", "sources": ["https://user:pw@a.example"]}, {"area": "x", "sources": [f"https://a{i}.example" for i in range(11)]},
                    {"area": "x", "country": "USA"}):
            with self.assertRaises(ProfileError, msg=bad):
                normalize_profile(bad)

    def test_area_text_becomes_a_location(self):
        self.assertEqual(location_spec(normalize_profile({"area": "Austin, TX"}))["query"], "Austin, TX")
        self.assertEqual(location_spec(normalize_profile({"area": "78701", "country": "us"})), {"radius_miles": 25.0, "country": "US", "zip": "78701"})
        pair = location_spec(normalize_profile({"area": "30.27, -97.74", "radius_miles": 10}))
        self.assertEqual((pair["latitude"], pair["longitude"]), (30.27, -97.74))
        home = location_spec(normalize_profile({"latitude": 30.26715, "longitude": -97.74306, "name": "Home"}))
        self.assertEqual((home["latitude"], home["name"]), (30.2672, "Home"))

    def test_private_addresses_are_not_public(self):
        self.assertFalse(public_host("x", resolver=lambda host, port: [(0, 0, 0, "", ("192.168.1.5", 0))]))
        self.assertFalse(public_host("x", resolver=lambda host, port: [(0, 0, 0, "", ("127.0.0.1", 0))]))
        self.assertTrue(public_host("x", resolver=lambda host, port: [(0, 0, 0, "", ("93.184.216.34", 0))]))


class Scheduling(EngineCase):
    def test_identical_requests_run_discovery_once_and_share_the_result(self):
        ids = {self.engine.register(AUSTIN) for _ in range(5)}
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.engine.snapshot(ids.pop())["state"], "pending")
        self.drain()
        self.assertEqual(len(self.refresh.calls), 1)
        pid = self.engine.register({"area": "austin, tx", "radius_miles": 25})
        self.assertEqual(self.engine.snapshot(pid)["state"], "ready")
        self.drain()
        self.assertEqual(len(self.refresh.calls), 1)

    def test_different_profiles_are_isolated_and_run_one_after_another(self):
        a, b = self.engine.register(AUSTIN), self.engine.register({"area": "Boston, MA"})
        self.drain()
        self.assertEqual(self.refresh.max_parallel, 1)
        self.assertEqual({e["id"].split(":")[0] for e in self.engine.snapshot(a)["events"]}, {"Austin, TX"})
        self.assertEqual({e["id"].split(":")[0] for e in self.engine.snapshot(b)["events"]}, {"Boston, MA"})
        outputs = {call[2] for call in self.refresh.calls}
        self.assertEqual(len(outputs), 2)
        self.assertEqual(len({os.path.dirname(o) for o in outputs}), 2)

    def test_a_failed_run_is_reported_and_keeps_the_previous_feed(self):
        pid = self.engine.register(AUSTIN)
        self.drain()
        before = self.engine.snapshot(pid)["events"]
        self.clock.now += 4000
        self.engine.tick()
        self.refresh.fail = "Coverage error: no enabled source applies to Austin"
        self.drain()
        snapshot = self.engine.snapshot(pid)
        self.assertEqual((snapshot["state"], snapshot["events"]), ("ready", before))
        self.assertIn("no enabled source applies", snapshot["error"])
        fresh = self.engine.register({"area": "Nowhere"})
        self.drain()
        failed = self.engine.snapshot(fresh)
        self.assertEqual(failed["state"], "error")
        self.assertIn("no enabled source applies", failed["error"])
        self.refresh.fail = None
        self.clock.now += 700  # after the retry delay a request retries the run
        self.engine.register({"area": "Nowhere"})
        self.drain()
        self.assertEqual(self.engine.snapshot(fresh)["state"], "ready")

    def test_stale_profiles_refresh_on_the_interval_and_unused_ones_are_dropped(self):
        pid = self.engine.register(AUSTIN)
        self.drain()
        self.engine.tick()
        self.assertIsNone(self.engine.run_next())
        self.clock.now += 3601
        self.engine.tick()
        self.assertEqual(self.engine.run_next(), pid)
        self.clock.now += 15 * 86400
        self.engine.tick()
        self.assertEqual(self.engine.status()["profiles"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "profiles", pid)))

    def test_profiles_and_feeds_survive_a_restart_without_new_runs(self):
        pid = self.engine.register(AUSTIN)
        self.drain()
        again = self.make()
        self.assertEqual(again.snapshot(pid)["state"], "ready")
        self.assertIsNone(again.run_next())

    def test_snapshot_omits_unchanged_events(self):
        pid = self.engine.register(AUSTIN)
        self.drain()
        generated = self.engine.snapshot(pid)["generated_at"]
        self.assertTrue(self.engine.snapshot(pid, since=generated)["unchanged"])
        self.assertNotIn("events", self.engine.snapshot(pid, since=generated))


class Admission(EngineCase):
    """Profile identity and admission: repeated loads reuse one profile, abandoned ones never exhaust the limit."""

    def typed_letter_by_letter(self, engine, text="San Diego, CA"):
        ids = []
        for i in range(len(text)):
            self.clock.now += 0.5  # keystrokes arrive over a few seconds
            ids.append(engine.register({"area": text[: i + 1].rstrip(), "radius_miles": 25}))
        return ids

    def test_repeated_card_loads_and_equivalent_spellings_reuse_one_profile(self):
        ids = set()
        for _ in range(20):  # page loads, a second dashboard, the editor preview of the same card
            for spelling in ("San Diego, CA", "  San   Diego, CA ", "san diego, ca"):
                ids.add(self.engine.register({"area": spelling, "radius_miles": 25}))
                ids.add(self.engine.register({"area": spelling, "radius_miles": 25.0, "sources": []}))
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.engine.status()["profiles"], 1)
        self.drain()
        self.assertEqual(len(self.refresh.calls), 1)  # one discovery, however often it was asked for

    def test_an_area_typed_letter_by_letter_never_locks_out_the_real_card(self):
        self.typed_letter_by_letter(self.engine)  # 13 distinct half-typed areas arrive within seconds, more than the limit of 12
        self.assertLessEqual(self.engine.status()["profiles"], 12)
        self.clock.now += 0.5
        real = self.engine.register({"area": "San Diego, CA", "radius_miles": 25})  # never refused
        self.assertEqual(self.engine.snapshot(real)["state"], "pending")
        self.assertEqual(self.engine.run_next(), real)  # whoever asked last is searched first, not after twelve typos
        self.assertEqual(self.engine.snapshot(real)["state"], "ready")

    def test_a_profile_being_discovered_is_never_evicted(self):
        engine = self.make(max_profiles=3)
        running = engine.register({"area": "Running, CA"})
        outcome = {}
        original = self.refresh.__call__

        def busy(config, options, **kwargs):
            for name in ("B", "C", "D", "E"):  # other areas arrive while this one is being searched
                engine.register({"area": name})
            outcome["still_there"] = engine.snapshot(running)["state"]
            return original(config, options, **kwargs)

        engine.refresh_fn = busy
        engine.run_next()
        self.assertEqual(outcome["still_there"], "discovering")
        self.assertEqual(engine.snapshot(running)["state"], "ready")
        self.assertLessEqual(engine.status()["profiles"], 3)

    def test_profiles_with_events_survive_the_clean_up_of_abandoned_ones(self):
        proven = self.engine.register({"area": "Austin, TX"})
        self.drain()
        before = self.engine.snapshot(proven)["events"]
        self.typed_letter_by_letter(self.engine, "Boston, MA")
        self.clock.now += 2 * 3600
        self.engine.tick()
        self.assertEqual(self.engine.status()["profiles"], 1)  # the unproven ones expired, the proven one stayed
        self.assertEqual(self.engine.snapshot(proven)["events"], before)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "profiles", proven, "events.json")))

    def test_when_every_profile_has_events_and_is_in_use_the_limit_is_reported_not_hidden(self):
        engine = self.make(max_profiles=2)
        for name in ("A1", "A2"):
            engine.register({"area": name})
        self.drain(engine)
        with self.assertRaises(EngineError) as raised:
            engine.register({"area": "A3"})
        self.assertEqual(raised.exception.status, 429)
        self.assertIn("too many different areas", raised.exception.message)
        self.clock.now += 700  # idle ones with events can be evicted once nobody has asked for ten minutes
        engine.register({"area": "A3"})
        self.assertEqual(engine.status()["profiles"], 2)

    def test_after_a_restart_abandoned_profiles_are_not_searched_but_used_ones_are(self):
        self.engine.register({"area": "Abandoned"})
        self.clock.now += 3600  # the engine was stopped; nobody has asked for it since
        restarted = self.make()
        self.assertEqual(restarted.status()["profiles"], 1)  # remembered on disk...
        self.assertIsNone(restarted.run_next())  # ...but not searched
        revived = restarted.register({"area": "Abandoned"})  # a card asks again
        self.assertEqual(restarted.run_next(), revived)

        fresh = self.make()
        recent = fresh.register({"area": "Just asked"})
        self.clock.now += 60
        again = self.make()
        self.assertEqual(again.run_next(), recent)  # asked a minute before the restart: still wanted

    def test_queued_requests_nobody_repeated_are_skipped(self):
        stale = self.engine.register({"area": "Typo"})
        self.clock.now += 700
        fresh = self.engine.register({"area": "Real"})
        self.assertEqual(self.engine.run_next(), fresh)
        self.assertEqual(self.engine.run_next(), stale)  # popped, but too old to be worth a search
        self.assertEqual(len(self.refresh.calls), 1)
        self.assertEqual(self.engine.snapshot(stale)["state"], "pending")

    def test_separate_areas_stay_isolated_through_all_of_it(self):
        a, b = self.engine.register({"area": "Austin, TX"}), self.engine.register({"area": "Boston, MA"})
        self.drain()
        self.typed_letter_by_letter(self.engine, "Chicago, IL")
        self.assertEqual({e["id"].split(":")[0] for e in self.engine.snapshot(a)["events"]}, {"Austin, TX"})
        self.assertEqual({e["id"].split(":")[0] for e in self.engine.snapshot(b)["events"]}, {"Boston, MA"})


class Sources(EngineCase):
    def test_named_websites_are_matched_probed_or_reported_unsupported(self):
        profile = {"area": "Austin, TX", "sources": ["https://calendar.utexas.edu/events", "https://www.kpbs.org/events", "https://news.example/localist",
                                                      "https://blog.example", "https://intranet.example"]}
        pid = self.engine.register(profile)
        self.drain()
        config, options, _ = self.refresh.calls[0]
        self.assertTrue(options["explicit_sources"])
        names = sorted(entry["adapter"] for entry in config["sources"])
        self.assertEqual(names, ["kpbs", "localist", "localist"])  # kpbs and utexas from the registry, news.example probed as Localist
        report = {item["url"]: item for item in self.engine.snapshot(pid)["report"]}
        self.assertEqual(report["https://www.kpbs.org/events"]["status"], "supported")
        self.assertEqual(report["https://news.example/localist"]["adapter"], "localist")
        self.assertIn("no adapter", report["https://blog.example"]["reason"])
        self.assertIn("not a public website", report["https://intranet.example"]["reason"])

    def test_only_unsupported_websites_is_an_honest_error_and_runs_nothing(self):
        pid = self.engine.register({"area": "Austin, TX", "sources": ["https://blog.example"]})
        self.drain()
        snapshot = self.engine.snapshot(pid)
        self.assertEqual(snapshot["state"], "error")
        self.assertIn("none of the requested websites", snapshot["error"])
        self.assertEqual(self.refresh.calls, [])
        self.assertEqual(snapshot["report"][0]["status"], "unsupported")

    def test_an_unreachable_website_is_unsupported_not_a_crash(self):
        def probe(fetcher, url):
            raise FetchError("timed out")
        engine = self.make(probe=probe)
        pid = engine.register({"area": "Austin, TX", "sources": ["https://calendar.utexas.edu", "https://down.example"]})
        self.drain(engine)
        report = {item["url"]: item for item in engine.snapshot(pid)["report"]}
        self.assertIn("could not be read", report["https://down.example"]["reason"])
        self.assertEqual(engine.snapshot(pid)["state"], "ready")

    def test_without_websites_the_registry_is_chosen_by_coverage(self):
        self.engine.register(AUSTIN)
        self.drain()
        config, options, _ = self.refresh.calls[0]
        self.assertFalse(options["explicit_sources"])
        self.assertGreater(len(config["sources"]), 1)


class Http(EngineCase):
    def setUp(self):
        super().setUp()
        self.api = Api(self.engine, TOKEN)
        self.server = make_server(self.api, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, method, path, body=None, token=TOKEN):
        request = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_authentication_is_required_but_health_is_open(self):
        self.assertEqual(self.call("GET", "/health", token=None)[0], 200)
        self.assertEqual(self.call("GET", "/api/v1/status", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/status", token="x" * 40)[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/status")[0], 200)

    def test_register_then_read(self):
        status, registered = self.call("POST", "/api/v1/profiles", AUSTIN)
        self.assertEqual(status, 200)
        self.assertNotIn("events", registered)
        self.drain()
        status, snapshot = self.call("GET", f"/api/v1/profiles/{registered['id']}")
        self.assertEqual((status, snapshot["state"], len(snapshot["events"])), (200, "ready", 2))
        self.assertEqual(self.call("GET", "/api/v1/profiles/unknown")[0], 404)

    def test_bad_requests_get_clear_errors(self):
        self.assertEqual(self.call("POST", "/api/v1/profiles", {"area": ""})[0], 422)
        self.assertEqual(self.call("POST", "/api/v1/profiles", {"url": "http://evil.example", "area": "x"})[0], 422)
        self.assertEqual(self.call("DELETE", "/api/v1/profiles/x")[0], 405)

    def test_repeated_wrong_tokens_are_throttled(self):
        for _ in range(5):
            self.call("GET", "/api/v1/status", token="y" * 40)
        self.assertEqual(self.call("GET", "/api/v1/status")[0], 429)


class Settings(unittest.TestCase):
    def test_options_file_and_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "options.json")
            with open(path, "w") as handle:
                json.dump({"token": "a" * 40, "refresh_minutes": 30, "log_level": "debug"}, handle)
            settings = server_module.load_settings({}, path)
            self.assertEqual((settings["token"], settings["refresh_minutes"], settings["log_level"]), ("a" * 40, 30, "DEBUG"))
            self.assertEqual(server_module.load_settings({"AREA_EVENTS_TOKEN": "b" * 40}, path)["token"], "b" * 40)
            self.assertEqual(server_module.load_settings({}, os.path.join(folder, "none.json"))["token"], "")


class AppPackage(unittest.TestCase):
    """Static checks of the Home Assistant app files (the image itself needs a Supervisor to build and run)."""

    APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "area_events")

    def read(self, name, mode="r"):
        with open(os.path.join(self.APP, name), mode, **({"encoding": "utf-8"} if mode == "r" else {})) as handle:
            return handle.read()

    def test_config_declares_what_the_supervisor_requires(self):
        config = self.read("config.yaml")
        for line in ('slug: area_events', 'init: false', '- aarch64', '- amd64', 'token: password?', 'watchdog: "http://[HOST]:[PORT:8765]/health"'):
            self.assertIn(line, config)
        self.assertNotIn("armv7", config)

    def test_run_script_has_unix_line_endings_and_starts_the_engine(self):
        script = self.read("run.sh", "rb")
        self.assertTrue(script.startswith(b"#!/command/with-contenv bashio\n"))
        self.assertNotIn(b"\r", script)
        self.assertIn(b"exec python3 -m discovery", script)

    def test_image_contains_only_the_engine_and_no_pip_dependencies(self):
        dockerfile = self.read("Dockerfile")
        self.assertIn("COPY discovery/ /app/discovery/", dockerfile)
        self.assertIn("FROM ghcr.io/home-assistant/base-python:", dockerfile)
        self.assertNotIn("apk add", dockerfile)
        self.assertNotIn("pip install", dockerfile.replace("nothing to pip install", ""))

    def test_the_app_version_matches_the_integration(self):
        version = json.load(open(os.path.join(os.path.dirname(self.APP), "custom_components", "area_events", "manifest.json"), encoding="utf-8"))["version"]
        self.assertIn(f'version: "{version}"', self.read("config.yaml"))


if __name__ == "__main__":
    unittest.main()
