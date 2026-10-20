"""Configurable geographic coverage.

The configured `location` is turned into a normalized location dict:

    {display_name, latitude, longitude, radius_miles, timezone, country, region, source, query}

by one of three routes: explicit coordinates (no network), the built-in San
Diego entry (the default, and "San Diego, CA", no network), or a free
geocoding provider (no API key) whose result is cached on disk so it is not
looked up again on every refresh. Source adapters declare a COVERAGE dict;
`select_sources` runs only those whose area overlaps the requested one.

Coverage kinds (the distinction is kept so the three can grow independently):
    regional    a fixed area (center + radius); runs only if it overlaps the request
    searchable  can search any location; always eligible and receives `location=`
    configured  a user-supplied source; always eligible
A source without a COVERAGE declaration is treated as configured.
"""
import json
import math
import time
import urllib.parse

from .core import FetchError

DEFAULT_RADIUS_MILES = 25
MAX_RADIUS_MILES = 250
CACHE_TTL_SECONDS = 180 * 24 * 3600  # a place does not move; stale entries are still used if a lookup fails
EARTH_RADIUS_MILES = 3958.8

SAN_DIEGO = {
    "display_name": "San Diego, California, United States", "latitude": 32.7157, "longitude": -117.1611,
    "timezone": "America/Los_Angeles", "country": "US", "region": "CA", "source": "builtin",
}
BUILTIN_QUERIES = {"san diego", "san diego, ca", "san diego, california", "san diego, ca, usa", "san diego, california, united states"}

US_STATE_TIMEZONES = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix", "AR": "America/Chicago",
    "CA": "America/Los_Angeles", "CO": "America/Denver", "CT": "America/New_York", "DE": "America/New_York",
    "DC": "America/New_York", "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis", "IA": "America/Chicago",
    "KS": "America/Chicago", "KY": "America/New_York", "LA": "America/Chicago", "ME": "America/New_York",
    "MD": "America/New_York", "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver", "NE": "America/Chicago",
    "NV": "America/Los_Angeles", "NH": "America/New_York", "NJ": "America/New_York", "NM": "America/Denver",
    "NY": "America/New_York", "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "SD": "America/Chicago", "TN": "America/Chicago", "TX": "America/Chicago",
    "UT": "America/Denver", "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver",
}


class LocationError(Exception):
    """The configured location is invalid or could not be resolved."""


# --- geocoding providers (interchangeable; no API keys) --------------------------

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
        except (FetchError, ValueError) as error:
            raise LocationError(f"{self.name} lookup failed: {error}") from error
        return self.parse(payload, spec)


class NominatimProvider(GeocodingProvider):
    """OpenStreetMap Nominatim. Usage policy: identifying User-Agent, <= 1 request/second, cache results."""
    name = "nominatim"

    def url(self, spec):
        return "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": spec["query"], "format": "jsonv2", "limit": 1, "addressdetails": 1})

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


PROVIDERS = {"nominatim": NominatimProvider(), "zippopotam": ZippopotamProvider()}


# --- configuration -> normalized location ----------------------------------------------

def parse_spec(raw):
    """Validate the `location` config block; returns a normalized spec dict."""
    raw = {"query": "San Diego, CA"} if raw is None else raw
    if not isinstance(raw, dict):
        raise LocationError("location must be an object with query, zip or latitude/longitude")
    radius = raw.get("radius_miles", DEFAULT_RADIUS_MILES)
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not 1 <= radius <= MAX_RADIUS_MILES:
        raise LocationError(f"location.radius_miles must be a number from 1 to {MAX_RADIUS_MILES}")
    has_coordinates = "latitude" in raw or "longitude" in raw
    chosen = [name for name, present in (("query", "query" in raw), ("zip", "zip" in raw), ("coordinates", has_coordinates)) if present]
    if len(chosen) != 1:
        raise LocationError("location needs exactly one of: query, zip, or latitude and longitude")
    spec = {"radius_miles": float(radius), "provider": raw.get("provider")}
    if chosen[0] == "query":
        query = raw["query"]
        if not isinstance(query, str) or not query.strip():
            raise LocationError("location.query must be a non-empty string")
        spec.update(kind="query", query=" ".join(query.split()))
    elif chosen[0] == "zip":
        zip_code = str(raw["zip"]).strip()
        country = str(raw.get("country", "US")).strip()
        if not zip_code or len(zip_code) > 12 or len(country) != 2 or not country.isalpha():
            raise LocationError("location.zip must be a postal code and location.country a 2-letter code (default US)")
        spec.update(kind="zip", zip=zip_code, country=country.upper())
    else:
        try:
            latitude, longitude = float(raw["latitude"]), float(raw["longitude"])
        except (KeyError, TypeError, ValueError) as error:
            raise LocationError("location needs both numeric latitude and longitude") from error
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise LocationError("location latitude must be within -90..90 and longitude within -180..180")
        spec.update(kind="coordinates", latitude=latitude, longitude=longitude, name=raw.get("name"),
                    timezone=raw.get("timezone"), country=raw.get("country"), region=raw.get("region"))
    return spec


