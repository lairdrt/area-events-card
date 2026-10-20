"""Long-running service: hourly discovery plus a read-only HTTP feed.

    GET /events.json   the most recent valid feed
    GET /health        service, discovery, feed-age and per-source status

Configuration comes from environment variables (all optional):
    AREA_EVENTS_DATA_DIR          /data
    AREA_EVENTS_CONFIG            discovery.config.json next to the package
    AREA_EVENTS_INTERVAL_SECONDS  3600 (minimum 300)
    AREA_EVENTS_PORT              8080
    AREA_EVENTS_HOST              0.0.0.0 (container-internal; Compose decides what the host exposes)
"""
import contextlib
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .api import MAX_BODY, ROUTE, Api, load_api_keys
from .locationctl import LocationController, effective_config
from .publish import FeedError, validate_feed, write_atomic
from .refresh import CONFIG_FILE, load_config, refresh

log = logging.getLogger("area-events")
log.addHandler(logging.NullHandler())  # no stderr fallback: stderr is captured into this logger during refreshes

MIN_INTERVAL = 300
STARTUP_GRACE_SECONDS = 20 * 60


class _LogWriter:
    """File-like object that forwards complete lines to the logger."""

    def __init__(self, level):
        self.level, self.buffer = level, ""

    def write(self, text):
        self.buffer += text
        *lines, self.buffer = self.buffer.split("\n")
        for line in lines:
            if line.strip():
                log.log(self.level, line.rstrip())
        return len(text)

    def flush(self):
        pass


