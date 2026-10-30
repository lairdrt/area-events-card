"""Generic, bounded producer for user-configured public event websites.

One adapter instance handles a list of independent seed URLs. It reads public HTML
JSON-LD Events, calendar (.ics) feeds and links to relevant same-host event pages.
Unstructured prose, publication dates, vague times and dates without a stated UTC
offset/time zone are never invented as events. Such pages remain SourceLeads.

The injected RecordingFetcher supplies all network operations, SSRF protection,
robots rules, shared host pacing, request/time limits, and durable page evidence.
The existing scheduler, normalizer, deduplication, and persistence own everything
downstream. This producer has no database, background jobs, or HA dependencies.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone, timedelta
from html import unescape
from html.parser import HTMLParser
import calendar
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...image_extractor.association import associate, wanted_for_page
from ...image_extractor.responsive import measurable
from ...image_extractor.v1 import ImageRequest
from ..producer import EventCandidate, ProducerResult, Provenance, SourceLead, stated
from ..safe import FetchError
from ..urls import canonical_url

MAX_SEEDS = 12
MAX_PAGES = 24
MAX_EVENTS = 150
MAX_DEPTH = 2
MAX_LINKS = 18
EVENT_WORDS = re.compile(r"(event|calendar|what.?s.?on|festival|fair|concert|performance|meetup|workshop|activity|activities|exhibit|class|show|lecture|tour|seminar|schedule|ical|\.ics)(?:s\b|/|[-_ ]|$)", re.I)
JSONLD_TYPE = re.compile(r"(?:^|[/#])Event$", re.I)
ICS_START = re.compile(r"^DTSTART(?:;[^:]*)?:", re.I)
ISO_ICAL = re.compile(r"^\d{8}T\d{6}Z?$")
FOLD = re.compile(r"\r?\n[ \t]")
TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")


def _clean(value, limit=500):
    if isinstance(value, dict):
        value = value.get("@value") or value.get("value")
    if not isinstance(value, str):
        return None
    value = _SPACE.sub(" ", unescape(TAG.sub(" ", value))).strip()
    return value[:limit] or None


def _url(value):
    """Validate a public-web-shaped URL; the injected SafeFetcher verifies DNS/redirects."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("each website URL must be a nonempty address under 2048 characters")
    try:
        raw = urlsplit(value.strip())
        port = raw.port
        if (raw.scheme.lower() not in ("http", "https") or not raw.hostname
                or raw.username is not None or raw.password is not None
                or (port is not None and (port != 80 if raw.scheme.lower() == "http" else port != 443))):
            raise ValueError("website URLs must use public http(s) on their default port, without credentials")
        if raw.hostname.lower() in ("localhost",) or raw.hostname.lower().endswith((".local", ".internal")):
            raise ValueError("local-network websites are not supported")
        normalized = canonical_url(urlunsplit((raw.scheme, raw.netloc, raw.path or "/", raw.query, "")))
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"invalid website URL: {error}") from error
    return normalized


def _integer(value, name, default, low, high):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def _timezone(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        raise ValueError("timezone must be an IANA timezone name")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as error:
        raise ValueError("timezone must be an installed IANA timezone name") from error
    return value


def _timestamp(value, zone=None):
    """Only preserve exact instants from source claims, never guess an offset."""
    if isinstance(value, dict):
        value = value.get("@value") or value.get("value")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if "T" not in value and " " not in value:
        return None  # Date-only (all-day) is not a scheduled start time.
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            if not zone:
                return None
            tz = ZoneInfo(zone)
            first = parsed.replace(tzinfo=tz, fold=0)
            second = parsed.replace(tzinfo=tz, fold=1)
            # Ambiguous/nonexistent DST wall times cannot be made authoritative.
            if first.utcoffset() != second.utcoffset():
                return None
            parsed = first
        return parsed.isoformat()
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return None


def _ical_timestamp(value, zone=None):
    if not isinstance(value, str) or not ISO_ICAL.fullmatch(value):
        return None  # all-day and floating/unrecognized times are not guessed
    try:
        parsed = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if value.endswith("Z"):
        return parsed.replace(tzinfo=timezone.utc).isoformat()
    return _timestamp(parsed.isoformat(), zone)


def _ics_unescape(value):
    return value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _ics_events(page, page_url, name, retrieved_at):
    events, skipped = [], 0
    current = None
    for line in FOLD.sub("", page).splitlines():
        if line.strip().upper() == "BEGIN:VEVENT":
            current = {}
            continue
        if line.strip().upper() == "END:VEVENT":
            if current is not None:
                title = _clean(current.get("SUMMARY"), 200)
                start = _ical_timestamp(current.get("DTSTART"), current.get("DTSTART_TZID"))
                end = _ical_timestamp(current.get("DTEND"), current.get("DTEND_TZID"))
                if title and start:
                    ident = current.get("UID") or f"{page_url}|{title}|{start}"
                    local_id = hashlib.sha256(f"{ident}|{start}".encode("utf-8")).hexdigest()[:32]
                    description = _clean(current.get("DESCRIPTION"), 4000)
                    venue = _clean(current.get("LOCATION"), 200)
                    link = current.get("URL")
                    link = urljoin(page_url, link) if link and link.startswith("/") else link
                    if link:
                        try:
                            link = _url(link)
                        except ValueError:
                            link = None
                    events.append(EventCandidate(
                        local_id=local_id, title=title, starts_at=start, ends_at=end,
                        venue=venue, description=description, url=link or page_url,
                        provenance=Provenance(name, page_url, retrieved_at, (page_url,)),
                        verification=stated((f"Event time from VEVENT in {page_url}",),
                                            False, end, venue, None, description)))
                else:
                    skipped += 1
            current = None
            continue
        if current is None or ":" not in line:
            continue
        heading, value = line.split(":", 1)
        pieces = heading.split(";")
        key = pieces[0].upper()
        if key in ("SUMMARY", "DESCRIPTION", "LOCATION", "UID", "URL", "DTSTART", "DTEND"):
            current[key] = _ics_unescape(value.strip())
        if key in ("DTSTART", "DTEND"):
            for part in pieces[1:]:
                if part.upper().startswith("TZID="):
                    current[f"{key}_TZID"] = part[5:].strip('"')
    return events, skipped


def _schema_type(value):
    items = value if isinstance(value, list) else [value]
    return any(isinstance(v, str) and JSONLD_TYPE.search(v) for v in items)


def _event_documents(payload):
    """Find @type Event in bounded JSON-LD graphs and list elements."""
    stack = [payload]
    seen = 0
    while stack and seen < 1000:
        item = stack.pop()
        seen += 1
        if isinstance(item, list):
            stack.extend(reversed(item[:300]))
        elif isinstance(item, dict):
            if _schema_type(item.get("@type")):
                yield item
            # Don't scan arbitrary giant map values: these are JSON-LD graph edges.
            for key in ("@graph", "itemListElement", "mainEntity", "subjectOf", "event", "events"):
                if key in item:
                    stack.append(item[key])
            if item.get("@type") == "ListItem" and "item" in item:
                stack.append(item["item"])


def _field_url(value, page):
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, (str, dict))), None)
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or value.get("@id")
    if not isinstance(value, str):
        return None
    try:
        return _url(urljoin(page, value))
    except ValueError:
        return None


def _description(value):
    return _clean(value, 4000)


def _jsonld_events(script, page, name, retrieved_at):
    try:
        data = json.loads(script)
    except (ValueError, TypeError):
        return [], 1
    accepted = []
    skipped = 0
    for item in _event_documents(data):
        title = _clean(item.get("name"), 200)
        timezone_name = item.get("eventTimeZone") or item.get("timezone")
        if timezone_name:
            try:
                timezone_name = _timezone(timezone_name)
            except ValueError:
                timezone_name = None
        start = _timestamp(item.get("startDate"), timezone_name)
        if not title or not start:
            skipped += 1
            continue
        end = _timestamp(item.get("endDate"), timezone_name)
        location = item.get("location")
        if isinstance(location, list):
            location = next((v for v in location if isinstance(v, (dict, str))), None)
        venue, address, geo = None, None, None
        if isinstance(location, dict):
            venue = _clean(location.get("name"), 200)
            postal = location.get("address")
            address = _clean(postal, 300)
            if isinstance(postal, dict):
                address = _clean(", ".join(str(postal.get(k, "")) for k in
                    ("streetAddress", "addressLocality", "addressRegion", "postalCode") if postal.get(k)), 300)
            coordinates = location.get("geo")
            if isinstance(coordinates, dict):
                try:
                    lat, lon = float(coordinates["latitude"]), float(coordinates["longitude"])
                    if -90 <= lat <= 90 and -180 <= lon <= 180:
                        geo = {"latitude": lat, "longitude": lon, "source": "source JSON-LD geo"}
                except (TypeError, ValueError, KeyError):
                    pass
        else:
            venue = _clean(location, 200)
        url = _field_url(item.get("url") or item.get("@id"), page) or page
        image = _field_url(item.get("image"), page)
        description = _description(item.get("description"))
        organizer = item.get("organizer")
        if isinstance(organizer, dict):
            organizer = organizer.get("name")
        if isinstance(organizer, list):
            organizer = None
        organizer = _clean(organizer, 200)
        categories = item.get("keywords")
        if isinstance(categories, str):
            categories = tuple(v.strip() for v in categories.split(",") if v.strip())[:12]
        elif isinstance(categories, list):
            categories = tuple(v.strip() for v in categories if isinstance(v, str) and v.strip())[:12]
        else:
            categories = ()
        price = None
        offers = item.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            amount = offers.get("price")
            currency = offers.get("priceCurrency")
            if isinstance(amount, (int, float, str)) and str(amount).strip():
                price = f"{amount} {currency}" if currency else str(amount)
        ident = item.get("identifier") or item.get("@id") or url
        if not isinstance(ident, str):
            ident = url
        local_id = hashlib.sha256(f"{ident}|{start}".encode("utf-8")).hexdigest()[:32]
        accepted.append(EventCandidate(
            local_id=local_id, title=title, starts_at=start, ends_at=end, timezone=timezone_name,
            venue=venue, address=address, description=description, url=url,
            image_url=image, image_source="source JSON-LD image" if image else None,
            categories=categories, organizer=organizer, price=price, geo=geo,
            provenance=Provenance(name, page, retrieved_at, (page,)),
            verification=stated((f"Title and offset-aware start from JSON-LD Event at {page}",),
                                False, end, venue, address, description, image)))
    return accepted, skipped


