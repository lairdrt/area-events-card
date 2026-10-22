"""The Discovery control and read API: authenticated HTTP/JSON over the standard library, versioned under /v1/.

    GET   /v1/discovery/status           read      engine, settings, areas, sources, jobs
    GET   /v1/areas                      read      GET /v1/areas/{id} reads one
    POST  /v1/areas                      control   create (idempotent with request_id)
    PATCH /v1/areas/{id}                 control   body must carry expected_revision
    PUT   /v1/discovery/settings         control   mode and intensity; body must carry expected_revision
    POST  /v1/discovery/pause|resume     control   idempotent
    GET   /v1/changes?after=N&limit=K    read      the durable change feed (hints are not needed; this is authoritative)
    POST  /v1/service/stop               control   graceful shutdown of the serving process

Every request needs `Authorization: Bearer <token>`. A token has scopes ("read", "control"; control includes read): a read token
can never change anything. Failed attempts are rate limited per client. Request bodies are bounded (8 KB), must be JSON objects, and
mutations need `Content-Type: application/json`. Responses carry no CORS headers and are never cached. Errors are
`{"error": {"code", "message"}}` with an explicit status (400, 401, 403, 404, 409, 413, 415, 429, 500). The API takes no SQL, no file paths
and no addresses to fetch. It binds to loopback unless remote binding is explicitly allowed, and refuses to start without tokens.
"""
import dataclasses
import hmac
import json
import logging
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .control import (CONTROL_SCHEMA, ControlError, CreateAreaRequest, RevisionConflict, SettingsRequest, UpdateAreaRequest, ValidationFailed)

log = logging.getLogger("area-events.discovery.api")
MAX_BODY = 8192
MIN_TOKEN_LENGTH = 32
AUTH_FAILURES = 5
AUTH_WINDOW = 60
AREA_PATH = re.compile(r"^/v1/areas/([A-Za-z0-9]{8,40})$")
SCOPES = ("read", "control")


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

    def __init__(self, control_token, read_token=None):
        for token in (control_token, read_token):
            if token is not None and (not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH):
                raise ValueError(f"tokens must have at least {MIN_TOKEN_LENGTH} characters")
        if control_token == read_token:
            raise ValueError("the read and control tokens must differ")
        self._tokens = tuple(((control_token.encode(), "control"),) + (((read_token.encode(), "read"),) if read_token else ()))

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

    def __init__(self, control, reader, tokens: ApiTokens, clock, on_stop=None):
        self._control, self._reader, self._tokens, self._clock, self._on_stop = control, reader, tokens, clock, on_stop
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

    def _json(self, headers, body):
        if len(body) > MAX_BODY:
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

    def _route(self, method, path, query, headers, body, scope):
        if method == "GET" and path == "/v1/discovery/status":
            return 200, {"schema": CONTROL_SCHEMA, "status": to_jsonable(self._control.get_status())}
        if method == "GET" and path == "/v1/areas":
            return 200, {"schema": CONTROL_SCHEMA, "areas": to_jsonable(self._control.list_areas())}
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
        known = path == "/v1/discovery/status" or path in ("/v1/areas", "/v1/changes", "/v1/discovery/settings", "/v1/discovery/pause", "/v1/discovery/resume",
                                                           "/v1/service/stop") or match
        raise ApiError(405 if known else 404, "method_not_allowed" if known else "not_found", "method not allowed" if known else "not found")


class _Handler(BaseHTTPRequestHandler):
    server_version = "area-events-discovery"
    sys_version = ""
    timeout = 10

    def _handle(self):
        length = self.headers.get("Content-Length", "0")
        if self.headers.get("Transfer-Encoding") or not length.isdigit():
            return self._send(411, {"error": {"code": "length_required", "message": "Content-Length is required"}})
        if int(length) > MAX_BODY:
            return self._send(413, {"error": {"code": "too_large", "message": "the request body is too large"}})
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

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def log_message(self, fmt, *args):  # never the request line's headers; paths only, at debug level
        log.debug("http %s", fmt % args)


class DiscoveryApiServer:
    """The HTTP server for a DiscoveryApi: explicit start and stop, loopback by default."""

    def __init__(self, api: DiscoveryApi, host="127.0.0.1", port=8780, allow_remote=False):
        if host not in ("127.0.0.1", "::1", "localhost") and not allow_remote:
            raise ValueError("binding beyond the loopback address needs allow_remote=True")
        self._api, self._host, self._port = api, host, port
        self._server = self._thread = None

    @property
    def address(self):
        return self._server.server_address if self._server else (self._host, self._port)

    def start(self):
        server = ThreadingHTTPServer((self._host, self._port), _Handler)
        server.daemon_threads = True
        server.api = self._api
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
