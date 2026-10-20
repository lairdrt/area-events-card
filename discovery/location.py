"""Configurable discovery area.

Nothing here knows any particular place. The `location` block of the
configuration names the area in exactly one of three ways:

    {"query": "Austin, TX", "country": "US", "region": "TX", "radius_miles": 25}
    {"zip": "78701", "country": "US", "radius_miles": 25}
    {"latitude": 30.2672, "longitude": -97.7431, "name": "Downtown Austin", "timezone": "America/Chicago", "radius_miles": 25}

and is resolved to a normalized dict:

    {display_name, latitude, longitude, radius_miles, timezone, timezone_source, country, region, source, query}

Explicit coordinates need no geocoding. Queries and ZIP codes use free,
key-less providers behind a small interface (Nominatim, Zippopotam.us); every
lookup is cached on disk so it is not repeated on each refresh. A timezone is
used only when it is configured or looked up (timeapi.io, cached); it is never
inferred from a country or state. Without one, `timezone` is None and event
times keep the UTC offsets their sources give.
"""
import json
import math
import re
import time
import urllib.parse

from .core import FetchError

DEFAULT_RADIUS_MILES = 25
MAX_RADIUS_MILES = 250
CACHE_TTL_SECONDS = 180 * 24 * 3600  # a place does not move; stale entries are still used if a lookup fails
EARTH_RADIUS_MILES = 3958.8
TIMEZONE_PATTERN = re.compile(r"^(UTC|[A-Za-z]+(/[A-Za-z0-9_+-]+)+)$")


class LocationError(Exception):
    """The configured location is invalid or could not be resolved."""


# --- providers (interchangeable; no API keys) ----------------------------------------------

class GeocodingProvider:
    name = ""

    def url(self, spec):  # pragma: no cover - interface
        raise NotImplementedError

    def parse(self, payload, spec):  # pragma: no cover - interface
        """Return {display_name, latitude, longitude, country, region} or None when nothing matched."""
        raise NotImplementedError

    def lookup(self, fetcher, spec):
        # Geocoding APIs are not crawled pages, so robots.txt does not apply, but the fetcher still
        # sends the descriptive User-Agent and spaces requests (Nominatim allows at most 1 per second).
        try:
            payload = json.loads(fetcher.get(self.url(spec), check_robots=False))
        except (FetchError, ValueError, AttributeError) as error:
            raise LocationError(f"{self.name} lookup failed: {error}") from error
        return self.parse(payload, spec)


class NominatimProvider(GeocodingProvider):
    """OpenStreetMap Nominatim. Usage policy: identifying User-Agent, <= 1 request/second, cache results."""
    name = "nominatim"

    def url(self, spec):
        params = {"q": spec["query"], "format": "jsonv2", "limit": 1, "addressdetails": 1}
        if spec.get("country"):
            params["countrycodes"] = spec["country"].lower()
        return "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)

    def parse(self, payload, spec):
        if not isinstance(payload, list) or not payload:
            return None
        hit = payload[0]
        address = hit.get("address") or {}
        region = address.get("ISO3166-2-lvl4", "")
        return {
            "display_name": hit.get("display_name") or spec["query"],
            "latitude": float(hit["lat"]), "longitude": float(hit["lon"]),
            "country": (address.get("country_code") or "").upper() or None,
            "region": region.split("-")[-1] if region else address.get("state"),
        }


class ZippopotamProvider(GeocodingProvider):
    """Zippopotam.us postal-code lookup (free, no key)."""
    name = "zippopotam"

    def url(self, spec):
        return f"https://api.zippopotam.us/{urllib.parse.quote(spec['country'].lower())}/{urllib.parse.quote(spec['zip'])}"

    def parse(self, payload, spec):
        places = payload.get("places") if isinstance(payload, dict) else None
        if not places:
            return None
        place = places[0]
        return {
            "display_name": f"{place.get('place name', spec['zip'])}, {place.get('state abbreviation') or place.get('state', '')} {spec['zip']}".replace(" ,", ","),
            "latitude": float(place["latitude"]), "longitude": float(place["longitude"]),
            "country": (payload.get("country abbreviation") or spec["country"]).upper(),
            "region": place.get("state abbreviation") or place.get("state"),
        }


class TimezoneProvider:
    """timeapi.io coordinate -> IANA zone lookup (free, no key)."""
    name = "timeapi"

    def lookup(self, fetcher, latitude, longitude):
        url = f"https://timeapi.io/api/TimeZone/coordinate?latitude={latitude:.4f}&longitude={longitude:.4f}"
        try:
            zone = json.loads(fetcher.get(url, check_robots=False)).get("timeZone")
        except (FetchError, ValueError, AttributeError):
            return None
        return zone if isinstance(zone, str) and TIMEZONE_PATTERN.match(zone) else None


PROVIDERS = {"nominatim": NominatimProvider(), "zippopotam": ZippopotamProvider()}
TIMEZONE_PROVIDER = TimezoneProvider()


