"""The Discovery control and read API: authenticated HTTP/JSON over the standard library, versioned under /v1/.

    GET   /v1/discovery/status           read      engine, settings, areas, sources, jobs, and `build`: the identifier of the backend code this process runs
    GET   /v1/areas                      read      GET /v1/areas/{id} reads one
    POST  /v1/areas                      control   create (idempotent with request_id)
    PATCH /v1/areas/{id}                 control   body must carry expected_revision
    GET   /v1/sources[/<id>]              read      configured producer instances
    POST  /v1/sources                    control   create a configured instance
    PATCH /v1/sources/<id>               control   update; body must carry expected_revision
    POST  /v1/sources/<id>/enable|disable control   enable or disable; body must carry expected_revision
    PUT   /v1/discovery/settings         control   mode and intensity; body must carry expected_revision
    POST  /v1/discovery/pause|resume     control   idempotent
    GET   /v1/changes?after=N&limit=K    read      the durable change feed (hints are not needed; this is authoritative)
    POST  /v1/service/stop               control   graceful shutdown of the serving process
    GET   /v1/events?area=&source=&from=&to=&ends_from=&after=&limit=   read   normalized events (bounded pages; `after` is the previous page's cursor;
                                                                    `ends_from` keeps those still going on then, so with `to` it asks for the events overlapping a window)
    GET   /v1/events/{key}               read      one event with its revision history and field evidence
    POST  /v1/events/{key}/trash         control   delete the event into the trashcan with a reason (`discovery.trashcan.v1`); repeating it changes nothing
    GET   /v1/trashcan?limit=K           read      the deleted events, their reasons and how often rediscovery was suppressed (`discovery.trashcan.v1`)
    GET   /v1/media/{name}               read      a cached picture (event photograph, venue photograph or map) by its content-derived name
    POST  /v1/processing/claim|submit|fail           process   an extraction worker's calls (a "process" token cannot read areas or events)
    GET   /v1/processing/status          read | process   work counts

Every request needs `Authorization: Bearer <token>`. A token has one scope: "read", "control" (includes read) or "process" (only the
/v1/processing routes). A read token can never change anything, and only a process token can submit extraction results. Failed attempts are rate limited per client. Request bodies are bounded (8 KB), must be JSON objects, and
mutations need `Content-Type: application/json`. Responses carry no CORS headers and are never cached. Errors are
`{"error": {"code", "message"}}` with an explicit status (400, 401, 403, 404, 409, 413, 415, 429, 500). The API takes no SQL, no file paths
and no addresses to fetch. It binds to loopback unless remote binding is explicitly allowed, and refuses to start without tokens.
"""
import dataclasses
import functools
import hashlib
import hmac
import json
import logging
import re
import secrets
import ssl
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import __version__
from ..control.service import (CONTROL_SCHEMA, ControlError, CreateAreaRequest, RevisionConflict, SettingsRequest, UpdateAreaRequest, ValidationFailed,
                               CreateSourceRequest, UpdateSourceRequest)
from ..trashcan import TRASHCAN_SCHEMA
from .events import EventQuery

log = logging.getLogger("area-events.discovery.api")
MAX_BODY = 8192
MAX_PROCESSING_BODY = 65536   # a submitted extraction result carries a description and evidence
MIN_TOKEN_LENGTH = 32
AUTH_FAILURES = 5
AUTH_WINDOW = 60
AREA_PATH = re.compile(r"^/v1/areas/([A-Za-z0-9]{8,40})$")
SOURCE_PATH = re.compile(r"^/v1/sources/([a-z][a-z0-9_-]{0,79})(?:/(enable|disable))?$")
MEDIA_PATH = re.compile(r"^/v1/media/([a-z0-9][a-z0-9.-]{3,60}\.(?:svg|jpg|png|webp))$")
TRASH_PATH = re.compile(r"^/v1/events/([A-Za-z0-9_-]{8,60})/trash$")
SCOPES = ("read", "control", "process")


