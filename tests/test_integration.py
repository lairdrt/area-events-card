"""The integration's Home Assistant-free logic: request building, the app client, and request coordination.

Home Assistant itself is not importable here, so config_flow.py, coordinator.py, websocket_api.py and __init__.py are
only checked for syntax (see test_glue_compiles); they need a running Home Assistant to verify.
"""
import ast
import asyncio
import importlib
import json
import os
import sys
import types
import unittest

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components", "area_events")

# Load the HA-free modules as a package without running __init__.py (which imports homeassistant).
package = types.ModuleType("area_events_core")
package.__path__ = [ROOT]
sys.modules["area_events_core"] = package
client_module = importlib.import_module("area_events_core.client")
hub_module = importlib.import_module("area_events_core.hub")
profile_module = importlib.import_module("area_events_core.profile")

AreaEventsAuthError = client_module.AreaEventsAuthError
AreaEventsConnectionError = client_module.AreaEventsConnectionError
AreaEventsRequestError = client_module.AreaEventsRequestError
Hub = hub_module.Hub
build_profile = profile_module.build_profile
ProfileInputError = profile_module.ProfileInputError
HOME = {"latitude": 30.26715, "longitude": -97.74306, "name": "Home", "time_zone": "America/Chicago"}


def run(coroutine):
    return asyncio.run(coroutine)


class FakeClient:
    """Stands in for the app: remembers registrations, can be made unreachable, forgetful or hostile."""

    def __init__(self):
        self.registered, self.gets, self.statuses = [], [], 0
        self.down = self.reject = self.deny = None
        self.known = {}
        self.generated = "2030-01-01T00:00:00+00:00"
        self.state = "ready"

    async def status(self):
        self.statuses += 1
        self._fail()
        return {}

    def _fail(self):
        if self.down:
            raise AreaEventsConnectionError(self.down)
        if self.deny:
            raise AreaEventsAuthError("denied")

    async def register(self, profile):
        self._fail()
        if self.reject:
            raise AreaEventsRequestError(*self.reject)
        self.registered.append(profile)
        pid = f"p{len(set(map(repr, self.registered)))}-{abs(hash(repr(sorted(profile.items())))) % 1000}"
        self.known[pid] = profile
        return {"id": pid, "state": "pending"}

    async def get(self, pid, since=None):
        self._fail()
        self.gets.append((pid, since))
        if pid not in self.known:
            raise AreaEventsRequestError(404, "unknown profile")
        answer = {"id": pid, "state": self.state, "generated_at": self.generated, "location": {"display_name": str(self.known[pid].get("area"))},
                  "sources": {}, "report": [], "error": None, "refreshing": False}
        if self.state == "ready":
            if since == self.generated:
                answer["unchanged"] = True
            else:
                answer["events"] = [{"id": "e1", "title": "Event", "start": "2030-01-02T00:00:00+00:00", "url": "https://x.example"}]
        return answer


class BuildProfile(unittest.TestCase):
    def test_area_radius_and_sources_pass_through(self):
        profile = build_profile({"area": "  Austin,  TX", "radius_miles": 30, "sources": [" https://calendar.utexas.edu "]}, HOME, "US")
        self.assertEqual(profile, {"radius_miles": 30.0, "area": "Austin, TX", "sources": ["https://calendar.utexas.edu"]})

    def test_a_zip_code_gets_the_ha_country_but_a_city_does_not(self):
        self.assertEqual(build_profile({"area": "78701"}, HOME, "US")["country"], "US")
        self.assertNotIn("country", build_profile({"area": "Paris, France"}, HOME, "US"))
        self.assertNotIn("country", build_profile({"area": "30.27, -97.74"}, HOME, "US"))
        with self.assertRaisesRegex(ProfileInputError, "needs a country"):
            build_profile({"area": "78701"}, HOME, None)

    def test_without_an_area_the_ha_home_is_used_rounded_with_its_time_zone(self):
        self.assertEqual(build_profile({}, HOME, "US"), {"radius_miles": 25.0, "latitude": 30.2672, "longitude": -97.7431, "name": "Home",
                                                          "timezone": "America/Chicago"})

    def test_a_missing_ha_home_is_a_clear_error(self):
        for home in (None, {"latitude": 0, "longitude": 0}, {"latitude": 95, "longitude": 0}, {}):
            with self.assertRaisesRegex(ProfileInputError, "Home Assistant has no home location"):
                build_profile({}, home, "US")

    def test_invalid_input(self):
        for bad in ({"radius_miles": 0}, {"radius_miles": 999}, {"radius_miles": "far"}, {"area": 5}, {"area": " "}, {"sources": "x"}, {"area": "x", "sources": [1]},
                    {"area": "x", "sources": ["https://a.example"] * 11}):
            with self.assertRaises(ProfileInputError, msg=bad):
                build_profile(bad, HOME, "US")


