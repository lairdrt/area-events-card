"""Generic website adapter: events from any public page a person points the card at.

No per-site code. A page is read in stages, preferring reliable structured data over guesswork:

  A  schema.org Event data in the page: JSON-LD (including @graph, lists and nested events) and microdata
  B  public calendar feeds the page links to: iCalendar (ICS) and RSS/Atom feeds that state event dates
  C  only when A and B found nothing: repeated listing blocks with a <time datetime> and a titled link
  D  a limited number of same-site event pages, to complete listings or to follow links the lists gave

The address may also be an ICS or RSS/Atom feed itself. Nothing is invented: events with no time, no usable time
zone, an end in the past, or an online-only venue are skipped (and counted in the notes). All requests go through
SafeFetcher (public addresses only, robots.txt, bounded size/time/redirects); no JavaScript is run.
The crawl of a site is cached briefly, so profiles that share a website do not fetch it again.
"""
import copy
import hashlib
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from ..core import FetchError, finish_event, make_event
from ..safe import BlockedError, SafeFetcher
from . import webdiscover, webfeeds, webparse, webtext
from .webtime import parse_when

ACCEPTS_LOCATION = True
MULTIPLE = True
MAX_REQUESTS = 30
MAX_DETAIL = 12
TIME_BUDGET = 100  # seconds one website may take in total (requests are spaced out politely, so this bounds the page count)
MAX_PAGES = 3
MAX_FEEDS = 3
MAX_HUBS = 4        # pages a site's own links point to as its calendar or events, read when the page given has no events itself
MAX_DOCS = 2        # linked event PDFs read for their text
MAX_EMBEDS = 2      # embedded public calendars whose feed is tried
BLOCKS = {"li", "p", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "div", "td", "article", "section", "blockquote", "span"}
SKIP_AREAS = {"nav", "footer", "header", "aside", "script", "style", "noscript", "form"}
CACHE_SECONDS = 900
CACHE_ENTRIES = 24

_cache, _cache_lock = {}, threading.Lock()


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _site(host):
    labels = (host or "").split(".")
    if len(labels) > 2 and labels[-1].__len__() == 2 and labels[-2] in ("co", "com", "org", "gov", "ac", "net"):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _client(fetcher):
    return fetcher if hasattr(fetcher, "fetch") else SafeFetcher(getattr(fetcher, "interval", 2.0))


