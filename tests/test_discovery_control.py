"""Milestone 2B: discovery areas, the control contract, the scheduler, the host loop, the authenticated HTTP API, the Home Assistant async client,
and an end-to-end run of an independent host. Simulated: saved pages and a fixed or real clock; no card, no Home Assistant, no website."""
import asyncio
import functools
import http.client
import importlib.util
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from discovery.core import FetchError
from discovery.service import app as app_module
from discovery.service.api import ApiTokens, DiscoveryApi, DiscoveryApiServer, MAX_BODY
from discovery.service.areas import AreaRepository, SettingsRepository
from discovery.service.contracts import CandidateQuery
from discovery.service.control import (CONTROL_SCHEMA, CreateAreaRequest, NotFound, RevisionConflict, SettingsRequest, UpdateAreaRequest, ValidationFailed)
from discovery.service.host import HostConfig
from discovery.service.repositories import SourceSpec
from discovery.service.scheduler import AreaPacing, SchedulerConfig
from discovery.service.schema import MIGRATIONS
from discovery.service.store import DiscoveryStore
from tests.test_discovery_service import Clock, FakeClient, SPEC, city_pages
from tests.test_site_discovery import CITY_URL

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROL_TOKEN = "c" * 40
READ_TOKEN = "r" * 40
OTHER = SourceSpec("other_city", "sandiego_gov", "Other city", "https://other.example/events", options=(("pages", 1),), interval_s=3600)


def other_pages(n=2):
    from tests.test_site_discovery import city_detail, city_listing
    items = [(f"o{i}", f"Other {i}", i + 1) for i in range(n)]
    pages = {"https://other.example/events": city_listing(items).replace("/event/", "/event/")}
    for i in range(n):
        pages[f"https://other.example/event/o{i}"] = city_detail(f"Elm Street {i}", f"{i + 1} Elm Street San Diego, CA")
    return pages


