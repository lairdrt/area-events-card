"""The one active discovery location, and how it is changed.

The service discovers events for a single place. The configuration names it; an authenticated request can change
it. A change never touches the published feed until a complete discovery for the new place has succeeded:

    1. the request is validated (only a place, a radius and a display name are accepted) and resolved;
    2. discovery runs in a separate staging directory with its own cache, holding the same single job slot as the
       hourly refresh, so no cache from the old place can reach the new one;
    3. only if that run produced a valid feed labeled with the requested place is it published: the new place is
       saved (so it survives restarts), then the cache and the feed are replaced atomically, in that order;
    4. on any failure the old feed, cache and place stay exactly as they were, and the failure is reported.
"""
import contextlib
import io
import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone

from .core import Fetcher
from .location import PROVIDERS, LocationError, resolve_location
from .publish import FeedError, validate_feed, write_atomic
from .refresh import load_config, refresh

ACTIVE_FILE = "active-location.json"   # the saved place, once it has been changed from the configuration
STATE_FILE = "location-change.json"    # progress and outcome of the latest change request
STAGING = "staging"
GEOCODE_CACHE = "location-cache.json"
REQUEST_KEYS = {"name", "query", "zip", "country", "region", "timezone", "latitude", "longitude", "radius_miles"}
LOCATION_KEYS = REQUEST_KEYS - {"name"}
DEFAULT_LIMITS = {"max_radius_miles": 100, "min_change_interval_seconds": 300, "geocodes_per_hour": 20}


class ApiError(Exception):
    """An error the HTTP layer reports verbatim."""

    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


class ChangeFailed(Exception):
    pass


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def effective_config(config, data_dir):
    """The configuration with its location replaced by the one saved by a location change, if there is one."""
    saved = _read_json(os.path.join(data_dir, ACTIVE_FILE)).get("location")
    try:
        resolve_location(saved)  # a damaged file must not stop discovery: fall back to the configuration
    except LocationError:
        return config
    return {**config, "location": saved}


def _text(value, limit, what):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit or not value.isprintable():
        raise ApiError(422, "invalid_request", f"{what} must be printable text of 1-{limit} characters")
    return " ".join(value.split())