class _Crawl:
    def __init__(self, client, url):
        self.client, self.url, self.host = client, url, urlsplit(url).hostname
        self.requests = 0
        self.notes = {"format": None, "formats": {}, "requests": 0, "pages": 0, "feeds": [], "details_fetched": 0, "failed_fetches": 0,
                      "malformed_jsonld": 0, "recurrence_unsupported": 0}
        self.candidates, self.name, self.javascript_hint = [], None, False
        self.fetched, self.pending_links, self.docs, self.last_error = set(), [], [], None
        self.notes.update(discovered=[], documents=[], embedded_calendars=[])
        self.deadline = time.monotonic() + TIME_BUDGET

    def fetch(self, url, required=False):
        if url in self.fetched or (not required and (self.requests >= MAX_REQUESTS or time.monotonic() > self.deadline)):
            if url not in self.fetched:
                self.notes["limit_reached"] = True
            return None
        self.requests += 1
        self.fetched.add(url)
        self.notes["requests"] = self.requests
        try:
            return self.client.fetch(url)
        except FetchError as error:
            self.last_error = str(error)[:200]
            if required:
                raise
            self.notes["failed_fetches"] += 1
            return None

    def fetch_binary(self, url, types):
        """A non-text document (a PDF), counted against the same request and time limits."""
        if url in self.fetched or self.requests >= MAX_REQUESTS or time.monotonic() > self.deadline:
            self.notes["limit_reached"] = True
            return None
        self.requests += 1
        self.fetched.add(url)
        self.notes["requests"] = self.requests
        try:
            return self.client.fetch(url, binary=types)
        except FetchError as error:
            self.last_error = str(error)[:200]
            self.notes["failed_fetches"] += 1
            return None

    def _queue(self, links):
        """Event pages worth a look later: this website only, never another domain."""
        self.pending_links += [l for l in links if _site(urlsplit(l).hostname) == _site(self.host) and l not in self.pending_links]

    def add(self, kind, candidates):
        if candidates:
            self.notes["formats"][kind] = self.notes["formats"].get(kind, 0) + len(candidates)
            self.notes["format"] = self.notes["format"] or kind
            self.candidates.extend(candidates)

    # --- stages ---------------------------------------------------------------------------------------

    def run(self):
        response = self.fetch(self.url, required=True)
        self.url = response.url
        self.host = urlsplit(self.url).hostname
        text = response.text
        if webfeeds.looks_like_ics(text):
            self._ics(response.url, text)
        elif text.lstrip("﻿ \r\n\t").startswith("<") and re.search(r"<(rss|feed|rdf:RDF)\b", text[:2000]) and "<html" not in text[:500].lower():
            self._feed(response.url, text)
        elif "html" in response.content_type.lower() or "<html" in text[:2000].lower() or "<!doctype" in text[:200].lower() or not response.content_type:
            self._html(response)
        else:
            self.notes["unsupported_content"] = response.content_type or "unknown"
        self._documents()
        return self.candidates, self.notes

    def _ics(self, url, text):
        horizon = datetime.now() + timedelta(days=400)
        found, notes = webfeeds.parse_ics(text, horizon)
        self.notes["recurrence_unsupported"] += notes["recurrence_unsupported"]
        self.name = self.name or notes.get("calendar_name")
        self.notes["format"] = self.notes["format"] or "ics"  # a calendar with no events is still a calendar
        self.add("ics", found)

    def _feed(self, url, text):
        try:
            found, links, title = webfeeds.parse_feed(text, url)
        except ValueError:
            self.notes["failed_fetches"] += 1
            return
        self.name = self.name or title
        self.notes["format"] = self.notes["format"] or "feed"
        self.add("feed", found)
        self._queue(links)

    def _feeds(self, page):
        for url, kind in page.feeds[:MAX_FEEDS * 2]:
            if _site(urlsplit(url).hostname) != _site(self.host) or len(self.notes["feeds"]) >= MAX_FEEDS:
                continue
            answer = self.fetch(url)
            if answer is None:
                continue
            self.notes["feeds"].append(url)
            if kind == "ics" or webfeeds.looks_like_ics(answer.text):
                self._ics(answer.url, answer.text)
            else:
                self._feed(answer.url, answer.text)

    def _html(self, response):
        text = webdiscover.unwrap(response.text)  # some site builders wrap the page's HTML in JSON
        page = webparse.scan(text, response.url)
        self.name = self.name or page.site_name
        self.notes["malformed_jsonld"] += page.malformed
        self._queue(webparse.detail_links(page, self.host))
        self.add("jsonld", page.jsonld)
        self.add("microdata", page.micro)
        self._feeds(page)
        if not self.candidates:
            cards = webparse.html_cards(page.root, response.url)
            self.add("html", cards)
        if self.candidates:
            self._paginate(page)
        else:
            self._embedded(text)
            if not self.candidates:
                self._hubs(page, response.url)
            if not self.candidates:
                self._text(page, response.url)
        self._collect_documents(page, response.url)
        if not self.candidates and not self.notes["discovered"] and not self.notes["documents"]:
            body = " ".join(n.text() for n in page.root.walk() if n.tag == "body")
            scripts = sum(1 for n in page.root.walk() if n.tag == "script" and n.attrs.get("src"))
            self.javascript_hint = len(body) < 400 and scripts >= 2 or bool(re.search(r"enable javascript|requires javascript", text, re.I))
            self.notes["javascript_hint"] = self.javascript_hint
        self.notes["pages"] += 1

    # --- discovery: where this website keeps its events -------------------------------------------------------

    def _embedded(self, text):
        """Public calendars embedded in the page: their published feed is tried like any other (robots.txt and all limits apply)."""
        for url in webdiscover.embedded_calendars(text)[:MAX_EMBEDS]:
            if url in self.fetched:
                continue
            answer = self.fetch(url)
            entry = {"url": url, "read": answer is not None}
            if answer is None:
                entry["error"] = self.last_error
            else:
                self._ics(answer.url, answer.text)
            self.notes["embedded_calendars"].append(entry)

    def _hubs(self, page, base):
        """One hop: the same-site pages that look like a calendar, upcoming events or announcements (best first, a few at most)."""
        for score, url, label in webdiscover.rank_links(page.anchors, base, MAX_HUBS, exclude=self.fetched):
            answer = self.fetch(url)
            if answer is None:
                continue
            hub_text = webdiscover.unwrap(answer.text)
            if "<" not in hub_text:
                continue
            hub = webparse.scan(hub_text, answer.url)
            self.notes["pages"] += 1
            self.notes["malformed_jsonld"] += hub.malformed
            before = len(self.candidates)
            self.add("jsonld", hub.jsonld)
            self.add("microdata", hub.micro)
            self._feeds(hub)
            if len(self.candidates) == before:
                self.add("html", webparse.html_cards(hub.root, answer.url))
            if len(self.candidates) == before:
                self._embedded(hub_text)
            if len(self.candidates) == before:
                self._text(hub, answer.url)
            self._queue(webparse.detail_links(hub, self.host))
            self._collect_documents(hub, answer.url)
            self.notes["discovered"].append({"url": answer.url, "score": round(score, 2), "events": len(self.candidates) - before})

    def _lines(self, page):
        """The page's readable text as lines: leaf blocks only, and never navigation, headers, footers or forms."""
        nodes = list(page.root.walk())
        holds_blocks = set()
        for node in nodes:
            if node.tag in BLOCKS and node.tag != "span":
                parent = node.parent
                while parent is not None and id(parent) not in holds_blocks:
                    holds_blocks.add(id(parent))
                    parent = parent.parent
        skipped = {}

        def in_skipped_area(node):
            if node is None:
                return False
            if id(node) not in skipped:
                skipped[id(node)] = node.tag in SKIP_AREAS or in_skipped_area(node.parent)
            return skipped[id(node)]

        lines = []
        for node in nodes:
            if node.tag not in BLOCKS or id(node) in holds_blocks or in_skipped_area(node):
                continue
            text = node.text()
            if text and text not in lines[-3:]:
                lines.append(text)
        return lines

    def _text(self, page, url):
        """Events written in the page's text (only when nothing better was found): a date and a start time with a title of their own."""
        today = datetime.now(timezone.utc).date()
        found = webtext.events_in(self._lines(page), today, url)
        for candidate in found:
            candidate["url"] = url
        self.add("text", found)

    def _collect_documents(self, page, url):
        for doc in webdiscover.document_links(page.anchors, url):
            if all(doc["url"] != known["url"] for known in self.docs):
                self.docs.append(doc)

    def _documents(self):
        """Linked event PDFs: text-based ones are read for events; image-only PDFs and flyer images are kept as links for later extraction."""
        today = datetime.now(timezone.utc).date()
        read = 0
        for doc in self.docs[:6]:
            entry = {"url": doc["url"], "kind": doc["kind"], "text": doc["text"], "extracted": False}
            if doc["kind"] == "pdf" and doc["eventish"] and read < MAX_DOCS:
                answer = self.fetch_binary(doc["url"], ("application/pdf", "application/x-pdf", "application/octet-stream"))
                read += 1
                if answer is None:
                    entry["reason"] = self.last_error or "not fetched"
                else:
                    lines, reason = webtext.pdf_text(answer.body)
                    if lines:
                        found = webtext.events_in(lines, today, doc["url"])
                        for candidate in found:
                            candidate["kind"] = "pdf"
                        self.add("pdf", found)
                        entry.update(extracted=True, events=len(found))
                    else:
                        entry["reason"] = reason
            else:
                entry["reason"] = "not read (not an event document, or the limit was reached)" if doc["kind"] == "pdf" else "an image: needs text recognition, which is not done"
            self.notes["documents"].append(entry)

    def _paginate(self, page):
        wanted_kind = self.notes["format"]
        url = page.next_url
        for _ in range(MAX_PAGES - 1):
            if not url or urlsplit(url).hostname != self.host:
                return
            answer = self.fetch(url)
            if answer is None:
                return
            nxt = webparse.scan(answer.text, answer.url)
            self.notes["pages"] += 1
            self.notes["malformed_jsonld"] += nxt.malformed
            self._queue(webparse.detail_links(nxt, self.host))
            if wanted_kind == "jsonld":
                self.add("jsonld", nxt.jsonld)
            elif wanted_kind == "microdata":
                self.add("microdata", nxt.micro)
            elif wanted_kind == "html":
                self.add("html", webparse.html_cards(nxt.root, answer.url))
            url = nxt.next_url

    def details(self, enabled):
        """Stage D: complete listing entries from their own pages and follow event links that carried no data."""
        if not enabled:
            return
        budget = MAX_DETAIL
        known = {c.get("url") for c in self.candidates}
        def local(c):
            return bool(c.get("url")) and _site(urlsplit(c["url"]).hostname) == _site(self.host)

        # listing entries missing everything useful come first, then those missing only a venue or only a picture
        needy = sorted((c for c in self.candidates if local(c) and (not (c.get("venue") or c.get("address")) or not c.get("image"))),
                       key=lambda c: sum(1 for f in ("venue", "image") if c.get(f)))
        for candidate in needy:
            if budget <= 0:
                break
            answer = self.fetch(candidate["url"])
            if answer is None:
                continue
            budget -= 1
            self.notes["details_fetched"] += 1
            self._enrich(candidate, answer)
        for link in self.pending_links:
            if budget <= 0 or link in known:
                continue
            answer = self.fetch(link)
            if answer is None:
                continue
            budget -= 1
            self.notes["details_fetched"] += 1
            page = webparse.scan(answer.text, answer.url)
            self.notes["malformed_jsonld"] += page.malformed
            found = page.jsonld or page.micro
            for candidate in found:
                candidate["url"] = candidate["url"] or answer.url
                candidate["detail"] = True
            if found:
                self.notes["formats"]["detail"] = self.notes["formats"].get("detail", 0) + len(found)
                self.notes["format"] = self.notes["format"] or ("jsonld" if page.jsonld else "microdata")
            self.candidates.extend(found)

    def _enrich(self, candidate, answer):
        page = webparse.scan(answer.text, answer.url)
        events = page.jsonld or page.micro
        match = next((e for e in events if e["start"] == candidate.get("start") or e.get("url") == candidate.get("url")), None)
        if match is None and len(events) == 1:
            match = events[0]
        if match is None:
            return
        for field in ("venue", "address", "description", "organizer", "geo", "price", "end", "image", "image_source"):
            if not candidate.get(field) and match.get(field):
                candidate[field] = match[field]
        if not candidate.get("categories") and match.get("categories"):
            candidate["categories"] = match["categories"]
        if not candidate.get("image"):
            og = webparse.absolute(answer.url, page.meta.get("og:image"))
            if og and len(events) <= 1:
                candidate["image"], candidate["image_source"] = og, "og:image of the event page"
        candidate["detail"] = True