@functools.lru_cache(maxsize=1)
def load_ha_client():
    spec = importlib.util.spec_from_file_location("discovery_client_under_test", os.path.join(ROOT, "custom_components", "area_events", "discovery_client.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = os.path.join(self.folder.name, "discovery.db")
        self.clock = Clock()

    def build(self, pages=None, **kwargs):
        pages = {**city_pages(), **other_pages()} if pages is None else pages
        client = kwargs.pop("fetch_client", None) or FakeClient(pages)
        built = app_module.build(self.db, clock=self.clock, fetch_client=client, **kwargs)
        self.addCleanup(built.close)
        built.client = client
        built.service.register_source(SPEC)
        built.service.register_source(OTHER)
        return built

    def count(self, table, where="1=1"):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
        finally:
            connection.close()

    @staticmethod
    def area(name="San Diego", sources=("sandiego_gov",), **kwargs):
        return CreateAreaRequest(name, kwargs.pop("latitude", 32.7157), kwargs.pop("longitude", -117.1611), kwargs.pop("radius", 25), sources=sources, **kwargs)


class Areas(Fixture):
    def test_an_area_is_stored_validated_and_survives_a_restart(self):
        built = self.build()
        result = built.control.create_area(self.area(request_id="r1", intensity="high"))
        self.assertEqual((result.schema, result.revision, result.replayed, result.change_seq is not None), (CONTROL_SCHEMA, 1, False, True))
        key = result.area.area_key
        built.close()
        again = self.build()
        area = again.control.get_area(key)
        self.assertEqual((area.name, area.latitude, area.longitude, area.radius, area.unit, area.enabled, area.intensity, area.revision, area.sources),
                         ("San Diego", 32.7157, -117.1611, 25.0, "mi", True, "high", 1, ("sandiego_gov",)))
        self.assertEqual(len(again.control.list_areas()), 1)

    def test_invalid_requests_are_refused_before_anything_is_written(self):
        built = self.build()
        for kwargs in ({"latitude": 91}, {"latitude": -91}, {"longitude": 181}, {"radius": 0}, {"radius": -3}, {"radius": 251}, {"radius": float("nan")}, {"radius": True},
                       {"latitude": "32"}, {"unit": "furlong"}, {"intensity": "extreme"}, {"name": ""}, {"name": "x" * 81}, {"name": "bad\x00name"}, {"enabled": "yes"},
                       {"sources": ("a" * 81,)}, {"sources": "sandiego_gov"}, {"sources": tuple(f"s{i}" for i in range(21))}, {"request_id": ""}):
            with self.subTest(kwargs):
                with self.assertRaises(ValidationFailed):
                    built.control.create_area(self.area(**kwargs))
        with self.assertRaises(NotFound):
            built.control.create_area(self.area(sources=("no_such_source",)))
        self.assertEqual(built.control.create_area(self.area(unit="km", radius=400)).area.radius, 400)
        with self.assertRaises(ValidationFailed):
            built.control.create_area(self.area(unit="mi", radius=400))
        self.assertEqual(self.count("areas"), 1)
        self.assertEqual(self.count("changes", "entity='area'"), 1)

    def test_areas_and_sources_are_many_to_many(self):
        built = self.build()
        a = built.control.create_area(self.area("North", sources=("sandiego_gov", "other_city"))).area
        b = built.control.create_area(self.area("South", sources=("sandiego_gov",))).area
        self.assertEqual((a.sources, b.sources), (("other_city", "sandiego_gov"), ("sandiego_gov",)))
        self.assertEqual(self.count("area_sources"), 3)
        updated = built.control.update_area(UpdateAreaRequest(b.area_key, 1, sources=("other_city",)))
        self.assertEqual((updated.area.sources, updated.area.revision), (("other_city",), 2))
        with built.store.read() as connection:
            eligible = AreaRepository().eligible_sources(connection)
        self.assertEqual([row[1] for row in eligible], ["other_city", "sandiego_gov"], "one row per source however many areas use it")

    def test_stale_and_conflicting_revisions_are_refused(self):
        built = self.build()
        area = built.control.create_area(self.area()).area
        first = built.control.update_area(UpdateAreaRequest(area.area_key, 1, radius=30))
        self.assertEqual(first.revision, 2)
        with self.assertRaises(RevisionConflict) as caught:
            built.control.update_area(UpdateAreaRequest(area.area_key, 1, radius=40))
        self.assertEqual(caught.exception.current_revision, 2)
        self.assertEqual(built.control.get_area(area.area_key).radius, 30.0)
        for bad in (None, 0, -1, True, "2"):
            with self.assertRaises(ValidationFailed):
                built.control.update_area(UpdateAreaRequest(area.area_key, bad, radius=1))
        with self.assertRaises(NotFound):
            built.control.update_area(UpdateAreaRequest("missing1234", 1, radius=1))
        settings = built.control.get_status().settings
        built.control.set_discovery_mode(SettingsRequest(settings.revision, mode="manual"))
        with self.assertRaises(RevisionConflict):
            built.control.set_discovery_mode(SettingsRequest(settings.revision, mode="automatic"))

    def test_commands_are_idempotent(self):
        built = self.build()
        first = built.control.create_area(self.area(request_id="same"))
        second = built.control.create_area(self.area(request_id="same", name="Ignored on replay"))
        self.assertEqual((second.replayed, second.area.area_key, second.change_seq, second.area.name), (True, first.area.area_key, None, "San Diego"))
        self.assertEqual(self.count("areas"), 1)
        key = first.area.area_key
        enabled_again = built.control.set_area_enabled(key, True)
        self.assertEqual((enabled_again.replayed, enabled_again.revision), (True, 1))
        off = built.control.set_area_enabled(key, False)
        self.assertEqual((off.replayed, off.revision, off.area.enabled), (False, 2, False))
        self.assertTrue(built.control.set_area_enabled(key, False).replayed)
        same_update = built.control.update_area(UpdateAreaRequest(key, 2, name="San Diego", radius=25))
        self.assertTrue(same_update.replayed)
        self.assertTrue(built.control.resume().replayed)
        self.assertFalse(built.control.pause().replayed)
        self.assertTrue(built.control.pause().replayed)
        self.assertEqual(self.count("changes", "entity IN ('area', 'settings', 'discovery')"), 3, "create, disable, pause; every replay and no-op wrote nothing")

    def test_every_change_is_a_durable_typed_record_in_one_sequence(self):
        built = self.build()
        area = built.control.create_area(self.area()).area
        built.control.update_area(UpdateAreaRequest(area.area_key, 1, radius=30))
        built.control.set_discovery_mode(SettingsRequest(1, intensity="high"))
        built.control.pause()
        built.control.resume()
        built.service.make_due("sandiego_gov")
        batch = built.reader.changes(0)
        self.assertEqual([c.kind for c in batch.changes], ["area.configuration_changed", "area.configuration_changed", "discovery.settings_changed", "discovery.paused", "discovery.resumed"])
        self.assertEqual([c.seq for c in batch.changes], sorted(c.seq for c in batch.changes))
        self.assertEqual({c.entity for c in batch.changes}, {"area", "settings", "discovery"})
        built.close()
        reopened = self.build()
        self.assertEqual(len(reopened.reader.changes(0).changes), 5, "the records are in SQLite, not in memory")

    def test_a_database_from_milestone_2a_is_migrated_without_losing_its_change_log(self):
        store = DiscoveryStore(self.db, migrations=MIGRATIONS[:1]).open()
        with store.write() as tx:
            tx.execute("INSERT INTO changes (entity, entity_key, op, payload_hash, created_at) VALUES ('candidate', 'a:1', 'upsert', ?, 1), ('page', 'k', 'upsert', ?, 2)", ("0" * 64, "1" * 64))
            tx.execute("DELETE FROM changes WHERE seq = 2")  # a pruned sequence number must never be reused
        store.close()
        built = self.build()
        self.assertEqual(built.store.version(), 2)
        batch = built.reader.changes(0)
        self.assertEqual([(c.seq, c.entity, c.kind) for c in batch.changes], [(1, "candidate", "candidate.changed")])
        built.control.pause()
        self.assertEqual(built.reader.changes(0).changes[-1].seq, 3)
        with built.store.read() as connection:
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0], "automatic")


