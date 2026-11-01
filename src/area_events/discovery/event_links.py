"""Finding an event's own page when its source gives none: a producer-support utility, not a stage of event processing.

    EventLinkFinder.find_event_url(title, date, venue, city) -> str | None

A producer whose candidate has no validated event page asks this once per event before submitting it, and sets the candidate's `url` only from what
comes back. The finder owns everything about the search; a producer never sees a provider, key, query or ranking:

* Provider: the Tavily Search API (https://docs.tavily.com), whose free plan gives 1,000 credits every month with no card. One `basic` search of at
  most MAX_RESULTS results costs one credit; nothing billed at a higher rate (advanced depth, answers, raw content, auto parameters) is ever asked for.
* Key: TAVILY_API_KEY in the host process's environment (as SERPAPI_API_KEY is), held in memory and sent only in the Authorization header to the
  one fixed https endpoint. Without a key nothing is searched and nothing is stored.
* Limits: at most DAILY_LIMIT requests a UTC day and MONTHLY_LIMIT a UTC month (under the free 1,000), counted in the database, so a restart does not
  reset them. The provider's usage-limit answer (HTTP 432) ends searching for the rest of the month; any other failure pauses searching for PAUSE_S
  (DISABLED_S for a refused key). There are no retries: a failed lookup is None, and a later scan asks again.
* Cache: through Persistence's EventLinkCache port (one database). A page found is reused while the event is current; "nothing credible" is reused
  for MISS_TTL_S, since an event's page often appears nearer its date. A failure is never remembered as an answer.

A search result becomes the event's url only when it could be one event's page (`event_page`), names the event (`names_event`, the rule every
producer link meets), shows its venue and no other town, and states no other day or year for it (a run such as 'November 6-December 19' includes
its days; when a page was posted is not an event day). A page that shows the day is preferred, and among those the event's own page over a social
post, and a post over a resale listing (`judge`). Pages that tie without showing the day are ambiguous and give None. Nothing found by search is
invented, completed or made verified.
"""
import hashlib
import http.client
import json
import os
import re
import ssl
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

from .safe import USER_AGENT
from .urls import canonical_url

PROVIDER_HOST, PROVIDER_PATH = "api.tavily.com", "/search"
KEY_VARIABLE = "TAVILY_API_KEY"
MAX_RESULTS = 5
DAILY_LIMIT, MONTHLY_LIMIT = 40, 900
MISS_TTL_S = 14 * 86400
KEEP_PAST_DAYS = 30           # a lookup is dropped this long after its event's day
PAUSE_S, DISABLED_S = 3600, 86400
TIMEOUT_S, MAX_BYTES = 15, 500_000
MIN_INTERVAL_S = 1.0
LIMIT_EXCEEDED = 432          # Tavily: "key or plan usage limit exceeded"

# Addresses that are never one event's own page: searches, maps and the provider's own API (serpapi_event_details_link is a keyed details search)
_NOT_EVENT_PAGES = re.compile(r"(?:^|\.)(?:serpapi\.com|google(?:\.[a-z]{2,3}){1,2}|goo\.gl|bing\.com|duckduckgo\.com|maps\.apple\.com)$")
_WORD = re.compile(r"[a-z0-9]+")
_COMMON = frozenset(("the", "and", "for", "with", "from", "presents", "featuring", "event", "events"))


def event_page(value):
    """A web address that could be one event's page: http(s) with a host and a path, no credentials or spaces, not a search, map or SerpApi address."""
    if not isinstance(value, str) or len(value) > 2000 or re.search(r"\s", value):
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        credentials = parts.username or parts.password
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or credentials or _NOT_EVENT_PAGES.search(host) or parts.path.strip("/") == "":
        return None
    return value


def _distinctive(title, place):
    return {w for w in _WORD.findall(title.lower()) if len(w) >= 3 and w not in _COMMON and not w.isdigit()} - set(_WORD.findall(place.lower()))


def _has(words, w):
    return any(p == w or (len(w) >= 4 and p.startswith(w)) for p in words)


def names_event(url, title, place):
    """Does the address name this event by its title? At least two of the title's distinctive words (one, for a title with only one) in its path, not
    counting words of the venue, address or search location (a venue's own page names the venue, not the event)."""
    distinctive = _distinctive(title, place)
    words = _WORD.findall(unquote(urlsplit(url).path).lower())
    found = sum(1 for w in distinctive if _has(words, w))
    return bool(distinctive) and found >= min(2, len(distinctive))


