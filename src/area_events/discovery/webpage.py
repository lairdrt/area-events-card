"""What a fetched web page states about events, read the same way by every producer and by the shared event-page finder: its JSON-LD Event documents,
its scripts and links, its visible text (with what sits in a footer or contact block marked), and street addresses written in it.

Nothing here fetches, decides or stores; it only reads markup. A producer is free to read more of a page in its own way.
"""
import re
from html import unescape
from html.parser import HTMLParser

JSONLD_TYPE = re.compile(r"(?:^|[/#])Event$", re.I)
# schema.org's Event and its kinds (FoodEvent, SocialEvent, Festival, EventSeries, ...), for a reader that takes every kind as an event
EVENT_KINDS = re.compile(r"(?:^|[/#])(?:\w*Event|Festival|EventSeries|CourseInstance|Hackathon)$", re.I)
# A street address as a contact block writes it: house number and street, then "City, ST 12345" ("15602 Olde Highway 80, Flinn Springs, CA 92021").
STREET_ADDRESS = re.compile(r"(?P<street>\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9 .'’#-]{1,60}?),\s*(?P<city>[A-Za-z][A-Za-z .'’-]{1,40}),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?\b")
_STREET_TYPES = (r"St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Way|Ct|Court|Pl|Place|Pkwy|Parkway|Hwy|Highway|Trail|Trl|Cir|Circle|"
                 r"Ter|Terrace|Loop|Pike|Row|Sq|Square")
# The street line of an address, however much follows it: '9990 AleSmith Court', '1068 E 10th St F', '3400 E 8th St Suite 115'
STREET_LINE = re.compile(r"(?<![\w#$.,])\d{1,6}[A-Za-z]?\s+(?:[A-Za-z0-9][A-Za-z0-9.'’-]*\s+){0,5}?(?:" + _STREET_TYPES + r")\b\.?"
                         r"(?:\s+(?:[NSEW]|NE|NW|SE|SW)\b\.?)?(?:\s*(?:#|Suite|Ste\.?|Unit)\s*[\w-]+|\s+[A-Z](?![\w'’]))?")
_SPACE = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]*>")
_HIDDEN = {"script", "style", "noscript", "svg", "template", "iframe"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def _schema_type(value, kinds):
    items = value if isinstance(value, list) else [value]
    return any(isinstance(v, str) and kinds.search(v) for v in items)


def event_documents(payload, kinds=JSONLD_TYPE):
    """Find @type Event in bounded JSON-LD graphs and list elements."""
    stack = [payload]
    seen = 0
    while stack and seen < 1000:
        item = stack.pop()
        seen += 1
        if isinstance(item, list):
            stack.extend(reversed(item[:300]))
        elif isinstance(item, dict):
            if _schema_type(item.get("@type"), kinds):
                yield item
            # Don't scan arbitrary giant map values: these are JSON-LD graph edges.
            for key in ("@graph", "itemListElement", "mainEntity", "subjectOf", "event", "events"):
                if key in item:
                    stack.append(item[key])
            if item.get("@type") == "ListItem" and "item" in item:
                stack.append(item["item"])


def _label(value, limit):
    return _SPACE.sub(" ", unescape(_TAG.sub(" ", value))).strip()[:limit] or None


class PageParts(HTMLParser):
    """A page's JSON-LD scripts and its links: (href, anchor text) for anchors, (href, rel) for next/alternate/calendar links."""

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
            self.links.append((self._anchor[0], _label(" ".join(self._anchor[1]), 150) or ""))
            self._anchor = None


class PageText(HTMLParser):
    """A page's visible text as the pieces its markup writes, each (text, aside): `aside` when it sits in a <footer>, an element whose class or id
    names a footer, an <address> (contact information, by HTML's definition) or <nav>. Also the page's <title>, its <h1> headings and its
    og:title and og:site_name."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pieces, self.headings, self.title, self.site_name = [], [], "", None
        self._open = []          # (tag, hidden, aside)
        self._in_title, self._h1 = False, None

    def _state(self):
        return (any(h for _, h, _ in self._open), any(a for _, _, a in self._open))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key, value = (attrs.get("property") or attrs.get("name") or "").lower(), attrs.get("content") or ""
            if key == "og:title" and value.strip():
                self.headings.append(_SPACE.sub(" ", value).strip())
            elif key == "og:site_name" and value.strip():
                self.site_name = _SPACE.sub(" ", value).strip()
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in _VOID:
            return
        names = f"{attrs.get('class') or ''} {attrs.get('id') or ''}".lower()
        self._open.append((tag, tag in _HIDDEN, tag in ("footer", "address", "nav") or "footer" in names))
        if tag == "h1":
            self._h1 = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if tag == "h1" and self._h1 is not None:
            text = _SPACE.sub(" ", "".join(self._h1)).strip()
            if text:
                self.headings.append(text)
            self._h1 = None
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                del self._open[index:]
                break

    def handle_data(self, data):
        if self._in_title:
            self.title = _SPACE.sub(" ", self.title + " " + data).strip()
            return
        hidden, aside = self._state()
        if hidden:
            return
        if self._h1 is not None:
            self._h1.append(data)
        text = _SPACE.sub(" ", data).strip()
        if text:
            self.pieces.append((text, aside))


def page_text(html):
    """The PageText of a page (at most its first 1.5 MB)."""
    reader = PageText()
    try:
        reader.feed(html[:1_500_000])
        reader.close()
    except (ValueError, TypeError, AssertionError):
        pass
    return reader