class Service:
    def __init__(self, data_dir, config_path=CONFIG_FILE, interval=3600, refresh_fn=refresh, clock=time.time, slot=None):
        self.slot = slot or threading.Lock()  # shared with location changes: one discovery job at a time
        self.data_dir = data_dir
        self.config_path = config_path
        self.interval = interval
        self.refresh_fn = refresh_fn
        self.clock = clock
        self.started = clock()
        self.feed_path = os.path.join(data_dir, "events.json")
        self.status_path = os.path.join(data_dir, "status.json")
        self._run_lock = threading.Lock()
        self._feed_lock = threading.Lock()
        self._feed = None  # (mtime_ns, raw bytes, document) of the last valid feed
        self.running = False
        self.next_run_at = None
        self.status = self._load_status()

    # --- persisted run status ------------------------------------------------

    def _load_status(self):
        try:
            with open(self.status_path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def _save_status(self):
        try:
            write_atomic(self.status_path, json.dumps(self.status, indent=2) + "\n")
        except OSError as error:
            log.error("could not save status: %s", error)

    # --- discovery -----------------------------------------------------------

    def run_once(self):
        """One refresh. Returns its exit code, or None if one is already running."""
        if not self._run_lock.acquire(blocking=False):
            log.warning("refresh skipped: previous refresh still running")
            return None
        started = self.clock()
        self.running = True
        code = 1
        try:
            log.info("refresh starting")
            options = {"interval": 2.0}
            with self.slot, contextlib.redirect_stdout(_LogWriter(logging.INFO)), contextlib.redirect_stderr(_LogWriter(logging.WARNING)):
                code = self.refresh_fn(
                    effective_config(load_config(self.config_path), self.data_dir), options,
                    output=self.feed_path,
                    cache_path=os.path.join(self.data_dir, "source-cache.json"),
                    lock_path=os.path.join(self.data_dir, ".refresh.lock"),
                )
        except Exception:  # config errors, unexpected adapter bugs: log and keep serving old data
            log.error("refresh crashed:\n%s", traceback.format_exc())
            code = 1
        finally:
            self.running = False
            self._run_lock.release()
        finished = self.clock()
        self.status.update(
            last_attempt_at=_iso(finished), last_exit_code=code, last_duration_seconds=round(finished - started, 1),
        )
        if code in (0, 2):
            self.status["last_success_at"] = _iso(finished)
        self._save_status()
        log.info("refresh finished with exit code %s in %.0fs", code, finished - started)
        return code

    def run_forever(self, stop):
        """Refresh on startup, then again `interval` seconds after each refresh ends (never overlapping)."""
        while not stop.is_set():
            self.run_once()
            self.next_run_at = self.clock() + self.interval
            stop.wait(self.interval)

    # --- feed ----------------------------------------------------------------

    def read_feed(self):
        """(raw bytes, document) of the newest valid feed, or None. Invalid files never replace a good copy."""
        with self._feed_lock:
            try:
                mtime = os.stat(self.feed_path).st_mtime_ns
            except OSError:
                return self._feed and self._feed[1:]
            if self._feed and self._feed[0] == mtime:
                return self._feed[1:]
            try:
                with open(self.feed_path, "rb") as handle:
                    raw = handle.read()
                document = json.loads(raw)
                validate_feed(document)
            except (OSError, ValueError, FeedError) as error:
                log.error("feed on disk is not valid (%s); serving the last valid copy", error)
                return self._feed and self._feed[1:]
            self._feed = (mtime, raw, document)
            return raw, document

    # --- health --------------------------------------------------------------

    def health(self):
        now = self.clock()
        feed = self.read_feed()
        document = feed[1] if feed else None
        stale_after = 3 * self.interval

        feed_info = {"present": bool(document), "events": 0, "generated_at": None, "age_seconds": None, "stale": None}
        if document:
            generated = datetime.fromisoformat(document["generated_at"]).timestamp()
            age = max(0, int(now - generated))
            feed_info.update(events=len(document["events"]), generated_at=document["generated_at"],
                             age_seconds=age, stale=age > stale_after)
        sources = {}
        for key, info in ((document or {}).get("sources") or {}).items():
            sources[key] = {k: info.get(k) for k in ("ok", "error", "stale", "last_success_at", "last_attempt_at", "events")}
        source_errors = [k for k, v in sources.items() if not v["ok"]]

        uptime = int(now - self.started)
        if not document:
            state = "starting" if uptime < STARTUP_GRACE_SECONDS or self.running else "unhealthy"
        elif feed_info["stale"]:
            state = "unhealthy"
        elif source_errors or self.status.get("last_exit_code") not in (0, None):
            state = "degraded"
        else:
            state = "ok"
        return state, {
            "status": state,
            "service": {"running": True, "started_at": _iso(self.started), "uptime_seconds": uptime},
            "discovery": {
                "state": "running" if self.running else "idle",
                "last_attempt_at": self.status.get("last_attempt_at"),
                "last_success_at": self.status.get("last_success_at"),
                "last_exit_code": self.status.get("last_exit_code"),
                "last_duration_seconds": self.status.get("last_duration_seconds"),
                "next_run_at": _iso(self.next_run_at) if self.next_run_at else None,
                "interval_seconds": self.interval,
            },
            "feed": feed_info,
            "sources": sources,
            "source_errors": source_errors,
        }


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


class Handler(BaseHTTPRequestHandler):
    server_version = "area-events"
    sys_version = ""
    timeout = 10

    def _send(self, code, body, content_type="application/json; charset=utf-8", extra=None, cors=True):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")  # public read-only feed; never sent by the API
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, (json.dumps(payload, indent=2) + "\n").encode())

    def _api(self):
        """Hand a /api/v1/location request to the authenticated API (404 when no key is configured)."""
        api = getattr(self.server, "api", None)
        length = self.headers.get("Content-Length")
        body = b""
        if self.command == "PUT" and length:
            if not length.isdigit() or int(length) > MAX_BODY:
                return self._send(413, b'{"error": {"code": "too_large", "message": "request body too large"}}' + b"\n", cors=False)
            body = self.rfile.read(int(length))
        elif self.headers.get("Transfer-Encoding"):
            return self._send(411, b'{"error": {"code": "length_required", "message": "Content-Length is required"}}' + b"\n", cors=False)
        if api is None:
            return self._send(404, b'{"error": {"code": "not_found", "message": "not found"}}' + b"\n", cors=False)
        status, headers, payload = api.handle(self.command, self.path, self.headers, body, self.client_address[0])
        content_type = headers.pop("Content-Type", "application/json; charset=utf-8")
        self._send(status, payload, content_type, headers, cors=False)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        service = self.server.service
        if path == ROUTE:
            return self._api()
        if path == "/events.json":
            feed = service.read_feed()
            if feed:
                self._send(200, feed[0])
            else:
                self._json(503, {"error": "no valid feed available yet"})
        elif path == "/health":
            state, payload = service.health()
            self._json(503 if state == "unhealthy" else 200, payload)
        else:
            self._json(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_OPTIONS(self):
        if self.path.split("?", 1)[0] == ROUTE:
            return self._send(404, b'{"error": {"code": "not_found", "message": "not found"}}' + b"\n", cors=False)
        self._send(204, b"", extra={"Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                                    "Access-Control-Allow-Headers": "Cache-Control, Pragma, Content-Type"})

    def _not_allowed(self):
        self._json(405, {"error": "read-only service"})

    def do_PUT(self):
        self._api() if self.path.split("?", 1)[0] == ROUTE else self._not_allowed()

    do_POST = do_DELETE = do_PATCH = _not_allowed

    def send_response(self, code, message=None):
        super().send_response(code, message)
        if code == 405:
            self.send_header("Allow", "GET, HEAD, OPTIONS")

    def log_message(self, fmt, *args):
        log.debug("http %s", fmt % args)


def make_server(service, host, port, api=None):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.service = service
    server.api = api
    return server


def setup_logging(data_dir):
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [logging.StreamHandler(sys.__stdout__)]
    try:
        os.makedirs(data_dir, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(os.path.join(data_dir, "refresh.log"), maxBytes=1_000_000, backupCount=5, encoding="utf-8"))
    except OSError as error:
        print(f"file logging disabled: {error}", file=sys.stderr)
    for handler in handlers:
        handler.setFormatter(formatter)
        log.addHandler(handler)


def main():
    data_dir = os.environ.get("AREA_EVENTS_DATA_DIR", "/data")
    setup_logging(data_dir)
    service = Service(data_dir, os.environ.get("AREA_EVENTS_CONFIG", CONFIG_FILE),
                      max(MIN_INTERVAL, int(os.environ.get("AREA_EVENTS_INTERVAL_SECONDS", "3600"))))
    # One container owns this volume, so a lock left by a killed run is stale.
    with contextlib.suppress(OSError):
        os.remove(os.path.join(data_dir, ".refresh.lock"))
    controller = api = None
    try:
        keys = load_api_keys()
    except ValueError as error:
        keys = []
        log.error("location control disabled: %s", error)
    if keys:
        controller = LocationController(data_dir, lambda: load_config(service.config_path), limits=load_config(service.config_path).get("control"),
                                        slot=service.slot)
        api = Api(controller, keys)
        log.info("location control enabled")
    else:
        log.info("location control disabled: no API key configured")
    server = make_server(service, os.environ.get("AREA_EVENTS_HOST", "0.0.0.0"), int(os.environ.get("AREA_EVENTS_PORT", "8080")), api)
    stop = threading.Event()

    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=service.run_forever, args=(stop,), daemon=True).start()
    if controller:
        threading.Thread(target=controller.run_forever, args=(stop,), daemon=True).start()
    log.info("serving on %s:%s, refreshing every %ss, data in %s", *server.server_address[:2], service.interval, data_dir)
    server.serve_forever()
    log.info("stopped")


if __name__ == "__main__":
    main()
