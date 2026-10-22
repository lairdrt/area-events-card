"""City of San Diego events listing adapter (https://www.sandiego.gov/events).

The listing is server-rendered with a title, a written date/time line and a
description per event; the event page adds a location. There is no
machine-readable data and no event imagery, so images are left empty. Date
lines that are not a single day with a start time (multi-day runs, "all day")
are skipped rather than guessed.
"""
import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin

from .. import textloc
from ..core import FetchError, clock_range, finish_event, html_to_text, localize_pacific, make_event, now_pacific
from .detailwork import DetailWork

KEY = "sandiego_gov"
NAME = "City of San Diego"
LISTING_URL = "https://www.sandiego.gov/events"
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_LINE = re.compile(rf"^\w+, ({MONTHS}) (\d{{1,2}}), (\d{{4}}),\s*(.+)$")


def _clean(value):
    return re.sub(r"\s+", " ", unescape(value)).strip() if value else None


def _parse_listing(page, listing_url=LISTING_URL):
    for block in page.split('class="grid-x grid-margin-x event-listing-large"')[1:]:
        title = _clean(re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S) and re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S).group(1))
        date_text = _clean(re.search(r'class="post__date[^>]*>\s*<strong>(.*?)</strong>', block, re.S) and
                           re.search(r'class="post__date[^>]*>\s*<strong>(.*?)</strong>', block, re.S).group(1))
        link = re.search(r'<a href="(/event/[^"]+)"', block)
        info = block.split('</p>', 1)[1] if '</p>' in block else ""
        description = html_to_text(info.split('<p><a href="/event/')[0].replace("<br>", "\n")) if info else None
        if title and date_text and link:
            yield {"title": title, "date_text": date_text, "url": urljoin(listing_url, link.group(1)), "description": description or None}


def _parse_start_end(date_text):
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


DETAIL_VERSION = 2  # 2: also reads the "Get Directions" address (events read at version 1 are read again)
REPEATED_ADDRESS = 3  # an address that heads this many different events is a general one (an office, a footer), not a venue
_COORDINATES = re.compile(r"^\s*-?\d+\.\d+\s*,\s*-?\d+\.\d+")
_STREET_NAME = re.compile(r"((?:[NSEW]\.?\s+|North\s+|South\s+|East\s+|West\s+)?(?:[A-Z0-9][\w']*\s+){0,3}?[A-Z0-9][\w']*)\s+"
                          r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Road|Rd|Way|Lane|Ln|Court|Ct|Place|Pl|Highway|Hwy)\b")
_DIRECTIONS = ("the", "of", "and", "from", "to", "north", "south", "east", "west", "n", "s", "e", "w")


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


def _parse_detail(page, title, start):
    text = unescape(re.sub(r"<[^>]+>", " ", page.split("<main", 1)[-1]))
    match = re.search(r"Location:\s*(.+?)(?:\s{2,}|-->|Get Directions)", text, re.S)
    day = f"{start:%B} {start.day}, {start.year}"
    venue = _clean(match.group(1)) if match else None
    line = re.search(r"Location:.*?-->\s*(.*?)\s*Get Directions", text, re.S)
    return {"venue": venue, "address": directions_address(line.group(1), venue) if line else None,
            "date_on_page": _clean(text) is not None and day in _clean(text) and title in _clean(text)}


def discover(fetcher, previous, details=True, limit=None, pages=4, max_details_per_run=20, source_id=KEY, source_name=NAME, url=LISTING_URL, **_):
    retrieved_at = now_pacific().isoformat(timespec="seconds")
    now = now_pacific()
    work = DetailWork(previous, lambda e: e["id"], max_details_per_run, enabled=details, version=DETAIL_VERSION)
    notes = {"pages": pages, "listing_items": 0, "unparsed_date_skipped": 0, "past_skipped": 0}
    events, seen, pages_ok = [], set(), 0

    for number in range(pages):
        page_url = url if number == 0 else f"{url}?page={number}"
        try:
            page = fetcher.get(page_url)
        except Exception as error:
            notes.setdefault("listing_errors", []).append(str(error))
            continue
        pages_ok += 1
        for item in _parse_listing(page, url):
            notes["listing_items"] += 1
            start, end = _parse_start_end(item["date_text"])
            if not start:
                notes["unparsed_date_skipped"] += 1
                continue
            if (end or start) < now:
                notes["past_skipped"] += 1
                continue
            slug = item["url"].rstrip("/").rsplit("/", 1)[-1]
            local_id = f"{slug}@{start.date().isoformat()}"
            if local_id in seen or (limit is not None and len(events) >= limit):
                continue
            seen.add(local_id)

            ident, failed = f"{source_id}:{local_id}", False
            detail, old = None, work.reusable(ident)
            if old:
                detail = {"venue": old["venue"], "address": old.get("address"), "date_on_page": old["verification"]["status"] == "corroborated"}
                work.reused()
            elif work.should_fetch(ident):  # what is not fetched now is picked up by a later refresh
                try:
                    detail = _parse_detail(fetcher.get(item["url"]), item["title"], start)
                    work.done()
                except Exception as error:
                    work.failed(ident, item["url"], error)
                    failed = True

            event = make_event(source_id, source_name, url, item["url"], item["title"], start, local_id=local_id,
                               end=end.isoformat() if end else None, venue=(detail or {}).get("venue"),
                               address=(detail or {}).get("address"), description=item["description"])
            work.stamp(event, ident, detail is not None, failed)
            evidence = [f"listing date line '{item['date_text']}' parsed as a single-day Pacific time start"]
            date_on_page = bool(detail and detail.get("date_on_page"))
            if date_on_page:
                evidence.append("event page repeats the title and the event date")
            if event.get("address"):
                evidence.append("address from the event page's Get Directions line (it names a street that the page's location text also names)")
            finish_event(event, retrieved_at, evidence, corroborated=date_on_page)
            events.append(event)
    if not pages_ok:
        raise FetchError("; ".join(notes.get("listing_errors", ["no listing page readable"])))
    titles = {}
    for event in events:
        if event.get("address"):
            titles.setdefault(event["address"].lower(), set()).add(event["title"])
    general = {address for address, names in titles.items() if len(names) >= REPEATED_ADDRESS}
    for event in events:
        if event.get("address") and event["address"].lower() in general:
            event["address"] = None  # the same address on several unrelated events is not a venue
            event["verification"]["missing"] = [f for f in ("end", "venue", "address", "description", "image_url") if not event.get(f)]
            event["verification"]["evidence"] = [e for e in event["verification"]["evidence"] if "Get Directions" not in e]
            notes["general_address_dropped"] = notes.get("general_address_dropped", 0) + 1
    return events, work.notes(notes)
