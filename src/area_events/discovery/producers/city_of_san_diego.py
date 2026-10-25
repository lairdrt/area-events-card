"""Producer for the City of San Diego events listing (https://www.sandiego.gov/events).

The listing is server-rendered with a title, a written date/time line and a description per event; each event page adds a location. There is no machine-readable data and no
event imagery, so images are left empty. Date lines that are not a single day with a start time (multi-day runs, "all day") are skipped rather than guessed.

This module owns everything about this source: the page reading (listing, date lines, event pages, the "Get Directions" address), the resumable detail-page work, and the
standard records it answers with. A run reads the listing, then a bounded number of event pages; pages already read come back to it as the state it left on the previous
run's records (`EventCandidate.state`), so nothing is stored anywhere but in the records the service keeps for it.
"""
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urljoin, urlsplit

from ...geolocation import textloc
from ..safe import FetchError
from ...common.textutil import html_to_text
from ...common.timeutil import TIMEZONE_NAME, clock_range, localize_pacific, now_pacific
from ..producer import EventCandidate, ProducerResult, Provenance, stated

NAME = "City of San Diego"
LISTING_URL = "https://www.sandiego.gov/events"
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_LINE = re.compile(rf"^\w+, ({MONTHS}) (\d{{1,2}}), (\d{{4}}),\s*(.+)$")
DETAIL_VERSION = 2   # 2: also reads the "Get Directions" address (events read at version 1 are read again)
REPEATED_ADDRESS = 3   # an address that heads this many different events is a general one (an office, a footer), not a venue
RETRY_BASE = 15 * 60
RETRY_MAX = 12 * 3600
_COORDINATES = re.compile(r"^\s*-?\d+\.\d+\s*,\s*-?\d+\.\d+")
_STREET_NAME = re.compile(r"((?:[NSEW]\.?\s+|North\s+|South\s+|East\s+|West\s+)?(?:[A-Z0-9][\w']*\s+){0,3}?[A-Z0-9][\w']*)\s+"
                          r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Road|Rd|Way|Lane|Ln|Court|Ct|Place|Pl|Highway|Hwy)\b")
_DIRECTIONS = ("the", "of", "and", "from", "to", "north", "south", "east", "west", "n", "s", "e", "w")


# --- reading the source's pages ----------------------------------------------------------------------------------

def _clean(value):
    return re.sub(r"\s+", " ", unescape(value)).strip() if value else None


def parse_listing(page, listing_url=LISTING_URL):
    for block in page.split('class="grid-x grid-margin-x event-listing-large"')[1:]:
        title = _clean(re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S) and re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S).group(1))
        date_text = _clean(re.search(r'class="post__date[^>]*>\s*<strong>(.*?)</strong>', block, re.S) and
                           re.search(r'class="post__date[^>]*>\s*<strong>(.*?)</strong>', block, re.S).group(1))
        link = re.search(r'<a href="(/event/[^"]+)"', block)
        info = block.split('</p>', 1)[1] if '</p>' in block else ""
        description = html_to_text(info.split('<p><a href="/event/')[0].replace("<br>", "\n")) if info else None
        if title and date_text and link:
            yield {"title": title, "date_text": date_text, "url": urljoin(listing_url, link.group(1)), "description": description or None}


def parse_start_end(date_text):
    match = DATE_LINE.match(date_text)
    if not match:
        return None, None
    start_hm, end_hm = clock_range(match.group(4))
    if not start_hm:
        return None, None
    day = datetime.strptime(f"{match.group(1)} {match.group(2)} {match.group(3)}", "%B %d %Y")
    start = localize_pacific(day.replace(hour=start_hm[0], minute=start_hm[1]))
    end = localize_pacific(day.replace(hour=end_hm[0], minute=end_hm[1])) if end_hm else None
    return start, end


def _street_words(text):
    """The lower-case name words of the streets a piece of text mentions ("4900 block of Newport Avenue" -> {"newport"})."""
    words = set()
    for match in _STREET_NAME.finditer(text or ""):
        words |= {w.lower() for w in re.findall(r"[A-Za-z0-9']+", match.group(1)) if w.lower() not in _DIRECTIONS}
    return words


def directions_address(line, location_text):
    """The street address on an event page's "Get Directions" line, or None when it is not usable as the event's: coordinates, no house
    number and street, or a street that the page's own location text does not mention while it names others (an organizer's address)."""
    line = _clean(line) or ""
    if not line or _COORDINATES.match(line):
        return None
    found = textloc.extract_addresses(line, ["San Diego"])
    if len(found) != 1:
        return None
    address = found[0]["address"]
    mentioned, own = _street_words(location_text), _street_words(re.sub(r"^\d+\s+", "", address))
    if mentioned and own and not (mentioned & own):
        return None
    if "," not in address:
        head = address
        tail = line[line.find(head) + len(head):] if head in line else ""
        place = re.match(r"^[,.\s]*([A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)?),?\s*(?:CA|Ca)\b", tail)
        address = f"{address}, {place.group(1)}, CA" if place else address
    return address


