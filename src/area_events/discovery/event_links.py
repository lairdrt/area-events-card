"""Finding an event's own page when its source gives none: a producer-support utility, not a stage of event processing.

    EventLinkFinder.find_event_link(title, date, venue, city) -> EventLink(url, address, address_basis) | None
    EventLinkFinder.find_event_url(title, date, venue, city) -> str | None          (the same lookup, its url only)

A producer whose candidate has no validated event page asks this once per event before submitting it, and sets the candidate's `url` (and, where
it has no street address, its `address`) only from what comes back. The finder owns everything about the search and the page reads; a producer
never sees a provider, key, query, ranking or page:

* Provider: the Tavily Search API (https://docs.tavily.com), whose free plan gives 1,000 credits every month with no card. One `basic` search of at
  most MAX_RESULTS results costs one credit; nothing billed at a higher rate (advanced depth, answers, raw content, auto parameters) is ever asked for.
* Key: TAVILY_API_KEY in the host process's environment (as SERPAPI_API_KEY is), held in memory and sent only in the Authorization header to the
  one fixed https endpoint. Without a key nothing is searched and nothing is stored.
* Limits: at most DAILY_LIMIT requests a UTC day and MONTHLY_LIMIT a UTC month (under the free 1,000), reserved in the database in one transaction
  before each request, so neither concurrent lookups nor a restart can pass them; requests are at least MIN_INTERVAL_S apart. The provider's
  usage-limit answer (HTTP 432) ends searching for the rest of the month; any other failure pauses searching for PAUSE_S (DISABLED_S for a refused
  key). There are no retries: a failed lookup is None, and a later scan asks again.
* Cache: through Persistence's EventLinkCache port (one database). A page found (with its address) is reused while the event is current, so it is
  neither searched for nor read again; "nothing credible" (no proposal, or every proposed page read and not the event's) is reused for MISS_TTL_S,
  since an event's page often appears nearer its date. When proposed pages could not be read for a passing reason (the network, a timeout, a server
  error, a site asking to slow down) and none was confirmed, those pages are kept and read again after REREAD_S, without a new search, at most
  MAX_REREADS times; after that the lookup is "nothing credible". A provider failure is never remembered as an answer.
* Threads: lookups of different events run side by side; the shared lock guards only the finder's own bookkeeping, never a search or a page read.
  A second lookup of an event already being looked up waits for that one (LOOKUP_WAIT_S at most) and takes its answer from the cache.

Two steps, and only the second can make a page the event's:

1. The search results are ranked by what their snippets say (`nominate`): a result can be proposed only when it could be one event's page
   (`event_page`), names the event (`names_event`, the rule every producer link meets), shows its venue and no other town, and states no other day
   or year for it (a run such as 'November 6-December 19' includes its days; when a page was posted is not an event day). One that shows the day
   comes first, and among those the event's own page before a social post, and a post before a resale listing.
2. At most MAX_PAGES proposed pages are read through the shared safe fetch client (public addresses only, robots.txt, redirects checked, size and
   time limits, host spacing), and a page is the event's only when the page itself confirms it (`check_page`): its title names the event, its
   structured data (JSON-LD Event) or its text gives the event's day (or a run that includes it) and the venue, and nothing on it gives the event
   another day, year, venue or town. A page that cannot be read or confirmed is never returned; among confirmed pages the event's own page is preferred.

The street address comes only from the confirmed page, and only when it belongs to the event's venue: the event's structured location, else a labelled
location field naming the venue, else the text written beside the venue's name. A footer address only when the page states none of those, the website
is demonstrably the venue's own (its name names the venue and its domain spells it), the footer gives it as the venue's location (under a location
label or the venue's name, not as a mailing, office or billing address) with the event's town, and it is the only one. Different addresses for the
venue give none. Nothing found is invented, completed or made verified.
"""
import hashlib
import http.client
import json
import os
import re
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import unquote, urlsplit

from .safe import USER_AGENT, BlockedError, FetchError
from .urls import canonical_url
from .webpage import EVENT_KINDS, STREET_LINE, PageParts, event_documents, page_text