_MONTHS = {name: number for number, names in enumerate((("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
                                                         ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
                                                         ("nov", "november"), ("dec", "december")), start=1) for name in names}
_MONTH = "(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b\.?"
_ORDINAL = r"(\d{1,2})(?:st|nd|rd|th)?\b(?!:\d)"                     # a day, not the hour of '17:00'
_OF_YEAR = r"(?:,?\s+((?:19|20)\d{2})(?!\d))?"
_NAMED_DAY = re.compile(r"\b" + _MONTH + r"\s+" + _ORDINAL + _OF_YEAR, re.IGNORECASE)                  # 'Nov 7', 'November 7th, 2026'
_DAY_NAMED = re.compile(r"(?<![\d.:])" + _ORDINAL + r"\s+" + _MONTH + _OF_YEAR, re.IGNORECASE)         # '7 November', '06 Nov, 2026'
_ISO_DAY = re.compile(r"(?<!\d)((?:19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
_US_DAY = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})(?!\d)")
# A run of days: 'November 6-December 19', 'Nov 14–15', 'November 1 – 8, 2026' (not the hours of 'Nov 6, 6 – 9 pm')
_RUN = re.compile(r"\b" + _MONTH + r"\s+" + _ORDINAL + _OF_YEAR + r"\s*(?:-|–|—|to|through|thru|until)\s*(?:" + _MONTH + r"\s+)?" + _ORDINAL
                  + r"(?!\s*(?:am|pm|a\.m|p\.m)\b)" + _OF_YEAR, re.IGNORECASE)
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_LONGEST_RUN_DAYS = 186
# When a page was written, not when its event is: 'Posted: Oct 22, 2026', a snippet's leading 'Oct 14, 2026 — ', a news address's /2026/10/07/
_PUBLISHED = re.compile(r"(?:posted|published|updated)(?:\s+on)?\s*:?\s*$", re.IGNORECASE)
_DATELINE = re.compile(r"^\s*(?:[a-z]{3,9}\.?\s+\d{1,2},\s+(?:19|20)\d{2}|\d{1,2}\s+[a-z]{3,9}\.?\s+(?:19|20)\d{2})\s+[—–-]\s+", re.IGNORECASE)
_NEWS_PATH = re.compile(r"^/(?:19|20)\d{2}/\d{1,2}/(?:\d{1,2}/)?")


def _days(text):
    """Every (year or None, month, day) the text states, leaving out when it was posted or updated."""
    found = []
    for pattern, order in ((_NAMED_DAY, "mdy"), (_DAY_NAMED, "dmy"), (_ISO_DAY, "ymd"), (_US_DAY, "mdy")):
        for match in pattern.finditer(text):
            if _PUBLISHED.search(text[max(0, match.start() - 24):match.start()]):
                continue
            parts = dict(zip(order, match.groups()))
            month = _MONTHS[parts["m"].lower()] if not parts["m"].isdigit() else int(parts["m"])
            found.append((int(parts["y"]) if parts["y"] else None, month, int(parts["d"])))
    return [(y, m, d) for y, m, d in found if 1 <= m <= 12 and 1 <= d <= 31]


def _runs(text, year):
    """Every run of days the text states, as (first, last) dates; a run that states no year is read in the event's `year`."""
    found = []
    for m1, d1, y1, m2, d2, y2 in _RUN.findall(text):
        last_year = int(y2 or y1 or year)
        first_month, last_month = _MONTHS[m1.lower()], _MONTHS[(m2 or m1).lower()]
        try:
            last = date(last_year, last_month, int(d2))
            first = date(int(y1) if y1 else last_year - ((first_month, int(d1)) > (last_month, int(d2))), first_month, int(d1))
        except ValueError:
            continue
        if 0 < (last - first).days <= _LONGEST_RUN_DAYS:
            found.append((first, last))
    return found


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _clean(value, limit=200):
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _identity(url):
    try:
        return canonical_url(url)
    except ValueError:
        return url


# Words that say what kind of place a venue is, not which one: 'Drift Cafe' is placed by 'drift', 'Revolver Dance Hall' by 'revolver'
_PLACE_NOUNS = frozenset((
    "venue", "venues", "park", "center", "centre", "hall", "church", "chapel", "school", "high", "college", "university", "company", "inc", "llc",
    "bar", "cafe", "coffee", "shop", "restaurant", "grill", "kitchen", "pub", "lounge", "brewing", "brewery", "winery", "club", "house", "room",
    "building", "theatre", "theater", "auditorium", "arena", "stadium", "amphitheater", "library", "museum", "gallery", "market", "farm", "hotel",
    "community", "county", "city", "town", "north", "south", "east", "west", "regional", "visitor", "main", "street", "plaza", "square",
    "station", "studio", "field", "course", "trail", "lot", "parking"))