class HubCoordination(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.now = 1000.0
        self.hub = Hub(self.client, clock=lambda: self.now)

    def test_many_identical_concurrent_requests_register_once_and_share_the_result(self):
        async def scenario():
            profile = {"area": "Austin, TX", "radius_miles": 25.0}
            return await asyncio.gather(*[self.hub.request(dict(profile)) for _ in range(6)])
        results = run(scenario())
        self.assertEqual(len(self.client.registered), 1)
        self.assertEqual(len(self.client.gets), 1)
        self.assertTrue(all(r["state"] == "ready" and len(r["events"]) == 1 for r in results))
        self.assertEqual(self.hub.profiles, 1)

    def test_different_profiles_never_share_or_overwrite(self):
        async def scenario():
            a = await self.hub.request({"area": "Austin, TX", "radius_miles": 25.0})
            b = await self.hub.request({"area": "Boston, MA", "radius_miles": 25.0})
            c = await self.hub.request({"area": "Austin, TX", "radius_miles": 40.0})
            return a, b, c
        a, b, c = run(scenario())
        self.assertEqual((a["location"]["display_name"], b["location"]["display_name"], c["location"]["display_name"]), ("Austin, TX", "Boston, MA", "Austin, TX"))
        self.assertEqual(self.hub.profiles, 3)
        self.assertEqual(len(self.client.registered), 3)

    def test_repeat_requests_are_served_from_the_cache_without_calling_the_app(self):
        profile = {"area": "Austin, TX", "radius_miles": 25.0}
        run(self.hub.request(profile))
        run(self.hub.request(profile))
        self.assertEqual((len(self.client.registered), len(self.client.gets)), (1, 1))

    def test_since_omits_unchanged_events(self):
        profile = {"area": "Austin, TX", "radius_miles": 25.0}
        first = run(self.hub.request(profile))
        again = run(self.hub.request(profile, since=first["generated_at"]))
        self.assertTrue(again["unchanged"])
        self.assertNotIn("events", again)

    def test_refresh_visits_each_active_profile_in_turn_and_keeps_events_when_unchanged(self):
        run(self.hub.request({"area": "Austin, TX", "radius_miles": 25.0}))
        run(self.hub.request({"area": "Boston, MA", "radius_miles": 25.0}))
        self.now += 100
        run(self.hub.refresh())
        self.assertEqual(self.client.statuses, 1)
        self.assertEqual(len(self.client.gets), 4)
        self.assertTrue(all(since for _, since in self.client.gets[2:]))
        answer = run(self.hub.request({"area": "Austin, TX", "radius_miles": 25.0}))
        self.assertEqual(len(answer["events"]), 1)  # the unchanged answer did not wipe the cached events

    def test_profiles_nobody_asks_for_are_dropped(self):
        run(self.hub.request({"area": "Austin, TX", "radius_miles": 25.0}))
        self.now += 3600
        run(self.hub.refresh())
        self.assertEqual(self.hub.profiles, 0)

    def test_registration_is_repeated_now_and_then(self):
        profile = {"area": "Austin, TX", "radius_miles": 25.0}
        run(self.hub.request(profile))
        self.now += 1000
        run(self.hub.refresh())
        self.assertEqual(len(self.client.registered), 2)

    def test_a_profile_the_app_forgot_is_registered_again(self):
        profile = {"area": "Austin, TX", "radius_miles": 25.0}
        run(self.hub.request(profile))
        self.client.known.clear()
        self.now += 10
        run(self.hub.refresh())
        self.assertEqual(len(self.client.registered), 2)
        self.assertEqual(run(self.hub.request(profile))["state"], "ready")

    def test_an_unreachable_app_is_reported_and_cached_results_are_kept(self):
        profile = {"area": "Austin, TX", "radius_miles": 25.0}
        self.client.down = "connection refused"
        down = run(self.hub.request(profile))
        self.assertEqual((down["state"], down["connected"]), ("unavailable", False))
        self.assertIn("connection refused", down["message"])
        self.assertTrue(self.hub.busy)  # keep trying quickly
        self.client.down = None
        run(self.hub.refresh())
        self.assertEqual(run(self.hub.request(profile))["state"], "ready")
        self.client.down = "gone again"
        with self.assertRaises(AreaEventsConnectionError):
            run(self.hub.refresh())
        kept = run(self.hub.request(profile))
        self.assertEqual((kept["state"], len(kept["events"])), ("ready", 1))

    def test_a_refused_profile_raises_and_is_not_kept(self):
        self.client.reject = (422, "no such place")
        with self.assertRaises(AreaEventsRequestError):
            run(self.hub.request({"area": "Nowhere", "radius_miles": 25.0}))
        self.assertEqual(self.hub.profiles, 0)

    def test_a_rejected_token_propagates(self):
        self.client.deny = True
        with self.assertRaises(AreaEventsAuthError):
            run(self.hub.request({"area": "Austin, TX", "radius_miles": 25.0}))

    def test_busy_while_discovery_runs(self):
        self.client.state = "discovering"
        run(self.hub.request({"area": "Austin, TX", "radius_miles": 25.0}))
        self.assertTrue(self.hub.busy)
        self.client.state = "ready"
        run(self.hub.refresh())
        self.assertFalse(self.hub.busy)


class Client(unittest.TestCase):
    def test_base_url_normalization(self):
        normalize = client_module.normalize_base_url
        self.assertEqual(normalize(" HTTP://Host-Name:8765/ "), "http://host-name:8765")
        for bad in ("host:8765", "ftp://host", "http://", "http://user:pw@host", "http://host?x=1", ""):
            with self.assertRaises(ValueError, msg=bad):
                normalize(bad)

    def test_http_errors_map_to_the_right_exceptions(self):
        class Response:
            def __init__(self, status, payload):
                self.status, self.payload = status, payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def json(self, content_type=None):
                if self.payload is None:
                    raise ValueError("not json")
                return self.payload

        class Session:
            def __init__(self, *answers):
                self.answers, self.calls = list(answers), []

            def request(self, method, url, json=None, headers=None):
                self.calls.append((method, url, json, headers))
                return self.answers.pop(0)

        async def scenario():
            session = Session(Response(200, {"id": "a"}), Response(401, {"error": "no"}), Response(422, {"error": "bad area"}), Response(500, None), Response(200, ["x"]))
            client = client_module.AreaEventsClient(session, "http://app:8765/", "secret-token")
            self.assertEqual(await client.register({"area": "x"}), {"id": "a"})
            self.assertEqual(session.calls[0][0:2], ("POST", "http://app:8765/api/v1/profiles"))
            self.assertEqual(session.calls[0][3], {"Authorization": "Bearer secret-token"})
            with self.assertRaises(AreaEventsAuthError):
                await client.status()
            with self.assertRaises(AreaEventsRequestError) as refused:
                await client.status()
            self.assertEqual((refused.exception.status, refused.exception.message), (422, "bad area"))
            with self.assertRaises(AreaEventsConnectionError):
                await client.status()
            with self.assertRaises(AreaEventsConnectionError):
                await client.get("p/../x", since="2030-01-01T00:00:00+00:00")
            self.assertIn("/api/v1/profiles/p%2F..%2Fx?since=2030-01-01T00%3A00%3A00%2B00%3A00", session.calls[-1][1])

        run(scenario())

    def test_network_failures_become_connection_errors(self):
        class Broken:
            def request(self, *args, **kwargs):
                raise OSError("no route")

        with self.assertRaises(AreaEventsConnectionError):
            run(client_module.AreaEventsClient(Broken(), "http://app:8765", "t").status())


class Glue(unittest.TestCase):
    def test_glue_compiles(self):
        for name in ("__init__", "config_flow", "coordinator", "websocket_api"):
            with open(os.path.join(ROOT, f"{name}.py"), encoding="utf-8") as handle:
                ast.parse(handle.read())

    def test_the_manifest_and_translations_agree(self):
        def read(*parts):
            with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
                return handle.read()

        manifest = json.loads(read("manifest.json"))
        self.assertEqual((manifest["domain"], manifest["config_flow"], manifest["iot_class"]), ("area_events", True, "local_polling"))
        self.assertEqual(read("strings.json"), read("translations", "en.json"))


if __name__ == "__main__":
    unittest.main()
