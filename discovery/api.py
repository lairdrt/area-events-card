"""Authenticated location-control API (/api/v1/location).

    GET /api/v1/location   the active place, the place the published feed is for, and the latest change's outcome
    PUT /api/v1/location   ask the service to switch discovery to another place

Both need `Authorization: Bearer <key>`; without a configured key the API does not exist (404). A change request
accepts only a place (name, ZIP + country, or coordinates), a radius and a display name: never a URL, adapter
name or file path. Responses carry no CORS headers because only the Home Assistant server calls this API, never a
browser. The unauthenticated read-only /events.json and /health are not part of it.
"""
import hmac
import json
import os
import time

from .locationctl import ApiError

ROUTE = "/api/v1/location"
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
    def __init__(self, controller, keys, clock=time.time):
        self.controller, self.keys, self.clock = controller, [k.encode() for k in keys], clock
        self._failures = {}

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

    def handle(self, method, path, headers, body, client):
        """Return (status, headers, body bytes). Never raises."""
        try:
            if not self.keys or path.split("?", 1)[0] != ROUTE:
                raise ApiError(404, "not_found", "not found")
            self.authenticate(headers.get("Authorization"), client)
            if method == "GET":
                status, payload = 200, self.controller.status()
            elif method == "PUT":
                status, payload = self.controller.request_change(self._json(body))
            else:
                raise ApiError(405, "method_not_allowed", "method not allowed", {"Allow": "GET, PUT"})
            return status, {"Content-Type": "application/json; charset=utf-8"}, (json.dumps(payload, indent=2) + "\n").encode()
        except ApiError as error:
            return error.status, {"Content-Type": "application/json; charset=utf-8", **error.headers}, \
                (json.dumps({"error": {"code": error.code, "message": error.message}}) + "\n").encode()
        except Exception:
            return 500, {"Content-Type": "application/json; charset=utf-8"}, b'{"error": {"code": "internal", "message": "internal error"}}\n'

    @staticmethod
    def _json(body):
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ApiError(400, "bad_json", "the request body is not valid JSON") from error