# --- configuration -> normalized location ----------------------------------------------------

def parse_spec(raw):
    """Validate the `location` config block; returns a normalized spec dict."""
    if raw is None:
        raise LocationError("no location configured: add a 'location' block (query, zip or latitude/longitude) to the configuration")
    if not isinstance(raw, dict):
        raise LocationError("location must be an object with query, zip or latitude/longitude")
    radius = raw.get("radius_miles", DEFAULT_RADIUS_MILES)
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not 1 <= radius <= MAX_RADIUS_MILES:
        raise LocationError(f"location.radius_miles must be a number from 1 to {MAX_RADIUS_MILES}")
    has_coordinates = "latitude" in raw or "longitude" in raw
    chosen = [name for name, present in (("query", "query" in raw), ("zip", "zip" in raw), ("coordinates", has_coordinates)) if present]
    if len(chosen) != 1:
        raise LocationError("location needs exactly one of: query, zip, or latitude and longitude")

    country = raw.get("country")
    if country is not None and (not isinstance(country, str) or len(country.strip()) != 2 or not country.strip().isalpha()):
        raise LocationError("location.country must be a 2-letter country code")
    timezone_name = raw.get("timezone")
    if timezone_name is not None and (not isinstance(timezone_name, str) or not TIMEZONE_PATTERN.match(timezone_name)):
        raise LocationError("location.timezone must be an IANA name such as America/Chicago")
    spec = {"radius_miles": float(radius), "provider": raw.get("provider"), "name": raw.get("name"),
            "country": country.strip().upper() if country else None, "region": raw.get("region"), "timezone": timezone_name}

    if chosen[0] == "query":
        query = raw["query"]
        if not isinstance(query, str) or not query.strip():
            raise LocationError("location.query must be a non-empty string")
        spec.update(kind="query", query=" ".join(query.split()))
    elif chosen[0] == "zip":
        zip_code = str(raw["zip"]).strip()
        if not zip_code or len(zip_code) > 12 or not spec["country"]:
            raise LocationError("location.zip needs a postal code and a 2-letter location.country")
        spec.update(kind="zip", zip=zip_code)
    else:
        try:
            latitude, longitude = float(raw["latitude"]), float(raw["longitude"])
        except (KeyError, TypeError, ValueError) as error:
            raise LocationError("location needs both numeric latitude and longitude") from error
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise LocationError("location latitude must be within -90..90 and longitude within -180..180")
        spec.update(kind="coordinates", latitude=latitude, longitude=longitude)
    return spec


def cache_key(spec):
    context = f"|{spec['country'] or ''}"
    if spec["kind"] == "query":
        return f"query:{spec['query'].lower()}{context}"
    return f"zip:{spec['country']}:{spec['zip']}"


def load_cache(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_cache(path, cache):
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=2)
    except (OSError, TypeError):
        pass  # not cacheable; the next refresh will look it up again


def cached_lookup(cache_path, key, lookup, now=time.time):
    """Run `lookup()` unless a fresh cached answer exists; a stale answer is reused if the lookup fails.

    A lookup returns a value, None (no match; not cached) or raises LocationError. Returns (value, how).
    """
    cache = load_cache(cache_path) if cache_path else {}
    entry = cache.get(key)
    if entry and now() - entry.get("resolved_at", 0) < CACHE_TTL_SECONDS:
        return entry["place"], "cache"
    try:
        value = lookup()
    except LocationError:
        if entry:
            return entry["place"], "cache (stale)"
        raise
    if value is not None and cache_path:
        cache[key] = {"place": value, "resolved_at": now()}
        save_cache(cache_path, cache)
    return value, "lookup"


def _timezone_for(spec, latitude, longitude, fetcher, cache_path, now):
    if spec["timezone"]:
        return spec["timezone"], "config"
    if fetcher is None:
        return None, "unknown"
    try:
        zone, _ = cached_lookup(cache_path, f"tz:{latitude:.2f},{longitude:.2f}",
                                lambda: TIMEZONE_PROVIDER.lookup(fetcher, latitude, longitude), now)
    except LocationError:
        zone = None
    return (zone, "lookup:timeapi") if zone else (None, "unknown")


def _finish(spec, place, source, timezone_name, timezone_source):
    return {
        "basis": spec.get("basis", "configured"),
        "display_name": spec["name"] or place["display_name"],
        "latitude": round(place["latitude"], 5), "longitude": round(place["longitude"], 5),
        "radius_miles": spec["radius_miles"],
        "timezone": timezone_name, "timezone_source": timezone_source,
        "country": place.get("country") or spec["country"], "region": place.get("region") or spec["region"],
        "source": source, "query": spec.get("query") or spec.get("zip"),
    }