def parse_detail(page, title, start):
    text = unescape(re.sub(r"<[^>]+>", " ", page.split("<main", 1)[-1]))
    match = re.search(r"Location:\s*(.+?)(?:\s{2,}|-->|Get Directions)", text, re.S)
    day = f"{start:%B} {start.day}, {start.year}"
    venue = _clean(match.group(1)) if match else None
    line = re.search(r"Location:.*?-->\s*(.*?)\s*Get Directions", text, re.S)
    return {"venue": venue, "address": directions_address(line.group(1), venue) if line else None,
            "date_on_page": _clean(text) is not None and day in _clean(text) and title in _clean(text)}


# --- the resumable detail-page work ------------------------------------------------------------------------------

class DetailQueue:
    """An event's page is read once (one request each, so a limited number per run). Pages already read are taken from the previous run's records and never fetched again
    (until `version` changes); a page that failed is not retried at once (15 minutes, doubling, at most 12 hours) and meanwhile the budget goes to other events; events are
    taken in listing order, which is date order, so the nearest are completed first. The bookkeeping travels in each record's `state`."""

    def __init__(self, previous, budget, version, now=None):
        self.old = {record.local_id: record for record in previous}
        self.budget, self.version = budget, version
        self.now = now or datetime.now(timezone.utc)
        self.counts = {"detail_fetched": 0, "detail_reused": 0, "detail_failed": 0, "detail_deferred": 0, "detail_backoff": 0}
        self.errors = []

    def reusable(self, local_id):
        """The previous record whose page was already read (at this version), else None."""
        old = self.old.get(local_id)
        state = old.state if old is not None else {}
        return old if old is not None and state.get("detail_page") and state.get("detail_v", 1) >= self.version else None

    def reused(self):
        self.counts["detail_reused"] += 1

    def _waiting(self, local_id):
        after = (self.old[local_id].state if local_id in self.old else {}).get("detail_retry_after")
        try:
            return bool(after) and datetime.fromisoformat(after) > self.now
        except ValueError:
            return False

    def should_fetch(self, local_id):
        """May this run fetch the event's page? Counts the reason when it may not."""
        if self._waiting(local_id):
            self.counts["detail_backoff"] += 1
            return False
        if self.budget <= 0:
            self.counts["detail_deferred"] += 1
            return False
        self.budget -= 1
        return True

    def done(self):
        self.counts["detail_fetched"] += 1

    def failed(self, url, error):
        self.counts["detail_failed"] += 1
        self.errors.append(f"{url}: {error}"[:300])

    def next_state(self, local_id, completed, failed=False):
        """The bookkeeping after this run: completed (with the version), or the failure count and when to try again."""
        state = {"detail_page": bool(completed)}
        if completed:
            state["detail_v"] = self.version
            return state
        previous = self.old[local_id].state if local_id in self.old else {}
        failures = int(previous.get("detail_failures") or 0)
        if failed:
            failures += 1
            state["detail_retry_after"] = (self.now + timedelta(seconds=min(RETRY_BASE * 2 ** (failures - 1), RETRY_MAX))).isoformat(timespec="seconds")
        elif previous.get("detail_retry_after"):
            state["detail_retry_after"] = previous["detail_retry_after"]   # still backing off: carry it forward
        if failures:
            state["detail_failures"] = failures
        return state

    def notes(self, notes):
        notes.update(self.counts)
        if self.errors:
            notes["detail_errors"] = self.errors[:5]
        return notes


# --- the producer ------------------------------------------------------------------------------------------------

