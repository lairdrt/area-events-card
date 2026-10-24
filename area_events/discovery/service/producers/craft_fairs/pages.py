"""How the craft-fair producer reads a web page: an HTML tree, the schema.org Event data in it, and the date and clock patterns people write in text.

Everything here is this producer's own and deliberately small: it reads what the craft-fair pilot needs (an element tree with text, links and meta tags; JSON-LD Event objects; a
written date and clock time) and nothing else. It depends on nothing but the standard library and the neutral text and time utilities.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

from ....textutil import html_to_text
from ....timeutil import localize_pacific

MAX_NODES = 60000
MAX_DEPTH = 150
MAX_JSONLD = 60
MAX_TITLE = 120
VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"))
SIBLING_CLOSES = frozenset(("li", "p", "dt", "dd", "tr", "td", "th", "option"))
MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
_MONTH = r"(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_WEEKDAY = r"(?:(?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)[a-z]*\.?,?\s+)?"
DATE = re.compile(r"\b" + _WEEKDAY + _MONTH + r"\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?:\s*,?\s*(?P<year>20\d{2})\b)?", re.I)
TIME = re.compile(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\b\.?|\bnoon\b", re.I)
TIME_RANGE = re.compile(r"(\d{1,2}(?::\d{2})?\s*(?:[ap]\.?\s?m\.?)?\s*(?:-|–|—|to)\s*\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\.?|\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\.?|noon)", re.I)
NOT_AN_EVENT = re.compile(r"\b(posted|published|updated|copyright|deadline|due by|due on|closes|closed|expires|registration closes|last updated|archive)\b|©", re.I)
LABEL = re.compile(r"\b(?:Location|Where|Venue|Place)\s*:\s*([^|•\n]{3,90}?)(?=\s{2,}|\s*[|•]|$)", re.I)
NAVIGATION = frozenset(("calendar", "events", "event", "home", "upcoming events", "read more", "more", "learn more", "view all", "see all", "menu", "search", "news",
                        "contact", "about", "donate", "give", "login", "sign in", "register", "subscribe", "today", "next", "previous", "back"))
WHEN = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})[T ](\d{2}):?(\d{2})(?::?(\d{2})(?:[.,]\d+)?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$", re.I)


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "data")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children, self.data = tag, attrs, parent, [], ""

    def text(self):
        out, stack = [], [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                out.append(item)
            elif item.tag not in ("script", "style"):
                stack.extend(reversed(item.children))
        return re.sub(r"\s+", " ", "".join(out)).strip()

    def walk(self):
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed([c for c in node.children if isinstance(c, Node)]))


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.cur = Node("#root", {}, None)
        self.count = self.depth = 0

    def handle_starttag(self, tag, attrs):
        if self.count >= MAX_NODES:
            return
        self.count += 1
        if tag in SIBLING_CLOSES and self.cur.tag == tag and self.cur.parent is not None:
            self.cur, self.depth = self.cur.parent, self.depth - 1
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID and self.depth < MAX_DEPTH:
            self.cur, self.depth = node, self.depth + 1

    def handle_startendtag(self, tag, attrs):
        before, depth = self.cur, self.depth
        self.handle_starttag(tag, attrs)
        self.cur, self.depth = before, depth

    def handle_endtag(self, tag):
        node, up = self.cur, 0
        while node is not None and node.tag != tag:
            node, up = node.parent, up + 1
        if node is not None and node.parent is not None:
            self.cur, self.depth = node.parent, self.depth - up - 1

    def handle_data(self, data):
        if self.cur.tag == "script":
            self.cur.data += data
        elif self.cur.tag != "style" and data.strip():
            self.cur.children.append(data)


def absolute(base, href):
    """An http(s) address resolved against `base`, without a fragment; None for anything else."""
    if not isinstance(href, str) or not href.strip() or len(href) > 2000:
        return None
    url = urldefrag(urljoin(base, unescape(href.strip())))[0]
    return url if urlsplit(url).scheme in ("http", "https") and urlsplit(url).hostname else None


def clean(value):
    if isinstance(value, dict):
        value = value.get("@value") or value.get("name") or value.get("text")
    if isinstance(value, list):
        value = next((v for v in value if v), None)
    if not isinstance(value, str) or not value.strip():
        return None
    text = html_to_text(value) if "<" in value or "&" in value else value
    return re.sub(r"\s+", " ", text).strip() or None


@dataclass
class Page:
    root: Node
    title: str = None
    meta: dict = field(default_factory=dict)
    site_name: str = None
    events: list = field(default_factory=list)   # schema.org Event objects the page carries, as plain dictionaries (see `_event_of`)


def read_page(html, base):
    root = _Builder()
    root.feed(html)
    root.close()
    page = Page(root.root)
    page.title = clean(next((n.text() for n in page.root.walk() if n.tag == "title"), None))
    for n in page.root.walk():
        if n.tag == "meta" and n.attrs.get("content"):
            page.meta.setdefault((n.attrs.get("property") or n.attrs.get("name") or "").lower(), n.attrs["content"])
    page.site_name = clean(page.meta.get("og:site_name")) or None
    page.events = _events_of(page.root, base)
    return page


# --- schema.org Event objects (JSON-LD) ---------------------------------------------------------------------------

def _types(node):
    raw = node.get("@type")
    return [re.split(r"[/#:]", t)[-1] for t in (raw if isinstance(raw, list) else [raw]) if isinstance(t, str)]


def _image(value, base):
    if isinstance(value, list):
        return next((found for found in (_image(item, base) for item in value) if found), None)
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or value.get("@id")
    return absolute(base, value) if isinstance(value, str) else None


def _address(location, venue):
    raw = location.get("address") if isinstance(location, dict) else None
    if isinstance(raw, str):
        return clean(raw)
    if not isinstance(raw, dict):
        return None
    street, city = clean(raw.get("streetAddress")), clean(raw.get("addressLocality"))
    region, postal = clean(raw.get("addressRegion")), clean(raw.get("postalCode"))
    tail = " ".join(part for part in (region, postal) if part)
    lead = street or venue
    if lead and city:
        return ", ".join(part for part in (lead, city, tail) if part)
    return (", ".join(part for part in (street, tail) if part) or None) if street else (city if city and not region else None)


def _event_of(node, base):
    locations = node.get("location")
    place = next((v for v in (locations if isinstance(locations, list) else [locations]) if isinstance(v, dict) and "VirtualLocation" not in _types(v)), None)
    venue = clean(place.get("name")) if place else None
    organizer = node.get("organizer")
    organizer = organizer[0] if isinstance(organizer, list) and organizer else organizer
    description = clean(node.get("description"))
    return {"title": clean(node.get("name") or node.get("headline")), "start": clean(node.get("startDate")), "end": clean(node.get("endDate")), "venue": venue,
            "address": _address(place, venue), "description": description[:1500] if description else None, "image": _image(node.get("image"), base),
            "organizer": clean(organizer) if organizer else None}


def _events_of(root, base):
    documents = []
    for node in root.walk():
        if node.tag == "script" and "ld+json" in node.attrs.get("type", "").lower() and node.data.strip() and len(documents) < MAX_JSONLD:
            try:
                documents.append(json.loads(re.sub(r"^\s*(<!--|//<!\[CDATA\[)|(-->|//\]\]>)\s*$", "", node.data.strip()), strict=False))
            except ValueError:
                continue
    found, stack = [], list(reversed(documents))
    while stack and len(found) < 50:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item[:300]))
        elif isinstance(item, dict):
            if any(t.endswith("Event") or t == "Festival" for t in _types(item)):
                event = _event_of(item, base)
                if event["start"] and event["title"]:
                    found.append(event)
            stack.extend(v for k, v in reversed(list(item.items())) if k != "@context" and isinstance(v, (dict, list)))
    return found


def when(value):
    """An aware datetime for an ISO 8601 date-time with a clock time: a stated UTC offset is used exactly, a time without one is Pacific wall-clock time. None for a date alone."""
    match = WHEN.match(str(value or "").strip())
    if not match:
        return None
    year, month, day, hour, minute, second, offset = match.groups()
    try:
        naive = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
    except ValueError:
        return None
    if not offset:
        return localize_pacific(naive)
    if offset.upper() == "Z":
        return naive.replace(tzinfo=timezone.utc)
    digits = offset[1:].replace(":", "")
    return naive.replace(tzinfo=timezone((-1 if offset[0] == "-" else 1) * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))))


# --- dates and titles written in text -----------------------------------------------------------------------------

def month_number(name):
    name = name.lower().rstrip(".")
    return next((index for index, full in enumerate(MONTH_NAMES, 1) if full.startswith(name[:3])), None)


def day_of(month, day, year, today):
    """The calendar date a month/day (and perhaps year) names; None when it is not a real one or is history (a date without a year that has passed is next year's only in
    the last weeks of a year)."""
    try:
        if not year:
            candidate = date(today.year, month, int(day))
            if candidate < today - timedelta(days=1):
                following = date(today.year + 1, month, int(day))
                return following if (following - today).days <= 120 else None
            return candidate
        return date(int(year), month, int(day))
    except ValueError:
        return None


def valid_title(title):
    if not title or len(title) < 4 or len(title) > MAX_TITLE:
        return False
    if title.lower().strip(".:!") in NAVIGATION:
        return False
    if not re.search(r"[A-Za-z]{3,}", title) or sum(ch.isdigit() for ch in title) > len(title) / 2:
        return False
    return not NOT_AN_EVENT.search(title)
