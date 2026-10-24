"""Location imagery for events that have no photograph of their own: a verified photograph of the venue, or a map.

Runs in the background, never inside event discovery, and never makes an event depend on it. For every distinct place found in
the current feeds it decides, from evidence stored with the result, whether the position is trustworthy enough for a map pin
(`locate`), draws a vector map from OpenFreeMap tiles (maps.py), and looks for a photograph of that very venue on Wikimedia
Commons (`find_venue_photo`). Results (including refusals) are cached on disk by a stable key and attached to events when the
feed is served (`annotate`); nothing here changes the feed itself.

Politeness: lookups to the public Nominatim service are spaced 15 s apart (its policy allows long-running programs 4 requests a
minute) and cached; everything is fetched a few places per cycle, and the whole feature can be switched off in the app options.

Trust rules (all must hold; anything weaker is refused and the refusal is cached):
  source   coordinates the event's own source supplied for the venue (the engine classified them `confirmed`)
  address  a street address whose house number AND street name were matched by the geocoder, in the right locality
  venue    a named place the geocoder returned with exactly the venue's name, a place-like type, in the right area, and no
           second candidate of that name elsewhere
City-centre estimates, partial addresses, ambiguous names and non-place results are never used. An address pin can only say
"somewhere on this property", and a building coordinate is not an entrance; the card says so (`approximate`).

A venue photograph needs an identity chain, not a name match: the geocoded place carries an OpenStreetMap `wikidata` link, or a
Wikipedia article whose title equals the venue's name stands within 250 m of the verified position; the Wikidata entity's label or an
alias equals the venue's name; its coordinates agree within 250 m; it names an image; the image is a JPEG photograph on Commons
with a permissive licence (CC0, public domain, CC BY, CC BY-SA). Credit (the creator when the file names one; otherwise the
credit is the link to the Commons file page, which is how the licences allow attribution), the licence and the file page are
stored and shown.
Everything fetched is untrusted: it is validated, bounded, and stored as data; images are never executed or re-encoded.
"""
import copy
import hashlib
import html
import json
import logging
import math
import os
import re
import threading
import time
from urllib.parse import urlencode

from . import maps, textloc
from .safe import FetchError

log = logging.getLogger("area-events.media")

MEDIA_URL = "/area_events/media"
RENDER_VERSION = 1
MAP_CREDIT = "OpenFreeMap © OpenMapTiles Data from OpenStreetMap"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_INTERVAL = 10.0  # seconds between Overpass queries (a shared public service)
INTERSECTION_RADIUS_M = 1500
OVERPASS_TIMEOUT = 60  # the public instance can take 30 s or more
TILEJSON = "https://tiles.openfreemap.org/planet"
TILE_TTL = 14 * 86400
RECORD_TTL = 60 * 86400
REFUSAL_TTL = 14 * 86400
MAX_NEW_PER_CYCLE = 8
RESOLVER = 2  # bump when the rules for reading a place change: refusals recorded under older rules are tried again where the new rules differ
NOMINATIM_INTERVAL = 15.0  # seconds between lookups: the public service allows long-running programs 4 requests a minute
MAX_RECORDS = 2000
MAX_PHOTO_BYTES = 700_000
MAX_TILE_FILES = 120
PLACE_CLASSES = {"amenity", "leisure", "tourism", "building", "shop", "historic", "office", "man_made", "club", "sport"}
NOT_PLACES = {"city", "town", "village", "hamlet", "administrative", "suburb", "neighbourhood", "quarter", "residential", "road", "street",
              "county", "state", "postcode", "country", "district", "borough", "yes_road"}
LICENCE_OK = re.compile(r"^(CC0|Public domain|PD[- ]|CC[- ]BY(?:[- ]SA)?[- ][1-4]\.\d)", re.I)
LICENCE_BAD = re.compile(r"\b(NC|ND|GFDL|fair use|non-?commercial)\b|-NC|-ND", re.I)
BAD_FILENAME = re.compile(r"logo|map|plan|seal|emblem|coat[ _]of|flag|diagram|icon|signature", re.I)
SUFFIXES = {"street": "st", "st": "st", "avenue": "ave", "ave": "ave", "av": "ave", "boulevard": "blvd", "blvd": "blvd", "road": "rd", "rd": "rd",
            "drive": "dr", "dr": "dr", "lane": "ln", "ln": "ln", "court": "ct", "ct": "ct", "place": "pl", "pl": "pl", "way": "way", "circle": "cir",
            "cir": "cir", "parkway": "pkwy", "pkwy": "pkwy", "highway": "hwy", "hwy": "hwy", "trail": "trl", "trl": "trl", "terrace": "ter", "ter": "ter",
            "north": "n", "south": "s", "east": "e", "west": "w", "n": "n", "s": "s", "e": "e", "w": "w"}


# --- names and addresses ----------------------------------------------------------------------------------------

def norm_name(text):
    text = html.unescape(str(text or "")).lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    words = [w for w in text.split() if w]
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def venue_names(venue):
    """The exact names a venue string can stand for: the whole text and its first comma-separated part, normalized."""
    text = html.unescape(str(venue or "")).strip()
    names = []
    for candidate in (text, text.split(",")[0]):
        name = norm_name(candidate)
        if name and name not in names and len(name) <= 80:
            names.append(name)
    return names


def place_names(venue, area):
    """`venue_names`, minus names that are only a city, county or state of the search area ("Austin, TX" is a place, not a venue)."""
    generic = set()
    for part in str((area or {}).get("display_name") or (area or {}).get("name") or "").split(","):
        generic.add(norm_name(part))
    text = html.unescape(str(venue or "")).strip()
    if re.fullmatch(r"[^,]{2,40},\s*[A-Za-z]{2}\.?", text):  # "City, ST"
        return []
    area_words = {w for part in generic for w in part.split()}
    return [n for n in venue_names(venue) if n not in generic and not set(n.split()) <= area_words
            and not re.fullmatch(r"(online|virtual|tba|tbd|various|off campus|offsite|multiple locations)", n)]