# Where a result is published: resale marketplaces list another seller's tickets, social posts talk about an event; a direct page is the event's own
_RESALE = re.compile(r"(?:^|\.)(?:stubhub|viagogo|vividseats|tickpick|gametime|ticketnetwork|ticketsmarter|scorebig)\.[a-z.]+$")
_SOCIAL = re.compile(r"(?:^|\.)(?:facebook|instagram|twitter|x|tiktok|threads)\.(?:com|net)$")
_STATES = frozenset("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT "
                    "VT VA WA WV WI WY DC".split())
_TOWN = re.compile(r"\b([A-Z][a-z][A-Za-z.'’-]*(?:\s+[A-Z][a-z][A-Za-z.'’-]*){0,3}),\s*([A-Z]{2})\b")   # 'Valley Center, CA', 'Cookeville, TN'


def _kind(host, path):
    """2 for a direct page (an organizer's, venue's, calendar's or ticketing platform's), 1 for a social post, 0 for a resale listing."""
    if _RESALE.search(host):
        return 0
    if _SOCIAL.search(host):
        return 2 if path.startswith("/events/") else 1    # a Facebook event is the event's own page; a post is talk about it
    return 2


def _towns(text):
    """The words of every 'Town, ST' the text writes."""
    return [set(_WORD.findall(town.lower())) for town, state in _TOWN.findall(text) if state in _STATES]


def judge(results, title, day, venue, city):
    """The one credible event page among a provider's results, or None. Deterministic: the same results give the same answer.

    A result is this event's page only when its address names the event (`names_event`), it shows the event's place (a word that names the venue, or,
    without a venue, the town) and names no other town, and the days it states (single days or runs) include the event's day in the event's year. A
    result that states no day may still be the page; among several such pages none is chosen. A page that states the day comes first; then direct pages
    before social posts and those before resale listings; then the one whose heading carries more of the title."""
    place = " ".join(filter(None, (venue, city)))
    title_words = _distinctive(title, place)
    city_words = set(_WORD.findall(city.lower()))
    venue_words = {w for w in _WORD.findall(venue.lower()) if len(w) >= 3 and w not in _COMMON} - city_words
    specific = venue_words - _PLACE_NOUNS
    accepted = []
    for rank, item in enumerate(results):
        if not isinstance(item, dict):
            continue
        url = event_page(item.get("url"))
        if not url or not names_event(url, title, place):
            continue
        parts = urlsplit(url)
        path = _NEWS_PATH.sub("/", unquote(parts.path))       # a news address's /2026/10/07/ is when the story was posted
        heading = _clean(item.get("title"), 500)
        content = _DATELINE.sub("", _clean(item.get("content"), 2000))
        text = " ".join((heading, content, path, re.sub(r"[-_/]+", " ", path)))   # '2026-11-07' as written, 'nov-7' as words
        words = _WORD.findall(text.lower())
        if specific:
            placed = any(_has(words, w) for w in specific)
        elif venue_words:
            placed = all(_has(words, w) for w in venue_words)
        else:
            placed = bool(city_words) and all(w in words for w in city_words)
        if not placed:
            continue                                         # the title and day alone do not tell this event from another one elsewhere
        towns = _towns(" ".join((heading, content)))
        if city_words and towns and not any(city_words <= town for town in towns):
            continue                                         # another town's event of the same name
        stated, runs = _days(text), _runs(text, day.year)
        on_day = any((y is None or y == day.year) and (m, d) == (day.month, day.day) for y, m, d in stated)
        in_run = any(first <= day <= last for first, last in runs)
        if (stated or runs) and not (on_day or in_run):
            continue                                         # another day: another occurrence or another event
        years = {y for y, _, _ in stated if y} | {d.year for run in runs for d in run}
        if years and day.year not in years:
            continue                                         # the days it states are another year's
        path_years = {int(y) for y in _YEAR.findall(path)}
        if path_years and day.year not in path_years:
            continue                                         # an address for another year's event
        heading_words = _WORD.findall(heading.lower())
        dated = on_day or in_run
        accepted.append(((dated, _kind(parts.hostname.lower(), parts.path), sum(1 for w in title_words if _has(heading_words, w))), -rank, url))
    if not accepted:
        return None
    accepted.sort(reverse=True)
    score, _, best = accepted[0]
    if not score[0] and len({_identity(url) for s, _, url in accepted if s == score}) > 1:
        return None                                          # pages that agree only on the place: which one is this event's is not known
    return best