def crawl(fetcher, url, use_details=True):
    """(candidates, notes, name) for a website, from the shared short-lived cache when another profile just read it."""
    with _cache_lock:
        hit = _cache.get((url, use_details))
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return copy.deepcopy(hit[1]), copy.deepcopy(hit[2]), hit[3]
    run = _Crawl(_client(fetcher), url)
    candidates, notes = run.run()
    run.details(use_details)
    notes["candidates"] = len(candidates)
    result = (copy.deepcopy(candidates), copy.deepcopy(notes), run.name)
    with _cache_lock:
        if len(_cache) >= CACHE_ENTRIES:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[(url, use_details)] = (time.monotonic(),) + result[:3]
    return candidates, notes, run.name


def _midnight_only(candidate, start):
    """Listing markup often writes a bare date as <time datetime="2026-10-22T00:00:00+00:00">: that is not a stated start time."""
    return candidate.get("kind") in ("html", "microdata") and (start.hour, start.minute, start.second) == (0, 0, 0)


def _has_time(candidate):
    match = re.search(r"[T ](\d{2}):?(\d{2})", candidate.get("start") or "")
    return bool(match) and not (candidate.get("kind") in ("html", "microdata") and match.groups() == ("00", "00"))


def probe(fetcher, url):
    """Is there anything to read at `url`? A dict with status (supported, unsupported, blocked, unavailable, no_events) and a reason."""
    try:
        candidates, notes, name = crawl(fetcher, url)
    except BlockedError as error:
        return {"status": "blocked", "reason": str(error)[:200]}
    except FetchError as error:
        return {"status": "unavailable", "reason": f"could not be read: {error}"[:200]}
    usable = [c for c in candidates if _has_time(c)]
    if usable:
        return {"status": "supported", "format": notes["format"], "events": len(usable), "name": name}
    if notes.get("unsupported_content"):
        return {"status": "unsupported", "reason": f"the address returns {notes['unsupported_content']}, not a web page or calendar feed"}
    if candidates:
        return {"status": "no_events", "reason": "events were found, but none states a start time", "format": notes["format"]}
    if notes["format"]:
        return {"status": "no_events", "reason": "no upcoming events were found", "format": notes["format"]}
    blocked = [e for e in notes.get("embedded_calendars", []) if not e.get("read")]
    if blocked:
        return {"status": "unsupported", "reason": "the events are in an embedded public calendar whose published feed cannot be read ("
                + (blocked[0].get("error") or "unavailable") + ")"}
    if notes.get("javascript_hint"):
        return {"status": "unsupported", "reason": "the page builds its events with JavaScript, which Area Events does not run, and it offers no calendar feed"}
    return {"status": "unsupported", "reason": "no event data was found (no Schema.org events, calendar feed or event listing)"}


