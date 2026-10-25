"""Reading event data out of an HTML page without running it: JSON-LD, microdata, links and listing cards.

Everything here is a pure function of the page text. A *candidate* is a plain dict (title, start, end, tz, description,
venue, address, url, image, image_source, organizer, geo, categories, price, online, cancelled, uid, kind); nothing is
filled in that the page does not state. The adapter (web.py) turns candidates into events.
"""
import json
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urldefrag

from ...common.textutil import html_to_text

MAX_NODES = 60000
MAX_DEPTH = 150
MAX_JSONLD = 60
MAX_CANDIDATES = 400
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SIBLING_CLOSES = {"li", "p", "dt", "dd", "tr", "td", "th", "option"}
HEADINGS = {"h1", "h2", "h3", "h4"}
GENERIC_LINK = re.compile(r"^(read more|more|more info|more information|details|learn more|tickets?|buy tickets?|register|rsvp|view|view event|"
                          r"see more|click here|info|get tickets?)\.?$", re.I)
EVENT_WORDS = re.compile(r"venue|location|where|place", re.I)
SKIP_SLUG = {"page", "category", "categories", "tag", "tags", "past", "upcoming", "list", "month", "week", "day", "today", "feed",
             "ical", "calendar", "archive", "archives", "all", "search", "submit", "add", "login", "register"}
EVENT_PATH = re.compile(r"/(?:events?|calendar|whats-on|what-s-on|shows?|performances?|programs?|exhibitions?|happenings)/[^/?#]+", re.I)
TYPE_WORDS = {"MusicEvent": "Music", "TheaterEvent": "Theater", "SportsEvent": "Sports", "Festival": "Festival", "FoodEvent": "Food",
              "ComedyEvent": "Comedy", "DanceEvent": "Dance", "EducationEvent": "Education", "ExhibitionEvent": "Exhibition",
              "ChildrensEvent": "Children", "LiteraryEvent": "Literary", "SocialEvent": "Social", "VisualArtsEvent": "Visual arts",
              "ScreeningEvent": "Film", "BusinessEvent": "Business", "SaleEvent": "Sale", "PublicationEvent": "Publication"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "data", "depth")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children, self.data = tag, attrs, parent, [], ""
        self.depth = parent.depth + 1 if parent else 0

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
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if self.count >= MAX_NODES:
            return
        self.count += 1
        if tag in SIBLING_CLOSES and self.cur.tag == tag and self.cur.parent is not None:
            self.cur = self.cur.parent
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID and node.depth < MAX_DEPTH:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        before = self.cur
        self.handle_starttag(tag, attrs)
        self.cur = before

    def handle_endtag(self, tag):
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        if self.cur.tag == "script":
            self.cur.data += data
        elif self.cur.tag != "style" and data.strip():
            self.cur.children.append(data)


def parse_html(html):
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


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


# --- schema.org --------------------------------------------------------------------------------------

def _types(node):
    raw = node.get("@type")
    raw = raw if isinstance(raw, list) else [raw]
    return [re.split(r"[/#:]", t)[-1] for t in raw if isinstance(t, str)]


def _is_event(types):
    return any(t.endswith("Event") or t == "Festival" for t in types)


def _image(value, base):
    if isinstance(value, list):
        for item in value:
            found = _image(item, base)
            if found:
                return found
        return None
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
    if street:
        return ", ".join(part for part in (street, tail) if part) or None
    return city if city and not region else None


