"""Authenticated management API (/api/v1) for areas.

Every route needs `Authorization: Bearer <key>`; without a configured key the API does not exist (404). The
routes accept only geographic choices (a name, ZIP or coordinates and a radius): never a URL, adapter name or
file path. Responses carry no CORS headers because only the Home Assistant server calls this API, never a
browser. The unauthenticated read-only /events.json and /health of the default area are not part of it.
"""
import hmac
import json
import os
import re
import time

from .areamgr import ApiError
from .areas import DEFAULT_AREA

PREFIX = "/api/v1"
MAX_BODY = 4096
MIN_KEY_LENGTH = 32
AUTH_FAILURES = 5          # failed attempts per address ...
AUTH_WINDOW = 60           # ... per this many seconds before that address is blocked for the rest of the window
KEY_VARIABLES = (("AREA_EVENTS_API_KEY", "AREA_EVENTS_API_KEY_FILE"), ("AREA_EVENTS_API_KEY_2", "AREA_EVENTS_API_KEY_2_FILE"))


def load_api_keys(environ=None):
    """Keys from the environment or secret files (two slots allow rotation). Empty means the API is off.

    Raises ValueError for a key that is too short, so a weak secret never silently enables the API.
    """
    environ = os.environ if environ is None else environ
    keys = []
    for variable, file_variable in KEY_VARIABLES:
        value = environ.get(variable, "")
        path = environ.get(file_variable)
        if path:
            try:
                with open(path, encoding="utf-8") as handle:
                    value = handle.read()
            except OSError:
                value = ""
        value = value.strip()
        if not value:
            continue
        if len(value) < MIN_KEY_LENGTH:
            raise ValueError(f"the API key in {file_variable if path else variable} is shorter than {MIN_KEY_LENGTH} characters")
        keys.append(value)
    return keys


class Api:
    def __init__(self, manager, keys, service=None, clock=time.time):
        self.manager, self.keys, self.service, self.clock = manager, [k.encode() for k in keys], service, clock
        self._failures = {}

    # --- authentication ---------------------------------------------------------------

    def _blocked(self, client):
        now = self.clock()
        recent = [t for t in self._failures.get(client, []) if now - t < AUTH_WINDOW]
        self._failures[client] = recent
        if len(self._failures) > 1000:  # bound memory when many addresses probe
            self._failures = {c: v for c, v in self._failures.items() if v}
        return AUTH_WINDOW - int(now - min(recent)) if len(recent) >= AUTH_FAILURES else 0

    def authenticate(self, header, client):
        wait = self._blocked(client)
        if wait:
            raise ApiError(429, "too_many_failures", "too many failed attempts", {"Retry-After": str(max(wait, 1))})
        token = header[7:].strip().encode() if isinstance(header, str) and header[:7].lower() == "bearer " else b""
        ok = False
        for key in self.keys:  # no early exit: every key is compared
            ok = hmac.compare_digest(token, key) or ok
        if not ok:
            self._failures.setdefault(client, []).append(self.clock())
            raise ApiError(401, "unauthorized", "a valid API key is required", {"WWW-Authenticate": "Bearer"})

    # --- routing ----------------------------------------------------------------------

    def handle(self, method, path, headers, body, client):
        """Return (status, headers, body bytes). Never raises."""
        try:
            if not self.keys:
                raise ApiError(404, "not_found", "not found")
            self.authenticate(headers.get("Authorization"), client)
            status, payload, extra = self._route(method, path.split("?", 1)[0], body)
            if isinstance(payload, bytes):
                return status, extra, payload
            return status, {"Content-Type": "application/json; charset=utf-8", **extra}, (json.dumps(payload, indent=2) + "\n").encode()
        except ApiError as error:
            return error.status, {"Content-Type": "application/json; charset=utf-8", **error.headers}, \
                (json.dumps({"error": {"code": error.code, "message": error.message}}) + "\n").encode()
        except Exception:
            return 500, {"Content-Type": "application/json; charset=utf-8"}, b'{"error": {"code": "internal", "message": "internal error"}}\n'

    def _route(self, method, path, body):
        if not path.startswith(PREFIX + "/"):
            raise ApiError(404, "not_found", "not found")
        parts = path[len(PREFIX) + 1:].strip("/").split("/")
        allowed = {
            ("health",): ("GET",), ("areas",): ("GET", "POST"), ("areas", None): ("GET", "DELETE"),
            ("areas", None, "events"): ("GET",), ("areas", None, "refresh"): ("POST",),
        }
        shape = tuple(p if i == 0 or p in ("events", "refresh") else None for i, p in enumerate(parts))
        if shape not in allowed:
            raise ApiError(404, "not_found", "not found")
        if method not in allowed[shape]:
            raise ApiError(405, "method_not_allowed", "method not allowed", {"Allow": ", ".join(allowed[shape])})
        manager = self.manager

        if shape == ("health",):
            return 200, {"status": "ok", **manager.summary(), "limits": manager.limits}, {}
        if shape == ("areas",):
            if method == "GET":
                return 200, {"areas": ([self._default()] if self.service else []) + manager.list_areas()}, {}
            area, created = manager.ensure_area(self._json(body))
            return (201 if created else 200), area, {}
        area_id = parts[1]
        if area_id == DEFAULT_AREA:
            return self._default_route(shape, method)
        if shape == ("areas", None):
            if method == "DELETE":
                manager.delete_area(area_id)
                return 200, {"deleted": area_id}, {}
            return 200, manager.describe(manager._require(area_id)["id"]), {}
        if parts[2] == "events":
            feed = manager.read_feed(area_id)
            if feed is None:
                raise ApiError(503, "no_feed", "this area has no feed yet; discovery is queued or running")
            return 200, feed[1], {"Content-Type": "application/json; charset=utf-8", "ETag": feed[0]}
        return 202, manager.request_refresh(area_id), {}

    def _default(self):
        service = self.service
        state, health = service.health()
        return {"id": DEFAULT_AREA, "names": ["Default area"], "location": None,
                "status": {"state": {"ok": "ok", "degraded": "degraded", "starting": "queued"}.get(state, "failed"),
                           "last_success_at": health["discovery"]["last_success_at"], "last_attempt_at": health["discovery"]["last_attempt_at"],
                           "last_exit_code": health["discovery"]["last_exit_code"], "events": health["feed"]["events"],
                           "feed_generated_at": health["feed"]["generated_at"], "paused": False}}

    def _default_route(self, shape, method):
        if not self.service:
            raise ApiError(404, "not_found", "no such area")
        if shape == ("areas", None) and method == "GET":
            return 200, self._default(), {}
        if shape == ("areas", None, "events"):
            feed = self.service.read_feed()
            if not feed:
                raise ApiError(503, "no_feed", "the default area has no feed yet")
            return 200, feed[0], {"Content-Type": "application/json; charset=utf-8"}
        raise ApiError(403, "forbidden", "the default area is managed by the service configuration")

    @staticmethod
    def _json(body):
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ApiError(400, "bad_json", "the request body is not valid JSON") from error