class _LinksAndEvents(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.links = []
        self._script = None
        self._anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and "ld+json" in (attrs.get("type") or "").lower():
            self._script = []
        if tag == "a" and attrs.get("href"):
            self._anchor = [attrs["href"], []]
        if tag == "link" and attrs.get("href") and (
                attrs.get("rel") in ("next", "alternate") or "calendar" in (attrs.get("type") or "")):
            self.links.append((attrs["href"], attrs.get("rel") or "feed"))

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        if self._anchor is not None:
            self._anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None
        if tag == "a" and self._anchor is not None:
            self.links.append((self._anchor[0], _clean(" ".join(self._anchor[1]), 150) or ""))
            self._anchor = None


def _attach_page_image(events, html, page_url, fetcher, extractor):
    """Give a single-event page's imageless events the page's primary image, as found by the shared image_extractor/v1 engine.

    Source-supplied images (JSON-LD) are never replaced. Which events may carry the page's image is the shared association policy's decision.
    """
    titled = [(event.title, event.image_url) for event in events]
    if extractor is None or not wanted_for_page(titled) or len(html) > 1_500_000:
        return events
    try:
        result = extractor.extract(ImageRequest(html, page_url, tuple(event.title for event in events),
                                                measurable(getattr(fetcher, "fetch_image", None), html, page_url)))
    except Exception:   # an engine that breaks its contract costs this page its image and nothing else
        return events
    verdict = associate(result, titled, html, page_url)
    if not verdict.image_url:
        return events
    note = f"Page primary image ({result.engine}{', ' + result.method if result.method else ''}) at {page_url}"
    return [event if event.image_url else replace(
        event, image_url=verdict.image_url, image_source="source HTML event image",
        verification=stated(tuple(event.verification.evidence) + (note,), False, event.ends_at, event.venue,
                            event.address, event.description, verdict.image_url)) for event in events]


def _feed_links(page):
    """RSS/Atom entries are discovery leads, not events: pubDate is not event time."""
    try:
        root = ElementTree.fromstring(page)
    except ElementTree.ParseError:
        return []
    links = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1].lower() not in ("item", "entry"):
            continue
        title = ""
        link = None
        for child in item:
            tag = child.tag.rsplit("}", 1)[-1].lower()
            if tag == "title":
                title = _clean(child.text) or ""
            if tag == "link":
                link = child.attrib.get("href") or child.text
        if link:
            links.append((link, title))
        if len(links) >= MAX_LINKS:
            break
    return links



# Unstructured HTML support lives in THIS adapter. No site-specific selectors,
# second producer, remote AI call or alternative downstream processing contract.
_MONTH_NAMES = {
    name.casefold(): index for index, name in enumerate(calendar.month_name)
    if name
}
_MONTH_NAMES.update({
    name.casefold(): index for index, name in enumerate(calendar.month_abbr) if name
})
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_TEXT_DATE = re.compile(
    rf"\b(?P<month>{_MONTH})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    r"(?:\s*,?\s*(?P<year>20\d{2}))?\b", re.I)
_TEXT_DATES_NUMERIC = re.compile(r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>20\d{2})\b")
_TEXT_DATES_ISO = re.compile(r"\b(?P<year>20\d{2})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_YEAR = re.compile(r"\b20\d{2}\b")
_CLOCK = re.compile(
    r"(?<![0-9A-Za-z])(?P<hour>1[0-2]|0?[1-9])(?::(?P<minute>[0-5]\d))?"
    r"\s*(?P<meridiem>[ap])\s*\.?\s*m\.?(?![a-z])", re.I)
_LINK_WORDS = re.compile(r"\b(event|show|concert|workshop|festival|fair|lecture|market|meetup|class)\b", re.I)
_UNHELPFUL_HEADINGS = re.compile(
    r"^(?:dates?|times?|locations?|details?|events?|calendar|programs?|"
    r"schedule|overview|information|about|home|learn more|registration|"
    r"event calendar|upcoming events|events and programs|calendar of events|"
    r"news|news and updates|visitor center news|"
    r"buy tickets|vendors?|parking|contact|directions|hours|venue)$", re.I)
_JUNK_TITLES = re.compile(
    r"^(?:visitor center|park hours|opening hours|hours of operation|"
    r"contact us|event registration|related events)$", re.I)
_LOCATION = re.compile(r"^(?:location|venue|where|place)\s*:\s*(.*)$", re.I)
_ADDRESS_LABEL = re.compile(r"^(?:street\s+)?address\s*:\s*", re.I)
_DESCRIPTION_LABEL = re.compile(r"^description\s*:\s*(.*)$", re.I)
_FIELD_LINE = re.compile(r"^[A-Z][A-Za-z ]{1,24}:(?:\s|$)")
_CITY_LINE = re.compile(r"\b[A-Za-z][A-Za-z .]+,\s*[A-Z]{2}\b")
_DATE_LABEL = re.compile(r"^(?:dates?|when)\s*:\s*", re.I)
_TIME_LABEL = re.compile(r"^(?:times?|hours?)\s*:\s*", re.I)
_WEEKDAY = re.compile(r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", re.I)
# A field label in card text ("Date: Sat. Nov. 7", "Date & Time: Fri., Dec. 11") sits between the title and its date. It is a label only with explicit field evidence: the
# label word is followed by a colon, and what follows the colon is a date (a weekday, a month or a digit) or nothing. "Save the Date Festival", "Dinner Date at the Museum"
# and "Save the Date: Festival of Lights" carry no such evidence and stay whole.
_FIELD_LABEL = re.compile(
    r"\s+(?:dates?|when)(?:\s*(?:&|and)\s*times?)?\s*:\s*(?=$|(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b|\d).*$", re.I)
# A heading that names a field of the event above it ("Dates and Times", "Location", "Cost", "Description"): its section is that event's field, never an event of its own.
_FIELD_HEADING = re.compile(
    r"^(?:event\s+)?(?:dates?|times?|dates?\s*(?:&|and)\s*times?|when|where|location|venue|address|cost|costs|price|prices|pricing|admission|tickets?|fees?|"
    r"description|details|event details)\s*:?$", re.I)
# A date a card writes day first and without a year ("Fri 30 Oct").
_DAY_MONTH = re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTH})\b\.?(?!\s*\d)", re.I)
_WEEKDAY_WORDS = frozenset(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                            "mon", "tue", "tues", "wed", "weds", "thu", "thur", "thurs", "fri", "sat", "sun"))
_SCHEDULE_WORDS = _WEEKDAY_WORDS | frozenset(_MONTH_NAMES) | frozenset((
    "sept", "st", "nd", "rd", "th", "am", "pm", "noon", "midnight", "and", "to", "through", "thru", "until", "till", "from", "at",
    "date", "dates", "when", "time", "times", "hours"))


def _schedule_only(text):
    """Is this text nothing but scheduling: dates, weekdays, date ranges, times and their labels ("Saturday-Sunday December 5 & 6, 2026", "Date: Sat. Nov. 7", "Mar 14 10:00-11:00am")?
    Such text is never an event's title. A title that contains a date ("Summer Fair — December 5", "May Day Festival") keeps its other words and is not schedule-only."""
    words = re.findall(r"[a-z]+|\d+", _CLOCK.sub(" ", text or "").casefold())
    scheduled = (any(pattern.search(text or "") for pattern in (_CLOCK, _TEXT_DATES_ISO, _TEXT_DATES_NUMERIC))
                 or any(word in _WEEKDAY_WORDS or word in _MONTH_NAMES for word in words))
    return scheduled and all(word.isdigit() or word in _SCHEDULE_WORDS for word in words)


