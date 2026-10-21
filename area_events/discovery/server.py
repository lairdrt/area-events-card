"""The engine's small HTTP API, used only by the Area Events integration inside Home Assistant.

    GET  /health                 liveness for the Supervisor watchdog (no authentication, reveals nothing)
    GET  /api/v1/status          engine counters
    POST /api/v1/profiles        register a profile (idempotent) -> {"id": ...}
    GET  /api/v1/profiles/<id>   status, location, sources and events of a profile; ?since=<generated_at> omits
                                 the events when they have not changed

Everything under /api/v1 needs `Authorization: Bearer <token>`. The token is the app's `token` option; without a
token of at least 32 characters the app refuses to start. Responses carry no CORS headers: only the integration
(server side) calls this API, never a browser.

Settings come from /data/options.json (Home Assistant app options) and, for running outside the Supervisor,
from the environment: AREA_EVENTS_TOKEN, AREA_EVENTS_REFRESH_MINUTES, AREA_EVENTS_DATA_DIR, AREA_EVENTS_PORT,
AREA_EVENTS_HOST, AREA_EVENTS_LOG_LEVEL.
"""
import hmac
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .engine import Engine, EngineError
from .profiles import ProfileError

log = logging.getLogger("area-events")

MAX_BODY = 8192
MIN_TOKEN_LENGTH = 32
AUTH_FAILURES = 5
AUTH_WINDOW = 60
PROFILE_ROUTE = "/api/v1/profiles"


class ApiError(Exception):
    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers or {}


class Api:
    def __init__(self, engine, token, clock=time.time):
        self.engine, self.token, self.clock = engine, token.encode(), clock
        self._failures = {}

    def _authenticate(self, header, client):
        now = self.clock()
        recent = [t for t in self._failures.get(client, []) if now - t < AUTH_WINDOW]
        self._failures[client] = recent
        if len(self._failures) > 1000:
            self._failures = {c: v for c, v in self._failures.items() if v}
        if len(recent) >= AUTH_FAILURES:
            raise ApiError(429, "too many failed attempts", {"Retry-After": str(max(AUTH_WINDOW - int(now - min(recent)), 1))})
        supplied = header[7:].strip().encode() if isinstance(header, str) and header[:7].lower() == "bearer " else b""
        if not hmac.compare_digest(supplied, self.token):
            self._failures.setdefault(client, []).append(now)
            raise ApiError(401, "a valid token is required", {"WWW-Authenticate": "Bearer"})

    def handle(self, method, target, headers, body, client):
        """(status, payload dict). Never raises."""
        try:
            parts = urlsplit(target)
            path = parts.path
            if method == "GET" and path == "/health":
                return 200, {"status": "ok"}
            if not path.startswith("/api/v1/"):
                raise ApiError(404, "not found")
            self._authenticate(headers.get("Authorization"), client)
            if method == "GET" and path == "/api/v1/status":
                return 200, self.engine.status()
            if method == "POST" and path == PROFILE_ROUTE:
                try:
                    request = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as error:
                    raise ApiError(400, "the request body is not valid JSON") from error
                pid = self.engine.register(request)
                return 200, self.engine.snapshot(pid, with_events=False)
            if method == "GET" and path.startswith(PROFILE_ROUTE + "/"):
                since = (parse_qs(parts.query).get("since") or [None])[0]
                return 200, self.engine.snapshot(path[len(PROFILE_ROUTE) + 1:], since=since)
            raise ApiError(404 if method in ("GET", "POST") else 405, "not found" if method in ("GET", "POST") else "method not allowed")
        except ApiError as error:
            return error.status, {"error": error.message, "_headers": error.headers}
        except ProfileError as error:
            return 422, {"error": str(error)}
        except EngineError as error:
            return error.status, {"error": error.message}
        except Exception:
            log.exception("request failed")
            return 500, {"error": "internal error"}


class Handler(BaseHTTPRequestHandler):
    server_version = "area-events"
    sys_version = ""
    timeout = 10

    def _handle(self):
        length = self.headers.get("Content-Length", "0")
        if self.headers.get("Transfer-Encoding") or not length.isdigit():
            return self._send(411, {"error": "Content-Length is required"})
        if int(length) > MAX_BODY:
            return self._send(413, {"error": "request body too large"})
        body = self.rfile.read(int(length)) if int(length) else b""
        status, payload = self.server.api.handle(self.command, self.path, self.headers, body, self.client_address[0])
        self._send(status, payload)

    def _send(self, status, payload):
        extra = payload.pop("_headers", {}) if isinstance(payload, dict) else {}
        raw = (json.dumps(payload) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

    def log_message(self, fmt, *args):
        log.debug("http %s", fmt % args)


def make_server(api, host, port):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.api = api
    return server


def load_settings(environ=None, options_path="/data/options.json"):
    """Settings from the app options file, overridden by the environment."""
    environ = os.environ if environ is None else environ
    options = {}
    try:
        with open(options_path, encoding="utf-8") as handle:
            options = json.load(handle)
    except (OSError, ValueError):
        pass
    return {
        "token": str(environ.get("AREA_EVENTS_TOKEN") or options.get("token") or "").strip(),
        "refresh_minutes": int(environ.get("AREA_EVENTS_REFRESH_MINUTES") or options.get("refresh_minutes") or 60),
        "log_level": str(environ.get("AREA_EVENTS_LOG_LEVEL") or options.get("log_level") or "info").upper(),
        "data_dir": environ.get("AREA_EVENTS_DATA_DIR", "/data"),
        "host": environ.get("AREA_EVENTS_HOST", "0.0.0.0"),
        "port": int(environ.get("AREA_EVENTS_PORT", "8765")),
    }


def main():
    settings = load_settings()
    logging.basicConfig(stream=sys.__stdout__, level=getattr(logging, settings["log_level"], logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    if len(settings["token"]) < MIN_TOKEN_LENGTH:
        log.error("the 'token' option must be at least %d characters. Set it in the app configuration and use the same value "
                  "in the Area Events integration. Not starting.", MIN_TOKEN_LENGTH)
        sys.exit(1)
    engine = Engine(settings["data_dir"], interval=settings["refresh_minutes"] * 60)
    server = make_server(Api(engine, settings["token"]), settings["host"], settings["port"])
    stop = threading.Event()

    def shutdown(*_):
        stop.set()
        engine.wake()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=engine.run_forever, args=(stop,), daemon=True).start()
    log.info("Area Events engine listening on %s:%s, refreshing every %s minutes, data in %s",
             settings["host"], settings["port"], engine.interval // 60, settings["data_dir"])
    server.serve_forever()
    log.info("stopped")


if __name__ == "__main__":
    main()