class Scheduling(Fixture):
    def test_jobs_follow_the_configuration_and_need_no_card(self):
        built = self.build()
        self.assertEqual(built.service.run_once().outcome, "nothing_due", "sources no area uses are registered but idle")
        area = built.control.create_area(self.area(sources=("sandiego_gov",))).area
        self.assertEqual(built.service.run_once().outcome, "completed", "an enabled area makes its source eligible at once")
        self.assertEqual(self.count("candidates"), 4)
        self.assertEqual(self.count("jobs"), 2, "one job per registered source")
        built.scheduler.plan()
        self.assertFalse(built.scheduler.plan()["changed"], "nothing changed, so planning does nothing")
        built.control.set_area_enabled(area.area_key, False)
        built.scheduler.plan()
        self.clock.t += 7200
        self.assertEqual(built.service.run_once().outcome, "nothing_due", "a disabled area does not keep its sources crawled")

    def test_overlapping_areas_and_many_cards_do_not_mean_more_crawls(self):
        built = self.build()
        for name in ("North", "South", "Downtown", "Card five"):
            built.control.create_area(self.area(name, sources=("sandiego_gov",)))
        built.scheduler.plan()
        self.assertEqual(built.service.run_until_idle(10)[0].outcome, "completed")
        self.assertEqual(built.client.urls.count(CITY_URL), 1, "the shared source was fetched once")
        self.assertEqual(built.service.run_once().outcome, "nothing_due")
        self.assertEqual(built.client.urls.count(CITY_URL), 1)
        self.assertEqual(self.count("jobs", "state <> 'ready'"), 1)

    def test_a_configuration_change_is_noticed_and_brings_the_next_run_forward(self):
        built = self.build()
        built.control.create_area(self.area())
        built.scheduler.plan()
        built.service.run_once()
        with built.store.read() as connection:
            before = connection.execute("SELECT next_run_at FROM jobs WHERE job_key='crawl_source:sandiego_gov'").fetchone()[0]
        self.assertEqual(before, self.clock() + 3600)
        built.control.set_discovery_mode(SettingsRequest(1, mode="manual", intensity="high"))
        plan = built.scheduler.plan()
        self.assertEqual((plan["changed"], plan["brought_forward"]), (True, 1))
        with built.store.read() as connection:
            after = connection.execute("SELECT next_run_at FROM jobs WHERE job_key='crawl_source:sandiego_gov'").fetchone()[0]
            seen = int(SettingsRepository().get(connection, "scheduler_seen_seq"))
            latest = connection.execute("SELECT MAX(seq) FROM changes WHERE entity IN ('area','settings')").fetchone()[0]
        self.assertEqual(after, self.clock() + 1800)
        self.assertEqual(seen, latest)
        built.control.set_discovery_mode(SettingsRequest(2, intensity="low"))
        built.scheduler.plan()
        with built.store.read() as connection:
            self.assertEqual(connection.execute("SELECT next_run_at FROM jobs WHERE job_key='crawl_source:sandiego_gov'").fetchone()[0], after, "slower pacing never delays a job already scheduled")

    def test_pacing_by_intensity_and_by_how_often_a_source_changes(self):
        pacing = AreaPacing(AreaRepository(), SettingsRepository(), SchedulerConfig())
        self.assertEqual([pacing.interval(3600, i, "manual", 9) for i in ("low", "normal", "high")], [7200, 3600, 1800])
        self.assertEqual([pacing.interval(3600, "normal", "automatic", q) for q in (0, 1, 2, 3, 4, 50)], [3600, 3600, 7200, 7200, 14400, 14400])
        self.assertEqual(pacing.interval(100, "high", "manual", 0), 300, "never faster than the floor")
        self.assertEqual(pacing.interval(86400, "low", "automatic", 9), 86400, "nor slower than a day")
        built = self.build()
        built.control.create_area(self.area())
        built.scheduler.plan()
        waits = []
        for _ in range(5):
            built.service.run_once()
            with built.store.read() as connection:
                waits.append(connection.execute("SELECT next_run_at FROM jobs WHERE job_key='crawl_source:sandiego_gov'").fetchone()[0] - self.clock())
            self.clock.t += 20000
        self.assertEqual(waits, [3600, 3600, 7200, 7200, 14400], "a source that stays unchanged is asked for less often")

    def test_pause_and_resume_survive_a_restart(self):
        built = self.build()
        built.control.create_area(self.area())
        built.scheduler.plan()
        built.control.pause()
        self.assertEqual(built.service.run_once().outcome, "paused")
        built.close()
        restarted = self.build()
        self.assertTrue(restarted.control.get_status().settings.paused)
        self.assertEqual(restarted.service.run_once().outcome, "paused")
        self.assertEqual(self.count("candidates"), 0)
        restarted.control.resume()
        restarted.close()
        again = self.build()
        self.assertFalse(again.control.get_status().settings.paused)
        again.scheduler.plan()
        self.assertEqual(again.service.run_once().outcome, "completed")

    def test_a_stalled_source_does_not_keep_other_sources_from_being_served(self):
        pages = {**city_pages(), **other_pages()}
        pages["https://other.example/events"] = FetchError("other.example: timed out")
        built = self.build(pages)
        built.control.create_area(self.area(sources=("sandiego_gov", "other_city")))
        built.scheduler.plan()
        outcomes = []
        for _ in range(4):
            outcomes.append(built.service.run_once().outcome)
        self.assertEqual(sorted(outcomes[:2]), ["completed", "fetch_failed"])
        self.assertEqual(self.count("candidates"), 4, "the healthy source was stored")
        with built.store.read() as connection:
            stalled = connection.execute("SELECT state, next_run_at - ?, attempts FROM jobs WHERE job_key='crawl_source:other_city'", (self.clock(),)).fetchone()
        self.assertEqual((stalled[0], stalled[1], stalled[2]), ("ready", 900, 1), "the failing source waits with backoff while the others continue")