# --- candidates -> events ---------------------------------------------------------------------------------

def _richness(event):
    return sum(1 for f in ("end", "venue", "address", "description", "image_url") if event.get(f))


def _words(title):
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) > 2}


def _without_restated(candidates):
    """Events read from page text or a PDF are dropped when a structured candidate already states the same event (same day and hour, a
    similar title): the weaker reading never adds a second copy and never overrides the stronger one."""
    strong = [c for c in candidates if c["kind"] not in ("text", "pdf")]
    if len(strong) == len(candidates):
        return candidates
    kept = list(strong)
    for c in candidates:
        if c["kind"] not in ("text", "pdf"):
            continue
        mine = _words(c["title"])
        same = False
        for other in strong + [k for k in kept if k["kind"] in ("text", "pdf")]:
            if (other.get("start") or "")[:13] != (c.get("start") or "")[:13]:
                continue
            theirs = _words(other["title"])
            if mine and theirs and len(mine & theirs) / len(mine | theirs) >= 0.5:
                same = True
                break
        if not same:
            kept.append(c)
    return kept


def discover(fetcher, previous, details=True, limit=None, days=60, source_id="web", source_name="Website", url=None, location=None, **_):
    """Return (events, notes). `url` is the page, ICS feed or RSS/Atom feed to read."""
    candidates, notes, site_name = crawl(fetcher, url, details)
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=int(days))
    zone_name = (location or {}).get("timezone")
    retrieved_at = now.isoformat(timespec="seconds")
    skipped = {}
    best = {}
    candidates = _without_restated(candidates)
    for c in candidates:
        if len(best) >= (limit or 10_000):
            break
        reason = None
        if c.get("cancelled"):
            reason = "cancelled"
        elif c.get("online"):
            reason = "online_only"
        else:
            start, reason = parse_when(c.get("start"), c.get("tz"), zone_name)
            if reason is None and _midnight_only(c, start):
                reason = "date_only"
        if reason is None:
            end, _ = parse_when(c.get("end"), c.get("tz"), zone_name) if c.get("end") else (None, None)
            if end is not None and end < start:
                end = None
            if (end or start) < now:
                reason = "ended"
            elif start > horizon:
                reason = "beyond_horizon"
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        key = ((c["title"] or "").casefold(), start.astimezone(timezone.utc).isoformat())
        identity = hashlib.sha1("|".join([c.get("uid") or "", c.get("url") or "", key[0], key[1]]).encode()).hexdigest()[:12]
        event = make_event(
            source_id, source_name, url, c.get("url") or url, c["title"], start, local_id=identity, end=end.isoformat() if end else None,
            venue=c.get("venue"), address=c.get("address"), description=c.get("description"), categories=list(c.get("categories") or []),
            image_url=c.get("image"), image_source=c.get("image_source"), price=c.get("price"))
        event["timezone"] = c.get("tz") or zone_name
        if c.get("organizer"):
            event["organizer"] = c["organizer"]
        if c.get("geo"):
            event["geo"] = c["geo"]
        event["extraction"] = {"detail_page": bool(c.get("detail")), "method": c["kind"]}
        if c.get("evidence_text"):
            event["extraction"]["source_text"] = c["evidence_text"][:400]  # the exact text a text or PDF event was read from
        evidence = [f"start time stated by the {urlsplit(url).hostname} page ({c['kind']}: {c['start']})"]
        if c.get("geo"):
            evidence.append(f"venue coordinates from the page ({c['geo']['latitude']}, {c['geo']['longitude']})")
        event = finish_event(event, retrieved_at, evidence, corroborated=False)
        if key in best:
            skipped["duplicate"] = skipped.get("duplicate", 0) + 1
            if _richness(event) <= _richness(best[key]):
                continue
        best[key] = event
    notes = dict(notes, skipped=skipped, site_name=site_name, days=int(days))
    return sorted(best.values(), key=lambda e: e["start"]), notes