def cache_key(spec):
    if spec["kind"] == "query":
        return f"query:{spec['query'].lower()}"
    if spec["kind"] == "zip":
        return f"zip:{spec['country']}:{spec['zip']}"
    return f"coordinates:{spec['latitude']:.5f},{spec['longitude']:.5f}"


def _finish(spec, place, source):
    region = place.get("region")
    country = place.get("country")
    return {
        "display_name": place["display_name"],
        "latitude": round(place["latitude"], 5), "longitude": round(place["longitude"], 5),
        "radius_miles": spec["radius_miles"],
        "timezone": place.get("timezone") or (US_STATE_TIMEZONES.get(region) if country == "US" else None),
        "country": country, "region": region, "source": source,
        "query": spec.get("query") or spec.get("zip") or None,
    }


def _load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_location(raw, fetcher=None, cache_path=None, providers=None, now=time.time):
    """Return the normalized location for the `location` config block, or raise LocationError."""
    spec = parse_spec(raw)
    if spec["kind"] == "coordinates":
        place = {"display_name": spec["name"] or f"{spec['latitude']:.4f}, {spec['longitude']:.4f}",
                 "latitude": spec["latitude"], "longitude": spec["longitude"], "timezone": spec["timezone"],
                 "country": spec["country"], "region": spec["region"]}
        return _finish(spec, place, "config")
    if spec["kind"] == "query" and spec["query"].lower() in BUILTIN_QUERIES:
        return _finish(spec, SAN_DIEGO, "builtin")

    key = cache_key(spec)
    cache = _load(cache_path) if cache_path else {}
    entry = cache.get(key)
    fresh = entry and now() - entry.get("resolved_at", 0) < CACHE_TTL_SECONDS
    if fresh:
        return _finish(spec, entry["place"], "cache")

    providers = providers or PROVIDERS
    name = spec["provider"] or ("zippopotam" if spec["kind"] == "zip" else "nominatim")
    provider = providers.get(name)
    if provider is None:
        raise LocationError(f"unknown geocoding provider '{name}' (available: {', '.join(sorted(providers))})")
    label = spec.get("query") or f"ZIP {spec['zip']} ({spec['country']})"
    try:
        place = provider.lookup(fetcher, spec)
    except LocationError as error:
        if entry:  # the place has not changed; an old answer beats failing the whole refresh
            return _finish(spec, entry["place"], "cache (stale)")
        raise LocationError(f"could not resolve location '{label}': {error}") from error
    if not place:
        raise LocationError(f"could not resolve location '{label}': {name} found no match")
    if cache_path:
        cache[key] = {"place": place, "provider": name, "resolved_at": now()}
        try:
            with open(cache_path, "w", encoding="utf-8") as handle:
                json.dump(cache, handle, indent=2)
        except OSError:
            pass  # not cacheable; the next refresh will look it up again
    return _finish(spec, place, f"geocoded:{name}")


# --- source coverage ---------------------------------------------------------------------

def distance_miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def covers(coverage, location):
    """(eligible, reason) for a coverage declaration against a normalized location."""
    kind = (coverage or {}).get("kind", "configured")
    if kind in ("searchable", "configured"):
        return True, kind
    if kind != "regional":
        return False, f"unknown coverage kind '{kind}'"
    gap = distance_miles(coverage["latitude"], coverage["longitude"], location["latitude"], location["longitude"])
    reach = coverage["radius_miles"] + location["radius_miles"]
    area = coverage.get("region", "its region")
    if gap <= reach:
        return True, f"regional ({area}) overlaps the requested area ({gap:.0f} mi apart)"
    return False, f"regional ({area}) is {gap:.0f} mi from the requested area, beyond {reach:.0f} mi"


def select_sources(specs, location, load):
    """Split enabled source specs into (selected, skipped); `load(key)` returns the adapter module.

    A source spec may carry its own `coverage` block (for user-configured sources) overriding the adapter's.
    """
    selected, skipped = [], []
    for spec in specs:
        if not spec.get("enabled", True):
            continue
        coverage = spec.get("coverage") or getattr(load(spec["key"]), "COVERAGE", None)
        eligible, reason = covers(coverage, location)
        (selected if eligible else skipped).append((spec, reason))
    return selected, skipped
