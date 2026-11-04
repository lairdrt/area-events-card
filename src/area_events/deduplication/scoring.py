"""Weighted, confidence-based event comparison for the deduplication strategies.

Two events are compared field by field. Every comparable field yields a similarity between 0 and 1 (RapidFuzz for text); the confidence is the weighted mean of the
fields BOTH events state, so a missing field neither helps nor hurts, but too little compared evidence (`min_coverage`) or no comparable place at all never matches.
Date, start time and location carry most of the weight. Before any score counts, hard vetoes reject the pairs that must never merge: a start or end outside its
tolerance, conflicting source pages (when configured), a title, address or venue that is too different, and differing numbers in a title, address or venue
("Night 1" and "Night 2", "500" and "501 Harbor Drive"). Every weight, tolerance and threshold lives in `MatchProfile`, so a strategy is only a profile.

One identity stands apart from the score (`venue_time_identity`): the exact same start instant at the same specific physical venue, under related titles, is one
event whichever page either cites. Both must name the venue completely alike and both must state an address with a town that agrees; a conflicting street, suite,
room, postcode, town, written location or end time (outside the profile's tolerance) keeps them apart, and so does a missing address on either side. Related titles
are reworded, shortened or extended forms of each other (one's words, nearly all, among the other's: `venue_time_title_min`), never two titles that differ by a
number, a roman numeral or a year, and never two unrelated titles: two different events can share a stage and a start ("Chili Cook-Off" and "Live Music").

Pure: no database, no persistence, no source knowledge.
"""
from dataclasses import dataclass, replace
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz

ABBREVIATIONS = {"street": "st", "avenue": "ave", "av": "ave", "drive": "dr", "road": "rd", "boulevard": "blvd", "lane": "ln", "court": "ct", "place": "pl",
                 "suite": "ste", "building": "bldg", "north": "n", "south": "s", "east": "e", "west": "w", "mount": "mt", "saint": "st", "center": "ctr",
                 "centre": "ctr", "park": "park", "united states": "", "usa": ""}
NUMBER = re.compile(r"\d+")
UNIT = re.compile(r"(?:#\s*|\b(?:suite|ste|unit|room|rm|apt|apartment|bldg|building|floor)\b\.?\s*#?\s*)([a-z0-9][a-z0-9.-]*)", re.I)   # not "FL": Florida
POSTCODE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
YEAR = re.compile(r"\b(?:19|20)\d\d\b")
VENUE_TIME = "the same start instant at the same venue and address, under related titles"
GENERIC_PATH = frozenset(("", "event", "events", "calendar", "calendars", "listing", "listings", "home", "index"))
TRACKING = frozenset(("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"))
PLACE_FIELDS = (("address", "w_address", "min_address"), ("venue_name", "w_venue", "min_venue"), ("location_text", "w_location_text", "min_venue"))


@dataclass(frozen=True)
class MatchProfile:
    """Everything a strategy may tune. Weights are relative (they are normalised over the fields compared); similarities and thresholds are 0..1."""
    name: str = "profile"
    # weights
    w_start: float = 0.30
    w_date: float = 0.10
    w_address: float = 0.15
    w_venue: float = 0.10
    w_location_text: float = 0.05
    w_title: float = 0.20
    w_description: float = 0.05
    w_end: float = 0.05
    # tolerances (seconds)
    start_tolerance_s: int = 0
    end_tolerance_s: int = 0
    time_decay: float = 0.4               # similarity lost at the very edge of a tolerance (0 = none, 1 = all)
    # vetoes: the least similarity two stated values may have
    min_title: float = 0.92
    min_title_same_page: float = 0.85     # when both events cite the same specific page, a reworded title is accepted down to this
    min_address: float = 0.85
    min_venue: float = 0.85
    url_conflict_vetoes: bool = True
    # acceptance
    threshold: float = 0.92               # weighted confidence needed
    min_coverage: float = 0.60            # share of the total weight that was actually compared
    location_agreement: float = 0.90      # at least one comparable place field must reach this
    venue_alone_agrees: bool = False      # may a matching venue name (without an address or location text) be the place evidence?
    ambiguity_margin: float = 0.005       # a runner-up this close to the best match makes the answer ambiguous
    max_compared: int = 1000              # events examined for one decision (bounded lookup)
    no_place_title_min: float = 0.0       # 0 = off. When NEITHER event states any place, the least title similarity that still counts as the same event, provided
    no_place_description_min: float = 0.80   # the start (and end) are exactly equal and, when both state a description, the descriptions agree to this degree
    venue_time_identity: bool = True      # the same start instant at the same specific venue and address, under related titles, is one event, whatever the pages say
    venue_time_title_min: float = 0.90    # how nearly one title's words must all be among the other's for the venue/time identity (RapidFuzz token set ratio)

    def with_(self, **changes):
        return replace(self, **changes)


