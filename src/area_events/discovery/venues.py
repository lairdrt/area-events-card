"""Resolving a venue to its Google Maps business record: a producer-support utility (a sibling of event_links.EventLinkFinder), not a stage of event processing.

    VenueResolver.resolve_venue(name, city, state) -> VenueResolution
    VenueResolver.for_scan() -> a view with the same resolve_venue that makes at most SCAN_LIMIT new searches (one producer scan)

A producer whose candidate has no street address asks this for the candidate's venue and town; it never sees a provider, key, query or result list.
The resolver does not know, and is not told, which producer asks.

    matched      one business of that name in that town, with a street address: the provider's name, address, coordinates and place id, and why
    no_match     no such business (a different business, another town only, nothing usable, or no street address)
    ambiguous    several such businesses at different places
    unavailable  no answer: no key, the account or a budget allows no search, or the provider failed. Never an answer about the venue.

* Provider: SerpApi's Google Maps engine (engine=google_maps, type=search, q="<venue>, <city>, <state>") through the service's ONE SerpApiAccount: every
  search holds its permit (the free-tier hard stop and the automatic pause apply) and counts against the same monthly allowance as the Google Events
  searches, and goes through the shared safe fetch client with the key as secret_query, never in an address that is kept. For a query that names one
  business Google answers with that business's own record (`place_results`); for several possibilities with a list (`local_results`). Each record
  states its name, address, coordinates and place id together, so a name is never paired with an address from a different record.
* Budget: at most MONTHLY_LIMIT Maps searches per UTC month, reserved in the database before the search (so a restart or a concurrent lookup cannot pass
  it), and at most SCAN_LIMIT new searches per scan view. A cached answer costs nothing. There are no retries: a failed search is 'unavailable', and a
  later scan may ask again.
* Cache: through Persistence's VenueCache port, by the normalized venue, town and state, so different events at one venue share one answer: a match is
  reused for MATCH_TTL_S, a no_match or ambiguity for MISS_TTL_S. 'unavailable' is never remembered.
* Matching (`judge`): the record's name must be the venue's name (letters and digits compared word for word, after harmless punctuation, '&'/'and',
  common abbreviations, business suffixes such as Inc or LLC, a parenthetical, and the town's own name when at least two words remain), its address
  must have a street line followed by the venue's town and state, and exactly one place may qualify. Nothing is inferred or completed: an address,
  its house number, street, ZIP code and coordinates are the provider's own. A match is provider-backed evidence, not independent proof of the place.
* Locks: the resolver's own lock guards only its daily tidy; the account's permit, held for the search alone, is the one lock around a network request.
"""
import hashlib
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from .webpage import STREET_LINE

SEARCH_URL = "https://serpapi.com/search"   # SerpApi's keyed search endpoint, as the Google Events producer uses it
KEY_FIELD = "api_key"
MONTHLY_LIMIT = 20             # Maps searches per UTC month, under (and counted in) the shared SerpApi allowance
SCAN_LIMIT = 3                 # new Maps searches per scan view
MATCH_TTL_S = 90 * 86400
MISS_TTL_S = 30 * 86400
NO_RESULTS = "hasn't returned any results"

_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY"}
_STATE_CODES = frozenset(_STATE_NAMES.values())
_WORD = re.compile(r"[a-z0-9]+")
_PARENTHETICAL = re.compile(r"\([^()]*\)")
_ABBREVIATIONS = {"st": "saint", "mt": "mount", "ft": "fort", "ctr": "center", "centre": "center", "theatre": "theater", "bros": "brothers"}
_FILLER = frozenset(("the", "and", "of", "at", "inc", "llc", "ltd", "co", "corp", "corporation", "company"))


@dataclass(frozen=True)
class VenueResolution:
    """One venue's answer. Only 'matched' carries a business; coordinates and place id only when the provider gave them."""
    status: str                          # matched | no_match | ambiguous | unavailable
    name: Optional[str] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    place_id: Optional[str] = None
    evidence: str = ""
    cached: bool = False

    @property
    def matched(self):
        return self.status == "matched"


def _unavailable(reason):
    return VenueResolution("unavailable", evidence=reason)


def _clean(value, limit=200):
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _words(text):
    """The words of a name or place, compared without case, apostrophes, punctuation or common abbreviations."""
    return tuple(_ABBREVIATIONS.get(w, w) for w in _WORD.findall(text.lower().replace("&", " and ").replace("'", "").replace("’", "")))


def state_code(text):
    """The two-letter US state of 'TN', 'TN 38501', 'Tennessee' or 'Tennessee 38501', or None."""
    words = [w for w in re.findall(r"[A-Za-z]+", text or "")]
    if words and len(words[0]) == 2 and words[0].upper() in _STATE_CODES:
        return words[0].upper()
    name = " ".join(w.lower() for w in words)
    return next((code for full, code in _STATE_NAMES.items() if name == full or name.startswith(full + " ")), None)