class Host(Fixture):
    def make_host(self, **config):
        built = self.build(host_config=HostConfig(idle_wait_s=0.05, busy_wait_s=0.01, **config))
        built.control.create_area(self.area(sources=("sandiego_gov", "other_city")))
        return built

    def test_a_cycle_runs_a_bounded_number_of_jobs_so_every_source_gets_a_turn(self):
        built = self.make_host(max_jobs_per_cycle=1)
        first = built.host.run_cycle()
        self.assertEqual((first["jobs"], first["plan"]["changed"]), (1, True))
        second = built.host.run_cycle()
        self.assertEqual(second["jobs"], 1)
        third = built.host.run_cycle()
        self.assertEqual(third["jobs"], 0)
        self.assertEqual(self.count("jobs", "state='done'"), 2)

    def test_the_loop_runs_on_its_own_and_stops_promptly(self):
        built = self.make_host()
        built.host.config = HostConfig(idle_wait_s=30, busy_wait_s=0.01)
        built.host.start()
        deadline = time.time() + 10
        while time.time() < deadline and self.count("candidates") < 6:
            time.sleep(0.05)
        self.assertEqual(self.count("candidates"), 6)
        self.assertEqual(built.host.status()["state"], "running")
        started = time.time()
        self.assertTrue(built.host.stop(timeout=5))
        self.assertLess(time.time() - started, 2, "an idle wait of 30 s does not delay a stop")
        self.assertEqual(built.host.status()["state"], "stopped")

    def test_a_failing_cycle_is_counted_and_does_not_kill_the_host(self):
        built = self.make_host(error_wait_s=0.01, error_wait_cap_s=0.02)
        calls = iter([RuntimeError("boom"), RuntimeError("boom")])

        def flaky(*args, **kwargs):
            error = next(calls, None)
            if error:
                raise error
            return mock.Mock(outcome="nothing_due", job_key=None, error=None)
        with mock.patch.object(built.service, "run_once", side_effect=flaky):
            built.host.start()
            deadline = time.time() + 5
            while time.time() < deadline and built.host.status()["errors"] < 2:
                time.sleep(0.02)
            self.assertGreaterEqual(built.host.status()["errors"], 2)
            time.sleep(0.2)
            self.assertEqual(built.host.status()["state"], "running", "the host kept going after its cycles failed")
            self.assertIn("RuntimeError", built.host.status()["last_error"])
            self.assertTrue(built.host.stop(timeout=5))

    def test_a_job_interrupted_by_a_crash_is_recovered_when_the_host_restarts(self):
        built = self.make_host()
        with built.store.write() as tx:
            built.service.jobs.claim(tx, "crashed", "lost", self.clock(), 600)
        built.close()
        self.clock.t += 601
        restarted = self.build(host_config=HostConfig(idle_wait_s=0.05, busy_wait_s=0.01))
        report = restarted.host.run_cycle()
        self.assertEqual(restarted.control.get_status().jobs.leased, 0)
        self.assertGreaterEqual(report["jobs"], 1)


