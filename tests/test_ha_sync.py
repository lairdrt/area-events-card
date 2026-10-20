"""Local tests for homeassistant/sync_area_events.py.

These run on the development machine against a local HTTP server. They do NOT
prove the script works inside Home Assistant OS; see docs/home-assistant-sync.md
for the runtime verification steps.
"""
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from discovery.publish import FeedError, validate_feed

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "homeassistant", "sync_area_events.py")
spec = importlib.util.spec_from_file_location("sync_area_events", SCRIPT)
sync_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_module)


def feed(n=2, generated="2030-01-01T00:00:00-08:00"):
    return {"generated_at": generated, "events": [
        {"id": f"x:{i}", "title": "T", "start": "2030-01-01T00:00:00-08:00", "url": "https://x.example"} for i in range(n)]}


class Server:
    """Serves one configurable response."""

    def __init__(self):
        self.status, self.body = 200, b""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *_):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/events.json"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class SyncCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.destination = os.path.join(self.folder, "events.json")
        self.server = Server()
        self.addCleanup(self.server.close)

    def serve(self, payload, status=200):
        self.server.status = status
        self.server.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def run_sync(self, url=None):
        return sync_module.main(["x", url or self.server.url, self.destination])

    def published(self):
        with open(self.destination, encoding="utf-8") as handle:
            return json.load(handle)

    def seed_good(self):
        self.serve(feed(3))
        self.assertEqual(self.run_sync(), 0)
        return open(self.destination, "rb").read()


class Publishing(SyncCase):
    def test_valid_feed_is_published_and_unchanged_feed_is_not_rewritten(self):
        self.serve(feed(2))
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(len(self.published()["events"]), 2)
        self.assertEqual(os.listdir(self.folder), ["events.json"])  # temp file removed
        mtime = os.stat(self.destination).st_mtime_ns
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(os.stat(self.destination).st_mtime_ns, mtime)

    def test_new_content_replaces_old(self):
        self.seed_good()
        self.serve(feed(5))
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(len(self.published()["events"]), 5)


class FailuresKeepPreviousFeed(SyncCase):
    def assert_fails_and_keeps(self, good):
        self.assertEqual(self.run_sync(), 1)
        self.assertEqual(open(self.destination, "rb").read(), good)
        self.assertEqual(os.listdir(self.folder), ["events.json"])

    def test_bad_payloads_are_rejected(self):
        good = self.seed_good()
        bad_payloads = [
            b"", b"{not json", b"[]", b"null", json.dumps({"generated_at": "x", "events": []}).encode(),
            json.dumps({"events": [{"id": "1"}]}).encode(),
            json.dumps({"events": [{"id": "1", "title": "t", "start": "s", "url": "u"}]}).encode(),  # no generated_at
            json.dumps({"generated_at": "x", "events": [{"id": "1", "title": "t", "start": "s"}]}).encode(),  # no url
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload[:40]):
                self.serve(payload)
                self.assert_fails_and_keeps(good)

    def test_http_error_status_keeps_feed(self):
        good = self.seed_good()
        self.serve(b'{"error": "no valid feed available yet"}', status=503)
        self.assert_fails_and_keeps(good)

    def test_unreachable_server_keeps_feed(self):
        good = self.seed_good()
        self.server.close()
        self.assert_fails_and_keeps(good)

    def test_missing_destination_folder_is_an_error_not_created(self):
        self.serve(feed())
        missing = os.path.join(self.folder, "nope", "events.json")
        self.assertEqual(sync_module.main(["x", self.server.url, missing]), 1)
        self.assertFalse(os.path.exists(os.path.dirname(missing)))

    def test_oversized_download_is_rejected(self):
        good = self.seed_good()
        self.serve(b" " * (sync_module.MAX_BYTES + 10))
        self.assert_fails_and_keeps(good)


class ValidatorAgreesWithDiscovery(unittest.TestCase):
    """Guards against the standalone validator drifting from the service's."""

    def test_same_verdicts(self):
        cases = [feed(1), {"generated_at": "x", "events": []}, {"events": None}, [], {"events": [{"id": "1"}]},
                 {"generated_at": "x", "events": [{"id": "1", "title": "t", "start": "s", "url": "u"}]}]
        for case in cases:
            def verdict(check, error):
                try:
                    check(case)
                    return True
                except error:
                    return False
            self.assertEqual(verdict(sync_module.validate, sync_module.SyncError), verdict(validate_feed, FeedError), case)


if __name__ == "__main__":
    unittest.main()
