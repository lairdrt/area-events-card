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
_DATE_LABEL = re.compile(r"^(?:dates?|when)\s*:\s*", re.I)
_TIME_LABEL = re.compile(r"^(?:times?|hours?)\s*:\s*", re.I)
_WEEKDAY = re.compile(r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", re.I)


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
        self.first_heading = ""

    def _section_flush(self):
        value = _visible_lines("".join(self._section))
        if self._heading or value:
            self.sections.append((self._heading, value))
        self._section = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._ignored.append(tag)
        if self._ignored:
            return
        if tag in self.HEADINGS:
            self._section_flush()
            self._heading_tag = tag
            self._heading = ""
            self._section.append("\n")
        elif tag in self.BLOCKS:
            self._section.append("\n")
        if tag in ("li", "article", "tr"):
            self._cards.append([tag, []])
        elif tag in ("td", "th") and self._cards and self._cards[-1][0] == "tr":
            self._cards[-1][1].append(" — ")

    def handle_data(self, data):
        if self._ignored:
            return
        if self._heading_tag:
            self._heading += data
        self._section.append(data)
        for card in self._cards:
            card[1].append(data)

    def handle_endtag(self, tag):
        if self._ignored:
            if tag == self._ignored[-1]:
                self._ignored.pop()
            return
        if tag in self.HEADINGS and tag == self._heading_tag:
            self._heading = _clean(self._heading, 160) or ""
            if self._heading and not self.first_heading:
                self.first_heading = self._heading
            self._heading_tag = None
            self._section.append("\n")
        elif tag in self.BLOCKS:
            self._section.append("\n")
        if tag in ("li", "article", "tr") and self._cards and self._cards[-1][0] == tag:
            card = self._cards.pop()
            lines = _visible_lines(" ".join(card[1]))
            if lines:
                self.cards.append(lines)

    def finish(self):
        self._section_flush()
        return self.sections, self.cards, self.first_heading


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


def _text_event_group(heading, lines, page_title, page_url, source_name, retrieved_at, zone):
    """Return only candidates with independently visible heading + explicit date + time."""
    candidate_title = re.sub(r"^\s*20\d{2}\s+", "", heading or "").strip(" :–—")
    if candidate_title.isupper() and len(candidate_title) > 6:
        candidate_title = candidate_title.title()
    if not candidate_title or _UNHELPFUL_HEADINGS.fullmatch(candidate_title):
        candidate_title = page_title
    if not candidate_title or _UNHELPFUL_HEADINGS.fullmatch(candidate_title) or _JUNK_TITLES.fullmatch(candidate_title):
        return [], 0
    raw_date_lines = [(i, line) for i, line in enumerate(lines[:35])
                      if _TEXT_DATE.search(line) or _TEXT_DATES_ISO.search(line)
                      or _TEXT_DATES_NUMERIC.search(line)]
    if not raw_date_lines:
        return [], 0
    # Avoid combining unrelated body paragraphs: time should be nearby a stated date.
    dates = []
    evidence = []
    clock_start = clock_end = clock_line = None
    for pos, line in raw_date_lines[:6]:
        if re.match(r"\s*(?:published|posted|updated|last updated|article date|news date)\s*:?\s*", line, re.I):
            continue  # Publication timestamps are not event dates.
        expanded = _text_dates(line, heading + " " + page_title)
        if not expanded:
            continue
        adjacent = lines[max(0, pos-2):min(len(lines), pos+4)]
        start, end, time_line = _text_clock(adjacent)
        if not start:
            continue
        dates.extend(expanded)
        if clock_line is None:
            clock_start, clock_end, clock_line = start, end, time_line
        evidence.append((line, time_line))
    dates = list(dict.fromkeys(dates))[:10]
    if not dates or clock_start is None:
        return [], len(raw_date_lines)
    venue = address = None
    for index, line in enumerate(lines[:35]):
        match = _LOCATION.match(line)
        if match:
            venue = _clean(match.group(1), 160)
            # Only use a nearby explicit street/city line, never a footer address.
            following = lines[index+1:index+3]
            address_lines = [v for v in following if re.search(r"\b\d{1,5}\s+[A-Za-z]|\b[A-Za-z][A-Za-z ]+,\s*[A-Z]{2}\b", v)]
            address = _clean(", ".join(address_lines), 250) if address_lines else None
            break
    description = next((line for line in lines[:15] if len(line) > 55 and not
                       (_TEXT_DATE.search(line) or _TEXT_DATES_ISO.search(line) or _TEXT_DATES_NUMERIC.search(line))
                       and not _CLOCK.search(line)), None)
    result = []
    for day in dates:
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
                f"Date shown: {evidence[0][0][:180]}",
                f"Time shown: {clock_line[:180]}",
                "Local timezone supplied by configured source: " + str(zone),
            ), False, until, venue, address, description)))
    return result, len(raw_date_lines) - int(bool(result))