def request(server, method, path, token=CONTROL_TOKEN, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.address[:2], timeout=10)
    try:
        sent = {"Authorization": f"Bearer {token}"} if token else {}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        connection.request(method, path, body=data, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        return response.status, (json.loads(raw) if raw else None)
    finally:
        connection.close()


class ApiFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.built = self.build()
        self.server = app_module.build_api(self.built, ApiTokens(CONTROL_TOKEN, READ_TOKEN), clock=self.clock, port=0).start()
        self.addCleanup(self.server.stop)

    def call(self, method, path, **kwargs):
        return request(self.server, method, path, **kwargs)


class Api(ApiFixture):
    def test_unauthenticated_and_wrongly_authenticated_requests_are_rejected_without_changing_anything(self):
        for method, path, body in (("GET", "/v1/areas", None), ("POST", "/v1/areas", {"name": "x", "latitude": 1, "longitude": 1, "radius": 1}),
                                   ("POST", "/v1/discovery/pause", {}), ("PUT", "/v1/discovery/settings", {"expected_revision": 1, "mode": "manual"})):
            for n, token in enumerate((None, "wrong" * 10, "")):  # a different client each time, so the failure throttle is not what answers
                headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token is not None else {})}
                status, payload = self.server._api.handle(method, path, headers, json.dumps(body or {}).encode(), f"client-{method}-{path}-{n}")
                self.assertEqual((status, payload["error"]["code"]), (401, "unauthorized"), (method, path))
        self.assertEqual(self.count("areas"), 0)
        self.assertFalse(self.built.control.get_status().settings.paused)

    def test_a_read_token_reads_but_can_never_change_anything(self):
        self.assertEqual(self.call("GET", "/v1/areas", token=READ_TOKEN)[0], 200)
        self.assertEqual(self.call("GET", "/v1/discovery/status", token=READ_TOKEN)[0], 200)
        self.assertEqual(self.call("GET", "/v1/changes", token=READ_TOKEN)[0], 200)
        for method, path, body in (("POST", "/v1/areas", {"name": "x", "latitude": 1, "longitude": 1, "radius": 1}), ("POST", "/v1/discovery/pause", {}),
                                   ("PUT", "/v1/discovery/settings", {"expected_revision": 1}), ("POST", "/v1/service/stop", {})):
            status, payload = self.call(method, path, token=READ_TOKEN, body=body)
            self.assertEqual((status, payload["error"]["code"]), (403, "forbidden"), path)
        self.assertEqual(self.count("areas"), 0)

    def test_repeated_failures_are_rate_limited_and_the_token_is_never_echoed(self):
        for _ in range(5):
            self.assertEqual(self.call("GET", "/v1/areas", token="bad" * 20)[0], 401)
        status, payload = self.call("GET", "/v1/areas", token="bad" * 20)
        self.assertEqual(status, 429)
        self.assertNotIn("bad" * 20, json.dumps(payload))
        self.assertNotIn(CONTROL_TOKEN, json.dumps(self.call("GET", "/v1/areas", token=CONTROL_TOKEN)))

    def test_area_lifecycle_through_the_api_with_revisions_and_idempotent_creation(self):
        body = {"name": "San Diego", "latitude": 32.7157, "longitude": -117.1611, "radius": 25, "sources": ["sandiego_gov"], "request_id": "abc"}
        status, created = self.call("POST", "/v1/areas", body=body)
        self.assertEqual((status, created["revision"], created["replayed"]), (201, 1, False))
        key = created["area"]["area_key"]
        status, replay = self.call("POST", "/v1/areas", body=body)
        self.assertEqual((status, replay["replayed"], replay["area"]["area_key"]), (200, True, key))
        status, patched = self.call("PATCH", f"/v1/areas/{key}", body={"expected_revision": 1, "radius": 30, "intensity": "high"})
        self.assertEqual((status, patched["revision"], patched["area"]["radius"]), (200, 2, 30.0))
        status, conflict = self.call("PATCH", f"/v1/areas/{key}", body={"expected_revision": 1, "radius": 40})
        self.assertEqual((status, conflict["error"]["code"], conflict["error"]["current_revision"]), (409, "revision_conflict", 2))
        self.assertEqual(self.call("GET", f"/v1/areas/{key}")[1]["area"]["radius"], 30.0)
        self.assertEqual(self.call("GET", "/v1/areas/zzzzzzzz")[0], 404)
        status, off = self.call("PATCH", f"/v1/areas/{key}", body={"expected_revision": 2, "enabled": False})
        self.assertEqual((status, off["area"]["enabled"]), (200, False))
        status, stat = self.call("GET", "/v1/discovery/status")
        self.assertEqual((stat["status"]["areas"], stat["status"]["areas_enabled"]), (1, 0))
        status, settings = self.call("PUT", "/v1/discovery/settings", body={"expected_revision": 1, "mode": "manual", "intensity": "low"})
        self.assertEqual((status, settings["settings"]["mode"], settings["settings"]["revision"]), (200, "manual", 2))
        self.assertEqual(self.call("POST", "/v1/discovery/pause")[1]["settings"]["paused"], True)
        self.assertTrue(self.call("POST", "/v1/discovery/pause")[1]["replayed"])
        self.assertEqual(self.call("POST", "/v1/discovery/resume")[1]["settings"]["paused"], False)
        kinds = [c["kind"] for c in self.call("GET", "/v1/changes?after=0")[1]["changes"]]
        self.assertEqual(kinds, ["area.configuration_changed"] * 3 + ["discovery.settings_changed", "discovery.paused", "discovery.resumed"])

    def test_malformed_oversized_and_unexpected_requests_are_rejected(self):
        good = {"name": "x", "latitude": 1, "longitude": 1, "radius": 1}
        cases = [("POST", "/v1/areas", b"{not json", 400), ("POST", "/v1/areas", b"[1, 2]", 400), ("POST", "/v1/areas", {"name": "x"}, 400),
                 ("POST", "/v1/areas", {**good, "extra": 1}, 400), ("POST", "/v1/areas", {**good, "latitude": 999}, 400), ("POST", "/v1/areas", {**good, "sources": ["nope"]}, 404),
                 ("PATCH", "/v1/areas/abcdefgh1", {"radius": 5}, 400), ("PUT", "/v1/discovery/settings", {"mode": "manual"}, 400),
                 ("PUT", "/v1/discovery/settings", {"expected_revision": 1, "mode": "chaos"}, 400), ("GET", "/v1/changes?after=x", None, 400),
                 ("GET", "/v1/nothing", None, 404), ("DELETE", "/v1/areas/abcdefgh1", None, 405), ("GET", "/v1/areas/../../etc/passwd", None, 404),
                 ("POST", "/v1/areas", b"x" * (MAX_BODY + 1), 413), ("GET", "/v1/areas;DROP%20TABLE%20areas", None, 404), ("GET", "/v1/areas?x=1'%20OR%20'1'='1", None, 200)]
        for method, path, body, expected in cases:
            with self.subTest(method=method, path=path[:30], body=str(body)[:30]):
                status, payload = self.call(method, path, body=body) if body is None or len(body if isinstance(body, bytes) else b"") <= MAX_BODY else \
                    (DiscoveryApi.handle(self.server._api, method, path, {"Authorization": f"Bearer {CONTROL_TOKEN}", "Content-Type": "application/json"}, body, "t"))
                self.assertEqual(status, expected, payload)
        status, _ = DiscoveryApi.handle(self.server._api, "POST", "/v1/areas", {"Authorization": f"Bearer {CONTROL_TOKEN}", "Content-Type": "text/plain"}, b'{"a": 1}', "t")
        self.assertEqual(status, 415)
        self.assertEqual(self.count("areas"), 0)
        self.assertEqual(self.count("sources"), 2, "the database is intact")

    def test_an_oversized_declared_body_is_refused_before_it_is_read(self):
        connection = socket.create_connection(self.server.address[:2], timeout=5)
        try:
            connection.sendall(f"POST /v1/areas HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {CONTROL_TOKEN}\r\nContent-Type: application/json\r\n"
                               f"Content-Length: 10000000\r\nConnection: close\r\n\r\n".encode())
            self.assertIn(b" 413 ", connection.recv(200))
        finally:
            connection.close()

    def test_the_server_binds_to_loopback_unless_remote_binding_is_chosen_and_always_needs_tokens(self):
        api = DiscoveryApi(self.built.control, self.built.reader, ApiTokens(CONTROL_TOKEN), self.clock)
        with self.assertRaises(ValueError):
            DiscoveryApiServer(api, host="0.0.0.0")
        DiscoveryApiServer(api, host="0.0.0.0", allow_remote=True)  # allowed only on request; the tokens still apply
        self.assertEqual(self.server.address[0], "127.0.0.1")
        for tokens in (("short",), (CONTROL_TOKEN, CONTROL_TOKEN), (None,)):
            with self.assertRaises((ValueError, AttributeError)):
                ApiTokens(*tokens)

    def test_the_service_can_be_stopped_through_the_api_only_with_the_control_token(self):
        stopped = []
        server = DiscoveryApiServer(DiscoveryApi(self.built.control, self.built.reader, ApiTokens(CONTROL_TOKEN, READ_TOKEN), self.clock, on_stop=lambda: stopped.append(1)), port=0).start()
        self.addCleanup(server.stop)
        self.assertEqual(request(server, "POST", "/v1/service/stop", token=READ_TOKEN, body={})[0], 403)
        self.assertEqual(stopped, [])
        self.assertEqual(request(server, "POST", "/v1/service/stop", body={})[0], 202)
        self.assertEqual(stopped, [1])

    def test_twenty_concurrent_status_and_read_clients_while_discovery_writes(self):
        self.built.control.create_area(self.area(sources=("sandiego_gov", "other_city")))
        self.built.host.config = HostConfig(idle_wait_s=0.02, busy_wait_s=0.01, max_jobs_per_cycle=1)
        self.built.host.start()
        errors, latencies = [], []

        def client(i):
            for n in range(10):
                path = ("/v1/discovery/status", "/v1/areas", "/v1/changes?after=0&limit=50", "/v1/discovery/status")[(i + n) % 4]
                started = time.perf_counter()
                try:
                    status, payload = self.call("GET", path, token=READ_TOKEN if i % 2 else CONTROL_TOKEN)
                    if status != 200 or (path == "/v1/areas" and len(payload["areas"]) != 1):
                        errors.append((i, path, status))
                except Exception as error:  # pragma: no cover
                    errors.append((i, path, repr(error)))
                latencies.append(time.perf_counter() - started)
                self.clock.t += 1

        threads = [threading.Thread(target=client, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        deadline = time.time() + 10
        while time.time() < deadline and self.count("candidates") < 6:
            time.sleep(0.05)
        self.assertTrue(self.built.host.stop(timeout=10))
        self.assertEqual(errors, [])
        self.assertEqual(self.count("candidates"), 6, "discovery kept writing while 20 clients read")
        values = sorted(latencies)
        summary = {"clients": 20, "requests": len(values), "p50_ms": round(values[len(values) // 2] * 1000, 1), "p95_ms": round(values[int(len(values) * 0.95)] * 1000, 1),
                   "write_stats": self.built.store.stats.summary()}
        print("\napi concurrency (simulated, development machine):", json.dumps(summary))
        self.assertEqual(summary["write_stats"]["failures"], 0)


class UrllibSession:
    """An aiohttp-like session for the HA client tests that talks to the real loopback server (the work runs in a thread, off the event loop)."""

    def request(self, method, url, json=None, headers=None):
        return _Call(method, url, json, headers)


class _Call:
    def __init__(self, method, url, body, headers):
        self.method, self.url, self.body, self.headers = method, url, body, headers or {}

    async def __aenter__(self):
        import urllib.error
        import urllib.request

        def send():
            data = None if self.body is None else __import__("json").dumps(self.body).encode()
            req = urllib.request.Request(self.url, data=data, method=self.method, headers={**self.headers, **({"Content-Type": "application/json"} if data else {})})
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()
        self.status, self.raw = await asyncio.to_thread(send)
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def status_code(self):
        return self.status

    async def json(self, content_type=None):
        return __import__("json").loads(self.raw.decode())


class _Resp:
    pass


def _patch_status(call):
    call.status = call.status


class HomeAssistantClient(ApiFixture):
    """The async client for the control API, with the contract's errors mapped to typed exceptions (simulated session; no Home Assistant)."""

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def client(self, token=CONTROL_TOKEN, port=None):
        module = load_ha_client()
        host, real_port = self.server.address[:2]
        return module, module.DiscoveryControlClient(UrllibSession(), f"http://{host}:{port or real_port}", token)

    def test_status_areas_changes_and_commands_round_trip_without_blocking_the_event_loop(self):
        module, client = self.client()

        async def scenario():
            ticks = []

            async def heartbeat():  # the event loop keeps running while requests are in flight
                for _ in range(20):
                    ticks.append(1)
                    await asyncio.sleep(0.005)

            beat = asyncio.create_task(heartbeat())
            created = await client.create_area("Home", 32.7, -117.1, 10, sources=["sandiego_gov"], request_id="ha-1")
            key = created["area"]["area_key"]
            again = await client.create_area("Home", 32.7, -117.1, 10, sources=["sandiego_gov"], request_id="ha-1")
            updated = await client.update_area(key, 1, radius=15)
            with self.assertRaises(module.DiscoveryConflictError) as conflict:
                await client.update_area(key, 1, radius=20)
            status = await client.status()
            areas = await client.areas()
            settings = await client.set_settings(status["settings"]["revision"], mode="manual", intensity="high")
            paused = await client.pause()
            resumed = await client.resume()
            feed = await client.changes(0, 50)
            one = await client.area(key)
            await beat
            return created, again, updated, conflict.exception, status, areas, settings, paused, resumed, feed, one, len(ticks)

        created, again, updated, conflict, status, areas, settings, paused, resumed, feed, one, ticks = self.run_async(scenario())
        self.assertEqual((created["replayed"], again["replayed"], updated["revision"], conflict.current_revision), (False, True, 2, 2))
        self.assertEqual((status["areas"], len(areas), settings["settings"]["mode"], paused["settings"]["paused"], resumed["settings"]["paused"]), (1, 1, "manual", True, False))
        self.assertEqual([c["kind"] for c in feed["changes"]][-3:], ["discovery.settings_changed", "discovery.paused", "discovery.resumed"])
        self.assertEqual(one["radius"], 15.0)
        self.assertGreater(ticks, 3)

    def test_failures_become_typed_errors_and_the_token_never_appears_in_them(self):
        module, wrong = self.client(token="x" * 40)
        _, read_only = self.client(token=READ_TOKEN)
        _, nowhere = self.client(port=1)

        async def scenario():
            errors = {}
            for name, call in (("wrong", wrong.status()), ("read_only", read_only.pause()), ("bad_request", self.client()[1].create_area("x", 1, 1, 0)),
                               ("missing", self.client()[1].area("zzzzzzzz")), ("down", nowhere.status())):
                try:
                    await call
                except module.DiscoveryError as error:
                    errors[name] = error
            return errors

        errors = self.run_async(scenario())
        self.assertIsInstance(errors["wrong"], module.DiscoveryAuthError)
        self.assertEqual((errors["wrong"].status, errors["read_only"].status), (401, 403))
        self.assertIsInstance(errors["bad_request"], module.DiscoveryRequestError)
        self.assertEqual((errors["bad_request"].status, errors["missing"].status), (400, 404))
        self.assertIsInstance(errors["down"], module.DiscoveryConnectionError)
        for error in errors.values():
            for secret in (CONTROL_TOKEN, READ_TOKEN, "x" * 40):
                self.assertNotIn(secret, str(error))

    def test_the_address_is_validated(self):
        module = load_ha_client()
        self.assertEqual(module.normalize_base_url("http://Cubi.local:8780/"), "http://cubi.local:8780")
        for bad in ("ftp://x", "http://u:p@x", "http://x?y=1", "cubi.local:8780", ""):
            with self.assertRaises(ValueError):
                module.normalize_base_url(bad)


class EndToEnd(Fixture):
    """An independent discovery host, controlled only through its API; a stand-in for the Cubi side. No card, no Home Assistant."""

    def start(self, clock=time.time):
        built = app_module.build(self.db, clock=clock, fetch_client=FakeClient({**city_pages()}), host_config=HostConfig(idle_wait_s=0.05, busy_wait_s=0.01))
        built.register_known_sources()
        server = app_module.build_api(built, ApiTokens(CONTROL_TOKEN, READ_TOKEN), clock=clock, port=0).start()
        built.host.start()
        return built, server

    def stop(self, built, server):
        server.stop()
        built.host.stop(timeout=10)
        built.close()

    def wait_for(self, predicate, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        return predicate()

    def test_create_an_area_observe_discovery_change_it_restart_and_find_everything_persisted(self):
        built, server = self.start()
        try:
            self.assertEqual(self.count("candidates"), 0)
            status, created = request(server, "POST", "/v1/areas", body={"name": "San Diego", "latitude": 32.7157, "longitude": -117.1611, "radius": 25,
                                                                          "sources": ["sandiego_gov"], "request_id": "e2e"})
            self.assertEqual(status, 201)
            key = created["area"]["area_key"]
            self.assertTrue(self.wait_for(lambda: self.count("candidates") == 4), "discovery ran with no card involved")
            self.assertEqual(self.count("pages"), 6)
            self.assertGreaterEqual(self.count("fetches"), 5)
            with built.store.read() as connection:
                first_next = connection.execute("SELECT next_run_at FROM jobs WHERE job_key='crawl_source:sandiego_gov'").fetchone()[0]
            status, settings = request(server, "PUT", "/v1/discovery/settings", body={"expected_revision": 1, "mode": "manual", "intensity": "high"})
            self.assertEqual(status, 200)
            self.assertEqual(request(server, "PATCH", f"/v1/areas/{key}", body={"expected_revision": 1, "radius": 40})[0], 200)
            moved = self.wait_for(lambda: self.count("settings", "key='scheduler_seen_seq' AND CAST(value AS INTEGER) >= 3"))
            self.assertTrue(moved, "the scheduler noticed the configuration change")
            with built.store.read() as connection:
                second_next = connection.execute("SELECT next_run_at FROM jobs WHERE job_key='crawl_source:sandiego_gov'").fetchone()[0]
            self.assertLess(second_next, first_next, "high intensity brought the next run forward")
            kinds = [c["kind"] for c in request(server, "GET", "/v1/changes?after=0&limit=200", token=READ_TOKEN)[1]["changes"]]
            self.assertEqual(kinds.count("area.configuration_changed"), 2)
            self.assertIn("discovery.settings_changed", kinds)
            self.assertEqual(kinds.count("candidate.changed"), 4)
            self.assertEqual(request(server, "POST", "/v1/discovery/pause")[0], 200)
            before = request(server, "GET", "/v1/changes?after=0&limit=500")[1]["latest_seq"]
        finally:
            self.stop(built, server)
        built2, server2 = self.start()
        try:
            status, area = request(server2, "GET", f"/v1/areas/{key}")
            self.assertEqual((status, area["area"]["radius"], area["area"]["revision"], area["area"]["sources"]), (200, 40.0, 2, ["sandiego_gov"]))
            state = request(server2, "GET", "/v1/discovery/status")[1]["status"]
            self.assertEqual((state["settings"]["mode"], state["settings"]["intensity"], state["settings"]["paused"]), ("manual", "high", True))
            self.assertEqual(state["jobs"]["done"], 1, "the job and its history persisted")
            self.assertEqual(request(server2, "GET", "/v1/changes?after=0&limit=500")[1]["latest_seq"], before)
            self.assertEqual(self.count("candidates"), 4)
            self.assertEqual(request(server2, "POST", "/v1/discovery/resume")[0], 200)
            self.assertEqual(built2.reader.candidates(CandidateQuery(limit=10))[0].source_key, "sandiego_gov")
        finally:
            self.stop(built2, server2)


class NoGlobalState(unittest.TestCase):
    def test_the_new_modules_hold_no_mutable_module_level_state(self):
        from tests.test_discovery_service import NoGlobals
        NoGlobals("test_the_service_package").test_the_service_package()


if __name__ == "__main__":
    unittest.main()