def https_post(host, path, headers, body, timeout=TIMEOUT_S, limit=MAX_BYTES):
    """One POST to a fixed https endpoint: (status, body). Nothing is followed; a body over `limit` is refused."""
    connection = http.client.HTTPSConnection(host, 443, timeout=timeout, context=ssl.create_default_context())
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read(limit + 1)
        if len(data) > limit:
            raise OSError("the answer is too large")
        return response.status, data
    finally:
        connection.close()


class EventLinkFinder:
    """See the module's documentation. `cache` is Persistence's EventLinkCache. Safe to share between producers and threads; `find_event_url` never raises.
    `status` says what the last lookup did, for a run's notes."""

    def __init__(self, cache, api_key=None, clock=time.time, post=https_post, sleep=time.sleep, daily_limit=DAILY_LIMIT, monthly_limit=MONTHLY_LIMIT):
        key = api_key if api_key is not None else os.environ.get(KEY_VARIABLE)
        self._key = key.strip() if isinstance(key, str) and key.strip() else None
        self._cache, self._clock, self._post, self._sleep = cache, clock, post, sleep
        self._daily, self._monthly = daily_limit, monthly_limit
        self._lock, self._paused_until, self._last_request, self._tidied = threading.Lock(), 0.0, float("-inf"), None
        self.status = "ready" if self._key else "unconfigured"

    def find_event_url(self, title, date, venue, city):
        """The event's own page found by one web search, or None (no credible match, no key, a limit reached or the provider unavailable)."""
        try:
            return self._find(_clean(title, 300), _day(date), _clean(venue), _clean(city))
        except Exception:   # a lookup never stops a producer
            self.status = "failed"
            return None

    # --- internals ---------------------------------------------------------------------------------

    def _find(self, title, day, venue, city):
        if not self._key or not title or day is None:
            return None
        key = hashlib.sha256("\x1f".join((title.lower(), day.isoformat(), venue.lower(), city.lower())).encode("utf-8")).hexdigest()
        with self._lock:
            now = self._clock()
            today = datetime.fromtimestamp(now, timezone.utc).date()
            if self._tidied != today:   # once a day: lookups for long-past events and old counts go
                self._cache.forget_before((today - timedelta(days=KEEP_PAST_DAYS)).isoformat())
                self._tidied = today
            known = self._cache.recall(key)
            if known and (known[0] or now - known[1] < MISS_TTL_S):
                self.status = "cached"
                return known[0]
            if now < self._paused_until:
                return None
            periods = (today.isoformat(), today.isoformat()[:7])
            used = self._cache.requests(periods)
            if used.get(periods[0], 0) >= self._daily or used.get(periods[1], 0) >= self._monthly:
                self.status = "limit reached"
                return None
            self._cache.count_request(periods)   # counted before asking: a request that fails may still cost a credit
            self._sleep(max(0.0, self._last_request + MIN_INTERVAL_S - self._clock()))
            self._last_request = self._clock()
            results = self._search(title, day, venue, city, periods[1])
            if results is None:
                return None
            url = judge(results, title, day, venue, city)
            self._cache.remember(key, url, int(now), day.isoformat())
            self.status = "searched"
            return url

    def _search(self, title, day, venue, city, month):
        query = _clean(" ".join(filter(None, (title, venue, city, f"{day:%B} {day.day}, {day.year}"))), 380)
        body = json.dumps({"query": query, "search_depth": "basic", "max_results": MAX_RESULTS, "topic": "general", "include_answer": False,
                           "include_raw_content": False, "include_images": False, "auto_parameters": False}).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
        try:
            status, data = self._post(PROVIDER_HOST, PROVIDER_PATH, headers, body)
        except Exception:   # its message could quote the request: none of it is kept
            return self._pause(PAUSE_S, "provider unavailable")
        if status == LIMIT_EXCEEDED:
            self._cache.use_up(month, self._monthly)
            return self._pause(0, "provider usage limit reached")
        if status == 401:
            return self._pause(DISABLED_S, "key refused")
        if status != 200:
            return self._pause(PAUSE_S, f"provider answered HTTP {int(status)}")
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._pause(PAUSE_S, "provider answer unreadable")
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return self._pause(PAUSE_S, "provider answer unreadable")
        return [item for item in results if isinstance(item, dict) and self._key not in str(item.get("url"))]

    def _pause(self, seconds, status):
        self._paused_until = max(self._paused_until, self._clock() + seconds)
        self.status = status
        return None