class _VisibleEventText(HTMLParser):
    """Turn arbitrary HTML into heading sections and self-contained listing cards."""
    BLOCKS = frozenset(("p", "br", "div", "li", "section", "article", "time",
                        "tr", "td", "th", "blockquote", "ul", "ol"))
    HEADINGS = frozenset(("h1", "h2", "h3", "h4", "h5"))
    SKIP = frozenset(("script", "style", "noscript", "svg", "template", "nav", "footer"))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections = []
        self.cards = []
        self._heading = ""
        self._heading_tag = None
        self._section = []
        self._ignored = []
        self._cards = []
        self.card_headings = []
        self.card_hrefs = []
        self._anchors = []
        self.first_heading = ""
        self._section_tag = None   # the heading that opened the current section
        self._flushed_tag = None   # the heading that opened the last stored section
        self.link_cards = []       # (href, lines) of links that wrap a whole card (<a class="card"><div>title</div><div>date</div></a>)
        self._link_cards = []      # open ones: [href, parts, wraps blocks]

    def _section_flush(self):
        value = _visible_lines("".join(self._section))
        self._flushed_tag = None
        if self._heading or value:
            self.sections.append((self._heading, value))
            self._flushed_tag = self._section_tag
        self._section = []

    def _dates_previous_heading(self, tag):
        """Is a schedule-only heading (<h5>Saturday-Sunday December 5 & 6, 2026</h5>) the date line of the heading right before it? Only when that heading is a genuine title of
        the same or a higher rank and nothing but its own text stands between the two."""
        if not self.sections or not self._flushed_tag:
            return False
        title, lines = self.sections[-1]
        return (bool(title) and not _schedule_only(title) and int(tag[1]) >= int(self._flushed_tag[1])
                and _title_key(" ".join(lines)) == _title_key(title))

    def _field_of_previous(self, tag):
        """Is a field-label heading (<h2>Dates and Times</h2>, <h2>Location</h2>) a field of the event titled by the heading before it? Only when that heading is a genuine title
        (not itself a label, a date or a generic word) of the same or a higher rank."""
        if not self.sections or not self._flushed_tag:
            return False
        title = self.sections[-1][0]
        return (bool(title) and not _schedule_only(title) and not _FIELD_HEADING.fullmatch(title) and not _UNHELPFUL_HEADINGS.fullmatch(title)
                and not _JUNK_TITLES.fullmatch(title) and int(tag[1]) >= int(self._flushed_tag[1]))

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._ignored.append(tag)
        if self._ignored:
            return
        if tag in self.HEADINGS or tag in self.BLOCKS:
            for card in self._link_cards:
                card[1].append("\n")
                card[2] = True
        if tag in self.HEADINGS:
            self._section_flush()
            self._heading_tag = self._section_tag = tag
            self._heading = ""
            self._section.append("\n")
        elif tag in self.BLOCKS:
            self._section.append("\n")
        if tag in ("li", "article", "tr"):
            self._cards.append([tag, [], "", []])
        if tag == "a":
            self._anchors.append([dict(attrs).get("href") or "", []])
            self._link_cards.append([dict(attrs).get("href") or "", [], False])
        if tag in ("td", "th") and self._cards and self._cards[-1][0] == "tr":
            self._cards[-1][1].append(" — ")

    def handle_data(self, data):
        if self._ignored:
            return
        if self._heading_tag:
            self._heading += data
        self._section.append(data)
        for anchor in self._anchors:
            anchor[1].append(data)
        for card in self._cards:
            card[1].append(data)
        for card in self._link_cards:
            card[1].append(data)

    def handle_endtag(self, tag):
        if self._ignored:
            if tag == self._ignored[-1]:
                self._ignored.pop()
            return
        if tag in self.HEADINGS or tag in self.BLOCKS:
            for card in self._link_cards:
                card[1].append("\n")
        if tag == "a" and self._anchors:
            href, text = self._anchors.pop()
            for card in self._cards:
                card[3].append((href, " ".join(text)))
        if tag == "a" and self._link_cards:
            href, parts, wraps_blocks = self._link_cards.pop()
            if wraps_blocks and href:
                self.link_cards.append((href, _visible_lines("".join(parts))))
        if tag in self.HEADINGS and tag == self._heading_tag:
            self._heading = _clean(self._heading, 160) or ""
            label = False
            if self._heading and _schedule_only(self._heading) and self._dates_previous_heading(tag):
                # The date belongs to the title above it: it stays in that title's section as a line, and the title stays the heading.
                title, lines = self.sections.pop()
                self._heading, self._section_tag = title, self._flushed_tag
                self._section[:0] = ["\n".join(lines), "\n"]
            elif self._heading and _FIELD_HEADING.fullmatch(self._heading) and self._field_of_previous(tag):
                # A field of the event above ("Dates and Times", "Location"): its text stays in that event's section under a labelled line ("Location:").
                title, lines = self.sections.pop()
                self._section = ["\n".join(lines), "\n", self._heading.rstrip(" :") + ":", "\n"]
                self._heading, self._section_tag, label = title, self._flushed_tag, True
            if self._heading and not self.first_heading:
                self.first_heading = self._heading
            if not label:
                for card in self._cards:
                    card[2] = card[2] or self._heading
            self._heading_tag = None
            self._section.append("\n")
        elif tag in self.BLOCKS:
            self._section.append("\n")
        if tag in ("li", "article", "tr") and self._cards and self._cards[-1][0] == tag:
            card = self._cards.pop()
            lines = _visible_lines(" ".join(card[1]))
            if lines:
                self.cards.append(lines)
                self.card_headings.append(card[2])
                self.card_hrefs.append(_event_link(card[3], card[2]))

    def finish(self):
        self._section_flush()
        return self.sections, self.cards, self.first_heading


def _listing_title(text):
    """The title in the text that precedes a card's date, without a scraped field label (and the partial date after it) such as "Date: Sat." or "Date & Time:"."""
    return _FIELD_LABEL.sub("", text).strip(" ,-–—|:")


def _visible_lines(text):
    return [_SPACE.sub(" ", unescape(line)).strip()[:500]
            for line in text.splitlines() if line.strip()][:150]


def _text_dates(line, title):
    """Read explicitly dated occurrences; share an explicitly supplied year, never guess."""
    found = []
    matches = list(_TEXT_DATE.finditer(line[:350]))
    years = {int(match.group("year")) for match in matches if match.group("year")}
    if not years:
        years = {int(year) for year in _YEAR.findall((line + " " + title)[:350])}
    # For dates with a missing year, accept one unambiguous year in this group.
    shared_year = next(iter(years)) if len(years) == 1 else None
    for match in matches:
        month_name = match.group("month").rstrip(".").casefold()
        month = _MONTH_NAMES.get(month_name)
        if not month:
            # abbreviated forms that intentionally omit a period are allowed.
            month = _MONTH_NAMES.get(month_name[:3])
        year = int(match.group("year")) if match.group("year") else shared_year
        if not year:
            continue
        try:
            start = datetime(year, month, int(match.group("day")))
            found.append(start.date())
        except (TypeError, ValueError):
            continue
        # Common prose: November 14–15, 2026; November 14 and 15, 2026.
        between = line[match.end():match.end()+32]
        remaining = re.match(r"\s*(?:-|–|—|&|and|to)\s*(?P<day>\d{1,2})(?:st|nd|rd|th)?\b", between, re.I)
        if remaining:
            try:
                other = datetime(year, month, int(remaining.group("day"))).date()
                if 0 < (other-start.date()).days <= 14:
                    found.append(other)
            except ValueError:
                pass
    for pattern in (_TEXT_DATES_ISO, _TEXT_DATES_NUMERIC):
        for m in pattern.finditer(line[:350]):
            try:
                found.append(datetime(int(m.group("year")), int(m.group("month")), int(m.group("day"))).date())
            except ValueError:
                pass
    return list(dict.fromkeys(found))


def _text_clock(lines):
    """Choose a bounded date-adjacent stated event clock, not a general website hour."""
    for line in lines[:18]:
        entries = list(_CLOCK.finditer(line[:180]))
        if not entries:
            continue
        if any(word in line.casefold() for word in ("open daily", "visitor center hours", "business hours", "office hours")):
            continue
        start = entries[0]
        finish = entries[1] if len(entries) > 1 and entries[1].start() - start.end() < 42 else None
        if finish and not re.search(r"(?:-|–|—|to|until|through)", line[start.end():finish.start()], re.I):
            finish = None
        def at(match):
            hour = int(match.group("hour")) % 12
            if match.group("meridiem").lower() == "p":
                hour += 12
            return hour, int(match.group("minute") or 0)
        return at(start), at(finish) if finish else None, line
    return None, None, None


def _local_instant(day, clock, zone):
    if not zone:
        return None
    return _timestamp(f"{day.isoformat()}T{clock[0]:02d}:{clock[1]:02d}:00", zone)


_LEARN_LINK = re.compile(r"\b(?:learn more|more info(?:rmation)?|details?|read more|view event|event page|see event)\b", re.I)
_NOT_THE_EVENT = re.compile(r"(?:register|registration|buy|ticket|rsvp|cart|checkout|log ?in|sign ?(?:in|up)|share|facebook|twitter|instagram|youtube|map|directions|calendar feed|subscribe)", re.I)
_STOPWORDS = frozenset(("the", "a", "an", "at", "on", "of", "and", "in", "for", "to", "with", "s"))


def _event_link(links, heading):
    """The link of a listing card that leads to ITS event: the one around the event's name, else a "learn more" style link, else the first link that is not a registration, ticket,
    share or social link. Links that go nowhere (fragments, mail, scripts) are never chosen."""
    usable = [(href, text) for href, text in links if href and not href.startswith(("#", "mailto:", "tel:", "javascript:"))]
    name = _title_key(heading or "")
    for href, text in usable:
        label = _title_key(text)
        if name and label and (label == name or (len(label) >= 5 and (label in name or name in label))):
            return href
    for href, text in usable:
        if _LEARN_LINK.search(text or ""):
            return href
    for href, text in usable:
        if not _NOT_THE_EVENT.search((text or "") + " " + href):
            return href
    return ""


def _instant(iso):
    try:
        value = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo is not None else None


def _same_instant(left, right):
    """Equal as points in time: "2026-11-07T09:00:00-08:00" and "2026-11-07T17:00:00+00:00" are the same instant."""
    a, b = _instant(left), _instant(right)
    return a == b if a is not None and b is not None else bool(left) and left == right


def _same_day(left, right):
    a, b = _instant(left), _instant(right)
    return a.date() == b.astimezone(a.tzinfo).date() if a is not None and b is not None else False


_NOISE_WORDS = frozenset(("annual", "official"))
_GENERIC_WORDS = frozenset(("craft", "crafts", "fair", "fairs", "concert", "concerts", "festival", "festivals", "show", "shows", "market", "markets", "marketplace", "event", "events",
                            "night", "day", "music", "live", "holiday", "sale", "party", "class", "classes", "tour", "walk", "run", "meeting", "open", "free", "family", "community"))


def _title_tokens(title):
    return {token for token in _title_key(title).split()
            if token not in _STOPWORDS and token not in _NOISE_WORDS and not re.fullmatch(r"(?:19|20)\d\d|\d{1,2}(?:st|nd|rd|th)", token)}


