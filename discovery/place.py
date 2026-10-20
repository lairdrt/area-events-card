"""Event locations: what is known about where an event happens, and how well.

Every event gets a `place` with one of three classifications:

    confirmed  the source gave event-specific coordinates (venue level)
    estimated  no coordinates, but the event's own address names a city; the city is geocoded
               (cached) for an approximate position. Precision is "city", confidence "medium" when
               the address also states a region or postal code, otherwise "low"
    unknown    nothing defensible; venue, neighborhood or address text is kept as given

Nothing is ever invented: the search area's center is never used as an event position, and an estimate
without coordinates (a city that could not be geocoded) says so. Radius rules: confirmed inside is
kept, confirmed outside is dropped, estimated and unknown events are kept and labeled uncertain.
Location confidence is independent of event verification.
"""
import re

from .location import LocationError, cached_lookup, distance_miles

CONFLICT_MILES = 50  # sources placing the same event farther apart than this disagree about where it is
RANK = {"confirmed": 2, "estimated": 1, "unknown": 0}
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}
POSTAL = re.compile(r"\b\d{4,5}(?:-\d{4})?\b")
REGION_AT_END = re.compile(r"\s+([A-Z]{2})$")
CITY_NAME = re.compile(r"^[^\W\d_][\w .'’-]{1,39}$")


def parse_address(address):
    """Pull street / city / region / postal code out of a free-form address, when the text really states them."""
    out = {"street": None, "city": None, "region": None, "postal": None}
    parts = [p.strip() for p in str(address or "").split(",") if p.strip()]
    if len(parts) < 2:
        return out
    out["street"] = parts[0]
    tail = parts[-1]
    postal = POSTAL.search(tail)
    if postal:
        out["postal"] = postal.group(0)
        tail = (tail[:postal.start()] + tail[postal.end():]).strip()
    region = REGION_AT_END.search(tail)
    if region and tail != region.group(0).strip():
        out["region"] = region.group(1)
        tail = tail[:region.start()].strip()
    elif re.fullmatch(r"[A-Z]{2}", tail) and len(parts) >= 3:
        out["region"] = tail  # "..., Austin, TX 78739": the last part is only the region
        tail = parts[-2]
    if tail and CITY_NAME.match(tail) and not re.search(r"\d", tail):
        out["city"] = tail
    return out


class CityResolver:
    """Geocodes city names for estimates: cached on disk, at most `max_lookups` network lookups per run."""

    def __init__(self, fetcher, cache_path, providers, max_lookups=10, now=None):
        self.fetcher, self.cache_path, self.providers, self.max_lookups = fetcher, cache_path, providers, max_lookups
        self.now, self.lookups, self.memo = now, 0, {}

    def resolve(self, city, region, country):
        key = f"city:{country or ''}:{(region or '').lower()}:{city.lower()}"
        if key in self.memo:
            return self.memo[key]
        provider = (self.providers or {}).get("nominatim")
        place = None
        if provider and self.fetcher is not None:
            def lookup():
                if self.lookups >= self.max_lookups:
                    raise LocationError("per-run city lookup limit reached")
                self.lookups += 1
                return provider.lookup(self.fetcher, {"query": f"{city}, {region}" if region else city, "country": country})
            try:
                kwargs = {"now": self.now} if self.now else {}
                place, _ = cached_lookup(self.cache_path, key, lookup, **kwargs)
            except LocationError:
                place = None
            if place and country and place.get("country") and place["country"] != country:
                place = None  # the city resolved in another country: not a defensible estimate
        self.memo[key] = place
        return place


def _empty(event):
    return {"classification": "unknown", "confidence": "none", "precision": None, "latitude": None, "longitude": None,
            "venue": event.get("venue"), "address": event.get("address"), "neighborhood": event.get("neighborhood"),
            "city": None, "region": None, "country": None, "distance_miles": None, "within_radius": None, "evidence": []}