def code_fingerprint(root):
    """A short deterministic fingerprint of the Python code under `root`: SHA-1 over every module's relative path and bytes, in path order (compiled caches left out)."""
    digest = hashlib.sha1()
    for path in sorted(p for p in Path(root).rglob("*.py") if "__pycache__" not in p.parts):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:8]


@functools.lru_cache(maxsize=None)
def backend_build():
    """The identifier of the backend this process runs, read once from its own installed package: "AREA-EVENTS-BACKEND <version>-<fingerprint>"."""
    return f"AREA-EVENTS-BACKEND {__version__}-{code_fingerprint(Path(__file__).resolve().parents[1])}"


def body_limit(path):
    return MAX_PROCESSING_BODY if path.startswith("/v1/processing/") else MAX_BODY


def to_jsonable(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return None
    return value


class ApiTokens:
    """Bearer tokens and what each may do. Immutable after construction; compared in constant time."""

    def __init__(self, control_token, read_token=None, process_token=None):
        if control_token is None:
            raise ValueError("a control token is required")
        given = [t for t in (control_token, read_token, process_token) if t is not None]
        for token in given:
            if not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH:
                raise ValueError(f"tokens must have at least {MIN_TOKEN_LENGTH} characters")
        if len(set(given)) != len(given):
            raise ValueError("the control, read and process tokens must differ")
        self._tokens = tuple((token.encode(), scope) for token, scope in ((control_token, "control"), (read_token, "read"), (process_token, "process")) if token)

    @staticmethod
    def generate():
        return secrets.token_urlsafe(32)

    def scope_of(self, header):
        supplied = header[7:].strip().encode() if isinstance(header, str) and header[:7].lower() == "bearer " else b""
        found = None
        for token, scope in self._tokens:  # every token is compared, so timing does not reveal which one matched
            if hmac.compare_digest(supplied, token):
                found = scope
        return found


class ApiError(Exception):
    def __init__(self, status, code, message, extra=None, headers=None):
        super().__init__(message)
        self.status, self.code, self.message, self.extra, self.headers = status, code, message, extra or {}, headers or {}


class DiscoveryApi:
    """Routes a request to the DiscoveryControl and DiscoveryReader contracts. Knows nothing about HTTP sockets."""

    def __init__(self, control, reader, tokens: ApiTokens, clock, on_stop=None, processing=None, events=None, media=None, trashcan=None):
        self._control, self._reader, self._tokens, self._clock, self._on_stop = control, reader, tokens, clock, on_stop
        self._processing, self._events, self._media, self._trashcan = processing, events, media, trashcan
        self._failures, self._failure_lock = {}, threading.Lock()

    def handle(self, method, target, headers, body, client):
        """(status, payload dict). Never raises."""
        try:
            parts = urlsplit(target)
            scope = self._authenticate(headers.get("Authorization"), client)
            return self._route(method, parts.path, parse_qs(parts.query), headers, body, scope)
        except ApiError as error:
            payload = {"error": {"code": error.code, "message": error.message, **error.extra}}
            if error.headers:
                payload["_headers"] = error.headers
            return error.status, payload
        except ControlError as error:
            extra = {"current_revision": error.current_revision} if isinstance(error, RevisionConflict) else {}
            return error.status, {"error": {"code": error.code, "message": str(error), **extra}}
        except Exception:
            log.exception("request failed")
            return 500, {"error": {"code": "internal_error", "message": "internal error"}}

    def _authenticate(self, header, client):
        now = self._clock()
        with self._failure_lock:
            recent = [t for t in self._failures.get(client, []) if now - t < AUTH_WINDOW]
            self._failures[client] = recent
            if len(self._failures) > 1000:
                self._failures = {c: v for c, v in self._failures.items() if v}
            if len(recent) >= AUTH_FAILURES:
                raise ApiError(429, "too_many_attempts", "too many failed attempts", headers={"Retry-After": str(max(AUTH_WINDOW - int(now - min(recent)), 1))})
        scope = self._tokens.scope_of(header)
        if scope is None:
            with self._failure_lock:
                self._failures.setdefault(client, []).append(now)
            raise ApiError(401, "unauthorized", "a valid token is required", headers={"WWW-Authenticate": "Bearer"})
        return scope

    @staticmethod
    def _need(scope, wanted):
        if wanted == "control" and scope != "control":
            raise ApiError(403, "forbidden", "this token may only read")
        if wanted == "process" and scope != "process":
            raise ApiError(403, "forbidden", "only a processing token may do this")

    def _json(self, headers, body, limit=MAX_BODY):
        if len(body) > limit:
            raise ApiError(413, "too_large", "the request body is too large")
        if not body:
            return {}
        if "application/json" not in (headers.get("Content-Type") or "").lower():
            raise ApiError(415, "unsupported_media_type", "send application/json")
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ValidationFailed("the request body is not valid JSON") from error
        if not isinstance(data, dict):
            raise ValidationFailed("the request body must be a JSON object")
        return data

    @staticmethod
    def _only(data, allowed):
        unknown = sorted(set(data) - set(allowed))
        if unknown:
            raise ValidationFailed(f"unknown fields: {', '.join(unknown)[:100]}")

    @staticmethod
    def _source_json(source):
        return {"id": source.source_key, "type": source.adapter, "name": source.name, "enabled": source.enabled,
                "interval_minutes": source.interval_minutes, "options": source.options_dict(), "revision": source.revision}

    def _route(self, method, path, query, headers, body, scope):
        if scope == "process" and not (path.startswith("/v1/processing/") and (method == "POST" or path == "/v1/processing/status")):
            raise ApiError(403, "forbidden", "a processing token may only use the processing routes")
        if path.startswith("/v1/processing/"):
            return self._processing_route(method, path, query, headers, body, scope)
        if path == "/v1/trashcan" or TRASH_PATH.match(path):
            return self._trashcan_route(method, path, query, headers, body, scope)
        if path == "/v1/events" or path.startswith("/v1/events/"):
            return self._events_route(method, path, query)
        if path.startswith("/v1/media/"):
            return self._media_route(method, path)
        if method == "GET" and path == "/v1/discovery/status":
            return 200, {"schema": CONTROL_SCHEMA, "status": {**to_jsonable(self._control.get_status()), "build": backend_build()}}
        if method == "GET" and path == "/v1/areas":
            return 200, {"schema": CONTROL_SCHEMA, "areas": to_jsonable(self._control.list_areas())}
        if method == "GET" and path == "/v1/producer-types":
            return 200, self._control.list_producer_types()
        if method == "GET" and path == "/v1/sources":
            return 200, {"schema": CONTROL_SCHEMA, "sources": [self._source_json(source) for source in self._control.list_sources()]}
        source_match = SOURCE_PATH.match(path)
        if method == "GET" and source_match and source_match.group(2) is None:
            return 200, {"schema": CONTROL_SCHEMA, "source": self._source_json(self._control.get_source(source_match.group(1)))}
        if method == "POST" and path == "/v1/sources":
            self._need(scope, "control")
            data = self._json(headers, body)
            self._only(data, ("id", "type", "name", "enabled", "interval_minutes", "options"))
            required = {"id", "type", "name", "enabled", "interval_minutes", "options"}
            if required - set(data):
                raise ValidationFailed("id, type, name, enabled, interval_minutes and options are required")
            result = self._control.create_source(CreateSourceRequest(data["id"], data["type"], data["name"], data["enabled"], data["interval_minutes"], data["options"]))
            return 201, {"schema": result.schema, "revision": result.revision, "change_seq": result.change_seq, "replayed": result.replayed,
                         "source": self._source_json(result.source)}
        if source_match and source_match.group(2) is not None and method == "POST":
            self._need(scope, "control")
            data = self._json(headers, body)
            self._only(data, ("expected_revision",))
            if "expected_revision" not in data:
                raise ValidationFailed("expected_revision is required")
            enabled = source_match.group(2) == "enable"
            result = self._control.set_source_enabled(source_match.group(1), enabled, data["expected_revision"])
            return 200, {"schema": result.schema, "revision": result.revision, "change_seq": result.change_seq, "replayed": result.replayed,
                         "source": self._source_json(result.source)}
        if source_match and source_match.group(2) is None and method == "PATCH":
            self._need(scope, "control")
            data = self._json(headers, body)
            self._only(data, ("expected_revision", "name", "enabled", "interval_minutes", "options"))
            if "expected_revision" not in data:
                raise ValidationFailed("expected_revision is required")
            result = self._control.update_source(UpdateSourceRequest(source_match.group(1), data["expected_revision"], data.get("name"), data.get("enabled"),
                                                                     data.get("interval_minutes"), data.get("options")))
            return 200, {"schema": result.schema, "revision": result.revision, "change_seq": result.change_seq, "replayed": result.replayed,
                         "source": self._source_json(result.source)}
        match = AREA_PATH.match(path)
        if method == "GET" and match:
            return 200, {"schema": CONTROL_SCHEMA, "area": to_jsonable(self._control.get_area(match.group(1)))}
        if method == "GET" and path == "/v1/changes":
            try:
                after, limit = int((query.get("after") or ["0"])[0]), int((query.get("limit") or ["200"])[0])
            except ValueError as error:
                raise ValidationFailed("after and limit must be integers") from error
            return 200, to_jsonable(self._reader.changes(max(after, 0), max(1, min(limit, 500))))
        if method == "POST" and path == "/v1/areas":
            self._need(scope, "control")
            data = self._json(headers, body)
            self._only(data, ("name", "latitude", "longitude", "radius", "unit", "intensity", "enabled", "sources", "request_id"))
            if not {"name", "latitude", "longitude", "radius"} <= set(data):
                raise ValidationFailed("name, latitude, longitude and radius are required")
            result = self._control.create_area(CreateAreaRequest(
                data["name"], data["latitude"], data["longitude"], data["radius"], data.get("unit", "mi"), data.get("intensity", "normal"), data.get("enabled", True),
                tuple(data["sources"]) if isinstance(data.get("sources", ()), list) else data.get("sources"), data.get("request_id")))
            return (200 if result.replayed else 201), to_jsonable(result)
        if method == "PATCH" and match:
            self._need(scope, "control")
            data = self._json(headers, body)
            self._only(data, ("expected_revision", "name", "latitude", "longitude", "radius", "unit", "intensity", "enabled", "sources"))
            if "expected_revision" not in data:
                raise ValidationFailed("expected_revision is required")
            sources = data.get("sources")
            result = self._control.update_area(UpdateAreaRequest(
                match.group(1), data["expected_revision"], data.get("name"), data.get("latitude"), data.get("longitude"), data.get("radius"), data.get("unit"),
                data.get("intensity"), data.get("enabled"), tuple(sources) if isinstance(sources, list) else sources))
            return 200, to_jsonable(result)
        if method == "PUT" and path == "/v1/discovery/settings":
            self._need(scope, "control")
            data = self._json(headers, body)
            self._only(data, ("expected_revision", "mode", "intensity"))
            if "expected_revision" not in data:
                raise ValidationFailed("expected_revision is required")
            return 200, to_jsonable(self._control.set_discovery_mode(SettingsRequest(data["expected_revision"], data.get("mode"), data.get("intensity"))))
        if method == "POST" and path in ("/v1/discovery/pause", "/v1/discovery/resume"):
            self._need(scope, "control")
            self._json(headers, body)
            return 200, to_jsonable(self._control.pause() if path.endswith("pause") else self._control.resume())
        if method == "POST" and path == "/v1/service/stop":
            self._need(scope, "control")
            if self._on_stop is None:
                raise ApiError(404, "not_found", "this service cannot be stopped through the API")
            self._on_stop()
            return 202, {"schema": CONTROL_SCHEMA, "stopping": True}
        known = path == "/v1/discovery/status" or path in ("/v1/areas", "/v1/sources", "/v1/changes", "/v1/discovery/settings", "/v1/discovery/pause", "/v1/discovery/resume",
                                                           "/v1/service/stop") or match or source_match
        raise ApiError(405 if known else 404, "method_not_allowed" if known else "not_found", "method not allowed" if known else "not found")


    def _media_route(self, method, path):
        """A cached picture. The name is matched against a strict pattern and looked up by name only: no path from a request ever reaches the file system."""
        if self._media is None:
            raise ApiError(404, "not_found", "this service does not serve media")
        if method != "GET":
            raise ApiError(405, "method_not_allowed", "method not allowed")
        match = MEDIA_PATH.match(path)
        found = self._media.read(match.group(1)) if match else None
        if found is None:
            raise ApiError(404, "not_found", "no such picture")
        data, kind = found
        return 200, {"_binary": data, "_content_type": kind, "_headers": {"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"}}

    def _trashcan_route(self, method, path, query, headers, body, scope):
        if self._trashcan is None:
            raise ApiError(404, "not_found", "this service has no trashcan")
        if path == "/v1/trashcan":
            if method != "GET":
                raise ApiError(405, "method_not_allowed", "method not allowed")
            try:
                limit = int((query.get("limit") or ["100"])[0])
            except ValueError as error:
                raise ValidationFailed("limit must be an integer") from error
            return 200, {"schema": TRASHCAN_SCHEMA, **self._trashcan.listing(limit)}
        if method != "POST":
            raise ApiError(405, "method_not_allowed", "method not allowed")
        self._need(scope, "control")
        data = self._json(headers, body)
        self._only(data, ("reason",))
        if "reason" not in data:
            raise ValidationFailed("reason is required")
        return 200, {"schema": TRASHCAN_SCHEMA, **self._trashcan.trash(TRASH_PATH.match(path).group(1), data["reason"])}

    def _events_route(self, method, path, query):
        if self._events is None:
            raise ApiError(404, "not_found", "this service does not serve events")
        if method != "GET":
            raise ApiError(405, "method_not_allowed", "method not allowed")
        if path == "/v1/events":
            unknown = sorted(set(query) - {"area", "source", "from", "to", "ends_from", "after", "limit"})
            if unknown:
                raise ValidationFailed(f"unknown parameters: {', '.join(unknown)[:100]}")

            def one(name):
                return (query.get(name) or [None])[0]
            try:
                limit = int(one("limit") or 50)
            except ValueError as error:
                raise ValidationFailed("limit must be an integer") from error
            return 200, to_jsonable(self._events.query(EventQuery(one("area"), one("source"), self._time(one("from"), "from"), self._time(one("to"), "to"),
                                                                  one("after"), max(1, limit), self._time(one("ends_from"), "ends_from"))))
        return 200, to_jsonable(self._events.get(path[len("/v1/events/"):]))

    @staticmethod
    def _time(value, name):
        """A UTC epoch in seconds, or an ISO-8601 time with an offset."""
        if value is None:
            return None
        if re.fullmatch(r"[0-9]{1,12}", value):
            return int(value)
        try:
            moment = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValidationFailed(f"{name} must be a UTC epoch or an ISO-8601 time with an offset") from error
        if moment.tzinfo is None:
            raise ValidationFailed(f"{name} needs a UTC offset")
        return int(moment.timestamp())

    def _processing_route(self, method, path, query, headers, body, scope):
        if self._processing is None:
            raise ApiError(404, "not_found", "this service does not offer processing")
        routes = ("/v1/processing/claim", "/v1/processing/submit", "/v1/processing/fail", "/v1/processing/status")
        if path not in routes:
            raise ApiError(404, "not_found", "not found")
        if method == "GET" and path == "/v1/processing/status":
            extractor = (query.get("extractor") or ["stored-candidate"])[0]
            try:
                version = int((query.get("version") or ["1"])[0])
            except ValueError as error:
                raise ValidationFailed("version must be an integer") from error
            return 200, to_jsonable(self._processing.status(extractor, version))
        if method != "POST" or path == "/v1/processing/status":
            raise ApiError(405, "method_not_allowed", "method not allowed")
        self._need(scope, "process")
        data = self._json(headers, body, MAX_PROCESSING_BODY)
        if path.endswith("claim"):
            self._only(data, ("worker", "extractor", "version", "limit", "lease_s"))
            if not {"worker", "extractor", "version"} <= set(data):
                raise ValidationFailed("worker, extractor and version are required")
            return 200, to_jsonable(self._processing.claim(data["worker"], data["extractor"], data["version"], data.get("limit", 5), data.get("lease_s", 300)))
        if path.endswith("submit"):
            self._only(data, ("worker", "extractor", "version", "candidate_key", "lease_token", "result"))
            if not {"extractor", "version", "candidate_key", "lease_token", "result"} <= set(data):
                raise ValidationFailed("extractor, version, candidate_key, lease_token and result are required")
            return 200, to_jsonable(self._processing.submit(data["candidate_key"], data["extractor"], data["version"], data["lease_token"], data["result"], data.get("worker")))
        self._only(data, ("extractor", "version", "candidate_key", "lease_token", "error"))
        if not {"extractor", "version", "candidate_key", "lease_token"} <= set(data):
            raise ValidationFailed("extractor, version, candidate_key and lease_token are required")
        return 200, self._processing.fail(data["candidate_key"], data["extractor"], data["version"], data["lease_token"], data.get("error"))


class _Handler(BaseHTTPRequestHandler):
    server_version = "area-events-discovery"
    sys_version = ""
    timeout = 10

    def _handle(self):
        length = self.headers.get("Content-Length", "0")
        if self.headers.get("Transfer-Encoding") or not length.isdigit():
            return self._send(411, {"error": {"code": "length_required", "message": "Content-Length is required"}})
        if int(length) > body_limit(urlsplit(self.path).path):
            return self._send(413, {"error": {"code": "too_large", "message": "the request body is too large"}})
        body = self.rfile.read(int(length)) if int(length) else b""
        status, payload = self.server.api.handle(self.command, self.path, self.headers, body, self.client_address[0])
        self._send(status, payload)

    def _send(self, status, payload):
        extra = payload.pop("_headers", {}) if isinstance(payload, dict) else {}
        binary = payload.pop("_binary", None) if isinstance(payload, dict) else None
        kind = payload.pop("_content_type", None) if binary is not None else None
        raw = binary if binary is not None else (json.dumps(payload) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", kind or "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def log_message(self, fmt, *args):  # never the request line's headers; paths only, at debug level
        log.debug("http %s", fmt % args)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """A client that spoke the wrong protocol (plain http to the TLS port, a port scan) or went away is not a service fault: no traceback, no stderr."""
        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            log.debug("connection from %s dropped: %s", client_address[0], sys.exc_info()[1])
        else:
            log.exception("request from %s failed", client_address[0])


class DiscoveryApiServer:
    """The HTTP server for a DiscoveryApi: explicit start and stop, loopback by default."""

    def __init__(self, api: DiscoveryApi, host="127.0.0.1", port=8780, allow_remote=False, tls=None, allow_plain=False):
        """`tls`: (certificate file, private key file) to serve https. Beyond the loopback address the service refuses to start without it,
        because every request carries a bearer token; `allow_plain=True` is the explicit, informed exception (a trusted link or a proxy)."""
        if host not in ("127.0.0.1", "::1", "localhost"):
            if not allow_remote:
                raise ValueError("binding beyond the loopback address needs allow_remote=True")
            if tls is None and not allow_plain:
                raise ValueError("binding beyond the loopback address needs TLS (tls=(certificate, key)); bearer tokens must not cross a network in plain text")
        self._api, self._host, self._port, self._tls = api, host, port, tls
        self._server = self._thread = None

    @property
    def scheme(self):
        return "https" if self._tls else "http"

    @property
    def address(self):
        return self._server.server_address if self._server else (self._host, self._port)

    def start(self):
        server = _Server((self._host, self._port), _Handler)
        server.daemon_threads = True
        server.api = self._api
        if self._tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(*self._tls)
            # the handshake happens on the first read, in the request's own thread and under its timeout, not in the accepting loop
            server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="discovery-api", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(5)
            self._server = None
