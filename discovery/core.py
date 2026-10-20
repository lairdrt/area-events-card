"""Shared plumbing: polite fetching, Pacific time handling, storage."""
import json
import os
import re
import time
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

USER_AGENT = "area-events-card-poc/0.1 (personal Home Assistant project; +https://github.com/lairdrt/area-events-card)"
REQUEST_INTERVAL = 2.0  # seconds between requests to the same host
TIMEOUT = 20
TIMEZONE_NAME = "America/Los_Angeles"
DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "events.json")


class FetchError(Exception):
    pass


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


# --- America/Los_Angeles without tzdata (not installed on Windows) ----------

def _nth_sunday(year, month, n):
    day = datetime(year, month, 1)
    first_sunday = day + timedelta(days=(6 - day.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def pacific_offset(utc):
    """UTC offset of America/Los_Angeles at a UTC instant (US rules since 2007)."""
    naive = utc.replace(tzinfo=None)
    dst_start = _nth_sunday(naive.year, 3, 2) + timedelta(hours=10)  # 02:00 PST
    dst_end = _nth_sunday(naive.year, 11, 1) + timedelta(hours=9)  # 02:00 PDT
    return timedelta(hours=-7 if dst_start <= naive < dst_end else -8)


def to_pacific(utc):
    return utc.astimezone(timezone(pacific_offset(utc)))


def parse_compact_utc(value):
    """'20261020T233000Z' -> aware UTC datetime."""
    return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


# --- HTML helpers -----------------------------------------------------------

class _TextExtractor(HTMLParser):
    BLOCK = {"p", "br", "li", "div", "h1", "h2", "h3", "h4", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def repair_mojibake(text):
    """Undo UTF-8 text that the source double-encoded (e.g. 'â€™' -> apostrophe)."""
    if "â€" not in text and "Ã" not in text:
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def html_to_text(fragment):
    extractor = _TextExtractor()
    extractor.feed(fragment)
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in "".join(extractor.parts).splitlines())
    return repair_mojibake("\n".join(line for line in lines if line))


# --- storage ----------------------------------------------------------------

def load_events(path=DATA_FILE):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save_events(document, path=DATA_FILE):
    """Atomic write so a crash never leaves a truncated feed."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, path)


# --- shared helpers for adapters ---------------------------------------------

def localize_pacific(naive):
    """Naive wall-clock time in America/Los_Angeles -> aware datetime."""
    for hours in (-7, -8):
        candidate = naive.replace(tzinfo=timezone(timedelta(hours=hours)))
        if pacific_offset(candidate.astimezone(timezone.utc)) == timedelta(hours=hours):
            return candidate
    return naive.replace(tzinfo=timezone(timedelta(hours=-8)))  # nonexistent hour


def parse_clock(text, default_meridiem=None):
    """'5pm', '4:30 PM', '6:30' (with default_meridiem) -> (hour, minute) or None."""
    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?m?\.?\s*", text.lower().replace("noon", "12pm"))
    if not match:
        return None
    meridiem = match.group(3) or default_meridiem
    if not meridiem:
        return None
    return int(match.group(1)) % 12 + (12 if meridiem == "p" else 0), int(match.group(2) or 0)


def clock_range(text):
    """'2 - 7pm', '9:30am - 1:30pm', '8 pm', '7:30 pm - 11:30 pm' -> (start, end|None) as (h, m)."""
    parts = [p.strip() for p in re.split(r"\s*(?:-|–|—|&ndash;|to)\s*", text.strip()) if p.strip()]
    if not parts or len(parts) > 2:
        return None, None
    end_meridiem = re.search(r"([ap])\.?m", parts[-1].lower())
    end = parse_clock(parts[-1])
    start = parse_clock(parts[0], end_meridiem.group(1) if end_meridiem else None)
    if start and len(parts) == 2 and end and not re.search(r"[ap]\.?m", parts[0].lower()) and start > end:
        start = (start[0] - 12, start[1])  # '11 - 1pm' means 11am
    return start, (end if len(parts) == 2 else None)


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


def now_pacific():
    return to_pacific(datetime.now(tz=timezone.utc))