ORDINALS = {"first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5", "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
            "eleventh": "11", "twelfth": "12", "thirteenth": "13", "fourteenth": "14", "fifteenth": "15", "sixteenth": "16", "seventeenth": "17",
            "eighteenth": "18", "nineteenth": "19", "twentieth": "20", "thirtieth": "30", "fortieth": "40", "fiftieth": "50"}


def street_tokens(text):
    """Lower-case words of a street with street types and directions abbreviated and ordinals as digits (8th = Eighth = 8)."""
    words = re.sub(r"[^\w\s]", " ", html.unescape(str(text or "")).lower()).split()
    out = []
    for w in words:
        digits = re.fullmatch(r"(\d+)(?:st|nd|rd|th)", w)
        out.append(digits.group(1) if digits else ORDINALS.get(w) or SUFFIXES.get(w, w))
    return out


_DIRECTIONS = {"n", "s", "e", "w"}
_TYPES = set(SUFFIXES.values()) - _DIRECTIONS
_LEAD = re.compile(r"^(?:(?:on|at|near|by)\s+)?(?:the\s+)?(?:(?:north|south)?(?:east|west)?\s+)?(?:corner|intersection|junction)\s+of\s+", re.I)
_JOIN = re.compile(r"\s+(?:and|&|at)\s+|\s*@\s*|\s*/\s*", re.I)


def _is_street(tokens):
    return any(t not in _NOT_CORE for t in tokens) and any(t in _TYPES for t in tokens)


def numeric_ordinals(text):
    """A street written for a geocoder: ordinal words as numerals ("Fifth Avenue" -> "5th Avenue"), which is how maps usually name them."""
    def number(word):
        digits = ORDINALS.get(word.lower())
        if not digits:
            return word
        suffix = "th" if digits[-2:] in ("11", "12", "13") or digits[-1] not in "123" else {"1": "st", "2": "nd", "3": "rd"}[digits[-1]]
        return digits + suffix
    return re.sub(r"[A-Za-z]+", lambda m: number(m.group(0)), text)


def parse_intersection(text):
    """{"a", "b" (street tokens), "a_text", "b_text", "locality"} for "Street A and Street B", "A & B", "A at B", "Corner of A and B"
    (with or without a trailing locality); None for anything else. Both sides must look like streets (a street type word), so
    "Arts and Crafts" or a venue name with "and" in it is not an intersection."""
    text = html.unescape(str(text or "")).strip()
    if not text or re.match(r"^\d+(?!\d|st\b|nd\b|rd\b|th\b)", text):  # a house number: that is an address, not a corner
        return None
    segments = [p.strip() for p in text.split(",") if p.strip()]
    head = _LEAD.sub("", segments[0].strip(" .")).strip(" .")
    parts = _JOIN.split(head)
    if len(parts) != 2 or any(len(part.split()) > 5 for part in parts):  # a sentence that happens to contain "and" is not a pair of streets
        return None
    a, b = street_tokens(parts[0]), street_tokens(parts[1])
    if not (_is_street(a) and _is_street(b)) or a == b:
        return None
    locality = None
    for part in segments[1:]:
        if not re.search(r"\d", part) and not re.fullmatch(r"[A-Za-z]{2}", part):
            locality = norm_name(part)
            break
    return {"a": a, "b": b, "a_text": parts[0].strip(), "b_text": parts[1].strip(), "locality": locality}


def street_like(wanted, name):
    """(True, note) when OpenStreetMap's `name` is the street `wanted`: the same name once types and directions are set aside, any
    direction that both state equal. A different street type ("Drive" for "Avenue") still matches, and the note says so."""
    found = street_tokens(name)
    core_a = [t for t in wanted if t not in _NOT_CORE]
    core_b = [t for t in found if t not in _NOT_CORE]
    if not core_a or core_a != core_b:
        return False, ""
    dir_a, dir_b = [t for t in wanted if t in _DIRECTIONS], [t for t in found if t in _DIRECTIONS]
    if dir_a and dir_b and dir_a != dir_b:
        return False, ""
    type_a, type_b = [t for t in wanted if t in _TYPES], [t for t in found if t in _TYPES]
    return True, "" if (not type_a or not type_b or type_a == type_b) else f"the listing says {' '.join(type_a)}, the map says {' '.join(type_b)}"


def parse_street(address):
    """(house number, normalized street tokens, locality or None) for an address that starts with a house number, else None."""
    parts = [p.strip() for p in html.unescape(str(address or "")).split(",") if p.strip()]
    if not parts:
        return None
    match = re.match(r"^(\d{1,6})[A-Za-z]?\s+(.+)$", parts[0])
    if not match:
        return None
    tokens = street_tokens(match.group(2))
    if not tokens:
        return None
    locality = None
    for part in parts[1:]:
        if not re.search(r"\d", part) and not re.fullmatch(r"[A-Za-z]{2}", part):
            locality = norm_name(part)
            break
    return match.group(1), tokens, locality


_NOT_CORE = set(SUFFIXES.values())


def streets_match(wanted, found):
    """The same street: equal once street-type and direction words (St/Street, E/East) are set aside."""
    a = [t for t in wanted if t not in _NOT_CORE]
    b = [t for t in found if t not in _NOT_CORE]
    return bool(a) and a == b


