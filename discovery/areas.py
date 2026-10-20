"""Per-area storage.

Every discovery area owns one directory holding everything that depends on where it is:

    events.json          the published feed
    source-cache.json    per-source cached events and refresh results
    location-cache.json  geocoding lookups made for this area
    status.json          last attempt / last success / exit code
    .refresh.lock        keeps two refreshes of the same area from overlapping
    location-hint.json   optional Home Assistant / browser location hint

The reserved area `default` is the existing top-level data directory, so a deployment's files, volume and
rollback do not change. Other areas live under `<data>/areas/<area_id>/`, where the id is derived from the
area's canonical coordinates and radius, so the same place always maps to the same directory and a different
place never shares one. Ids are validated before they touch the filesystem. Nothing here starts discovery for
a non-default area; it only provides the isolated storage and the guard rails.
"""
import hashlib
import json
import os
import re
from dataclasses import dataclass

from .publish import write_atomic

DEFAULT_AREA = "default"
AREAS_DIR = "areas"
AREA_META = "area.json"
AREA_ID = re.compile(r"^a_[0-9a-f]{12}$")
COORDINATE_DIGITS = 4  # about 11 m: finer differences are the same place
RADIUS_DIGITS = 1


class AreaError(ValueError):
    """An unsafe or inconsistent area id, or storage that does not belong to the requested area."""


def canonical_location(location):
    """The part of a location that identifies an area: rounded coordinates and radius, as stable text."""
    try:
        latitude, longitude, radius = float(location["latitude"]), float(location["longitude"]), float(location["radius_miles"])
    except (KeyError, TypeError, ValueError) as error:
        raise AreaError("a location needs latitude, longitude and radius_miles") from error
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and radius > 0):
        raise AreaError("location coordinates or radius are out of range")
    return {"latitude": f"{latitude + 0.0:.{COORDINATE_DIGITS}f}".replace("-0.0000", "0.0000"),
            "longitude": f"{longitude + 0.0:.{COORDINATE_DIGITS}f}".replace("-0.0000", "0.0000"),
            "radius_miles": f"{radius:.{RADIUS_DIGITS}f}"}


def derive_area_id(location):
    """`a_` plus 12 hex digits of the hash of the canonical location (names, timezones and wording do not matter)."""
    canonical = json.dumps(canonical_location(location), sort_keys=True, separators=(",", ":"))
    return "a_" + hashlib.sha256(canonical.encode()).hexdigest()[:12]


def validate_area_id(value):
    if not isinstance(value, str) or not (value == DEFAULT_AREA or AREA_ID.match(value)):
        raise AreaError(f"invalid area id: {value!r}")
    return value


@dataclass(frozen=True)
class AreaPaths:
    area_id: str
    directory: str
    feed: str
    source_cache: str
    location_cache: str
    status: str
    lock: str
    hint: str

    def refresh_arguments(self):
        """Keyword arguments for discovery.refresh.refresh() that point it at this area's files only."""
        return {"output": self.feed, "cache_path": self.source_cache, "lock_path": self.lock, "location_cache_path": self.location_cache}


class AreaStore:
    def __init__(self, data_dir):
        self.root = os.path.abspath(data_dir)

    def directory(self, area_id):
        validate_area_id(area_id)
        if area_id == DEFAULT_AREA:
            return self.root
        path = os.path.abspath(os.path.join(self.root, AREAS_DIR, area_id))
        areas_root = os.path.realpath(os.path.join(self.root, AREAS_DIR))
        # Defence in depth: the id regex already forbids separators and dots; also refuse symlinks that leave the area tree.
        if os.path.dirname(path) != os.path.abspath(os.path.join(self.root, AREAS_DIR)) or \
                (os.path.exists(path) and os.path.dirname(os.path.realpath(path)) != areas_root):
            raise AreaError(f"area directory escapes the storage root: {area_id}")
        return path

    def paths(self, area_id):
        directory = self.directory(area_id)
        join = lambda name: os.path.join(directory, name)
        return AreaPaths(area_id, directory, join("events.json"), join("source-cache.json"), join("location-cache.json"),
                         join("status.json"), join(".refresh.lock"), join("location-hint.json"))

    def ensure(self, area_id, location=None):
        """Create the area's directory (non-default areas) and bind it to its location.

        For a derived id the stored `area.json` must match the id, and a given location must hash to it, so a
        directory can never be reused for a different place.
        """
        paths = self.paths(area_id)
        if area_id == DEFAULT_AREA:
            return paths
        if location is not None and area_id != derive_area_id(location):
            raise AreaError(f"location does not belong to area {area_id}")
        os.makedirs(paths.directory, exist_ok=True)
        meta_path = os.path.join(paths.directory, AREA_META)
        try:
            with open(meta_path, encoding="utf-8") as handle:
                stored = json.load(handle)
        except FileNotFoundError:
            stored = None
        except (OSError, ValueError) as error:
            raise AreaError(f"area {area_id} has unreadable metadata: {error}") from error
        if stored is not None:
            if stored.get("id") != area_id or area_id != derive_area_id(stored.get("location") or {}):
                raise AreaError(f"area {area_id} metadata does not match its id")
        elif location is not None:
            write_atomic(meta_path, json.dumps({"id": area_id, "location": canonical_location(location)}, indent=2) + "\n")
        return paths

    def list_areas(self):
        """Ids of the areas that have storage (default is always present)."""
        found = [DEFAULT_AREA]
        folder = os.path.join(self.root, AREAS_DIR)
        if os.path.isdir(folder):
            found += sorted(name for name in os.listdir(folder) if AREA_ID.match(name) and os.path.isdir(os.path.join(folder, name)))
        return found


def load_status(paths):
    try:
        with open(paths.status, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_status(paths, status):
    """Atomic: a crash or a full disk leaves the previous status file intact."""
    write_atomic(paths.status, json.dumps(status, indent=2) + "\n")
