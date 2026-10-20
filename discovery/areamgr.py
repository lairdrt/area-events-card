"""Multi-area management: definitions, quotas, scheduling, status and cleanup.

An area is a place (coordinates + radius) with its own storage (see areas.py). Callers choose *where*; the
sources always come from the operator's registry in the configuration, so nothing here accepts a URL, adapter
name or file path. Every area is refreshed by the same refresh() function the default area uses, one job at a
time, with paths and location that belong to that area only.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import threading
import time
import zlib

from .areas import DEFAULT_AREA, AreaError, AreaStore, derive_area_id, load_status, save_status, validate_area_id
from .core import Fetcher
from .location import PROVIDERS, LocationError, resolve_location
from .publish import FeedError, validate_feed, write_atomic
from .refresh import load_config, refresh

DEFINITIONS_FILE = "areas.json"
GEOCODE_CACHE = "geocode-cache.json"
REQUEST_KEYS = {"name", "query", "zip", "country", "region", "timezone", "latitude", "longitude", "radius_miles"}
LOCATION_KEYS = REQUEST_KEYS - {"name"}
DEFAULT_LIMITS = {
    "max_areas": 8,                    # areas besides the default one
    "max_radius_miles": 100,
    "creations_per_hour": 10,          # new areas only; re-selecting an existing area is free
    "geocodes_per_hour": 20,           # uncached lookups against the public geocoding services
    "refresh_interval_seconds": 3600,  # automatic refresh of active areas
    "manual_refresh_min_seconds": 900,
    "pause_after_days": 7,             # areas nobody has read stop refreshing ...
    "delete_after_days": 30,           # ... and are removed after this long
    "max_names_per_area": 5,
}
DAY = 86400


class ApiError(Exception):
    """An error the HTTP layer reports verbatim."""

    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


class _Window:
    """Sliding-window counter: at most `limit` events per `span` seconds."""

    def __init__(self, limit, span, clock):
        self.limit, self.span, self.clock, self.times = limit, span, clock, []

    def take(self):
        now = self.clock()
        self.times = [t for t in self.times if now - t < self.span]
        if len(self.times) >= self.limit:
            return False
        self.times.append(now)
        return True

    def retry_after(self):
        return max(1, int(self.span - (self.clock() - min(self.times)))) if self.times else 1


class _CountingProvider:
    """Wraps a geocoding provider so uncached lookups count against the hourly budget."""

    def __init__(self, inner, window):
        self.inner, self.window, self.name = inner, window, getattr(inner, "name", "")

    def lookup(self, fetcher, spec):
        if not self.window.take():
            raise LocationError("the hourly geocoding budget is used up; try again later or use coordinates")
        return self.inner.lookup(fetcher, spec)


def _text(value, limit, what):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit or not value.isprintable():
        raise ApiError(422, "invalid_request", f"{what} must be printable text of 1-{limit} characters")
    return " ".join(value.split())


class AreaManager:
    def __init__(self, data_dir, config_loader=load_config, refresh_fn=refresh, clock=time.time, providers=None, fetcher=None,
                 limits=None, slot=None):
        self.store = AreaStore(data_dir)
        self.config_loader, self.refresh_fn, self.clock = config_loader, refresh_fn, clock
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.providers = providers or PROVIDERS
        self.fetcher = fetcher or Fetcher(2.0)
        self.slot = slot or threading.Lock()  # shared with the default area's refresh: one discovery job at a time
        self._lock = threading.RLock()
        self._geocode_lock = threading.Lock()
        self._creations = _Window(self.limits["creations_per_hour"], 3600, clock)
        self._geocodes = _Window(self.limits["geocodes_per_hour"], 3600, clock)
        self._running = None
        self._definitions_path = os.path.join(self.store.root, DEFINITIONS_FILE)
        self._feeds = {}  # area id -> (mtime_ns, raw, etag) of the last valid feed
        self.areas = self._load()

    # --- persistence ----------------------------------------------------------------

    def _load(self):
        try:
            with open(self._definitions_path, encoding="utf-8") as handle:
                data = json.load(handle).get("areas", {})
        except (OSError, ValueError, AttributeError):
            return {}
        return {key: value for key, value in data.items() if isinstance(value, dict) and self._valid_stored(key, value)}

    def _valid_stored(self, key, value):
        try:
            validate_area_id(key)
            return key != DEFAULT_AREA and derive_area_id(value["location"]) == key and isinstance(value.get("names"), list)
        except (AreaError, KeyError, TypeError):
            return False

    def _save(self):
        write_atomic(self._definitions_path, json.dumps({"areas": self.areas}, indent=2) + "\n")

    # --- requests -------------------------------------------------------------------

    def _validate_request(self, body):
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
        name = _text(body["name"], 60, "name") if "name" in body else None
        return raw, name

    def _resolve(self, raw):
        providers = {key: _CountingProvider(value, self._geocodes) for key, value in self.providers.items()}
        try:
            with self._geocode_lock:  # the public geocoder allows one request per second
                return resolve_location(raw, self.fetcher, os.path.join(self.store.root, GEOCODE_CACHE), providers, self.clock,
                                        timezone_lookup=False)
        except LocationError as error:
            raise ApiError(422, "location", str(error)) from error

    def ensure_area(self, body):
        """Create the area for a request (or return the existing one). Returns (public dict, created)."""
        raw, name = self._validate_request(body)
        resolved = self._resolve(raw)
        key = derive_area_id(resolved)
        with self._lock:
            existing = self.areas.get(key)
            if name:
                holder = next((a for a in self.areas.values() if name in a["names"] and a["id"] != key), None)
                if holder:
                    raise ApiError(409, "name_taken", f"the name '{name}' already belongs to another area")
            if existing is None:
                if len(self.areas) >= self.limits["max_areas"]:
                    raise ApiError(429, "area_limit", "the area limit is reached; delete an unused area first")
                if not self._creations.take():
                    raise ApiError(429, "creation_rate", "too many new areas; try again later",
                                   {"Retry-After": str(self._creations.retry_after())})
                now = self.clock()
                location = {k: resolved[k] for k in ("display_name", "latitude", "longitude", "radius_miles", "country", "region", "timezone")
                            if resolved.get(k) is not None}
                existing = {"id": key, "names": [], "location": location, "created_at": now, "last_read_at": now,
                            "refresh_requested": True}
                self.store.ensure(key, location)
                self.areas[key] = existing
                created = True
            else:
                created = False
            label = name or (existing["names"][0] if existing["names"] else resolved["display_name"])
            if label not in existing["names"] and len(existing["names"]) < self.limits["max_names_per_area"]:
                existing["names"].append(label)
            existing["last_read_at"] = self.clock()
            self._save()
            return self.describe(key), created

    def delete_area(self, area_id):
        if area_id == DEFAULT_AREA:
            raise ApiError(403, "forbidden", "the default area cannot be deleted")
        with self._lock:
            self._require(area_id)
            self._remove(area_id)

    def _remove(self, area_id):
        directory = self.store.directory(area_id)  # validated, inside <data>/areas
        self.areas.pop(area_id, None)
        self._feeds.pop(area_id, None)
        self._save()
        shutil.rmtree(directory, ignore_errors=True)

    def _require(self, area_id):
        try:
            validate_area_id(area_id)
        except AreaError as error:
            raise ApiError(404, "not_found", "no such area") from error
        if area_id not in self.areas:
            raise ApiError(404, "not_found", "no such area")
        return self.areas[area_id]

    def request_refresh(self, area_id):
        if area_id == DEFAULT_AREA:
            raise ApiError(403, "forbidden", "the default area refreshes on its own schedule")
        with self._lock:
            area = self._require(area_id)
            wait = self.limits["manual_refresh_min_seconds"] - (self.clock() - area.get("last_manual_refresh_at", 0))
            if wait > 0 and area.get("last_manual_refresh_at"):
                raise ApiError(429, "refresh_rate", "this area was refreshed manually a moment ago", {"Retry-After": str(int(wait) + 1)})
            area["last_manual_refresh_at"] = self.clock()
            area["refresh_requested"] = True
            area["last_read_at"] = self.clock()
            self._save()
            return self.describe(area_id)

    # --- reading --------------------------------------------------------------------

    def describe(self, area_id):
        area = self._require(area_id)
        paths = self.store.paths(area_id)
        status = load_status(paths)
        if self._running == area_id:
            state = "running"
        elif area.get("refresh_requested") or "last_attempt_at" not in status:
            state = "queued"
        else:
            state = {0: "ok", 2: "degraded"}.get(status.get("last_exit_code"), "failed")
        feed = self._read_feed_file(area_id)
        return {
            "id": area_id, "names": list(area["names"]), "location": dict(area["location"]),
            "created_at": area["created_at"], "last_read_at": area["last_read_at"],
            "status": {"state": state, "last_attempt_at": status.get("last_attempt_at"), "last_success_at": status.get("last_success_at"),
                       "last_exit_code": status.get("last_exit_code"), "last_error": status.get("last_error"),
                       "last_duration_seconds": status.get("last_duration_seconds"), "next_run_at": self._next_run_iso(area, status),
                       "paused": self._paused(area), "events": len(feed[2]["events"]) if feed else 0,
                       "feed_generated_at": feed[2].get("generated_at") if feed else None},
        }

    def list_areas(self):
        with self._lock:
            return [self.describe(key) for key in sorted(self.areas, key=lambda k: self.areas[k]["created_at"])]

    def _read_feed_file(self, area_id):
        """(etag, raw bytes, document) of the last valid feed, or None. A corrupt file never replaces a good copy."""
        path = self.store.paths(area_id).feed
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return None
        cached = self._feeds.get(area_id)
        if cached and cached[0] == mtime:
            return cached[2], cached[1], cached[3]
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
            document = json.loads(raw)
            validate_feed(document)
        except (OSError, ValueError, FeedError):
            return (cached[2], cached[1], cached[3]) if cached else None
        etag = '"' + hashlib.sha256(raw).hexdigest()[:20] + '"'
        self._feeds[area_id] = (mtime, raw, etag, document)
        return etag, raw, document

    def read_feed(self, area_id):
        """(etag, raw feed bytes) for an area, marking it as read; None if it has no valid feed yet."""
        with self._lock:
            area = self._require(area_id)
            area["last_read_at"] = self.clock()
        feed = self._read_feed_file(area_id)
        return (feed[0], feed[1]) if feed else None

    # --- scheduling -----------------------------------------------------------------

    def _paused(self, area):
        return self.clock() - area["last_read_at"] > self.limits["pause_after_days"] * DAY

    def _next_run(self, area, status):
        if area.get("refresh_requested") or "last_attempt_at" not in status:
            return None  # due now
        jitter = zlib.crc32(area["id"].encode()) % 600  # spreads areas out so they do not all start together
        return (status.get("last_attempt_ts") or 0) + self.limits["refresh_interval_seconds"] + jitter

    def _next_run_iso(self, area, status):
        when = self._next_run(area, status)
        return _iso(when) if when else None

    def due(self):
        """Area ids ready to refresh, most overdue first. Paused areas only run when someone asked for a refresh."""
        now, ready = self.clock(), []
        with self._lock:
            for key, area in self.areas.items():
                status = load_status(self.store.paths(key))
                if area.get("refresh_requested"):
                    ready.append((0, key))
                elif not self._paused(area):
                    when = self._next_run(area, status)
                    if when is None or when <= now:
                        ready.append((when or 0, key))
        return [key for _, key in sorted(ready)]

    def run_area(self, area_id):
        """Refresh one area now (blocking). Returns the exit code. Failures stay inside this area."""
        with self._lock:
            area = self._require(area_id)
            location = {k: v for k, v in area["location"].items() if k != "display_name"}
            location["name"] = area["location"]["display_name"]
            self._running = area_id
        paths = self.store.ensure(area_id, area["location"])
        started = self.clock()
        buffer = io.StringIO()
        code = 1
        try:
            with self.slot:  # at most one discovery job at a time across all areas
                config = {**self.config_loader(), "location": location}
                with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                    code = self.refresh_fn(config, {}, fetcher=self.fetcher, **paths.refresh_arguments())
        except Exception as error:  # an unexpected bug in one area must not stop the others
            buffer.write(f"{type(error).__name__}: {error}\n")
            code = 1
        finally:
            finished = self.clock()
            status = load_status(paths)
            lines = [line.strip() for line in buffer.getvalue().splitlines() if line.strip()]
            # Prefer the line that names the cause ("... FAILED ...: site down") over the summary that follows it.
            problem = next((line for key in ("failed", "error", "refusing", "no source") for line in lines if key in line.lower()), None)
            status.update(last_attempt_at=_iso(finished), last_attempt_ts=finished, last_exit_code=code,
                          last_duration_seconds=round(finished - started, 1))
            if code in (0, 2):
                status["last_success_at"] = _iso(finished)
            status["last_error"] = None if code == 0 else (problem or "refresh failed")
            try:
                save_status(paths, status)
            except OSError:
                pass
            with self._lock:
                if area_id in self.areas:
                    self.areas[area_id]["refresh_requested"] = False
                    self._save()
                self._running = None
        return code

    def run_next(self):
        """Run the most overdue area, if any. Returns its id or None."""
        for area_id in self.due():
            self.run_area(area_id)
            return area_id
        return None

    def cleanup(self):
        """Remove areas nobody has read for `delete_after_days`. Returns the removed ids."""
        removed = []
        with self._lock:
            for key, area in list(self.areas.items()):
                if self.clock() - area["last_read_at"] > self.limits["delete_after_days"] * DAY:
                    self._remove(key)
                    removed.append(key)
        return removed

    def run_forever(self, stop, poll_seconds=5):
        while not stop.is_set():
            try:
                self.cleanup()
                ran = self.run_next()
            except Exception:  # keep the worker alive whatever happens to one area
                ran = None
            if not ran:
                stop.wait(poll_seconds)

    def summary(self):
        with self._lock:
            states = [self.describe(key)["status"]["state"] for key in self.areas]
        return {"areas": len(states), "limit": self.limits["max_areas"], "running": self._running,
                "by_state": {state: states.count(state) for state in sorted(set(states))}}


def _iso(timestamp):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")