def _related(left, right):
    """Does an event page's title name the same event as a listing's? Only on strong evidence: the same words once case, punctuation, articles, years and "annual" are set aside,
    or one title that extends the other by further words when the shorter one has at least two words including one that is not generic ("Craft Fair" never extends "Holiday Craft
    Fair"; "Christmas on the Farm" does extend into "Christmas on the Farm at the Park"). Shared generic words alone, however many, never relate two titles."""
    a, b = _title_tokens(left), _title_tokens(right)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter = a if len(a) <= len(b) else b
    return (a <= b or b <= a) and len(shorter) >= 2 and bool(shorter - _GENERIC_WORDS)


def _share_a_distinctive_word(left, right):
    """Do two titles share a word that is not generic? Not evidence of the same event on its own; only with a card's own link to a page whose one event is at that exact time."""
    return bool((_title_tokens(left) & _title_tokens(right)) - _GENERIC_WORDS)


def _line_groups(line, title):
    """Split a date line into its occurrences: each date (or consecutive dates of a range) with the first clock that FOLLOWS it in the same line. [(dates, clock-or-None)]."""
    starts = sorted({m.start() for pattern in (_TEXT_DATE, _TEXT_DATES_ISO, _TEXT_DATES_NUMERIC) for m in pattern.finditer(line[:350])})
    if not starts:
        return []
    bounds = starts + [len(line)]
    groups, pending = [], []
    for index in range(len(starts)):
        segment = line[0 if index == 0 else starts[index]:bounds[index + 1]]
        pending.append(segment)
        clock = _text_clock([segment])
        if clock[0]:
            groups.append((_text_dates(" ".join(pending), title), clock))
            pending = []
    if pending:
        groups.append((_text_dates(" ".join(pending), title), (None, None, None)))
    return [(dates, clock) for dates, clock in groups if dates]


def _has_date(text):
    return bool(_TEXT_DATE.search(text) or _TEXT_DATES_ISO.search(text) or _TEXT_DATES_NUMERIC.search(text))


def _place_name(text):
    """Can a line on its own name a place: short, no field label, no date or time, not a street address?"""
    return (0 < len(text) <= 120 and len(text.split()) <= 12 and not _FIELD_LINE.match(text) and not _has_date(text) and not _CLOCK.search(text)
            and not re.match(r"\d{1,6}\s+[A-Za-z]", text))


def _labelled_facts(lines):
    """The place and description a page states in labelled fields (Location/Venue/Where, Address, Description): (venue, address, description) with None for what it omits."""
    venue = address = description = None
    for index, line in enumerate(lines[:35]):
        match = _LOCATION.match(line)
        if match:
            value, following = match.group(1), lines[index+1:index+3]
            if not value and following and _place_name(following[0]):   # "Location:" on its own line (a heading made a label): the place is the next line
                value, following = following[0], lines[index+2:index+4]
            venue = _clean(value, 160)
            # Only use a nearby explicit street/city line, never a footer address, and stop at the next field or date.
            following = following[:next((i for i, v in enumerate(following) if _FIELD_LINE.match(v) or _has_date(v)), len(following))]
            address_lines = [_ADDRESS_LABEL.sub("", v) for v in following if re.search(r"\b\d{1,5}\s+[A-Za-z]|\b[A-Za-z][A-Za-z ]+,\s*[A-Z]{2}\b", v)]
            address = _clean(", ".join(address_lines), 250) if address_lines else None
            break
    if address is None:   # a labelled "Address:" field stands on its own (its city line may follow)
        for index, line in enumerate(lines[:35]):
            if _ADDRESS_LABEL.match(line):
                parts = [_ADDRESS_LABEL.sub("", line)]
                if index + 1 < len(lines) and _CITY_LINE.search(lines[index + 1]) and not _FIELD_LINE.match(lines[index + 1]):
                    parts.append(lines[index + 1])
                address = _clean(", ".join(part for part in parts if part), 250) or None
                break
    for index, line in enumerate(lines[:35]):   # a labelled "Description:" field is the description, whatever times it mentions
        match = _DESCRIPTION_LABEL.match(line)
        if match:
            parts = [match.group(1)] if match.group(1) else []
            for extra in lines[index + 1:index + 4]:
                # a line of under four words after the field ("Share", "Link copied!") is page furniture, not more description: the description ends there
                if _FIELD_LINE.match(extra) or len(extra.split()) < 4 or sum(len(part) for part in parts) >= 300:
                    break
                parts.append(extra)
            description = " ".join(parts) or None
            break
    return venue, address, description


# A street address as a contact block writes it: house number and street, then "City, ST 12345" ("15602 Olde Highway 80, Flinn Springs, CA 92021").
_CONTACT_ADDRESS = re.compile(r"(?P<street>\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9 .'’#-]{1,60}?),\s*(?P<city>[A-Za-z][A-Za-z .'’-]{1,40}),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?\b")
_PREMISES = r"(?:lawn|grounds|gardens?|barn|property|premises|farm|ranch|store|shop|patio|courtyard|field|orchard|vineyard|winery|venue|location)"
_OUR_PREMISES = re.compile(rf"\b(?:at|on|in|to|around|through)\s+our\s+(?!(?:other|sister|partner|second|new)\b)(?:[a-z]+\s+){{0,2}}{_PREMISES}\b", re.I)


class _ContactBlocks(HTMLParser):
    """The lines of a page's contact blocks: the text of its <footer> and <address> elements."""
    LINES = frozenset(("br", "p", "li", "div", "tr", "address", "footer", "h1", "h2", "h3", "h4", "h5", "h6"))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self._open, self._skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template", "svg"):
            self._skip += 1
        elif tag in ("footer", "address"):
            self._open.append([tag, []])
        if tag in self.LINES:
            for block in self._open:
                block[1].append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template", "svg"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("footer", "address") and self._open and self._open[-1][0] == tag:
            self.blocks.append(_visible_lines("".join(self._open.pop()[1])))
        if tag in self.LINES:
            for block in self._open:
                block[1].append("\n")

    def handle_data(self, data):
        if not self._skip:
            for block in self._open:
                block[1].append(data)


def _address_key(address):
    match = _CONTACT_ADDRESS.search(address or "")
    return (_title_key(match.group("street")), match.group("zip")) if match else None


def _page_contact(html, page_url):
    """The page's own contact address and the name the website goes by, or (None, None) when the page does not establish exactly one of each.
    The address is the one distinct street address its <footer> and <address> elements write (a line, or a street line and the city line after it). The name is a run of words
    on the page (its <title> or contact blocks) that spells the website's own domain ("Summers Past Farms" on summerspastfarms.com)."""
    parser = _ContactBlocks()
    try:
        parser.feed(html[:1_500_000])
        parser.close()
    except (ValueError, TypeError):
        return None, None
    found = {}
    for lines in parser.blocks:
        lines = [_ADDRESS_LABEL.sub("", line) for line in lines]
        for index, line in enumerate(lines):
            for text in (line, line + ", " + lines[index + 1] if index + 1 < len(lines) else ""):
                match = _CONTACT_ADDRESS.match(text.strip())
                if match:
                    found.setdefault(_address_key(match.group(0)), _clean(match.group(0), 250))
                    break
    if len(found) != 1:
        return None, None
    title = re.search(r"<title[^>]*>(.*?)</title>", html[:200_000], re.I | re.S)
    texts = [unescape(title.group(1)) if title else ""] + [" ".join(lines) for lines in parser.blocks]
    parts = (urlsplit(page_url).hostname or "").split(".")
    domain = re.sub(r"[^a-z0-9]", "", parts[-2] if len(parts) >= 2 else "")
    for text in texts if len(domain) >= 3 else ():
        words = re.findall(r"[A-Za-z0-9]+(?:['’&][A-Za-z0-9]+)*", text)
        for start in range(len(words)):
            spelled = ""
            for end in range(start, min(start + 6, len(words))):
                spelled += re.sub(r"[^a-z0-9]", "", words[end].casefold())
                if spelled == domain:
                    return next(iter(found.values())), " ".join(words[start:end + 1])
                if not domain.startswith(spelled):
                    break
    return None, None


def _on_site(text, name):
    """Does an event's own text place it at the website's own establishment? Only by an explicit phrase: "at <name>" or "the gardens of <name>", "at the <kind>" where the
    kind is the last word of the name ("at the Farm" for Summers Past Farms), or the owner's own premises ("on our spacious lawn area"). The quoted phrase, or None."""
    words = r"\s+".join(re.escape(word) for word in name.split())
    kind = re.escape(re.sub(r"s$", "", name.split()[-1], flags=re.I))
    for pattern in (rf"\b(?:at|visit)\s+(?:the\s+)?{words}\b", rf"\b{_PREMISES}\s+(?:of|at)\s+{words}\b",
                    rf"\b(?:at|to|on|in|around|through|visit)\s+(?:the|our)\s+{kind}s?\b"):
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(0)
    match = _OUR_PREMISES.search(text)
    return match.group(0) if match else None