def distance_m(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return 6371000 * math.hypot(x, math.radians(lat2 - lat1))


def make_key(kind, *parts):
    return kind + ":" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


def _known_cities(area):
    first = str((area or {}).get("display_name") or (area or {}).get("name") or "").split(",")[0].strip()
    return [first] if first else []


def _text_address(text, area, found_in, venue_name=None):
    """A candidate for the one full street address written in `text` (None when there is none, or more than one different one)."""
    found = textloc.extract_addresses(text, _known_cities(area))
    if len(found) != 1:
        return None
    item = found[0]
    street = parse_street(item["address"])
    if not street:
        return None
    number, tokens, locality = street
    name = venue_name or item["name_before"] or ""
    cities = [norm_name(c) for c in _known_cities(area)]
    return {"kind": "address", "key": make_key("a", number, " ".join(tokens), locality or "", _area_key(area)), "venue": name, "address": item["address"],
            "street": (number, tokens, locality or (cities[0] if cities else None)), "area": area, "names": place_names(name, area) if name else [],
            "found_in": found_in, "retry_old": True}


def _intersection(crossing, text, area, found_in=None):
    cores = sorted(" ".join(t for t in side if t not in _NOT_CORE) for side in (crossing["a"], crossing["b"]))
    return {"kind": "intersection", "key": make_key("x", *cores, crossing["locality"] or "", _area_key(area)), "venue": "", "address": text,
            "intersection": crossing, "area": area, "names": [], "found_in": found_in, "retry_old": found_in is not None}


def candidate_for(event, area):
    """A description of the place an event names, with a stable cache key, or None when it names nothing usable.
    `area` is the feed's location ({latitude, longitude, radius_miles, name}) or None.

    The place is taken from the first kind of evidence the event has, in this order: coordinates the source supplied; a street address in the
    address field; a corner of two streets in the address field; a full address written in the venue text (or, when the venue is empty or only a
    sentence, in the description); a corner in the venue field; the venue's name; a park or centre named at the start of a venue sentence; a
    full address in the description (when the venue is a name); a "corner of A and B" phrase in the text. The result carries the next
    candidates as `then`, which are tried only if the one before it is refused (at most three in all)."""
    place = event.get("place") or {}
    geo = event.get("geo") or {}
    raw_venue = html.unescape(str(event.get("venue") or "")).strip()
    venue = raw_venue[:120]
    address = html.unescape(str(event.get("address") or "")).strip()[:200]
    description = html.unescape(str(event.get("description") or ""))[:4000]
    try:
        lat, lon = float(geo["latitude"]), float(geo["longitude"])
    except (KeyError, TypeError, ValueError):
        lat = lon = None
    if place.get("classification") == "confirmed" and lat is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        return {"kind": "source", "key": make_key("g", f"{lat:.4f}", f"{lon:.4f}"), "lat": lat, "lon": lon, "venue": venue, "address": address,
                "provenance": f"coordinates supplied by the event source ({geo.get('source') or 'event data'})", "area": area, "names": place_names(venue, area)}
    street = parse_street(address)
    if street:
        return {"kind": "address", "key": make_key("a", street[0], " ".join(street[1]), street[2] or "", _area_key(area)), "venue": venue, "address": address,
                "street": street, "area": area, "names": place_names(venue, area)}
    options = []
    crossing = parse_intersection(address)
    if crossing:
        options.append(_intersection(crossing, address, area))
    prose = textloc.is_prose(raw_venue)
    in_venue = _text_address(raw_venue[:600], area, "venue text", None if prose else venue) if raw_venue else None
    if in_venue:
        options.append(in_venue)
    in_description = _text_address(description, area, "description", None if prose or not raw_venue else venue) if description else None
    if in_description and (prose or not raw_venue):
        options.append(in_description)
    crossing = parse_intersection(venue)
    if crossing:
        options.append(_intersection(crossing, venue, area))
    names = [] if prose else place_names(venue, area)
    variants = [] if prose else [v for v in textloc.without_rooms(raw_venue) if place_names(v, area)]
    if names and not re.match(r"^\s*\d", venue):
        options.append({"kind": "venue", "key": make_key("v", names[0], _area_key(area)), "venue": venue, "address": address,
                        "names": names + [n for v in variants for n in place_names(v, area) if n not in names], "area": area, "variants": variants, "retry_old": bool(variants)})
    elif prose:
        named = textloc.named_place(raw_venue)
        if named and place_names(named, area):
            names = place_names(named, area)
            options.append({"kind": "venue", "key": make_key("v", names[0], _area_key(area)), "venue": named, "address": "", "names": names, "area": area,
                            "variants": [], "found_in": "venue text", "retry_old": True})
    if in_description and in_description not in options:
        options.append(in_description)
    for text in (raw_venue, description):
        for phrase in textloc.corner_phrases(text):
            crossing = parse_intersection(phrase)
            if crossing:
                options.append(_intersection(crossing, phrase, area, "venue text" if text is raw_venue else "description"))
                break
    unique = []
    for option in options:
        if all(option["key"] != other["key"] for other in unique):
            unique.append(option)
    unique = unique[:3]
    for first, second in zip(unique, unique[1:]):
        first["then"] = second
    return unique[0] if unique else None


def _area_key(area):
    if not area:
        return ""
    try:
        return f"{float(area['latitude']):.2f},{float(area['longitude']):.2f}"
    except (KeyError, TypeError, ValueError):
        return ""


# --- geocoding with strict acceptance ----------------------------------------------------------------------------

def _locality(result):
    address = result.get("address") or {}
    for key in ("city", "town", "village", "municipality", "suburb", "hamlet", "county"):
        if address.get(key):
            return norm_name(address[key])
    return None


def _in_area(lat, lon, area, slack_miles=5):
    if not area:
        return False
    try:
        return distance_m(lat, lon, float(area["latitude"]), float(area["longitude"])) <= (float(area.get("radius_miles", 25)) + slack_miles) * 1609.34
    except (KeyError, TypeError, ValueError):
        return False


def accept_street(candidate, tokens, results):
    """Points on the named street, from Nominatim results for it: highways whose name is that street, inside the search area and,
    when the listing names a locality, in that locality."""
    area, locality = candidate["area"], candidate["intersection"]["locality"]
    points = []
    for r in results or []:
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if r.get("category") != "highway" or not street_like(tokens, r.get("name") or "")[0] or not _in_area(lat, lon, area):
            continue
        names = {norm_name(v) for v in (r.get("address") or {}).values() if isinstance(v, str)}
        if locality and locality not in names:
            continue
        points.append((lat, lon))
    return points


def bounding_box(points, margin_m):
    """(south, west, north, east) around the points, widened by margin_m; None when that is more than about 8 km across."""
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    dlat = margin_m / 111320
    dlon = margin_m / (111320 * max(0.2, math.cos(math.radians(sum(lats) / len(lats)))))
    box = (min(lats) - dlat, min(lons) - dlon, max(lats) + dlat, max(lons) + dlon)
    return box if (box[2] - box[0]) * 111320 <= 8000 else None


def _alternatives(token):
    words = [word for word, digits in ORDINALS.items() if digits == token]
    return "|".join([re.escape(token)] + words) if token.isdigit() else re.escape(token)


def overpass_query(box, tokens_a, tokens_b):
    """An Overpass QL query for the roads that might be either street (a loose name filter; the exact test is made on the answer)."""
    def clause(tokens):
        core = [t for t in tokens if t not in _NOT_CORE]
        return ".*".join(f"({_alternatives(t)})" for t in core)
    south, west, north, east = (f"{v:.6f}" for v in box)
    return (f'[out:json][timeout:25];(way["highway"]["name"~"{clause(tokens_a)}",i]({south},{west},{north},{east});'
            f'way["highway"]["name"~"{clause(tokens_b)}",i]({south},{west},{north},{east}););out body;>;out skel qt;')


def shared_nodes(elements, tokens_a, tokens_b, merge_m=100):
    """Where a way named like A and a way named like B share a node, as [{"lat", "lon", "node", "names", "note"}] (nearby nodes are one
    place). The roads must actually be connected in OpenStreetMap; two streets that merely pass near each other do not count."""
    if not isinstance(elements, list):
        return []
    ways = [e for e in elements if isinstance(e, dict) and e.get("type") == "way" and isinstance(e.get("nodes"), list) and isinstance(e.get("tags"), dict)]
    nodes = {e["id"]: e for e in elements if isinstance(e, dict) and e.get("type") == "node" and "lat" in e and "lon" in e}
    notes, side_a, side_b = [], [], []
    for way in ways:
        name = way["tags"].get("name")
        if not isinstance(name, str):
            continue
        ok_a, note_a = street_like(tokens_a, name)
        ok_b, note_b = street_like(tokens_b, name)
        if ok_a and not ok_b:
            side_a.append(way)
            notes.append(note_a)
        elif ok_b and not ok_a:
            side_b.append(way)
            notes.append(note_b)
    places = []
    for wa in side_a:
        for wb in side_b:
            for node_id in set(wa["nodes"]) & set(wb["nodes"]):
                node = nodes.get(node_id)
                if node is None:
                    continue
                if any(distance_m(p["lat"], p["lon"], node["lat"], node["lon"]) <= merge_m for p in places):
                    continue
                note = next((n for n in notes if n), "")
                places.append({"lat": float(node["lat"]), "lon": float(node["lon"]), "node": node_id, "names": (wa["tags"]["name"], wb["tags"]["name"]), "note": note})
    return places


def _without_city(name, area):
    for city in _known_cities(area):
        city = norm_name(city)
        if city and name.endswith(" " + city) and len(name.split()) - len(city.split()) >= 2:
            return name[:-len(city)].strip()
    return name


def accept_geocode(candidate, results):
    """(location dict, None) or (None, reason): applies the trust rules to Nominatim results (jsonv2 with addressdetails and extratags)."""
    kind, area = candidate["kind"], candidate["area"]
    if not results:
        return None, "the geocoder found nothing"
    good = []
    for r in results:
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not _in_area(lat, lon, area):
            continue
        address, extra = r.get("address") or {}, r.get("extratags") or {}
        if kind == "address":
            number, tokens, locality = candidate["street"]
            found_number = str(address.get("house_number") or "").split("-")[0].split(";")[0].strip()
            if found_number != number or not streets_match(tokens, street_tokens(address.get("road") or "")):
                continue
            names = {norm_name(v) for v in address.values() if isinstance(v, str)}
            if locality and _locality(r) and locality != _locality(r) and locality not in names:
                continue
            good.append({"lat": lat, "lon": lon, "precision": "address", "confidence": "address", "approximate": True, "osm": f"{r.get('osm_type')}/{r.get('osm_id')}",
                         "name": r.get("name") or "", "wikidata": extra.get("wikidata"),
                         "provenance": f"street address matched by OpenStreetMap Nominatim (house number {number}, {' '.join(tokens)}, osm {r.get('osm_type')}/{r.get('osm_id')})"})
        elif kind == "venue":
            names = candidate["names"]
            found = norm_name(r.get("name") or (r.get("namedetails") or {}).get("name") or "")
            if r.get("category") not in PLACE_CLASSES or r.get("type") in NOT_PLACES:
                continue
            # the same name once the search area's own city is set aside ("Hard Rock Hotel" and "Hard Rock Hotel San Diego"), never a different name
            if found not in names and _without_city(found, area) not in {_without_city(n, area) for n in names}:
                continue
            if not _locality(r) and not address.get("road"):
                continue
            good.append({"lat": lat, "lon": lon, "precision": "poi", "confidence": "venue", "approximate": True, "osm": f"{r.get('osm_type')}/{r.get('osm_id')}",
                         "name": r.get("name") or "", "wikidata": extra.get("wikidata"),
                         "provenance": f"named place matched exactly by OpenStreetMap Nominatim ({r.get('name')}, {r.get('category')}={r.get('type')}, osm {r.get('osm_type')}/{r.get('osm_id')})"})
    if not good:
        return None, "no result matched the address or venue name closely enough"
    first = good[0]
    if len(good) > 1 and any(distance_m(first["lat"], first["lon"], g["lat"], g["lon"]) > 300 for g in good[1:]):
        return None, "the name matches places in more than one location"
    return first, None


# --- venue photographs --------------------------------------------------------------------------------------------

def _label_names(entity):
    names = set()
    for item in (entity.get("labels") or {}).values():
        names.add(norm_name(item.get("value")))
    for items in (entity.get("aliases") or {}).values():
        for item in items:
            names.add(norm_name(item.get("value")))
    names.discard("")
    return names


def _clean_credit(text):
    text = re.sub(r"<[^>]*>", " ", html.unescape(str(text or "")))
    text = re.sub(r"\s+", " ", text).strip()
    link = re.fullmatch(r"https?://(?:www\.)?([^/\s]+)/(?:[^/\s]+/)*([^/\s]+)/?", text)
    if link:  # the file's author field is only an address: show who and where, not the address
        text = f"{link.group(2)} ({link.group(1)})"
    return text[:120]


def find_venue_photo(candidate, location, fetch_json, fetch_bytes):
    """A verified photograph of the venue as a record, or (None, reason). `fetch_json(url)` and `fetch_bytes(url, types)` do the network."""
    names = candidate.get("names")
    if names is None:
        names = place_names(candidate.get("venue"), candidate.get("area"))
    if not names:
        return None, "no venue name (or only a city or area name)"
    lat, lon = location["lat"], location["lon"]
    evidence = []
    qid = location.get("wikidata") if re.fullmatch(r"Q\d{1,12}", str(location.get("wikidata") or "")) else None
    if qid:
        evidence.append(f"the geocoded place carries the OpenStreetMap link wikidata={qid}")
    else:
        near = fetch_json("https://en.wikipedia.org/w/api.php?" + urlencode({"action": "query", "list": "geosearch", "gscoord": f"{lat}|{lon}", "gsradius": 250,
                                                                           "gslimit": 10, "format": "json"}))
        exact = [n for n in (near.get("query", {}).get("geosearch") or []) if norm_name(n.get("title")) in names]
        if len(exact) != 1:
            return None, "no single Wikipedia article with the venue's exact name stands at this position"
        pages = fetch_json("https://en.wikipedia.org/w/api.php?" + urlencode({"action": "query", "pageids": exact[0]["pageid"], "prop": "pageprops", "ppprop": "wikibase_item", "format": "json"}))
        qid = next(iter((pages.get("query", {}).get("pages") or {}).values()), {}).get("pageprops", {}).get("wikibase_item")
        if not re.fullmatch(r"Q\d{1,12}", str(qid or "")):
            return None, "the Wikipedia article has no Wikidata item"
        evidence.append(f"Wikipedia article '{exact[0]['title']}' has exactly the venue's name and lies {exact[0].get('dist')} m from the verified position (Wikidata {qid})")
    data = fetch_json("https://www.wikidata.org/w/api.php?" + urlencode({"action": "wbgetentities", "ids": qid, "props": "labels|aliases|claims", "languages": "en", "format": "json"}))
    entity = (data.get("entities") or {}).get(qid) or {}
    if not (_label_names(entity) & set(names)):
        return None, "the Wikidata label and aliases do not equal the venue's name"
    evidence.append("the Wikidata label or an alias equals the venue's name")
    claims = entity.get("claims") or {}
    try:
        coordinate = claims["P625"][0]["mainsnak"]["datavalue"]["value"]
        far = distance_m(lat, lon, float(coordinate["latitude"]), float(coordinate["longitude"]))
    except (KeyError, IndexError, TypeError, ValueError):
        return None, "the Wikidata item has no coordinates to compare"
    if far > 250:
        return None, f"the Wikidata coordinates are {far:.0f} m from the verified position"
    evidence.append(f"Wikidata coordinates agree within {far:.0f} m")
    try:
        filename = claims["P18"][0]["mainsnak"]["datavalue"]["value"]
    except (KeyError, IndexError, TypeError):
        return None, "the Wikidata item names no image"
    if not isinstance(filename, str) or BAD_FILENAME.search(filename) or not re.search(r"\.jpe?g$", filename, re.I):
        return None, "the image is not a JPEG photograph by its file name"
    info = fetch_json("https://commons.wikimedia.org/w/api.php?" + urlencode({"action": "query", "titles": "File:" + filename, "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata",
                                                                            "iiurlwidth": 640, "format": "json"}))
    page = next(iter((info.get("query", {}).get("pages") or {}).values()), {})
    entry = (page.get("imageinfo") or [{}])[0]
    meta = entry.get("extmetadata") or {}
    licence = _clean_credit((meta.get("LicenseShortName") or {}).get("value"))
    creator = _clean_credit((meta.get("Artist") or {}).get("value"))
    if entry.get("mime") != "image/jpeg" or int(entry.get("width") or 0) < 800:
        return None, "the file is not a large enough JPEG"
    if not LICENCE_OK.match(licence) or LICENCE_BAD.search(licence):
        return None, f"the licence '{licence or 'unknown'}' does not allow reuse here"
    if meta.get("Restrictions", {}).get("value"):
        return None, "the file carries usage restrictions"
    thumb, page_url = entry.get("thumburl"), entry.get("descriptionurl")
    if not (isinstance(thumb, str) and re.match(r"^https://(upload|thumb)\.wikimedia\.org/", thumb) and isinstance(page_url, str) and page_url.startswith("https://commons.wikimedia.org/")):
        return None, "the file has no Wikimedia thumbnail address"
    body = fetch_bytes(thumb, ("image/jpeg",))
    if not (body[:3] == b"\xff\xd8\xff" and 2000 < len(body) <= MAX_PHOTO_BYTES):
        return None, "the downloaded image is not an acceptable JPEG"
    digest = hashlib.sha256(body).hexdigest()[:16]
    return {"file": f"p{digest}.jpg", "bytes": body, "venue": candidate.get("venue"), "qid": qid, "creator": creator, "license": licence,
            "license_url": (meta.get("LicenseUrl") or {}).get("value") or "", "source_url": page_url, "commons_file": filename,
            "attribution_required": not licence.upper().startswith(("CC0", "PUBLIC DOMAIN", "PD")), "evidence": evidence}, None


# --- storage and orchestration ------------------------------------------------------------------------------------

class MediaStore:
    """Files and an index under <data>/media: records by key, map/photo files by name, with bounded size."""

    def __init__(self, folder, clock=time.time):
        self.folder, self.clock = folder, clock
        self.files = os.path.join(folder, "files")
        self.lock = threading.Lock()
        os.makedirs(self.files, exist_ok=True)
        self.index = self._load()

    def _path(self, name):
        return os.path.join(self.folder, name)

    def _load(self):
        try:
            with open(self._path("index.json"), encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) and isinstance(data.get("records"), dict) else {"version": 1, "records": {}}
        except (OSError, ValueError):
            return {"version": 1, "records": {}}

    def save(self):
        temporary = self._path("index.json.tmp")
        with self.lock:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self.index, handle)
            os.replace(temporary, self._path("index.json"))

    def get_record(self, key):
        with self.lock:
            return copy.deepcopy(self.index["records"].get(key))

    def get_records(self, keys):
        with self.lock:
            return {key: copy.deepcopy(self.index["records"][key]) for key in keys if key in self.index["records"]}

    def put_record(self, key, record):
        with self.lock:
            self.index["records"][key] = copy.deepcopy(record)

    def mark_saved(self, at):
        with self.lock:
            self.index["saved_at"] = int(at)

    def revision(self):
        with self.lock:
            return int(self.index.get("saved_at") or 0) if self.index["records"] else 0

    def put_file(self, name, data):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{3,60}\.(svg|jpg)", name):
            raise ValueError("unsafe file name")
        temporary = os.path.join(self.files, name + ".tmp")
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, os.path.join(self.files, name))

    def read_file(self, name):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{3,60}\.(svg|jpg)", name or ""):
            return None
        try:
            with open(os.path.join(self.files, name), "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def has_file(self, name):
        return bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{3,60}\.(svg|jpg)", name or "")) and os.path.isfile(os.path.join(self.files, name))

    def intact(self, record):
        """Is every file a verified record points at still stored? (A record whose files are gone must not be advertised.)"""
        if not record.get("ok"):
            return True
        names = list((record.get("map") or {}).get("files", {}).values())
        if record.get("photo"):
            names.append(record["photo"].get("file"))
        return bool(names) and all(self.has_file(name) for name in names)

    def prune(self):
        """Drop expired records, and files no record points at; bound the number of records."""
        now = self.clock()
        records = self.index["records"]
        for key in [k for k, r in records.items() if now - r.get("at", 0) > (RECORD_TTL if r.get("ok") else REFUSAL_TTL)]:
            del records[key]
        if len(records) > MAX_RECORDS:
            for key in sorted(records, key=lambda k: records[k].get("at", 0))[:len(records) - MAX_RECORDS]:
                del records[key]
        keep = {r["photo"]["file"] for r in records.values() if r.get("ok") and r.get("photo")}
        keep |= {name for r in records.values() if r.get("ok") and r.get("map") for name in r["map"]["files"].values()}
        try:
            for name in os.listdir(self.files):
                if name not in keep:
                    os.remove(os.path.join(self.files, name))
        except OSError:
            pass


class MediaService:
    """Finds, verifies, draws and caches location imagery for the places in the current feeds."""

    def __init__(self, folder, fetcher, clock=time.time, sleep=time.sleep, nominatim_interval=NOMINATIM_INTERVAL, store=None):
        self.store = store if store is not None else MediaStore(os.path.join(folder, "media"), clock)   # a different store (the database-backed one) may be injected
        self.fetcher, self.clock, self.sleep = fetcher, clock, sleep
        self.nominatim_interval, self._nominatim_at = nominatim_interval, 0.0
        self._overpass_at = 0.0
        self.tiles_dir = os.path.join(folder, "media", "tiles")
        os.makedirs(self.tiles_dir, exist_ok=True)
        self._tile_template = None
        self._tile_template_at = 0.0
        self.stats = {"cycles": 0, "resolved": 0, "refused": 0, "failed": 0, "requests": 0}

    @property
    def revision(self):
        """Changes whenever the stored imagery changes (0 while there is none); part of the card's `since` token."""
        return self.store.revision()

    # --- network (bounded; every call is to a fixed public API host through the safe fetcher) -------------------

    def get_json(self, url, timeout=None):
        if url.startswith(NOMINATIM):
            # OpenStreetMap's usage policy: results are cached (they are), and a long-running program makes at most 4 lookups a minute
            wait = self._nominatim_at + self.nominatim_interval - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._nominatim_at = self.clock()
        self.stats["requests"] += 1
        return json.loads((self.fetcher.fetch(url, check_robots=False, timeout=timeout) if timeout else self.fetcher.fetch(url, check_robots=False)).text)

    def get_bytes(self, url, types):
        self.stats["requests"] += 1
        return self.fetcher.fetch(url, check_robots=False, binary=types).body

    def _tile(self, x, y):
        name = os.path.join(self.tiles_dir, f"14-{x}-{y}.pbf")
        try:
            if self.clock() - os.path.getmtime(name) < TILE_TTL:
                with open(name, "rb") as handle:
                    return maps.decode_tile(handle.read())
        except (OSError, ValueError):
            pass
        if not self._tile_template or self.clock() - self._tile_template_at > 86400:
            self._tile_template = self.get_json(TILEJSON)["tiles"][0]
            if not re.fullmatch(r"https://tiles\.openfreemap\.org/planet/[\w-]+/\{z\}/\{x\}/\{y\}\.pbf", self._tile_template):
                self._tile_template = None
                raise FetchError("unexpected tile address")
            self._tile_template_at = self.clock()
        raw = self.get_bytes(self._tile_template.format(z=14, x=x, y=y), ("application/x-protobuf", "application/octet-stream", "application/vnd.mapbox-vector-tile"))
        decoded = maps.decode_tile(raw)
        with open(name, "wb") as handle:
            handle.write(raw)
        self._prune_tiles()
        return decoded

    def _prune_tiles(self):
        try:
            names = sorted(os.listdir(self.tiles_dir), key=lambda n: os.path.getmtime(os.path.join(self.tiles_dir, n)))
            for name in names[:max(0, len(names) - MAX_TILE_FILES)]:
                os.remove(os.path.join(self.tiles_dir, name))
        except OSError:
            pass

    # --- one place ---------------------------------------------------------------------------------------------------

    def _resolve_intersection(self, candidate, record):
        """Place a pin where the two named streets share a node in OpenStreetMap's road network, or refuse with the reason."""
        crossing, area = candidate["intersection"], candidate["area"]
        where = crossing["locality"] or (area or {}).get("display_name") or (area or {}).get("name") or ""
        params = {"q": f"{numeric_ordinals(crossing['a_text'])}, {where}", "format": "jsonv2", "limit": 5, "addressdetails": 1}
        country = str((area or {}).get("country") or "").lower()
        if re.fullmatch(r"[a-z]{2}", country):
            params["countrycodes"] = country
        points = accept_street(candidate, crossing["a"], self.get_json(NOMINATIM + "?" + urlencode(params)))
        if not points:
            record["reason"] = f"'{crossing['a_text']}' was not found in the locality"
            return None
        box = bounding_box(points, INTERSECTION_RADIUS_M)
        if box is None:
            record["reason"] = f"the streets named '{crossing['a_text']}' are spread over too wide an area to check"
            return None
        wait = self._overpass_at + OVERPASS_INTERVAL - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._overpass_at = self.clock()
        data = self.get_json(OVERPASS + "?" + urlencode({"data": overpass_query(box, crossing["a"], crossing["b"])}), timeout=OVERPASS_TIMEOUT)
        places = shared_nodes(data.get("elements") if isinstance(data, dict) else None, crossing["a"], crossing["b"])
        if not places:
            record["reason"] = f"'{crossing['a_text']}' and '{crossing['b_text']}' do not meet in OpenStreetMap's road network"
            return None
        if len(places) > 1:
            record["reason"] = f"'{crossing['a_text']}' and '{crossing['b_text']}' meet in more than one place"
            return None
        place = places[0]
        if not _in_area(place["lat"], place["lon"], area):
            record["reason"] = "the intersection lies outside the search area"
            return None
        note = f"; {place['note']}" if place["note"] else ""
        return {"lat": place["lat"], "lon": place["lon"], "precision": "intersection", "confidence": "intersection", "approximate": True, "wikidata": None, "name": "",
                "provenance": f"intersection of {place['names'][0]} and {place['names'][1]}: OpenStreetMap node {place['node']} is shared by both streets{note}"}

    def resolve(self, candidate, draw=True, photos=True):
        """The record for a place: the first of the candidate and its `then` candidates that verifies, else a refusal giving every reason.
        `draw` and `photos` may be switched off by a diagnostic that only needs the location decision; production always uses both."""
        reasons, first = [], None
        option = candidate
        while option:
            record = self._resolve_one(option, draw, photos)
            record["rv"] = RESOLVER
            if first is None:
                first = record
            if record["ok"]:
                if option is not candidate:
                    record["via"] = option["kind"]
                    first_reason = "; ".join(reasons)
                    record["earlier"] = first_reason[:300]
                return record
            reasons.append(f"{option['kind']}: {record.get('reason', 'refused')}")
            option = option.get("then")
        first["reason"] = "; ".join(reasons)[:400] if len(reasons) > 1 else first.get("reason", "refused")
        return first

    def _resolve_one(self, candidate, draw=True, photos=True):
        now = self.clock()
        record = {"at": now, "ok": False, "kind": candidate["kind"], "venue": candidate.get("venue", "")}
        if candidate["kind"] == "source":
            location = {"lat": candidate["lat"], "lon": candidate["lon"], "precision": "venue", "confidence": "source", "approximate": False,
                        "provenance": candidate["provenance"], "wikidata": None, "name": candidate.get("venue", "")}
        elif candidate["kind"] == "intersection":
            location = self._resolve_intersection(candidate, record)
            if not location:
                self.stats["refused"] += 1
                return record
        else:
            where = (candidate["area"] or {}).get("display_name") or (candidate["area"] or {}).get("name") or ""
            if candidate["kind"] == "venue":
                queries = [f"{candidate['venue']}, {where}"] + [f"{v}, {where}" for v in candidate.get("variants") or []][:2]
            else:
                address = candidate["address"]
                if where and not re.search(r"\b[A-Z]{2}\b|\d{5}", address):
                    address = f"{address}, {where}"
                # the venue and its address together find the place itself (with its OpenStreetMap links); the address alone is the fallback
                queries = ([f"{candidate['venue']}, {address}"] if candidate.get("venue") and norm_name(candidate["venue"]) != norm_name(address) else []) + [address]
            location, reason = None, "the geocoder found nothing"
            for query in queries:
                params = {"q": query, "format": "jsonv2", "limit": 3, "addressdetails": 1, "extratags": 1, "namedetails": 1}
                country = str((candidate["area"] or {}).get("country") or "").lower()
                if re.fullmatch(r"[a-z]{2}", country):
                    params["countrycodes"] = country  # keeps a name from matching a place in another country
                results = self.get_json(NOMINATIM + "?" + urlencode(params))
                location, reason = accept_geocode(candidate, results)
                if location:
                    if candidate.get("found_in"):
                        location["provenance"] += f"; the address was taken from the event's {candidate['found_in']}"
                    break
            if not location:
                record["reason"] = reason
                self.stats["refused"] += 1
                return record
        files = self._draw_map(location) if draw else {}
        record.update(ok=True, location={k: location[k] for k in ("lat", "lon", "precision", "confidence", "approximate", "provenance")}, map={"files": files, "credit": MAP_CREDIT})
        try:
            photo, why = find_venue_photo(candidate, location, self.get_json, self.get_bytes) if candidate.get("names") and photos else (None, "no venue name" if photos else "not looked up")
        except (FetchError, ValueError, KeyError, TypeError, OSError) as error:
            photo, why = None, f"the lookup failed: {error}"
        if photo:
            self.store.put_file(photo["file"], photo.pop("bytes"))
            record["photo"] = {**photo, "fetched_at": int(now)}
        else:
            record["photo_reason"] = why
        self.stats["resolved"] += 1
        return record

    def _draw_map(self, location):
        key = hashlib.sha1(f"{RENDER_VERSION}|{location['lat']:.5f}|{location['lon']:.5f}".encode()).hexdigest()[:16]
        names = {"light": f"m{key}-light.svg", "dark": f"m{key}-dark.svg"}
        if all(self.store.read_file(n) for n in names.values()):
            return names
        tiles = {(x, y): self._tile(x, y) for x, y in maps.tiles_needed(location["lat"], location["lon"])}
        for theme, name in names.items():
            self.store.put_file(name, maps.render(tiles, location["lat"], location["lon"], theme).encode("utf-8"))
        return names

    # --- the cycle ---------------------------------------------------------------------------------------------------

    def cycle(self, feeds, limit=MAX_NEW_PER_CYCLE):
        """Resolve up to `limit` new places from `feeds` (an iterable of (location, events)); returns how many were handled."""
        self.stats["cycles"] += 1
        wanted = {}
        for area, events in feeds:
            for event in events:
                candidate = candidate_for(event, area)
                if candidate and candidate["key"] not in wanted:
                    wanted[candidate["key"]] = [candidate, 0]
                if candidate:
                    wanted[candidate["key"]][1] += 1
        records = self.store.get_records(wanted)
        # a place is looked up again when it has no record, or when its stored files have gone missing (the record then cannot serve)
        todo = [(count, c) for c, count in wanted.values() if self._needs_work(c, records)]
        todo.sort(key=lambda item: -item[0])
        done = 0
        for _, candidate in todo[:limit]:
            try:
                record = self.resolve(candidate)
            except (FetchError, ValueError, KeyError, TypeError, OSError) as error:
                log.warning("media for %s skipped: %s", candidate["kind"], error)
                self.stats["failed"] += 1
                continue  # not cached: a temporary failure is tried again in a later cycle
            self.store.put_record(candidate["key"], record)
            done += 1
        if done:
            self.store.prune()
            self.store.mark_saved(int(self.clock()))
            self.store.save()
        return done

    def _needs_work(self, candidate, records):
        """No record yet, a record whose files are gone, or a refusal made under older rules for a place the newer rules read differently."""
        record = records.get(candidate["key"])
        if record is None or not self.store.intact(record):
            return True
        if record.get("ok") or record.get("rv", 1) >= RESOLVER:
            return False
        return bool(candidate.get("retry_old")) or str(record.get("reason", "")).startswith("no result matched")

    def pending(self, feeds):
        candidates = {}
        for area, events in feeds:
            for event in events:
                candidate = candidate_for(event, area)
                if candidate:
                    candidates.setdefault(candidate["key"], candidate)
        have = self.store.get_records(candidates)
        keys = set()
        for candidate in candidates.values():
            if self._needs_work(candidate, have):
                keys.add(candidate["key"])
        return len(keys)

    # --- serving -------------------------------------------------------------------------------------------------------

    def annotate(self, events, area):
        """Events with `media` added where a verified map or venue photo exists (copies; the feed itself is untouched)."""
        candidates = [candidate_for(event, area) for event in events]
        records = self.store.get_records(candidate["key"] for candidate in candidates if candidate)
        if not records:
            return events
        out = []
        for event, candidate in zip(events, candidates):
            record = records.get(candidate["key"]) if candidate else None
            if not (record and record.get("ok")):
                out.append(event)
                continue
            media = {}
            photo = record.get("photo")
            if photo and not self.store.has_file(photo["file"]):
                photo = None  # never point the card at a file that is not there
            if photo:
                media["photo"] = {"url": f"{MEDIA_URL}/{photo['file']}", "venue": photo["venue"], "creator": photo["creator"], "license": photo["license"],
                                  "license_url": photo["license_url"], "source_url": photo["source_url"], "attribution_required": photo["attribution_required"]}
            where = record["location"]
            if not all(self.store.has_file(name) for name in record["map"]["files"].values()):
                out.append({**event, "media": media} if media else event)
                continue
            media["map"] = {"light": f"{MEDIA_URL}/{record['map']['files']['light']}", "dark": f"{MEDIA_URL}/{record['map']['files']['dark']}",
                            "confidence": where["confidence"], "precision": where["precision"], "approximate": where["approximate"], "credit": record["map"]["credit"]}
            out.append({**event, "media": media})
        return out
