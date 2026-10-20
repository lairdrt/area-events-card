"""Data-driven source registry.

Each entry of the configuration's `sources` list describes one source:

    {"id": "kpbs", "name": "KPBS Events", "adapter": "kpbs", "url": "https://www.kpbs.org/events/all",
     "enabled": true, "publisher": "kpbs.org",
     "coverage": {"type": "area", "latitude": 32.7157, "longitude": -117.1611, "radius_miles": 45, "name": "San Diego County"},
     "options": {"days": 3}}

`coverage` says where the source has events:
    area     a center and radius; selected when that circle overlaps the search area
    global   searchable anywhere; always selected and given the search location
    unknown  (or omitted) not declared; never selected, because unknown coverage must not mean everywhere
Nothing about a place is built into the code; the examples/ folder holds ready-made configurations.
"""
import re
from urllib.parse import urlparse

from . import sources as adapters
from .location import distance_miles

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
RESERVED_OPTIONS = {"details", "limit", "source_id", "source_name", "url", "location"}
MAX_COVERAGE_RADIUS = 5000


class RegistryError(ValueError):
    """The sources configuration is invalid."""


class Source:
    def __init__(self, id, name, adapter, url, publisher, enabled, coverage, options, module):
        self.id, self.name, self.adapter, self.url, self.publisher = id, name, adapter, url, publisher
        self.enabled, self.coverage, self.options, self.module = enabled, coverage, options, module

    @property
    def wants_location(self):
        return bool(getattr(self.module, "ACCEPTS_LOCATION", False)) or self.coverage["type"] == "global"

    def discover(self, fetcher, previous, details=True, limit=None, location=None):
        options = dict(self.options, source_id=self.id, source_name=self.name, url=self.url)
        if self.wants_location:
            options["location"] = location
        return self.module.discover(fetcher, previous, details=details, limit=limit, **options)


def _coverage(raw, where):
    if raw is None:
        return {"type": "unknown"}
    if not isinstance(raw, dict) or raw.get("type") not in ("area", "global", "unknown"):
        raise RegistryError(f"{where}: coverage.type must be 'area', 'global' or 'unknown'")
    if raw["type"] != "area":
        return {"type": raw["type"]}
    try:
        latitude, longitude, radius = float(raw["latitude"]), float(raw["longitude"]), float(raw["radius_miles"])
    except (KeyError, TypeError, ValueError) as error:
        raise RegistryError(f"{where}: an 'area' coverage needs numeric latitude, longitude and radius_miles") from error
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and 0 < radius <= MAX_COVERAGE_RADIUS):
        raise RegistryError(f"{where}: coverage coordinates or radius are out of range")
    return {"type": "area", "latitude": latitude, "longitude": longitude, "radius_miles": radius, "name": raw.get("name")}


def _publisher(url):
    host = (urlparse(url).hostname or "").removeprefix("www.")
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) > 2 else host


def load_sources(config):
    """Validate the `sources` list and return Source objects (including disabled ones)."""
    entries = config.get("sources")
    if not isinstance(entries, list) or not entries:
        raise RegistryError("configuration needs a non-empty 'sources' list")
    sources, seen_ids, single_use = [], set(), set()
    for number, raw in enumerate(entries, 1):
        if not isinstance(raw, dict):
            raise RegistryError(f"sources[{number}] must be an object")
        if "key" in raw and "id" not in raw:
            raise RegistryError(f"sources[{number}] uses the old 'key' format; use id, name, adapter, url and coverage (see examples/)")
        source_id = raw.get("id")
        where = f"source '{source_id}'" if isinstance(source_id, str) else f"sources[{number}]"
        if not isinstance(source_id, str) or not ID_PATTERN.match(source_id):
            raise RegistryError(f"{where}: id must be lowercase letters, digits or underscores")
        if source_id in seen_ids:
            raise RegistryError(f"{where}: duplicate source id")
        seen_ids.add(source_id)
        kind = raw.get("adapter")
        if kind not in adapters.ADAPTERS:
            raise RegistryError(f"{where}: unknown adapter '{kind}' (available: {', '.join(sorted(adapters.ADAPTERS))})")
        module = adapters.adapter_module(kind)
        if not getattr(module, "MULTIPLE", False):
            if kind in single_use:
                raise RegistryError(f"{where}: the '{kind}' adapter can be registered only once")
            single_use.add(kind)
        url = raw.get("url")
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not urlparse(url).hostname:
            raise RegistryError(f"{where}: url must be an absolute http(s) URL")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RegistryError(f"{where}: enabled must be true or false")
        options = raw.get("options", {})
        if not isinstance(options, dict) or RESERVED_OPTIONS & set(options):
            raise RegistryError(f"{where}: options must be an object and may not set {', '.join(sorted(RESERVED_OPTIONS))}")
        name = raw.get("name", source_id)
        if not isinstance(name, str) or not name.strip():
            raise RegistryError(f"{where}: name must be a non-empty string")
        publisher = raw.get("publisher") or _publisher(url)
        sources.append(Source(source_id, name.strip(), kind, url, publisher, enabled, _coverage(raw.get("coverage"), where), options, module))
    return sources


def covers(coverage, location):
    """(eligible, reason) for a coverage declaration against a normalized location."""
    kind = coverage["type"]
    if kind == "global":
        return True, "searchable anywhere"
    if kind != "area":
        return False, "coverage is not specified, so it is not assumed to apply here"
    gap = distance_miles(coverage["latitude"], coverage["longitude"], location["latitude"], location["longitude"])
    reach = coverage["radius_miles"] + location["radius_miles"]
    area = coverage.get("name") or "its area"
    if gap <= reach:
        return True, f"{area} overlaps the search area ({gap:.0f} mi apart)"
    return False, f"{area} is {gap:.0f} mi from the search area, beyond {reach:.0f} mi"


def select_sources(sources, location):
    """Split sources into (selected, skipped); skipped is a list of (source, reason)."""
    selected, skipped = [], []
    for source in sources:
        if not source.enabled:
            skipped.append((source, "disabled"))
            continue
        eligible, reason = covers(source.coverage, location)
        if eligible:
            selected.append((source, reason))
        else:
            skipped.append((source, reason))
    return selected, skipped
