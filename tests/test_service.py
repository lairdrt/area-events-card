import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from discovery import service as service_module
from discovery.publish import write_atomic
from discovery.service import Service, make_server


def feed(generated=None, sources=None, events=2):
    generated = generated or datetime.now(timezone.utc)
    return {
        "generated_at": generated.isoformat(timespec="seconds"),
        "sources": sources if sources is not None else {"a": {"ok": True, "error": None, "events": events}},
        "events": [{"id": f"x:{i}", "title": "T", "start": "2030-01-01T00:00:00-08:00", "url": "https://x.example"} for i in range(events)],
    }


class Clock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()

    def __call__(self):
        return self.now


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.clock = Clock()
        self.calls = []
        self.service = Service(self.folder, config_path=os.path.join(self.folder, "missing.json"), interval=3600,
                               refresh_fn=self.fake_refresh, clock=self.clock)
        self.outcomes = []
        self.write_config()

    def fake_refresh(self, config, options, output, cache_path, lock_path):
        self.calls.append(output)
        outcome = self.outcomes.pop(0) if self.outcomes else ("write", feed(datetime.fromtimestamp(self.clock.now, timezone.utc)))
        if outcome == "crash":
            raise RuntimeError("boom")
        if isinstance(outcome, int):
            return outcome
        write_atomic(output, json.dumps(outcome[1]))
        return 0

    def write_config(self):
        path = os.path.join(self.folder, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"sources": []}, handle)
        self.service.config_path = path


class Http(ServiceCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.service, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, path, method="GET"):
        request = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_events_json_serves_the_valid_feed(self):
        write_atomic(self.service.feed_path, json.dumps(feed(datetime.fromtimestamp(self.clock.now, timezone.utc))))
        status, headers, body = self.request("/events.json?ts=123")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["events"]), 2)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")

    def test_no_feed_yet_is_503_not_an_empty_feed(self):
        status, _, body = self.request("/events.json")
        self.assertEqual(status, 503)
        self.assertNotIn("events", json.loads(body))

    def test_corrupt_or_empty_file_on_disk_never_replaces_the_good_feed(self):
        write_atomic(self.service.feed_path, json.dumps(feed(datetime.fromtimestamp(self.clock.now, timezone.utc))))
        self.assertEqual(self.request("/events.json")[0], 200)
        for bad in ("{not json", json.dumps({"generated_at": "x", "events": []}), ""):
            with open(self.service.feed_path, "w", encoding="utf-8") as handle:
                handle.write(bad)
            os.utime(self.service.feed_path, None)
            status, _, body = self.request("/events.json")
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(body)["events"]), 2)

    def test_read_only_and_unknown_paths(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            status, headers, _ = self.request("/events.json", method)
            self.assertEqual(status, 405)
            self.assertIn("GET", headers["Allow"])
        self.assertEqual(self.request("/nope")[0], 404)
        self.assertEqual(self.request("/events.json", "OPTIONS")[0], 204)

    def test_head_has_no_body(self):
        write_atomic(self.service.feed_path, json.dumps(feed(datetime.fromtimestamp(self.clock.now, timezone.utc))))
        status, headers, body = self.request("/events.json", "HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(headers["Content-Length"]), 0)

    def test_health_endpoint_reports_json(self):
        status, _, body = self.request("/health")
        payload = json.loads(body)
        self.assertEqual(status, 200)  # no feed yet but just started
        self.assertEqual(payload["status"], "starting")
        self.assertTrue(payload["service"]["running"])


class Scheduling(ServiceCase):
    def test_runs_at_startup_then_each_interval_without_overlap(self):
        stop = threading.Event()
        waits = []

        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                stop.set()
            return stop.is_set()

        stop.wait = wait  # replace sleeping with a recorder
        self.service.run_forever(stop)
        self.assertEqual(len(self.calls), 3)  # startup run + two more
        self.assertEqual(waits, [3600, 3600, 3600])

    def test_overlapping_run_is_skipped(self):
        inside = []

        def reentrant(config, options, output, cache_path, lock_path):
            inside.append(self.service.run_once())  # a second refresh while one is running
            write_atomic(output, json.dumps(feed(datetime.fromtimestamp(self.clock.now, timezone.utc))))
            return 0

        self.service.refresh_fn = reentrant
        self.assertEqual(self.service.run_once(), 0)
        self.assertEqual(inside, [None])
        self.assertFalse(self.service.running)


class Recovery(ServiceCase):
    def state(self):
        return self.service.health()

    def test_failure_keeps_serving_last_feed_and_reports_degraded_then_recovers(self):
        self.assertEqual(self.service.run_once(), 0)
        self.assertEqual(self.state()[0], "ok")
        good = self.service.read_feed()[0]

        for outcome in ("crash", 1):
            self.outcomes.append(outcome)
            self.service.run_once()
            self.assertEqual(self.service.read_feed()[0], good)  # still the last valid feed
            state, payload = self.state()
            self.assertEqual(state, "degraded")
            self.assertNotEqual(payload["discovery"]["last_exit_code"], 0)
            self.assertTrue(payload["discovery"]["last_success_at"])

        self.clock.now += 3600
        self.service.run_once()  # recovers
        self.assertEqual(self.state()[0], "ok")

    def test_source_errors_are_reported(self):
        sources = {"a": {"ok": True, "error": None}, "b": {"ok": False, "error": "site down", "stale": False}}
        self.outcomes.append(("write", feed(datetime.fromtimestamp(self.clock.now, timezone.utc), sources=sources)))
        self.service.run_once()
        state, payload = self.state()
        self.assertEqual((state, payload["source_errors"]), ("degraded", ["b"]))
        self.assertEqual(payload["sources"]["b"]["error"], "site down")

    def test_feed_age_and_staleness(self):
        self.service.run_once()
        self.clock.now += 7200
        state, payload = self.state()
        self.assertEqual((state, payload["feed"]["age_seconds"], payload["feed"]["stale"]), ("ok", 7200, False))
        self.clock.now += 4 * 3600  # older than three intervals
        state, payload = self.state()
        self.assertEqual((state, payload["feed"]["stale"]), ("unhealthy", True))

    def test_no_feed_after_grace_period_is_unhealthy(self):
        self.outcomes.append(1)
        self.service.run_once()
        self.assertEqual(self.state()[0], "starting")
        self.clock.now += service_module.STARTUP_GRACE_SECONDS + 1
        self.assertEqual(self.state()[0], "unhealthy")

    def test_status_survives_restart(self):
        self.service.run_once()
        restarted = Service(self.folder, interval=3600, refresh_fn=self.fake_refresh, clock=self.clock)
        self.assertEqual(restarted.status["last_success_at"], self.service.status["last_success_at"])
        self.assertEqual(restarted.health()[1]["feed"]["events"], 2)  # feed persisted on the data volume


if __name__ == "__main__":
    unittest.main()