def _price(node):
    offers = node.get("offers")
    offers = offers if isinstance(offers, list) else [offers]
    if node.get("isAccessibleForFree") is True:
        return "Free"
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        price = offer.get("price", offer.get("lowPrice"))
        try:
            amount = float(str(price).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if amount == 0:
            return "Free"
        currency = (clean(offer.get("priceCurrency")) or "").upper()
        text = f"{amount:g}" if amount != int(amount) or amount > 1e6 else str(int(amount))
        return f"${text}" if currency in ("USD", "") else f"{currency} {text}"
    return None


def candidate_from_ld(node, base, ids, kind="jsonld"):
    """A candidate from one schema.org Event object (JSON-LD node or microdata item)."""
    def ref(value):
        if isinstance(value, dict) and set(value) <= {"@id"} and value.get("@id") in ids:
            return ids[value["@id"]]
        return value

    locations = node.get("location")
    locations = [ref(v) for v in (locations if isinstance(locations, list) else [locations]) if v]
    physical = next((v for v in locations if isinstance(v, (dict, str)) and not (isinstance(v, dict) and "VirtualLocation" in _types(v))), None)
    mode = " ".join(m for m in (node.get("eventAttendanceMode") if isinstance(node.get("eventAttendanceMode"), list) else [node.get("eventAttendanceMode")]) if isinstance(m, str))
    online = ("OnlineEventAttendanceMode" in mode and "Offline" not in mode and "Mixed" not in mode) or (bool(locations) and physical is None)
    venue = address = geo = None
    if isinstance(physical, str):
        venue = clean(physical)
        if venue and re.search(r"\d", venue) and "," in venue:
            venue, address = None, venue
    elif isinstance(physical, dict):
        venue = clean(physical.get("name"))
        address = _address(physical, venue)
        point = ref(physical.get("geo"))
        try:
            geo = {"latitude": float(point["latitude"]), "longitude": float(point["longitude"]), "source": "schema.org geo"}
        except (KeyError, TypeError, ValueError):
            geo = None
    organizer = ref(node.get("organizer"))
    organizer = organizer[0] if isinstance(organizer, list) and organizer else organizer
    status = str(node.get("eventStatus") or "")
    keywords = node.get("keywords")
    keywords = [k.strip() for k in re.split(r"[,;]", keywords)] if isinstance(keywords, str) else [k for k in keywords if isinstance(k, str)] if isinstance(keywords, list) else []
    categories = [TYPE_WORDS[t] for t in _types(node) if t in TYPE_WORDS] + [k for k in keywords if 1 < len(k) <= 40][:5]
    description = clean(node.get("description"))
    return {
        "kind": kind, "title": clean(node.get("name") or node.get("headline")), "start": clean(node.get("startDate")), "end": clean(node.get("endDate")),
        "tz": None, "description": description[:1500] if description else None, "venue": venue, "address": address,
        "url": absolute(base, node.get("url") if isinstance(node.get("url"), str) else None), "image": _image(node.get("image"), base),
        "image_source": f"{'JSON-LD' if kind == 'jsonld' else 'microdata'} image property" if _image(node.get("image"), base) else None,
        "organizer": clean(ref(organizer)) if organizer else None, "geo": geo, "categories": list(dict.fromkeys(categories)),
        "price": _price(node), "online": online, "cancelled": bool(re.search(r"Cancel+ed|Postponed", status)),
        "uid": clean(node.get("@id")) if isinstance(node.get("@id"), str) else None,
    }


def candidates_from_ld(documents, base, kind="jsonld"):
    """(candidates, detail_links) from parsed JSON-LD documents: @graph, lists, ItemLists and nested events are all searched."""
    ids = {}
    for document in documents:
        stack = [document]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if isinstance(item.get("@id"), str) and len(item) > 1:
                    ids.setdefault(item["@id"], item)
                stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
            elif isinstance(item, list):
                stack.extend(item)
    found, links, seen = [], [], set()

    def collect(item, depth):
        if depth > 8 or len(found) >= MAX_CANDIDATES:
            return
        if isinstance(item, list):
            for entry in item[:300]:
                collect(entry, depth + 1)
        elif isinstance(item, dict):
            if id(item) in seen:
                return
            seen.add(id(item))
            types = _types(item)
            if _is_event(types):
                candidate = candidate_from_ld(item, base, ids, kind)
                if candidate["start"] and candidate["title"]:
                    found.append(candidate)
                elif candidate["url"]:
                    links.append(candidate["url"])
                collect(item.get("subEvent"), depth + 1)
                return
            if "ListItem" in types and isinstance(item.get("url"), str) and not item.get("item"):
                url = absolute(base, item["url"])
                if url:
                    links.append(url)
            for key, value in item.items():
                if key != "@context" and isinstance(value, (dict, list)):
                    collect(value, depth + 1)

    for document in documents:
        collect(document, 0)
    return found, list(dict.fromkeys(links))


def _json_documents(root):
    documents, malformed = [], 0
    for node in root.walk():
        if node.tag == "script" and "ld+json" in node.attrs.get("type", "").lower() and node.data.strip():
            if len(documents) + malformed >= MAX_JSONLD:
                break
            text = re.sub(r"^\s*(<!--|//<!\[CDATA\[)|(-->|//\]\]>)\s*$", "", node.data.strip())
            try:
                documents.append(json.loads(text, strict=False))
            except ValueError:
                malformed += 1
    return documents, malformed


# --- microdata ---------------------------------------------------------------------------------------

def _prop_value(node, base):
    tag = node.tag
    if tag == "meta":
        return node.attrs.get("content")
    if tag == "time":
        return node.attrs.get("datetime") or node.text()
    if tag in ("a", "link", "area"):
        return absolute(base, node.attrs.get("href")) or node.text()
    if tag in ("img", "source"):
        return absolute(base, node.attrs.get("src"))
    return node.attrs.get("content") or node.text()


def _micro(node, base):
    item = {"@type": re.split(r"[/#]", node.attrs.get("itemtype", "").split()[0] if node.attrs.get("itemtype") else "")[-1]}

    def visit(parent):
        for child in parent.children:
            if not isinstance(child, Node):
                continue
            prop = child.attrs.get("itemprop")
            if prop:
                value = _micro(child, base) if "itemscope" in child.attrs else _prop_value(child, base)
                for name in prop.split():
                    item.setdefault(name, value)
                if "itemscope" not in child.attrs:
                    visit(child)  # properties may sit inside other properties (a name inside a link to the event)
            elif "itemscope" not in child.attrs:
                visit(child)

    visit(node)
    return item


def micro_events(root, base):
    found = []
    for node in root.walk():
        kind = node.attrs.get("itemtype", "")
        if "itemscope" in node.attrs and re.search(r"schema\.org/\w*(Event|Festival)\s*$", kind.strip()):
            item = _micro(node, base)
            found.append(item)
            if len(found) >= MAX_CANDIDATES:
                break
    return found


# --- listing cards -----------------------------------------------------------------------------------

def _first(node, predicate):
    return next((n for n in node.walk() if predicate(n)), None)


def html_cards(root, base):
    """Candidates from repeated blocks that hold a <time datetime> and a titled link (the least reliable source)."""
    cards, used = [], set()
    for time_node in root.walk():
        if time_node.tag != "time" or not time_node.attrs.get("datetime"):
            continue
        ancestor = time_node.parent
        for _ in range(6):
            if ancestor is None or ancestor.tag in ("body", "html", "#root", "main"):
                ancestor = None
                break
            times = {t.attrs["datetime"] for t in ancestor.walk() if t.tag == "time" and t.attrs.get("datetime")}
            if len(times) > 2:
                ancestor = None
                break
            if any(a.tag == "a" and a.attrs.get("href") and len(a.text()) >= 3 for a in ancestor.walk()) or _first(ancestor, lambda n: n.tag in HEADINGS):
                break
            ancestor = ancestor.parent
        if ancestor is None or id(ancestor) in used:
            continue
        used.add(id(ancestor))
        heading = _first(ancestor, lambda n: n.tag in HEADINGS and n.text())
        anchors = [a for a in ancestor.walk() if a.tag == "a" and a.attrs.get("href") and absolute(base, a.attrs["href"])]
        titled = [a for a in anchors if a.text() and not GENERIC_LINK.match(a.text())]
        title = heading.text() if heading else (titled[0].text() if titled else None)
        link = None
        if heading:
            link = _first(heading, lambda n: n.tag == "a" and n.attrs.get("href")) or next((a for a in titled if a.text() == title), None)
        link = link or (titled[0] if titled else (anchors[0] if anchors else None))
        times = sorted({t.attrs["datetime"] for t in ancestor.walk() if t.tag == "time" and t.attrs.get("datetime")})
        image = _first(ancestor, lambda n: n.tag == "img" and (n.attrs.get("src") or n.attrs.get("data-src")))
        venue = _first(ancestor, lambda n: n is not time_node and n.tag != "time" and EVENT_WORDS.search(n.attrs.get("class", "") + " " + n.attrs.get("itemprop", "")) and 2 < len(n.text()) <= 120)
        summary = _first(ancestor, lambda n: n.tag == "p" and len(n.text()) >= 40)
        if not title or len(title) > 200:
            continue
        picture = absolute(base, image.attrs.get("src") or image.attrs.get("data-src")) if image else None
        cards.append({
            "kind": "html", "title": title, "start": times[0], "end": times[1] if len(times) > 1 else None, "tz": None,
            "description": summary.text()[:1500] if summary else None, "venue": venue.text() if venue else None, "address": None,
            "url": absolute(base, link.attrs["href"]) if link else None, "image": picture,
            "image_source": "image in the listing block" if picture else None, "organizer": None, "geo": None, "categories": [],
            "price": None, "online": False, "cancelled": False, "uid": None,
        })
        if len(cards) >= MAX_CANDIDATES:
            break
    return cards


# --- page scan ---------------------------------------------------------------------------------------

FEED_TYPES = {"text/calendar": "ics", "application/rss+xml": "feed", "application/atom+xml": "feed"}


class Page:
    pass


def feed_kind(url, type_=""):
    parts = urlsplit(url)
    kind = FEED_TYPES.get((type_ or "").split(";")[0].strip().lower())
    if kind:
        return kind
    if parts.path.lower().endswith(".ics") or parts.scheme == "webcal" or re.search(r"(^|&)(ical|ics|outlook-ical|format=ical|format=ics)", parts.query, re.I):
        return "ics"
    return None


def event_like(url):
    """Does the address look like a page about one event (a slug under an events path)?"""
    parts = urlsplit(url)
    match = EVENT_PATH.search(parts.path)
    if not match:
        return False
    slug = match.group(0).rsplit("/", 1)[-1].lower()
    return slug not in SKIP_SLUG and (bool(re.search(r"[-\d]", slug)) or len(slug) > 12)


def scan(html, base):
    page = Page()
    page.root = root = parse_html(html)
    page.title = clean(next((n.text() for n in root.walk() if n.tag == "title"), None))
    meta = {}
    for n in root.walk():
        if n.tag == "meta" and n.attrs.get("content"):
            meta.setdefault((n.attrs.get("property") or n.attrs.get("name") or "").lower(), n.attrs["content"])
    page.meta = meta
    page.site_name = clean(meta.get("og:site_name")) or None
    documents, page.malformed = _json_documents(root)
    page.jsonld, page.links = candidates_from_ld(documents, base)
    page.micro = [candidate_from_ld(item, base, {}, kind="microdata") for item in micro_events(root, base)]
    page.micro = [c for c in page.micro if c["start"] and c["title"]]
    page.feeds, page.anchors, page.next_url = [], [], None
    for n in root.walk():
        if n.tag == "link" and n.attrs.get("href"):
            rel = n.attrs.get("rel", "").lower().split()
            url = absolute(base, n.attrs["href"].replace("webcal:", "https:", 1))
            if "alternate" in rel and url and feed_kind(url, n.attrs.get("type")):
                page.feeds.append((url, feed_kind(url, n.attrs.get("type"))))
            elif "next" in rel and url and not page.next_url:
                page.next_url = url
        elif n.tag == "a" and n.attrs.get("href"):
            raw = n.attrs["href"].strip()
            url = absolute(base, raw.replace("webcal:", "https:", 1) if raw.lower().startswith("webcal:") else raw)
            if not url:
                continue
            if feed_kind(url) == "ics":
                page.feeds.append((url, "ics"))
            elif "next" in n.attrs.get("rel", "").lower().split() and not page.next_url:
                page.next_url = url
            else:
                page.anchors.append((url, n.text()))
    page.feeds = list(dict.fromkeys(page.feeds))
    return page


def detail_links(page, base_host):
    """Same-site addresses on the page that look like single-event pages (after those the structured data named)."""
    out = list(page.links)
    for url, _ in page.anchors:
        if urlsplit(url).hostname == base_host and event_like(url) and url not in out:
            out.append(url)
    return out