def _inherit_contact_address(events, texts, html, page_url, stated_elsewhere=(), described_elsewhere=()):
    """Give the page's contact address to the events read from THIS page that state no place of their own and whose own text places them at the website's establishment.
    Nothing is inherited when the page has no single contact address or name, or when any event on it states a different place (a labelled location or address, a venue, or
    a street address in its text): such a page cannot establish one location. Only the address is inherited, never a venue name, and the evidence says where it came from.
    `described_elsewhere`: ids of events that have their own event page; they inherit nothing here (their places still count as conflicts)."""
    address, name = _page_contact(html, page_url)
    if not address:
        return events, 0
    home = _address_key(address)
    lines = [line for text in texts.values() for line in text.split("\n")]
    elsewhere = [m.group(1) for m in (_LOCATION.match(line) for line in lines) if m]
    elsewhere += [e.venue for e in list(events) + list(stated_elsewhere) if e.venue] + [e.address for e in list(events) + list(stated_elsewhere) if e.address]
    elsewhere += [m.group(0) for text in texts.values() for m in _CONTACT_ADDRESS.finditer(text)]
    if any(_address_key(place) != home and _title_key(place) != _title_key(name) for place in elsewhere if place):
        return events, 0
    result, inherited = [], 0
    for e in events:
        phrase = None if (e.venue or e.address or e.local_id in described_elsewhere) else _on_site(texts.get(e.local_id, ""), name)
        if not phrase:
            result.append(e)
            continue
        evidence = tuple(e.verification.evidence) + (
            f"Address from the contact block (footer/address) of {page_url}: {address}",
            f"The event's own text places it at {name}: {phrase!r}",
            "The event states no venue or address of its own")
        result.append(replace(e, address=address, verification=stated(evidence, False, e.ends_at, e.venue, address, e.description, e.image_url)))
        inherited += 1
    return result, inherited


def _text_event_group(heading, lines, page_title, page_url, source_name, retrieved_at, zone):
    """Return only candidates with independently visible heading + explicit date + time. Every occurrence takes the time stated WITH its own date."""
    candidate_title = re.sub(r"^\s*20\d{2}\s+", "", heading or "").strip(" :–—")
    if candidate_title.isupper() and len(candidate_title) > 6:
        candidate_title = candidate_title.title()
    if not candidate_title or _UNHELPFUL_HEADINGS.fullmatch(candidate_title) or _FIELD_HEADING.fullmatch(candidate_title):
        candidate_title = page_title
    # A date, a time or a field label is never a title: with no descriptive title there is no candidate, and none is invented.
    if (not candidate_title or _UNHELPFUL_HEADINGS.fullmatch(candidate_title) or _JUNK_TITLES.fullmatch(candidate_title) or _schedule_only(candidate_title)
            or _FIELD_HEADING.fullmatch(candidate_title)):
        return [], 0
    raw_date_lines = [(i, line) for i, line in enumerate(lines[:35])
                      if _TEXT_DATE.search(line) or _TEXT_DATES_ISO.search(line)
                      or _TEXT_DATES_NUMERIC.search(line)]
    if not raw_date_lines:
        return [], 0
    has_date = lambda value: bool(_TEXT_DATE.search(value) or _TEXT_DATES_ISO.search(value) or _TEXT_DATES_NUMERIC.search(value))   # noqa: E731
    slots = {}   # day -> (clock start, clock end, date line, time line): the first statement of an occurrence wins
    consumed = set()   # lines that gave a time to an earlier date
    for pos, line in raw_date_lines[:6]:
        if re.match(r"\s*(?:published|posted|updated|last updated|article date|news date)\s*:?\s*", line, re.I):
            continue  # Publication timestamps are not event dates.
        for dates, (start, end, time_line) in _line_groups(line, heading + " " + page_title):
            if not start:   # the time is on a line right after this date or right before it; the search stops at any other date line and never reuses a line another date took
                following = []
                for index in range(pos + 1, min(len(lines), pos + 4)):
                    if has_date(lines[index]):
                        break
                    following.append(index)
                before = []
                for index in range(pos - 1, max(-1, pos - 3), -1):
                    if has_date(lines[index]) or index in consumed:
                        break
                    before.append(index)
                for window in (following, before[::-1]):
                    clock = _text_clock([lines[index] for index in window])
                    if clock[0]:
                        start, end, time_line = clock
                        consumed.update(window)
                        break
            if not start:
                continue
            for day in dates:
                slots.setdefault(day, (start, end, line, time_line))
    slots = dict(list(slots.items())[:10])
    if not slots:
        return [], len(raw_date_lines)
    venue, address, labelled = _labelled_facts(lines)
    description = labelled or next((line for line in lines[:15] if len(line) > 55 and not
                                    (_TEXT_DATE.search(line) or _TEXT_DATES_ISO.search(line) or _TEXT_DATES_NUMERIC.search(line))
                                    and not _CLOCK.search(line)), None)
    result = []
    for day, (clock_start, clock_end, date_line, clock_line) in slots.items():
        begin = _local_instant(day, clock_start, zone)
        if begin is None:
            continue
        until = None
        if clock_end:
            finish_day = day + timedelta(days=1) if clock_end < clock_start else day
            until = _local_instant(finish_day, clock_end, zone)
        fingerprint = f"{page_url}|{candidate_title.casefold()}|{begin}"
        result.append(EventCandidate(
            local_id=hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32],
            title=_clean(candidate_title, 200), starts_at=begin, ends_at=until,
            venue=venue, address=address, description=_clean(description, 600),
            url=page_url,
            provenance=Provenance(source_name, page_url, retrieved_at, (page_url,)),
            verification=stated((
                f"Heading on {page_url}: {heading or page_title}",
                f"Date shown: {date_line[:180]}",
                f"Time shown: {clock_line[:180]}",
                "Local timezone supplied by configured source: " + str(zone),
            ), False, until, venue, address, description)))
    return result, len(raw_date_lines) - int(bool(result))


def _page_facts(html):
    """The labelled place and description of a page that states no date (an event page whose date is only on its listing): (venue, address, description)."""
    parser = _VisibleEventText()
    try:
        parser.feed(html[:1_500_000])
        groups, _, _ = parser.finish()
    except (ValueError, TypeError):
        return None, None, None
    return _labelled_facts([line for _, lines in groups[:30] for line in lines][:120])


def _merge_detail(listing, detail, detail_url, dated=True, linked_only=False):
    """One candidate for an occurrence the listing and its own event page both describe. The event page's facts win; any fact it omits keeps the listing's value, and every
    disagreement is kept in the evidence. The listing candidate's identity (local_id) and listing attribution are kept, and the event page becomes the event's URL.
    `dated` is False for an event page that states no date (it then corroborates nothing about when). `linked_only` is True when the page's title words the event differently
    and the card's own link decided the match: the listing's title is then kept."""
    same_start = _same_instant(listing.starts_at, detail.starts_at)
    ends_at = detail.ends_at or (listing.ends_at if same_start else None)
    venue, address = detail.venue or listing.venue, detail.address or listing.address
    description = detail.description or listing.description
    image_url = detail.image_url or listing.image_url
    disagreements = []
    if dated and not same_start:
        disagreements.append(f"start: the listing says {listing.starts_at}, the event page {detail.starts_at}")
    if dated and listing.ends_at and detail.ends_at and not _same_instant(listing.ends_at, detail.ends_at):
        disagreements.append(f"end: the listing says {listing.ends_at}, the event page {detail.ends_at}")
    for label, old, new in (("venue", listing.venue, detail.venue), ("address", listing.address, detail.address)):
        if old and new and _title_key(old) != _title_key(new):
            disagreements.append(f"{label}: the listing says {old!r}, the event page {new!r}")
    evidence = tuple(dict.fromkeys(tuple(listing.verification.evidence) + tuple(detail.verification.evidence)
                                   + (f"Facts read from the event page {detail_url}, which take precedence over the listing {listing.provenance.listing_url or listing.url}",)
                                   + ((f"The listing card links to {detail_url}, whose one event is at the listing's time; the listing's title {listing.title!r} is kept over "
                                       f"the page's {detail.title!r}",) if linked_only else ())
                                   + tuple("Listing and event page disagree on " + line for line in disagreements)))
    return replace(
        detail, local_id=listing.local_id, title=listing.title if linked_only else detail.title, ends_at=ends_at, venue=venue, address=address, description=description, url=detail.url or detail_url,
        image_url=image_url, image_source=detail.image_source if detail.image_url else listing.image_source, timezone=detail.timezone or listing.timezone,
        categories=detail.categories or listing.categories, organizer=detail.organizer or listing.organizer, price=detail.price or listing.price, geo=detail.geo or listing.geo,
        provenance=Provenance(listing.provenance.source_name, listing.provenance.listing_url, detail.provenance.retrieved_at,
                              tuple(dict.fromkeys(tuple(listing.provenance.found_on) + tuple(detail.provenance.found_on) + (detail_url,)))),
        verification=stated(evidence, dated and same_start and not disagreements, ends_at, venue, address, description, image_url),
        state={"detail_url": detail_url, "listing_starts_at": listing.starts_at})


def _carry_forward(candidate, old):
    """The event page could not be read this time (failed, rate-limited, over budget): keep the facts it gave on an earlier scan instead of erasing them from the stored record."""
    unchanged = _same_instant(candidate.starts_at, old.state.get("listing_starts_at", ""))
    starts_at, ends_at = (old.starts_at, old.ends_at or candidate.ends_at) if unchanged else (candidate.starts_at, candidate.ends_at)
    venue, address, description = candidate.venue or old.venue, candidate.address or old.address, candidate.description or old.description
    image_url = candidate.image_url or old.image_url
    note = f"Facts from the event page {old.state['detail_url']} are those of an earlier scan: the page was not read this time"
    return replace(
        candidate, starts_at=starts_at, ends_at=ends_at, venue=venue, address=address, description=description, url=old.url or candidate.url,
        image_url=image_url, image_source=candidate.image_source if candidate.image_url else old.image_source,
        provenance=Provenance(candidate.provenance.source_name, candidate.provenance.listing_url, candidate.provenance.retrieved_at,
                              tuple(dict.fromkeys(tuple(candidate.provenance.found_on) + (old.state["detail_url"],)))),
        verification=stated(tuple(candidate.verification.evidence) + (note,), False, ends_at, venue, address, description, image_url),
        state=dict(old.state))


