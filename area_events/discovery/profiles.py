"""Discovery profiles: what a card asks the engine to discover.

A profile is the part of a card's configuration that changes which events exist:

    {"area": "Austin, TX", "radius_miles": 25, "country": "US", "sources": ["https://calendar.utexas.edu"]}
    {"latitude": 30.2672, "longitude": -97.7431, "name": "Home", "radius_miles": 25}

`area` is a city, a ZIP code (needs `country`) or "latitude, longitude"; explicit coordinates may be given instead.
Display preferences (categories, date range, sorting ...) are not part of a profile: they never need a new
discovery run, so cards that differ only in them share one profile and one set of results.

`sources` is an optional list of website URLs. A URL is supported only when an adapter can read it: either a
source in sources.json (matched by website host) or a Localist calendar (probed when the profile runs). Anything
else is reported as unsupported, never silently ignored and never fetched. Without `sources`, the engine uses the
registered sources whose declared coverage overlaps the area.
"""
import hashlib
import ipaddress
import json
import re
import socket
from urllib.parse import urlparse

from .location import MAX_RADIUS_MILES, DEFAULT_RADIUS_MILES

MAX_SOURCES = 10
MAX_AREA_LENGTH = 100
PAIR = re.compile(r"^(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)$")
POSTAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{2,10}$")


class ProfileError(ValueError):
    """The requested profile is invalid."""


def _host(url):
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def normalize_url(raw):
    if not isinstance(raw, str):
        raise ProfileError("source URLs must be text")
    url = raw.strip()
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ProfileError(f"'{raw[:80]}' is not a plain http(s) website address")
    return f"{parts.scheme}://{parts.hostname.lower()}{parts.path.rstrip('/') if parts.path != '/' else ''}"


def normalize_profile(raw):
    """Validate a profile and return it in canonical form (the same request always gives the same dict)."""
    if not isinstance(raw, dict):
        raise ProfileError("the profile must be an object")
    unknown = sorted(set(raw) - {"area", "latitude", "longitude", "name", "country", "radius_miles", "timezone", "sources"})
    if unknown:
        raise ProfileError(f"unknown profile fields: {', '.join(unknown)}")
    radius = raw.get("radius_miles", DEFAULT_RADIUS_MILES)
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not 1 <= radius <= MAX_RADIUS_MILES:
        raise ProfileError(f"radius_miles must be a number from 1 to {MAX_RADIUS_MILES}")
    profile = {"radius_miles": round(float(radius), 1)}
    country = raw.get("country")
    if country not in (None, ""):
        if not isinstance(country, str) or len(country.strip()) != 2 or not country.strip().isalpha():
            raise ProfileError("country must be a 2-letter country code")
        profile["country"] = country.strip().upper()

    area, latitude, longitude = raw.get("area"), raw.get("latitude"), raw.get("longitude")
    if (latitude is None) != (longitude is None):
        raise ProfileError("latitude and longitude must be given together")
    if latitude is not None:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (latitude, longitude)):
            raise ProfileError("latitude and longitude must be numbers")
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180) or (latitude == 0 and longitude == 0):
            raise ProfileError("those coordinates are not valid")
        profile.update(latitude=round(float(latitude), 4), longitude=round(float(longitude), 4))
        if raw.get("name"):
            profile["name"] = str(raw["name"]).strip()[:60]
    elif isinstance(area, str) and area.strip():
        profile["area"] = " ".join(area.split())[:MAX_AREA_LENGTH]
    else:
        raise ProfileError("give an area (a city, a ZIP code or 'latitude, longitude') or coordinates")
    timezone_name = raw.get("timezone")
    if timezone_name:
        profile["timezone"] = timezone_name

    sources = raw.get("sources") or []
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise ProfileError(f"sources must be a list of at most {MAX_SOURCES} website addresses")
    unique = sorted({normalize_url(url) for url in sources})
    if unique:
        profile["sources"] = unique
    return profile


def profile_id(profile):
    """Stable identifier of a normalized profile; also the name of its storage folder."""
    canonical = dict(profile)
    if "area" in canonical:
        canonical["area"] = canonical["area"].lower()
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def location_spec(profile):
    """The `location` block (see location.py) for a normalized profile."""
    spec = {"radius_miles": profile["radius_miles"]}
    if profile.get("country"):
        spec["country"] = profile["country"]
    if profile.get("timezone"):
        spec["timezone"] = profile["timezone"]
    if "latitude" in profile:
        spec.update(latitude=profile["latitude"], longitude=profile["longitude"])
        if profile.get("name"):
            spec["name"] = profile["name"]
        return spec
    area = profile["area"]
    pair = PAIR.match(area)
    if pair:
        spec.update(latitude=float(pair.group(1)), longitude=float(pair.group(2)))
    elif re.search(r"\d", area) and "," not in area and POSTAL.match(area):
        spec["zip"] = area
    else:
        spec["query"] = area
    return spec


# --- sources ----------------------------------------------------------------------------

def match_registered(urls, registered):
    """Split URLs into ([(url, Source)], [url]) by whether a registered source reads that website."""
    by_host = {}
    for source in registered:
        by_host.setdefault(_host(source.url), source)
    matched, rest = [], []
    for url in urls:
        source = by_host.get(_host(url))
        (matched.append((url, source)) if source else rest.append(url))
    return matched, rest


def public_host(host, resolver=socket.getaddrinfo):
    """True when `host` resolves only to public addresses (a probe must never reach the local network)."""
    try:
        addresses = {info[4][0] for info in resolver(host, None)}
    except OSError:
        return False
    if not addresses:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            return False
    return True