def _forms(name, town):
    """The comparable forms of a business name: its words without filler, without a parenthetical, and without the town's own words while two remain."""
    forms = set()
    for text in {name, _PARENTHETICAL.sub(" ", name)}:
        words = tuple(w for w in _words(text) if w not in _FILLER)
        if words:
            forms.add(words)
            local = tuple(w for w in words if w not in town)
            if len(local) >= 2:
                forms.add(local)
    return forms


def same_name(wanted, offered, city):
    """Is the provider's business name the venue's own name (see the module's documentation)?"""
    town = frozenset(_words(city))
    return bool(_forms(wanted, town) & _forms(offered, town))


def in_town(address, city, state):
    """(street, town): does the address have a street line before the venue's town and state ('118 W 1st St, Cookeville, TN 38501')?"""
    parts = [p.strip() for p in address.split(",")]
    town = _words(city)
    for index in range(len(parts) - 1):   # the town may come first: an address that is only 'Cookeville, TN 38501' is in town, without a street
        if _words(parts[index]) == town and state_code(parts[index + 1]) == state:
            return bool(STREET_LINE.search(", ".join(parts[:index]))), True
    return False, False


def _number(value, bound):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and -bound <= value <= bound else None


def records(payload):
    """The business records of an answer: its `place_results` (Google's own pick for a query that names one business) and its `local_results`."""
    found = [payload["place_results"]] if isinstance(payload.get("place_results"), dict) else []
    if isinstance(payload.get("local_results"), list):
        found += [entry for entry in payload["local_results"] if isinstance(entry, dict)]
    return found


def judge(payload, name, city, state):
    """The VenueResolution an answer gives for the venue (never 'unavailable': an answer that cannot be read is the caller's)."""
    named, elsewhere, placed, unique = [], set(), [], {}
    for record in records(payload):
        title, address = _clean(record.get("title"), 300), _clean(record.get("address"), 300)
        if not title or not same_name(name, title, city):
            continue
        named.append(title)
        street, town = in_town(address, city, state) if address else (False, False)
        if not town:
            elsewhere.add(address or "no address")
            continue
        placed.append(title)
        if not street:
            continue
        gps = record.get("gps_coordinates") if isinstance(record.get("gps_coordinates"), dict) else {}
        latitude, longitude = _number(gps.get("latitude"), 90), _number(gps.get("longitude"), 180)
        if latitude is None or longitude is None:
            latitude = longitude = None
        place_id = _clean(record.get("place_id"), 200) or None
        unique.setdefault(place_id or " ".join(_words(address)), (title, address, latitude, longitude, place_id))
    searched = f"'{name}, {city}, {state}'"
    if len(unique) == 1:
        title, address, latitude, longitude, place_id = next(iter(unique.values()))
        return VenueResolution("matched", title, address, latitude, longitude, place_id,
                               f"the Google Maps business record '{title}'" + (f" (place {place_id})" if place_id else "")
                               + f" is the only business of that name in {city}, {state} for a SerpApi Google Maps search for {searched}; provider-reported")
    if len(unique) > 1:
        return VenueResolution("ambiguous", evidence=f"{len(unique)} Google Maps businesses named like {searched} at different addresses in {city}, {state}")
    if placed:
        return VenueResolution("no_match", evidence=f"the Google Maps business named like {searched} has no street address")
    if named:
        return VenueResolution("no_match", evidence=f"Google Maps businesses named like {searched} are only elsewhere ({'; '.join(sorted(elsewhere))[:200]})")
    return VenueResolution("no_match", evidence=f"no Google Maps business named like {searched}")


