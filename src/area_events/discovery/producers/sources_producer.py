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
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
                    local_id = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:32]
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


class SourcesProducer:
    adapter = "sources_producer"
    extractor = "producers/sources_producer@v1"
    method = "adapter"
    display_name = "Website event sources (generic)"
    configuration_fields = (
        {"name": "urls", "label": "Website URLs (one per line)", "kind": "url_list", "required": True,
         "placeholder": "https://example.org/events\nhttps://another.org/calendar"},
        {"name": "max_pages", "label": "Maximum pages per scan (all websites)", "kind": "integer",
         "default": 12, "min": 1, "max": MAX_PAGES},
        {"name": "max_depth", "label": "Link levels to follow", "kind": "integer",
         "default": 1, "min": 0, "max": MAX_DEPTH},
        {"name": "max_events", "label": "Maximum events per scan", "kind": "integer",
         "default": 75, "min": 1, "max": MAX_EVENTS},
    )

    @staticmethod
    def validate_options(options):
        if not isinstance(options, dict):
            raise ValueError("options must be an object")
        unknown = sorted(set(options) - {"url", "urls", "max_pages", "max_depth", "max_events"})
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
            "max_pages": _integer(options.get("max_pages"), "max_pages", 12, 1, MAX_PAGES),
            "max_depth": _integer(options.get("max_depth"), "max_depth", 1, 0, MAX_DEPTH),
            "max_events": _integer(options.get("max_events"), "max_events", 75, 1, MAX_EVENTS),
        }
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
                rejected_dates += rejected
            elif "xml" in media or page.lstrip().startswith(("<?xml", "<rss", "<feed")):
                events, rejected = [], 0
                discovered = _feed_links(page)
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
             "failed_pages": len(errors), "links_remaining": len(queue)})