class LocationController:
    def __init__(self, data_dir, config_loader=load_config, refresh_fn=refresh, clock=time.time, providers=None, fetcher=None,
                 limits=None, slot=None):
        self.data_dir, self.config_loader, self.refresh_fn, self.clock = data_dir, config_loader, refresh_fn, clock
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.providers = providers or PROVIDERS
        self.fetcher = fetcher or Fetcher(2.0)
        self.slot = slot or threading.Lock()  # shared with the hourly refresh: one discovery job at a time
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._lookups = []  # times of uncached geocoding lookups, for the hourly budget
        self._path = lambda name: os.path.join(data_dir, name)
        self._recover()

    # --- state ---------------------------------------------------------------------------

    def _state(self):
        return _read_json(self._path(STATE_FILE)) or {"state": "idle"}

    def _write_state(self, state):
        write_atomic(self._path(STATE_FILE), json.dumps(state, indent=2) + "\n")

    def _recover(self):
        """A restart abandons a change that was queued or running; the old feed and place are untouched."""
        shutil.rmtree(self._path(STAGING), ignore_errors=True)
        state = self._state()
        if state.get("state") in ("queued", "running"):
            state.update(state="failed", error="interrupted by a restart; repeat the change", finished_at=_iso(self.clock()))
            self._write_state(state)

    def status(self):
        with self._lock:
            config = self.config_loader()
            effective = effective_config(config, self.data_dir)
            feed = _read_json(self._path("events.json")).get("location") or {}
            change = {k: v for k, v in self._state().items() if k != "requested_ts"}
            return {"active": {"source": "changed" if effective is not config else "configuration", "location": effective.get("location")},
                    "feed": {"location": feed.get("display_name"), "latitude": feed.get("latitude"), "longitude": feed.get("longitude"),
                             "radius_miles": feed.get("radius_miles"), "generated_at": _read_json(self._path("events.json")).get("generated_at")},
                    "change": change}

    # --- requesting a change ----------------------------------------------------------------

    def _validate(self, body):
        if not isinstance(body, dict):
            raise ApiError(422, "invalid_request", "the request must be a JSON object")
        unknown = sorted(set(body) - REQUEST_KEYS)
        if unknown:
            raise ApiError(422, "invalid_request", f"unknown fields: {', '.join(map(str, unknown))}")
        raw = {key: body[key] for key in LOCATION_KEYS if key in body}
        for key in ("query", "region"):
            if key in raw:
                raw[key] = _text(raw[key], 100, key)
        if "zip" in raw:
            raw["zip"] = _text(str(raw["zip"]), 12, "zip")
        radius = raw.get("radius_miles", 25)
        if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not 1 <= radius <= self.limits["max_radius_miles"]:
            raise ApiError(422, "invalid_request", f"radius_miles must be between 1 and {self.limits['max_radius_miles']}")
        raw["radius_miles"] = radius
        return raw, (_text(body["name"], 60, "name") if "name" in body else None)

    def _resolve(self, raw):
        budget = self.limits["geocodes_per_hour"]

        class Counting:  # uncached geocoder lookups count against an hourly budget
            def __init__(self, inner, controller):
                self.inner, self.controller, self.name = inner, controller, getattr(inner, "name", "")

            def lookup(self, fetcher, spec):
                now = self.controller.clock()
                self.controller._lookups = [t for t in self.controller._lookups if now - t < 3600]
                if len(self.controller._lookups) >= budget:
                    raise LocationError("the hourly geocoding budget is used up; try again later or use coordinates")
                self.controller._lookups.append(now)
                return self.inner.lookup(fetcher, spec)

        providers = {key: Counting(value, self) for key, value in self.providers.items()}
        try:
            return resolve_location(raw, self.fetcher, self._path(GEOCODE_CACHE), providers, self.clock, timezone_lookup=False)
        except LocationError as error:
            raise ApiError(422, "location", str(error)) from error

    @staticmethod
    def _same_place(a, b):
        return all(round(float(a[k]), 4) == round(float(b[k]), 4) for k in ("latitude", "longitude", "radius_miles"))

    def request_change(self, body):
        """Validate and queue a change. Returns (HTTP status, payload)."""
        raw, name = self._validate(body)
        with self._lock:
            state = self._state()
            busy = state.get("state") in ("queued", "running")
            resolved = self._resolve(raw)
            request = {k: resolved[k] for k in ("latitude", "longitude", "radius_miles", "country", "region", "timezone") if resolved.get(k) is not None}
            request["name"] = name or resolved["display_name"]
            if busy:
                # The same request repeated (several cards, an editor preview) joins the change underway; a different one conflicts.
                if state.get("requested") and self._same_place(state["requested"], request):
                    return 202, {"change": {k: v for k, v in state.items() if k != "requested_ts"}}
                raise ApiError(409, "busy", "a location change is already in progress")
            current = effective_config(self.config_loader(), self.data_dir).get("location")
            try:
                if current and self._same_place(resolve_location(current, None, self._path(GEOCODE_CACHE), self.providers), request):
                    return 200, {"change": {"state": "unchanged", "requested": request}}
            except LocationError:
                pass  # the current place cannot be resolved offline: treat the request as a change
            wait = self.limits["min_change_interval_seconds"] - (self.clock() - state.get("requested_ts", 0))
            if state.get("requested_ts") and wait > 0:
                raise ApiError(429, "change_rate", "the location was changed a moment ago", {"Retry-After": str(int(wait) + 1)})
            now = self.clock()
            self._write_state({"state": "queued", "requested": request, "requested_at": _iso(now), "requested_ts": now})
            self._wake.set()
            return 202, {"change": {k: v for k, v in self._state().items() if k != "requested_ts"}}

    # --- running a change ---------------------------------------------------------------------

    def run_pending(self):
        """Discover and publish a queued change. Returns True if one was processed."""
        with self._lock:
            state = self._state()
            if state.get("state") != "queued":
                return False
            state.update(state="running", started_at=_iso(self.clock()))
            self._write_state(state)
        request = state["requested"]
        staging = self._path(STAGING)
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        try:
            self._publish(request, staging, self._discover(request, staging))
            outcome = {"state": "succeeded", "error": None}
        except ChangeFailed as error:
            outcome = {"state": "failed", "error": str(error)}
        except Exception as error:  # nothing that goes wrong here may leave the service half-switched
            outcome = {"state": "failed", "error": f"{type(error).__name__}: {error}"}
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        with self._lock:
            state = self._state()
            state.update(outcome, finished_at=_iso(self.clock()))
            self._write_state(state)
        return True

    def _discover(self, request, staging):
        buffer = io.StringIO()
        with self.slot:
            config = {**self.config_loader(), "location": request}
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                code = self.refresh_fn(config, {}, fetcher=self.fetcher, output=os.path.join(staging, "events.json"),
                                       cache_path=os.path.join(staging, "source-cache.json"),
                                       lock_path=os.path.join(staging, ".refresh.lock"), location_cache_path=self._path(GEOCODE_CACHE))
        if code not in (0, 2):
            lines = [line.strip() for line in buffer.getvalue().splitlines() if line.strip()]
            # Prefer the line that names the cause ("... FAILED ...: site down") over the summary that follows it.
            problem = next((line for key in ("failed", "error", "refusing", "no source") for line in lines if key in line.lower()), None)
            raise ChangeFailed(problem or "discovery failed for the new location")
        return code

    def _publish(self, request, staging, code):
        feed_path, cache_path = os.path.join(staging, "events.json"), os.path.join(staging, "source-cache.json")
        try:
            with open(feed_path, encoding="utf-8") as handle:
                feed = json.load(handle)
            validate_feed(feed)
        except (OSError, ValueError, FeedError) as error:
            raise ChangeFailed(f"discovery produced no valid feed ({error})") from error
        label = feed.get("location") or {}
        if label.get("latitude") is None or not self._same_place(label, request):
            raise ChangeFailed("the new feed is not labeled with the requested location; nothing was published")
        active_path = self._path(ACTIVE_FILE)
        try:
            with open(active_path, "rb") as handle:
                previous = handle.read()
        except OSError:
            previous = None
        try:
            write_atomic(active_path, json.dumps({"location": request, "changed_at": _iso(self.clock())}, indent=2) + "\n")
            os.replace(cache_path, self._path("source-cache.json"))
            os.replace(feed_path, self._path("events.json"))
        except OSError as error:
            # Put the saved place back so the old place stays the active one (the old feed was never replaced).
            with contextlib.suppress(OSError):
                if previous is None:
                    os.remove(active_path)
                else:
                    write_atomic(active_path, previous.decode("utf-8"))
            raise ChangeFailed(f"could not publish the new feed: {error}") from error

    def run_forever(self, stop):
        while not stop.is_set():
            self._wake.wait(5)
            self._wake.clear()
            try:
                while self.run_pending():
                    pass
            except Exception:  # the worker must outlive any single change
                pass