def _stabilize_ids(candidates, previous):
    """An occurrence whose stated time was corrected would get a new id (ids are made from the time). When exactly ONE earlier record of the same listing, title and day exists and
    nothing now claims its id, the corrected occurrence keeps that earlier id, so the stored candidate is updated instead of a second one being created.
    An occurrence whose title was corrected from a date line (an earlier extractor titled it "Saturday-Sunday December 5 & 6, 2026") likewise keeps the id of exactly ONE earlier
    record of the same listing at the same instant whose title was schedule-only and states this occurrence's day, when no other new occurrence of that listing has that instant.
    One corrected from a field label ("Dates and Times") keeps the id of exactly ONE earlier record of the same event page at the same instant titled by such a label.
    Returns how many ids were kept and how many ambiguous corrections could not be matched."""
    known = {old.local_id: old for old in previous}
    free = {old.local_id: old for old in previous if old.local_id not in candidates}
    kept = unmatched = 0
    for local_id, candidate in list(candidates.items()):
        if local_id in known:
            continue
        same_listing = lambda other: other.provenance.listing_url == candidate.provenance.listing_url   # noqa: E731
        options = [old for old in free.values() if same_listing(old)
                   and _title_key(old.title) == _title_key(candidate.title) and _same_day(old.starts_at, candidate.starts_at)]
        rivals = [other for lid, other in candidates.items() if lid not in known and same_listing(other)
                  and _title_key(other.title) == _title_key(candidate.title) and _same_day(other.starts_at, candidate.starts_at)]
        if not options and not _schedule_only(candidate.title):
            day = _instant(candidate.starts_at)
            options = [old for old in free.values() if same_listing(old) and _schedule_only(old.title) and _same_instant(old.starts_at, candidate.starts_at)
                       and day is not None and day.date() in _text_dates(old.title, "")]
            rivals = [other for lid, other in candidates.items() if lid not in known and same_listing(other) and _same_instant(other.starts_at, candidate.starts_at)]
        if not options and not _FIELD_HEADING.fullmatch(candidate.title):
            # an earlier extractor titled the occurrence by a field label of its page ("Dates and Times"): the same event page at the same instant identifies it
            same_page = lambda other: same_listing(other) and bool(other.url) and _page_key(other.url) == _page_key(candidate.url or "")   # noqa: E731
            options = [old for old in free.values() if same_page(old) and _FIELD_HEADING.fullmatch(old.title or "") and _same_instant(old.starts_at, candidate.starts_at)]
            rivals = [other for lid, other in candidates.items() if lid not in known and same_page(other) and _same_instant(other.starts_at, candidate.starts_at)]
        if len(options) == 1 and len(rivals) == 1:
            old = options[0]
            del candidates[local_id]
            candidates[old.local_id] = replace(candidate, local_id=old.local_id)
            free.pop(old.local_id, None)
            kept += 1
        elif options:
            unmatched += 1
    return kept, unmatched


def _title_key(title):
    return re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()


def _card_occurrence(href, lines):
    """What one linked listing card says that is not enough for an event on its own: its title (its first line), ONE date written without a year ("Fri 30 Oct"), the time stated
    with it and, when one other short line is left, the place it names. None when the card states no such single date and time. Nothing is read from outside the card."""
    if not lines or not href or _has_date(lines[0]) or _CLOCK.search(lines[0]) or _schedule_only(lines[0]) or _UNHELPFUL_HEADINGS.fullmatch(lines[0]) or _FIELD_HEADING.fullmatch(lines[0]):
        return None
    rest = lines[1:]
    row = " ".join(rest)
    if _YEAR.search(row) or _TEXT_DATES_ISO.search(row) or _TEXT_DATES_NUMERIC.search(row):
        return None   # a card with a full date is read as an event by the card and section rules, not here
    days = {(_MONTH_NAMES.get(m.group("month").rstrip(".").casefold()) or _MONTH_NAMES.get(m.group("month")[:3].casefold()), int(m.group("day")))
            for pattern in (_TEXT_DATE, _DAY_MONTH) for m in pattern.finditer(row)}
    start, end, clock_line = _text_clock(rest)
    if len(days) != 1 or not start:
        return None
    month, day = next(iter(days))
    others = [line for line in rest if line != clock_line and not _CLOCK.search(line)
              and not all(word.isdigit() or word in _SCHEDULE_WORDS for word in re.findall(r"[a-z]+|\d+", line.casefold()))]
    venue = others[0] if len(others) == 1 and _place_name(others[0]) else None
    return {"href": href, "title": _clean(lines[0], 200), "month": month, "day": day, "start": start, "end": end, "clock": clock_line,
            "date": " ".join(line for line in rest if line not in others and line != clock_line), "venue": venue}


def _with_listing_card(event, occurrences, page_url):
    """An event page's occurrence, with the evidence of the ONE listing card that links to this page and states the same title, day and time. The card gives no year: the event
    page's own date stands. Agreement is recorded as corroboration; any disagreement is kept in the evidence and leaves the occurrence uncorroborated."""
    start = _instant(event.starts_at)
    if start is None or not occurrences:
        return event
    fits = [o for o in occurrences if (o["month"], o["day"]) == (start.month, start.day) and o["start"] == (start.hour, start.minute) and _related(o["title"], event.title)]
    if len(fits) != 1:
        return event
    card = fits[0]
    end = _instant(event.ends_at) if event.ends_at else None
    disagreements = []
    if card["end"] and end is not None and card["end"] != (end.hour, end.minute):
        disagreements.append(f"end: the listing card says {card['clock']!r}, the event page {event.ends_at}")
    if card["venue"] and event.venue and _title_key(card["venue"]) != _title_key(event.venue):
        disagreements.append(f"venue: the listing card says {card['venue']!r}, the event page {event.venue!r}")
    lines = ((f"Listing card on {', '.join(card['listings'])} links to {page_url}: {card['title']!r}, {card['date']}, {card['clock']}"
              + (f", at {card['venue']}" if card["venue"] else "")),
             "The listing card states no year; the date is the event page's") + tuple("Listing card and event page disagree on " + line for line in disagreements)
    return replace(event, provenance=Provenance(event.provenance.source_name, event.provenance.listing_url, event.provenance.retrieved_at,
                                                tuple(dict.fromkeys(tuple(event.provenance.found_on) + tuple(card["listings"])))),
                   verification=stated(tuple(event.verification.evidence) + lines, not disagreements, event.ends_at, event.venue, event.address, event.description, event.image_url))


def _plain_text_events(html, page_url, source_name, retrieved_at, zone, links=None, structured=(), undated=None):
    """Events read from visible text. When `links` (a dict) is given it is filled with {local_id: href} for events read from a listing card that links to a page.
    `structured`: the events this page also states in structured data (their places count when deciding whether the page establishes one location).
    `undated` (a list), when given, receives the linked listing cards that state a day and time but no year (`_card_occurrence`)."""
    parser = _VisibleEventText()
    try:
        parser.feed(html[:1_500_000])
        groups, cards, page_title = parser.finish()
    except (ValueError, TypeError):
        return [], 0
    if undated is not None:
        undated.extend(card for card in (_card_occurrence(href, lines) for href, lines in parser.link_cards[:200]) if card)
    events, missing = [], 0
    texts = {}   # local_id -> the event's own text (its heading section or listing card)
    for heading, lines in groups[:100]:
        extracted, skipped = _text_event_group(heading, lines, page_title,
                                               page_url, source_name, retrieved_at, zone)
        events.extend(extracted)
        missing += skipped
        for e in extracted:
            texts.setdefault(e.local_id, "\n".join([heading] + lines[:35]))
    # Listing cards often have no headings, e.g. <li>Craft Fair — Nov 14 at 9am</li>.
    card_links = {}
    for card, card_heading, card_href in zip(cards[:120], parser.card_headings[:120], parser.card_hrefs[:120]):
        row = " ".join(card)
        if not (_TEXT_DATE.search(row) or _TEXT_DATES_ISO.search(row) or _TEXT_DATES_NUMERIC.search(row)):
            continue
        date_match = _TEXT_DATE.search(row) or _TEXT_DATES_ISO.search(row) or _TEXT_DATES_NUMERIC.search(row)
        title = _listing_title(row[:date_match.start()]).rstrip(" ,-–—|:")
        title = re.sub(r"(?:\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))\s*,?$",
                       "", title, flags=re.I).strip(" ,-–—|:")
        if " — " in title:
            title = title.split(" — ", 1)[0].strip()
        # A card that carries its own heading is titled by it (the text before the date also holds field labels); otherwise the labels are trimmed off.
        title = card_heading if card_heading and title.startswith(card_heading) else title
        if not title or _UNHELPFUL_HEADINGS.fullmatch(title):
            continue
        extracted, skipped = _text_event_group(title[:120], [row], page_title,
                                               page_url, source_name, retrieved_at, zone)
        events.extend(extracted)
        missing += skipped
        for e in extracted:
            texts.setdefault(e.local_id, "\n".join(card))
        if card_href:
            for e in extracted:
                card_links.setdefault((_title_key(e.title), e.starts_at), card_href)
    # A page can expose the same event in text, JSON-LD, a list item and a heading.
    distinct = {}
    for e in events:
        distinct.setdefault((_title_key(e.title), e.starts_at), e)
    if links is not None:
        for key, e in distinct.items():
            if key in card_links:
                links[e.local_id] = card_links[key]
    # A listing card that links to its own event page is described by that page (and by that page's contact block), not by the listing's.
    own_page = {e.local_id for key, e in distinct.items() if key in card_links and _page_key(urljoin(page_url, card_links[key])) != _page_key(page_url)}
    found, _ = _inherit_contact_address(list(distinct.values())[:MAX_EVENTS], texts, html, page_url, structured, own_page)
    return found, missing