PROVIDER_HOST, PROVIDER_PATH = "api.tavily.com", "/search"
KEY_VARIABLE = "TAVILY_API_KEY"
MAX_RESULTS = 5
MAX_PAGES = 3                 # proposed pages read for one lookup
DAILY_LIMIT, MONTHLY_LIMIT = 40, 900
MISS_TTL_S = 14 * 86400
REREAD_S, MAX_REREADS = 3 * 3600, 3   # proposed pages that could not be read are read again (not searched again) after REREAD_S, at most MAX_REREADS times
LOOKUP_WAIT_S = 180           # how long a lookup waits for another thread's lookup of the same event (one search: up to MAX_PAGES bounded reads)
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


class _Event:
    """What a page is compared with: the event's distinctive title words, its day, and the words of its venue and town."""

    def __init__(self, title, day, venue, city):
        self.title, self.day, self.venue, self.place = title, day, venue, " ".join(filter(None, (venue, city)))
        self.title_words = _distinctive(title, self.place)
        self.city_words = set(_WORD.findall(city.lower()))
        self.venue_words = {w for w in _WORD.findall(venue.lower()) if len(w) >= 3 and w not in _COMMON} - self.city_words
        self.specific = self.venue_words - _PLACE_NOUNS

    def placed(self, text):
        """Does the text show the event's place: a word that names the venue ('julep' of 'JULEP Venue'), or, without a venue, the whole town?"""
        words = _WORD.findall(text.lower())
        if self.specific:
            return any(_has(words, w) for w in self.specific)
        if self.venue_words:
            return all(_has(words, w) for w in self.venue_words)
        return bool(self.city_words) and all(w in words for w in self.city_words)

    def names_venue(self, text):
        """Does the text name the venue: every word that names it ('red' and 'silo' of 'Red Silo Brewing'; every word, when all say only what kind of
        place it is)?"""
        words = _WORD.findall(text.lower())
        wanted = self.specific or self.venue_words
        return bool(wanted) and all(_has(words, w) for w in wanted)

    def named(self, text, share=0.0):
        """Does the text name the event: at least two of its title's distinctive words (one, for a title with only one), and at least `share` of them?"""
        words = _WORD.findall(text.lower())
        found = sum(1 for w in self.title_words if _has(words, w))
        return bool(self.title_words) and found >= max(min(2, len(self.title_words)), -(-len(self.title_words) * share // 1))

    def other_town(self, text):
        """Does the text name a town ('Knoxville, TN') and not the event's?"""
        towns = _towns(text)
        return bool(self.city_words and towns) and not any(self.city_words <= town for town in towns)

    def dates(self, text):
        """(states a day, includes the event's day, states only other years) for the days and runs the text writes."""
        stated, runs = _days(text), _runs(text, self.day.year)
        on_day = any((y is None or y == self.day.year) and (m, d) == (self.day.month, self.day.day) for y, m, d in stated)
        in_run = any(first <= self.day <= last for first, last in runs)
        years = {y for y, _, _ in stated if y} | {d.year for run in runs for d in run}
        return bool(stated or runs), on_day or in_run, bool(years) and self.day.year not in years


def nominate(results, title, day, venue, city):
    """The results whose snippets could be this event's page, best first, as ((dated, kind, title words in the heading), url). Deterministic.

    A result is proposed only when its address names the event (`names_event`), it shows the event's place and names no other town, and the days it
    states (single days or runs) include the event's day in the event's year. A result that states the day comes first; then direct pages before
    social posts and those before resale listings (`_kind`); then the one whose heading carries more of the title; then the provider's order."""
    event = _Event(title, day, venue, city)
    accepted = []
    for rank, item in enumerate(results):
        if not isinstance(item, dict):
            continue
        url = event_page(item.get("url"))
        if not url or not names_event(url, title, event.place):
            continue
        parts = urlsplit(url)
        path = _NEWS_PATH.sub("/", unquote(parts.path))       # a news address's /2026/10/07/ is when the story was posted
        heading = _clean(item.get("title"), 500)
        content = _DATELINE.sub("", _clean(item.get("content"), 2000))
        text = " ".join((heading, content, path, re.sub(r"[-_/]+", " ", path)))   # '2026-11-07' as written, 'nov-7' as words
        if not event.placed(text):
            continue                                         # the title and day alone do not tell this event from another one elsewhere
        if event.other_town(" ".join((heading, content))):
            continue                                         # another town's event of the same name
        stated, dated, other_years = event.dates(text)
        if stated and not dated:
            continue                                         # another day: another occurrence or another event
        if other_years:
            continue                                         # the days it states are another year's
        path_years = {int(y) for y in _YEAR.findall(path)}
        if path_years and day.year not in path_years:
            continue                                         # an address for another year's event
        heading_words = _WORD.findall(heading.lower())
        accepted.append(((dated, _kind(parts.hostname.lower(), parts.path), sum(1 for w in event.title_words if _has(heading_words, w))), -rank, url))
    accepted.sort(reverse=True)
    return [(score, url) for score, _, url in accepted]


def judge(results, title, day, venue, city):
    """The one credible event page among a provider's results by their snippets alone, or None: the first `nominate`d, unless several pages that do
    not state the day tie for first (which one is this event's is then not known)."""
    ranked = nominate(results, title, day, venue, city)
    if not ranked:
        return None
    score, best = ranked[0]
    if not score[0] and len({_identity(url) for s, url in ranked if s == score}) > 1:
        return None
    return best


# --- the page itself ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PageCheck:
    """What a read page established: `confirmed` that it is the event's page (else `reason` says why not), and the venue's street address it states."""
    confirmed: bool
    reason: str
    address: Optional[str] = None
    address_basis: Optional[str] = None


_LOCATION_LABEL = re.compile(r"^(?:event\s+)?(?:location|address|where|place|venue)\s*(?::\s*|$)", re.IGNORECASE)
_TOWN_LINE = re.compile(r"[A-Z][A-Za-z .'’-]{1,40},\s*(?:[A-Z]{2}|[A-Z][a-z]+)(?:,?\s+\d{5}(?:-\d{4})?)?")   # 'San Diego, CA 92126', 'Crossville, Tennessee'
_TOWN_TAIL = re.compile(r"\s*,\s*(" + _TOWN_LINE.pattern + r"|[A-Z][A-Za-z .'’-]{1,40})\s*$")              # ', San Diego' after a street
_COUNTRY = re.compile(r"(?:,\s*(?:United States(?: of America)?|USA|US))+\s*$", re.IGNORECASE)
_TITLE_SEPARATOR = re.compile(r"\s+[|\-–—·:]\s+")
_SHORT = 120                  # a piece of text that names the venue and nothing much else
_HEADING_SHARE = 0.6          # of the title's distinctive words a page's own title must carry: a festival's page does not name each of its events
STRUCTURED, LABELLED, BESIDE_VENUE, VENUE_SITE = "the event's structured location", "the page's labelled event location", \
    "the page's text beside the venue's name", "the venue's own website"


def _text(value, limit=400):
    if isinstance(value, dict):
        value = value.get("@value") or value.get("name")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", value)).strip()[:limit] if isinstance(value, str) else ""


def _documents(html):
    parts = PageParts()
    try:
        parts.feed(html[:1_500_000])
    except (ValueError, TypeError, AssertionError):
        return []
    found = []
    for script in parts.scripts:
        try:
            found.extend(event_documents(json.loads(script), EVENT_KINDS))
        except (ValueError, TypeError, RecursionError):
            continue
    return found


def _covers(document, day):
    """Do the structured start and end dates (as written, in the event's own time) include the day?"""
    try:
        first = date.fromisoformat(str(document.get("startDate") or "")[:10])
        last = date.fromisoformat(str(document.get("endDate") or "")[:10]) if document.get("endDate") else first
    except ValueError:
        return False
    return first <= day <= last and (last - first).days <= _LONGEST_RUN_DAYS


def _location(document):
    """(name, PostalAddress mapping or address text) of a structured event's location."""
    location = document.get("location")
    if isinstance(location, list):
        location = next((item for item in location if isinstance(item, (dict, str))), None)
    if isinstance(location, dict):
        return _text(location.get("name"), 200), location.get("address")
    return _text(location, 200), None


def _address_key(line):
    """The house number and first street word: '9990 AleSmith Court' and '9990 AleSmith Ct, San Diego' are one address."""
    words = _WORD.findall(line.lower())
    return tuple(words[:2])


def _tidy(line):
    line = _COUNTRY.sub("", re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", line)).strip(" ,;|·"))
    return line[:300] or None


def _structured_address(address):
    """The street address a structured location gives, as it writes it: its street line (whole, when that already carries the town) and then its
    town, region and postal code. None without a street line."""
    if isinstance(address, str):
        return _tidy(address) if STREET_LINE.match(address.strip()) else None
    if not isinstance(address, dict):
        return None
    street = _text(address.get("streetAddress"), 200)
    if not STREET_LINE.match(street):
        return None
    if "," in street:
        return _tidy(street)
    region = " ".join(filter(None, (_text(address.get("addressRegion"), 40), _text(address.get("postalCode"), 12))))
    return _tidy(", ".join(filter(None, (street, _text(address.get("addressLocality"), 80), region))))


def _street_in(pieces, index):
    """The street address that starts in pieces[index] (with the town line the next piece writes, when this one has none), and the text before it."""
    text = pieces[index]
    match = STREET_LINE.search(text)
    if not match:
        return None, ""
    tail = _TOWN_TAIL.match(re.split(r"\s[|·;(]\s?|\s{2,}", text[match.end():])[0])
    line = match.group(0) + (", " + tail.group(1) if tail else "")
    if not tail:
        following = next((p for p in pieces[index + 1:index + 3] if re.search(r"[A-Za-z0-9]", p)), "")
        if _TOWN_LINE.fullmatch(following.strip()):
            line += ", " + following.strip()
    return _tidy(line), text[:match.start()]


def _beside_venue(event, pieces, index, prefix):
    """How the text ties the street address in pieces[index] to the venue: LABELLED (a location label, then the venue's name, then the address),
    BESIDE_VENUE (the venue's name just before it), or None."""
    labelled = bool(_LOCATION_LABEL.match(prefix.strip()))
    rest = _LOCATION_LABEL.sub("", prefix.strip())
    venue_near = bool(rest) and len(rest) <= _SHORT and event.names_venue(rest)
    if re.search(r"[A-Za-z0-9]", rest) and not venue_near:
        return None                                          # 'Parking at the garage, 1200 Third Ave': the address of what its own line names
    for back in range(index - 1, max(-1, index - 5), -1):
        piece = pieces[back].strip()
        if not re.search(r"[A-Za-z0-9]", piece):
            continue                                         # a separator ('·', '|')
        if _LOCATION_LABEL.match(piece):
            named = _LOCATION_LABEL.sub("", piece)
            venue_near = venue_near or (bool(named) and len(named) <= _SHORT and event.names_venue(named))
            labelled = True
            break
        if not venue_near and len(piece) <= _SHORT and event.names_venue(piece):
            venue_near = True
            continue                                         # a label may stand just before the venue's name
        break
    if not venue_near:
        return None
    return LABELLED if labelled else BESIDE_VENUE


def _spells(domain, words):
    """Does the domain name spell the venue's name, each word whole or by its initial, in order ('ccplayhouse': Cumberland County Playhouse,
    'batesnutfarm': Bates Nut Farm)? A leading 'the' may be left out."""
    if words and words[0] == "the":
        return _spells(domain, words[1:]) or (domain.startswith("the") and _spells(domain[3:], words[1:]))
    if not words:
        return not domain
    word = words[0]
    return any(domain.startswith(part) and _spells(domain[len(part):], words[1:]) for part in {word, word[0]})


def _venue_site(event, reader, host):
    """Is the website demonstrably the venue's own: its name (og:site_name, else the last part of its <title>) names the venue, every distinctive
    word of it, and its domain spells the venue's name?"""
    name = reader.site_name or (_TITLE_SEPARATOR.split(reader.title)[-1] if _TITLE_SEPARATOR.search(reader.title) else "")
    words = _WORD.findall(name.lower())
    wanted = event.specific or event.venue_words
    if not wanted or not all(_has(words, w) for w in wanted):
        return False
    labels = [label for label in (host or "").lower().split(".") if label and label != "www"]
    domain = re.sub(r"[^a-z0-9]", "", labels[-2]) if len(labels) >= 2 else ""
    venue = [w for w in _WORD.findall(event.venue.lower()) if w not in event.city_words]
    return bool(domain) and _spells(domain, venue)


_CONTACT_LABEL = re.compile(r"\b(?:mailing|billing|office|offices|headquarters|corporate|p\.?\s?o\.?\s+box|box office|send|remit|payments?)\b", re.IGNORECASE)


def _footer_address(event, pieces):
    """The one street address the venue's own website writes in its footer for its location: under a location label ('LOCATION', 'Address',
    'Visit us', 'Find us', 'Directions') or right after the venue's name, with the event's town written out; or None (none, several, a mailing, office
    or billing address, or another town)."""
    found = set()
    for index in range(len(pieces)):
        line, prefix = _street_in(pieces, index)
        if not line or _CONTACT_LABEL.search(prefix):
            continue
        before = [p.strip() for p in pieces[max(0, index - 3):index] if re.search(r"[A-Za-z0-9]", p)][-2:]
        if any(_CONTACT_LABEL.search(p) for p in before):
            continue                                         # 'Mailing address', 'Box office', 'Office': not where the venue is
        tied = bool(_LOCATION_LABEL.match(prefix.strip())) or any(_LOCATION_LABEL.match(p) or _VISIT_LABEL.match(p) or event.names_venue(p)
                                                                  for p in before)
        if tied:
            found.add(line)
    if len({_address_key(line) for line in found}) != 1:
        return None
    line = max(found, key=len)
    town = set(_WORD.findall(line.lower()))
    return line if event.city_words and event.city_words <= town and not _names_another_town(event, line) else None


_VISIT_LABEL = re.compile(r"^(?:visit|find)\s+us\b|^directions\b|^getting\s+here\b", re.IGNORECASE)


def _page_address(event, reader, structured, host=None):
    """(address, basis) for the venue as the confirmed page states it, or (None, None) when it states none, or more than one, for the venue."""
    found = []                                               # (rank, address)
    if structured is not None:
        name, postal = _location(structured)
        line = _structured_address(postal)
        if line and (not name or event.placed(name)):
            found.append((0, line, STRUCTURED))
    body = [text for text, aside in reader.pieces if not aside]
    for index in range(len(body)):
        line, prefix = _street_in(body, index)
        if line:
            basis = _beside_venue(event, body, index, prefix)
            if basis:
                found.append((1 if basis == LABELLED else 2, line, basis))
    if not found and _venue_site(event, reader, host):
        line = _footer_address(event, [text for text, is_aside in reader.pieces if is_aside])
        if line:
            found.append((3, line, VENUE_SITE))
    found = [(rank, line, basis) for rank, line, basis in found if not event.other_town(line) and not _names_another_town(event, line)]
    if not found or len({_address_key(line) for _, line, _ in found}) > 1:
        return None, None                                    # none, or the venue given more than one address: which is right is not known
    best = min(rank for rank, _, _ in found)
    basis = next(basis for rank, _, basis in found if rank == best)
    return max((line for _, line, _ in found), key=len), basis   # the fullest writing of the one address


def _names_another_town(event, line):
    """Does the address's town part ('…, El Cajon, CA 92019') name a town other than the event's?"""
    parts = [part.strip() for part in line.split(",")[1:]]
    town = next((part for part in parts if re.search(r"[a-z]", part) and not re.fullmatch(r"[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?", part)), None)
    return bool(town and event.city_words) and not event.city_words <= set(_WORD.findall(town.lower()))


def _labelled_locations(pieces):
    """The place names a page writes under a location label ('Location: Bates Nut Farm', 'Place | JULEP Venue'), street addresses aside."""
    found = []
    for index, piece in enumerate(pieces):
        match = _LOCATION_LABEL.match(piece.strip())
        if not match or piece.strip().lower().startswith("address"):
            continue
        named = piece.strip()[match.end():] or next((p for p in pieces[index + 1:index + 3] if re.search(r"[A-Za-z0-9]", p)), "")
        if named and len(named) <= _SHORT and not STREET_LINE.match(named):
            found.append(named)
    return found


def check_page(html, title, day, venue, city, url=None):
    """Is this read page the event's own page? It must name the event in its title (its <title>, an <h1> or og:title) and give the event's day and
    venue: through a structured event (JSON-LD) that names the event, or else through its text. Nothing on it may give the event another day, year,
    venue or town. A confirmed page also gives the venue's street address when it states one for the venue (see `_page_address`); `url` is where the
    page was read, so a website's own footer address can be taken only on the venue's own domain."""
    event = _Event(title, day, venue, city)
    reader = page_text(html if isinstance(html, str) else "")
    heading = " | ".join([reader.title] + reader.headings)
    if not event.named(heading, _HEADING_SHARE):
        return PageCheck(False, "its title does not name the event")
    named = [document for document in _documents(html) if event.named(_text(document.get("name"), 300))]
    structured = None
    if named:
        covering = [document for document in named if _covers(document, day)]
        if not covering:
            return PageCheck(False, "its structured data gives the event another day")
        structured = covering[0]
        name, postal = _location(structured)
        where = " ".join(filter(None, (name, postal if isinstance(postal, str) else " ".join(_text(v, 200) for v in (postal or {}).values()))))
        if where and not event.placed(where):
            return PageCheck(False, "its structured data places the event at another venue")
        town = _text(postal.get("addressLocality"), 80) if isinstance(postal, dict) else ""
        if town and event.city_words and not event.city_words <= set(_WORD.findall(town.lower())):
            return PageCheck(False, "its structured data places the event in another town")
    else:
        pieces = [text for text, aside in reader.pieces if not aside]
        body = " | ".join(pieces)
        if not event.names_venue(heading + " " + body):
            return PageCheck(False, "the page does not show the venue")
        locations = _labelled_locations(pieces)
        if locations and not any(event.placed(location) for location in locations):
            return PageCheck(False, "its labelled location is another venue")
        stated, dated, _ = event.dates(heading)
        if stated and not dated:
            return PageCheck(False, "its title gives another day")
        stated, dated, other_years = event.dates(heading + " | " + body)
        if not dated:
            return PageCheck(False, "the page does not show the event's day")
        if other_years:
            return PageCheck(False, "the days the page shows are another year's")
    address, basis = _page_address(event, reader, structured, urlsplit(url).hostname if isinstance(url, str) else None)
    return PageCheck(True, "confirmed", address, basis)


@dataclass(frozen=True)
class EventLink:
    """The event's own page, confirmed by reading it, and the venue's street address it states (with what on the page gave it), when it states one."""
    url: str
    address: Optional[str] = None
    address_basis: Optional[str] = None


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
    """See the module's documentation. `cache` is Persistence's EventLinkCache; `fetch` is the shared safe fetch client (SafeFetcher: `fetch(url)` gives
    a response with `.url` and `.text`), through which proposed pages are read. Safe to share between producers and threads; a lookup never raises.
    `status` says what the last lookup did, for a run's notes."""

    def __init__(self, cache, api_key=None, clock=time.time, post=https_post, sleep=time.sleep, daily_limit=DAILY_LIMIT, monthly_limit=MONTHLY_LIMIT,
                 fetch=None):
        key = api_key if api_key is not None else os.environ.get(KEY_VARIABLE)
        self._key = key.strip() if isinstance(key, str) and key.strip() else None
        self._cache, self._clock, self._post, self._sleep, self._fetch = cache, clock, post, sleep, fetch
        self._daily, self._monthly = daily_limit, monthly_limit
        # the lock guards only this bookkeeping (never a search or a page read): pause, request pacing, the daily tidy and the lookups in progress
        self._lock, self._paused_until, self._next_request, self._tidied = threading.Lock(), 0.0, float("-inf"), None
        self._looking = {}       # lookup key -> threading.Event set when that lookup ends
        self.status = "ready" if self._key and self._fetch is not None else "unconfigured"   # without a page reader nothing could be confirmed

    def find_event_link(self, title, date, venue, city):
        """The event's own page, confirmed by reading it, with the venue's street address when the page states one; or None (no confirmed page, no key,
        a limit reached or the provider unavailable)."""
        try:
            return self._find(_clean(title, 300), _day(date), _clean(venue), _clean(city))
        except Exception:   # a lookup never stops a producer
            self.status = "failed"
            return None

    def find_event_url(self, title, date, venue, city):
        """The url of `find_event_link`, or None."""
        link = self.find_event_link(title, date, venue, city)
        return link.url if link else None

    # --- internals ---------------------------------------------------------------------------------

    def _find(self, title, day, venue, city):
        if not self._key or self._fetch is None or not title or day is None:
            return None
        key = hashlib.sha256("\x1f".join((title.lower(), day.isoformat(), venue.lower(), city.lower())).encode("utf-8")).hexdigest()
        while True:   # one lookup of an event at a time: a second waits for the first, then reads its answer from the cache
            with self._lock:
                running = self._looking.get(key)
                if running is None:
                    self._looking[key] = threading.Event()
                    break
            if not running.wait(LOOKUP_WAIT_S):
                self.status = "another lookup of this event is still running"
                return None
        try:
            return self._lookup(key, title, day, venue, city)
        finally:
            with self._lock:
                self._looking.pop(key).set()

    def _lookup(self, key, title, day, venue, city):
        now = self._clock()
        today = datetime.fromtimestamp(now, timezone.utc).date()
        with self._lock:
            tidy, self._tidied = self._tidied != today, today
        if tidy:   # once a day: lookups for long-past events and old counts go
            self._cache.forget_before((today - timedelta(days=KEEP_PAST_DAYS)).isoformat())
        known = self._cache.recall(key)
        if known and known[0]:
            self.status = "cached"
            return EventLink(*known[:3])
        if known and known[4]:   # pages that could not be read: read again once REREAD_S has passed, without searching again
            if now - known[3] < REREAD_S:
                self.status = "pages unreadable; reading them again later"
                return None
            return self._settle(key, day, now, *self._read(known[4], title, day, venue, city), rereads=known[5] + 1)
        if known and now - known[3] < MISS_TTL_S:
            self.status = "cached"
            return None
        with self._lock:
            paused = now < self._paused_until
        if paused:
            return None
        periods = {today.isoformat(): self._daily, today.isoformat()[:7]: self._monthly}
        if not self._cache.reserve_request(periods):   # counted before asking (a request that fails may still cost a credit), atomically
            self.status = "limit reached"
            return None
        with self._lock:   # a slot MIN_INTERVAL_S after the previous request; the wait itself is outside the lock
            start = max(self._clock(), self._next_request)
            self._next_request = start + MIN_INTERVAL_S
        self._sleep(max(0.0, start - self._clock()))
        results = self._search(title, day, venue, city, today.isoformat()[:7])
        if results is None:
            return None
        ranked = [(url, kind) for (_, kind, _), url in nominate(results, title, day, venue, city)[:MAX_PAGES]]
        return self._settle(key, day, now, *self._read(ranked, title, day, venue, city), rereads=0)

    def _settle(self, key, day, now, link, unread, rereads):
        """Remember a lookup's answer: the page found; pages to read again later (while MAX_REREADS allows); or nothing credible."""
        if link is None and unread and rereads >= MAX_REREADS:
            unread = ()                                      # read again often enough: nothing credible for MISS_TTL_S
        self._cache.remember(key, link.url if link else None, link.address if link else None, link.address_basis if link else None, int(now),
                             day.isoformat(), pending=() if link else unread, rereads=rereads if unread and not link else 0)
        self.status = "searched" if not rereads else "pages read again"
        if link is None and unread:
            self.status += "; pages unreadable for now"
        return link

    def _read(self, ranked, title, day, venue, city):
        """(the best proposed page that reading it confirms, or None; the proposed pages that could not be read for a passing reason). At most
        MAX_PAGES are read, in ranking order, and reading stops at the first confirmed page of the event's own (not a social post or a resale
        listing). A page that cannot be read is passed over, never taken on its snippet."""
        place = " ".join(filter(None, (venue, city)))
        confirmed, unread = [], []
        for order, (url, kind) in enumerate(ranked[:MAX_PAGES]):
            try:
                response = self._fetch.fetch(url)
            except Exception as error:   # unreachable, refused by the site or its robots.txt, too large, not a page: not confirmed
                if _passing(error):
                    unread.append((url, kind))
                continue
            try:
                check = check_page(getattr(response, "text", "") or "", title, day, venue, city, getattr(response, "url", None) or url)
            except Exception:
                continue
            if not check.confirmed:
                continue
            final = event_page(getattr(response, "url", None))
            if final and final != url and names_event(final, title, place):
                url = final                                  # where the page now is ('/icons-legends' -> '/icons-legends/')
            confirmed.append((kind, -order, EventLink(url, check.address, check.address_basis)))
            if kind == 2:
                break
        return (max(confirmed, key=lambda item: item[:2])[2] if confirmed else None), tuple(unread)

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
        with self._lock:
            self._paused_until = max(self._paused_until, self._clock() + seconds)
        self.status = status
        return None


# What a page read that failed says: a passing reason (worth reading again later) or a lasting one (robots.txt, a refusal, not a page, too large)
_LASTING = re.compile(r"HTTP 4\d\d|too large|not a text document|unexpected content type|too many redirects|redirect without|content encoding|"
                      r"decompressed|not a public website|robots\.txt|refused access|user name or password|port \d+ is not read|only http", re.IGNORECASE)


def _passing(error):
    """Is a failed page read worth trying again later: the network, a timeout, a server error, or the site asking us to slow down (HTTP 429/503)?"""
    text = str(error)
    if isinstance(error, BlockedError):
        return "slow down" in text or "HTTP 429" in text
    if isinstance(error, FetchError):
        return not _LASTING.search(text)
    return isinstance(error, (OSError, TimeoutError))
