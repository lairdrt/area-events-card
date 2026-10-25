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
import unicodedata
import urllib.parse

from ..discovery.safe import FetchError

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


def _fold(text):
    """Lower-case ASCII words only, so 'São Paulo' and 'Sao Paulo', or 'U.S.A.' and 'usa', compare equal."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


ADMIN_PREFIX = re.compile(r"^(greater|city of|town of|village of|county of|municipality of|borough of|hamlet of) ")  # OSM: "Greater London", "City of London"
MIN_NAME_LETTERS = 3
AMBIGUITY_MILES = 25
DOMINANCE = 1.3
CITY_RANK = 16  # Nominatim place_rank of a city/town; villages, suburbs and streets (19+) need a qualifier to be accepted
US_STATES = {"al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn",
             "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa",
             "wv", "wi", "wy", "pr", "gu", "vi", "as", "mp"}
GB_ALIASES = {"uk", "u k", "united kingdom", "great britain"}
US_ALIASES = {"us", "u s", "usa", "u s a", "united states", "united states of america", "america"}
NAME_FIELDS = ("city", "town", "village", "hamlet", "municipality", "suburb", "city_district", "county", "state")


class NominatimProvider(GeocodingProvider):
    """OpenStreetMap Nominatim. Usage policy: identifying User-Agent, <= 1 request/second, cache results.

    Nominatim ranks fuzzily, so its first hit is never trusted blindly. A hit is accepted only if it is a place with the
    queried name and in every place the query's "City, State/Country" qualifiers name; if several different places
    qualify and none clearly dominates, the query is ambiguous and refused instead of guessed.
    """
    name = "nominatim"

    def url(self, spec):
        params = {"q": spec["query"], "format": "jsonv2", "limit": 5, "addressdetails": 1}
        if spec.get("country"):
            params["countrycodes"] = spec["country"].lower()
        return "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)

    @staticmethod
    def _candidate(hit, primary, qualifiers):
        """The normalized place if this hit is the queried name inside every qualifier, else None."""
        address = hit.get("address") or {}
        names = {_fold(hit.get("name")), _fold((hit.get("display_name") or "").split(",")[0])}
        names.update(_fold(address.get(field)) for field in NAME_FIELDS)
        names |= {ADMIN_PREFIX.sub("", name) for name in names}
        if primary not in names:
            return None
        iso = address.get("ISO3166-2-lvl4", "")
        region = iso.split("-")[-1] if iso else address.get("state")
        country = (address.get("country_code") or "").upper() or None
        fields = {_fold(region), _fold(iso), _fold(address.get("state")), _fold(address.get("county")), _fold(address.get("state_district")),
                  _fold(address.get("country"))}
        # "CA", "IN", "DE", "IL" ... are US states before they are countries: Austin, CA is California, never Canada.
        if not any(q in US_STATES for q in qualifiers):
            fields.add(_fold(country))
        if country == "US":
            fields |= US_ALIASES
        if country == "GB":
            fields |= GB_ALIASES
        if any(q not in fields for q in qualifiers):
            return None
        return {"display_name": hit.get("display_name") or "", "latitude": float(hit["lat"]), "longitude": float(hit["lon"]),
                "country": country, "region": region, "_importance": float(hit.get("importance") or 0), "_rank": int(hit.get("place_rank") or CITY_RANK),
                "_label": ", ".join(x for x in (address.get("city") or address.get("town") or address.get("village") or hit.get("name"), address.get("state"), address.get("country")) if x)}

    def parse(self, payload, spec):
        if not isinstance(payload, list) or not payload:
            return None
        parts = [part.strip() for part in spec["query"].split(",") if part.strip()]
        primary, qualifiers = _fold(parts[0] if parts else ""), [_fold(part) for part in parts[1:]]
        if len(primary.replace(" ", "")) < MIN_NAME_LETTERS:
            raise LocationError("the place name is too short; enter a city such as 'Austin, TX'")
        matches = [place for hit in payload if (place := self._candidate(hit, primary, qualifiers))]
        if not qualifiers:
            matches = [place for place in matches if place["_rank"] <= CITY_RANK]  # a bare name must be a city, not whichever village shares it
        if not matches:
            raise LocationError(f"no place called '{spec['query']}' was found" + (" (the state or country given does not match any place of that name)" if qualifiers else "; add a state or country, for example 'Austin, TX'"))
        top = matches[0]
        rivals = [m for m in matches[1:] if distance_miles(top["latitude"], top["longitude"], m["latitude"], m["longitude"]) > AMBIGUITY_MILES]
        if rivals and top["_importance"] < DOMINANCE * rivals[0]["_importance"]:
            names = "; ".join(dict.fromkeys(m["_label"] or m["display_name"] for m in [top, *rivals[:2]]))
            raise LocationError(f"'{spec['query']}' matches several places ({names}); add a state or country")
        return {key: value for key, value in top.items() if not key.startswith("_")}


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


def _timezone_for(spec, latitude, longitude, fetcher, cache_path, now, lookup=True):
    if spec["timezone"]:
        return spec["timezone"], "config"
    if fetcher is None or not lookup:
        return None, "unknown"
    try:
        zone, _ = cached_lookup(cache_path, f"tz:{latitude:.2f},{longitude:.2f}",
                                lambda: TIMEZONE_PROVIDER.lookup(fetcher, latitude, longitude), now)
    except LocationError:
        zone = None
    return (zone, "lookup:timeapi") if zone else (None, "unknown")


def _finish(spec, place, source, timezone_name, timezone_source):
    return {
        "display_name": spec["name"] or place["display_name"],
        "latitude": round(place["latitude"], 5), "longitude": round(place["longitude"], 5),
        "radius_miles": spec["radius_miles"],
        "timezone": timezone_name, "timezone_source": timezone_source,
        "country": place.get("country") or spec["country"], "region": place.get("region") or spec["region"],
        "source": source, "query": spec.get("query") or spec.get("zip"),
    }


def resolve_location(raw, fetcher=None, cache_path=None, providers=None, now=time.time, timezone_lookup=True):
    """Return the normalized location for the `location` config block, or raise LocationError."""
    spec = parse_spec(raw)
    if spec["kind"] == "coordinates":
        place = {"display_name": f"{spec['latitude']:.4f}, {spec['longitude']:.4f}",
                 "latitude": spec["latitude"], "longitude": spec["longitude"]}
        zone, zone_source = _timezone_for(spec, spec["latitude"], spec["longitude"], fetcher, cache_path, now, timezone_lookup)
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
    zone, zone_source = _timezone_for(spec, place["latitude"], place["longitude"], fetcher, cache_path, now, timezone_lookup)
    return _finish(spec, place, how if how != "lookup" else f"geocoded:{name}", zone, zone_source)


def distance_miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))
