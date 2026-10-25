"""Finding where a website keeps its events, from the pages it already gave us. Pure functions on text and links; the website adapter does
the (bounded, polite) fetching.

* `unwrap`: some site builders answer a page request with JSON whose `content` field holds the page's HTML; this returns that HTML.
* `embedded_calendars`: public Google Calendar embeds (an iframe, or a site builder's calendar widget) and the calendar's published feed.
* `rank_links`: same-site links that look like an events calendar, an upcoming-events page, a bulletin or a single event, best first;
  navigation, accounts, giving, shops, archives and feeds never score.
* `document_links`: linked PDFs and flyer images, with whether their name or wording says they are about events.
"""
import base64
import binascii
import json
import re
from urllib.parse import parse_qs, quote, unquote, urlsplit

GOOD_PATH = re.compile(r"(?:^|[/_.-])(events?|calendars?|upcoming|whats?-?on|happenings|schedule|bulletins?|programs?|activities|festivals?|fairs?|markets?|"
                       r"concerts?|shows?|performances?|classes|workshops?|gatherings?|community|announcements?|news-?events?|fundraisers?)(?:$|[/_.-])", re.I)
GOOD_TEXT = re.compile(r"\b(events?|calendar|upcoming|what'?s on|happenings|schedule|bulletin|festival|fair|market|concerts?|performances?|"
                       r"activities|announcements?|fundraiser|things to do)\b", re.I)
BAD_PATH = re.compile(r"(?:^|[/_.-])(login|log-in|signin|sign-in|account|cart|checkout|shop|store|donate|giving|give|privacy|terms|cookies?|contact|about|"
                      r"careers?|jobs?|staff|directory|search|tag|tags|category|categories|author|feed|rss|wp-json|wp-admin|archives?|past|sitemap|"
                      r"policy|policies|faq|press|login|register|subscribe|unsubscribe|print|share|mailto|tel)(?:$|[/_.-])", re.I)
BAD_TEXT = re.compile(r"\b(past events?|archive|log ?in|sign ?in|donate|give now|privacy|terms|contact us|careers|search|shop|cart)\b", re.I)
YEAR_ARCHIVE = re.compile(r"/(19\d{2}|20[01]\d|202[0-4])(?:/|$|-)")
SKIP_SUFFIX = (".css", ".js", ".json", ".xml", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".zip", ".mp3", ".mp4", ".doc", ".docx", ".xls", ".xlsx")
EVENTISH_DOC = re.compile(r"\b(flyers?|events?|calendar|schedule|bulletin|newsletter|program|festival|fair|market|concert|invitation|poster|brochure|save the date)\b", re.I)
IMAGE_SUFFIX = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def unwrap(text):
    """The HTML inside a JSON page wrapper (`{"content": "<html>..."}`), else the text unchanged."""
    if not text or text.lstrip()[:1] != "{":
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    content = data.get("content") if isinstance(data, dict) else None
    return content if isinstance(content, str) and "<" in content else text


def _ical_url(calendar_id):
    calendar_id = calendar_id.strip()
    if not re.fullmatch(r"[\w.%+@-]{3,200}", calendar_id):
        return None
    return f"https://calendar.google.com/calendar/ical/{quote(unquote(calendar_id), safe='@')}/public/basic.ics"


def embedded_calendars(html):
    """Published feed addresses for the public Google Calendars embedded in a page (at most three)."""
    found = []
    for match in re.finditer(r"calendar\.google\.com/calendar/(?:u/\d+/)?embed\?([^\"'<>\s]+)", html or ""):
        query = parse_qs(match.group(1).replace("&amp;", "&"))
        for value in query.get("src", []):
            url = _ical_url(value)
            if url and url not in found:
                found.append(url)
    for match in re.finditer(r"calendar\.google\.com/calendar/ical/([^/\"'<>\s]+)/public/", html or ""):
        url = _ical_url(match.group(1))
        if url and url not in found:
            found.append(url)
    for match in re.finditer(r"data-public-calendar-id=[\"']([^\"']+)[\"']", html or ""):  # Duda's calendar widget
        raw = match.group(1)
        try:
            raw = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            pass
        url = _ical_url(raw)
        if url and url not in found:
            found.append(url)
    return found[:3]


def _site(host):
    labels = (host or "").split(".")
    return ".".join(labels[-3:] if len(labels) > 2 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "org", "gov", "ac", "net") else labels[-2:])


def rank_links(anchors, base_url, limit=4, exclude=()):
    """[(score, url, text)] for the same-site links most likely to hold events, best first. `anchors` is [(url, text)]."""
    host = urlsplit(base_url).hostname
    seen, scored = set(exclude) | {base_url.split("#")[0]}, []
    for url, text in anchors:
        url = (url or "").split("#")[0]
        parts = urlsplit(url)
        if not url or url in seen or parts.scheme not in ("http", "https") or _site(parts.hostname) != _site(host):
            continue
        path = parts.path.lower()
        if path.endswith(SKIP_SUFFIX) or path.endswith(".pdf") or path in ("", "/"):
            continue
        label = (text or "").strip()
        if BAD_PATH.search(path) or (BAD_TEXT.search(label) and not GOOD_PATH.search(path)) or YEAR_ARCHIVE.search(path) or len(label) > 80:
            continue
        score = 0
        if GOOD_PATH.search(path):
            score += 3
        if GOOD_TEXT.search(label):
            score += 2
        if parts.query and not GOOD_PATH.search(path):
            score -= 1
        if score <= 0:
            continue
        seen.add(url)
        scored.append((score - 0.01 * path.count("/"), url, label))
    scored.sort(key=lambda item: -item[0])
    return scored[:limit]


def document_links(anchors, base_url, limit=6):
    """[{"url", "kind" ("pdf" or "image"), "text", "eventish"}] for linked PDFs and flyer images on this site, event-looking ones first."""
    out, seen = [], set()
    for url, text in anchors:
        url = (url or "").split("#")[0]
        path = urlsplit(url).path.lower()
        kind = "pdf" if path.endswith(".pdf") else "image" if path.endswith(IMAGE_SUFFIX) else None
        if not kind or url in seen:
            continue
        seen.add(url)
        label = (text or "").strip()[:100]
        eventish = bool(EVENTISH_DOC.search(label) or EVENTISH_DOC.search(unquote(path).replace("+", " ").replace("_", " ")))
        if kind == "image" and not (eventish and re.search(r"flyer|poster|event|calendar|save", unquote(path) + " " + label, re.I)):
            continue
        out.append({"url": url, "kind": kind, "text": label, "eventish": eventish})
    out.sort(key=lambda d: (not d["eventish"], d["kind"] != "pdf"))
    return out[:limit]