def resolve_location(raw, fetcher=None, cache_path=None, providers=None, now=time.time):
    """Return the normalized location for the `location` config block, or raise LocationError."""
    spec = parse_spec(raw)
    if spec["kind"] == "coordinates":
        place = {"display_name": f"{spec['latitude']:.4f}, {spec['longitude']:.4f}",
                 "latitude": spec["latitude"], "longitude": spec["longitude"]}
        zone, zone_source = _timezone_for(spec, spec["latitude"], spec["longitude"], fetcher, cache_path, now)
        return _finish(spec, place, "config", zone, zone_source)

    providers = providers or PROVIDERS
    name = spec["provider"] or ("zippopotam" if spec["kind"] == "zip" else "nominatim")
    provider = providers.get(name)
    if provider is None:
        raise LocationError(f"unknown geocoding provider '{name}' (available: {', '.join(sorted(providers))})")
    label = spec.get("query") or f"ZIP {spec['zip']} ({spec['country']})"
    try:
        place, how = cached_lookup(cache_path, cache_key(spec), lambda: provider.lookup(fetcher, spec), now)
    except LocationError as error:
        raise LocationError(f"could not resolve location '{label}': {error}") from error
    if not place:
        raise LocationError(f"could not resolve location '{label}': {name} found no match")
    if spec["country"] and place.get("country") and place["country"] != spec["country"]:
        raise LocationError(f"location '{label}' resolved to country {place['country']}, but {spec['country']} is configured")
    zone, zone_source = _timezone_for(spec, place["latitude"], place["longitude"], fetcher, cache_path, now)
    return _finish(spec, place, how if how != "lookup" else f"geocoded:{name}", zone, zone_source)


# --- where the area comes from: configuration, then Home Assistant, then the browser ----------------------------

HINT_FILE = "location-hint.json"
HINT_SOURCES = ("home_assistant", "browser")  # priority order after an explicit configuration
HINT_LABELS = {"home_assistant": "Home Assistant home location", "browser": "Browser location"}


def read_hints(path):
    """Read the optional location hint file. Returns (valid hints by source, problems).

    The hint file is the backend's only input for a location that came from Home Assistant or a browser.
    Nothing remote writes it: an operator (or a future authenticated bridge) does, for example with
    `python -m discovery.hint`. Malformed entries are ignored and reported, never repaired.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}, []
    except (OSError, ValueError) as error:
        return {}, [f"location hint file is unreadable: {error}"]
    hints, problems = {}, []
    for source in HINT_SOURCES:
        entry = raw.get(source) if isinstance(raw, dict) else None
        if entry is None:
            continue
        try:
            spec = parse_spec({k: entry.get(k) for k in ("latitude", "longitude", "timezone") if entry.get(k) is not None})
            if spec["kind"] != "coordinates" or (spec["latitude"] == 0 and spec["longitude"] == 0):
                raise LocationError("needs real latitude and longitude")
        except (LocationError, AttributeError) as error:
            problems.append(f"{HINT_LABELS[source]} hint ignored: {error}")
            continue
        name = entry.get("display_name")
        hints[source] = {"latitude": spec["latitude"], "longitude": spec["longitude"], "timezone": spec["timezone"],
                         "name": name if isinstance(name, str) and name.strip() else None,
                         "accuracy_m": entry.get("accuracy_m"), "received_at": entry.get("received_at")}
    return hints, problems


def resolve_area(config, hint_path=None, fetcher=None, cache_path=None, providers=None, now=time.time):
    """The discovery area, by priority:

      1. an explicit `location` in the configuration
      2. the Home Assistant home location from the hint file
      3. a browser location from the hint file
      4. otherwise LocationError asking for configuration

    Hint locations use `default_radius_miles` from the configuration (default 25). The result's `basis` says
    which rule applied. A hint records where Home Assistant or a browser said a place was; it is never
    treated as the user's current physical position.
    """
    if config.get("location") is not None:
        return resolve_location(config["location"], fetcher, cache_path, providers, now)
    hints, problems = read_hints(hint_path) if hint_path else ({}, [])
    for source in HINT_SOURCES:
        hint = hints.get(source)
        if not hint:
            continue
        raw = {"latitude": hint["latitude"], "longitude": hint["longitude"], "name": hint["name"] or HINT_LABELS[source],
               "radius_miles": config.get("default_radius_miles", DEFAULT_RADIUS_MILES), "timezone": hint["timezone"]}
        spec = parse_spec({k: v for k, v in raw.items() if v is not None})
        spec["basis"] = source
        place = {"display_name": f"{hint['latitude']:.4f}, {hint['longitude']:.4f}", "latitude": hint["latitude"], "longitude": hint["longitude"]}
        zone, zone_source = _timezone_for(spec, spec["latitude"], spec["longitude"], fetcher, cache_path, now)
        location = _finish(spec, place, f"hint:{source}", zone, zone_source)
        location.update(received_at=hint["received_at"], accuracy_m=hint["accuracy_m"])
        return location
    detail = ("; " + "; ".join(problems)) if problems else ""
    raise LocationError("no location available: set 'location' in the configuration, or provide a Home Assistant or browser "
                        f"location hint (python -m discovery.hint){detail}")


def distance_miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))