@dataclass(frozen=True)
class Score:
    matched: bool
    confidence: float
    reason: str
    venue_time: bool = False              # matched on the venue/time identity rather than the score


def normalize(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# A field label that a listing scraped into a title is removed only with explicit field evidence: the label word, a colon, then a date (or nothing): "... Date: Sat.",
# "... Date & Time:". A bare trailing "Date"/"When" is never removed, however the other title reads: "Blind Date", "Save the Date" and "Dinner Date" are real titles, and a
# stored title whose colon was already trimmed ("Halloween Finale Date") cannot be told from them by the matcher.
FIELD_LABEL = re.compile(
    r"\s+(?:dates?|when)(?:\s*(?:&|and)\s*times?)?\s*:\s*(?=$|(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b|\d).*$", re.I)


def _fielded(text):
    return FIELD_LABEL.sub("", (text or "").strip())


def title_key(text):
    """The normalized title (without an explicitly scraped field label): what a strategy buckets events by when they state no place."""
    return normalize(_fielded(text))


def _titles(left, right):
    """Both titles, normalized for comparison, without an explicitly scraped field label (see FIELD_LABEL)."""
    return title_key(left), title_key(right)


def _place(text):
    words = [ABBREVIATIONS.get(word, word) for word in normalize(text).split()]
    return " ".join(word for word in words if word)


def _address_parts(address, venue):
    """(street line, units, postcodes, town words) of a written address. A part that only repeats the venue's name ("Red Silo Brewing, Cookeville, TN") is not
    part of the place; a suite, room or unit ("#200", "Ste H", "Room 4.244") is set apart so that it is compared on its own."""
    units = frozenset(match.lower().rstrip(".") for match in UNIT.findall(address))
    parts = [_place(part) for part in UNIT.sub(" ", address).split(",")]
    parts = [part for part in parts if part and part != _place(venue)]
    street = next((part for part in parts if part.split()[0].isdigit()), "")
    rest = " ".join(part for part in parts if part != street)
    return street, units, frozenset(POSTCODE.findall(rest)), frozenset(word for word in rest.split() if not word.isdigit())


def _same_physical_place(left, right):
    """Do both events name the same specific venue completely alike, at addresses that agree (see the module's documentation)? Incomplete evidence is no."""
    venue = _place(getattr(left, "venue_name", None))
    if not venue or venue != _place(getattr(right, "venue_name", None)) or not left.address or not right.address:
        return False
    texts = _place(getattr(left, "location_text", None)), _place(getattr(right, "location_text", None))
    if all(texts) and texts[0] != texts[1]:
        return False
    (a_street, a_units, a_codes, a_town), (b_street, b_units, b_codes, b_town) = _address_parts(left.address, venue), _address_parts(right.address, venue)
    if not a_town or not b_town or not (a_town <= b_town or b_town <= a_town):
        return False
    return not ((a_street and b_street and a_street != b_street) or (a_units and b_units and a_units != b_units) or (a_codes and b_codes and a_codes != b_codes))


def _related_titles(left, right, floor):
    """Normalized titles that can name one event at one venue and instant (see the module's documentation)."""
    if _numbers_differ(left, right) or set(YEAR.findall(left)) != set(YEAR.findall(right)) or (set(left.split()) ^ set(right.split())) & EDITIONS:
        return False
    return fuzz.token_set_ratio(left, right) / 100.0 >= floor


def _url_identity(value):
    if not value:
        return ""
    before_query, separator, query = value.strip().split("#", 1)[0].partition("?")
    scheme, marker, rest = before_query.partition("://")
    if not marker:
        return normalize(value)
    host, _, path = rest.partition("/")
    pairs = sorted((k, v) for k, _, v in (pair.partition("=") for pair in query.split("&")) if separator and k.lower() not in TRACKING)
    return f"{scheme.lower()}://{host.lower()}/{path.rstrip('/')}?" + "&".join(f"{k}={v}" for k, v in pairs)


def specific_url(value):
    identity = _url_identity(value)
    if "://" not in identity:
        return False
    parts = identity.split("://", 1)[1].split("/", 1)
    segments = parts[1].split("?", 1)[0].lower().split("/") if len(parts) > 1 else []
    return any(segment not in GENERIC_PATH for segment in segments)


def _similarity(left, right, partial=False):
    if not left or not right:
        return 0.0
    best = max(fuzz.ratio(left, right), fuzz.token_sort_ratio(left, right))
    if partial and min(len(left), len(right)) >= 8:
        best = max(best, 0.95 * fuzz.partial_ratio(left, right))
    return best / 100.0


ROMAN = frozenset(("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"))
EDITIONS = ROMAN - {"i", "x"}   # "I" and "x" ("Melómano x UCSD") are ordinary words too


def _numbers_differ(left, right):
    """Digits that cannot both be right: {500} and {501} differ; {500} and {500, 92101} do not (one is the other plus a postcode)."""
    a, b = set(NUMBER.findall(left)), set(NUMBER.findall(right))
    return bool(a and b and not (a <= b or b <= a))


def _marker_differs(left, right, digits=True):
    """Do the two texts differ by a number, a roman numeral or a single letter ("Night 1" / "Night 2", "Concert" / "Concert II", "Hall A" / "Hall B")?
    Such a token is what tells one performance, room or edition from the next, however similar the rest is. Without `digits`, numbers are ignored entirely (a room
    number such as the "3" of "3.646" is no identifier here; `_numbers_differ` judges digits) while a single letter still counts."""
    return any((digits and token.isdigit()) or token in ROMAN or (len(token) == 1 and (digits or token.isalpha())) for token in set(left.split()) ^ set(right.split()))


# Whole words that exclude each other: an event for one group is not the event for the other. Each set is one side of a pair of opposites.
OPPOSITES = ((frozenset(("men", "mens", "man", "male", "boys", "boy")), frozenset(("women", "womens", "woman", "female", "girls", "girl", "ladies"))),)


def _qualifier_sides(title):
    """Which side of each pair of opposites a normalized title names: 'a', 'b', or both when it explicitly involves both groups."""
    words = set(title.split())
    return tuple((bool(words & first), bool(words & second)) for first, second in OPPOSITES)


def _qualifiers_oppose(left, right):
    """True when one title names one group and the other names the opposite one ("Men's Swimming" / "Women's Swimming"). A title naming both groups opposes neither."""
    return any(a_first != a_second and b_first != b_second and a_first != b_first
               for (a_first, a_second), (b_first, b_second) in zip(_qualifier_sides(left), _qualifier_sides(right)))


_ZONES = {}


def _day(instant, name):
    try:
        zone = _ZONES.get(name)
        if zone is None:
            zone = _ZONES[name] = ZoneInfo(name) if name else timezone.utc
    except Exception:
        zone = _ZONES[name] = timezone.utc
    return datetime.fromtimestamp(int(instant), zone).date()


def _same_day(left, right):
    """The calendar day in the zone either event states (the same zone for both, so that one event without a zone is not compared in UTC)."""
    name = getattr(left, "timezone", None) or getattr(right, "timezone", None)
    return _day(left.starts_at, name) == _day(right.starts_at, name)


def compare(left, right, profile, same_page=None):
    """Score one pair under a profile. `matched` is True only when no veto applies and the confidence, coverage and place evidence all pass."""
    a_title, b_title = _titles(left.title, right.title)
    if not a_title or not b_title:
        return Score(False, 0.0, "a title is missing")
    gap = abs(int(left.starts_at) - int(right.starts_at))
    if gap > profile.start_tolerance_s:
        return Score(False, 0.0, "the start times differ by more than the tolerance")
    end_gap = None
    if left.ends_at is not None and right.ends_at is not None:
        end_gap = abs(int(left.ends_at) - int(right.ends_at))
        if end_gap > profile.end_tolerance_s:
            return Score(False, 0.0, "the end times differ by more than the tolerance")
    if (profile.venue_time_identity and gap == 0 and _related_titles(a_title, b_title, profile.venue_time_title_min)
            and _same_physical_place(left, right)):
        return Score(True, 1.0, VENUE_TIME, venue_time=True)
    page_left, page_right = _url_identity(left.source_url), _url_identity(right.source_url)
    if page_left and page_right and page_left != page_right and profile.url_conflict_vetoes:
        return Score(False, 0.0, "the events cite different pages")
    if same_page is None:
        same_page = bool(page_left and page_left == page_right and specific_url(left.source_url))
    title = _similarity(a_title, b_title)
    floor = 1.0 if a_title == b_title else (profile.min_title_same_page if same_page else profile.min_title)
    if title < floor:
        return Score(False, title, "the titles are too different")
    if _marker_differs(a_title, b_title):
        return Score(False, title, "the titles differ by a number, numeral or letter")
    if _qualifiers_oppose(a_title, b_title):
        return Score(False, title, "the titles name opposing groups")

    parts = [(profile.w_title, title), (profile.w_start, 1.0 - profile.time_decay * gap / profile.start_tolerance_s if profile.start_tolerance_s else 1.0),
             (profile.w_date, 1.0 if _same_day(left, right) else 0.0)]
    if end_gap is not None:
        parts.append((profile.w_end, 1.0 - profile.time_decay * end_gap / profile.end_tolerance_s if profile.end_tolerance_s else 1.0))
    agreed = False
    for field, weight, floor_name in PLACE_FIELDS:
        a, b = _place(getattr(left, field, None)), _place(getattr(right, field, None))
        if not a or not b:
            continue
        if _numbers_differ(a, b) or (a != b and _marker_differs(a, b, digits=False)):
            return Score(False, title, f"the {field} differs by a number or letter")
        similarity = _similarity(a, b, partial=True)
        if similarity < getattr(profile, floor_name):
            return Score(False, title, f"the {field} values are too different")
        if field != "venue_name" or profile.venue_alone_agrees:
            agreed = agreed or similarity >= profile.location_agreement
        parts.append((getattr(profile, weight), similarity))
    description_left, description_right = normalize(getattr(left, "description", None))[:300], normalize(getattr(right, "description", None))[:300]
    if description_left and description_right:
        parts.append((profile.w_description, _similarity(description_left, description_right, partial=True)))

    unplaced = False
    if not agreed:
        # Nothing states a place on either side: only an exceptionally strong identity may stand in for the missing location. The start (and end) must be
        # exactly equal (both ends absent, or both stated and equal), the titles near-identical, the descriptions (when both exist) in agreement; every veto above still applies.
        stated = any(_place(getattr(event, field, None)) for event in (left, right) for field, _, _ in PLACE_FIELDS)
        if stated or not profile.no_place_title_min:
            return Score(False, title, "no comparable place fact agrees")
        if gap or (left.ends_at is None) != (right.ends_at is None) or end_gap or title < profile.no_place_title_min:   # ends: both absent, or both stated and equal
            return Score(False, title, "no place is stated and the events are not identical enough to match without one")
        if description_left and description_right and _similarity(description_left, description_right, partial=True) < profile.no_place_description_min:
            return Score(False, title, "no place is stated and the descriptions differ")
        unplaced = True
    possible = (profile.w_title + profile.w_start + profile.w_date + profile.w_end + profile.w_address + profile.w_venue + profile.w_location_text +
                profile.w_description)
    weight = sum(w for w, _ in parts)
    if weight <= 0 or (weight / possible < profile.min_coverage and not unplaced):
        return Score(False, 0.0, "too little evidence was comparable")
    confidence = sum(w * s for w, s in parts) / weight
    if confidence < profile.threshold:
        return Score(False, confidence, "the confidence is below the threshold")
    return Score(True, confidence, "matching date, start time, place and title")


def best_match(event, candidates, profile):
    """(match, reason): the single best candidate, or None when there is none, when the best is not clearly ahead of the runner-up (ambiguous), or when it is
    a tie. A runner-up within `ambiguity_margin` makes the answer ambiguous: the caller must not link. A venue/time identity is never ambiguous: it is the event
    already there, and when several are (duplicates made before the identity existed) the one whose title is nearest, then the lowest event key, so the same
    answer every time."""
    scored, same_place = [], []
    for other in candidates:
        result = compare(event, other, profile)
        if result.matched:
            scored.append((result.confidence, other.event_key, other))
            if result.venue_time:
                same_place.append(other)
    if same_place:
        wanted = title_key(event.title)
        first = min(same_place, key=lambda other: (-_similarity(wanted, title_key(other.title)), other.event_key))
        return first, VENUE_TIME + (f" (the nearest title of {len(same_place)} such events)" if len(same_place) > 1 else "")
    if not scored:
        return None, "no known event matches"
    scored.sort(key=lambda item: (-item[0], item[1]))
    if len(scored) > 1 and scored[0][0] - scored[1][0] < profile.ambiguity_margin:
        return None, f"ambiguous: {len(scored)} known events match about equally"
    return scored[0][2], f"matched with confidence {scored[0][0]:.2f}"