def _plain_text_events(html, page_url, source_name, retrieved_at, zone):
    parser = _VisibleEventText()
    try:
        parser.feed(html[:1_500_000])
        groups, cards, page_title = parser.finish()
    except (ValueError, TypeError):
        return [], 0
    events, missing = [], 0
    for heading, lines in groups[:100]:
        extracted, skipped = _text_event_group(heading, lines, page_title,
                                               page_url, source_name, retrieved_at, zone)
        events.extend(extracted)
        missing += skipped
    # Listing cards often have no headings, e.g. <li>Craft Fair — Nov 14 at 9am</li>.
    for card in cards[:120]:
        row = " ".join(card)
        if not (_TEXT_DATE.search(row) or _TEXT_DATES_ISO.search(row) or _TEXT_DATES_NUMERIC.search(row)):
            continue
        date_match = _TEXT_DATE.search(row) or _TEXT_DATES_ISO.search(row) or _TEXT_DATES_NUMERIC.search(row)
        title = row[:date_match.start()].rstrip(" ,-–—|:")
        title = re.sub(r"(?:\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))\s*,?$",
                       "", title, flags=re.I).strip(" ,-–—|:")
        if " — " in title:
            title = title.split(" — ", 1)[0].strip()
        if not title or _UNHELPFUL_HEADINGS.fullmatch(title):
            continue
        extracted, skipped = _text_event_group(title[:120], [row], page_title,
                                               page_url, source_name, retrieved_at, zone)
        events.extend(extracted)
        missing += skipped
    # A page can expose the same event in text, JSON-LD, a list item and a heading.
    distinct = {(e.title.casefold(), e.starts_at): e for e in events}
    return list(distinct.values())[:MAX_EVENTS], missing



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
        {"name": "max_pages", "label": "Maximum pages per scan (all websites)", "kind": "integer",
         "default": 12, "min": 1, "max": MAX_PAGES},
        {"name": "max_depth", "label": "Link levels to follow", "kind": "integer",
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
            raise ValueError("max_pages must be at least the number of website URLs, so every site can be checked")
        return urls[0], values

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
        queue = deque((url, 0, url, None) for url in cfg["urls"])
        seen = set()
        candidates = {}
        leads = []
        errors = []
        rejected_dates = 0
        unstructured_events = 0
        limit = cfg["max_pages"]
        max_depth = cfg["max_depth"]

        while queue and len(seen) < limit and len(candidates) < cfg["max_events"]:
            page_url, depth, seed, found_on = queue.popleft()
            if page_url in seen:
                continue
            seen.add(page_url)
            try:
                response = fetcher.fetch(page_url)
            except FetchError as error:
                errors.append(str(error)[:200])
                leads.append(SourceLead(page_url, "seed" if found_on is None else "event",
                    "rejected", (str(error)[:200],), found_on=found_on, fetched=False))
                continue
            page = response.text
            media = (response.content_type or "").lower()
            before = len(candidates)
            discovered = []
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
                    visible, from_text = _plain_text_events(page, page_url, name, retrieved_at,
                                                            cfg.get("timezone"))
                    # Favor exact structured source claims when the same event is also visible as text.
                    existing = {(e.title.casefold(), e.starts_at) for e in events}
                    text_events = [e for e in visible if (e.title.casefold(), e.starts_at) not in existing]
                    events.extend(text_events)
                    events = _attach_page_image(events, page, page_url, fetcher, self.image_extractor)
                    unstructured_events += len(text_events)
                    rejected += from_text
                except (ValueError, TypeError):
                    events, rejected = [], 0
            rejected_dates += rejected
            for event in events:
                if event.local_id not in candidates and len(candidates) < cfg["max_events"]:
                    candidates[event.local_id] = event
            leads.append(SourceLead(page_url, "seed" if found_on is None else "event",
                "accepted", ("public page inspected for offset-aware event data",),
                score=10 if events else 0, found_on=found_on, events=len(candidates)-before, fetched=True))
            if depth >= max_depth:
                continue
            added = 0
            for link, title in discovered:
                if added >= MAX_LINKS or len(seen) + len(queue) >= limit * 3:
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
                if target in seen or any(row[0] == target for row in queue):
                    continue
                queue.append((target, depth + 1, seed, page_url))
                added += 1
        if not any(lead.fetched for lead in leads) and errors:
            raise FetchError("all configured websites failed: " + "; ".join(errors[:2]))
        return ProducerResult(tuple(candidates.values()), tuple(leads),
            {"sites": len(cfg["urls"]), "pages": sum(v.fetched for v in leads),
             "candidates": len(candidates), "invalid_or_ambiguous_event_dates": rejected_dates,
             "failed_pages": len(errors), "links_remaining": len(queue),
             "unstructured_candidates": unstructured_events})
