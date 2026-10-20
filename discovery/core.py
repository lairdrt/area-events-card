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
