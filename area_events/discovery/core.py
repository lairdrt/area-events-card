"""The earlier engine's plumbing: its polite fetcher, event dictionaries and feed storage. Nothing in the Discovery Service imports this module; the generic utilities it used
to hold live in timeutil, textutil and safe, and are re-exported here for the earlier engine."""
import json
import os
import time
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import urlsplit

from .safe import USER_AGENT, FetchError  # noqa: F401  (re-exported)
from .textutil import clean_text, html_to_text, repair_mojibake  # noqa: F401  (re-exported)
from .timeutil import (TIMEZONE_NAME, clock_range, localize_pacific, now_pacific, pacific_offset, parse_clock, parse_compact_utc, to_pacific)  # noqa: F401  (re-exported)

REQUEST_INTERVAL = 2.0  # seconds between requests to the same host
TIMEOUT = 20


class Fetcher:
    """GET with robots.txt checks, a fixed delay and a descriptive User-Agent."""

    def __init__(self, interval=REQUEST_INTERVAL):
        self.interval = interval
        self._last = {}
        self._robots = {}

    def _raw_get(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")

    def _allowed(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            try:
                parser.parse(self.get(f"{origin}/robots.txt", check_robots=False).splitlines())
            except FetchError:
                # No readable robots.txt: refuse rather than assume permission.
                parser.parse(["User-agent: *", "Disallow: /"])
            self._robots[origin] = parser
        return parser.can_fetch(USER_AGENT, url)

    def get(self, url, check_robots=True):
        if check_robots and not self._allowed(url):
            raise FetchError(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        wait = self._last.get(host, 0) + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return self._raw_get(url)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise FetchError(f"{url}: {error}") from error
        finally:
            self._last[host] = time.monotonic()


TEXT_FIELDS = ("title", "venue", "address", "description", "organizer", "price", "neighborhood")


def normalize_event_text(event):
    """Clean the human-readable fields of an event in place (titles, places, descriptions, organizers, tags, source label)."""
    for field in TEXT_FIELDS:
        if field in event:
            event[field] = clean_text(event[field])
    if isinstance(event.get("categories"), list):
        event["categories"] = [clean_text(c) for c in event["categories"]]
    source = event.get("source")
    if isinstance(source, dict) and isinstance(source.get("name"), str):
        source["name"] = clean_text(source["name"])
    return event


# --- storage ----------------------------------------------------------------

def save_events(document, path):
    """Atomic write so a crash never leaves a truncated feed."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def make_event(source_key, source_name, listing_url, url, title, start, **fields):
    """Uniform adapter output; absent values stay None/empty (never invented)."""
    event = {
        "id": f"{source_key}:{fields.pop('local_id')}",
        "title": title,
        "start": start.isoformat(),
        "end": None, "timezone": TIMEZONE_NAME,
        "venue": None, "address": None, "description": None,
        "image_url": None, "image_source": None,
        "url": url, "categories": [], "price": None,
        "source": {"name": source_name, "listing_url": listing_url},
        "retrieved_at": None,
        "verified": False,
        "verification": {"status": "unverified", "evidence": [], "missing": [], "note": ""},
        "extraction": {"detail_page": False},
    }
    event.update(fields)
    return event


def finish_event(event, retrieved_at, evidence, corroborated):
    """Fill the verification block and the missing-field list."""
    event["retrieved_at"] = retrieved_at
    event["verification"] = {
        "status": "corroborated" if corroborated else "unverified",
        "evidence": evidence,
        "missing": [f for f in ("end", "venue", "address", "description", "image_url") if not event.get(f)],
        "note": "Single-source; verified stays false until independently confirmed.",
    }
    return event