class CityOfSanDiegoProducer:
    adapter = "sandiego_gov"
    extractor = f"producers/city_of_san_diego@v{DETAIL_VERSION}"
    method = "adapter"

    @staticmethod
    def validate_options(options):
        """Validate this producer's configured instance and return its URL and crawl options."""
        if any(not isinstance(key, str) for key in options):
            raise ValueError("option names must be text")
        unknown = sorted(set(options) - {"url", "pages", "max_details_per_run"})
        if unknown:
            raise ValueError(f"unsupported sandiego_gov options: {', '.join(unknown)[:100]}")
        url = options.get("url", LISTING_URL)
        try:
            parts = urlsplit(url) if isinstance(url, str) else None
            host = parts.hostname if parts else None
            port = parts.port if parts else None
        except ValueError:
            parts, host, port = None, None, None
        if not parts or parts.scheme not in ("http", "https") or not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment or (port is not None and not 1 <= port <= 65535):
            raise ValueError("options.url must be an http(s) site address without credentials, query or fragment")
        clean_options = {}
        for key, value, low, high in (("pages", options.get("pages", 2), 1, 20),
                                      ("max_details_per_run", options.get("max_details_per_run", 10), 0, 100)):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key} must be an integer between {low} and {high}")
            if key in options:
                clean_options[key] = value
        return url.rstrip("/"), clean_options

    def crawl(self, fetcher, previous, options):
        source_name, url = options.get("name", NAME), options.get("url", LISTING_URL)
        pages = int(options.get("pages", 2))
        retrieved_at = now_pacific().isoformat(timespec="seconds")
        now = now_pacific()
        queue = DetailQueue(previous, int(options.get("max_details_per_run", 10)), DETAIL_VERSION)
        notes = {"pages": pages, "listing_items": 0, "unparsed_date_skipped": 0, "past_skipped": 0}
        rows, seen, pages_ok = [], set(), 0
        for number in range(pages):
            page_url = url if number == 0 else f"{url}?page={number}"
            try:
                page = fetcher.get(page_url)
            except Exception as error:
                notes.setdefault("listing_errors", []).append(str(error))
                continue
            pages_ok += 1
            for item in parse_listing(page, url):
                notes["listing_items"] += 1
                start, end = parse_start_end(item["date_text"])
                if not start:
                    notes["unparsed_date_skipped"] += 1
                    continue
                if (end or start) < now:
                    notes["past_skipped"] += 1
                    continue
                local_id = f"{item['url'].rstrip('/').rsplit('/', 1)[-1]}@{start.date().isoformat()}"
                if local_id in seen:
                    continue
                seen.add(local_id)
                failed, detail, old = False, None, queue.reusable(local_id)
                if old:
                    detail = {"venue": old.venue, "address": old.address, "date_on_page": old.verification.status == "corroborated"}
                    queue.reused()
                elif queue.should_fetch(local_id):   # what is not fetched now is picked up by a later refresh
                    try:
                        detail = parse_detail(fetcher.get(item["url"]), item["title"], start)
                        queue.done()
                    except Exception as error:
                        queue.failed(item["url"], error)
                        failed = True
                evidence = [f"listing date line '{item['date_text']}' parsed as a single-day Pacific time start"]
                date_on_page = bool(detail and detail.get("date_on_page"))
                if date_on_page:
                    evidence.append("event page repeats the title and the event date")
                if (detail or {}).get("address"):
                    evidence.append("address from the event page's Get Directions line (it names a street that the page's location text also names)")
                rows.append({"local_id": local_id, "title": item["title"], "start": start.isoformat(), "end": end.isoformat() if end else None, "url": item["url"],
                             "venue": (detail or {}).get("venue"), "address": (detail or {}).get("address"), "description": item["description"], "evidence": evidence,
                             "corroborated": date_on_page, "state": queue.next_state(local_id, detail is not None, failed)})
        if not pages_ok:
            raise FetchError("; ".join(notes.get("listing_errors", ["no listing page readable"])))
        titles = {}
        for row in rows:
            if row["address"]:
                titles.setdefault(row["address"].lower(), set()).add(row["title"])
        general = {address for address, names in titles.items() if len(names) >= REPEATED_ADDRESS}
        for row in rows:
            if row["address"] and row["address"].lower() in general:
                row["address"] = None   # the same address on several unrelated events is not a venue
                row["evidence"] = [e for e in row["evidence"] if "Get Directions" not in e]
                notes["general_address_dropped"] = notes.get("general_address_dropped", 0) + 1
        provenance = Provenance(source_name, url, retrieved_at)
        candidates = [EventCandidate(local_id=row["local_id"], title=row["title"], starts_at=row["start"], ends_at=row["end"], timezone=TIMEZONE_NAME, venue=row["venue"],
                                     address=row["address"], description=row["description"], url=row["url"], provenance=provenance, state=row["state"],
                                     verification=stated(row["evidence"], row["corroborated"], row["end"], row["venue"], row["address"], row["description"], None))
                      for row in rows]
        return ProducerResult(tuple(candidates), (), queue.notes(notes))

    @staticmethod
    def page_role(url, base_url):
        return "listing" if url.split("?")[0].rstrip("/") == base_url.split("?")[0].rstrip("/") else "detail"