class VenueResolver:
    """See the module's documentation. `account` is the shared SerpApiAccount (its permit() and secret()); `cache` is Persistence's VenueCache; `fetch` is
    the shared safe fetch client (SafeFetcher). Safe to share between producers and threads; a lookup never raises. `status` says what the last lookup did."""

    def __init__(self, account, cache, clock=time.time, fetch=None, monthly_limit=MONTHLY_LIMIT, scan_limit=SCAN_LIMIT):
        self._account, self._cache, self._clock, self._fetch = account, cache, clock, fetch
        self._monthly, self._scan_limit = monthly_limit, scan_limit
        self._lock, self._tidied = threading.Lock(), None   # guards only the daily tidy
        self._configured = account is not None and bool(getattr(account, "configured", False)) and fetch is not None
        self.status = "ready" if self._configured else "unconfigured"

    def for_scan(self):
        """A view for one producer scan: the same lookups, with at most `scan_limit` new searches between them."""
        return _ScanVenues(self, self._scan_limit)

    def resolve_venue(self, name, city, state):
        """The venue's Google Maps business record, or why there is none (see VenueResolution). Not bounded per scan: a producer uses for_scan()."""
        return self._resolve(name, city, state, None)

    # --- internals ---------------------------------------------------------------------------------

    def _resolve(self, name, city, state, scan):
        try:
            return self._lookup(_clean(name), _clean(city, 100), state_code(_clean(state, 60)), scan)
        except Exception as error:   # a lookup never stops a producer; its message could quote a request, so only its kind is kept
            self.status = "failed"
            return _unavailable(f"the venue lookup failed ({type(error).__name__})")

    def _lookup(self, name, city, state, scan):
        if not name or not city or not state:
            self.status = "nothing to look up"
            return _unavailable("the venue, its town or its state is missing")
        query = f"{name}, {city}, {state}"
        key = hashlib.sha256("|".join((" ".join(_words(name)), " ".join(_words(city)), state)).encode("utf-8")).hexdigest()
        now = self._clock()
        month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
        self._tidy(now)
        known = self._recall(key, now)
        if known is not None:
            self.status = "cached"
            return known
        if not self._configured:
            self.status = "unconfigured"
            return _unavailable("no SerpApi key or fetch client is configured; no Maps search is made")
        if scan is not None and scan.searches >= scan.limit:
            self.status = "scan limit reached"
            return _unavailable(f"this scan has made its {scan.limit} new Maps searches")
        if self._cache.requests((month,)).get(month, 0) >= self._monthly:
            self.status = "monthly limit reached"
            return _unavailable(f"the {self._monthly} Maps searches of {month} are used")
        url = f"{SEARCH_URL}?{urlencode({'engine': 'google_maps', 'type': 'search', 'q': query})}"
        try:
            with self._account.permit():   # the account's own lock and allowance: the hard stop and the automatic pause apply to Maps as to Events
                # counted before asking (a request that fails may still be billed); losing a race for the month's last search here leaves the account
                # counting one search that was not made, which only errs towards searching less
                if not self._cache.reserve_request({month: self._monthly}):
                    self.status = "monthly limit reached"
                    return _unavailable(f"the {self._monthly} Maps searches of {month} are used")
                if scan is not None:
                    scan.searches += 1
                # SerpApi documents /search as its keyed API endpoint; its robots.txt, written for crawlers, disallows that path (as for Google Events).
                text = self._fetch.get(url, check_robots=False, secret_query=self._account.secret(), drop_fields=(KEY_FIELD,))
        except Exception as error:   # the account refused (UsageBlocked carries its state), or the provider or network failed: no answer
            self.status = "provider unavailable"
            return _unavailable(f"no Maps answer ({getattr(error, 'state', None) or type(error).__name__})")
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            self.status = "provider answer unreadable"
            return _unavailable("the Maps answer is not a JSON object")
        if payload.get("error"):
            if NO_RESULTS not in str(payload["error"]):
                self.status = "provider error"
                return _unavailable("the Maps search reported an error")
            found = VenueResolution("no_match", evidence=f"Google Maps has no results for '{query}'")
        elif "place_results" not in payload and "local_results" not in payload:
            self.status = "provider answer unreadable"
            return _unavailable("the Maps answer has no business records")
        else:
            found = judge(payload, name, city, state)
        self._cache.remember(key, query, found.status, found.name, found.address, found.latitude, found.longitude, found.place_id, found.evidence, int(now))
        self.status = "searched"
        return found

    def _recall(self, key, now):
        known = self._cache.recall(key)
        if known is None:
            return None
        status, name, address, latitude, longitude, place_id, evidence, checked_at = known
        if now - checked_at >= (MATCH_TTL_S if status == "matched" else MISS_TTL_S):
            return None
        return VenueResolution(status, name, address, latitude, longitude, place_id, evidence, cached=True)

    def _tidy(self, now):
        today = datetime.fromtimestamp(now, timezone.utc).date()
        with self._lock:
            tidy, self._tidied = self._tidied != today, today
        if tidy:   # once a day: answers older than any reuse, and the counts of months before the last
            last_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            self._cache.forget_before(int(now) - max(MATCH_TTL_S, MISS_TTL_S), last_month)


class _ScanVenues:
    """One scan's view of the shared resolver: the same answers and cache, at most `limit` new searches."""

    def __init__(self, resolver, limit):
        self._resolver, self.limit, self.searches = resolver, limit, 0

    @property
    def status(self):
        return self._resolver.status

    def resolve_venue(self, name, city, state):
        return self._resolver._resolve(name, city, state, self)