def _geo_point(geo):
    try:
        latitude, longitude = float(geo["latitude"]), float(geo["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return (latitude, longitude) if -90 <= latitude <= 90 and -180 <= longitude <= 180 and (latitude or longitude) else None


def locate_event(event, location, resolver=None):
    """Build the event's `place` (see module docstring)."""
    place = _empty(event)
    parsed = parse_address(event.get("address"))
    geo = event.get("geo") or {}
    point = _geo_point(geo)

    if point:
        place.update(classification="confirmed", confidence="high", precision="venue", latitude=point[0], longitude=point[1],
                     city=geo.get("city") or parsed["city"], region=geo.get("state") or parsed["region"],
                     country=geo.get("country"))
        place["evidence"].append(f"coordinates supplied by the source ({geo.get('source') or 'event data'})")
    elif parsed["city"]:
        region = parsed["region"] or location.get("region")
        found = resolver.resolve(parsed["city"], region, location.get("country")) if resolver else None
        place.update(classification="estimated", precision="city", city=parsed["city"], region=parsed["region"],
                     confidence="medium" if (parsed["region"] or parsed["postal"]) else "low")
        place["evidence"].append(f"city '{parsed['city']}' taken from the event's address")
        if found:
            place.update(latitude=round(found["latitude"], 5), longitude=round(found["longitude"], 5),
                         country=found.get("country"))
            place["evidence"].append(f"approximate position is the geocoded city centre of '{parsed['city']}'"
                                     + ("" if parsed["region"] else " (search area's region used as context)"))
        else:
            place["evidence"].append("no coordinates: the city could not be geocoded")
    else:
        place["evidence"].append("no coordinates or city in the event data")

    if place["latitude"] is not None:
        miles = distance_miles(place["latitude"], place["longitude"], location["latitude"], location["longitude"])
        place["distance_miles"] = round(miles, 1)
        place["within_radius"] = miles <= location["radius_miles"]
        if place["classification"] == "estimated" and not place["within_radius"]:
            place["evidence"].append(f"estimated position is {miles:.0f} mi away, beyond the {location['radius_miles']:.0f} mi radius; "
                                     "kept because the estimate is uncertain")
    return place


def locate_events(events, location, resolver=None):
    """Attach `place` to each event. Returns (kept, summary); confirmed-outside events are dropped."""
    kept = []
    summary = {"confirmed": 0, "estimated": 0, "unknown": 0, "outside_area": 0}
    for event in events:
        place = locate_event(event, location, resolver)
        if place["classification"] == "confirmed" and not place["within_radius"]:
            summary["outside_area"] += 1
            continue
        event["place"] = place
        summary[place["classification"]] += 1
        kept.append(event)
    return kept, summary


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def combine_places(records):
    """Merge the `place` of duplicate records (each a (source name, place) pair, in priority order).

    The best-supported classification wins; missing descriptive fields are filled from the others. Sources
    that put the event more than CONFLICT_MILES apart, or in different cities, are flagged as conflicting.
    """
    entries = [(name, place) for name, place in records if place]
    if not entries:
        return None
    best_name, best = max(entries, key=lambda e: (RANK[e[1]["classification"]], CONFIDENCE_RANK[e[1]["confidence"]]))
    merged = dict(best, evidence=[f"[{name}] {line}" for name, place in entries for line in place["evidence"]]
                  if len(entries) > 1 else list(best["evidence"]))
    for field in ("venue", "address", "neighborhood", "city", "region", "country"):
        if not merged.get(field):
            merged[field] = next((p[field] for _, p in entries if p.get(field)), None)

    pointed = [p for _, p in entries if p["latitude"] is not None]
    apart = max((distance_miles(a["latitude"], a["longitude"], b["latitude"], b["longitude"]) for a in pointed for b in pointed), default=0)
    cities = {_norm(p["city"]) for _, p in entries if p.get("city")}
    different_cities = len(cities) > 1 and not any(a != b and (a in b or b in a) for a in cities for b in cities)
    # With two or more positions, distance decides; otherwise differing city names are the only evidence.
    if (len(pointed) >= 2 and apart > CONFLICT_MILES) or (len(pointed) < 2 and different_cities):
        merged["confidence"] = "low"
        merged["conflict"] = True
        merged["evidence"].append(f"CONFLICT: sources place this event {apart:.0f} mi apart" if len(pointed) >= 2
                                  else "CONFLICT: sources name different cities for this event")
    return merged
