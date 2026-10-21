"""Token handling and the Supervisor discovery announcement (against a stand-in Supervisor)."""
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from discovery import server as server_module
from discovery import supervisor

SUPERVISOR_TOKEN = "supervisor-secret"


class FakeSupervisor:
    """Answers /addons/self/info and /discovery like the Supervisor; can be told to lack the older paths."""

    def __init__(self, old_api=True, hostname="abc12345-area-events", forbid_discovery=False):
        self.requests, self.old_api, self.hostname, self.forbid = [], old_api, hostname, forbid_discovery
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append((self.command, self.path, self.headers.get("Authorization"), body))
                if self.headers.get("Authorization") != f"Bearer {SUPERVISOR_TOKEN}":
                    return self._reply(401, {"result": "error"})
                if self.path == "/addons/self/info" and outer.old_api:
                    return self._reply(200, {"result": "ok", "data": {"hostname": outer.hostname}})
                if self.path == "/v2/apps/self/info":
                    return self._reply(200, {"result": "ok", "data": {"hostname": outer.hostname}})
                if self.path == "/discovery" and outer.old_api:
                    if outer.forbid:
                        return self._reply(403, {"result": "error", "message": "Apps must list services they provide via discovery in their config!"})
                    return self._reply(200, {"result": "ok", "data": {"uuid": "0" * 32}})
                if self.path == "/v2/discovery":
                    return self._reply(200, {"result": "ok", "data": {"uuid": "0" * 32}})
                return self._reply(404, {"result": "error"})

            do_GET = do_POST = _handle

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Announce(unittest.TestCase):
    def fake(self, **options):
        fake = FakeSupervisor(**options)
        self.addCleanup(fake.close)
        return fake

    def test_the_integration_is_told_the_hostname_port_and_token(self):
        fake = self.fake()
        self.assertEqual(supervisor.announce(8765, "t" * 43, SUPERVISOR_TOKEN, fake.base), "abc12345-area-events")
        method, path, auth, body = fake.requests[-1]
        self.assertEqual((method, path, auth), ("POST", "/discovery", f"Bearer {SUPERVISOR_TOKEN}"))
        self.assertEqual(body, {"service": "area_events", "config": {"host": "abc12345-area-events", "port": 8765, "token": "t" * 43}})

    def test_the_newer_api_is_used_when_the_older_one_is_gone(self):
        fake = self.fake(old_api=False)
        supervisor.announce(8765, "t" * 43, SUPERVISOR_TOKEN, fake.base)
        self.assertEqual([r[1] for r in fake.requests], ["/addons/self/info", "/v2/apps/self/info", "/discovery", "/v2/discovery"])

    def test_a_refused_announcement_is_an_error_not_a_crash(self):
        fake = self.fake(forbid_discovery=True)
        with self.assertRaises(supervisor.AnnounceError):
            supervisor.announce(8765, "t" * 43, "wrong-token", fake.base)
        self.assertFalse(supervisor.announce_with_retries(8765, "t" * 43, "wrong-token", threading.Event(), fake.base, attempts=2, delay=0))

    def test_retries_stop_when_the_app_is_stopping(self):
        stop = threading.Event()
        stop.set()
        calls = []
        original = supervisor.announce
        supervisor.announce = lambda *args: calls.append(args) or (_ for _ in ()).throw(supervisor.AnnounceError("down"))
        try:
            self.assertFalse(supervisor.announce_with_retries(8765, "t" * 43, "x", stop, attempts=5, delay=60))
        finally:
            supervisor.announce = original
        self.assertEqual(len(calls), 1)

    def test_the_config_yaml_lists_the_service_the_supervisor_requires(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "area_events", "config.yaml")
        with open(root, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(f"discovery:\n  - {supervisor.SERVICE}\n", text)


class Token(unittest.TestCase):
    def settings(self, folder, token=""):
        return {"token": token, "data_dir": folder}

    def test_a_token_is_generated_once_kept_private_and_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            first = server_module.resolve_token(self.settings(folder))
            self.assertGreaterEqual(len(first), server_module.MIN_TOKEN_LENGTH)
            self.assertEqual(server_module.resolve_token(self.settings(folder)), first)
            mode = stat.S_IMODE(os.stat(os.path.join(folder, "token")).st_mode)
            if os.name != "nt":
                self.assertEqual(mode & 0o077, 0)

    def test_a_token_set_by_hand_wins_but_must_be_long_enough(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(server_module.resolve_token(self.settings(folder, "h" * 40)), "h" * 40)
            self.assertFalse(os.path.exists(os.path.join(folder, "token")))
            with self.assertRaises(ValueError):
                server_module.resolve_token(self.settings(folder, "short"))

    def test_a_damaged_token_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "token"), "w") as handle:
                handle.write("short")
            self.assertGreaterEqual(len(server_module.resolve_token(self.settings(folder))), server_module.MIN_TOKEN_LENGTH)


if __name__ == "__main__":
    unittest.main()