def _page_key(url):
    """What makes two addresses the same page for crawling: "/events" and "/events/" are one page."""
    parts = urlsplit(url)
    return (parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", parts.query)


class _Frontier:
    """The pages still to be fetched. Every starting URL comes first, in order. After them the websites take turns, one page each, and a website offers its event-detail links
    (a listing card's own link) before its generic discovery links, so that a website with many links cannot use up the budget of another."""

    def __init__(self, starts):
        self._starts = deque((url, 0, url, None) for url in starts)
        self._known = {_page_key(url) for url in starts}
        self._sites = {url: (deque(), deque()) for url in starts}   # starting URL -> (event-detail links, generic links), in the order the sites were configured
        self._turn = 0

    def __len__(self):
        return len(self._starts) + sum(len(a) + len(b) for a, b in self._sites.values())

    def __contains__(self, url):
        return _page_key(url) in self._known

    def pending(self, seed):
        events, generic = self._sites[seed]
        return len(events) + len(generic)

    def push(self, url, depth, seed, found_on, event):
        if _page_key(url) in self._known:
            return False
        self._known.add(_page_key(url))
        self._sites[seed][0 if event else 1].append((url, depth, seed, found_on))
        return True

    def pop(self):
        if self._starts:
            return self._starts.popleft()
        seeds = list(self._sites)
        for step in range(len(seeds)):
            index = (self._turn + step) % len(seeds)
            events, generic = self._sites[seeds[index]]
            queue = events or generic
            if queue:
                self._turn = (index + 1) % len(seeds)
                return queue.popleft()
        raise IndexError("no page is waiting")

    def waiting(self):
        return list(self._starts) + [item for events, generic in self._sites.values() for item in (*events, *generic)]


class SourcesProducer:
    adapter = "sources_producer"
    extractor = "producers/sources_producer@v1"
    method = "adapter"
    display_name = "Website event sources (generic)"
    configuration_fields = (
        {"name": "urls", "label": "Website URLs (one per line)", "kind": "url_list", "required": True,
         "placeholder": "https://example.org/events\nhttps://another.org/calendar"},
        {"name": "timezone", "label": "Local timezone for page text", "kind": "text",
         "placeholder": "America/Los_Angeles"},
        {"name": "max_pages", "label": "Page budget per scan: the starting URLs AND every page followed from them, all websites together", "kind": "integer",
         "default": 12, "min": 1, "max": MAX_PAGES},
        {"name": "max_depth", "label": "Link levels to follow (0 = only the starting URLs; 1 = also the event pages they link to)", "kind": "integer",
         "default": 1, "min": 0, "max": MAX_DEPTH},
        {"name": "max_events", "label": "Maximum events per scan", "kind": "integer",
         "default": 75, "min": 1, "max": MAX_EVENTS},
    )

    def __init__(self, image_extractor=None):
        """`image_extractor`: any image_extractor/v1 engine, injected by the composition root; None means pages are not searched for images."""
        self.image_extractor = image_extractor

    @staticmethod
    def validate_options(options):
        if not isinstance(options, dict):
            raise ValueError("options must be an object")
        unknown = sorted(set(options) - {"url", "urls", "timezone", "max_pages", "max_depth", "max_events"})
        if unknown:
            raise ValueError(f"unsupported sources_producer options: {', '.join(unknown)[:100]}")
        raw = options.get("urls")
        if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= MAX_SEEDS:
            raise ValueError(f"urls must be a list of 1 to {MAX_SEEDS} website addresses")
        urls = tuple(dict.fromkeys(_url(v) for v in raw))
        if options.get("url") is not None and _url(options["url"]) != urls[0]:
            # The stored base URL is derived from the list; never accept a second authority.
            raise ValueError("url must match the first website in urls")
        values = {
            "urls": list(urls),
            **({"timezone": _timezone(options["timezone"])} if options.get("timezone") else {}),
            "max_pages": _integer(options.get("max_pages"), "max_pages", 12, 1, MAX_PAGES),
            "max_depth": _integer(options.get("max_depth"), "max_depth", 1, 0, MAX_DEPTH),
            "max_events": _integer(options.get("max_events"), "max_events", 75, 1, MAX_EVENTS),
        }
        if values["max_pages"] < len(urls):
            raise ValueError(f"max_pages ({values['max_pages']}) is the page budget for the whole scan, starting URLs included, so it must be at least the number of "
                             f"starting URLs ({len(urls)}); to follow links as well it must be larger")
        return urls[0], values

    @staticmethod
    def configuration_warnings(options):
        """Valid but ineffective settings, in plain words (never an error: an existing source keeps running)."""
        urls, max_pages, max_depth = options.get("urls") or (), options.get("max_pages", 12), options.get("max_depth", 1)
        if isinstance(max_pages, int) and max_depth and max_pages <= len(urls):
            return (f"max_depth is {max_depth} but max_pages ({max_pages}) leaves no budget beyond the {len(urls)} starting URL(s): no linked event page can be fetched. "
                    f"Raise max_pages (it counts starting URLs plus followed pages, all websites together).",)
        return ()

    @staticmethod
    def page_role(url, base_url):
        parts = urlsplit(url)
        if parts.path.lower().endswith((".ics", ".ical")):
            return "feed"
        if EVENT_WORDS.search(parts.path):
            return "event"
        return "listing"

    def crawl(self, fetcher, previous, options):
        # Control validates on create/edit; validate again at the adapter boundary.
        _, cfg = self.validate_options({k: v for k, v in options.items()
                                         if k not in ("source_key", "name")})
        name = options.get("name", "Website event sources")
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        frontier = _Frontier(cfg["urls"])
        per_site = {}        # website -> what this scan did there (reported in the notes)

        def site_of(url):
            return per_site.setdefault(urlsplit(url).hostname, {
                "pages_attempted": 0, "pages_fetched": 0, "pages_failed": 0, "event_pages_followed": 0, "event_pages_failed": 0,
                "links_over_cap": 0, "pages_unvisited": 0, "pages_skipped_candidate_limit": 0})
        seen = set()
        read_ok = set()      # pages that were fetched and read
        candidates = {}
        leads = []
        errors = []
        rejected_dates = 0
        unstructured_events = 0
        limit = cfg["max_pages"]
        max_depth = cfg["max_depth"]
        cap = cfg["max_events"]
        origin = {}          # local_id -> (depth, site, page) of the page that supplied the candidate
        linked_from = {}     # absolute event-page URL -> local_ids of the listing candidates whose card links to it
        page_targets = {}    # page -> the event pages its cards link to
        card_pages = set()   # event pages reached through a listing card
        card_dates = {}      # event page (page key) -> {card identity: occurrence}: listing cards that link to it with a day and time but no year
        counts = {"followed": 0, "merged": 0, "failed": 0, "not_added": 0, "ignored": 0, "detail_only": 0, "facts_only": 0}
        warnings = SourcesProducer.configuration_warnings(cfg)

        def add(event, depth, site, page_url):
            if len(candidates) >= cap:
                counts["not_added"] += 1
                return False
            candidates[event.local_id] = event
            origin[event.local_id] = (depth, site, page_url)
            return True

        def merge_into(local_id, event, depth, site, page_url, dated=True, linked_only=False):
            candidates[local_id] = _merge_detail(candidates[local_id], event, page_url, dated, linked_only)
            origin[local_id] = (depth, site, page_url)
            counts["merged"] += 1

        def swap_in_listing(old_id, event, depth, site, page_url):
            """`event` is the listing of a candidate that an event page supplied first (a starting URL that is also linked): the listing's id is the stable one."""
            old, old_page = candidates.pop(old_id), origin.pop(old_id)[2]
            candidates[event.local_id] = _merge_detail(event, old, old_page)
            origin[event.local_id] = (depth, site, page_url)
            for owners in linked_from.values():
                owners[:] = [event.local_id if lid == old_id else lid for lid in owners]
            counts["merged"] += 1

        def absorb(page_events, page_url, depth, site):
            """Add a page's events. An event the site already described on its listing is MERGED into that candidate instead of duplicating it; an event page speaks only for the
            occurrences whose cards linked to it. Returns how many of its events were used."""
            owners = [lid for lid in linked_from.get(page_url, ()) if lid in candidates]
            yearless = list(card_dates.get(_page_key(page_url), {}).values())
            page_events = [_with_listing_card(event, yearless, page_url) for event in page_events]
            used, claimed = 0, set()
            for event in page_events:
                if event.local_id in candidates:
                    continue
                if owners:
                    plausible = [lid for lid in owners if lid not in claimed and _same_instant(candidates[lid].starts_at, event.starts_at)
                                 and _related(candidates[lid].title, event.title)]
                    if len(plausible) > 1:   # several owners fit: the one with exactly this title, else none (never guess)
                        plausible = [lid for lid in plausible if _title_key(candidates[lid].title) == _title_key(event.title)]
                        if len(plausible) != 1:
                            counts["ignored"] += 1
                            continue
                    match = plausible[0] if plausible else None
                    if match is None and len(page_events) == 1:   # the one occurrence the card linked to, at a corrected time
                        near = [lid for lid in owners if lid not in claimed and _same_day(candidates[lid].starts_at, event.starts_at) and _related(candidates[lid].title, event.title)]
                        match = near[0] if len(near) == 1 else None
                    linked_only = False
                    if match is None and len(page_events) == 1:   # the page words the title differently: the card's own link to a page whose one event is at exactly this
                        # occurrence's time decides, when exactly one linking card is at that time, the end times do not disagree and the titles share a distinctive word
                        timed = [lid for lid in owners if _same_instant(candidates[lid].starts_at, event.starts_at)]
                        if (len(timed) == 1 and timed[0] not in claimed and _share_a_distinctive_word(candidates[timed[0]].title, event.title)
                                and not (candidates[timed[0]].ends_at and event.ends_at and not _same_instant(candidates[timed[0]].ends_at, event.ends_at))):
                            match, linked_only = timed[0], True
                    if match is not None:
                        merge_into(match, event, depth, site, page_url, linked_only=linked_only)
                        claimed.add(match)
                        used += 1
                    elif any(_related(candidates[lid].title, event.title) for lid in owners):   # another date of the same event that only its page lists
                        used += 1
                        counts["detail_only"] += add(event, depth, site, page_url)
                    else:   # a related event, a navigation item or other text of the page: not this event
                        counts["ignored"] += 1
                    continue
                twin = next((lid for lid, c in candidates.items() if origin[lid][1] == site and _title_key(c.title) == _title_key(event.title)
                             and _same_instant(c.starts_at, event.starts_at)), None)
                if twin is None:
                    used += add(event, depth, site, page_url)
                    continue
                twin_page = origin[twin][2]
                if page_url in page_targets.get(twin_page, ()):
                    merge_into(twin, event, depth, site, page_url)
                elif twin_page in page_targets.get(page_url, ()):
                    swap_in_listing(twin, event, depth, site, page_url)
                elif depth > origin[twin][0]:
                    merge_into(twin, event, depth, site, page_url)
                used += 1
            return used

        for url in cfg["urls"]:
            site_of(url)
        while frontier and len(seen) < limit:
            page_url, depth, seed, found_on = frontier.pop()
            if _page_key(page_url) in seen:
                continue
            stats = site_of(seed)
            if depth > 0 and len(candidates) >= cap and page_url not in card_pages:
                stats["pages_skipped_candidate_limit"] += 1   # nothing new can be added there; only the event pages of known candidates are still read
                continue
            seen.add(_page_key(page_url))
            stats["pages_attempted"] += 1
            if page_url in card_pages:
                counts["followed"] += 1
                stats["event_pages_followed"] += 1
            try:
                response = fetcher.fetch(page_url)
            except FetchError as error:
                stats["pages_failed"] += 1
                if page_url in card_pages:
                    counts["failed"] += 1   # the listing's own facts stand
                    stats["event_pages_failed"] += 1
                errors.append(str(error)[:200])
                leads.append(SourceLead(page_url, "seed" if found_on is None else "event",
                    "rejected", (str(error)[:200],), found_on=found_on, fetched=False))
                continue
            stats["pages_fetched"] += 1
            read_ok.add(page_url)
            page = response.text
            media = (response.content_type or "").lower()
            before = len(candidates)
            discovered = []
            page_links = {}
            if (page_url.lower().split("?", 1)[0].endswith((".ics", ".ical"))
                    or "text/calendar" in media or "begin:vcalendar" in page[:500].lower()):
                events, rejected = _ics_events(page, page_url, name, retrieved_at)
            elif "xml" in media or page.lstrip().startswith(("<?xml", "<rss", "<feed")):
                events, rejected = [], 0
                discovered = _feed_links(page)
            elif "json" in media or page.lstrip().startswith(("{", "[")):
                events, rejected = _jsonld_events(page, page_url, name, retrieved_at)
            else:
                parser = _LinksAndEvents()
                try:
                    parser.feed(page)
                    events, rejected = [], 0
                    for script in parser.scripts[:30]:
                        extracted, discarded = _jsonld_events(script, page_url, name, retrieved_at)
                        events.extend(extracted)
                        rejected += discarded
                    discovered = parser.links
                    undated = []
                    visible, from_text = _plain_text_events(page, page_url, name, retrieved_at,
                                                            cfg.get("timezone"), page_links, events, undated)
                    for card in undated:   # the same card on two listing pages (/events and /events/calendar) is one card, seen on both
                        try:
                            target = _url(urljoin(page_url, card["href"]))
                        except ValueError:
                            continue
                        if urlsplit(target).hostname == urlsplit(page_url).hostname and _page_key(target) != _page_key(page_url):
                            identity = (_title_key(card["title"]), card["month"], card["day"], card["start"], card["end"], _title_key(card["venue"] or ""))
                            known = card_dates.setdefault(_page_key(target), {}).setdefault(identity, {**card, "listings": []})
                            known["listings"] = list(dict.fromkeys(known["listings"] + [page_url]))
                    # Favor exact structured source claims when the same event is also visible as text.
                    existing = {(_title_key(e.title), _instant(e.starts_at) or e.starts_at) for e in events}
                    text_events = [e for e in visible if (_title_key(e.title), _instant(e.starts_at) or e.starts_at) not in existing]
                    events.extend(text_events)
                    events = _attach_page_image(events, page, page_url, fetcher, self.image_extractor)
                    unstructured_events += len(text_events)
                    rejected += from_text
                except (ValueError, TypeError):
                    events, rejected = [], 0
            rejected_dates += rejected
            site = urlsplit(seed).hostname
            card_targets = []
            for local_id, href in page_links.items():   # the event pages the listing cards link to, in page order
                try:
                    target = _url(urljoin(page_url, href))
                except ValueError:
                    continue
                if urlsplit(target).hostname == site and _page_key(target) != _page_key(page_url):
                    linked_from.setdefault(target, []).append(local_id)
                    if target not in card_targets:
                        card_targets.append(target)
            page_targets[page_url] = set(card_targets)
            used = absorb(events, page_url, depth, site)
            owners = [lid for lid in linked_from.get(page_url, ()) if lid in candidates and lid in origin and origin[lid][2] != page_url]
            if page_url in card_pages and owners and not events:   # an event page that states no usable date can still describe the occurrences that linked to it
                venue, address, description = _page_facts(page)
                if venue or address or description:
                    pseudo = [replace(candidates[lid], venue=venue or candidates[lid].venue, address=address or candidates[lid].address,
                                      description=_clean(description, 600) or candidates[lid].description, url=page_url,
                                      provenance=Provenance(candidates[lid].provenance.source_name, candidates[lid].provenance.listing_url, retrieved_at, (page_url,)),
                                      verification=stated((f"Labelled facts on {page_url}",), False, candidates[lid].ends_at, venue, address, description, candidates[lid].image_url))
                              for lid in owners]
                    for lid, detail in zip(owners, _attach_page_image(pseudo, page, page_url, fetcher, self.image_extractor)):
                        merge_into(lid, detail, depth, site, page_url, dated=False)
                        counts["facts_only"] += 1
            leads.append(SourceLead(page_url, "seed" if found_on is None else "event",
                "accepted", ("public page inspected for offset-aware event data",),
                score=10 if events else 0, found_on=found_on, events=len(candidates)-before, fetched=True))
            if depth >= max_depth:
                continue
            added = 0
            for target in card_targets:   # evidence-backed event links first: a card's own link needs no keyword
                if added >= MAX_LINKS or stats["pages_attempted"] + frontier.pending(seed) >= limit * 3:
                    break
                if _page_key(target) in seen or target in frontier:
                    continue
                card_pages.add(target)
                frontier.push(target, depth + 1, seed, page_url, True)
                added += 1
            stats["links_over_cap"] += sum(1 for target in card_targets if _page_key(target) not in seen and target not in frontier)   # event links beyond the per-page / per-site cap
            for link, title in discovered:
                if len(candidates) >= cap or added >= MAX_LINKS or stats["pages_attempted"] + frontier.pending(seed) >= limit * 3:
                    break
                if not EVENT_WORDS.search(link + " " + title):
                    continue
                try:
                    target = _url(urljoin(page_url, link))
                except ValueError:
                    continue
                # Do not crawl arbitrary external websites linked from a seed.
                if urlsplit(target).hostname != urlsplit(seed).hostname:
                    continue
                if _page_key(target) in seen or target in frontier:
                    continue
                frontier.push(target, depth + 1, seed, page_url, False)
                added += 1
        if not any(lead.fetched for lead in leads) and errors:
            raise FetchError("all configured websites failed: " + "; ".join(errors[:2]))
        unvisited = [row for row in frontier.waiting() if _page_key(row[0]) not in seen]
        for row in unvisited:
            site_of(row[2])["pages_unvisited"] += 1
        exhausted = bool(unvisited) and len(seen) >= limit
        # Stable identities: a corrected time keeps the stored candidate's id, and event pages that could not be read this time keep what an earlier scan learned from them.
        kept, ambiguous = _stabilize_ids(candidates, previous or ())
        earlier = {old.local_id: old for old in previous or ()}
        carried = 0
        for local_id, candidate in list(candidates.items()):
            old = earlier.get(local_id)
            if old is not None and not candidate.state.get("detail_url") and old.state.get("detail_url") and old.state["detail_url"] not in read_ok:
                candidates[local_id] = _carry_forward(candidate, old)
                carried += 1
        for stats in per_site.values():
            stats["pages_skipped"] = stats["links_over_cap"] + stats["pages_unvisited"] + stats["pages_skipped_candidate_limit"]
        notes = {"sites": len(cfg["urls"]), "pages": sum(v.fetched for v in leads),
                 "candidates": len(candidates), "invalid_or_ambiguous_event_dates": rejected_dates,
                 "failed_pages": len(errors), "links_remaining": len(frontier),
                 "unstructured_candidates": unstructured_events,
                 "page_budget": limit, "pages_attempted": len(seen), "page_budget_exhausted": exhausted, "pages_unvisited": len(unvisited) if exhausted else 0,
                 "candidate_limit": cap, "candidate_limit_reached": len(candidates) >= cap, "events_not_added": counts["not_added"],
                 "event_pages_followed": counts["followed"], "event_pages_failed": counts["failed"], "event_pages_merged": counts["merged"],
                 "event_pages_facts_only": counts["facts_only"], "event_page_unrelated_events_ignored": counts["ignored"], "event_page_only_occurrences": counts["detail_only"],
                 "identities_kept_after_correction": kept, "identity_corrections_ambiguous": ambiguous, "event_page_facts_carried_forward": carried,
                 "by_website": json.dumps(per_site, sort_keys=True)}   # a JSON text: run notes are flat
        if warnings:
            notes["configuration_warning"] = " ".join(warnings)
        return ProducerResult(tuple(candidates.values()), tuple(leads), notes)
